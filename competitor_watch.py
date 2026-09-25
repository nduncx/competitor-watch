#!/usr/bin/env python3
"""
competitor_watch.py - emails you when Hungry Monkey (order.hungrymonkey.gi)
stops taking orders during its normal trading hours - i.e. it has paused
because it is too busy - and again when it starts taking orders again. It can
also warn you when Hungry Monkey starts showing its "orders may incur long
delays" notice.

How it decides:
  1. Loads the Hungry Monkey directory and picks a few venues it lists as open.
  2. Presses ORDER NOW on each, which opens the venue's ordering page.
  3. If every venue refuses orders - or any page shows Hungry Monkey's own
     "we will resume our deliveries" notice - Hungry Monkey is closed.
     One venue refusing while the others accept just means that venue is shut.

Usage
  python competitor_watch.py               run one check (cron / Task Scheduler / GitHub Actions)
  python competitor_watch.py --every 600   keep running, re-checking every 10 minutes
  python competitor_watch.py --test-email  send a test email to confirm the email settings
  python competitor_watch.py --dry-run     run a check and print the result; no email, no state saved

All settings live in the block below. Each one can also be overridden with an
environment variable of the same name. See SETUP.md for the walkthrough.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import smtplib
import sys
import time
import urllib.request
import urllib.error
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo


def setting(name: str, default: str) -> str:
    """Environment variable if set and non-empty, otherwise the default."""
    return os.getenv(name) or default


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
DIRECTORY_URL = setting("DIRECTORY_URL", "https://order.hungrymonkey.gi/")
TARGET_NAME = setting("TARGET_NAME", "Hungry Monkey")

# How many venues to open per check. More = fewer false alarms, slower check.
VENUES_TO_CHECK = int(setting("VENUES_TO_CHECK", "3"))
PREFERRED_VENUE = setting("PREFERRED_VENUE", "Essaouira")
ORDER_BUTTON_TEXT = setting("ORDER_BUTTON_TEXT", "ORDER NOW")

# Hungry Monkey's normal trading hours, local time, 24-hour clock.
# A closure that starts INSIDE this window is treated as "closed because busy"
# and triggers an email. A closure outside it is just them shutting for the
# night. Set the end 20-30 minutes BEFORE they really close so the normal
# switch-off never triggers an alert. Windows may cross midnight ("18:00-01:00").
TRADING_HOURS = setting("TRADING_HOURS", "09:00-23:30")
TIMEZONE = setting("TIMEZONE", "Europe/Gibraltar")

# Wording the check looks for on a venue's ordering page (case-insensitive).
PLATFORM_CLOSED_PHRASES = [       # Hungry Monkey's own "we're too busy" notice
    "resume our deliveries",
    "everyone is a hungry monkey",
]
CLOSED_PHRASES = ["not taking orders"]           # the standard closed pop-up + basket panel
DELAY_PHRASES = ["long delays", "incur delays"]  # their "busy but still open" warning
OPEN_PHRASES = ["collection or delivery"]        # the basket's order button when open
PAGE_LOADED_PHRASES = ["basket", "search menu items"]

# Words on a directory card that mean the venue is NOT open right now.
NOT_OPEN_HINTS = ["closed", "pre-order", "preorder", "opens at", "opening at", "unavailable", "tomorrow"]

# Extra emails ("true" / "false")
ALERT_ON_REOPEN = setting("ALERT_ON_REOPEN", "true").strip().lower() == "true"
ALERT_ON_DELAYS = setting("ALERT_ON_DELAYS", "true").strip().lower() == "true"

# Consecutive checks that must read "taking orders" before an incident (closed
# or delays) is declared over and a reopening / all-clear is posted. Closures
# and delays warnings are still announced on the first check that shows them.
RECOVERY_CONFIRMATIONS = int(setting("RECOVERY_CONFIRMATIONS", "2"))
# The agreeing checks must be consecutive: any unreadable check, failed check,
# skipped check or a gap longer than this starts the count again.
RECOVERY_MAX_GAP_MINUTES = int(setting("RECOVERY_MAX_GAP_MINUTES", "12"))

STATE_FILE = Path(setting("STATE_FILE", "state.json"))
RECOVERY_FILE = Path(setting("RECOVERY_FILE", "notification-recovery.json"))
SCREENSHOT_FILE = Path(setting("SCREENSHOT_FILE", "evidence.png"))

# Email. For Gmail: smtp.gmail.com, port 587, and an App Password (SETUP.md).
SLACK_WEBHOOK_URL = setting("SLACK_WEBHOOK_URL", "")

SMTP_HOST = setting("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(setting("SMTP_PORT", "587"))
SMTP_USER = setting("SMTP_USER", "")
SMTP_PASS = setting("SMTP_PASS", "")
EMAIL_FROM = setting("EMAIL_FROM", SMTP_USER)
EMAIL_TO = setting("EMAIL_TO", SMTP_USER)   # comma-separate several addresses


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def log(message: str) -> None:
    print(message, flush=True)


def in_trading_hours(now: dt.datetime) -> bool:
    start_s, end_s = TRADING_HOURS.split("-")
    start = dt.time.fromisoformat(start_s.strip())
    end = dt.time.fromisoformat(end_s.strip())
    t = now.time()
    if start <= end:
        return start <= t < end
    return t >= start or t < end          # window crosses midnight


def classify_venue(page_text: str) -> str:
    """One venue page -> platform_closed | closed | delays | open | unknown."""
    t = page_text.lower()
    if any(p in t for p in PLATFORM_CLOSED_PHRASES):
        return "platform_closed"
    if any(p in t for p in CLOSED_PHRASES):
        return "closed"
    if any(p in t for p in DELAY_PHRASES):
        return "delays"
    if any(p in t for p in OPEN_PHRASES):
        return "open"
    return "unknown"


BUTTON_LABELS = {"ok", "got it", "close", "dismiss", "continue", "accept"}


def strip_buttons(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if line.strip().lower() not in BUTTON_LABELS
    ).strip()


def dedupe(texts: list[str]) -> list[str]:
    """Drop empty, duplicate, and contained-in-another texts."""
    cleaned = [t for t in dict.fromkeys(strip_buttons(t) for t in texts if t.strip()) if t]
    return [t for t in cleaned if not any(t != o and t in o for o in cleaned)]


def extract_notice(page_text: str, popups: list[str]) -> str:
    """The notice a venue page is showing, for the email body."""
    phrases = PLATFORM_CLOSED_PHRASES + CLOSED_PHRASES + DELAY_PHRASES
    from_popups = [t for t in dedupe(popups) if any(p in t.lower() for p in phrases)]
    if from_popups:
        return "\n\n".join(from_popups)
    lines = (line.strip() for line in page_text.splitlines())
    hits = [line for line in lines if any(p in line.lower() for p in phrases)]
    return "\n".join(dict.fromkeys(hits))


BADGES = {"new", "special offer", "offer", "collection", "delivery"}


def venue_name(card_text: str, fallback: str) -> str:
    """First line of a directory card that looks like a name."""
    for line in card_text.splitlines():
        s = line.strip()
        if len(s) < 3 or s.lower() in BADGES or s.upper() == ORDER_BUTTON_TEXT.upper():
            continue
        if ":" in s or s.startswith(("£", "$", "€")):
            continue
        return s
    return fallback


def looks_open(card_text: str) -> bool:
    # A current delivery estimate is evidence of availability; a future
    # clock time (Today/Tomorrow/weekday at...) is not.
    return bool(re.search(
        r"delivery(?:\s+only)?\s*:\s*(?:[0-9]+(?:\s*[-–]\s*[0-9]+)?\s*min(?:ute)?s?\b|asap\b)",
        card_text, re.IGNORECASE,
    ))


def select_venues(cards: list[dict]) -> list[dict]:
    """Keep the user's late-closing anchor and fill remaining slots from open venues."""
    preferred = next((c for c in cards if PREFERRED_VENUE.casefold() in c["name"].casefold()), None)
    chosen = [preferred] if preferred else []
    others = [c for c in cards if c is not preferred]
    candidates = [c for c in others if c["looks_open"]] + [c for c in others if not c["looks_open"]]
    return (chosen + candidates)[:VENUES_TO_CHECK]


