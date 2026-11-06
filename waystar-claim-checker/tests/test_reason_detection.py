"""Detect denial descriptions from real DOM capabilities, independent of payer."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
for folder in (ROOT / "src", ROOT.parent / "Kantime-engine-main" / "src"):
    sys.path.insert(0, str(folder))

from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout
from kantime_engine.browser import BrowserActions
from kantime_engine.cancellation import CancellationToken, CancellationRequested
from waystar_claim_checker.application import create_application
from waystar_claim_checker.pages import ClaimsPage


HTML = r"""<style>td {padding:15px} [role=tooltip] {position:fixed;top:400px}</style>
<input id="search"><table>
<tr><td id="popup1" title="Wrong tooltip description">
  <a href="javascript:openReason(1)">U5235</a></td></tr>
<tr><td id="hover1">U1000</td></tr>
<tr><td id="popup2"><a onclick="openReason(2)" href="#">U5235</a></td></tr>
<tr><td id="hover2">U2000</td></tr>
<tr><td id="native" title="Native description for this claim">U3000</td></tr>
<tr><td id="broken" title="Never substitute this tooltip">
  <a href="javascript:openReason(3)">U9999</a></td></tr>
<tr><td id="codeonly" title="U7777">U7777</td></tr>
<tr><td id="ambiguous"><a onclick="openReason(1)">U5235</a>
  <a onclick="openReason(2)">U5235</a></td></tr>
