# Competitor Watch

Monitors Hungry Monkey delivery availability and posts availability updates to Rock Hero’s #competitor-watch Slack channel.

## Alerts

- Alert window: **09:00–23:30 Europe/Gibraltar**, daily.
- Aims for three usable venues, including Essaouira when eligible. Pre-order stores are excluded and replaced; at most six venue candidates are inspected.
- Sends a status message on every successful check while delays or closure continue.
- Recovery requires two consecutive successful checks: this covers closed → delays/open and delays → normal. The first candidate posts an **unconfirmed check** message naming the last confirmed state, rather than announcing reopening.
- Sends one reopening message after confirmation, including any continuing delays. If delays remain, five-minute status updates continue.
- Sends one recovery message when normal service resumes, then stays quiet while normal.
- Unreadable/failed checks and checks outside the alert window reset recovery confirmation. A gap longer than 12 minutes also resets it. Prior incident state and queued notifications are preserved.
- Closure and new delay alerts remain immediate. Confirmed recovery normally takes one additional five-minute check.
- This is a safeguard against isolated false recovery readings, not independent proof that immediate delivery is available. A generic closure does not establish its cause.

## Continuous GitHub sessions (26 September 2026)

The regular workflow still requests a start every five minutes. Each start during the alert window now checks immediately, then every five minutes inside the running job for up to five hours or until 23:30 Europe/Gibraltar, whichever comes first. This applies on future dates too. A failed check is retried on the next tick. A queued GitHub start can take over after a session finishes; the concurrency lock prevents overlap.

**GitHub can still delay or skip starting jobs.** This improves cadence within an active session, but does not guarantee a maximum five-minute gap between sessions or at morning startup. A dedicated server and independent outage alarm remain future work; OVH/OpenClaw is unchanged.

`monitor-health.json` records each attempt, last successful check, exit code, consecutive failures and session end. Every attempt commits state and health. A checkpoint failure stops the job rather than discarding undelivered messages. Pending notifications are retried, including outside the alert window. Slack must acknowledge delivery before a notification is marked delivered.

A failed check queues one **monitoring problem** Slack message, separate from competitor status. The next successful check queues **monitoring restored**. Consecutive failures do not create repeated identical warnings; unacknowledged messages remain queued. A stopped or never-started GitHub job cannot send its own warning, so this is not an independent watchdog. GitHub failure notifications remain useful.

The Friday 25 September trial finished successfully at 23:30 that night. Its `trial-health.json` file is historical. Use the regular workflow's current `monitor-health.json` and run history for current health.

## Recognized promotion

The informational “Win your order for FREE / Busy Monkey Game” notice observed on 26 September is recorded and dismissed only through its lone OK button, without forms or entering the promotion. Cookies and subsequent status notices are then inspected. This popup never proves availability: the actual basket test is still required. Other unfamiliar notices remain unknown, with a monitoring-problem alert and retry.

## Settings and tests

Secret: `SLACK_WEBHOOK_URL`. Never commit its value. The existing Slack secret, detector and alert hours are unchanged by the trial.

Use Actions → Competitor watch → Run workflow with the test notification option to test Slack. With no options it runs a real check; the dry-run option checks the live site without alerts or state changes. A manually dispatched run shares the concurrency lock and waits for any active session.

Screenshot evidence is kept for three days as a run artifact. Current venue screenshots become available when the session finishes.

No OVH/OpenClaw changes have been made. A VPS and external heartbeat monitoring remain a later step.

## Validation

Run `python -m unittest discover -p "test_*.py" -v`. The containment tests cover the default two-check recovery rule, both disputed alert sequences, complete incident/recovery cycles, failed checks, long gaps, overnight skips, and notification failures/retries. The older notification tests use one-check recovery to isolate queue and transport behaviour. These are mocked tests; verify a live message in Slack separately.


### Reading sequential notices

The detector reads informational notices before dismissing OK / GOT IT / Close / Dismiss, then reads the next notice and the final basket. It retains every refusal and platform-pause notice found during the check; an estimated reopening time never changes status. Unresolved dialogs, failed dismissal and the action limit prevent a recovery inference.

Reopening remains a reading of the ordering pages, not a completed or verified delivery order. The observed Delivery flow requests an address before presenting delivery times. No address, personal information, payment or order is submitted by this monitor.

Local browser regression fixtures are in `test_dialog_regressions.py`. They exercise the real browser reader against simulated sequential notices; passing these tests does not itself verify the live service or Slack delivery.

Fresh browser sessions can display a cookie dialog above an operational notice. The reader preserves both, presses only the exact “Reject non-essential” button on the recognized cookie notice, then resumes status reading. Semantic dialog containers keep their footer buttons in scope. Missing or failed consent dismissal remains uncertain; refusal evidence still wins.

### User-defined basket test (25 September 2026)

The user defines **open for deliveries** as a simple item successfully appearing in an anonymous basket after status notices have been dismissed. Checkout, login, an account, an address and a delivery slot are not part of this criterion. Slack identifies the result as a basket test. No order is submitted.

A positive test requires a current delivery estimate in the directory. A venue saying “We are currently closed but you can still pre-order” is excluded and replaced, and does not establish platform closure or recovery. Explicit refusal/platform-pause notices still take precedence. Products marked pre-order, products requiring choices, and disabled Add buttons are skipped; no modifiers are selected. The actual basket must change from empty to containing the selected item. A failed test is unknown, not closed or open.

Each venue uses an isolated disposable browser context. The detector tries up to six simple products and up to six venue candidates to obtain three usable readings. Probe work is bounded to 25 seconds per venue and total browser work to 180 seconds; each check subprocess is limited to 240 seconds. Evidence includes the item, probe result, reason and notice sequence. The two-check recovery guard and durable Slack retry queue are unchanged.

`test_basket_regressions.py` covers successful addition without Checkout, pre-order exclusions and replacements, required options, unchanged/dirty baskets, refusals revealed after adding, unfamiliar popups, timeouts, and aggregation. These local fixtures do not prove the live site or Slack delivery.

`test_continuous_watch.py` verifies five-minute retries, daily and overnight cutoff, five-hour session limits, failure/recovery messages, durable delivery retries and checkpoint failure handling.