# ---------------------------------------------------------------------------
# Browser work
# ---------------------------------------------------------------------------
# Finds every visible ORDER NOW button on the directory, tags it so it can be
# clicked later, and returns the text of the card it sits in (the biggest
# ancestor that still contains only that one button) plus its link, if any.
CARD_SCRIPT = r"""
(label) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim().toUpperCase();
  const visible = el => el.getClientRects().length > 0;
  const leaves = Array.from(document.querySelectorAll('body *'))
    .filter(el => el.children.length === 0 && norm(el.textContent) === label && visible(el));
  const countIn = el => leaves.filter(l => el.contains(l)).length;
  return leaves.map((btn, i) => {
    btn.setAttribute('data-cw-index', String(i));
    let node = btn;
    while (node.parentElement && node.parentElement !== document.body
           && countIn(node.parentElement) === 1) node = node.parentElement;
    const link = btn.closest('a[href]');
    const href = link && /^https?:/.test(link.href) ? link.href : null;
    return { index: i, text: node.innerText || '', href: href };
  });
}
"""

PAGE_HAS_TEXT = "needles => { const t = document.body.innerText.toLowerCase();" \
                " return needles.some(n => t.includes(n)); }"


def new_context(browser):
    return browser.new_context(
        viewport={"width": 1280, "height": 900},
        locale="en-GB",
        timezone_id=TIMEZONE,
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        ),
    )


