"""Behavioural acceptance checks; all site reads and Slack sends are mocked."""
import contextlib
import datetime as dt
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("watch", Path(__file__).with_name("competitor_watch.py"))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
RealDateTime = dt.datetime


class Containment(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        w.STATE_FILE = Path(self.tmp.name) / "state.json"
        w.RECOVERY_FILE = Path(self.tmp.name) / "no-recovery.json"
        w.ALERT_ON_DELAYS = w.ALERT_ON_REOPEN = True
        w.RECOVERY_CONFIRMATIONS = 2
        self.time = RealDateTime(2026, 9, 25, 19, tzinfo=w.ZoneInfo(w.TIMEZONE))
        self.sent = []
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(w, "in_trading_hours", return_value=True))
        self.stack.enter_context(patch.object(w, "send_email", side_effect=lambda *a, **k: self.sent.append(a)))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def check(self, status, minutes=5, dry_run=False):
        self.time += dt.timedelta(minutes=minutes)
        now = self.time
        class Clock(RealDateTime):
            @classmethod
            def now(cls, tz=None):
                return now.astimezone(tz) if tz else now.replace(tzinfo=None)
        result = {"status": status, "notice": "long delays" if status == "delays" else "",
                  "venues": [{"name": "Essaouira", "status": status}], "detail": "mock acceptance test"}
        site = patch.object(w, "check_platform", side_effect=status) if isinstance(status, Exception) else patch.object(w, "check_platform", return_value=result)
        with site, patch.object(w.dt, "datetime", Clock):
            return w.run_check(dry_run=dry_run)

    def test_false_reopening_in_real_incident_is_held(self):
        for status in ["closed", "closed", "delays", "closed"]:
            self.assertEqual(self.check(status), 0)
        self.assertEqual(w.load_state()["status"], "closed")
        self.assertFalse(any("taking orders again" in s[0] for s in self.sent))
        candidate = self.sent[2][0].lower()
        self.assertIn("unconfirmed", candidate)
        self.assertNotIn("is still", candidate)
        self.assertIn("last confirmed", candidate)

    def test_false_all_clear_is_held(self):
        for status in ["delays", "open", "closed"]:
            self.check(status)
        self.assertFalse(any("warning cleared" in s[0] for s in self.sent))
        self.assertIn("STOPPED", self.sent[-1][0])

    def test_full_cycle_repeats_incidents_and_recovers_once(self):
        for status in ["open", "delays", "delays", "closed", "closed", "delays", "delays", "delays", "open", "open", "open"]:
            self.assertEqual(self.check(status), 0)
        subjects = [s[0] for s in self.sent]
        self.assertEqual(sum("taking orders again" in s for s in subjects), 1)
        self.assertEqual(sum("warning cleared" in s for s in subjects), 1)
        self.assertEqual(sum("unconfirmed" in s.lower() for s in subjects), 2)
        self.assertEqual(sum("STILL warning" in s for s in subjects), 2)
        self.assertEqual(sum("STILL not taking orders" in s for s in subjects), 1)
        reopening = next(s for s in self.sent if "taking orders again" in s[0])
        self.assertIn("still showing a long-delays warning", reopening[1])
        self.assertEqual(w.load_state()["status"], "open")
        self.assertEqual(len(w.load_state()["notification_receipts"]), len(self.sent))
        before = len(self.sent)
        self.check("open")
        self.assertEqual(len(self.sent), before)
        self.check("closed")
        self.assertIn("STOPPED", self.sent[-1][0])

    def test_failed_checks_break_recovery_confirmation(self):
        for failure in ["unknown", "no_open_venues", RuntimeError("site unavailable")]:
            with self.subTest(failure=str(failure)):
                w.STATE_FILE.unlink(missing_ok=True)
                self.sent.clear()
                self.check("closed")
                self.check("delays")
                self.assertEqual(self.check(failure), 2)
                self.check("delays")
                self.assertEqual(w.load_state()["status"], "closed")
                self.assertFalse(any("taking orders again" in s[0] for s in self.sent))
                self.check("delays")
                self.assertEqual(sum("taking orders again" in s[0] for s in self.sent), 1)

    def test_gap_and_outside_hours_break_confirmation(self):
        self.check("closed")
        self.check("delays")
        self.check("delays", minutes=13)
        self.assertEqual(w.load_state()["status"], "closed")
        with patch.object(w, "in_trading_hours", return_value=False):
            self.check("open")
        self.check("delays")
        self.assertEqual(w.load_state()["status"], "closed")
        self.check("delays")
        self.assertEqual(sum("taking orders again" in s[0] for s in self.sent), 1)

    def test_failed_delivery_preserves_closure_and_confirmed_reopening(self):
        with patch.object(w, "send_email", side_effect=RuntimeError("offline")):
            self.assertEqual(self.check("closed"), 3)
            self.assertEqual(self.check("delays"), 3)
            self.assertEqual(self.check("delays"), 3)
        pending = w.load_state()["pending_notifications"]
        self.assertTrue(any("STOPPED" in p["subject"] for p in pending))
        self.assertTrue(any("taking orders again" in p["subject"] for p in pending))
        self.assertEqual(w.load_state().get("notification_receipts", []), [])
        self.check("delays")
        self.assertEqual(w.load_state()["pending_notifications"], [])
        subjects = [s[0] for s in self.sent]
        self.assertEqual(sum("STOPPED" in s for s in subjects), 1)
        self.assertEqual(sum("taking orders again" in s for s in subjects), 1)
        self.assertLess(next(i for i,s in enumerate(subjects) if "STOPPED" in s), next(i for i,s in enumerate(subjects) if "again" in s))

    def test_partial_delivery_retry_does_not_repeat_acknowledged_message(self):
        with patch.object(w, "send_email", side_effect=RuntimeError("offline")):
            self.check("closed")
            self.check("delays")
            self.check("delays")
        with patch.object(w, "send_email", side_effect=[None, RuntimeError("offline")]):
            self.assertEqual(self.check("unknown"), 2)
        self.assertEqual(len(w.load_state()["notification_receipts"]), 1)
        self.check("unknown")
        self.assertFalse(any("STOPPED" in s[0] for s in self.sent))
        self.assertEqual(w.load_state()["pending_notifications"], [])

    def test_dry_run_never_confirms_or_saves_recovery(self):
        self.check("closed")
        self.check("delays")
        before = w.STATE_FILE.read_bytes()
        count = len(self.sent)
        self.check("delays", dry_run=True)
        self.assertEqual(w.STATE_FILE.read_bytes(), before)
        self.assertEqual(len(self.sent), count)


if __name__ == "__main__":
    unittest.main()
