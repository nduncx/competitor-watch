"""Real local Chromium fixtures for the user's basket-only definition; no external orders."""
import contextlib
import io
import json
import time
import unittest
from playwright.sync_api import sync_playwright
import competitor_watch as cw

DELAY = 'Hungry Monkey orders may incur long delays.'
PREORDER = 'We are currently closed but you can still pre-order.'
CLOSED = 'Sorry, we are not taking orders right now.'


def basket_fixture(items=None, outcome='added', dirty=False):
    items = items or [{'name': 'Simple side'}]
    return '''<!doctype html><html><body>
    <h2>Basket</h2><p>%s</p><button>Collection or Delivery</button>
    <div id="basket">%s</div><div id="menu"></div><div id="overlay"></div>
    <script>
    const items=%s; const outcome=%s; window.actions=[];
    const basket=document.getElementById('basket'), overlay=document.getElementById('overlay');
    for(let i=0;i<items.length;i++){
      const h=document.createElement('h3');h.className='section-item__name';h.innerText=items[i].name;
      h.onclick=()=>showItem(i);document.getElementById('menu').append(h);
    }
    function showItem(i){
      window.actions.push('item:'+items[i].name);overlay.replaceChildren();
      const d=document.createElement('div');d.setAttribute('role','dialog');
      const h=document.createElement('h2');h.innerText=items[i].name;d.append(h);
      const p=document.createElement('p');p.innerText=items[i].description||'';d.append(p);
      const close=document.createElement('button');close.innerText='Close';close.onclick=()=>overlay.replaceChildren();d.append(close);
      if(items[i].required){const p=document.createElement('p');p.innerText='Choose drink. Select 1 option';d.append(p);}
      const add=document.createElement('button');add.innerText='Add to order• £7.20';add.disabled=!!items[i].required;
      add.onclick=()=>{
        window.actions.push('add:'+items[i].name);overlay.replaceChildren();
        if(outcome!=='no_effect'){
          basket.replaceChildren();const h=document.createElement('h4');h.innerText=items[i].name;basket.append(h);
        }
        if(outcome==='closed'||outcome==='pause'||outcome==='preorder'){
          const p=document.createElement('p');p.innerText=outcome==='closed'?%s:outcome==='pause'?'We will resume our deliveries at 23:15pm.':%s;basket.append(p);
        }
        if(outcome==='unknown'){
          const d=document.createElement('div');d.setAttribute('role','dialog');d.innerHTML='<p>Confirm your account change</p><button>OK</button>';overlay.append(d);
        }
      };d.append(add);overlay.append(d);
    }
    </script></body></html>''' % (DELAY, '<h4>Simple side</h4>' if dirty else '<p>There are no items in your basket</p>',
                                  json.dumps(items), json.dumps(outcome), json.dumps(CLOSED), json.dumps(PREORDER))


class BasketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw=sync_playwright().start();cls.browser=cls.pw.chromium.launch()
    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.pw.stop()
    def probe(self,html,deadline=None,status='delays'):
        page=self.browser.new_page();self.addCleanup(page.close);page.set_content(html)
        with contextlib.redirect_stdout(io.StringIO()):
            result=cw.probe_ordering(page,{'status':status,'text':DELAY,'notice':DELAY,'notice_transcript':[]},deadline)
        return result,page.evaluate('window.actions')
    def test_item_added_is_sufficient_without_checkout_or_login(self):
        r,a=self.probe(basket_fixture())
        self.assertEqual(r['status'],'delays');self.assertTrue(r['basket_probe']['passed'])
        self.assertEqual(a,['item:Simple side','add:Simple side'])
    def test_required_choices_are_skipped_without_filling_them(self):
        r,a=self.probe(basket_fixture([{'name':'Meal','required':True},{'name':'Simple side'}]))
        self.assertTrue(r['basket_probe']['passed']);self.assertEqual(r['basket_probe']['item'],'Simple side')
        self.assertNotIn('add:Meal',a)
    def test_preorder_product_description_is_skipped(self):
        r,a=self.probe(basket_fixture([{'name':'Special','description':'Minimum 24-hour pre-order required.'},{'name':'Simple side'}]))
        self.assertTrue(r['basket_probe']['passed']);self.assertNotIn('add:Special',a)
    def test_preorder_product_name_is_never_opened(self):
        r,a=self.probe(basket_fixture([{'name':'Pre-order cake'},{'name':'Simple side'}]))
        self.assertTrue(r['basket_probe']['passed']);self.assertNotIn('item:Pre-order cake',a)
    def test_disabled_add_is_unknown_not_closed(self):
        r,a=self.probe(basket_fixture([{'name':'Meal','required':True}]))
        self.assertEqual(r['status'],'unknown');self.assertFalse(r['basket_probe']['passed'])
    def test_unchanged_basket_cannot_pass(self):
        r,a=self.probe(basket_fixture(outcome='no_effect'))
        self.assertEqual(r['status'],'unknown');self.assertFalse(r['basket_probe']['passed'])
    def test_preexisting_item_cannot_fake_success(self):
        r,a=self.probe(basket_fixture(dirty=True))
        self.assertEqual(r['status'],'unknown');self.assertEqual(a,[])
    def test_refusal_revealed_after_add_wins(self):
        r,a=self.probe(basket_fixture(outcome='closed'))
        self.assertEqual(r['status'],'closed');self.assertFalse(r['basket_probe']['passed'])
    def test_platform_pause_revealed_after_add_wins(self):
        r,a=self.probe(basket_fixture(outcome='pause'))
        self.assertEqual(r['status'],'platform_closed')
    def test_preorder_revealed_after_add_is_excluded(self):
        r,a=self.probe(basket_fixture(outcome='preorder'))
        self.assertEqual(r['status'],'venue_closed');self.assertFalse(r['basket_probe']['passed'])
    def test_unfamiliar_dialog_after_add_blocks_pass(self):
        r,a=self.probe(basket_fixture(outcome='unknown'))
        self.assertEqual(r['status'],'unknown');self.assertFalse(r['basket_probe']['passed'])
    def test_expired_budget_cannot_pass_or_add(self):
        r,a=self.probe(basket_fixture(),deadline=time.monotonic()-1)
        self.assertEqual(r['status'],'unknown');self.assertEqual(a,[])
    def test_preorder_venue_never_gets_basket_test(self):
        r,a=self.probe(basket_fixture(),status='venue_closed')
        self.assertEqual(r['status'],'venue_closed');self.assertEqual(a,[])
    def test_live_preorder_delay_order_button_is_excluded(self):
        page=self.browser.new_page();self.addCleanup(page.close)
        page.set_content('<h2>Basket</h2><p>'+PREORDER+'</p><p>'+DELAY+'</p><button>Collection or Delivery</button><button>Checkout</button>')
        with contextlib.redirect_stdout(io.StringIO()):r=cw.read_venue_page(page)
        self.assertEqual(r['status'],'venue_closed');self.assertIn(PREORDER,r['notice'])
        self.assertEqual(cw.classify_venue(PREORDER+'\n'+CLOSED),'closed')
    def test_product_preorder_mentions_are_not_venue_closure(self):
        self.assertEqual(cw.classify_venue('Pre-order cake\nCollection or Delivery'),'open')
    def test_preorder_samples_do_not_establish_closure_or_recovery(self):
        def v(s):return {'status':s,'listed_open':True}
        self.assertEqual(cw.summarise([v('closed'),v('closed'),v('venue_closed')])['status'],'closed')
        self.assertEqual(cw.summarise([v('closed'),v('venue_closed'),v('venue_closed')])['status'],'unknown')
        self.assertEqual(cw.summarise([v('venue_closed')]*3)['status'],'unknown')
        self.assertFalse(cw.enough_samples([v('delays'),v('venue_closed'),v('unknown')]))
    def test_preorder_directory_cards_are_skipped_and_anchor_kept(self):
        cards=[{'name':n,'text':t,'looks_open':True} for n,t in [('Preorder','Pre-order for tomorrow'),('Essaouira','Delivery:45 mins'),('B','Delivery:45 mins'),('C','Delivery:45 mins'),('D','Delivery:45 mins')]]
        chosen=cw.candidate_venues(cards)
        self.assertEqual(chosen[0]['name'],'Essaouira');self.assertNotIn('Preorder',[c['name'] for c in chosen])
        self.assertGreater(len(chosen),3)

class BasketMessageTests(unittest.TestCase):
    def test_successful_basket_messages_use_user_definition(self):
        now=cw.dt.datetime(2026,9,25,22,45,tzinfo=cw.ZoneInfo(cw.TIMEZONE))
        r={'status':'delays','notice':DELAY,'venues':[{'name':'Essaouira','status':'delays','basket_probe':{'passed':True}}]}
        messages=[cw.delays_notification(now,r),cw.ongoing_notification(now,r),cw.reopen_notification(now,None,r)]
        for m in messages:
            self.assertIn('OPEN for deliveries',m['body'])
            self.assertIn('successfully added',m['body'])
            self.assertNotIn('delivery availability is not verified',m['body'].lower())
            self.assertNotIn('checkout',m['body'].lower())
        pending=cw.unconfirmed_update_notification(now,{'status':'closed','since':now.isoformat()},r,1)
        self.assertIn('unconfirmed',pending['subject'])
        self.assertIn('basket test passed',pending['body'])
    def test_failed_basket_cannot_use_open_message_basis(self):
        self.assertFalse(cw.basket_passed({'venues':[{'basket_probe':{'passed':False}}]}))

if __name__=='__main__':unittest.main()
