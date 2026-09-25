import importlib.util,tempfile,contextlib,io
from pathlib import Path
from unittest.mock import patch,Mock
spec=importlib.util.spec_from_file_location('watch',Path(__file__).with_name('competitor_watch.py'))
w=importlib.util.module_from_spec(spec);spec.loader.exec_module(w)
def venues(states,listed=False):
 return [{'name':n,'status':s,'listed_open':listed,'notice':"Sorry, we’re not taking orders right now" if s=='closed' else '', 'url':'https://example.com'} for n,s in zip(['Gela Ti Amo',"Jury`s",'Era'],states)]
closed=w.summarise(venues(['closed']*3),directory_has_open_venues=False)
assert closed['status']=='closed'
for states in [['closed','closed','unknown'],['closed','open','open'],['unknown']*3]:
 assert w.summarise(venues(states),directory_has_open_venues=False)['status']!='closed'
assert w.summarise(venues(['closed']*3),directory_has_open_venues=True)['status']=='unknown'
assert w.summarise(venues(['closed']*3,True),directory_has_open_venues=True)['status']=='closed'
assert w.summarise(venues(['platform_closed','unknown','unknown']),directory_has_open_venues=False)['status']=='closed'
print('PASS: observed three closed stores with no delivery estimates -> closed; ambiguous and individual closures stay guarded')
page=Mock();page.evaluate.return_value='Basket. Sorry, we’re not taking orders right now'
page.locator.return_value.all_inner_texts.return_value=['Everyone is a Hungry Monkey today! We will resume our deliveries at 19:25pm.']
page.url='https://example.com';assert w.read_venue_page(page)['status']=='platform_closed'
print('PASS: visible resume-deliveries popup takes precedence over generic basket closure')
with tempfile.TemporaryDirectory() as tmp:
 w.STATE_FILE=Path(tmp)/'state.json';alerts=[]
 w.save_state({'status':'delays','since':'2026-09-25T19:07:00+02:00','alerted':True})
 with patch.object(w,'in_trading_hours',return_value=True),patch.object(w,'send_email',side_effect=lambda subject,*a,**k: alerts.append(subject)),contextlib.redirect_stdout(io.StringIO()):
  for r in [closed,closed,w.summarise(venues(['open']*3,True),directory_has_open_venues=True)]:
   with patch.object(w,'check_platform',return_value=r):assert w.run_check()==0
 assert len(alerts)==2,alerts
 assert 'STOPPED' in alerts[0] and 'again' in alerts[1]
 print('PASS: actual delays -> closed -> closed -> open sequence sends exactly one closure and one reopening')
 with patch.object(w,'in_trading_hours',return_value=False),patch.object(w,'check_platform') as browser,patch.object(w,'send_email') as send,contextlib.redirect_stdout(io.StringIO()):
  assert w.run_check()==0;browser.assert_not_called();send.assert_not_called()
 print('PASS: no night-time checks or closure alerts')
