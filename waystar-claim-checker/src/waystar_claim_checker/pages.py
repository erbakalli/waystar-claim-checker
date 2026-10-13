"""Read-only Waystar browser adapter with an explicitly calibrated result profile."""
import json
import re
from pathlib import Path
from urllib.parse import urlparse

from kantime_engine import PortalAutomationError, RecoveryAction, RetryScope
from kantime_engine.cancellation import CancellationRequested
from .claims import header_indexes, parse_table

CLAIMS_URL = (
    "https://mgmt.zirmed.com/ExternalUserManagement/IntegrationRedirect/Index"
    "?tabappid=87&redirecturl=https:%2F%2Fclaimseditor.ecorpnet.com%2Fmember%2FSelectSummary.aspx"
)


def load_profile(path):
    try:
        profile = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("Choose a valid Waystar portal profile JSON file") from None
    required = ("initial_search_input", "search_mode_toggle", "pcn_option",
                "search_input", "results_table", "no_results", "ready")
    if not isinstance(profile, dict) or not all(
        isinstance(profile.get(key), str) and profile[key].strip() for key in required
    ):
        raise ValueError("Portal profile needs initial search, PCN selection, search input and result selectors")
    pending = [key for key in (*required, "submit")
               if isinstance(profile.get(key), str) and profile[key].startswith("REPLACE_WITH_")]
    if pending:
        labels = {"ready": "the completed-search indicator", "no_results": "the no-results message",
                  "submit": "the search button"}
        raise ValueError("Still missing: " + ", ".join(labels.get(key, key) for key in pending))
    # ready identifies a COMPLETED search, including the empty result case.
    if profile.get("verified") is not True:
        raise ValueError("Live result selectors must be verified against your Waystar account first")
    if not isinstance(profile.get("headers", {}), dict):
        raise ValueError("Portal profile headers must be a mapping")
    for key in ("submit", "next_page", "busy", "reason_tooltip", "data_rows", "selected_pcn"):
        if key in profile and not isinstance(profile[key], str):
            raise ValueError("Optional portal selectors must be strings")
    if "column_headers" in profile:
        headers = profile["column_headers"]
        if (not isinstance(headers, list) or not headers
                or not all(isinstance(h, str) and h.strip() for h in headers)
                or not profile.get("data_rows")):
            raise ValueError("Explicit column headers require a nonempty list and a data-row selector")
    return profile