def load_directory(page) -> list[dict]:
    """Open the directory and return its venue cards, in page order."""
    page.goto(DIRECTORY_URL, wait_until="domcontentloaded", timeout=60_000)
    try:
        page.wait_for_function(PAGE_HAS_TEXT, arg=[ORDER_BUTTON_TEXT.lower()], timeout=30_000)
    except Exception:
        pass
    page.wait_for_timeout(2_500)                  # let the venue list finish rendering
    for label in ("No thanks", "Accept"):         # app-download nag and cookie bar
        try:
            page.get_by_text(label, exact=True).first.click(timeout=1_000)
        except Exception:
            pass
    cards = page.evaluate(CARD_SCRIPT, ORDER_BUTTON_TEXT.upper())
    for card in cards:
        card["name"] = venue_name(card["text"], f"venue {card['index'] + 1}")
        card["looks_open"] = looks_open(card["text"])
    return cards


def open_venue(context, page, card):
    """Press ORDER NOW on a directory card. Returns (venue page, opened_new_tab)."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    if card.get("href"):                          # the button is a plain link: open it directly
        venue_page = context.new_page()
        venue_page.goto(card["href"], wait_until="domcontentloaded", timeout=60_000)
        return venue_page, True

    button = page.locator(f'[data-cw-index="{card["index"]}"]').first
    button.scroll_into_view_if_needed(timeout=5_000)
    try:
        with context.expect_page(timeout=10_000) as new_tab:
            try:
                button.click(timeout=5_000)
            except PlaywrightTimeout:            # something overlays it: fire the click directly
                button.dispatch_event("click")
        venue_page = new_tab.value
        venue_page.wait_for_load_state("domcontentloaded", timeout=60_000)
        return venue_page, True
    except PlaywrightTimeout:                     # no new tab: it navigated in the same tab
        page.wait_for_load_state("domcontentloaded", timeout=60_000)
        return page, False


NOTICE_SELECTOR = '[role="dialog"], [role="alertdialog"], md-dialog'
NOTICE_FALLBACK = '.preo-modal, .mat-dialog-container, .modal-content'
DISMISS_LABEL = re.compile(r"^(?:got\s+it|ok|close|dismiss)$", re.IGNORECASE)
NOTICE_SNAPSHOT = r"""() => {
  const visible = e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
  let dialogs = [...document.querySelectorAll('[role="dialog"], [role="alertdialog"], md-dialog')].filter(visible);
  if (!dialogs.length) dialogs = [...document.querySelectorAll('.preo-modal, .mat-dialog-container, .modal-content')].filter(visible);
  // Keep semantic containers with their footer; a text-only nested wrapper is not a whole dialog.
  const withButtons = dialogs.filter(e => [...e.querySelectorAll('button, [role="button"]')].some(visible));
  const leaves = dialogs.filter(e => !dialogs.some(other => other !== e && e.contains(other)
    && (withButtons.includes(other) || !withButtons.includes(e))));
  return {text: document.body.innerText || '', notices: leaves.map(e => ({
    text: e.innerText || '', buttons: [...e.querySelectorAll('button, [role="button"]')].filter(visible).map(b => b.innerText.trim()),
    has_inputs: [...e.querySelectorAll('input, textarea, select, [contenteditable="true"]')].some(visible)
  }))};
}"""


def notice_snapshot(page) -> dict:
    """Capture rendered evidence and leaf dialog containers, excluding duplicate wrappers."""
    return page.evaluate(NOTICE_SNAPSHOT)


def read_venue_page(venue_page) -> dict:
    """Read the complete bounded sequence; never clear evidence by dismissing it."""
    needles = PLATFORM_CLOSED_PHRASES + CLOSED_PHRASES + OPEN_PHRASES + PAGE_LOADED_PHRASES
    try:
        venue_page.wait_for_function(PAGE_HAS_TEXT, arg=needles, timeout=30_000)
    except Exception:
        pass
    venue_page.wait_for_timeout(2_500)
    transcript, texts, popups = [], [], []
    unresolved = False
    final_text = ""
    notice_clicks = 0
    cookie_clicks = 0
    for step in range(6):
        try:
            snapshot = notice_snapshot(venue_page)
            final_text = snapshot["text"]
            texts.append(final_text)
            notices = snapshot["notices"]
            transcript.append({"step": step, "notices": notices})
            popups.extend(n["text"] for n in notices)
            if not notices:
                break
            # Classify all visible overlays before acting, so an unfamiliar one cannot be hidden.
            cookie_notices = [n for n in notices if 'we use cookies' in n['text'].lower()
                              and 'cookie policy' in n['text'].lower()]
            unknown_notices = [n for n in notices if n not in cookie_notices and not any(
                p in n['text'].lower() for p in PLATFORM_CLOSED_PHRASES + CLOSED_PHRASES + DELAY_PHRASES)]
            if step == 5 or unknown_notices or any(n['has_inputs'] for n in notices):
                unresolved = True
                break
            # Fresh GitHub contexts show consent above the operational notice. Reject it explicitly.
            if cookie_notices:
                if cookie_clicks or len(cookie_notices) != 1:
                    unresolved = True
                    break
                notice = cookie_notices[0]
                labels = [b for b in notice['buttons'] if b == 'Reject non-essential']
                cookie_clicks += 1
            elif len(notices) == 1 and notice_clicks < 4:
                notice = notices[0]
                labels = [b for b in notice["buttons"] if DISMISS_LABEL.fullmatch(b)]
                notice_clicks += 1
            else:
                unresolved = True
                break
            if len(labels) != 1:
                unresolved = True
                break
            # The button may belong to nested wrappers; an exact role match identifies one DOM node.
            button = venue_page.get_by_role("button", name=labels[0], exact=True)
            button.click(timeout=2_000)
            transcript[-1]["action"] = labels[0]
            venue_page.wait_for_function(
                'old => JSON.stringify((' + NOTICE_SNAPSHOT + ')().notices.map(n => n.text)) !== JSON.stringify(old)',
                arg=[n['text'] for n in notices], timeout=2_500)
            venue_page.wait_for_timeout(500)
        except Exception as exc:
            transcript.append({"step": step, "error": type(exc).__name__})
            unresolved = True
            break
    combined = "\n".join(texts + popups)
    status = classify_venue(combined)
    try:
        order_control = venue_page.get_by_role("button", name=re.compile(r"^collection or delivery$", re.I))
        order_button = (not unresolved and order_control.count() == 1
                        and order_control.is_visible() and order_control.is_enabled())
        if order_button:
            # Check whether an overlay intercepts it, without actually clicking or progressing an order.
            order_control.click(trial=True, timeout=1_000)
    except Exception:
        order_button, unresolved = False, True
    if status not in {"closed", "platform_closed"} and (unresolved or not order_button):
        status = "unknown"
    result = {"status": status, "notice": extract_notice(combined, popups),
              "url": venue_page.url, "text": combined, "notice_transcript": transcript,
              "unresolved_notice": unresolved, "order_button_after_notices": order_button}
    log("Notice sequence: " + json.dumps({k: v for k, v in result.items() if k != "text"}, ensure_ascii=False))
    return result


def check_platform() -> dict:
    """Run one full check. Returns a result dict with an overall 'status'."""
    from playwright.sync_api import sync_playwright

    results: list[dict] = []
    directory_text = ""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            context = new_context(browser)
            page = context.new_page()
            cards = load_directory(page)
            directory_text = page.evaluate("document.body.innerText") or ""
            if not cards:
                return {"status": "unknown", "venues": [], "notice": "",
                        "detail": f"no '{ORDER_BUTTON_TEXT}' buttons found on the directory",
                        "text": directory_text}

            open_cards = [c for c in cards if c["looks_open"]]
            chosen = select_venues(cards)
            log(f"Directory lists {len(cards)} venues with an {ORDER_BUTTON_TEXT} button, "
                f"{len(open_cards)} of them showing as open. Checking: "
                + ", ".join(c["name"] for c in chosen))

            on_directory = True
            for n, card in enumerate(chosen, 1):
                if not on_directory:              # last click navigated this tab away: go back
                    fresh = load_directory(page)
                    card = (next((c for c in fresh if c["name"] == card["name"]), None)
                            or (fresh[card["index"]] if card["index"] < len(fresh) else None))
                    on_directory = True
                    if card is None:
                        results.append({"status": "unknown", "name": "?", "notice": "", "url": "",
                                        "text": "venue card disappeared after reloading"})
                        continue
                venue_page, new_tab = open_venue(context, page, card)
                result = read_venue_page(venue_page)
                result["name"] = card["name"]
                result["listed_open"] = card["looks_open"]
                result["screenshot"] = f"evidence-{n}.png"
                venue_page.screenshot(path=result["screenshot"])
                results.append(result)
                log(f"  {card['name']}: {result['status']}   {result['url']}")
                if new_tab:
                    venue_page.close()
                else:
                    on_directory = False
        finally:
            browser.close()

    summary = summarise(results, directory_text, directory_has_open_venues=bool(open_cards))
    if not open_cards and summary["status"] not in {"closed", "delays"}:
        summary["status"] = "no_open_venues"
        summary["detail"] = "Directory has no venues with current delivery estimates."
    return summary


def summarise(results: list[dict], directory_text: str = "", *,
              directory_has_open_venues: bool | None = None) -> dict:
    """Combine per-venue results into one platform status."""
    statuses = [r["status"] for r in results]
    known = [r for r in results if r["status"] != "unknown"]
    deciding = None

    if "platform_closed" in statuses:
        status, deciding = "closed", next(r for r in results if r["status"] == "platform_closed")
    elif known and all(r["status"] == "closed" for r in known) \
            and len(known) == len(results) and len(known) >= 2 \
            and (sum(bool(r.get("listed_open", False)) for r in known) >= 2
                 or directory_has_open_venues is False):
        status, deciding = "closed", known[0]
    elif "delays" in statuses:
        status, deciding = "delays", next(r for r in results if r["status"] == "delays")
    elif "open" in statuses:
        status, deciding = "open", next(r for r in results if r["status"] == "open")
    else:
        status = "unknown"

    # keep only the deciding venue's screenshot, as evidence.png
    keep = (deciding or {}).get("screenshot") or (results[0].get("screenshot") if results else None)
    for r in results:
        shot = r.get("screenshot")
        if shot and Path(shot).exists():
            if shot == keep:
                Path(shot).replace(SCREENSHOT_FILE)
            else:
                Path(shot).unlink()

    return {
        "status": status,
        "notice": (deciding or {}).get("notice", ""),
        "url": (deciding or {}).get("url", ""),
        "venues": results,
        "text": (deciding or {}).get("text") or directory_text,
    }


# ---------------------------------------------------------------------------
# State (observations, pending notifications, and delivery receipts)
# ---------------------------------------------------------------------------
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"status": "unknown", "since": None, "alerted": False}


def save_state(state: dict) -> None:
    temporary = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temporary.replace(STATE_FILE)


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
def send_email(subject: str, body: str, attach_screenshot: bool) -> None:
    if SLACK_WEBHOOK_URL:
        # Plain text avoids interpreting page text as Slack mentions or markup.
        text = subject + "\n\n" + body.replace("A screenshot is attached.", "")
        run_id = os.getenv("GITHUB_RUN_ID")
        repository = os.getenv("GITHUB_REPOSITORY")
        if attach_screenshot and run_id and repository:
            text += f"\nScreenshot: https://github.com/{repository}/actions/runs/{run_id} (Artifacts)"
        payload = {"text": text, "mrkdwn": False, "unfurl_links": False, "unfurl_media": False}
        request = urllib.request.Request(SLACK_WEBHOOK_URL,
            data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.status != 200 or response.read().strip() != b"ok":
                    raise RuntimeError("Slack did not confirm notification delivery")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Slack delivery failed: HTTP {exc.code}") from None
        except urllib.error.URLError:
            raise RuntimeError("Slack delivery failed: connection error") from None
        log("Slack notification delivered")
        return
    if not (SMTP_USER and SMTP_PASS and EMAIL_TO):
        raise RuntimeError("Email is not configured: set SMTP_USER, SMTP_PASS and EMAIL_TO")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg.set_content(body)
    if attach_screenshot and SCREENSHOT_FILE.exists():
        msg.add_attachment(SCREENSHOT_FILE.read_bytes(), maintype="image",
                           subtype="png", filename="hungry-monkey-page.png")

    if SMTP_PORT == 465:
        server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30)
    else:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
    with server:
        if SMTP_PORT != 465:
            server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)
    log(f"Email sent to {EMAIL_TO}: {subject}")


VENUE_LABELS = {
    "platform_closed": "refusing orders (Hungry Monkey's own notice)",
    "closed": "refusing orders",
    "delays": "long-delays notice; ordering control shown, delivery not verified",
    "open": "ordering control shown; delivery not verified",
    "unknown": "could not read the page",
}


def venue_lines(result: dict) -> str:
    lines = [f"  - {v.get('name', '?')}: {VENUE_LABELS.get(v['status'], v['status'])}"
             for v in result.get("venues", [])]
    return "\n".join(lines) or "  (none)"


def notice_block(result: dict) -> str:
    notice = result.get("notice") or "(no pop-up text captured)"
    return "\n".join("    " + line for line in notice.splitlines())


def duration_text(since: str | None, now: dt.datetime) -> str:
    if not since:
        return ""
    minutes = int((now - dt.datetime.fromisoformat(since)).total_seconds() // 60)
    if minutes >= 60:
        return f" after about {minutes // 60}h {minutes % 60:02d}m"
    return f" after about {minutes} minutes"


def closed_notification(now: dt.datetime, result: dict) -> dict:
    subject = f"{TARGET_NAME} has STOPPED taking orders ({now:%H:%M})"
    body = (
        f"{TARGET_NAME} was detected as not taking orders at {now:%H:%M} on {now:%A %d %B %Y}.\n\n"
        f"Notice on their ordering page:\n\n{notice_block(result)}\n\n"
        f"Venues checked:\n{venue_lines(result)}\n\n"
        f"Directory: {DIRECTORY_URL}\n"
        f"Ordering page: {result.get('url') or '-'}\n\n"
    )
    if ALERT_ON_REOPEN:
        body += " You'll get another notification when they start taking orders again."
    return {"subject": subject, "body": body + "\n", "attach_screenshot": True}


def reopen_notification(now: dt.datetime, closed_since: str | None, result: dict) -> dict:
    subject = f"{TARGET_NAME}: ordering pages read as taking orders again ({now:%H:%M}) - delivery not verified"
    body = (f"{RECOVERY_CONFIRMATIONS} consecutive checks found an ordering control without a refusal "
            f"on the deciding pages, as of {now:%H:%M}{duration_text(closed_since, now)}. "
            "This is a page-level reading. Delivery availability is not verified: "
            "the observed Delivery flow requires an address before offering delivery times.\n")
    if result["status"] == "delays":
        body += f"\nThey are still showing a long-delays warning:\n\n{notice_block(result)}\n"
    body += f"\nVenues checked:\n{venue_lines(result)}\n\nDirectory: {DIRECTORY_URL}\n"
    return {"subject": subject, "body": body, "attach_screenshot": False}


def delays_notification(now: dt.datetime, result: dict) -> dict:
    subject = f"{TARGET_NAME} is warning of long delays ({now:%H:%M})"
    body = (
        f"At {now:%H:%M} on {now:%A %d %B %Y}, {TARGET_NAME} started showing this notice on "
        f"its ordering pages:\n\n{notice_block(result)}\n\n"
        "An ordering control is shown, but delivery availability is not verified.\n\n"
        f"Venues checked:\n{venue_lines(result)}\n\nDirectory: {DIRECTORY_URL}\n"
    )
    return {"subject": subject, "body": body, "attach_screenshot": True}


def delays_cleared_notification(now: dt.datetime, since: str | None) -> dict:
    subject = f"{TARGET_NAME}: long-delays warning cleared ({now:%H:%M}) - page-level reading"
    body = (f"{TARGET_NAME} is no longer showing its long-delays notice as of {now:%H:%M}"
            f"{duration_text(since, now).replace('after', 'up for')}. "
            f"This follows {RECOVERY_CONFIRMATIONS} consecutive page-level readings. "
            "Delivery availability has not been verified.\n")
    return {"subject": subject, "body": body, "attach_screenshot": False}


def ongoing_notification(now: dt.datetime, result: dict) -> dict:
    """One status update per successful check while delayed or closed."""
    if result["status"] == "closed":
        subject = f"{TARGET_NAME} is STILL not taking orders ({now:%H:%M})"
        summary = "They are still not taking orders."
    else:
        subject = f"{TARGET_NAME} is STILL warning of long delays ({now:%H:%M})"
        summary = "The long-delays warning remains. An ordering control is shown; delivery availability is not verified."
    body = (f"Check at {now:%H:%M} on {now:%A %d %B %Y} ({TIMEZONE}).\n\n"
            f"{summary}\n\nNotice:\n{notice_block(result)}\n\n"
            f"Venues checked:\n{venue_lines(result)}\n\nDirectory: {DIRECTORY_URL}\n")
    return {"subject": subject, "body": body, "attach_screenshot": False}


def unconfirmed_update_notification(now: dt.datetime, state: dict, result: dict,
                                    streak: int) -> dict:
    """Per-check update when a check reads 'taking orders' during an incident
    but has not yet been confirmed by the following check. The headline asserts
    nothing about the present: it names the last confirmed status and when."""
    confirmed_label = {"closed": "not taking orders",
                       "delays": "showing a long-delays warning"}.get(
                           state.get("status"), state.get("status"))
    confirmed_at = state.get("last_confirmed_at") or state.get("since")
    confirmed_time = dt.datetime.fromisoformat(confirmed_at).strftime("%H:%M") if confirmed_at else "?"
    subject = (f"{TARGET_NAME}: {now:%H:%M} check unconfirmed - last confirmed "
               f"{confirmed_label} at {confirmed_time}")
    seen = VENUE_LABELS.get(result["status"], result["status"])
    body = (f"Check at {now:%H:%M} on {now:%A %d %B %Y} ({TIMEZONE}).\n\n"
            f"This check read as: {seen}. That is an inference from the ordering page, and one "
            f"check is not enough to end an incident ({streak} of {RECOVERY_CONFIRMATIONS} "
            f"consecutive agreeing checks so far).\n\n"
            f"Last confirmed status: {confirmed_label}, confirmed by the {confirmed_time} check.\n\n"
            f"Notice seen:\n{notice_block(result)}\n\n"
            f"Venues checked:\n{venue_lines(result)}\n\nDirectory: {DIRECTORY_URL}\n")
    return {"subject": subject, "body": body, "attach_screenshot": False}


def break_recovery_streak(state: dict, reason: str) -> None:
    """Consecutive means consecutive: anything that is not an agreeing, readable
    check in the normal cadence starts the recovery count again."""
    if state.get("recovery_streak"):
        log(f"Recovery count reset ({reason}).")
    state["recovery_streak"] = 0
    state.pop("recovery_candidate_at", None)


def recovery_streak_after(state: dict, now: dt.datetime) -> int:
    """The streak this check continues, or 0 if the last candidate is too old."""
    last = state.get("recovery_candidate_at")
    if not last or not state.get("recovery_streak"):
        return 0
    age = (now - dt.datetime.fromisoformat(last)).total_seconds() / 60
    if age > RECOVERY_MAX_GAP_MINUTES:
        break_recovery_streak(state, f"{age:.0f} min since the previous candidate")
        return 0
    return state["recovery_streak"]


def import_recovery(state: dict) -> None:
    """Queue a reviewed historical correction once, without falsifying current status."""
    if not RECOVERY_FILE.exists():
        return
    correction = json.loads(RECOVERY_FILE.read_text(encoding="utf-8"))
    key = correction["id"]
    applied = state.setdefault("applied_recoveries", [])
    if key in applied:
        return
    item = {"id": key, "subject": correction["subject"], "body": correction["body"],
            "attach_screenshot": False, "historical": True}
    if not all(isinstance(item[k], str) and item[k].strip() for k in ("id", "subject", "body")):
        raise ValueError("Invalid reviewed notification recovery")
    state.setdefault("pending_notifications", []).append(item)
    reconciliation = correction.get("state_reconciliation", {})
    if (reconciliation and state.get("status") == reconciliation.get("expected_status")
            and state.get("since") == reconciliation.get("expected_since")):
        state["since"] = reconciliation["corrected_since"]
    applied.append(key)
    save_state(state)


def flush_notifications(state: dict) -> bool:
    """Deliver in observation order, retaining failures for the next check."""
    pending = state.setdefault("pending_notifications", [])
    while pending:
        item = pending[0]
        try:
            send_email(item["subject"], item["body"], item.get("attach_screenshot", False))
        except Exception as exc:
            # Never include transport exceptions that could contain credentials.
            log(f"Notification delivery not confirmed ({type(exc).__name__}); retained for retry.")
            save_state(state)
            return False
        pending.pop(0)
        receipts = state.setdefault("notification_receipts", [])
        receipts.append({"id": item["id"], "subject": item["subject"],
                         "delivered_at": dt.datetime.now(ZoneInfo(TIMEZONE)).isoformat()})
        state["notification_receipts"] = receipts[-100:]
        save_state(state)
    return True


# ---------------------------------------------------------------------------
# One check
# ---------------------------------------------------------------------------
def run_check(dry_run: bool = False) -> int:
    now = dt.datetime.now(ZoneInfo(TIMEZONE))
    state = load_state()
    if not dry_run:
        import_recovery(state)
    if not dry_run and not in_trading_hours(now):
        log(f"{now:%Y-%m-%d %H:%M %Z} | outside alert window; skipping")
        state["last_checked_date"] = now.date().isoformat()
        break_recovery_streak(state, "outside alert window")
        save_state(state)
        return 0 if flush_notifications(state) else 3

    try:
        result = check_platform()
    except Exception as exc:
        log(f"ERROR during check: {exc}")
        if not dry_run:
            break_recovery_streak(state, "check failed")
            save_state(state)
            flush_notifications(state)
        return 2

    status = result["status"]
    trading = in_trading_hours(now)
    log(f"{now:%Y-%m-%d %H:%M %Z} | status={status} | previous={state.get('status')} "
        f"| trading hours={trading}")

    if status == "no_open_venues":
        log(result["detail"])
        if not dry_run:
            break_recovery_streak(state, "no open venues")
            save_state(state)
        if not trading:
            log("Outside trading hours: expected overnight state; no availability inference.")
            return 0
        log("During trading hours: cannot establish platform availability.")
        if not dry_run:
            flush_notifications(state)
        return 2

    if status == "unknown":
        log("Could not tell whether they are open. " + result.get("detail", ""))
        log("Page text began: " + " ".join(result.get("text", "").split())[:400])
        if not dry_run:
            break_recovery_streak(state, "unreadable check")
            save_state(state)
            flush_notifications(state)
        return 2

    if dry_run:
        log("Dry run: not emailing or saving state.")
        if result.get("notice"):
            log("Notice found:\n" + notice_block(result))
        return 0

    # Persist the observation and its notification together before contacting Slack.
    # A failed delivery stays queued even if the site changes again next time.
    previous = dict(state)
    notification = None
    # closed -> open, closed -> delays and delays -> open all end an incident;
    # they take effect only once RECOVERY_CONFIRMATIONS consecutive checks agree.
    ends_incident = (previous.get("status") in ("closed", "delays")
                     and status in ("open", "delays") and status != previous.get("status"))
    if ends_incident and recovery_streak_after(state, now) + 1 < RECOVERY_CONFIRMATIONS:
        state["recovery_streak"] = state.get("recovery_streak", 0) + 1
        state["recovery_candidate_at"] = now.isoformat()
        log(f"Read as {status} during a {previous.get('status')} incident: possible recovery "
            f"{state['recovery_streak']}/{RECOVERY_CONFIRMATIONS}; not announced yet.")
        if trading and not state.get("pending_notifications"):
            notification = unconfirmed_update_notification(now, state, result, state["recovery_streak"])
        status = previous.get("status")           # the incident stands for now
    elif status != previous.get("status"):
        break_recovery_streak(state, "status changed")
        state.update(status=status, since=now.isoformat(), alerted=False,
                     last_confirmed_at=now.isoformat())
        if status == "closed" and trading:
            notification = closed_notification(now, result)
            state["alerted"] = True
        elif previous.get("status") == "closed":
            if ALERT_ON_REOPEN and previous.get("alerted"):
                notification = reopen_notification(now, previous.get("since"), result)
                state["alerted"] = status == "delays" and ALERT_ON_DELAYS
            elif status == "delays" and ALERT_ON_DELAYS and trading:
                notification = delays_notification(now, result)
                state["alerted"] = True
        elif status == "delays" and ALERT_ON_DELAYS and trading:
            notification = delays_notification(now, result)
            state["alerted"] = True
        elif status == "open" and previous.get("status") == "delays":
            if ALERT_ON_DELAYS and previous.get("alerted"):
                notification = delays_cleared_notification(now, previous.get("since"))
    elif trading and (status == "closed" or (status == "delays" and ALERT_ON_DELAYS)):
        break_recovery_streak(state, "status re-confirmed")
        state["last_confirmed_at"] = now.isoformat()
        # Pending transitions are delivered first; don't pile repeated reminders on them.
        if not state.get("pending_notifications"):
            notification = ongoing_notification(now, result)
            state["alerted"] = True
    else:
        break_recovery_streak(state, "status re-confirmed")
        state["last_confirmed_at"] = now.isoformat()
    if notification:
        notification.update(id=now.isoformat() + ":" + status,
                            observed_at=now.isoformat())
        # A later retry could otherwise attach a screenshot from a different check.
        notification["attach_screenshot"] = False
        state.setdefault("pending_notifications", []).append(notification)

    state.update(notice=result.get("notice", "") if status == result["status"] else state.get("notice", ""),
                 last_observed_at=now.isoformat(), last_result=result["status"],
                 last_checked_date=now.date().isoformat(),
                 last_venues=[{"name": v.get("name"), "status": v["status"],
                               "notice": v.get("notice", ""),
                               "notice_transcript": v.get("notice_transcript", []),
                               "unresolved_notice": v.get("unresolved_notice", False),
                               "order_button_after_notices": v.get("order_button_after_notices", False)}
                              for v in result.get("venues", [])])
    save_state(state)
    return 0 if flush_notifications(state) else 3


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--every", type=int, metavar="SECONDS",
                        help="keep running and re-check every SECONDS")
    parser.add_argument("--test-email", "--test-notification", action="store_true",
                        help="send a test email and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="run one check and print the result without emailing or saving state")
    args = parser.parse_args()

    if args.test_email:
        now = dt.datetime.now(ZoneInfo(TIMEZONE))
        send_email(
            "Competitor watch: test notification",
            f"Notifications are working.\n\nWatching: {TARGET_NAME}\n{DIRECTORY_URL}\n\n"
            f"Alerting on closures between {TRADING_HOURS} ({TIMEZONE}).\n"
            f"Sent {now:%Y-%m-%d %H:%M %Z}.\n",
            attach_screenshot=False,
        )
        return 0

    if not args.every:
        return run_check(dry_run=args.dry_run)

    log(f"Checking every {args.every} seconds. Press Ctrl+C to stop.")
    while True:
        try:
            run_check(dry_run=args.dry_run)
        except Exception as exc:               # keep the loop alive whatever happens
            log(f"ERROR: {exc}")
        time.sleep(args.every)


if __name__ == "__main__":
    sys.exit(main())
