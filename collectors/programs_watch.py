"""Watch the official page of each co-op / scholarship program for "applications open".

Opening months in the research files are the usual window only. Every few days this
reads each program's official page (robots.txt respected, polite pacing) and records
whether it currently says applications are open. When a page changes from "not open"
to "open", users who asked for that kind of opportunity get one Telegram alert.
The first look at a page only sets the baseline (no alert), so a career page that
always shows "Apply now" never alerts.
"""

import logging
import re
from datetime import datetime, timedelta

from backend import normalize, notifier, programs, storage

from . import base

log = logging.getLogger("rizq.programs")

WATCH_EVERY_DAYS = 3
MAX_PER_RUN = 30
OPEN_RE = re.compile(
    r"التقديم متاح|باب التقديم مفتوح|فتح باب التقديم|بدء التقديم|بدا التقديم|قدم الان|سجل الان|التسجيل متاح|"
    r"التسجيل مفتوح|استقبال الطلبات|apply now|applications? (?:are |is )?(?:now )?open|now accepting|"
    r"accepting applications|registration (?:is )?(?:now )?open", re.I)
CLOSED_RE = re.compile(
    r"التقديم مغلق|انتهى التقديم|انتهاء التقديم|اغلاق باب التقديم|اغلق باب التقديم|تم اغلاق التقديم|"
    r"applications? (?:are |is )?(?:now )?closed|no longer accepting|registration (?:is )?closed", re.I)
SCRIPT_RE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.I | re.S)


def page_signal(html_text):
    """True / False: does the page say applications are open (and not closed)?"""
    text = normalize.norm(normalize.TAG_RE.sub(" ", SCRIPT_RE.sub(" ", html_text or "")))
    return bool(OPEN_RE.search(text)) and not CLOSED_RE.search(text)


def collect(source):
    now = datetime.now()
    due_before = (now - timedelta(days=WATCH_EVERY_DAYS)).isoformat(timespec="seconds")
    state = storage.read("programs_watch")
    todo = [p for p in programs.all_programs()
            if (p["apply_url"] or p["source_url"])
            and (state.get(p["kind"] + ":" + p["id"]) or {}).get("checked_at", "") < due_before]
    todo.sort(key=lambda p: (state.get(p["kind"] + ":" + p["id"]) or {}).get("checked_at", ""))
    checked, opened, errors = 0, [], 0
    for p in todo[:MAX_PER_RUN]:
        key = p["kind"] + ":" + p["id"]
        url = p["apply_url"] or p["source_url"]
        try:
            signal = page_signal(base.fetch(url))
            err = ""
        except (base.Disallowed, base.Blocked, base.FetchError) as e:
            signal, err = None, str(e)[:200]
            errors += 1
        checked += 1
        with storage.edit("programs_watch") as st:
            entry = st.setdefault(key, {})
            entry["checked_at"] = now.isoformat(timespec="seconds")
            entry["error"] = err
            if signal is None:
                continue
            first_look = "open_signal" not in entry
            was_open = bool(entry.get("open_signal"))
            entry["open_signal"] = signal
            if signal and not was_open:
                entry["open_seen_at"] = now.isoformat(timespec="seconds")
                if not first_look:
                    opened.append(p)
            if not signal:
                entry.pop("open_seen_at", None)
    for p in opened:
        try:
            notifier.notify_program_open(p)
        except Exception:
            log.exception("program alert failed for %s", p["id"])
    source["_note"] = f"فحص {checked} صفحة برنامج، {len(opened)} فتحت التقديم، {errors} تعذّر فحصها"
    return []
