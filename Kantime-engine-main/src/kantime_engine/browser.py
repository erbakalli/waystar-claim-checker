"""Generic Microsoft Edge launch and synchronous Playwright CDP ownership."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
import http.client
import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
from urllib.parse import urlsplit
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from .api import BrowserSpec
from .cancellation import CancellationRequested, CancellationToken
from .paths import ApplicationPaths, resolve_profile_dir


class BrowserSessionError(RuntimeError):
    pass


def find_edge_executable() -> str:
    candidates = [
        shutil.which("msedge"),
        os.path.expandvars(r"%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise BrowserSessionError("Microsoft Edge was not found in a standard location.")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _cdp_json(endpoint: str, path: str, timeout: float = 1.0) -> Any:
    """Read local Edge HTTP without initializing unrelated HTTPS/SSL handlers."""
    address = urlsplit(endpoint)
    connection = http.client.HTTPConnection(address.hostname, address.port, timeout=timeout)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        if response.status != 200:
            raise OSError(f"Edge CDP returned HTTP {response.status}")
        return json.loads(response.read().decode("utf-8"))
    finally:
        connection.close()


def _cdp_version(endpoint: str, timeout: float = 1.0) -> dict[str, Any] | None:
    try:
        payload = _cdp_json(endpoint, "/json/version", timeout)
        return payload if isinstance(payload, dict) else None
    except (http.client.HTTPException, TimeoutError, OSError, ValueError):
        logging.getLogger(__name__).debug("Edge CDP probe failed at %s", endpoint, exc_info=True)
        return None


def _is_edge_cdp(info: Mapping[str, Any] | None) -> bool:
    if not info:
        return False
    return any("Edg/" in str(info.get(key, "")) for key in ("Browser", "User-Agent"))


def _running_edge_endpoint(profile_dir: Path) -> str | None:
    """Recover the exact managed profile's endpoint if its marker was lost."""
    if os.name != "nt":
        return None
    script = r'''
    $ErrorActionPreference = 'Stop'
    $items = @(Get-CimInstance Win32_Process -Filter "name = 'msedge.exe'" | ForEach-Object {
        $line = $_.CommandLine
        if ($line -and $line -notmatch '--type=' -and $line -match '--remote-debugging-port=(\d+)') {
            $port = [int]$Matches[1]
            if ($line -match '(?:"--user-data-dir=([^"]+)"|--user-data-dir="([^"]+)"|--user-data-dir=([^\s"]+))') {
                $profile = $Matches[1]
                if (-not $profile) { $profile = $Matches[2] }
                if (-not $profile) { $profile = $Matches[3] }
                [PSCustomObject]@{ profile = $profile; port = $port }
            }
        }
    })
    ConvertTo-Json -InputObject $items -Compress
    '''
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode:
            return None
        records = json.loads(result.stdout)
        for record in records:
            if os.path.normcase(os.path.abspath(record["profile"])) != os.path.normcase(os.path.abspath(profile_dir)):
                continue
            port = int(record["port"])
            if not 0 < port < 65536:
                continue
            endpoint = f"http://127.0.0.1:{port}"
            info = _cdp_version(endpoint)
            if _is_edge_cdp(info):
                return endpoint
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        pass
    return None


