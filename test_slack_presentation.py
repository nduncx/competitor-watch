"""Message rendering contracts; no live network or competitor checks."""
import datetime as dt
import json
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo
import competitor_watch as w
from slack_presentation import slack_payload

NOW = dt.datetime(2026, 9, 28, 19, 30, tzinfo=ZoneInfo('Europe/Gibraltar'))


def result(status, passed=True):
    return {'status': status, 'notice': 'We will resume our deliveries at 19:45pm.',
            'venues': [{'name': 'Essaouira', 'status': status, 'basket_probe': {'passed': passed}}]}


class Presentation(unittest.TestCase):
    def render(self, message):
        return slack_payload(message['subject'], message['body'])

    def test_confirmed_states_and_reopening_keep_the_distinction(self):
        examples = [
            (w.closed_notification(NOW, result('closed')), 'CLOSED — ORDERS STOPPED'),
            (w.ongoing_notification(NOW, result('closed')), 'STILL CLOSED'),
            (w.delays_notification(NOW, result('delays')), 'OPEN — LONG DELAYS'),
            (w.ongoing_notification(NOW, result('delays')), 'STILL OPEN — LONG DELAYS'),
            (w.reopen_notification(NOW, None, result('delays')), 'REOPENED — LONG DELAYS'),
            (w.reopen_notification(NOW, None, result('open')), 'REOPENED — NO DELAY WARNING'),
            (w.delays_cleared_notification(NOW, None, result('open')), 'OPEN — NO DELAY WARNING'),
        ]
        for message, label in examples:
            with self.subTest(label=label):
                payload=self.render(message)
                self.assertIn(label, payload['blocks'][0]['text']['text'])
                self.assertIn(label, payload['text'])  # mobile/accessibility fallback
                self.assertIn('Checked 19:30 · Gibraltar time', payload['text'])
                self.assertNotIn('Venues checked', payload['text'])
                self.assertLess(len(payload['text']), 310)

    def test_estimated_time_is_not_reported_as_actual_reopening(self):
        p=self.render(w.closed_notification(NOW,result('closed')))
        self.assertIn('Estimated reopening: 19:45, according to their notice.',p['text'])
        self.assertNotIn('REOPENED',p['text'])
        r=result('closed');r['notice']='Sorry, not taking orders.'
        self.assertNotIn('Estimated',self.render(w.closed_notification(NOW,r))['text'])

    def test_unconfirmed_improvement_never_claims_current_open_or_closed(self):
        m=w.unconfirmed_update_notification(NOW,{'status':'closed','last_confirmed_at':NOW.isoformat()},result('delays'),1)
        p=self.render(m)
        self.assertIn('CHANGE AWAITING CONFIRMATION',p['text'])
        self.assertIn('Last confirmed: CLOSED at 19:30',p['text'])
        self.assertNotIn('REOPENED',p['text'])
        self.assertNotIn('STILL CLOSED',p['text'])

    def test_no_basket_evidence_does_not_gain_open_claim_in_redesign(self):
        for m in [w.delays_notification(NOW,result('delays',False)),
                  w.reopen_notification(NOW,None,result('open',False)),
                  w.delays_cleared_notification(NOW,None,result('open',False))]:
            p=self.render(m)
            self.assertIn('ORDERING UNCONFIRMED',p['text'])
            self.assertNotIn('🟢',p['text'])

    def test_health_and_layout_preview_cannot_be_mistaken_for_live_status(self):
        p=slack_payload('Competitor Watch: monitoring problem','At 19:30, a check failed.')
        self.assertIn('CURRENT STATUS UNKNOWN',p['text'])
        p=slack_payload('Competitor Watch: message layout preview','')
        self.assertIn('NOT live status reports',p['text'])
        for block in p['blocks']:
            if block['type']=='header': self.assertLessEqual(len(block['text']['text']),150)

    def test_unknown_historical_message_is_preserved_and_text_is_safe(self):
        p=slack_payload('Historical correction','Prior incident at 19:30. <@here> *bold*')
        self.assertEqual(p['text'],'Historical correction\n\nPrior incident at 19:30. <@here> *bold*')
        self.assertFalse(p['mrkdwn'])
        p=self.render(w.delays_notification(NOW,result('delays')))
        self.assertTrue(all(b['text']['type']=='plain_text' for b in p['blocks'] if 'text' in b))

    def test_transport_still_requires_slack_acknowledgement(self):
        with patch.object(w,'SLACK_WEBHOOK_URL','https://example.invalid/webhook'), patch.object(w.urllib.request,'urlopen') as send:
            response=send.return_value.__enter__.return_value
            response.status=200;response.read.return_value=b'ok'
            m=w.delays_notification(NOW,result('delays'))
            w.send_email(**m)
            payload=json.loads(send.call_args.args[0].data)
            self.assertIn('OPEN — LONG DELAYS',payload['text'])
            self.assertEqual(payload['blocks'][0]['type'],'header')
            response.read.return_value=b'not_ok'
            with self.assertRaises(RuntimeError):w.send_email(**m)

if __name__=='__main__':unittest.main()
