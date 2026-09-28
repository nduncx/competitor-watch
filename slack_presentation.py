"""Slack-only presentation of existing notifications; no monitoring/state decisions."""
import re


def _card(headline, summary, footer=''):
    blocks = [
        {'type': 'header', 'text': {'type': 'plain_text', 'text': headline[:150], 'emoji': True}},
        {'type': 'section', 'text': {'type': 'plain_text', 'text': summary[:3000], 'emoji': True}},
    ]
    if footer:
        blocks.append({'type': 'context', 'elements': [
            {'type': 'plain_text', 'text': footer, 'emoji': True}]})
    return {'text': '\n'.join(x for x in (headline, summary, footer) if x),
            'blocks': blocks, 'mrkdwn': False, 'unfurl_links': False, 'unfurl_media': False}


def slack_payload(subject, body, target='Hungry Monkey', timezone='Europe/Gibraltar'):
    """Restyle known internal templates, preserving unfamiliar/historical messages.

    This adapter runs only after the existing alert rules have chosen a message.
    It never changes state, queues, delivery acknowledgement or website readings.
    Page-derived text is always plain_text, never executable Slack markup.
    """
    if subject == 'Competitor Watch: message layout preview':
        payload = _card('🎨 MESSAGE LAYOUT PREVIEW',
                        'Examples only — these are NOT live status reports. Monitoring rules are unchanged.')
        for headline, summary in [
            ('🟠 OPEN — LONG DELAYS', 'Orders accepted, but delays continue.'),
            ('🔴 CLOSED — ORDERS STOPPED', 'Orders are not being accepted.'),
            ('🟢 OPEN — NO DELAY WARNING', 'Orders accepted; no long-delay warning shown.'),
        ]:
            payload['blocks'].append({'type': 'divider'})
            payload['blocks'].extend(_card(headline, summary)['blocks'])
        return payload

    clock = re.search(r'\b\d{2}:\d{2}\b', subject) or re.search(r'\b\d{2}:\d{2}\b', body)
    zone = 'Gibraltar time' if timezone == 'Europe/Gibraltar' else timezone
    footer = f'Checked {clock.group()} · {zone}' if clock else ''
    name = target.upper()
    passed = 'OPEN (basket test)' in subject or 'basket test passed' in subject.lower()
    passed = passed or 'OPEN for deliveries' in body
    headline = summary = None

    if subject.startswith(target + ':') and 'check unconfirmed - last confirmed' in subject:
        headline = f'🟡 {name}: CHANGE AWAITING CONFIRMATION'
        last = re.search(r'Last confirmed status: (.*?), confirmed by the (\d{2}:\d{2}) check', body)
        previous = 'Last confirmed status is retained.'
        if last:
            label = {'not taking orders': 'CLOSED', 'showing a long-delays warning': 'LONG DELAYS'}.get(last[1], last[1])
            previous = f'Last confirmed: {label} at {last[2]}.'
        summary = 'Possible improvement; waiting for the next confirming check.\n' + previous
    elif subject.startswith(target + ' has STOPPED taking orders') or subject.startswith(target + ' is STILL not taking orders'):
        headline = f'🔴 {name}: ' + ('STILL CLOSED' if 'STILL' in subject else 'CLOSED — ORDERS STOPPED')
        summary = 'Orders are not being accepted.'
        estimate = re.search(r'resume our deliveries at\s+(\d{1,2}:\d{2})\s*(am|pm)?', body, re.I)
        if estimate:
            time = estimate[1]
            if estimate[2] and int(time.split(':')[0]) <= 12:
                time += estimate[2].lower()
            summary += f'\nEstimated reopening: {time}, according to their notice.'
    elif subject.startswith(target + ' is taking orders again') or subject.startswith(target + ': ordering pages read as taking orders again'):
        delays = 'still showing a long-delays warning' in body
        if passed:
            headline = f'{"🟠" if delays else "🟢"} {name}: REOPENED — ' + ('LONG DELAYS' if delays else 'NO DELAY WARNING')
            summary = 'Orders accepted again' + (', but delays continue.' if delays else '; no long-delay warning shown.')
        else:
            headline = f'🟡 {name}: ORDERING UNCONFIRMED'
            summary = 'Ordering controls have returned; a successful basket test is not confirmed.'
    elif subject.startswith(target + ' is warning of long delays') or subject.startswith(target + ' is STILL warning of long delays'):
        if passed:
            headline = f'🟠 {name}: ' + ('STILL OPEN' if 'STILL' in subject else 'OPEN') + ' — LONG DELAYS'
            summary = 'Orders accepted, but delays continue.'
        else:
            headline = f'🟠 {name}: LONG DELAYS — ORDERING UNCONFIRMED'
            summary = 'Long-delay warning shown; a successful basket test is not confirmed.'
    elif subject.startswith(target + ': long-delays warning cleared'):
        headline = f'🟢 {name}: OPEN — NO DELAY WARNING' if passed else f'🟡 {name}: NO DELAY WARNING — ORDERING UNCONFIRMED'
        summary = 'Orders accepted; the long-delay warning has cleared.' if passed else 'The warning has cleared; a successful basket test is not confirmed.'
    elif subject == 'Competitor Watch: monitoring problem':
        headline = '⚠️ MONITORING ISSUE — CURRENT STATUS UNKNOWN'
        summary = 'The latest check or notification delivery failed. Retrying; do not treat silence as normal service.'
    elif subject == 'Competitor Watch: monitoring restored':
        headline = '✅ MONITORING RESTORED'
        summary = 'Checks are working again. Competitor status changes are reported separately.'
    elif subject == 'Competitor Watch: no suitable stores available to test':
        headline = '⚪ STATUS UNKNOWN — NO SUITABLE STORES'
        summary = 'All sampled stores were pre-order only and were skipped. The monitor is running and will check again.'
    elif subject == 'Competitor Watch: suitable stores available again':
        headline = '✅ MONITORING COVERAGE RESTORED'
        summary = 'Suitable stores can be checked again. This is not a competitor reopening alert.'
    elif subject.lower() == 'competitor watch: test notification':
        headline = '🧪 COMPETITOR WATCH — TEST MESSAGE'
        summary = 'Slack delivery is working. This is not a live competitor status report.'

    if headline is None:
        # Do not reinterpret historical corrections or future, unrecognized templates.
        return {'text': subject + '\n\n' + body.replace('A screenshot is attached.', ''),
                'mrkdwn': False, 'unfurl_links': False, 'unfurl_media': False}
    return _card(headline, summary, footer)
