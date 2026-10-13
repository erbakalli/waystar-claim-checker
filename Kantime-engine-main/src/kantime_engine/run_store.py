"""Crash-safe, encrypted SQLite state for spreadsheet automation runs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import shutil
import sqlite3
from typing import Any, Iterable, Iterator, Mapping
import uuid

from .api import RowDisposition, RowRecord, RowResult
from .paths import ApplicationPaths
from .secure_data import protect, unprotect


RUN_SCHEMA_VERSION = 1
RESUMABLE_RUN_STATES = ("PREPARED", "RUNNING", "PAUSED", "FAILED")
COMPLETE_ROW_STATES = ("RESOLVED", "SKIPPED")
PENDING_ROW_STATES = ("PENDING", "RETRY_LATER")


class RunStoreError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _row_key_hash(run_id: str, key: str) -> str:
    return hashlib.sha256(f"{run_id}\0{key}".encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RunInfo:
    id: str
    plugin_id: str
    variant_id: str
    automation_name: str
    source_path: str
    source_hash: str
    source_ext: str
    snapshot_path: str
    output_path: str
    sheet_name: str
    header_row: int
    first_col: int
    schema_hash: str
    status: str
    row_count: int
    created_at: str
    updated_at: str
    completed_at: str | None


@dataclass(frozen=True)
class PersistedRow:
    ordinal: int
    source_row: int
    key_hash: str
    inputs: Mapping[str, Any]
    result: Mapping[str, Any]
    comment: str | None
    status: str
    disposition: str | None


class RunStore:
    def __init__(
        self,
        path: str | Path | None = None,
        *,
        app_paths: ApplicationPaths | None = None,
    ) -> None:
        if path is None and app_paths is None:
            raise ValueError("RunStore requires an explicit path or ApplicationPaths")
        self.app_paths = app_paths
        self.path = Path(path) if path is not None else app_paths.database
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=10.0)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA busy_timeout=10000")
        self.connection.execute("PRAGMA secure_delete=ON")
        self._initialize()

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                plugin_id TEXT NOT NULL,
                variant_id TEXT NOT NULL,
                automation_name TEXT NOT NULL,
                source_path TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                source_ext TEXT NOT NULL,
                snapshot_path TEXT NOT NULL,
                output_path TEXT NOT NULL,
                sheet_name TEXT NOT NULL,
                header_row INTEGER NOT NULL,
                first_col INTEGER NOT NULL,
                headers_blob BLOB NOT NULL,
                column_map_blob BLOB NOT NULL,
                statistics_blob BLOB NOT NULL,
                schema_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                row_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                schema_version INTEGER NOT NULL,
                is_prepared INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS ix_runs_identity
                ON runs(plugin_id, variant_id, source_hash, sheet_name, schema_hash, updated_at);
            CREATE TABLE IF NOT EXISTS rows (
                run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                source_row INTEGER NOT NULL,
                key_hash TEXT NOT NULL,
                input_blob BLOB NOT NULL,
                result_blob BLOB,
                status TEXT NOT NULL,
                disposition TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_batch_id TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(run_id, ordinal),
                UNIQUE(run_id, source_row)
            );
            CREATE INDEX IF NOT EXISTS ix_rows_pending
                ON rows(run_id, status, ordinal);
            CREATE TABLE IF NOT EXISTS migrations (
                migration_key TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                imported_count INTEGER NOT NULL,
                migrated_at TEXT NOT NULL
            );
            """
        )
        columns = {
            str(row[1]) for row in self.connection.execute("PRAGMA table_info(runs)")
        }
        if "is_prepared" not in columns:
            self.connection.execute(
                "ALTER TABLE runs ADD COLUMN is_prepared INTEGER NOT NULL DEFAULT 1"
            )
        self.connection.commit()

    @staticmethod
    def new_run_id() -> str:
        return uuid.uuid4().hex

    def run_directory(self, run_id: str) -> Path:
        if self.app_paths is not None:
            root = self.app_paths.runs
        else:
            root = self.path.parent / "runs"
        path = root / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _run_info(row: sqlite3.Row) -> RunInfo:
        return RunInfo(
            id=str(row["id"]),
            plugin_id=str(row["plugin_id"]),
            variant_id=str(row["variant_id"]),
            automation_name=str(row["automation_name"]),
            source_path=str(row["source_path"]),
            source_hash=str(row["source_hash"]),
            source_ext=str(row["source_ext"]),
            snapshot_path=str(row["snapshot_path"]),
            output_path=str(row["output_path"]),
            sheet_name=str(row["sheet_name"]),
            header_row=int(row["header_row"]),
            first_col=int(row["first_col"]),
            schema_hash=str(row["schema_hash"]),
            status=str(row["status"]),
            row_count=int(row["row_count"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            completed_at=(
                str(row["completed_at"]) if row["completed_at"] is not None else None
            ),
        )

    def create_run(
        self,
        *,
        run_id: str,
        plugin_id: str,
        variant_id: str,
        automation_name: str,
        source_path: str,
        source_hash: str,
        source_ext: str,
        snapshot_path: str,
        output_path: str,
        sheet_name: str,
        header_row: int,
        first_col: int,
        headers: list[Any],
        column_map: Mapping[str, int | None],
        schema_hash: str,
    ) -> RunInfo:
        timestamp = _now()
        self.connection.execute(
            """
            INSERT INTO runs(
                id, plugin_id, variant_id, automation_name, source_path, source_hash,
                source_ext, snapshot_path, output_path, sheet_name, header_row, first_col,
                headers_blob, column_map_blob, statistics_blob, schema_hash, status,
                row_count, created_at, updated_at, schema_version, is_prepared
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ABANDONED', 0, ?, ?, ?, 0)
            """,
            (
                run_id,
                plugin_id,
                variant_id,
                automation_name,
                source_path,
                source_hash,
                source_ext,
                snapshot_path,
                output_path,
                sheet_name,
                int(header_row),
                int(first_col),
                protect(headers),
                protect(dict(column_map)),
                protect({}),
                schema_hash,
                timestamp,
                timestamp,
                RUN_SCHEMA_VERSION,
            ),
        )
        self.connection.commit()
        return self.get_run(run_id)

    def mark_prepared(self, run_id: str) -> None:
        self.connection.execute(
            """
            UPDATE runs
            SET is_prepared=1, status='PREPARED', updated_at=?
            WHERE id=?
            """,
            (_now(), run_id),
        )
        self.connection.commit()

    def get_run(self, run_id: str) -> RunInfo:
        row = self.connection.execute(
            "SELECT * FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise RunStoreError("Saved automation run was not found")
        return self._run_info(row)

    def find_latest(
        self,
        *,
        plugin_id: str,
        variant_id: str,
        source_hash: str,
        sheet_name: str,
        schema_hash: str,
        resumable_only: bool,
    ) -> RunInfo | None:
        params: list[Any] = [
            plugin_id,
            variant_id,
            source_hash,
            sheet_name,
            schema_hash,
        ]
        state_sql = ""
        if resumable_only:
            state_sql = " AND status IN (?, ?, ?, ?)"
            params.extend(RESUMABLE_RUN_STATES)
        row = self.connection.execute(
            """
            SELECT * FROM runs
            WHERE plugin_id=? AND variant_id=? AND source_hash=?
              AND sheet_name=? AND schema_hash=? AND is_prepared=1
            """
            + state_sql
            + " ORDER BY updated_at DESC LIMIT 1",
            params,
        ).fetchone()
        return self._run_info(row) if row is not None else None

    def update_locations(self, run_id: str, source_path: str, output_path: str) -> None:
        self.connection.execute(
            "UPDATE runs SET source_path=?, output_path=?, updated_at=? WHERE id=?",
            (source_path, output_path, _now(), run_id),
        )
        self.connection.commit()

    def headers(self, run_id: str) -> list[Any]:
        row = self.connection.execute(
            "SELECT headers_blob FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise RunStoreError("Saved automation run was not found")
        return list(unprotect(row[0], []))

    def column_map(self, run_id: str) -> dict[str, int | None]:
        row = self.connection.execute(
            "SELECT column_map_blob FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise RunStoreError("Saved automation run was not found")
        return dict(unprotect(row[0], {}))

    def set_statistics(self, run_id: str, values: Mapping[str, int]) -> None:
        self.connection.execute(
            "UPDATE runs SET statistics_blob=?, updated_at=? WHERE id=?",
            (protect(dict(values)), _now(), run_id),
        )
        self.connection.commit()

    def statistics(self, run_id: str) -> dict[str, int]:
        row = self.connection.execute(
            "SELECT statistics_blob FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise RunStoreError("Saved automation run was not found")
        return {str(key): int(value) for key, value in unprotect(row[0], {}).items()}

    def insert_rows(
        self,
        run_id: str,
        rows: Iterable[tuple[int, int, str, Mapping[str, Any]]],
    ) -> int:
        count = 0
        timestamp = _now()
        payloads = []
        for ordinal, source_row, key, values in rows:
            payloads.append(
                (
                    run_id,
                    int(ordinal),
                    int(source_row),
                    _row_key_hash(run_id, key),
                    protect(dict(values)),
                    "PENDING",
                    timestamp,
                )
            )
            count += 1
        if payloads:
            self.connection.executemany(
                """
                INSERT INTO rows(
                    run_id, ordinal, source_row, key_hash, input_blob, status, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                payloads,
            )
            self.connection.execute(
                "UPDATE runs SET row_count=row_count+?, updated_at=? WHERE id=?",
                (count, timestamp, run_id),
            )
            self.connection.commit()
        return count

    def trim_rows(self, run_id: str, keep_count: int) -> None:
        """Discard streamed trailing blank rows without buffering them in memory."""
        keep = max(0, int(keep_count))
        timestamp = _now()
        with self.connection:
            self.connection.execute(
                "DELETE FROM rows WHERE run_id=? AND ordinal>=?", (run_id, keep)
            )
            self.connection.execute(
                "UPDATE runs SET row_count=?, updated_at=? WHERE id=?",
                (keep, timestamp, run_id),
            )

    def recover(self, run_id: str) -> None:
        timestamp = _now()
        with self.connection:
            self.connection.execute(
                """
                UPDATE rows SET status='PENDING', updated_at=?
                WHERE run_id=? AND status='IN_PROGRESS'
                """,
                (timestamp, run_id),
            )
            self.connection.execute(
                "UPDATE runs SET status='PAUSED', updated_at=? WHERE id=?",
                (timestamp, run_id),
            )

    def abandon(self, run_id: str) -> None:
        self._set_run_status(run_id, "ABANDONED")

    def begin_batch(self, run_id: str, limit: int) -> tuple[str, int]:
        batch_id = uuid.uuid4().hex
        total = min(max(0, int(limit)), self.outstanding_count(run_id))
        self._set_run_status(run_id, "RUNNING")
        return batch_id, total

    def claim_next(self, run_id: str, batch_id: str) -> RowRecord | None:
        timestamp = _now()
        with self.connection:
            row = self.connection.execute(
                """
                SELECT ordinal, input_blob FROM rows
                WHERE run_id=? AND status IN ('PENDING', 'RETRY_LATER')
                  AND (last_batch_id IS NULL OR last_batch_id<>?)
                ORDER BY ordinal LIMIT 1
                """,
                (run_id, batch_id),
            ).fetchone()
            if row is None:
                return None
            ordinal = int(row["ordinal"])
            updated = self.connection.execute(
                """
                UPDATE rows
                SET status='IN_PROGRESS', attempts=attempts+1, last_batch_id=?, updated_at=?
                WHERE run_id=? AND ordinal=? AND status IN ('PENDING', 'RETRY_LATER')
                """,
                (batch_id, timestamp, run_id, ordinal),
            ).rowcount
            if updated != 1:
                return None
        values = dict(unprotect(row["input_blob"], {}))
        key = str(values.pop("__business_key__", ""))
        return RowRecord(index=ordinal, key=key, values=values)

    def commit_result(self, run_id: str, record: RowRecord, result: RowResult) -> None:
        if result.disposition == RowDisposition.RESOLVED:
            status = "RESOLVED"
        elif result.disposition == RowDisposition.SKIPPED:
            status = "SKIPPED"
        else:
            status = "RETRY_LATER"
        payload = {"values": dict(result.values), "comment": result.comment}
        updated = self.connection.execute(
            """
            UPDATE rows
            SET result_blob=?, status=?, disposition=?, updated_at=?
            WHERE run_id=? AND ordinal=? AND status='IN_PROGRESS'
            """,
            (
                protect(payload),
                status,
                result.disposition.value,
                _now(),
                run_id,
                int(record.index),
            ),
        ).rowcount
        if updated != 1:
            self.connection.rollback()
            raise RunStoreError("The row result could not be committed atomically")
        self.connection.commit()

    def finish_batch(self, run_id: str) -> int:
        outstanding = self.outstanding_count(run_id)
        self._set_run_status(run_id, "COMPLETED" if outstanding == 0 else "PAUSED")
        return outstanding

    def pause(self, run_id: str) -> None:
        self._release_and_set(run_id, "PAUSED")

    def fail(self, run_id: str) -> None:
        self._release_and_set(run_id, "FAILED")

    def _release_and_set(self, run_id: str, status: str) -> None:
        timestamp = _now()
        with self.connection:
            self.connection.execute(
                """
                UPDATE rows SET status='PENDING', updated_at=?
                WHERE run_id=? AND status='IN_PROGRESS'
                """,
                (timestamp, run_id),
            )
            completed_at = timestamp if status == "COMPLETED" else None
            self.connection.execute(
                "UPDATE runs SET status=?, updated_at=?, completed_at=? WHERE id=?",
                (status, timestamp, completed_at, run_id),
            )

    def _set_run_status(self, run_id: str, status: str) -> None:
        timestamp = _now()
        completed_at = timestamp if status == "COMPLETED" else None
        self.connection.execute(
            "UPDATE runs SET status=?, updated_at=?, completed_at=? WHERE id=?",
            (status, timestamp, completed_at, run_id),
        )
        self.connection.commit()

    def status_counts(self, run_id: str) -> dict[str, int]:
        result = {key: 0 for key in (*COMPLETE_ROW_STATES, *PENDING_ROW_STATES, "IN_PROGRESS")}
        for row in self.connection.execute(
            "SELECT status, COUNT(*) AS count FROM rows WHERE run_id=? GROUP BY status",
            (run_id,),
        ):
            result[str(row["status"])] = int(row["count"])
        return result

    def completed_count(self, run_id: str) -> int:
        row = self.connection.execute(
            """
            SELECT COUNT(*) FROM rows
            WHERE run_id=? AND status IN ('RESOLVED', 'SKIPPED')
            """,
            (run_id,),
        ).fetchone()
        return int(row[0])

    def outstanding_count(self, run_id: str) -> int:
        row = self.connection.execute(
            """
            SELECT COUNT(*) FROM rows
            WHERE run_id=? AND status NOT IN ('RESOLVED', 'SKIPPED')
            """,
            (run_id,),
        ).fetchone()
        return int(row[0])

    def iter_rows(self, run_id: str) -> Iterator[PersistedRow]:
        cursor = self.connection.execute(
            """
            SELECT ordinal, source_row, key_hash, input_blob, result_blob,
                   status, disposition
            FROM rows WHERE run_id=? ORDER BY source_row
            """,
            (run_id,),
        )
        for row in cursor:
            inputs = dict(unprotect(row["input_blob"], {}))
            inputs.pop("__business_key__", None)
            result_payload = dict(unprotect(row["result_blob"], {}))
            yield PersistedRow(
                ordinal=int(row["ordinal"]),
                source_row=int(row["source_row"]),
                key_hash=str(row["key_hash"]),
                inputs=inputs,
                result=dict(result_payload.get("values") or {}),
                comment=result_payload.get("comment"),
                status=str(row["status"]),
                disposition=(
                    str(row["disposition"]) if row["disposition"] is not None else None
                ),
            )

    def inputs_for_ordinal(self, run_id: str, ordinal: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT input_blob FROM rows WHERE run_id=? AND ordinal=?",
            (run_id, int(ordinal)),
        ).fetchone()
        if row is None:
            return None
        values = dict(unprotect(row["input_blob"], {}))
        values.pop("__business_key__", None)
        return values

    def import_completed(
        self,
        run_id: str,
        ordinal: int,
        *,
        expected_key: str | None = None,
        expected_legacy_hash: str | None = None,
        result_values: Mapping[str, Any],
        comment: Any,
    ) -> bool:
        row = self.connection.execute(
            "SELECT input_blob FROM rows WHERE run_id=? AND ordinal=?",
            (run_id, int(ordinal)),
        ).fetchone()
        if row is None:
            return False
        inputs = dict(unprotect(row["input_blob"], {}))
        key = str(inputs.get("__business_key__", ""))
        if expected_key is not None and key != str(expected_key):
            return False
        if expected_legacy_hash is not None:
            actual = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
            if actual != str(expected_legacy_hash):
                return False
        payload = {"values": dict(result_values), "comment": comment}
        self.connection.execute(
            """
            UPDATE rows SET status='RESOLVED', disposition='resolved',
                result_blob=?, updated_at=? WHERE run_id=? AND ordinal=?
            """,
            (protect(payload), _now(), run_id, int(ordinal)),
        )
        self.connection.commit()
        return True

    def migration_done(self, migration_key: str) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM migrations WHERE migration_key=?", (migration_key,)
            ).fetchone()
            is not None
        )

    def record_migration(self, migration_key: str, run_id: str, count: int) -> None:
        self.connection.execute(
            """
            INSERT OR IGNORE INTO migrations(migration_key, run_id, imported_count, migrated_at)
            VALUES(?, ?, ?, ?)
            """,
            (migration_key, run_id, int(count), _now()),
        )
        self.connection.commit()

    def cleanup(self, retention_days: int = 30) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat(
            timespec="microseconds"
        )
        rows = self.connection.execute(
            """
            SELECT id, snapshot_path FROM runs
            WHERE status IN ('COMPLETED', 'ABANDONED') AND updated_at<?
            """,
            (cutoff,),
        ).fetchall()
        if rows:
            with self.connection:
                self.connection.executemany(
                    "DELETE FROM runs WHERE id=?", ((str(row["id"]),) for row in rows)
                )
        for row in rows:
            snapshot = Path(str(row["snapshot_path"])).resolve()
            root = (
                self.app_paths.runs if self.app_paths is not None
                else self.path.parent / "runs"
            ).resolve()
            try:
                if snapshot.parent.parent == root:
                    shutil.rmtree(snapshot.parent, ignore_errors=True)
            except OSError:
                pass
        if rows:
            self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return len(rows)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> RunStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
