# Competitor Watch

Monitors Hungry Monkey delivery availability and posts availability updates to Rock Hero’s #competitor-watch Slack channel.

## Alerts

- Alert window: **09:00–23:30 Europe/Gibraltar**, daily.
- Checks three venues, including Essaouira when available, with the other two prioritising current delivery availability.
- Sends a status message on every successful check while delays or closure continue.
- Recovery requires two consecutive successful checks: this covers closed → delays/open and delays → normal. The first candidate posts an **unconfirmed check** message naming the last confirmed state, rather than announcing reopening.
- Sends one reopening message after confirmation, including any continuing delays. If delays remain, five-minute status updates continue.
- Sends one recovery message when normal service resumes, then stays quiet while normal.
- Unreadable/failed checks and checks outside the alert window reset recovery confirmation. A gap longer than 12 minutes also resets it. Prior incident state and queued notifications are preserved.
- Closure and new delay alerts remain immediate. Confirmed recovery normally takes one additional five-minute check.
- This is a safeguard against isolated false recovery readings, not independent proof that immediate delivery is available. The current page classifier remains under investigation. A generic closure does not establish its cause.

## Friday trial — 25 September 2026

[Follow the live trial](https://github.com/nduncx/competitor-watch/actions/runs/36145304829). It starts checks five minutes apart inside a running GitHub job, avoiding a new scheduled start for every check. Two sequential segments cover the evening and stop at **23:30 Gibraltar time**. There is a brief handover while the second segment starts. No manual restart is needed during a healthy trial.

After every check, `trial-health.json` records the attempt time, result code, last successful check and last known competitor status. A zero result code means that check succeeded; an old timestamp means monitoring may have stalled. The existing `state.json` is saved after each check to preserve pending messages and avoid repeating recovery alerts at the segment handover. These records do not replace an independent outage alarm.

The trial and regular workflow share a concurrency lock to prevent overlapping checks or duplicate alerts. This dated trial will not restart monitoring on later days.

## Regular schedule

The normal workflow requests a check every **five minutes** during a wider UTC window; the script applies the exact Gibraltar alert window. GitHub may delay or skip scheduled runs. Do not treat silence in Slack as proof the competitor is open—check the run history.

Slack delivery must return a successful acknowledgement before a notification is marked delivered. Unconfirmed messages remain queued for retry, and the state file retains delivery receipts. Website checks and mocked tests alone do not prove messages arrived in the office channel.

## Settings and tests

Secret: `SLACK_WEBHOOK_URL`. Never commit its value. The existing Slack secret, detector and alert hours are unchanged by the trial.

Use Actions → Competitor watch → Run workflow with the test notification option to test Slack. With no options it runs a real check; the dry-run option checks the live site without alerts or state changes. Wait until the trial ends before dispatching a separate run because they share the concurrency lock.

Screenshot evidence is kept for three days as a run artifact. During the continuous trial, the latest screenshot becomes available when each segment finishes.

No OVH/OpenClaw changes have been made. A VPS and external heartbeat monitoring remain a later step after the live trial.

## Validation

Run `python -m unittest discover -p "test_*.py" -v`. The containment tests cover the default two-check recovery rule, both disputed alert sequences, complete incident/recovery cycles, failed checks, long gaps, overnight skips, and notification failures/retries. The older notification tests use one-check recovery to isolate queue and transport behaviour. These are mocked tests; verify a live message in Slack separately.
