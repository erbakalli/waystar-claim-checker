from pathlib import Path
from datetime import date
from decimal import Decimal
import json
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch, call

ROOT = Path(__file__).resolve().parents[1]
for folder in (ROOT / "src", ROOT.parent / "Kantime-engine-main" / "src"):
    sys.path.insert(0, str(folder))

from kantime_engine import PortalAutomationError, RecoveryAction, RetryScope
from kantime_engine.cancellation import CancellationRequested
from waystar_claim_checker.pages import ClaimsPage, CLAIMS_URL, _SearchRequests


class PortalTests(unittest.TestCase):
    def test_open_reuses_signed_in_claims_page_for_another_workbook(self):
        adapter, locators = self.adapter()
        locators["#search"].is_visible.return_value = True
        adapter.open()
        adapter.context.actions.goto.assert_not_called()
        self.assertEqual(adapter.context.actions.click.call_args_list,
                         [call(locators["#search-mode"]), call(locators["#pcn"])])

    def test_open_reuses_signed_in_embedded_claims(self):
        adapter, locators, outer, frame = self.embedded_adapter()
        locators["#search"].is_visible.return_value = True
        adapter.open()
        adapter.context.actions.goto.assert_not_called()
        self.assertIs(adapter.page, frame)

    def test_open_recovers_when_claims_search_is_missing(self):
        adapter, locators = self.adapter()
        adapter.open()
        adapter.context.actions.goto.assert_called_once_with(
            adapter.context.page, CLAIMS_URL, timeout_ms=60_000)

    def test_office_is_read_from_account_heading_without_account_number(self):
        adapter, locators = self.adapter()
        body = Mock()
        locators["body"] = body
        body.inner_text.return_value = "Waystar\nAccount\nOther Home Care (000123)\nWelcome, User"
        adapter.context.actions.wait_for_any.side_effect = lambda outcomes: (
            "account" if outcomes["account"]() else "login")
        self.assertEqual(adapter.read_office(), "Other Home Care")

    def test_missing_account_stops_without_assuming_an_office(self):
        adapter, locators = self.adapter()
        adapter.context.actions.wait_for_any.side_effect = TimeoutError("No Account heading")
        with self.assertRaises(PortalAutomationError) as caught:
            adapter.read_office()
        self.assertEqual(caught.exception.reason, "office_not_found")

    def split_header_table(self):
        adapter, _ = self.adapter()
        example = json.loads((ROOT / "tests" / "portal-profile.fixture.json").read_text())
        adapter.profile.update(column_headers=example["column_headers"], data_rows=example["data_rows"])
        row_values = []
        table_rows = []
        for number in range(3):
            cells = [""] * 24
            cells[1] = "Rejected"
            cells[2] = "EXAMPLE, PATIENT"
            cells[7] = "TEST-MBI"
            cells[8] = "TEST-PCN"
            cells[12:14] = ["05/01/26", "05/31/26"]
            cells[15] = "$100.01"
            cells[14] = "06/01/26"
            cells[16] = "$80.00" if number == 0 else ""
            cells[17] = "06/10/26" if number == 0 else ""
            cells[18] = str(10000 + number)
            cells[19] = "000000" + str(number)
            cells[20] = "CROSS-REFERENCE-" + str(number)
            reason = Mock()
            reason.get_attribute.return_value = "Synthetic description " + str(number)
            cell_locator = Mock()
            cell_locator.all_text_contents.return_value = cells
            cell_locator.nth.side_effect = lambda index, reason=reason: reason if index == 18 else None
            row = Mock()
            row.locator.return_value = cell_locator
            table_rows.append(row)
            row_values.append(cells)
        table = Mock()
        table.locator.return_value.all.return_value = table_rows
        return adapter, table, row_values

    def test_split_hidden_header_preserves_all_three_rows_and_columns(self):
        adapter, table, _ = self.split_header_table()
        claims = adapter.read_table(table)
        self.assertEqual(len(claims), 3)
        self.assertEqual([c.dcn for c in claims], ["0000000", "0000001", "0000002"])
        self.assertEqual([c.primary_reason for c in claims],
                         ["Synthetic description 0", "Synthetic description 1", "Synthetic description 2"])
        self.assertEqual(claims[0].invoice, "TEST-PCN")
        self.assertEqual(claims[0].patient_full_name, "EXAMPLE, PATIENT")
        self.assertEqual(claims[0].from_date, date(2026, 5, 1))
        self.assertEqual(claims[0].billed, Decimal("100.01"))
        self.assertEqual(claims[0].received_date, date(2026, 6, 1))
        self.assertEqual(claims[0].paid, Decimal("80.00"))
        self.assertIsNone(claims[1].paid)
        self.assertEqual(claims[0].paid_date, date(2026, 6, 10))
        self.assertIsNone(claims[1].paid_date)
        table.locator.assert_called_once_with(adapter.profile["data_rows"])

    def test_split_header_rejects_changed_column_count(self):
        adapter, table, rows = self.split_header_table()
        rows[0].pop()
        with self.assertRaises(ValueError):
            adapter.read_table(table)

    def test_pcn_results_allow_missing_beneficiary_dcn_or_status(self):
        for missing in ((7,), (19,), (1,), (7, 19, 1)):
            with self.subTest(missing=missing):
                adapter, table, rows = self.split_header_table()
                for index in missing:
                    rows[0][index] = ""
                claims = adapter.read_table(table)
                self.assertEqual(len(claims), 3)
                self.assertEqual(claims[0].invoice, "TEST-PCN")
                self.assertEqual(claims[0].billed, Decimal("100.01"))
                if 7 in missing:
                    self.assertEqual(claims[0].beneficiary_id, "")
                if 19 in missing:
                    self.assertEqual(claims[0].dcn, "")
                if 1 in missing:
                    self.assertEqual(claims[0].status, "")

    def test_pcn_results_still_reject_missing_pcn(self):
        adapter, table, rows = self.split_header_table()
        rows[0][8] = ""
        with self.assertRaisesRegex(ValueError, "empty PCN"):
            adapter.read_table(table)

    def test_split_header_empty_table_needs_explicit_no_results(self):
        adapter, table, _ = self.split_header_table()
        table.locator.return_value.all.return_value = []
        with self.assertRaises(ValueError):
            adapter.read_table(table)

    def test_all_blank_data_rows_are_not_exported_as_not_found(self):
        adapter, table, rows = self.split_header_table()
        for cells in rows:
            cells[:] = [""] * len(cells)
        with self.assertRaisesRegex(ValueError, "Result rows are empty"):
            adapter.read_table(table)

    def adapter(self):
        profile = dict(initial_search_input="#initial-search", search_mode_toggle="#search-mode",
                       pcn_option="#pcn", search_input="#search", results_table="#results",
                       ready="#completed", no_results="#empty", next_page="#next")
        locators = {selector: Mock() for selector in (*profile.values(), 'input[type="password"]')}
        for locator in locators.values():
            locator.is_visible.return_value = False
            locator.count.return_value = 1
            locator.first = locator
            locator.all.return_value = [locator]
            locator.evaluate.return_value = False
        locators["#results"].evaluate.return_value = dict(
            pager_texts=[], pager_present=False, navigation_enabled=False, page_count=None,
            page_index=None, metadata_error=False, row_count=None)
        page = Mock(url="https://claimseditor.ecorpnet.com/member/SelectSummary.aspx", frames=[])
        page.evaluate.return_value = dict(active=False, completed=0, failed=False)
        page.locator.side_effect = lambda selector: locators[selector]
        context = SimpleNamespace(page=page, actions=Mock(), cancellation=Mock())
        context.actions.fill.side_effect = lambda locator, value: setattr(
            locator.input_value, "return_value", str(value))
        context.actions.wait_for_any.return_value = "search"
        return ClaimsPage(context, profile), locators

    def embedded_adapter(self):
        adapter, locators = self.adapter()
        claim_frame = adapter.context.page
        outer = Mock(url=CLAIMS_URL)
        outer.locator.return_value.is_visible.return_value = False
        sso = Mock(url="https://mgmt.zirmed.com/ExternalUserManagement/integrationsso/index")
        sso.locator.return_value.is_visible.return_value = False
        outer.frames = [sso, claim_frame]
        adapter.context.page = outer
        return adapter, locators, outer, claim_frame

    def test_embedded_claims_navigates_outer_tab_and_selects_pcn_inside_frame(self):
        adapter, locators, outer, frame = self.embedded_adapter()
        adapter.context.actions.wait_for_any.return_value = "search"
        adapter.open()
        self.assertIs(adapter.page, frame)
        self.assertFalse(adapter.signed_out())
        adapter.context.actions.goto.assert_called_once_with(outer, CLAIMS_URL, timeout_ms=60_000)
        self.assertEqual(adapter.context.actions.click.call_args_list,
                         [call(locators["#search-mode"]), call(locators["#pcn"])])

    def test_claim_frame_is_resolved_again_after_sso_replaces_it(self):
        adapter, locators, outer, frame = self.embedded_adapter()
        self.assertIs(adapter.page, frame)
        replacement = Mock(url=frame.url)
        outer.frames = [replacement]
        self.assertIs(adapter.page, replacement)

    def test_embedded_login_is_detected(self):
        adapter, locators, outer, frame = self.embedded_adapter()
        login = Mock(url="https://mgmt.zirmed.com/login")
        login.locator.return_value.is_visible.return_value = True
        outer.frames = [login]
        self.assertTrue(adapter.login_visible())
        self.assertTrue(adapter.signed_out())

    def test_frame_tooltip_moves_mouse_on_outer_page(self):
        adapter, locators, outer, frame = self.embedded_adapter()
        adapter.profile["reason_tooltip"] = "#tooltip"
        tooltip = Mock()
        tooltip.count.return_value = 1
        tooltip.inner_text.return_value = "Synthetic reason description"
        locators["#tooltip"] = tooltip
        cell = Mock()
        cell.get_attribute.return_value = None
        cell.locator.return_value.count.return_value = 0
        self.assertEqual(adapter.read_primary_reason(cell, "12345"), "Synthetic reason description")
        outer.mouse.move.assert_called_once_with(0, 0)
        frame.mouse.move.assert_not_called()

    def test_open_uses_exact_supplied_integration_url(self):
        adapter, locators = self.adapter()
        adapter.context.actions.wait_for_any.return_value = "search"
        adapter.open()
        adapter.context.actions.goto.assert_called_once_with(adapter.page, CLAIMS_URL, timeout_ms=60_000)
        self.assertEqual(adapter.context.actions.click.call_args_list,
                         [call(locators["#search-mode"]), call(locators["#pcn"])])
        adapter.context.actions.wait_visible.assert_called_once_with(locators["#search"])

    def test_signed_out_requests_reauthentication(self):
        adapter, locators = self.adapter()
        adapter.context.actions.wait_for_any.return_value = "login"
        with self.assertRaises(PortalAutomationError) as caught:
            adapter.open()
        self.assertEqual(caught.exception.recovery, RecoveryAction.REAUTH)
        adapter.context.actions.click.assert_not_called()

    def test_pcn_selection_failure_stops_without_searching(self):
        adapter, locators = self.adapter()
        adapter.context.actions.wait_for_any.return_value = "search"
        adapter.context.actions.click.side_effect = TimeoutError("PCN menu unavailable")
        with self.assertRaises(PortalAutomationError) as caught:
            adapter.search("INV-01")
        self.assertEqual(caught.exception.reason, "pcn_search_not_ready")
        self.assertEqual(caught.exception.recovery, RecoveryAction.STOP)
        adapter.context.actions.fill.assert_not_called()

    def test_pcn_label_is_confirmed_before_search_input_is_ready(self):
        adapter, locators = self.adapter()
        adapter.profile["selected_pcn"] = "#selected-pcn"
        locators["#selected-pcn"] = Mock()
        locators["#selected-pcn"].is_visible.return_value = False
        adapter.context.actions.wait_for_any.return_value = "search"
        adapter.open()
        self.assertEqual(adapter.context.actions.wait_visible.call_args_list,
                         [call(locators["#selected-pcn"]), call(locators["#search"])])

    def test_unchanged_beneficiary_label_prevents_pcn_search(self):
        adapter, locators = self.adapter()
        adapter.profile["selected_pcn"] = "#selected-pcn"
        locators["#selected-pcn"] = Mock()
        locators["#selected-pcn"].is_visible.return_value = False
        adapter.context.actions.wait_for_any.return_value = "search"
        adapter.context.actions.wait_visible.side_effect = TimeoutError("Still Beneficiary ID")
        with self.assertRaises(PortalAutomationError) as caught:
            adapter.search("TEST-PCN")
        self.assertEqual(caught.exception.reason, "pcn_search_not_ready")
        adapter.context.actions.fill.assert_not_called()

    def test_pcn_selection_cancellation_propagates(self):
        adapter, locators = self.adapter()
        adapter.context.actions.wait_for_any.return_value = "search"
        adapter.context.actions.click.side_effect = CancellationRequested("Stopped")
        with self.assertRaises(CancellationRequested):
            adapter.open()

    def test_reason_reads_native_title_without_exporting_code(self):
        adapter, locators = self.adapter()
        cell = Mock()
        cell.get_attribute.return_value = "Example rejection description"
        self.assertEqual(adapter.read_primary_reason(cell, "12345"), "Example rejection description")
        cell.hover.assert_not_called()

    def test_reason_hovers_and_clears_previous_tooltip(self):
        adapter, locators = self.adapter()
        adapter.profile["reason_tooltip"] = "#tooltip"
        tooltip = Mock()
        tooltip.count.return_value = 1
        tooltip.inner_text.return_value = "Current row description"
        locators["#tooltip"] = tooltip
        cell = Mock()
        cell.get_attribute.return_value = None
        cell.locator.return_value.count.return_value = 0
        self.assertEqual(adapter.read_primary_reason(cell, "12345"), "Current row description")
        adapter.page.mouse.move.assert_called_once_with(0, 0)
        adapter.context.actions.wait_hidden.assert_called_once_with(tooltip)
        cell.hover.assert_called_once_with(timeout=30_000)

    def test_missing_reason_description_fails(self):
        adapter, locators = self.adapter()
        cell = Mock()
        cell.get_attribute.return_value = None
        cell.locator.return_value.count.return_value = 0
        with self.assertRaises(ValueError):
            adapter.read_primary_reason(cell, "12345")

    def test_completed_empty_search_returns_no_claims(self):
        adapter, locators = self.adapter()
        locators["#empty"].is_visible.return_value = True
        with patch.object(adapter, "open") as opened:
            self.assertEqual(adapter.search("TEST-ID"), [])
        opened.assert_called_once()
        adapter.context.actions.fill.assert_called_once_with(locators["#search"], "TEST-ID")
        adapter.context.actions.press.assert_called_once_with(locators["#search"], "Enter")

    def test_stale_completion_marker_is_not_accepted_for_new_search(self):
        adapter, locators = self.adapter()
        locators["#completed"].is_visible.return_value = True
        locators["#search"].is_visible.return_value = True
        def wait(outcomes, **kwargs):
            self.assertFalse(outcomes["completed"]())
            raise TimeoutError("Previous results never changed")
        adapter.context.actions.wait_for_any.side_effect = wait
        with patch.object(adapter, "select_pcn"), patch.object(adapter, "result_snapshot", return_value="old"), \
                self.assertRaises(PortalAutomationError) as caught:
            adapter.search("TEST-ID")
        self.assertEqual(caught.exception.recovery, RecoveryAction.BACKOFF)
        self.assertEqual(caught.exception.retry_scope, RetryScope.CURRENT_ROW)
        adapter.context.actions.fill.assert_called_once_with(locators["#search"], "TEST-ID")
        adapter.context.actions.goto.assert_not_called()

    def test_consecutive_searches_reuse_page_and_wait_for_each_update(self):
        adapter, locators = self.adapter()
        locators["#search"].is_visible.return_value = True
        locators["#completed"].is_visible.return_value = True
        locators["#empty"].is_visible.return_value = True
        def wait(outcomes, **kwargs):
            self.assertFalse(outcomes["completed"]())
            self.assertFalse(outcomes["completed"]())
            self.assertTrue(outcomes["completed"]())
            return "completed"
        adapter.context.actions.wait_for_any.side_effect = wait
        with patch.object(adapter, "select_pcn") as selected, \
                patch.object(adapter, "postback_state", side_effect=[
                    dict(active=False, completed=n, failed=False) for n in (0, 1, 1, 1, 1, 2, 2, 2)]), \
                patch.object(adapter, "result_snapshot", side_effect=["old", "old", "new", "new", "new", "new", "newer", "newer"]):
            self.assertEqual(adapter.search("INV-01"), [])
            self.assertEqual(adapter.search("INV-02"), [])
        self.assertEqual(selected.call_count, 2)
        self.assertEqual(adapter.context.actions.fill.call_args_list,
                         [call(locators["#search"], "INV-01"), call(locators["#search"], "INV-02")])
        adapter.context.actions.goto.assert_not_called()

    def test_changed_grid_with_unchanged_empty_marker_is_not_completion(self):
        adapter, locators = self.adapter()
        locators["#completed"].is_visible.return_value = True
        locators["#empty"].is_visible.return_value = True
        locators["#search"].is_visible.return_value = True
        previous = ["document", ["old-table", ""], ["old-empty", "No records"]]
        interim = ["document", ["new-table", ""], ["old-empty", "No records"]]
        def wait(outcomes, **kwargs):
            self.assertFalse(outcomes["completed"]())
            self.assertFalse(outcomes["completed"]())
            raise TimeoutError("No verified completion")
        adapter.context.actions.wait_for_any.side_effect = wait
        with patch.object(adapter, "select_pcn"), \
                patch.object(adapter, "result_snapshot", side_effect=[previous, interim, interim]), \
                self.assertRaises(PortalAutomationError) as caught:
            adapter.search("TEST-ID")
        self.assertEqual(caught.exception.reason, "unrecognized_results")
        self.assertEqual(adapter.context.page.remove_listener.call_count, 3)

    def test_new_empty_marker_without_search_completion_is_not_not_found(self):
        adapter, locators = self.adapter()
        locators["#completed"].is_visible.return_value = True
        locators["#empty"].is_visible.return_value = True
        locators["#search"].is_visible.return_value = True
        previous = ["document", ["old-table", ""], ["old-empty", "No records"]]
        interim = ["document", ["new-table", ""], ["new-empty", "No records"]]
        def wait(outcomes, **kwargs):
            self.assertFalse(outcomes["completed"]())
            self.assertFalse(outcomes["completed"]())
            raise TimeoutError("No verified search completion")
        adapter.context.actions.wait_for_any.side_effect = wait
        with patch.object(adapter, "select_pcn"), \
                patch.object(adapter, "result_snapshot", side_effect=[previous, interim, interim]), \
                self.assertRaises(PortalAutomationError) as caught:
            adapter.search("TEST-ID")
        self.assertEqual(caught.exception.reason, "unrecognized_results")

    def test_failed_asp_net_postback_is_retryable_instead_of_not_found(self):
        adapter, locators = self.adapter()
        locators["#completed"].is_visible.return_value = True
        locators["#empty"].is_visible.return_value = True
        locators["#search"].is_visible.return_value = True
        def wait(outcomes, **kwargs):
            self.assertFalse(outcomes["completed"]())
            self.assertTrue(outcomes["request_failed"]())
            return "request_failed"
        adapter.context.actions.wait_for_any.side_effect = wait
        with patch.object(adapter, "select_pcn"), \
                patch.object(adapter, "postback_state", side_effect=[
                    dict(active=False, completed=0, failed=False),
                    dict(active=False, completed=1, failed=True)]), \
                self.assertRaises(PortalAutomationError) as caught:
            adapter.search("TEST-ID")
        self.assertEqual(caught.exception.retry_scope, RetryScope.CURRENT_ROW)
        self.assertIn("Search request failed", str(caught.exception))

    def test_query_changed_while_waiting_is_not_exported(self):
        adapter, locators = self.adapter()
        locators["#empty"].is_visible.return_value = True
        adapter.context.actions.press.side_effect = lambda *_: setattr(
            locators["#search"].input_value, "return_value", "OTHER-PCN")
        with patch.object(adapter, "open"), self.assertRaises(PortalAutomationError) as caught:
            adapter.search("TEST-PCN")
        self.assertIn("A different query was entered", str(caught.exception))

    def test_search_request_lifecycle_ignores_other_frames_and_assets(self):
        top, surface = Mock(), Mock()
        relevant = Mock(frame=surface, resource_type="xhr")
        relevant.response.return_value.status = 200
        other = Mock(frame=Mock(), resource_type="fetch")
        asset = Mock(frame=surface, resource_type="image")
        with _SearchRequests(top, surface) as requests:
            requests.started(other)
            requests.started(asset)
            self.assertFalse(requests.observed)
            requests.started(relevant)
            self.assertFalse(requests.completed)
            requests.finished(relevant)
            self.assertTrue(requests.completed)
        self.assertEqual(top.on.call_count, 3)
        self.assertEqual(top.remove_listener.call_count, 3)

    def test_search_request_lifecycle_tracks_replacement_claims_frame(self):
        top, old, replacement = Mock(), Mock(), Mock()
        request = Mock(frame=replacement, resource_type="xhr")
        request.response.return_value.status = 200
        with _SearchRequests(top, old, lambda: replacement) as requests:
            requests.started(request)
            self.assertTrue(requests.observed)
            requests.finished(request)
            self.assertTrue(requests.completed)

    def test_http_and_network_failures_do_not_confirm_empty_search(self):
        for failure in ("http", "network"):
            with self.subTest(failure=failure):
                top, surface = Mock(), Mock()
                relevant = Mock(frame=surface, resource_type="fetch")
                relevant.response.return_value.status = 500
                with _SearchRequests(top, surface) as requests:
                    requests.started(relevant)
                    if failure == "http":
                        requests.finished(relevant)
                    else:
                        requests.request_failed(relevant)
                    self.assertTrue(requests.failed)
                    self.assertFalse(requests.completed)

    def test_selected_pcn_is_reused_without_opening_dropdown(self):
        adapter, locators = self.adapter()
        adapter.profile["selected_pcn"] = "#selected-pcn"
        locators["#selected-pcn"] = Mock()
        locators["#selected-pcn"].is_visible.return_value = True
        adapter.select_pcn()
        adapter.context.actions.click.assert_not_called()
        adapter.context.actions.goto.assert_not_called()

    def test_no_records_overlay_is_recognized_with_visible_grid(self):
        adapter, locators = self.adapter()
        adapter.profile["data_rows"] = "tbody > tr.rgRow"
        locators["#empty"].is_visible.return_value = True
        locators["#results"].is_visible.return_value = True
        locators["#results"].locator.return_value.count.return_value = 0
        with patch.object(adapter, "open"):
            self.assertEqual(adapter.search("EMPTY-PCN"), [])

    def test_empty_message_with_result_rows_is_not_inferred_as_not_found(self):
        adapter, locators = self.adapter()
        adapter.profile["data_rows"] = "tbody > tr.rgRow"
        locators["#empty"].is_visible.return_value = True
        locators["#results"].is_visible.return_value = True
        locators["#results"].locator.return_value.count.return_value = 1
        with patch.object(adapter, "open"), self.assertRaises(PortalAutomationError) as caught:
            adapter.search("TEST-PCN")
        self.assertEqual(caught.exception.reason, "unrecognized_results")

    def test_completion_wait_re_resolves_frame_and_uses_first_visible_match(self):
        adapter, locators = self.adapter()
        locators["#empty"].is_visible.return_value = True
        with patch.object(adapter, "open"):
            self.assertEqual(adapter.search("EMPTY-PCN"), [])
        outcomes = adapter.context.actions.wait_for_any.call_args.args[0]
        locators["#completed"].count.return_value = 2
        locators["#completed"].is_visible.return_value = True
        with patch.object(adapter, "result_snapshot", return_value="new document"), \
                patch.object(adapter, "postback_state", return_value=dict(active=False, completed=1, failed=False)):
            self.assertFalse(outcomes["completed"]())
            self.assertTrue(outcomes["completed"]())

    def test_unknown_result_shape_is_never_not_found(self):
        adapter, locators = self.adapter()
        with patch.object(adapter, "open"), self.assertRaises(PortalAutomationError) as caught:
            adapter.search("TEST-ID")
        self.assertEqual(caught.exception.reason, "unrecognized_results")

    def test_paginated_results_are_deferred(self):
        adapter, locators = self.adapter()
        locators["#results"].is_visible.return_value = True
        locators["#next"].is_visible.return_value = True
        locators["#next"].is_enabled.return_value = True
        with patch.object(adapter, "open"), self.assertRaises(PortalAutomationError) as caught:
            adapter.search("TEST-ID")
        self.assertEqual(caught.exception.retry_scope, RetryScope.NEXT_RUN)
        self.assertEqual(caught.exception.reason, "paginated_results")

    def test_shipped_empty_next_page_selector_still_checks_pagination(self):
        adapter, locators = self.adapter()
        adapter.profile["next_page"] = ""
        locators["#results"].is_visible.return_value = True
        locators["#results"].evaluate.return_value.update(
            pager_texts=["Page 1 of 2, items 1 to 10 of 15"], pager_present=True, row_count=10)
        with patch.object(adapter, "open"), patch.object(adapter, "read_table") as read, \
                self.assertRaises(PortalAutomationError) as caught:
            adapter.search("TEST-PCN")
        self.assertEqual(caught.exception.reason, "paginated_results")
        self.assertEqual(caught.exception.retry_scope, RetryScope.NEXT_RUN)
        read.assert_not_called()

    def test_recognized_pager_without_completeness_evidence_is_deferred(self):
        adapter, locators = self.adapter()
        adapter.profile["next_page"] = ""
        locators["#results"].is_visible.return_value = True
        locators["#results"].evaluate.return_value.update(pager_texts=["Choose a page"], pager_present=True)
        with patch.object(adapter, "open"), patch.object(adapter, "read_table") as read, \
                self.assertRaises(PortalAutomationError) as caught:
            adapter.search("TEST-PCN")
        self.assertEqual(caught.exception.reason, "pagination_unverified")
        self.assertEqual(caught.exception.retry_scope, RetryScope.NEXT_RUN)
        read.assert_not_called()

    def test_disabled_configured_next_control_does_not_reject_complete_grid(self):
        adapter, locators = self.adapter()
        locators["#results"].is_visible.return_value = True
        locators["#next"].is_visible.return_value = True
        locators["#next"].is_enabled.return_value = False
        locators["#results"].evaluate.return_value.update(
            pager_texts=["Page 1 of 1, items 1 to 3 of 3"], pager_present=True, row_count=3)
        with patch.object(adapter, "open"), patch.object(adapter, "read_table", return_value=["claims"]):
            self.assertEqual(adapter.search("TEST-PCN"), ["claims"])

    def test_cancellation_propagates_without_becoming_portal_error(self):
        adapter, locators = self.adapter()
        adapter.context.actions.fill.side_effect = CancellationRequested("Stopped")
        with patch.object(adapter, "open"), self.assertRaises(CancellationRequested):
            adapter.search("TEST-ID")


if __name__ == "__main__":
    unittest.main()