class BrowserActions:
    def __init__(
        self,
        cancellation: CancellationToken,
        logger: Any = None,
        default_timeout_ms: int = 30_000,
    ) -> None:
        self.cancellation = cancellation
        self.logger = logger
        self.default_timeout_ms = default_timeout_ms

    def sleep(self, seconds: float) -> bool:
        """Compatibility wait returning False when cancellation interrupts it."""
        return self.cancellation.wait(seconds)

    def pause(self, seconds: float) -> None:
        """Interruptible pause that raises immediately when cancellation wins."""
        if not self.cancellation.wait(seconds):
            raise CancellationRequested("Automation was stopped")

    @contextmanager
    def step(self, event: str, message: str = "", **fields: Any) -> Iterator[None]:
        manager = (
            self.logger.step(event, message, **fields)
            if self.logger is not None
            else nullcontext()
        )
        with manager:
            yield

    def wait_visible(self, locator: Any, timeout_ms: int | None = None) -> Any:
        timeout = timeout_ms or self.default_timeout_ms
        deadline = time.monotonic() + max(0, timeout) / 1000.0
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            self.cancellation.raise_if_cancelled()
            remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
            try:
                locator.wait_for(state="visible", timeout=min(500, remaining_ms))
                return locator
            except CancellationRequested:
                raise
            except Exception as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise TimeoutError("Timed out waiting for a visible element")

    def click(self, locator: Any, timeout_ms: int | None = None, **kwargs: Any) -> None:
        timeout = timeout_ms or self.default_timeout_ms
        self.wait_visible(locator, timeout)
        self.cancellation.raise_if_cancelled()
        locator.click(timeout=timeout, **kwargs)

    def fill(self, locator: Any, value: Any, timeout_ms: int | None = None) -> None:
        timeout = timeout_ms or self.default_timeout_ms
        self.wait_visible(locator, timeout)
        self.cancellation.raise_if_cancelled()
        locator.fill(str(value), timeout=timeout)

    def wait_hidden(self, locator: Any, timeout_ms: int | None = None) -> Any:
        timeout = timeout_ms or self.default_timeout_ms
        deadline = time.monotonic() + timeout / 1000.0
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            self.cancellation.raise_if_cancelled()
            remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
            try:
                locator.wait_for(state="hidden", timeout=min(500, remaining_ms))
                return locator
            except CancellationRequested:
                raise
            except Exception as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise TimeoutError("Timed out waiting for a hidden element")

    def wait_enabled(self, locator: Any, timeout_ms: int | None = None) -> Any:
        deadline = time.monotonic() + (timeout_ms or self.default_timeout_ms) / 1000.0
        while time.monotonic() < deadline:
            self.cancellation.raise_if_cancelled()
            try:
                if locator.is_visible() and locator.is_enabled():
                    return locator
            except Exception:
                pass
            if not self.cancellation.wait(0.2):
                raise CancellationRequested("Automation was stopped")
        raise TimeoutError("Timed out waiting for an enabled element")

    def select_option(self, locator: Any, value: Any, timeout_ms: int | None = None) -> Any:
        timeout = timeout_ms or self.default_timeout_ms
        self.wait_enabled(locator, timeout)
        return locator.select_option(value=value, timeout=timeout)

    def press(self, locator: Any, key: str, timeout_ms: int | None = None) -> None:
        timeout = timeout_ms or self.default_timeout_ms
        self.wait_visible(locator, timeout)
        self.cancellation.raise_if_cancelled()
        locator.press(key, timeout=timeout)

    def goto(self, page: Any, url: str, timeout_ms: int | None = None,
             *, wait_until: str = "domcontentloaded") -> Any:
        self.cancellation.raise_if_cancelled()
        return page.goto(
            url,
            wait_until=wait_until,
            timeout=timeout_ms or self.default_timeout_ms,
        )

    def wait_url(
        self, page: Any, expected: str | Callable[[str], bool], timeout_ms: int | None = None
    ) -> str:
        deadline = time.monotonic() + (timeout_ms or self.default_timeout_ms) / 1000.0
        while time.monotonic() < deadline:
            self.cancellation.raise_if_cancelled()
            current = str(page.url or "")
            if (callable(expected) and expected(current)) or (
                isinstance(expected, str) and expected in current
            ):
                return current
            if not self.cancellation.wait(0.2):
                raise CancellationRequested("Automation was stopped")
        raise TimeoutError("Timed out waiting for the expected page URL")

    def wait_for_any(
        self,
        outcomes: Mapping[str, Callable[[], bool]],
        timeout_ms: int | None = None,
        poll_ms: int = 200,
    ) -> str:
        deadline = time.monotonic() + (timeout_ms or self.default_timeout_ms) / 1000.0
        while time.monotonic() < deadline:
            self.cancellation.raise_if_cancelled()
            for name, probe in outcomes.items():
                try:
                    if probe():
                        return name
                except Exception:
                    continue
            if not self.cancellation.wait(max(0.01, poll_ms / 1000.0)):
                raise CancellationRequested("Automation was stopped")
        raise TimeoutError("Timed out waiting for an expected portal outcome")


