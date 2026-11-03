"""Read-only Waystar browser adapter with an explicitly calibrated result profile."""
import json
import re
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from kantime_engine import PortalAutomationError, RecoveryAction, RetryScope, BrowserSpec
from kantime_engine.cancellation import CancellationRequested
from .claims import header_indexes, parse_table

CLAIMS_URL = (
    "https://mgmt.zirmed.com/ExternalUserManagement/IntegrationRedirect/Index"
    "?tabappid=87&redirecturl=https:%2F%2Fclaimseditor.ecorpnet.com%2Fmember%2FSelectSummary.aspx"
)
HH_CLAIMS_URL = (
    "https://mgmt.zirmed.com/ExternalUserManagement/IntegrationRedirect/Index"
    "?tabappid=85&redirecturl=https%3A%2F%2Fanalytics.ecorpnet.com%2Fmember%2FSelectSummary.aspx"
)


def _surface_url(surface):
    """Edge may omit an SSO frame's reported URL after cross-process navigation."""
    reported = str(surface.url)
    if reported not in ("", "about:blank"):
        return reported
    try:
        actual = surface.evaluate("() => location.href")
        return actual if isinstance(actual, str) else reported
    except CancellationRequested:
        raise
    except Exception:
        return reported


class _SearchRequests:
    """Observe this Claims frame's search traffic without retaining query data."""

    def __init__(self, top, surface, resolve_surface=None, search_only=False):
        self.top = top
        self.frame = top.main_frame if surface is top else surface
        self.resolve_surface = resolve_surface
        self.search_only = search_only
        self.surface_url = _surface_url(surface)
        self.ignored = 0
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
            if relevant and self.search_only and request.resource_type != "document":
                address = urlparse(str(request.url))
                original = urlparse(self.surface_url)
                current = urlparse(_surface_url(surface or self.frame))
                # Session keep-alives, telemetry and other background fetches
                # do not establish (or delay) completion of a PCN search.
                endpoint = address.path.lower()
                background = bool(re.match(
                    r"session|keep[-_]?alive|heartbeat|telemetry|beacon|ping|signalr|sockjs|analytics|tracking|notification",
                    endpoint.rsplit('/', 1)[-1]))
                relevant = (address.hostname in (original.hostname, current.hostname)
                            and (endpoint in (original.path.lower(), current.path.lower())
                                 or not background))
                if not relevant:
                    self.ignored += 1
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

    def confirm_document(self, surface):
        """A committed result document need not wait for its load to finish.

        Called only after the result snapshot proves a new document. Match the
        actual Claims frame and final URL, and require a successful response;
        an unrelated document request cannot confirm this search. The caller
        also requires a ready, stable result grid. Do not require interactive:
        a stalled trailing script can keep readyState at loading even though
        the new grid and its reason links are already usable.
        A redirect's finished event can also be missing: confirm its terminal
        response, then release the matching redirect chain together.
        """
        frame = self.top.main_frame if surface is self.top else surface
        for request in tuple(self.pending):
            if request.resource_type != "document" or request.frame != frame:
                continue
            chain = [request]
            while chain[-1].redirected_to is not None and len(chain) < 50:
                target = chain[-1].redirected_to
                if target in chain:
                    break
                chain.append(target)
            final = chain[-1]
            if final.frame != frame or str(final.url) != _surface_url(surface):
                continue
            response = final.response()
            if response is not None and 200 <= response.status < 300:
                self.pending.difference_update(chain)

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
        browser_spec = getattr(getattr(context, "variant", None), "browser", None)
        self.auto_portal = getattr(getattr(context, "variant", None), "id", None) == "automatic"
        self.start_url = browser_spec.start_url if isinstance(browser_spec, BrowserSpec) else CLAIMS_URL
        redirect = parse_qs(urlparse(self.start_url).query).get("redirecturl", [""])[0]
        self.portal_host = urlparse(redirect).hostname or "claimseditor.ecorpnet.com"

    @property
    def page(self):
        top = self.context.page
        if self.auto_portal:
            candidates = []
            observed_hosts = set()
            for surface in [top, *top.frames]:
                address = urlparse(_surface_url(surface))
                if (address.hostname not in ("claimseditor.ecorpnet.com", "analytics.ecorpnet.com")
                        or address.path.lower().endswith("/reason.aspx")):
                    continue
                observed_hosts.add(address.hostname)
                try:
                    if surface.locator(self.profile["search_input"]).first.is_visible():
                        candidates.append(surface)
                except CancellationRequested:
                    raise
                except Exception:
                    continue
            hosts = {urlparse(_surface_url(item)).hostname for item in candidates} or observed_hosts
            if len(hosts) > 1:
                raise PortalAutomationError("More than one Claims portal is visible. Keep the portal to check open and retry.",
                                            reason="ambiguous_portal", recovery=RecoveryAction.STOP)
            if hosts:
                self.portal_host = next(iter(hosts))
                self.start_url = HH_CLAIMS_URL if self.portal_host == "analytics.ecorpnet.com" else CLAIMS_URL
            if candidates:
                return candidates[0]
        def claims_surface(surface):
            address = urlparse(_surface_url(surface))
            return (address.hostname == self.portal_host
                    and not address.path.lower().endswith('/reason.aspx'))

        if claims_surface(top):
            return top
        # The integration URL remains on mgmt.zirmed.com while sessionExtendFrame
        # redirects into Claims. Playwright includes nested frames in page.frames.
        candidates = [frame for frame in top.frames if claims_surface(frame)]
        for frame in candidates:
            try:
                if frame.locator(self.profile["search_input"]).count() > 0:
                    return frame
            except Exception:
                continue
        return candidates[0] if candidates else top

    def login_visible(self):
        for surface in (self.context.page, *self.context.page.frames):
            host = urlparse(_surface_url(surface)).hostname
            if host not in (("mgmt.zirmed.com", "claimseditor.ecorpnet.com", "analytics.ecorpnet.com")
                            if self.auto_portal else ("mgmt.zirmed.com", self.portal_host)):
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
        host = urlparse(_surface_url(self.page)).hostname or ""
        return host != self.portal_host or self.login_visible()

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
            self.context.actions.goto(self.context.page, self.start_url, timeout_ms=60_000)
        self.select_pcn()

    def read_office(self):
        """Read the active Account heading once, excluding its numeric account ID."""
        office = ""
        def captured():
            nonlocal office
            top = self.context.page
            surfaces = [self.page, top, *top.frames]
            seen = set()
            for surface in surfaces:
                if id(surface) in seen:
                    continue
                seen.add(id(surface))
                if urlparse(_surface_url(surface)).hostname not in (
                    "mgmt.zirmed.com", self.portal_host
                ):
                    continue
                try:
                    # A detached/loading frame must not consume the full default
                    # Playwright timeout or prevent reading the outer header.
                    text = surface.locator("body").inner_text(timeout=500)
                except CancellationRequested:
                    raise
                except Exception:
                    continue
                match = re.search(r"\bAccount\s*:?\s*([^()]{1,200}?)\s*\(\s*\d+\s*\)", text, re.IGNORECASE)
                if match:
                    office = " ".join(match.group(1).split())
                    return bool(office)
            return False
        try:
            outcome = self.context.actions.wait_for_any(
                {"login": self.login_visible, "account": captured}, timeout_ms=10_000)
            if outcome == "login" or self.signed_out():
                raise self.reauth_error()
            if not office:
                raise ValueError("Account heading has no office name")
            return office
        except (CancellationRequested, PortalAutomationError):
            raise
        except Exception:
            return ""

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
        self.progress(f"Opening claim: Invoice No: {pcn}", "claims.opening")
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
            self.progress("Waiting for this invoice's claim results...", "claims.search_waiting")
            candidate = None
            busy_seen = False
            postback_failed = False
            last_wait_state = {}
            with _SearchRequests(self.context.page, self.page, lambda: self.page, search_only=True) as requests:
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
                    current = self.result_snapshot() if ready and query_unchanged else None
                    if (isinstance(previous, list) and isinstance(current, list)
                            and current[0] != previous[0]):
                        requests.confirm_document(self.page)
                    last_wait_state.update(ready=ready, pending_requests=len(requests.pending),
                                          request_failed=requests.failed, postback_active=postback["active"],
                                          query_unchanged=query_unchanged, ignored_background_requests=requests.ignored,
                                          pending_request_types=[request.resource_type for request in requests.pending],
                                          pending_endpoint_files=[
                                              urlparse(str(request.url)).path.rsplit('/', 1)[-1]
                                              if re.search(r'\.(aspx|ashx|asmx)$', urlparse(str(request.url)).path, re.I)
                                              else '[other endpoint]' for request in requests.pending])
                    if (requests.pending or requests.failed or postback["active"] or postback_failed
                            or not ready or not query_unchanged):
                        candidate = None
                        return False
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
            self.progress("Claim results are ready. Reading every returned claim...", "claims.results_ready")
            return self.read_all_results(table, pcn)
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

    def progress(self, message, event):
        logger = getattr(self.context, "logger", None)
        if logger is not None:
            logger.user(message, event=event)

    def paging_position(self, table):
        """Read the current grid's page identity, without changing its page size."""
        state = table.evaluate("""table => {
            const grid = table.closest('.RadGrid') || table;
            const visible = e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
            const texts = [...grid.querySelectorAll('.rgPager, .rgPagerCell, .rgInfoPart')]
                .filter(e => visible(e) && (!e.closest('.RadGrid') || e.closest('.RadGrid') === grid))
                .map(e => e.innerText);
            let view = typeof window.$find === 'function' ? window.$find(table.id) : null;
            if (!view?.get_pageCount) view = typeof window.$find === 'function'
                ? window.$find(grid.id)?.get_masterTableView?.() : null;
            const own = view?.get_element?.() === table;
            return {texts, count: own ? view.get_pageCount?.() : null,
                    index: own ? view.get_currentPageIndex?.() : null};
        }""")
        positions = []
        count, index = state.get("count"), state.get("index")
        if isinstance(count, int) and isinstance(index, int) and count > 0 and 0 <= index < count:
            positions.append((index, count))
        totals = set()
        for text in state.get("texts", []):
            pages = re.search(r"\bpage\s+([\d,]+)\s+of\s+([\d,]+)\b", text, re.I)
            if pages:
                current, total = (int(value.replace(',', '')) for value in pages.groups())
                if not 1 <= current <= total:
                    raise ValueError("Invalid claim result page numbers")
                positions.append((current - 1, total))
            items = re.search(r"\bitems?\s+[\d,]+\s+to\s+[\d,]+\s+of\s+([\d,]+)\b", text, re.I)
            if items:
                totals.add(int(items.group(1).replace(',', '')))
        if not positions or len(set(positions)) != 1 or len(totals) > 1:
            raise ValueError("Claim result page position could not be verified")
        return (*positions[0], next(iter(totals)) if totals else None)

    def read_all_results(self, table, pcn):
        try:
            self.verify_complete_results(table)
        except PortalAutomationError as exc:
            if exc.reason != "paginated_results":
                raise
            try:
                index, page_count, total_items = self.paging_position(table)
            except ValueError:
                raise exc
        else:
            claims = self.read_table(table)
            self.progress(f"All {len(claims)} returned claims have been read.", "claims.invoice_complete")
            return claims

        if index != 0:
            raise ValueError("Claim search did not start on the first result page")
        claims = []
        for expected in range(page_count):
            self.context.cancellation.raise_if_cancelled()
            table = self.page.locator(self.profile["results_table"])
            position = self.paging_position(table)
            if position[:2] != (expected, page_count) or position[2] != total_items:
                raise ValueError("Claim result paging changed before all claims were read")
            if not self.submitted_query_unchanged(pcn):
                raise ValueError("A different invoice was entered while reading claim pages")
            self.progress(f"Reading claim result page {expected + 1}/{page_count}...", "claims.page_reading")
            claims.extend(self.read_table(table))
            if expected + 1 == page_count:
                break
            selector = self.profile.get("next_page")
            controls = (self.page.locator(selector) if selector else
                        table.locator("xpath=ancestor-or-self::*[contains(concat(' ', normalize-space(@class), ' '), ' RadGrid ')][1]")
                        .locator('.rgPageNext'))
            next_control = None
            for control in controls.all():
                if (control.is_visible() and control.is_enabled() and not control.evaluate(
                        "e => !!e.closest('[disabled], [aria-disabled=\"true\"], .aspNetDisabled, .rgDisabled')")):
                    next_control = control
                    break
            if next_control is None:
                raise ValueError("Next claim result page has no usable Next control")
            previous = self.result_snapshot()
            candidate = None
            with _SearchRequests(self.context.page, self.page, lambda: self.page, search_only=True) as requests:
                self.context.actions.click(next_control)
                def advanced():
                    nonlocal candidate
                    self.context.cancellation.raise_if_cancelled()
                    if self.signed_out():
                        raise self.reauth_error()
                    current_table = self.page.locator(self.profile["results_table"])
                    current = self.result_snapshot()
                    if isinstance(current, list) and current[0] != previous[0]:
                        requests.confirm_document(self.page)
                    busy = self.profile.get("busy")
                    if (requests.failed or requests.pending or self.postback_state()["active"]
                            or (busy and self.page.locator(busy).is_visible())
                            or self.paging_position(current_table) != (expected + 1, page_count, total_items)
                            or current == previous):
                        candidate = None
                        return False
                    stable = current == candidate
                    candidate = current
                    return stable
                outcome = self.context.actions.wait_for_any({"advanced": advanced,
                    "login": self.login_visible, "failed": lambda: requests.failed})
                if outcome == "login":
                    raise self.reauth_error()
                if outcome == "failed":
                    raise ValueError("Next claim result page request failed")
        if total_items is not None and len(claims) != total_items:
            raise ValueError("Not all returned claims were read; this invoice must be retried")
        self.progress(f"All {len(claims)} returned claims have been read.", "claims.invoice_complete")
        return claims

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

    def result_headers(self, table, configured, column_count):
        """Use this grid's own column order, including its split header table."""
        candidates = table.evaluate("""table => {
            const root = table.closest('.RadGrid') || table.parentElement;
            const candidates = [];
            const label = element => (element?.textContent || '').trim();
            const firstRow = table.querySelector('tbody > tr.rgRow, tbody > tr.rgAltRow');
            if (firstRow) {
                const labels = [...firstRow.children].filter(cell => cell.tagName === 'TD')
                    .map(cell => (cell.getAttribute('headers') || '').split(/\\s+/)
                        .map(id => label(table.ownerDocument.getElementById(id))).filter(Boolean).join(' '));
                if (labels.some(Boolean)) candidates.push(labels);
            }
            if (root) {
                for (const row of root.querySelectorAll('thead tr')) {
                    const cells = [...row.children].filter(cell => ['TH', 'TD'].includes(cell.tagName));
                    candidates.push(cells.map(label));
                    candidates.push(cells.filter(cell => getComputedStyle(cell).display !== 'none').map(label));
                }
            }
            if (typeof window.$find === 'function') {
                try {
                    let view = window.$find(table.id);
                    if (!view || typeof view.get_columns !== 'function') {
                        view = window.$find(root?.id)?.get_masterTableView?.();
                    }
                    if (view?.get_element?.() === table && typeof view.get_columns === 'function') {
                        const columns = view.get_columns();
                        const name = column => label(column.get_element?.()) || column.get_uniqueName?.() || '';
                        candidates.push(columns.map(name));
                        candidates.push(columns.filter(column => column.get_visible?.() !== false).map(name));
                    }
                } catch (_) {}
            }
            return candidates;
        }""")
        required = {"beneficiary_id", "from_date", "to_date", "billed", "status",
                    "patient_full_name", "invoice", "primary_reason", "paid_date", "dcn"}
        if isinstance(candidates, list):
            for headers in candidates:
                if not isinstance(headers, list) or len(headers) != column_count:
                    continue
                try:
                    indexes = header_indexes(headers, self.profile.get("headers"))
                except ValueError:
                    continue
                if required <= indexes.keys():
                    return headers
        if len(configured) == column_count:
            return configured
        raise ValueError(f"Result grid has {column_count} columns; its column headings could not be matched")

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
        # Tooltip/detail tables can contain their own td elements. They are not
        # columns of the claim row.
        rows = [row.locator(":scope > td").all_text_contents() for row in table_rows]
        if self.profile.get("column_headers"):
            headers = self.result_headers(table, headers, len(rows[0]))
        indexes = header_indexes(headers, self.profile.get("headers"))
        if not {"patient_full_name", "invoice", "from_date", "to_date", "billed",
                "primary_reason", "paid_date", "dcn"} <= indexes.keys():
            raise ValueError("Result table is missing requested export columns")
        reasons = []
        for number, (row, cells) in enumerate(zip(table_rows, rows), 1):
            self.context.cancellation.raise_if_cancelled()
            if len(cells) != len(headers):
                raise ValueError("Result row does not match table headers")
            claim_index = indexes.get("claim_id", indexes["dcn"])
            claim_id = cells[claim_index].strip() or cells[indexes["dcn"]].strip()
            invoice = cells[indexes["invoice"]].strip()
            self.progress(
                f"Opening claim: {claim_id or str(number)} | Invoice No: {invoice} ({number}/{len(rows)})",
                "claims.opening",
            )
            reason_cell = row.locator(":scope > td").nth(indexes["primary_reason"])
            self.progress(f"Reading Primary Reason for claim {number}/{len(rows)}...", "claims.reason_reading")
            reasons.append(self.read_primary_reason(reason_cell, cells[indexes["primary_reason"]]))
            self.progress(f"Primary Reason finished for claim {number}/{len(rows)}.", "claims.reason_finished")
        claims = parse_table(headers, rows, self.profile.get("headers"), primary_reasons=reasons)
        if not claims:
            raise ValueError("Result rows are empty without a verified empty-search message")
        return claims

    def read_primary_reason(self, cell, code):
        """Read the description from a reason window or a descriptive tooltip."""
        self.context.cancellation.raise_if_cancelled()
        if not str(code).strip():
            return ""
        # Detect the current cell's capabilities, independent of payer and host.
        # Recheck every row because a grid can contain both popup and tooltip cells.
        link = cell.locator('a[href*="reason.aspx" i]:visible, [onclick*="reason.aspx" i]:visible')
        link_count = link.count()
        if link_count > 1:
            raise ValueError("Primary Reason has multiple description popup targets")
        if link_count == 1:
            self.progress("Primary Reason uses a clickable description popup.", "claims.reason_method_click")
            # A broken popup must fail the invoice; never silently accept its title.
            return self.read_reason_window(link, link.inner_text().strip())
        self.progress("Primary Reason uses a tooltip description.", "claims.reason_method_hover")
        title = cell.get_attribute("title")
        if self.is_reason_description(title) and title.strip().casefold() != str(code).strip().casefold():
            return title.strip()
        titled = cell.locator("[title]")
        if titled.count() == 1:
            title = titled.get_attribute("title")
            if self.is_reason_description(title) and title.strip().casefold() != str(code).strip().casefold():
                return title.strip()
        selector = self.profile.get("reason_tooltip") or '[role="tooltip"]:visible, .RadToolTip:visible'
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
        if (not self.is_reason_description(text)
                or text.casefold() == str(code).strip().casefold()):
            raise ValueError("Reason tooltip has no verified description")
        return text

    @staticmethod
    def is_reason_description(text):
        return (isinstance(text, str) and bool(re.search(r"[A-Za-z]", text))
                and not re.fullmatch(r"(?=[A-Za-z0-9]*\d)[A-Za-z0-9]+", text.strip())
                and not re.search(r"<[^>]+>|&lt;|javascript:|(?:click|hover)\s+(?:for|to\s+(?:view|see))\s+(?:a\s+)?description", text, re.I))

    def read_reason_window(self, link, code):
        target = (link.get_attribute("href") or "") + " " + (link.get_attribute("onclick") or "")
        match = re.search(r"reason\.aspx\?reasonid=(\d+)", target, re.I)
        if not match:
            raise ValueError("Reason link has no recognized description target")
        reason_id = match.group(1)
        description = ""

        def surfaces():
            return list({id(surface): surface for surface in
                         [self.page, self.context.page, *self.context.page.frames]}.values())

        def read():
            nonlocal description
            self.context.cancellation.raise_if_cancelled()
            for surface in surfaces():
                address = urlparse(_surface_url(surface))
                is_reason_frame = (address.path.lower().endswith("/reason.aspx")
                                   and parse_qs(address.query).get("reasonid") == [reason_id])
                try:
                    text = surface.evaluate("""({code, frame}) => {
                        const roots = frame ? [document.body] :
                            [...document.querySelectorAll('.RadWindow, [role="dialog"]')];
                        for (const root of roots) {
                            if (!root || !root.getClientRects().length) continue;
                            const label = root.innerText.match(/\\bReason Code\\s*:\\s*([A-Z0-9]+)\\b/i);
                            if (!label || label[1].toUpperCase() !== code.trim().toUpperCase()) continue;
                            const field = root.querySelector('textarea[id$="_txtReasonDesc"]') || root.querySelector('textarea');
                            if (field && field.getClientRects().length && field.value.trim()) {
                                return field.value.trim();
                            }
                        }
                        return '';
                    }""", {"code": str(code).strip(), "frame": is_reason_frame})
                    if self.is_reason_description(text):
                        description = text
                        return True
                except CancellationRequested:
                    raise
                except Exception:
                    # The reason iframe can detach while Telerik replaces it.
                    continue
            return False

        def close():
            for surface in surfaces():
                try:
                    closed = surface.evaluate("""({id, code}) => {
                        const windows = [...document.querySelectorAll('.RadWindow, [role="dialog"]')];
                        for (const [index, root] of windows.entries()) {
                            if (!root.getClientRects().length) continue;
                            const label = root.innerText.match(/\\bReason Code\\s*:\\s*([A-Z0-9]+)\\b/i);
                            const matchingFrame = [...root.querySelectorAll('iframe')].some(frame => {
                                try {
                                    const url = new URL(frame.src, document.baseURI);
                                    return url.pathname.toLowerCase().endsWith('/reason.aspx') &&
                                        url.searchParams.get('reasonid') === id;
                                } catch (_) { return false; }
                            });
                            if (!matchingFrame && (!label || label[1].toUpperCase() !== code.trim().toUpperCase())) continue;
                            const button = root.querySelector(
                                '.rwCloseButton, .rwClose, [title="Close"], [aria-label="Close"]');
                            if (button) return {index};
                        }
                        return false;
                    }""", {"id": reason_id, "code": str(code).strip()})
                    if isinstance(closed, dict) and "index" in closed:
                        # Telerik's lightweight Close control is a span. Use a
                        # pointer click so its mouse handlers also run.
                        wrapper = surface.locator('.RadWindow, [role="dialog"]').nth(closed["index"])
                        wrapper.locator('.rwCloseButton, .rwClose, [title="Close"], [aria-label="Close"]').first.click(timeout=3000)
                        return True
                except Exception:
                    continue
            return False

        def is_closed():
            for surface in surfaces():
                try:
                    visible = surface.evaluate("""({id, code}) => {
                        return [...document.querySelectorAll('.RadWindow, [role="dialog"]')].some(root => {
                            if (!root.getClientRects().length || getComputedStyle(root).visibility === 'hidden') return false;
                            const label = root.innerText.match(/\\bReason Code\\s*:\\s*([A-Z0-9]+)\\b/i);
                            return (label && label[1].toUpperCase() === code.trim().toUpperCase()) || [...root.querySelectorAll('iframe')].some(frame => {
                                try {
                                    const url = new URL(frame.src, document.baseURI);
                                    return url.pathname.toLowerCase().endsWith('/reason.aspx') &&
                                        url.searchParams.get('reasonid') === id;
                                } catch (_) { return false; }
                            });
                        });
                    }""", {"id": reason_id, "code": str(code).strip()})
                    if visible:
                        return False
                except Exception:
                    continue
            return True

        self.progress("Clicking the Primary Reason code to open its popup...", "claims.reason_opening")
        self.context.actions.click(link)
        try:
            self.context.actions.wait_for_any({"description": read})
        finally:
            self.progress("Closing the Primary Reason popup with X...", "claims.reason_closing")
            closed = close()
            if not closed:
                raise ValueError("Reason description window could not be closed")
            self.context.actions.wait_for_any({"closed": is_closed})
            self.progress("Primary Reason popup is closed.", "claims.reason_closed")
        return description
