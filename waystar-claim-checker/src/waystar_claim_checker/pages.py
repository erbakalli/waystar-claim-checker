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


class _SearchRequests:
    """Observe this Claims frame's search traffic without retaining query data."""

    def __init__(self, top, surface, resolve_surface=None):
        self.top = top
        self.frame = top.main_frame if surface is top else surface
        self.resolve_surface = resolve_surface
        self.pending = set()
        self.observed = False
        self.failed = False
        self.listeners = (("request", self.started), ("requestfinished", self.finished),
                          ("requestfailed", self.request_failed))

    def __enter__(self):
        for name, listener in self.listeners:
            self.top.on(name, listener)
        return self

    def __exit__(self, *_):
        for name, listener in self.listeners:
            self.top.remove_listener(name, listener)

    def started(self, request):
        try:
            surface = self.resolve_surface() if self.resolve_surface else None
            current_frame = self.top.main_frame if surface is self.top else surface
            relevant = (request.frame in (self.frame, current_frame)
                        and request.resource_type in ("document", "xhr", "fetch"))
        except Exception:
            # Service-worker requests have no frame and cannot prove completion
            # of the selected Claims grid.
            return
        if relevant:
            self.observed = True
            self.pending.add(request)

    def finished(self, request):
        if request not in self.pending:
            return
        self.pending.discard(request)
        try:
            response = request.response()
            if response is None or not 200 <= response.status < 400:
                self.failed = True
        except Exception:
            self.failed = True

    def request_failed(self, request):
        if request in self.pending:
            self.pending.discard(request)
            self.failed = True

    @property
    def completed(self):
        return self.observed and not self.pending and not self.failed


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
        # Keep the signed-in Claims page when another workbook/batch starts.
        # Re-entering SSO here can replace a working frame and stall startup.
        search_ready = False
        try:
            search_ready = not self.signed_out() and self.page.locator(
                self.profile["search_input"]
            ).is_visible()
        except Exception:
            # A detached frame needs the normal integration recovery path.
            pass
        if not search_ready:
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
            if not self.search_matches(pcn):
                raise ValueError("PCN query was not set before submitting the search")
            previous = self.result_snapshot()
            previous_postback = self.postback_state()
            candidate = None
            busy_seen = False
            postback_failed = False
            last_wait_state = {}
            with _SearchRequests(self.context.page, self.page, lambda: self.page) as requests:
                if self.profile.get("submit"):
                    actions.click(self.page.locator(self.profile["submit"]))
                else:
                    actions.press(search, "Enter")

                def completed():
                    nonlocal candidate, busy_seen, postback_failed
                    postback = self.postback_state()
                    postback_completed = postback["completed"] > previous_postback["completed"]
                    postback_failed = postback_completed and postback["failed"]
                    busy = self.profile.get("busy")
                    if busy and self.page.locator(busy).is_visible():
                        busy_seen = True
                        candidate = None
                        return False
                    # A browser call must run even while traffic is pending so
                    # synchronous Playwright can dispatch finished/failed events.
                    ready = self.page.locator(self.profile["ready"]).first.is_visible()
                    query_unchanged = self.submitted_query_unchanged(pcn)
                    last_wait_state.update(ready=ready, pending_requests=len(requests.pending),
                                          request_failed=requests.failed, postback_active=postback["active"],
                                          query_unchanged=query_unchanged)
                    if (requests.pending or requests.failed or postback["active"] or postback_failed
                            or not ready or not query_unchanged):
                        candidate = None
                        return False
                    current = self.result_snapshot()
                    confirmed = requests.completed or busy_seen or postback_completed
                    last_wait_state.update(completion_observed=confirmed, snapshot_changed=current != previous)
                    if current == previous and not confirmed:
                        candidate = None
                        return False
                    # A cleared/replaced table can appear long before its AJAX
                    # result arrives. An old empty overlay proves nothing about
                    # the new query. Require completed traffic, a busy cycle or
                    # a completed ASP.NET postback before accepting no claims.
                    empty = self.page.locator(self.profile["no_results"]).is_visible()
                    if empty and not confirmed:
                        candidate = None
                        return False
                    stable = current == candidate
                    candidate = current
                    return stable and not requests.pending and not requests.failed

                stage = "wait_for_results"
                outcome = actions.wait_for_any({
                    "completed": completed,
                    "login": self.login_visible,
                    "request_failed": lambda: requests.failed or postback_failed,
                })
            if outcome == "login":
                raise self.reauth_error()
            if outcome == "request_failed":
                raise ValueError("Search request failed; no empty result was confirmed")
            if self.profile.get("busy"):
                actions.wait_hidden(self.page.locator(self.profile["busy"]))
            if self.signed_out():
                raise self.reauth_error()
            if not self.submitted_query_unchanged(pcn):
                raise ValueError("A different query was entered before results were read")
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
            if table.count() != 1:
                raise ValueError("Multiple result tables")
            self.verify_complete_results(table)
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
                if stage == "wait_for_results":
                    logger.warning("search.wait_state", "Search completion indicators", **last_wait_state)
            raise PortalAutomationError(
                f"Claim results could not be read ({detail}). This invoice needs another attempt.",
                reason="unrecognized_results", recovery=RecoveryAction.BACKOFF,
                retry_scope=RetryScope.CURRENT_ROW,
            ) from None

    def search_matches(self, pcn):
        """Do not export results after a user or the portal changes the query."""
        surface = self.page
        selected = self.profile.get("selected_pcn")
        return (surface.locator(self.profile["search_input"]).input_value().strip().upper()
                == str(pcn).strip().upper()
                and (not selected or surface.locator(selected).is_visible()))

    def submitted_query_unchanged(self, pcn):
        """A result navigation may clear the input or reset its dropdown.

        PCN mode and the full query are verified before submit. After submit,
        only a different nonempty query means the requested search was changed.
        Returned claim PCNs are still checked before committing any result.
        """
        search = self.page.locator(self.profile["search_input"])
        if not search.count():
            return True
        value = search.input_value().strip().upper()
        return not value or value == str(pcn).strip().upper()

    def postback_state(self):
        """Wait for ASP.NET's endRequest rather than just its response body."""
        return self.page.evaluate("""() => {
            const manager = window.Sys?.WebForms?.PageRequestManager?.getInstance?.();
            if (!manager || typeof manager.get_isInAsyncPostBack !== 'function') {
                return {active: false, completed: 0, failed: false};
            }
            let state = window.__waystarPostbackObserver;
            if (!state || state.manager !== manager) {
                state = {manager, completed: 0, failed: false};
                window.__waystarPostbackObserver = state;
                manager.add_endRequest((sender, args) => {
                    state.completed += 1;
                    state.failed = typeof args?.get_error === 'function' && !!args.get_error();
                });
            }
            return {active: manager.get_isInAsyncPostBack(),
                    completed: state.completed, failed: state.failed};
        }""")

    def verify_complete_results(self, table):
        """Defer partial or unverified grids, even without a profile pager selector.

        Telerik documents page count/current index on GridTableView and the
        default pager's ``Page X of Y, items A to B of N`` text. Read those and
        the current grid's controls; never navigate or change its page size.
        """
        self.context.cancellation.raise_if_cancelled()

        def incomplete():
            raise PortalAutomationError(
                "Search has more result pages. Refine the search or configure all-results view.",
                reason="paginated_results", retry_scope=RetryScope.NEXT_RUN,
            )

        if self.profile.get("next_page"):
            # Top and bottom pagers can both match. HTML disabled attributes,
            # ARIA and ASP.NET/Telerik disabled containers all suppress links.
            for control in self.page.locator(self.profile["next_page"]).all():
                if (control.is_visible() and control.is_enabled() and not control.evaluate(
                    "element => !!element.closest('[disabled], [aria-disabled=\"true\"], .aspNetDisabled, .rgDisabled')"
                )):
                    incomplete()

        state = table.evaluate("""(table, rowSelector) => {
            const grid = table.closest('.RadGrid') || table;
            const visible = element => !!element.getClientRects().length &&
                getComputedStyle(element).visibility !== 'hidden';
            const belongs = element => !element.closest('.RadGrid') ||
                element.closest('.RadGrid') === grid;
            const disabled = element => !!element.closest(
                '[disabled], [aria-disabled="true"], .aspNetDisabled, .rgDisabled');
            const pagers = [...grid.querySelectorAll('.rgPager, .rgPagerCell, .rgInfoPart')]
                .filter(element => belongs(element) && visible(element));
            const controls = [...grid.querySelectorAll(
                '.rgPageNext, .rgPageLast, .rgPagePrev, .rgPageFirst, .rgNumPart a:not(.rgCurrentPage)')]
                .filter(element => belongs(element) && visible(element));
            const result = {
                pager_texts: pagers.map(element => element.innerText),
                pager_present: pagers.length > 0 || controls.length > 0,
                navigation_enabled: controls.some(element => !disabled(element)),
                page_count: null, page_index: null, metadata_error: false,
                row_count: rowSelector ? table.querySelectorAll(rowSelector).length : null
            };
            if (typeof window.$find === 'function') {
                try {
                    let view = window.$find(table.id);
                    if (!view || typeof view.get_pageCount !== 'function') {
                        const owner = window.$find(grid.id);
                        view = owner && typeof owner.get_masterTableView === 'function'
                            ? owner.get_masterTableView() : null;
                    }
                    // A wrapper can also contain detail tables. Its master
                    // view's metadata must describe this exact result table.
                    if (view && typeof view.get_element === 'function' &&
                            view.get_element() === table && typeof view.get_pageCount === 'function') {
                        const count = view.get_pageCount();
                        const index = typeof view.get_currentPageIndex === 'function'
                            ? view.get_currentPageIndex() : null;
                        if (!Number.isInteger(count) || count < 0 ||
                                (index !== null && (!Number.isInteger(index) || index < 0))) {
                            result.metadata_error = true;
                        } else {
                            result.page_count = count;
                            result.page_index = index;
                        }
                    }
                } catch (_) {
                    result.metadata_error = true;
                }
            }
            return result;
        }""", self.profile.get("data_rows"))
        if state["navigation_enabled"] or (state["page_count"] or 0) > 1 or (state["page_index"] or 0) > 0:
            incomplete()
        single_page = state["page_count"] == 1
        unverified = False
        row_count = state["row_count"]
        for text in state["pager_texts"]:
            pages = re.search(r"\bpage\s+([\d,]+)\s+of\s+([\d,]+)\b", text, re.IGNORECASE)
            if pages:
                current, total = (int(value.replace(",", "")) for value in pages.groups())
                if current > 1 or total > 1:
                    incomplete()
                if current != 1 or total != 1:
                    unverified = True
                single_page = single_page or current == total == 1
            items = re.search(r"\bitems?\s+([\d,]+)\s+to\s+([\d,]+)\s+of\s+([\d,]+)\b", text, re.IGNORECASE)
            if items:
                first, last, total = (int(value.replace(",", "")) for value in items.groups())
                if first > 1 or last < total or (row_count is not None and total > row_count):
                    incomplete()
                if first < 1 or last < first or total < last or (row_count is not None and total < row_count):
                    unverified = True
                # Counts must agree with visible data rows before they can
                # establish completeness without a page-count indicator.
                if row_count is not None and first == 1 and last == total == row_count:
                    single_page = True
        if state["metadata_error"] or unverified or (state["pager_present"] and not single_page):
            raise PortalAutomationError(
                "The result grid has paging information whose completeness could not be verified. "
                "Configure an all-results view or verify the pager before retrying this invoice.",
                reason="pagination_unverified", retry_scope=RetryScope.NEXT_RUN,
            )

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
        claims = parse_table(headers, rows, self.profile.get("headers"), primary_reasons=reasons)
        if not claims:
            raise ValueError("Result rows are empty without a verified empty-search message")
        return claims

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
