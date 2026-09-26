import datetime as dt
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import continuous_watch as worker

TZ = ZoneInfo('Europe/Gibraltar')
START = dt.datetime(2026, 9, 26, 21, 0, tzinfo=TZ)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, path in [('STATE_FILE', 'state.json')]:
            p = patch.object(worker.monitor, name, Path(self.tmp.name)/path)
            p.start(); self.addCleanup(p.stop)
        p = patch.object(worker, 'HEALTH_FILE', Path(self.tmp.name)/'health.json')
        p.start(); self.addCleanup(p.stop)
        p = patch.object(worker.monitor, 'TRADING_HOURS', '09:00-23:30')
        p.start(); self.addCleanup(p.stop)

    def test_failure_retry_recovery_keeps_competitor_state_and_pending_order(self):
        state = {'status':'closed','pending_notifications':[{'id':'closure','subject':'Closed',
                 'body':'Observed closure','attach_screenshot':False}]}
        health = {}
        for i, code in enumerate([2,2,0,0]):
            at = START + dt.timedelta(minutes=5*i)
            health = worker.update_health(state,health,code,at,at,START+dt.timedelta(hours=1))
        self.assertEqual(state['status'],'closed')
        self.assertEqual([n['subject'] for n in state['pending_notifications']],
                         ['Closed','Competitor Watch: monitoring problem','Competitor Watch: monitoring restored'])
        worker.monitor.save_state(state)
        with patch.object(worker.monitor,'send_email',side_effect=RuntimeError('offline')):
            self.assertFalse(worker.monitor.flush_notifications(state))
        self.assertEqual(len(worker.monitor.load_state()['pending_notifications']),3)
        self.assertEqual(state.get('notification_receipts',[]),[])
        with patch.object(worker.monitor,'send_email') as send:
            self.assertTrue(worker.monitor.flush_notifications(worker.monitor.load_state()))
        self.assertEqual(send.call_count,3)
        self.assertEqual(len(worker.monitor.load_state()['notification_receipts']),3)
        self.assertEqual(worker.monitor.load_state()['pending_notifications'],[])

    def test_success_does_not_send_health_messages_when_already_healthy(self):
        state={}
        h=worker.update_health(state,{},0,START,START,START+dt.timedelta(hours=1))
        self.assertEqual(h['last_success_at'],START.isoformat())
        self.assertNotIn('pending_notifications',state)

    def test_timeout_is_saved_and_reported_with_no_competitor_claim(self):
        with patch.object(worker.subprocess,'run',side_effect=subprocess.TimeoutExpired('check',240)), \
             patch.object(worker.monitor,'send_email') as send:
            self.assertEqual(worker.attempt(START+dt.timedelta(hours=1)),124)
        h=json.loads(worker.HEALTH_FILE.read_text())
        self.assertEqual(h['last_exit_code'],124)
        self.assertIn('time limit',send.call_args.args[1])
        self.assertIn('not evidence',send.call_args.args[1])

    def test_slack_failure_retains_monitor_message_for_retry(self):
        with patch.object(worker.subprocess,'run',return_value=subprocess.CompletedProcess([],2)), \
             patch.object(worker.monitor,'send_email',side_effect=RuntimeError('offline')):
            self.assertEqual(worker.attempt(START+dt.timedelta(hours=1)),3)
        saved=worker.monitor.load_state()
        self.assertEqual(len(saved['pending_notifications']),1)
        self.assertEqual(saved.get('notification_receipts',[]),[])
        health=json.loads(worker.HEALTH_FILE.read_text())
        self.assertFalse(health['notification_delivery_confirmed'])
        self.assertEqual(health['status'],'delivery_pending')

    def test_active_job_retries_on_five_minute_starts_and_stops_at_window_end(self):
        base=START.replace(hour=23,minute=18)
        elapsed=[0]; starts=[]; checkpoints=[]; codes=iter([2,0,0])
        def check(end):
            starts.append(elapsed[0]);elapsed[0]+=60;return next(codes)
        def sleep(delay): elapsed[0]+=delay
        result=worker.run_session(check,lambda:checkpoints.append(elapsed[0]),
            lambda:base+dt.timedelta(seconds=elapsed[0]),lambda:elapsed[0],sleep)
        self.assertEqual(starts,[0,300,600])
        self.assertEqual(len(checkpoints),3)
        self.assertEqual(elapsed[0],720)
        self.assertEqual(result,0)

    def test_session_is_bounded_and_does_not_have_a_fixed_calendar_date(self):
        base=START.replace(year=2027,month=1,day=9,hour=9)
        elapsed=[0];starts=[]
        def check(end): starts.append(elapsed[0]);return 0
        worker.run_session(check,lambda:None,lambda:base+dt.timedelta(seconds=elapsed[0]),
            lambda:elapsed[0],lambda n:elapsed.__setitem__(0,elapsed[0]+n))
        self.assertEqual(len(starts),60)
        self.assertEqual(elapsed[0],worker.MAX_SESSION_SECONDS)

    def test_outside_hours_does_not_check_or_invent_recovery(self):
        with patch.object(worker,'attempt') as check:
            result=worker.run_session(check,lambda:None,lambda:START.replace(hour=1))
        check.assert_not_called();self.assertEqual(result,0)

    def test_overnight_window_end_is_next_day(self):
        with patch.object(worker.monitor,'TRADING_HOURS','18:00-01:00'):
            self.assertEqual(worker.window_end(START),dt.datetime(2026,9,27,1,tzinfo=TZ))

    def test_checkpoint_failure_stops_instead_of_discarding_unpersisted_messages(self):
        with self.assertRaises(RuntimeError):
            worker.run_session(lambda end:0,lambda:(_ for _ in ()).throw(RuntimeError('git failed')),
                               lambda:START)


if __name__=='__main__': unittest.main()
