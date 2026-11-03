"""Exercise complete invoices, alphanumeric reason codes and paging in real Edge."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
for folder in (ROOT / 'src', ROOT.parent / 'Kantime-engine-main' / 'src'):
    sys.path.insert(0, str(folder))

from playwright.sync_api import sync_playwright
from kantime_engine.browser import BrowserActions
from kantime_engine import PortalAutomationError
from kantime_engine.cancellation import CancellationToken
from waystar_claim_checker.application import create_application
from waystar_claim_checker.pages import ClaimsPage


class InvoiceFlowTests(unittest.TestCase):
    def test_every_reason_code_is_clicked_and_closed_before_next_page_or_invoice(self):
        self.run_invoice_flow()

    def test_failed_next_page_does_not_return_part_of_an_invoice_as_complete(self):
        self.run_invoice_flow(fail_next=True)

    def test_missing_popup_description_closes_X_and_does_not_skip_to_the_next_claim(self):
        self.run_invoice_flow(fail_reason=True)

    def run_invoice_flow(self, fail_next=False, fail_reason=False):
        profile = json.loads((ROOT / 'tests/portal-profile.fixture.json').read_text())
        profile.update(search_input='#search', selected_pcn='#selected', results_table='#results',
                       ready='#results:has(tr.rgRow), #empty:visible', no_results='#empty:visible',
                       next_page='', busy='')
        headers = profile['column_headers']
        invoices = {'INV-A': [('U5235', '501'), ('1002', '502'), ('U5235', '501')],
                    'INV-B': [('2001', '601')]}
        descriptions = {'501': ('U5235', 'First claim primary description.'),
                        '502': ('1002', 'Second claim needs documentation.'),
                        '601': ('2001', 'Next invoice primary description.')}
        html = """<input id="search"><span id="selected">PCN</span>
        <div class="RadGrid"><table id="results"></table><div class="rgPager"></div></div>
        <div id="empty">No records</div>
        <div class="RadWindow" hidden><button class="rwCloseButton" title="Close">X</button><iframe></iframe></div>
        <script>
        const headers=HEADERS, invoices=INVOICES;
        window.events=[];let invoice='', pageIndex=0, opened='';
        function assertClosed() {
          if (!document.querySelector('.RadWindow').hidden) throw Error('Popup is still open');
        }
        async function render() {
          const response=await fetch('/result?invoice='+invoice+'&page='+pageIndex);
          await response.text();
          const claims=invoices[invoice], start=pageIndex*2, selected=claims.slice(start,start+2);
          const rows=selected.map(([code,id],index) => {
            const values={'Claim Status':'Paid','Patient Full Name':'SYNTHETIC TEST',
              'Beneficiary ID':'TEST','PCN':invoice,'Stmt Start':'05/01/26','Stmt End':'05/31/26',
              'Total Charges':'100.00','Provider Reimb':'80.00','DCN':invoice+'-'+(start+index)};
            return '<tr class="rgRow">'+headers.map(h=>'<td>'+(h==='Primary Reason'
              ? `<a href="javascript:ShowRadWindow('reason.aspx?reasonid=${id}', 'rwnPopup');">${code}</a>`
              : (values[h]||''))+'</td>').join('')+'</tr>';
          }).join('');
          document.querySelector('#results').innerHTML='<tbody>'+rows+'</tbody>';
          document.querySelector('#empty').hidden=true;
          const count=Math.ceil(claims.length/2);
          document.querySelector('.rgPager').innerHTML=`<span class="rgInfoPart">Page ${pageIndex+1} of ${count}, items ${start+1} to ${start+selected.length} of ${claims.length}</span>
            <button class="rgPageNext" ${pageIndex+1===count?'disabled':''} onclick="nextPage()">Next</button>`;
        }
        document.querySelector('#search').onkeydown=async e=>{
          if(e.key!=='Enter')return;assertClosed();invoice=e.target.value;pageIndex=0;
          events.push('search:'+invoice);await render();
        };
        async function nextPage(){assertClosed();events.push('next:'+invoice);pageIndex++;await render();}
        document.addEventListener('click',e=>{
          if(e.target.matches('td a')) {
            if(!e.isTrusted)throw Error('Reason needs a pointer click');
            events.push('code:'+e.target.innerText);
          }
        },true);
        function ShowRadWindow(url,name){
          assertClosed();opened=new URL(url,location.href).searchParams.get('reasonid');
          events.push('open:'+opened);document.querySelector('.RadWindow').hidden=false;
          document.querySelector('iframe').src=url;
        }
        document.querySelector('.rwCloseButton').onclick=e=>{
          if(!e.isTrusted)throw Error('X needs a pointer click');
          setTimeout(()=>{document.querySelector('.RadWindow').hidden=true;events.push('X:'+opened);},100);
        };
        </script>""".replace('HEADERS', json.dumps(headers)).replace('INVOICES', json.dumps(invoices))
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='msedge', headless=True)
            try:
                page = browser.new_page()
                def route(request):
                    address = urlparse(request.request.url)
                    if address.path.endswith('/reason.aspx'):
                        reason_id = parse_qs(address.query)['reasonid'][0]
                        code, description = descriptions[reason_id]
                        if fail_reason and reason_id == '502':
                            description = ''
                        body = f'<div>Reason Code: {code}</div><textarea id="ctl00_txtAnsiReasonDesc">Wrong ANSI description</textarea>'
                        body += f'<textarea id="ctl00_txtReasonDesc">{description}</textarea><div>Ansi Reason Code: 99</div>'
                    elif address.path == '/result':
                        if fail_next and parse_qs(address.query).get('page') == ['1']:
                            request.fulfill(status=500, body='Failed test response')
                            return
                        body = '{}'
                    else:
                        body = html
                    request.fulfill(body=body, content_type='text/html')
                page.route('**/*', route)
                page.goto('https://analytics.ecorpnet.com/member/invoice-test')
                token = CancellationToken()
                adapter = ClaimsPage(SimpleNamespace(page=page, cancellation=token,
                    actions=BrowserActions(token, default_timeout_ms=3000),
                    variant=create_application().resolve('Any agency','HH - Medicare')), profile)
                if fail_next or fail_reason:
                    with self.assertRaises(PortalAutomationError):
                        adapter.search('INV-A')
                    events = page.evaluate('events')
                    self.assertEqual(events.count('open:501'), 1)
                    self.assertNotIn('search:INV-B', events)
                    self.assertEqual(events.count('X:502'), 1)
                    if fail_reason:
                        self.assertNotIn('next:INV-A', events)
                    return
                first = adapter.search('INV-A')
                self.assertEqual([c.invoice for c in first], ['INV-A']*3)
                self.assertEqual([c.primary_reason for c in first],
                    [descriptions['501'][1], descriptions['502'][1], descriptions['501'][1]])
                second = adapter.search('INV-B')
                self.assertEqual([c.invoice for c in second], ['INV-B'])
                self.assertEqual(second[0].primary_reason, descriptions['601'][1])
                self.assertEqual(page.evaluate('events'), [
                    'search:INV-A','code:U5235','open:501','X:501',
                    'code:1002','open:502','X:502','next:INV-A',
                    'code:U5235','open:501','X:501','search:INV-B',
                    'code:2001','open:601','X:601'])
            finally:
                browser.close()


if __name__ == '__main__':
    unittest.main()