class ClaimsPage:
    def __init__(self, context, profile):
        self.context = context
        self.profile = profile

    @property
    def page(self):
        top = self.context.page
        if urlparse(str(top.url)).hostname == "claimseditor.ecorpnet.com":
            return top
        # The integration URL remains on mgmt.zirmed.com while sessionExtendFrame
        # redirects into Claims. Playwright includes nested frames in page.frames.
        for frame in top.frames:
            if urlparse(str(frame.url)).hostname == "claimseditor.ecorpnet.com":
                return frame
        return top

    def login_visible(self):
        for surface in (self.context.page, *self.context.page.frames):
            host = urlparse(str(surface.url)).hostname
            if host not in ("mgmt.zirmed.com", "claimseditor.ecorpnet.com"):
                continue
            try:
                if surface.locator('input[type="password"]').is_visible():
                    return True
            except Exception:
                # A frame can detach while SSO redirects. The next poll sees
                # the replacement frame; do not mistake this for a login form.
                continue
        return False

    def signed_out(self):
        host = urlparse(str(self.page.url)).hostname or ""
        return host != "claimseditor.ecorpnet.com" or self.login_visible()

    def reauth_error(self):
        return PortalAutomationError(
            "Sign in to Waystar, complete MFA, then return here and retry.",
            reason="login_required", recovery=RecoveryAction.REAUTH,
        )

    def open(self):
        # Open the integration only during initial setup or recovery.
        self.context.actions.goto(self.context.page, CLAIMS_URL, timeout_ms=60_000)
        self.select_pcn()

    def read_office(self):
        """Read the active Account heading once, excluding its numeric account ID."""
        office = ""
        def captured():
            nonlocal office
            for surface in (self.page, self.context.page):
                text = surface.locator("body").inner_text()
                match = re.search(r"\bAccount\s*:?\s*([^\r\n]+?)\s*\(\s*\d+\s*\)", text, re.IGNORECASE)
                if match:
                    office = " ".join(match.group(1).split())
                    return bool(office)
            return False
        try:
            outcome = self.context.actions.wait_for_any({"account": captured, "login": self.login_visible})
            if outcome == "login" or self.signed_out():
                raise self.reauth_error()
            if not office:
                raise ValueError("Account heading has no office name")
            return office
        except (CancellationRequested, PortalAutomationError):
            raise
        except Exception:
            raise PortalAutomationError(
                "Could not read the active Account office on the initial Waystar page.",
                reason="office_not_found", recovery=RecoveryAction.STOP,
            ) from None

    def select_pcn(self):
        try:
            selected = self.profile.get("selected_pcn")
            if selected and self.page.locator(selected).is_visible():
                self.context.actions.wait_visible(self.page.locator(self.profile["search_input"]))
                return
            outcome = self.context.actions.wait_for_any({
                "search": lambda: self.page.locator(self.profile["initial_search_input"]).is_visible(),
                "login": self.login_visible,
            }, timeout_ms=60_000)
        except CancellationRequested:
            raise
        except Exception:
            if self.signed_out():
                raise self.reauth_error() from None
            raise PortalAutomationError(
                "Claims search did not appear. Verify the portal profile.",
                reason="search_not_ready", recovery=RecoveryAction.STOP,
            ) from None
        if outcome == "login" or self.signed_out():
            raise self.reauth_error()
        try:
            self.context.actions.click(self.page.locator(self.profile["search_mode_toggle"]))
            self.context.actions.click(self.page.locator(self.profile["pcn_option"]))
            if self.profile.get("selected_pcn"):
                self.context.actions.wait_visible(self.page.locator(self.profile["selected_pcn"]))
            self.context.actions.wait_visible(self.page.locator(self.profile["search_input"]))
        except CancellationRequested:
            raise
        except Exception:
            if self.signed_out():
                raise self.reauth_error() from None
            raise PortalAutomationError(
                "Could not select PCN search. Verify the search-mode selectors in the portal profile.",
                reason="pcn_search_not_ready", recovery=RecoveryAction.STOP,
            ) from None

    def search(self, pcn):
        # Reuse the results page's search bar; recover only if it is unavailable.
        if self.signed_out():
            raise self.reauth_error()
        if not self.page.locator(self.profile["search_input"]).is_visible():
            self.open()
        else:
            self.select_pcn()
        actions = self.context.actions
        stage = "submit_search"
        try:
            search = self.page.locator(self.profile["search_input"])
            actions.fill(search, pcn)
            previous = self.result_snapshot()
            if self.profile.get("submit"):
                actions.click(self.page.locator(self.profile["submit"]))
            else:
                actions.press(search, "Enter")
            candidate = None
            def completed():
                nonlocal candidate
                if not self.page.locator(self.profile["ready"]).first.is_visible():
                    candidate = None
                    return False
                current = self.result_snapshot()
                if current == previous:
                    candidate = None
                    return False
                stable = current == candidate
                candidate = current
                return stable
            stage = "wait_for_results"
            outcome = actions.wait_for_any({
                "completed": completed,
                "login": self.login_visible,
            })
            if outcome == "login":
                raise self.reauth_error()
            if self.profile.get("busy"):
                actions.wait_hidden(self.page.locator(self.profile["busy"]))
            if self.signed_out():
                raise self.reauth_error()
            stage = "read_results"
            table = self.page.locator(self.profile["results_table"])
            # The empty-result overlay can coexist with the visible grid table.
            if self.page.locator(self.profile["no_results"]).is_visible():
                if (table.is_visible() and self.profile.get("data_rows")
                        and table.locator(self.profile["data_rows"]).count()):
                    raise ValueError("Search shows both result rows and an empty-result message")
                return []
            if not table.is_visible():
                raise ValueError("Completed search has no recognized results")
            # An incomplete result set cannot establish a unique match or absence.
            if self.profile.get("next_page"):
                next_page = self.page.locator(self.profile["next_page"])
                if next_page.is_visible() and next_page.is_enabled():
                    raise PortalAutomationError(
                        "Search has more result pages. Refine the search or configure all-results view.",
                        reason="paginated_results", retry_scope=RetryScope.NEXT_RUN,
                    )
            if table.count() != 1:
                raise ValueError("Multiple result tables")
            return self.read_table(table)
        except (CancellationRequested, PortalAutomationError):
            raise
        except Exception as exc:
            if self.signed_out():
                raise self.reauth_error() from None
            detail = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            logger = getattr(self.context, "logger", None)
            if logger is not None:
                logger.warning("search.read_failed", "Claim results could not be read",
                               stage=stage, error_type=type(exc).__name__, detail=detail)
            raise PortalAutomationError(
                f"Claim results could not be read ({detail}). This invoice needs another attempt.",
                reason="unrecognized_results", recovery=RecoveryAction.BACKOFF,
                retry_scope=RetryScope.CURRENT_ROW,
            ) from None

    def result_snapshot(self):
        """Distinguish a new document or replaced result nodes from stale results.

        The node identity also detects identical repeated empty searches. If the
        portal leaves the same result nodes and content untouched, wait times out
        rather than exporting a previous search as the current one.
        """
        surface = self.page
        document_id = surface.evaluate("""() => {
            window.__waystarDocumentId ||= crypto.randomUUID();
            return window.__waystarDocumentId;
        }""")
        values = [document_id]
        for key in ("results_table", "no_results"):
            locator = surface.locator(self.profile[key]).first
            if locator.count():
                values.append(locator.evaluate("""element => {
                    element.__waystarResultId ||= crypto.randomUUID();
                    return [element.__waystarResultId, element.innerHTML];
                }"""))
            else:
                values.append(None)
        return values

    def read_table(self, table):
        # Waystar splits visible column labels from its data table. Its hidden
        # thead contains one empty th, so use the supplied 24-column layout.
        headers = self.profile.get("column_headers")
        if headers:
            table_rows = table.locator(self.profile["data_rows"]).all()
        else:
            headers = table.locator("thead th").all_text_contents()
            if not headers or not any(h.strip() for h in headers):
                headers = table.locator("tr").first.locator("th, td").all_text_contents()
            table_rows = table.locator("tr").all()[1:]
        if not table_rows:
            raise ValueError("No data rows without a verified empty-search message")
        rows = [row.locator("td").all_text_contents() for row in table_rows]
        indexes = header_indexes(headers, self.profile.get("headers"))
        if not {"patient_full_name", "invoice", "from_date", "to_date", "billed",
                "primary_reason", "paid_date", "dcn"} <= indexes.keys():
            raise ValueError("Result table is missing requested export columns")
        reasons = []
        for row, cells in zip(table_rows, rows):
            self.context.cancellation.raise_if_cancelled()
            if len(cells) != len(headers):
                raise ValueError("Result row does not match table headers")
            reason_cell = row.locator("td").nth(indexes["primary_reason"])
            reasons.append(self.read_primary_reason(reason_cell, cells[indexes["primary_reason"]]))
        return parse_table(headers, rows, self.profile.get("headers"), primary_reasons=reasons)

    def read_primary_reason(self, cell, code):
        """Read the tooltip description without exporting the displayed reason code."""
        self.context.cancellation.raise_if_cancelled()
        if not str(code).strip():
            return ""
        title = cell.get_attribute("title")
        if title and title.strip():
            return title.strip()
        titled = cell.locator("[title]")
        if titled.count() == 1:
            title = titled.get_attribute("title")
            if title and title.strip():
                return title.strip()
        selector = self.profile.get("reason_tooltip")
        if not selector:
            raise ValueError("Primary Reason needs a title attribute or a verified tooltip selector")
        # Move away before the next hover so the previous row's tooltip cannot be reused.
        self.context.page.mouse.move(0, 0)
        tooltip = self.page.locator(selector)
        self.context.actions.wait_hidden(tooltip)
        self.context.actions.wait_visible(cell)
        self.context.cancellation.raise_if_cancelled()
        cell.hover(timeout=30_000)
        self.context.actions.wait_visible(tooltip)
        if tooltip.count() != 1:
            raise ValueError("Multiple reason tooltips")
        text = tooltip.inner_text().strip()
        if not text:
            raise ValueError("Empty reason tooltip")
        return text
