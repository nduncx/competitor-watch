"""Observed weather notice through the real dialog and anonymous basket flow."""
import contextlib
import io
import unittest

from playwright.sync_api import sync_playwright

import competitor_watch as cw
import ops_notifications as ops
from slack_presentation import slack_payload
from test_basket_regressions import basket_fixture, CLOSED, DELAY, PREORDER
from test_dialog_regressions import fixture, PAUSE


WEATHER = ("Due to the adverse weather conditions we are currently experiencing across "
           "Gibraltar delays may incur. Thank you for your patience!")


class WeatherDelayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def page(self, html):
        page = self.browser.new_page()
        self.addCleanup(page.close)
        page.set_content(html)
        return page

    def read(self, page):
        with contextlib.redirect_stdout(io.StringIO()):
            return cw.read_venue_page(page)

    def probe(self, outcome='added', items=None):
        html = basket_fixture(items=items, outcome=outcome).replace(DELAY, '')
        html += '''<div role="alertdialog" id="weather">
          <p>''' + WEATHER + '''</p>
          <button onclick="window.actions.push('weather:OK');this.parentElement.remove()">OK</button>
        </div>
        <div role="dialog" id="cookies" style="position:fixed;inset:0;z-index:100;background:white">
          We use cookies to improve your online experience. See our cookie policy.
          <button>Accept all</button>
          <button onclick="window.actions.push('cookies:Reject non-essential');this.parentElement.remove()">Reject non-essential</button>
          <button>Manage cookies</button>
        </div>'''
        page = self.page(html)
        before = self.read(page)
        with contextlib.redirect_stdout(io.StringIO()):
            result = cw.probe_ordering(page, before)
        return before, result, page.evaluate('window.actions')

    def test_weather_with_cookie_overlay_reaches_and_verifies_fresh_basket(self):
        before, result, actions = self.probe()
        self.assertEqual(before['status'], 'delays')
        self.assertFalse(before['unresolved_notice'])
        self.assertTrue(result['basket_probe']['passed'])
        self.assertEqual(result['status'], 'delays')
        self.assertIn(WEATHER, result['notice'])
        self.assertEqual(actions, ['cookies:Reject non-essential', 'weather:OK',
                                   'item:Simple side', 'add:Simple side'])

    def test_weather_does_not_hide_later_closure_or_platform_pause(self):
        for notices, expected in [([[WEATHER, 'OK'], [CLOSED, 'GOT IT']], 'closed'),
                                  ([[WEATHER, 'OK'], [CLOSED, 'GOT IT'], [PAUSE, 'OK']], 'platform_closed')]:
            with self.subTest(expected=expected):
                page = self.page(fixture(notices, basket=CLOSED))
                result = self.read(page)
                self.assertEqual(result['status'], expected)
                self.assertIn(WEATHER, result['notice'])
                self.assertEqual(page.evaluate('window.pressed'), [n[1] for n in notices])

    def test_weather_never_overrides_a_live_preorder_venue(self):
        page = self.page(fixture([[WEATHER, 'OK']], basket=PREORDER))
        result = self.read(page)
        with contextlib.redirect_stdout(io.StringIO()):
            result = cw.probe_ordering(page, result)
        self.assertEqual(result['status'], 'venue_closed')
        self.assertFalse(result['basket_probe']['passed'])

    def test_refusal_or_preorder_revealed_after_weather_basket_add_still_wins(self):
        for outcome, expected in [('closed', 'closed'), ('pause', 'platform_closed'),
                                  ('preorder', 'venue_closed')]:
            with self.subTest(outcome=outcome):
                before, result, actions = self.probe(outcome=outcome)
                self.assertEqual(result['status'], expected)
                self.assertFalse(result['basket_probe']['passed'])
                self.assertIn('add:Simple side', actions)

    def test_weather_cannot_turn_required_choices_into_success(self):
        before, result, actions = self.probe(items=[{'name': 'Meal', 'required': True}])
        self.assertEqual(result['status'], 'unknown')
        self.assertFalse(result['basket_probe']['passed'])
        self.assertNotIn('add:Meal', actions)

    def test_weather_acknowledgement_does_not_accept_forms_or_other_actions(self):
        for html in [fixture([[WEATHER, 'Continue']]),
                     fixture([[WEATHER, 'OK']]) + "<script>document.querySelector('md-dialog').append(document.createElement('input'));</script>",
                     fixture([['Adverse weather conditions: confirm your account change', 'OK']])]:
            with self.subTest(html=html):
                page = self.page(html)
                result = self.read(page)
                self.assertEqual(result['status'], 'unknown')
                self.assertEqual(page.evaluate('window.pressed'), [])

    def test_stubborn_weather_notice_cannot_pass_or_claim_ordering(self):
        page = self.page(fixture([[WEATHER, 'OK']], stubborn=True))
        result = self.read(page)
        self.assertEqual(result['status'], 'unknown')
        self.assertTrue(result['unresolved_notice'])

    def test_confirmed_weather_warning_uses_existing_busy_card_and_ops_filter(self):
        before, result, actions = self.probe()
        now = cw.dt.datetime(2026, 10, 18, 9, 0, tzinfo=cw.ZoneInfo(cw.TIMEZONE))
        combined = {'status': result['status'], 'notice': result['notice'],
                    'venues': [dict(result, name='Fixture venue')]}
        message = cw.delays_notification(now, combined)
        message['id'] = now.isoformat() + ':delays'
        self.assertIn(WEATHER, message['body'])
        payload = slack_payload(message['subject'], message['body'])
        self.assertIn('OPEN — LONG DELAYS', payload['text'])
        self.assertEqual(ops.status_payload(message, cw.TARGET_NAME, cw.TIMEZONE), payload)
        reminder = cw.ongoing_notification(now, combined)
        self.assertIsNone(ops.status_payload(reminder, cw.TARGET_NAME, cw.TIMEZONE))
        combined['venues'][0]['basket_probe']['passed'] = False
        unverified = cw.delays_notification(now, combined)
        self.assertIsNone(ops.status_payload(unverified, cw.TARGET_NAME, cw.TIMEZONE))


if __name__ == '__main__':
    unittest.main()
