"""Independent, durable copies of confirmed status cards for Ops."""
import datetime as dt
import json
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

from slack_presentation import slack_payload


def status_payload(item, target, timezone):
    """Allow only the five confirmed transition cards, preserving their payload."""
    if item.get('historical') or any(
            word in item.get('subject', '').lower() for word in ('historical', 'correction')):
        return None
    payload = slack_payload(item['subject'], item['body'], target, timezone)
    name = target.upper()
    allowed = {
        f'🔴 {name}: CLOSED — ORDERS STOPPED',
        f'🟠 {name}: OPEN — LONG DELAYS',
        f'🟠 {name}: REOPENED — LONG DELAYS',
        f'🟢 {name}: REOPENED — NO DELAY WARNING',
        f'🟢 {name}: OPEN — NO DELAY WARNING',
    }
    blocks = payload.get('blocks', [])
    if not blocks or blocks[0].get('text', {}).get('text') not in allowed:
        return None
    return payload


def enqueue(state, item, webhook, target, timezone):
    if not webhook:
        return
    payload = status_payload(item, target, timezone)
    if payload is None:
        return
    pending = state.setdefault('pending_ops_notifications', [])
    receipts = state.get('ops_notification_receipts', [])
    if any(x['id'] == item['id'] for x in pending + receipts):
        return
    pending.append({'id': item['id'], 'payload': payload})


def post_payload(webhook, payload):
    request = urllib.request.Request(webhook, data=json.dumps(payload).encode(),
                                     headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.status != 200 or response.read().strip() != b'ok':
                raise RuntimeError('Ops Slack did not acknowledge delivery')
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f'Ops Slack delivery failed: HTTP {exc.code}') from None
    except urllib.error.URLError:
        raise RuntimeError('Ops Slack delivery failed: connection error') from None


def flush(state, webhook, save, log, timezone):
    """An Ops outage must never hold up the primary channel's queue."""
    pending = state.get('pending_ops_notifications', [])
    if pending and not webhook:
        log('Ops destination is unconfigured; its pending messages are retained.')
        return False
    while pending:
        item = pending[0]
        try:
            post_payload(webhook, item['payload'])
        except Exception as exc:
            # Transport errors can include a secret URL. Log only the class.
            log(f'Ops delivery not confirmed ({type(exc).__name__}); retained for retry.')
            save(state)
            return False
        pending.pop(0)
        receipts = state.setdefault('ops_notification_receipts', [])
        receipts.append({'id': item['id'],
                         'delivered_at': dt.datetime.now(ZoneInfo(timezone)).isoformat()})
        state['ops_notification_receipts'] = receipts[-100:]
        save(state)
        log('Ops status card delivered')
    return True
