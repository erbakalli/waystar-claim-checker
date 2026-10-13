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
from waystar_claim_checker.pages import ClaimsPage, CLAIMS_URL


class PortalTests(unittest.TestCase):
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

    def test_split_header_empty_table_needs_explicit_no_results(self):
        adapter, table, _ = self.split_header_table()
        table.locator.return_value.all.return_value = []
        with self.assertRaises(ValueError):
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
        page = Mock(url="https://claimseditor.ecorpnet.com/member/SelectSummary.aspx", frames=[])
        page.locator.side_effect = lambda selector: locators[selector]
        context = SimpleNamespace(page=page, actions=Mock(), cancellation=Mock())
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
                patch.object(adapter, "result_snapshot", side_effect=["old", "old", "new", "new", "new", "new", "newer", "newer"]):
            self.assertEqual(adapter.search("INV-01"), [])
            self.assertEqual(adapter.search("INV-02"), [])
        self.assertEqual(selected.call_count, 2)
        self.assertEqual(adapter.context.actions.fill.call_args_list,
                         [call(locators["#search"], "INV-01"), call(locators["#search"], "INV-02")])
        adapter.context.actions.goto.assert_not_called()

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
        with patch.object(adapter, "result_snapshot", return_value="new document"):
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

    def test_cancellation_propagates_without_becoming_portal_error(self):
        adapter, locators = self.adapter()
        adapter.context.actions.fill.side_effect = CancellationRequested("Stopped")
        with patch.object(adapter, "open"), self.assertRaises(CancellationRequested):
            adapter.search("TEST-ID")


if __name__ == "__main__":
    unittest.main()
