"""Exercise complete alert cycles and recovery with no live Slack traffic."""
import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("watch", Path(__file__).with_name("competitor_watch.py"))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)


class Notifications(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        w.STATE_FILE = Path(self.tmp.name) / "state.json"
        w.RECOVERY_FILE = Path(self.tmp.name) / "recovery.json"
        w.ALERT_ON_DELAYS = w.ALERT_ON_REOPEN = True
        # Isolate queue/transport behaviour; test_containment covers the production two-check guard.
        w.RECOVERY_CONFIRMATIONS = 1
        self.sent = []
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(w, "in_trading_hours", return_value=True))
        self.stack.enter_context(patch.object(w, "send_email", side_effect=lambda *a, **k: self.sent.append(a)))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def check(self, status, dry_run=False):
        result = {"status": status, "notice": "long delays" if status == "delays" else "",
                  "venues": [{"name": "Essaouira", "status": status}], "detail": "test"}
        with patch.object(w, "check_platform", return_value=result):
            return w.run_check(dry_run)

    def test_full_cycle_and_repeated_states(self):
        for status in ["open", "delays", "delays", "closed", "closed", "delays", "delays", "open", "open"]:
            self.assertEqual(self.check(status), 0)
        self.assertEqual(len(self.sent), 7)
        self.assertIn("warning of long delays", self.sent[0][0])
        self.assertIn("STILL warning", self.sent[1][0])
        self.assertIn("STOPPED", self.sent[2][0])
        self.assertIn("STILL not taking orders", self.sent[3][0])
        self.assertIn("taking orders again", self.sent[4][0])
        self.assertIn("still showing a long-delays warning", self.sent[4][1])
        self.assertIn("STILL warning", self.sent[5][0])
        self.assertIn("warning cleared", self.sent[6][0])
        self.assertEqual(len(w.load_state()["notification_receipts"]), 7)

    def test_normal_service_is_quiet_until_next_incident(self):
        for status in ["closed", "open", "open", "open", "delays", "open", "open"]:
            self.assertEqual(self.check(status), 0)
        self.assertEqual(len(self.sent), 4)
        self.assertIn("STOPPED", self.sent[0][0])
        self.assertIn("taking orders again", self.sent[1][0])
        self.assertIn("warning of long delays", self.sent[2][0])
        self.assertIn("warning cleared", self.sent[3][0])

    def test_unknown_observation_does_not_repeat_stale_busy_status(self):
        self.check("delays")
        self.sent.clear()
        self.assertEqual(self.check("unknown"), 2)
        self.assertEqual(self.sent, [])
        self.assertEqual(w.load_state()["status"], "delays")

    def test_failed_closure_delivery_survives_reopening_and_restart(self):
        self.check("delays")
        self.sent.clear()
        with patch.object(w, "send_email", side_effect=RuntimeError("transport unavailable")):
            self.assertEqual(self.check("closed"), 3)
            self.assertEqual(w.load_state()["status"], "closed")
            self.assertEqual(self.check("delays"), 3)
        # State is reloaded from disk on every check, including the pending closure.
        self.assertEqual(len(w.load_state()["pending_notifications"]), 2)
        self.assertEqual(self.check("delays"), 0)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("STOPPED", self.sent[0][0])
        self.assertIn("again", self.sent[1][0])
        self.assertEqual(w.load_state()["pending_notifications"], [])
        self.check("delays")
        self.assertEqual(len(self.sent), 3)
        self.assertIn("STILL warning", self.sent[2][0])

    def test_retry_does_not_repeat_already_delivered_closure(self):
        self.check("delays")
        self.sent.clear()
        with patch.object(w, "send_email", side_effect=RuntimeError("offline")):
            self.check("closed")
        with patch.object(w, "send_email", side_effect=[None, RuntimeError("offline")]):
            self.assertEqual(self.check("delays"), 3)
        self.assertEqual(len(w.load_state()["pending_notifications"]), 1)
        self.check("delays")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("again", self.sent[0][0])

    def test_unreadable_page_preserves_status_but_retries_pending_alert(self):
        self.check("delays")
        with patch.object(w, "send_email", side_effect=RuntimeError("offline")):
            self.check("closed")
        self.sent.clear()
        self.assertEqual(self.check("unknown"), 2)
        self.assertEqual(w.load_state()["status"], "closed")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("STOPPED", self.sent[0][0])

    def test_reviewed_correction_once_and_dry_run_has_no_side_effects(self):
        correction = {"id": "incident-1", "subject": "Historical correction", "body": "Past observations"}
        w.RECOVERY_FILE.write_text(json.dumps(correction))
        self.assertEqual(self.check("delays", dry_run=True), 0)
        self.assertFalse(w.STATE_FILE.exists())
        self.assertEqual(self.sent, [])
        self.check("delays")
        self.assertEqual(len(self.sent), 2)  # correction plus first actual busy observation
        self.check("delays")
        self.check("closed")
        self.check("delays")
        self.assertEqual(sum(a[0] == "Historical correction" for a in self.sent), 1)
        self.assertEqual(w.load_state()["applied_recoveries"], ["incident-1"])

    def test_nighttime_skips_site_and_retries_existing_alert(self):
        with patch.object(w, "send_email", side_effect=RuntimeError("offline")):
            self.check("closed")
        with patch.object(w, "in_trading_hours", return_value=False), patch.object(w, "check_platform") as site:
            self.assertEqual(w.run_check(), 0)
            site.assert_not_called()
        self.assertEqual(len(self.sent), 1)


if __name__ == "__main__":
    unittest.main()
