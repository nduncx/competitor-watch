"""Local browser fixtures: these simulate the observed UI, not the live service."""
import json
import unittest

from playwright.sync_api import sync_playwright
import competitor_watch as cw


DELAY = "Hungry Monkey orders may incur long delays."
CLOSED = "Sorry, we’re not taking orders right now"
PAUSE = "Everyone is a Hungry Monkey today! We will resume our deliveries at 21:20pm."


def fixture(notices, basket="Collection or Delivery", final_basket=None, stubborn=False):
    return """<!doctype html><html><body>
      <h2>Basket</h2><div id="basket"></div><div id="overlay"></div>
      <script>
      const notices = %s;
      let step=0; window.pressed=[];
      const basket=document.getElementById('basket');
      function renderBasket(value) {
        basket.replaceChildren();
        const child=document.createElement(value==='Collection or Delivery'?'button':'p');
        child.innerText=value;basket.append(child);
      }
      renderBasket(%s);
      function show() {
        const overlay=document.getElementById('overlay');
        overlay.replaceChildren();
        if(step>=notices.length) { if(%s!==null) renderBasket(%s); return; }
        const outer=document.createElement('div'); outer.setAttribute('role','alertdialog');
        const dialog=document.createElement('md-dialog'); dialog.className='appMessage';
        dialog.setAttribute('role','dialog');
        const content=document.createElement('div'); content.className='preo-modal';
        const text=document.createElement('p'); text.innerText=notices[step][0];
        const footer=document.createElement('div'); footer.className='modal-footer';
        const button=document.createElement('button'); button.innerText=notices[step][1];
        button.onclick=()=>{window.pressed.push(button.innerText); if(!%s){step++;show();}};
        footer.append(button);content.append(text,footer);dialog.append(content);
        outer.append(dialog);overlay.append(outer);
      }
      show();
      </script></body></html>""" % tuple(map(json.dumps, [notices,basket,final_basket,final_basket,stubborn]))


class DialogRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright=sync_playwright().start()
        cls.browser=cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def inspect(self, html):
        page=self.browser.new_page()
        self.addCleanup(page.close)
        page.set_content(html)
        result=cw.read_venue_page(page)
        return result,page.evaluate('window.pressed')

    def test_actual_closed_then_pause_retains_second_notice_and_closed_basket(self):
        result,pressed=self.inspect(fixture([[CLOSED,'GOT IT'],[PAUSE,'OK']],basket=CLOSED))
        self.assertEqual(result['status'],'platform_closed')
        self.assertIn('21:20',result['notice'])
        self.assertEqual(pressed,['GOT IT','OK'])

    def test_reported_delay_then_closed_then_pause(self):
        result,pressed=self.inspect(fixture([[DELAY,'OK'],[CLOSED,'GOT IT'],[PAUSE,'OK']]))
        self.assertEqual(result['status'],'platform_closed')
        self.assertIn('resume our deliveries',result['notice'])
        self.assertEqual(pressed,['OK','GOT IT','OK'])

    def test_dismissed_refusal_never_becomes_open_from_clean_final_basket(self):
        result,_=self.inspect(fixture([[CLOSED,'GOT IT']],basket=CLOSED,final_basket='Collection or Delivery'))
        self.assertEqual(result['status'],'closed')
        self.assertIn('not taking orders',result['notice'])

    def test_unfamiliar_dialog_never_clicks_continue_or_counts_as_open(self):
        result,pressed=self.inspect(fixture([['Please confirm your age','Continue']]))
        self.assertEqual(result['status'],'unknown')
        self.assertEqual(pressed,[])

    def test_stubborn_dialog_cannot_count_as_open(self):
        result,pressed=self.inspect(fixture([[DELAY,'OK']],stubborn=True))
        self.assertEqual(result['status'],'unknown')
        self.assertLessEqual(len(pressed),4)

    def test_dialog_cap_leaves_reading_unknown(self):
        notices=[[DELAY+str(n),'OK'] for n in range(6)]
        result,pressed=self.inspect(fixture(notices))
        self.assertEqual(result['status'],'unknown')
        self.assertLessEqual(len(pressed),4)

    def test_delay_without_final_order_control_is_not_recovery(self):
        result,_=self.inspect(fixture([[DELAY,'OK']],basket='No ordering control present'))
        self.assertEqual(result['status'],'unknown')

    def test_complete_delay_page_keeps_notice_after_dismissal(self):
        result,pressed=self.inspect(fixture([[DELAY,'OK']]))
        self.assertEqual(result['status'],'delays')
        self.assertIn('long delays',result['notice'])
        self.assertEqual(pressed,['OK'])

    def test_plain_ordering_words_are_not_an_enabled_order_control(self):
        result,_=self.inspect(fixture([],basket='Please choose Collection or Delivery later'))
        self.assertEqual(result['status'],'unknown')

    def test_unknown_ok_dialog_does_not_get_acknowledged(self):
        result,pressed=self.inspect(fixture([['Confirm your account change','OK']]))
        self.assertEqual(result['status'],'unknown')
        self.assertEqual(pressed,[])

    def test_order_control_removed_after_notice_cannot_prove_recovery(self):
        result,_=self.inspect(fixture([[DELAY,'OK']],final_basket='Ordering is unavailable'))
        self.assertEqual(result['status'],'unknown')

    def test_disabled_order_control_is_not_positive_evidence(self):
        html=fixture([])+"<script>document.querySelector('#basket button').disabled=true;</script>"
        result,_=self.inspect(html)
        self.assertEqual(result['status'],'unknown')

    def test_overlay_covered_order_control_is_not_positive_evidence(self):
        html=fixture([])+"<div style='position:fixed;inset:0;background:white;z-index:999'>Loading</div>"
        result,_=self.inspect(html)
        self.assertEqual(result['status'],'unknown')

    def test_semantic_dialog_retains_sibling_footer(self):
        html=fixture([[DELAY,'OK'],[CLOSED,'GOT IT'],[PAUSE,'OK']])
        html=html.replace('content.append(text,footer);dialog.append(content);',
                          'content.append(text);dialog.append(content,footer);')
        result,pressed=self.inspect(html)
        self.assertEqual(result['status'],'platform_closed')
        self.assertEqual(pressed,['OK','GOT IT','OK'])

    def test_cookie_dialog_is_rejected_before_reading_status_queue(self):
        html=fixture([[DELAY,'OK'],[CLOSED,'GOT IT'],[PAUSE,'OK']])+"""
        <div role="dialog" id="cookies" style="position:fixed;inset:0;z-index:100;background:white">
          We use cookies to improve your online experience. See our cookie policy.
          <button onclick="window.pressed.push('Accept all')">Accept all</button>
          <button onclick="window.pressed.push('Reject non-essential');document.getElementById('cookies').remove()">Reject non-essential</button>
          <button>Manage cookies</button>
        </div>"""
        result,pressed=self.inspect(html)
        self.assertEqual(result['status'],'platform_closed')
        self.assertEqual(pressed,['Reject non-essential','OK','GOT IT','OK'])

    def test_cookie_dismissal_failure_cannot_recover(self):
        html=fixture([[DELAY,'OK']])+"""
        <div role="dialog" style="position:fixed;inset:0;z-index:100;background:white">
          We use cookies to improve your online experience. See our cookie policy.
          <button onclick="window.pressed.push('Reject non-essential')">Reject non-essential</button>
        </div>"""
        result,_=self.inspect(html)
        self.assertEqual(result['status'],'unknown')

    def test_cookie_notice_can_arrive_after_delay_notice(self):
        cookies='We use cookies to improve your online experience. See our cookie policy.'
        result,pressed=self.inspect(fixture([[DELAY,'OK'],[cookies,'Reject non-essential'],[PAUSE,'OK']]))
        self.assertEqual(result['status'],'platform_closed')
        self.assertEqual(pressed,['OK','Reject non-essential','OK'])

    def test_failed_cookie_dismissal_does_not_erase_visible_refusal(self):
        html=fixture([[CLOSED,'GOT IT']])+"""
        <div role="dialog" style="position:fixed;inset:0;z-index:100;background:white">
          We use cookies to improve your online experience. See our cookie policy.
          <button>Reject non-essential</button>
        </div>"""
        result,_=self.inspect(html)
        self.assertEqual(result['status'],'closed')

    def test_cookie_with_no_reject_option_is_not_accepted(self):
        result,pressed=self.inspect(fixture([['We use cookies. See our cookie policy.','Accept all']]))
        self.assertEqual(result['status'],'unknown')
        self.assertEqual(pressed,[])


if __name__=='__main__':
    unittest.main()
