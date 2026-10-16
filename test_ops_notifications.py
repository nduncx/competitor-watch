"""Exercise filtering and independent outboxes without sending live messages."""
import contextlib
import copy
import datetime as dt
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import competitor_watch as monitor
import continuous_watch as worker
import ops_notifications as ops
from slack_presentation import slack_payload


class OpsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(monitor, 'STATE_FILE', Path(self.tmp.name)/'state.json'))
        self.stack.enter_context(patch.object(worker, 'HEALTH_FILE', Path(self.tmp.name)/'health.json'))
        self.stack.enter_context(patch.object(monitor, 'OPS_SLACK_WEBHOOK_URL', 'https://example.invalid/ops'))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def item(self, subject='Hungry Monkey has STOPPED taking orders (20:18)', body='Check at 20:18.'):
        return {'id':subject,'subject':subject,'body':body,'attach_screenshot':False}

    def test_exact_same_card_payload_for_each_confirmed_transition(self):
        cases = [
            self.item(),
            self.item('Hungry Monkey is warning of long delays (20:18)', 'OPEN for deliveries'),
            self.item('Hungry Monkey is taking orders again (20:18)', 'OPEN for deliveries; still showing a long-delays warning'),
            self.item('Hungry Monkey is taking orders again (20:18)', 'OPEN for deliveries'),
            self.item('Hungry Monkey: long-delays warning cleared (20:18)', 'OPEN for deliveries'),
        ]
        for item in cases:
            with self.subTest(subject=item['subject'], body=item['body']):
                self.assertEqual(ops.status_payload(item, monitor.TARGET_NAME, monitor.TIMEZONE),
                    slack_payload(item['subject'],item['body'],monitor.TARGET_NAME,monitor.TIMEZONE))
                self.assertIsNotNone(ops.status_payload(item, monitor.TARGET_NAME, monitor.TIMEZONE))

    def test_repeated_unconfirmed_technical_tests_and_corrections_are_excluded(self):
        subjects = [
            'Hungry Monkey is STILL not taking orders (20:18)',
            'Hungry Monkey is STILL warning of long delays (20:18)',
            'Hungry Monkey: 20:18 check unconfirmed - last confirmed not taking orders at 20:13',
            'Hungry Monkey: ordering pages read as taking orders again (20:18)',
            'Competitor Watch: monitoring problem',
            'Competitor Watch: monitoring restored',
            'Competitor Watch: no suitable stores available to test',
            'Competitor Watch: suitable stores available again',
            'Competitor Watch: Ops alerts delivery problem',
            'Competitor Watch: Ops alerts delivery restored',
            'Competitor watch: test notification',
            'Historical correction',
        ]
        for subject in subjects:
            self.assertIsNone(ops.status_payload(self.item(subject),monitor.TARGET_NAME,monitor.TIMEZONE),subject)
        old=self.item();old['historical']=True
        self.assertIsNone(ops.status_payload(old,monitor.TARGET_NAME,monitor.TIMEZONE))

    def test_ops_failure_does_not_repeat_acknowledged_primary_or_block_new_primary(self):
        state={'status':'closed','pending_notifications':[self.item()]}
        with patch.object(monitor,'send_email') as primary, \
             patch.object(ops,'post_payload',side_effect=RuntimeError('private transport detail')):
            self.assertTrue(monitor.flush_notifications(state))
            self.assertEqual(primary.call_count,1)
            self.assertEqual(state['pending_notifications'],[])
            self.assertEqual(len(state['pending_ops_notifications']),1)
            state['pending_notifications'].append(self.item(
                'Hungry Monkey is taking orders again (20:23)','OPEN for deliveries'))
            self.assertTrue(monitor.flush_notifications(state))
            self.assertEqual(primary.call_count,2)
        self.assertEqual(len(state['pending_ops_notifications']),2)
        reloaded=monitor.load_state()
        with patch.object(monitor,'send_email') as primary, patch.object(ops,'post_payload') as post:
            self.assertTrue(monitor.flush_notifications(reloaded))
            primary.assert_not_called()
            self.assertEqual(post.call_count,2)
            self.assertEqual([x['id'] for x in reloaded['ops_notification_receipts']],
                             [x['id'] for x in state['pending_ops_notifications']])
            self.assertTrue(monitor.flush_notifications(reloaded))
            self.assertEqual(post.call_count,2)
        self.assertEqual(reloaded['pending_ops_notifications'],[])
        self.assertEqual(reloaded['status'],'closed')

    def test_primary_failure_keeps_primary_and_retries_existing_ops_backlog(self):
        state={'pending_notifications':[self.item()]}
        ops.enqueue(state,self.item('Hungry Monkey is taking orders again (20:10)','OPEN for deliveries'),
                    monitor.OPS_SLACK_WEBHOOK_URL,monitor.TARGET_NAME,monitor.TIMEZONE)
        with patch.object(monitor,'send_email',side_effect=RuntimeError('offline')), \
             patch.object(ops,'post_payload') as post:
            self.assertFalse(monitor.flush_notifications(state))
            self.assertEqual(post.call_count,1)
        self.assertEqual(len(state['pending_notifications']),1)
        self.assertEqual(state['pending_ops_notifications'],[])

    def test_receipts_and_pending_ids_prevent_duplicate_enqueues(self):
        state={}; item=self.item()
        for unused in range(2):
            ops.enqueue(state,item,monitor.OPS_SLACK_WEBHOOK_URL,monitor.TARGET_NAME,monitor.TIMEZONE)
        self.assertEqual(len(state['pending_ops_notifications']),1)
        with patch.object(ops,'post_payload'):
            self.assertTrue(ops.flush(state,monitor.OPS_SLACK_WEBHOOK_URL,
                                     monitor.save_state,monitor.log,monitor.TIMEZONE))
        ops.enqueue(state,item,monitor.OPS_SLACK_WEBHOOK_URL,monitor.TARGET_NAME,monitor.TIMEZONE)
        self.assertEqual(state['pending_ops_notifications'],[])

    def test_missing_ops_setting_leaves_existing_behavior_unchanged_and_preserves_backlog(self):
        state={'pending_notifications':[self.item()]}
        with patch.object(monitor,'OPS_SLACK_WEBHOOK_URL',''), \
             patch.object(monitor,'send_email') as primary, patch.object(ops,'post_payload') as post:
            self.assertTrue(monitor.flush_notifications(state))
            primary.assert_called_once();post.assert_not_called()
        self.assertNotIn('pending_ops_notifications',state)
        state['pending_ops_notifications']=[{'id':'old','payload':{'text':'old'}}]
        with patch.object(ops,'post_payload') as post:
            self.assertFalse(ops.flush(state,'',monitor.save_state,monitor.log,monitor.TIMEZONE))
            post.assert_not_called()
        self.assertEqual(state['pending_ops_notifications'][0]['id'],'old')

    def test_worker_reports_ops_problem_separately_and_only_once_until_restored(self):
        state={'status':'closed','pending_ops_notifications':[{'id':'pending'}]}; health={}
        now=dt.datetime(2026,10,16,20,0,tzinfo=ZoneInfo('Europe/Gibraltar'))
        worker.update_ops_health(state,health,now)
        worker.update_ops_health(state,health,now)
        self.assertEqual(len(state['pending_notifications']),1)
        self.assertIn('Ops alerts delivery problem',state['pending_notifications'][0]['subject'])
        self.assertFalse(health['ops_notification_delivery_confirmed'])
        state['pending_ops_notifications']=[]
        worker.update_ops_health(state,health,now+dt.timedelta(minutes=2))
        self.assertEqual(len(state['pending_notifications']),2)
        self.assertIn('restored',state['pending_notifications'][1]['subject'])
        self.assertTrue(health['ops_notification_delivery_confirmed'])
        self.assertEqual(state['status'],'closed')

    def test_transport_requires_slack_ack_and_sends_the_identical_block_kit(self):
        payload=ops.status_payload(self.item(),monitor.TARGET_NAME,monitor.TIMEZONE)
        class Response:
            status=200
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self):return b'ok'
        with patch.object(ops.urllib.request,'urlopen',return_value=Response()) as urlopen:
            ops.post_payload(monitor.OPS_SLACK_WEBHOOK_URL,payload)
        self.assertEqual(json.loads(urlopen.call_args.args[0].data),payload)
        self.assertEqual(urlopen.call_args.kwargs['timeout'],15)
        class Rejected(Response):
            def read(self):return b'not_ok'
        with patch.object(ops.urllib.request,'urlopen',return_value=Rejected()):
            with self.assertRaises(RuntimeError):ops.post_payload(monitor.OPS_SLACK_WEBHOOK_URL,payload)


if __name__=='__main__':unittest.main()
