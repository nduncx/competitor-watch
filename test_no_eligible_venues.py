"""The 27 September night shutdown is not a monitor outage or proof of reopening."""
import contextlib
import copy
import datetime as dt
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import competitor_watch as m
import continuous_watch as w

AT = dt.datetime(2026, 9, 27, 23, 4, tzinfo=ZoneInfo('Europe/Gibraltar'))
END = AT.replace(hour=23, minute=30)
NO_SAMPLE = {'status': 'no_open_venues', 'detail': 'No current delivery estimates.',
             'venues': [{'name': n, 'status': 'venue_closed', 'listed_open': False,
                         'unresolved_notice': False} for n in
                        ['Essaouira', '3Bros', '4 Stagioni', '54 Dining', 'AquaTerra', 'BABA']]}


class NoEligibleVenuesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.stack = contextlib.ExitStack(); self.addCleanup(self.stack.close)
        for obj, key, value in [
                (m, 'STATE_FILE', Path(self.tmp.name)/'state.json'),
                (m, 'RECOVERY_FILE', Path(self.tmp.name)/'recovery.json'),
                (w, 'HEALTH_FILE', Path(self.tmp.name)/'health.json')]:
            self.stack.enter_context(patch.object(obj, key, value))
        self.stack.enter_context(patch.object(m, 'in_trading_hours', return_value=True))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def test_nighttime_sample_is_excluded_without_changing_availability(self):
        for previous in ['open', 'closed', 'delays']:
            m.save_state({'status': previous, 'since': 'old', 'last_confirmed_at': 'old',
                          'recovery_streak': 1, 'recovery_candidate_at': AT.isoformat()})
            with patch.object(m, 'check_platform', return_value=NO_SAMPLE):
                self.assertEqual(m.run_check(), 4)
            s=m.load_state()
            self.assertEqual(s['status'], previous)
            self.assertEqual(s['last_confirmed_at'], 'old')
            self.assertEqual(s['recovery_streak'], 0)
            self.assertTrue(s['last_sample_check']['exclusions_confirmed'])
            self.assertFalse(s.get('notification_receipts'))

    def test_missing_unknown_unresolved_or_listed_open_sample_is_still_failure(self):
        variants = [[], NO_SAMPLE['venues'][:2]]
        for change in [{'status':'unknown'}, {'unresolved_notice':True}, {'listed_open':True},
                       {'status':'closed'}, {'status':'platform_closed'}]:
            v=copy.deepcopy(NO_SAMPLE['venues']); v[0].update(change); variants.append(v)
        for venues in variants:
            result={**NO_SAMPLE,'venues':venues}
            self.assertFalse(m.only_excluded_venues(result))
            with patch.object(m,'check_platform',return_value=result):
                self.assertEqual(m.run_check(),2)

    def test_existing_closure_queue_is_retained_and_retried_during_sample_gap(self):
        m.save_state({'status':'closed','pending_notifications':[
            {'id':'closure','subject':'Closed','body':'Earlier confirmed closure'}]})
        with patch.object(m,'check_platform',return_value=NO_SAMPLE), \
             patch.object(m,'send_email',side_effect=RuntimeError('offline')):
            self.assertEqual(m.run_check(),3)
        self.assertEqual(len(m.load_state()['pending_notifications']),1)
        with patch.object(m,'check_platform',return_value=NO_SAMPLE),patch.object(m,'send_email') as send:
            self.assertEqual(m.run_check(),4)
        send.assert_called_once()
        self.assertEqual(m.load_state()['pending_notifications'],[])

    def test_exclusion_dry_run_does_not_save_state_or_send(self):
        with patch.object(m,'check_platform',return_value=NO_SAMPLE),patch.object(m,'send_email') as send:
            self.assertEqual(m.run_check(dry_run=True),4)
        self.assertFalse(m.STATE_FILE.exists());send.assert_not_called()

    def test_worker_sample_gap_is_successful_but_never_marked_available(self):
        m.save_state({'status':'closed'})
        with patch.object(w.subprocess,'run',return_value=subprocess.CompletedProcess([],4)), \
             patch.object(m,'send_email') as send:
            self.assertEqual(w.attempt(END),0)
        h=json.loads(w.HEALTH_FILE.read_text())
        self.assertEqual(h['status'],'no_eligible_venues')
        self.assertEqual(h['consecutive_failures'],0)
        self.assertNotIn('last_success_at',h)
        self.assertEqual(m.load_state()['status'],'closed')
        self.assertIn('no suitable stores',send.call_args.args[0])
        self.assertIn('does not confirm',send.call_args.args[1])

    def test_repeated_no_sample_notices_do_not_pile_up_during_slack_failure(self):
        with patch.object(w.subprocess,'run',return_value=subprocess.CompletedProcess([],4)), \
             patch.object(m,'send_email',side_effect=RuntimeError('offline')):
            self.assertEqual(w.attempt(END),3)
            self.assertEqual(w.attempt(END),3)
        self.assertEqual(len(m.load_state()['pending_notifications']),1)
        self.assertFalse(m.load_state().get('notification_receipts'))
        with patch.object(w.subprocess,'run',return_value=subprocess.CompletedProcess([],4)), \
             patch.object(m,'send_email') as send:
            self.assertEqual(w.attempt(END),0)
        send.assert_called_once()
        self.assertEqual(len(m.load_state()['notification_receipts']),1)

    def test_health_cycle_distinguishes_expected_exclusions_and_real_outage(self):
        state={'status':'delays'};health={}
        for i,code in enumerate([0,4,4,2,2,4,0,0]):
            at=AT+dt.timedelta(minutes=5*i)
            health=w.update_health(state,health,code,at,at,END)
        subjects=[n['subject'] for n in state['pending_notifications']]
        self.assertEqual(subjects,[
            'Competitor Watch: no suitable stores available to test',
            'Competitor Watch: monitoring problem',
            'Competitor Watch: no suitable stores available to test',
            'Competitor Watch: suitable stores available again'])
        self.assertEqual(state['status'],'delays')
        self.assertEqual(health['status'],'healthy')


if __name__=='__main__': unittest.main()