class BrowserSession:
    def __init__(
        self,
        spec: BrowserSpec,
        cancellation: CancellationToken,
        app_paths: ApplicationPaths,
        logger: Any = None,
        default_timeout_ms: int = 30_000,
    ) -> None:
        self.spec = spec
        self.cancellation = cancellation
        self.app_paths = app_paths
        self.actions = BrowserActions(cancellation, logger, default_timeout_ms)
        self.profile_dir: Path | None = None
        self.endpoint: str | None = None
        self.process: subprocess.Popen[Any] | None = None
        self.playwright: Any = None
        self.browser: Any = None
        self.context: Any = None
        self.page: Any = None
        self._owner_thread: int | None = None
        self._previous_page: Any = None
        self.status_callback: Callable[[str], None] | None = None

    @staticmethod
    def _surface_url(surface: Any) -> str:
        reported = str(surface.url or "")
        if reported not in ("", "about:blank"):
            return reported
        try:
            actual = surface.evaluate("() => location.href")
            return actual if isinstance(actual, str) else reported
        except Exception:
            return reported

    def _status(self, message: str) -> None:
        if self.status_callback is not None:
            self.status_callback(message)

    def _login_page_visible(self, page: Any) -> bool:
        for surface in (page, *page.frames):
            try:
                url = self._surface_url(surface).lower()
                if (any(pattern.lower() in url for pattern in self.spec.login_url_patterns)
                        and surface.locator('body').is_visible()):
                    return True
            except Exception:
                continue
        return False

    def _portal_ready(self, page: Any) -> bool:
        for surface in (page, *page.frames):
            try:
                url = self._surface_url(surface).lower()
                if (any(pattern.lower() in url for pattern in self.spec.page_url_patterns)
                        and surface.locator(self.spec.ready_selector).first.is_visible()):
                    return True
            except Exception:
                continue
        return False

    def matches_page(self, page: Any) -> bool:
        try:
            url = str(page.url or "").lower()
        except Exception:
            return False
        return any(pattern.lower() in url for pattern in self.spec.page_url_patterns)

    def _marker_path(self) -> Path | None:
        return self.profile_dir / ".kantime-cdp.json" if self.profile_dir else None

    def _existing_profile_endpoint(self) -> str | None:
        marker = self._marker_path()
        if marker is not None:
            try:
                payload = json.loads(marker.read_text(encoding="utf-8"))
                endpoint = str(payload.get("endpoint", ""))
                if endpoint and _is_edge_cdp(_cdp_version(endpoint)):
                    return endpoint
            except (OSError, ValueError, json.JSONDecodeError):
                pass
        if self.profile_dir is not None:
            try:
                port = int(
                    (self.profile_dir / "DevToolsActivePort")
                    .read_text(encoding="utf-8")
                    .splitlines()[0]
                )
                endpoint = f"http://127.0.0.1:{port}"
                if _is_edge_cdp(_cdp_version(endpoint)):
                    return endpoint
            except (OSError, ValueError, IndexError):
                pass
        return _running_edge_endpoint(self.profile_dir) if self.profile_dir is not None else None

    def _write_marker(self) -> None:
        marker = self._marker_path()
        if marker is None or not self.endpoint:
            return
        try:
            marker.write_text(json.dumps({"endpoint": self.endpoint}), encoding="utf-8")
        except OSError:
            pass

    def _remove_marker(self) -> None:
        marker = self._marker_path()
        if marker is not None:
            try:
                marker.unlink()
            except OSError:
                pass

    def is_cdp_ready(self) -> bool:
        if self.spec.existing_edge_user_data_dir:
            try:
                if self.browser is not None and self.browser.is_connected():
                    return True
                endpoint = self._personal_edge_endpoint()
                if self.endpoint != endpoint:
                    return False
                address = urlsplit(endpoint)
                with socket.create_connection((address.hostname, address.port), timeout=0.5):
                    return True
            except (OSError, BrowserSessionError):
                return False
        if not self.endpoint:
            return False
        return _is_edge_cdp(_cdp_version(self.endpoint))

    def _personal_edge_endpoint(self) -> str:
        folder = Path(os.path.expandvars(self.spec.existing_edge_user_data_dir)).expanduser()
        try:
            lines = (folder / 'DevToolsActivePort').read_text(encoding='utf-8').splitlines()
            port = int(lines[0].strip())
            path = lines[1].strip()
            if not 0 < port < 65536 or not path.startswith('/devtools/browser/'):
                raise ValueError('Invalid browser endpoint')
            if any(char.isspace() for char in path) or '#' in path or '?' in path:
                raise ValueError('Invalid browser endpoint')
            return f'ws://127.0.0.1:{port}{path}'
        except (OSError, ValueError, IndexError):
            raise BrowserSessionError(
                'Open your usual Microsoft Edge, go to edge://inspect/#remote-debugging, '
                'and enable "Allow remote debugging for this browser instance". '
                'Then click Open Browser again and allow the connection if Edge asks.'
            ) from None

    def open_window(self) -> bool:
        """Open a portal tab, including when the managed Edge is already running.

        Login happens before Playwright attaches. Edge forwards this disposable
        launcher to the existing profile, so the original process ownership and
        debugging endpoint remain unchanged.
        """
        self.cancellation.raise_if_cancelled()
        if self.spec.existing_edge_user_data_dir:
            self.launch()
            page = self.attach(lambda item: self.matches_page(item) or self._login_page_visible(item))
            # Make the tab visible before navigation, including slow SSO pages.
            page.bring_to_front()
            for attempt in range(2):
                self.cancellation.raise_if_cancelled()
                try:
                    if attempt or not (self.matches_page(page) or self._login_page_visible(page)):
                        self._status("Opening Waystar in your Edge tab...")
                        self.actions.goto(page, self.spec.start_url, timeout_ms=20_000, wait_until="commit")
                    if self.spec.ready_selector:
                        self._status("Waiting for Waystar to show its search or sign-in page...")
                        outcome = self.actions.wait_for_any({
                            "ready": lambda: self._portal_ready(page),
                            "login": lambda: self._login_page_visible(page),
                        }, timeout_ms=20_000)
                        self._status("Waystar is ready." if outcome == "ready" else
                                     "Waystar sign-in is open. Complete login and MFA in this tab.")
                    return False
                except CancellationRequested:
                    raise
                except Exception:
                    if attempt:
                        raise BrowserSessionError(
                            "Waystar did not finish opening after an automatic retry in the same tab. "
                            "Check the connection or complete any sign-in request shown in Edge."
                        ) from None
                    self._status("Waystar is taking longer to open. Retrying automatically in the same tab...")
        launched = self.launch()
        if launched:
            return True
        self.cancellation.raise_if_cancelled()
        if self.profile_dir is None:
            self.profile_dir = resolve_profile_dir(
                self.app_paths.profiles, self.spec.profile_id, self.spec.legacy_profile_dir
            )
        args = [
            find_edge_executable(),
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            self.spec.start_url,
        ]
        kwargs: dict[str, Any] = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "stdin": subprocess.DEVNULL,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["close_fds"] = True
        try:
            subprocess.Popen(args, **kwargs)
        except OSError as exc:
            raise BrowserSessionError(f"Could not open the managed Edge login window: {exc}") from exc
        return False

    def launch(self) -> bool:
        """Launch a managed Edge without attaching Playwright, allowing extensions."""
        if self.is_cdp_ready():
            return False
        if self.spec.existing_edge_user_data_dir:
            self.endpoint = self._personal_edge_endpoint()
            return False
        self.profile_dir = resolve_profile_dir(
            self.app_paths.profiles, self.spec.profile_id, self.spec.legacy_profile_dir
        )
        existing = self._existing_profile_endpoint()
        if existing:
            self.endpoint = existing
            self._write_marker()
            return False
        edge = find_edge_executable()
        last_error: Exception | None = None
        for _attempt in range(3):
            port = _free_port()
            endpoint = f"http://127.0.0.1:{port}"
            args = [
                edge,
                f"--user-data-dir={self.profile_dir}",
                f"--remote-debugging-port={port}",
                "--remote-allow-origins=*",
                "--enable-extensions",
                "--enable-unsafe-extension-debugging",
                "--silent-debugger-extension-api",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-popup-blocking",
                "--new-window",
            ]
            for extension_id in self.spec.extension_ids:
                args.append(f"--allowlisted-extension-id={extension_id}")
            args.append(self.spec.start_url)
            kwargs: dict[str, Any] = {
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "stdin": subprocess.DEVNULL,
            }
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["close_fds"] = True
            try:
                self.process = subprocess.Popen(args, **kwargs)
                self.endpoint = endpoint
                deadline = time.monotonic() + 30.0
                while time.monotonic() < deadline:
                    if self.is_cdp_ready():
                        self._write_marker()
                        return True
                    if self.process.poll() is not None:
                        existing = self._existing_profile_endpoint()
                        if existing:
                            self.endpoint = existing
                            self.process = None
                            self._write_marker()
                            return False
                        break
                    self.cancellation.raise_if_cancelled()
                    time.sleep(0.25)
                last_error = BrowserSessionError(
                    f"Timed out waiting for Edge CDP at {endpoint}."
                )
            except CancellationRequested:
                self._terminate_process()
                self.endpoint = None
                raise
            except Exception as exc:
                last_error = exc
            self._terminate_process()
            self.endpoint = None
        raise BrowserSessionError(str(last_error or "Could not launch Microsoft Edge."))

    def _assert_thread(self) -> None:
        current = threading.get_ident()
        if self._owner_thread is not None and self._owner_thread != current:
            raise BrowserSessionError("Playwright browser session used from the wrong thread")

    def attach(self, matcher: Callable[[Any], bool]) -> Any:
        self._assert_thread()
        if not self.is_cdp_ready():
            self.launch()
        if self.browser is not None:
            try:
                if self.browser.is_connected():
                    return self.select_page(matcher)
            except Exception:
                pass
        if self.playwright is not None or self.browser is not None:
            self.detach()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserSessionError("Install the playwright package to use CDP.") from exc
        self.playwright = sync_playwright().start()
        self._owner_thread = threading.get_ident()
        try:
            self.browser = self.playwright.chromium.connect_over_cdp(
                self.endpoint, timeout=60_000 if self.spec.existing_edge_user_data_dir else 30_000)
        except Exception as exc:
            self.playwright.stop()
            self.playwright = None
            if self.spec.existing_edge_user_data_dir:
                raise BrowserSessionError(
                    'Could not connect to your open Edge. Enable remote debugging at '
                    'edge://inspect/#remote-debugging, then retry and allow the connection in Edge.'
                ) from None
            raise BrowserSessionError(f"Playwright could not attach to {self.endpoint}: {exc}") from exc
        if not self.browser.contexts:
            raise BrowserSessionError("The CDP browser has no context")
        self.context = self.browser.contexts[0]
        return self.select_page(matcher)

    @staticmethod
    def _open_pages(context: Any) -> list[Any]:
        pages: list[Any] = []
        for page in list(context.pages or []):
            try:
                if page.is_closed():
                    continue
            except Exception:
                pass
            pages.append(page)
        return pages

    def select_page(self, matcher: Callable[[Any], bool]) -> Any:
        self._assert_thread()
        if self.context is None:
            raise BrowserSessionError("No browser context is attached")
        pages = self._open_pages(self.context)
        # Open Browser may create a fresh sign-in window. Prefer that recent
        # portal tab over an older matching tab with an expired session.
        match = next((page for page in reversed(pages) if matcher(page)), None)
        if match is not None:
            self.page = match
        elif self.spec.existing_edge_user_data_dir and self.page not in pages:
            self.page = self.context.new_page()
        elif self.page not in pages:
            ordinary_pages = []
            for page in pages:
                try:
                    if not str(page.url or "").startswith("chrome-extension://"):
                        ordinary_pages.append(page)
                except Exception:
                    ordinary_pages.append(page)
            self.page = ordinary_pages[0] if ordinary_pages else (
                pages[0] if pages else self.context.new_page()
            )
        return self.page

    def reload(self) -> Any:
        self._assert_thread()
        if self.page is None:
            raise BrowserSessionError("No browser page is attached")
        self.page.reload(wait_until="domcontentloaded", timeout=45_000)
        return self.page

    def new_tab(self) -> Any:
        self._assert_thread()
        if self.context is None:
            raise BrowserSessionError("No browser context is attached")
        old_page = self.page
        fresh = self.context.new_page()
        self.page = fresh
        self._previous_page = old_page if old_page is not fresh else None
        return fresh

    def commit_page_replacement(self) -> None:
        old_page = self._previous_page
        self._previous_page = None
        if old_page is not None and old_page is not self.page:
            try:
                old_page.close()
            except Exception:
                pass

    def rollback_page_replacement(self) -> None:
        old_page = self._previous_page
        fresh = self.page
        self._previous_page = None
        if old_page is None:
            return
        self.page = old_page
        if fresh is not None and fresh is not old_page:
            try:
                fresh.close()
            except Exception:
                pass

    def detach(self) -> None:
        self._assert_thread()
        if self.browser is not None and hasattr(self.browser, "disconnect"):
            try:
                self.browser.disconnect()
            except Exception:
                pass
        if self.playwright is not None:
            try:
                self.playwright.stop()
            except Exception:
                pass
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self._owner_thread = None

    def close(self) -> None:
        self._assert_thread()
        if self.spec.existing_edge_user_data_dir:
            self.detach()
            self.endpoint = None
            return
        if self.browser is not None:
            try:
                self.browser.close()
            except Exception:
                pass
        if self.playwright is not None:
            try:
                self.playwright.stop()
            except Exception:
                pass
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self._owner_thread = None
        self._terminate_process()
        self._remove_marker()
        self.endpoint = None

    def _terminate_process(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
        self.process = None
