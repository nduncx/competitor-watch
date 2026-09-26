"""Run five-minute checks inside a bounded GitHub job, with durable health alerts."""
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

import competitor_watch as monitor

HEALTH_FILE = Path('monitor-health.json')
INTERVAL = 300
MAX_SESSION_SECONDS = 5 * 60 * 60


def window_end(now):
    """Return this active local window's end; outside the window, do no checks."""
    if not monitor.in_trading_hours(now):
        return None
    start, end = [dt.time.fromisoformat(x) for x in monitor.TRADING_HOURS.split('-')]
    day = now.date()
    if end <= start and now.time().replace(tzinfo=None) >= start:
        day += dt.timedelta(days=1)
    return dt.datetime.combine(day, end, tzinfo=now.tzinfo)


def update_health(state, previous, code, started, finished, session_end):
    """Queue monitor failures/recovery separately from competitor availability."""
    health = dict(previous)
    failed_before = previous.get('consecutive_failures', 0)
    health.update(last_attempt_started_at=started.isoformat(),
                  last_attempt_finished_at=finished.isoformat(), last_exit_code=code,
                  run_id=os.getenv('GITHUB_RUN_ID'), status='healthy' if code == 0 else 'impaired',
                  session_stops_at=session_end.isoformat(),
                  next_check_at=min(started + dt.timedelta(seconds=INTERVAL), session_end).isoformat(),
                  consecutive_failures=failed_before + 1 if code else 0)
    message = None
    if code:
        if not failed_before:
            health['incident_since'] = started.isoformat()
            reasons = {2: 'The website could not be read reliably.',
                       3: 'Slack did not acknowledge delivery of a queued notification.',
                       124: 'The website check exceeded its time limit.'}
            message = ('Competitor Watch: monitoring problem',
                       f"At {finished:%H:%M} ({monitor.TIMEZONE}), a check failed. "
                       + reasons.get(code, f'The check exited with code {code}.')
                       + '\nThis is a monitoring problem, not evidence that Hungry Monkey is open or closed.'
                       + '\nThe running job will retry on the next five-minute check. '
                       'Queued notifications are retained. Do not treat silence as normal service.')
    else:
        health['last_success_at'] = finished.isoformat()
        health.pop('incident_since', None)
        if failed_before:
            message = ('Competitor Watch: monitoring restored',
                       f'A complete check succeeded at {finished:%H:%M} ({monitor.TIMEZONE}). '
                       'Five-minute checks have resumed inside the running GitHub job. '
                       'Hungry Monkey status changes are reported separately.')
    if message:
        state.setdefault('pending_notifications', []).append({
            'id': 'monitor:' + started.isoformat() + ':' + ('failed' if code else 'restored'),
            'subject': message[0], 'body': message[1], 'attach_screenshot': False,
            'observed_at': started.isoformat()})
    return health


def now():
    return dt.datetime.now(ZoneInfo(monitor.TIMEZONE))


def attempt(session_end):
    started = now()
    try:
        code = subprocess.run([sys.executable, '-u', 'competitor_watch.py'], timeout=240).returncode
    except subprocess.TimeoutExpired:
        code = 124
        print('Check timed out; retaining incident state and retrying next interval.', flush=True)
    previous = json.loads(HEALTH_FILE.read_text()) if HEALTH_FILE.exists() else {}
    state = monitor.load_state()
    health = update_health(state, previous, code, started, now(), session_end)
    # Save queued messages before transport. Only the existing Slack ack path removes them.
    monitor.save_state(state)
    temporary = HEALTH_FILE.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(health, indent=2) + '\n')
    temporary.replace(HEALTH_FILE)
    delivered = monitor.flush_notifications(state)
    health['notification_delivery_confirmed'] = delivered
    if not delivered:
        health['status'] = 'delivery_pending'
    temporary.write_text(json.dumps(health, indent=2) + '\n')
    temporary.replace(HEALTH_FILE)
    return code if delivered else 3


def checkpoint():
    files = [str(p) for p in (monitor.STATE_FILE, HEALTH_FILE) if p.exists()]
    subprocess.run(['git', 'add', *files], check=True)
    if subprocess.run(['git', 'diff', '--cached', '--quiet']).returncode:
        subprocess.run(['git', 'commit', '-m', 'Save monitor checkpoint [skip ci]'], check=True)
    # This also picks up detector fixes for the next subprocess.
    for retry in range(3):
        subprocess.run(['git', 'pull', '--rebase', 'origin', 'main'], check=True)
        if subprocess.run(['git', 'push', 'origin', 'HEAD:main']).returncode == 0:
            return
    raise RuntimeError('Could not persist monitor checkpoint; stopping to protect pending notifications')


def run_session(run_attempt=attempt, save_checkpoint=checkpoint,
                now_fn=now, monotonic=time.monotonic, sleep=time.sleep):
    started = now_fn()
    end = window_end(started)
    if end is None:
        print('Outside the alert window; no competitor availability inference.', flush=True)
        return 0
    session_end = min(end, started + dt.timedelta(seconds=MAX_SESSION_SECONDS))
    deadline = monotonic() + (session_end - started).total_seconds()
    last_code = 0
    print(f'Five-minute worker active until {session_end.isoformat()}', flush=True)
    while monotonic() < deadline and now_fn() < session_end:
        tick = monotonic()
        last_code = run_attempt(session_end)
        save_checkpoint()
        if last_code:
            print(f'Check returned {last_code}; retrying next interval.', flush=True)
        delay = min(deadline, tick + INTERVAL) - monotonic()
        if delay > 0:
            sleep(delay)
    print('Bounded monitoring session finished; checkpoints saved.', flush=True)
    return last_code


if __name__ == '__main__':
    if window_end(now()) is None:
        # Preserve overnight pending-notification retries and recovery-streak reset.
        code = monitor.run_check()
        checkpoint()
        sys.exit(code)
    sys.exit(run_session())