</table>
<div role="tooltip" hidden></div>
<div class="RadWindow" hidden><button title="Close">X</button><iframe></iframe></div>
<script>
window.events=[];
// The actual portal advertises reason.aspx in its href or onclick attribute.
for (const a of document.querySelectorAll('a')) {
  const target=(a.getAttribute('href')||'')+' '+(a.getAttribute('onclick')||'');
  const id=target.match(/openReason\((\d+)\)/)[1];
  a.setAttribute('href', `javascript:openReason('reason.aspx?reasonid=${id}')`);
  a.removeAttribute('onclick');
}
function openReason(url) {
  if (!document.querySelector('.RadWindow').hidden) throw Error('Popup still open');
  events.push('open:'+new URL(url,location.href).searchParams.get('reasonid'));
  document.querySelector('.RadWindow').hidden=false;
  document.querySelector('iframe').src=url;
}
document.querySelector('[title=Close]').onclick=()=>{
  document.querySelector('.RadWindow').hidden=true;events.push('close');
};
for (const id of ['hover1','hover2']) {
  const cell=document.getElementById(id), tip=document.querySelector('[role=tooltip]');
  cell.onmouseenter=()=>{tip.textContent='Current description for '+id;tip.hidden=false;events.push(id)};
  cell.onmouseleave=()=>{tip.hidden=true};
}
</script>"""


class ReasonDetectionTests(unittest.TestCase):
    def test_all_payors_detect_generic_click_cells_and_hover_links(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            try:
                page = browser.new_page()
                def route(request):
                    if urlparse(request.request.url).path.endswith('/reason.aspx'):
                        reason_id = parse_qs(urlparse(request.request.url).query)['reasonid'][0]
                        description = 'Second popup description' if reason_id == '2' else 'Verified popup description'
                        body = f'<div>Reason Code: U5235</div><textarea>{description}</textarea>'
                    else:
                        body = HTML
                    request.fulfill(body=body, content_type='text/html')
                page.route('**/*', route)
                variant = create_application().spec.automation.variants[-1]
                for payor in ('HH Medicare', 'Palmetto GBA', 'NGS'):
                    for target in ('anchor', 'cell', 'hover-link'):
                        with self.subTest(payor=payor, target=target):
                            page.goto('https://analytics.ecorpnet.com/member/adaptive-reason-test')
                            page.evaluate('''target => {
                                const cell=document.querySelector('#popup1');
                                cell.removeAttribute('title');
                                if (target === 'anchor' || target === 'hover-link') {
                                    const a=cell.querySelector('a');
                                    a.href='#';
                                    a.onclick=e=>{e.preventDefault();openReason('reason.aspx?reasonid=1')};
                                    if (target === 'hover-link') {
                                        const tip=document.querySelector('[role=tooltip]');
                                        cell.onmouseenter=()=>{tip.textContent='Verified hover description';tip.hidden=false};
                                        cell.onmouseleave=()=>{tip.hidden=true};
                                    }
                                } else {
                                    cell.textContent='U5235';
                                    cell.style.cursor='pointer';
                                    cell.addEventListener('click',()=>openReason('reason.aspx?reasonid=1'));
                                }
                            }''', target)
                            token = CancellationToken()
                            adapter = ClaimsPage(SimpleNamespace(page=page, cancellation=token,
                                actions=BrowserActions(token, default_timeout_ms=1000), variant=variant), {})
                            adapter.payor = payor  # Must never control the interaction.
                            expected = ('Verified hover description' if target == 'hover-link'
                                        else 'Verified popup description')
                            self.assertEqual(adapter.read_primary_reason(page.locator('#popup1'), 'U5235'), expected)
                            self.assertEqual(page.evaluate('events'), [] if target == 'hover-link' else ['open:1', 'close'])
                            self.assertFalse(page.locator('.RadWindow').is_visible())
                # Reuse a popup iframe for the same code with a different reason
                # description. The old iframe document remains until navigation.
                page.goto('https://analytics.ecorpnet.com/member/adaptive-reason-test')
                page.evaluate('''() => {
                    const cell=document.querySelector('#popup1'), a=cell.querySelector('a');
                    cell.removeAttribute('title'); a.href='#'; let clicks=0;
                    a.onclick=e=>{
                        e.preventDefault(); clicks++;
                        if (clicks===1) openReason('reason.aspx?reasonid=1');
                        else {
                            document.querySelector('.RadWindow').hidden=false;
                            setTimeout(()=>document.querySelector('iframe').src='reason.aspx?reasonid=2',350);
                        }
                    };
                }''')
                self.assertEqual(adapter.read_primary_reason(page.locator('#popup1'), 'U5235'),
                                 'Verified popup description')
                self.assertEqual(adapter.read_primary_reason(page.locator('#popup1'), 'U5235'),
                                 'Second popup description')
                self.assertFalse(page.locator('.RadWindow').is_visible())
                adapter.payor = 'HH Medicare'
                self.assertEqual(adapter.read_primary_reason(page.locator('#native'), 'U3000'),
                                 'Native description for this claim')
            finally:
                browser.close()

    def test_all_portal_variants_handle_mixed_click_hover_and_titles_in_either_order(self):
        self.run_detection()

    def test_wrong_popup_code_is_closed_and_never_replaced_with_hover(self):
        self.run_detection(failure="popup")

    def test_code_only_and_ambiguous_links_are_not_exported_as_descriptions(self):
        self.run_detection(failure="unverified")

    def test_cancellation_prevents_opening_a_reason(self):
        self.run_detection(failure="cancel")

    def run_detection(self, failure=None):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            try:
                page = browser.new_page()

                def route(request):
                    address = urlparse(request.request.url)
                    if address.path.endswith("/reason.aspx"):
                        reason_id = parse_qs(address.query)["reasonid"][0]
                        code = "WRONG" if reason_id == "3" else "U5235"
                        body = (f'<div>Reason Code: {code}</div>'
                                f'<textarea id="ctl00_txtReasonDesc">Popup description {reason_id}</textarea>')
                    else:
                        body = HTML
                    request.fulfill(body=body, content_type="text/html")

                page.route("**/*", route)
                for variant in create_application().spec.automation.variants:
                    for first in ("click", "hover"):
                        with self.subTest(payer=variant.payer, first=first, failure=failure):
                            host = ("analytics.ecorpnet.com" if variant.payer == "HH - Medicare"
                                    else "claimseditor.ecorpnet.com")
                            page.goto(f"https://{host}/member/reason-detection-test")
                            token = CancellationToken()
                            adapter = ClaimsPage(SimpleNamespace(page=page, cancellation=token,
                                actions=BrowserActions(token, default_timeout_ms=1000), variant=variant), {})
                            if failure == "cancel":
                                token.cancel()
                                with self.assertRaises(CancellationRequested):
                                    adapter.read_primary_reason(page.locator("#popup1"), "U5235")
                                self.assertEqual(page.evaluate("events"), [])
                            elif failure == "popup":
                                with self.assertRaises(TimeoutError):
                                    adapter.read_primary_reason(page.locator("#broken"), "U9999")
                                self.assertFalse(page.locator(".RadWindow").is_visible())
                                self.assertEqual(page.evaluate("events"), ["open:3", "close"])
                            elif failure == "unverified":
                                with self.assertRaises(BrowserTimeout):
                                    adapter.read_primary_reason(page.locator("#codeonly"), "U7777")
                                with self.assertRaisesRegex(ValueError, "multiple"):
                                    adapter.read_primary_reason(page.locator("#ambiguous"), "U5235")
                                self.assertEqual(page.evaluate("events"), [])
                            else:
                                order = (["popup1", "hover1", "popup2", "hover2"] if first == "click"
                                         else ["hover1", "hover2", "popup1", "popup2"])
                                expected = {"popup1": "Popup description 1", "popup2": "Popup description 2",
                                            "hover1": "Current description for hover1",
                                            "hover2": "Current description for hover2"}
                                for cell_id in order:
                                    code = "U5235" if cell_id.startswith("popup") else "U1000"
                                    self.assertEqual(adapter.read_primary_reason(page.locator("#"+cell_id), code),
                                                     expected[cell_id])
                                    self.assertFalse(page.locator(".RadWindow").is_visible())
                                self.assertEqual(adapter.read_primary_reason(page.locator("#native"), "U3000"),
                                                 "Native description for this claim")
                                self.assertEqual(page.evaluate("events.filter(e=>e==='close').length"), 2)
            finally:
                browser.close()
