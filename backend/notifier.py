"""Send newly collected matching jobs to linked users on Telegram."""

import html
import logging
import time
from datetime import date

from . import config, matching, programs, storage, telegram_bot

log = logging.getLogger("rizq.notify")
MAX_PER_RUN = 5
MAX_PER_DAY = 20   # fast lane runs hourly: keep alerts useful, never a flood
SENT_KEEP = 500


def notify_new_jobs(new_jobs, sent_per_user=None):
    """Returns number of messages sent.

    sent_per_user ({user_id: jobs sent}) is shared across the calls of one collection
    run, so a user gets at most MAX_PER_RUN jobs per run even though every source
    alerts as soon as it is stored.
    """
    sent_per_user = {} if sent_per_user is None else sent_per_user
    if not telegram_bot.enabled() or not new_jobs:
        return 0
    candidates = [j for j in new_jobs if not j.get("is_demo") and not j.get("scam_risk")]
    if not candidates:
        return 0
    sent_total = 0
    linked = [u for u in storage.read("users") if u.get("telegram_chat_id")]
    for user in linked:
        today = date.today().isoformat()
        daily = storage.read("alerts_daily")
        used_today = daily.get("counts", {}).get(user["id"], 0) if daily.get("date") == today else 0
        room = min(MAX_PER_RUN - sent_per_user.get(user["id"], 0), MAX_PER_DAY - used_today)
        if room <= 0:
            continue
        already = set(storage.read("sent").get(user["id"], []))
        top = [(s, j) for s, j in matching.top_matches(candidates, user, limit=MAX_PER_RUN * 2)
               if j["id"] not in already]
        # Co-op / scholarship posts go to users who asked for them, whatever their specialty
        wanted = set(user.get("interests") or [])
        if wanted:
            have = {j["id"] for _, j in top}
            top = [(None, j) for j in candidates
                   if j["id"] not in already and j["id"] not in have and programs.kind_of(j) in wanted] + top
        top = top[:room]
        if not top:
            continue
        header = "وظائف جديدة تناسبك 🔔" if len(top) > 1 else "وظيفة جديدة تناسبك 🔔"
        text = header + "\n\n" + "\n\n".join(telegram_bot.format_job(j, s) for s, j in top)
        try:
            telegram_bot.send(user["telegram_chat_id"], text)
        except Exception as e:  # any failure for one user must not stop the others
            log.warning("send to %s failed: %r", user["id"], e)
            continue
        sent_total += 1
        sent_per_user[user["id"]] = sent_per_user.get(user["id"], 0) + len(top)
        with storage.edit("alerts_daily") as d:
            if d.get("date") != today:
                d.clear()
                d["date"], d["counts"] = today, {}
            d["counts"][user["id"]] = d["counts"].get(user["id"], 0) + len(top)
        with storage.edit("sent") as sent:
            lst = sent.setdefault(user["id"], [])
            lst.extend(j["id"] for _, j in top)
            del lst[:-SENT_KEEP]
        time.sleep(1.1)  # stay under Telegram's per-chat/global rate limits
    return sent_total


def _room_today(user_id):
    daily = storage.read("alerts_daily")
    used = daily.get("counts", {}).get(user_id, 0) if daily.get("date") == date.today().isoformat() else 0
    return MAX_PER_DAY - used


def notify_program_open(program):
    """One alert per interested user when a program's official page starts saying "applications open"."""
    if not telegram_bot.enabled():
        return 0
    esc = telegram_bot.esc
    label = "التدريب التعاوني" if program["kind"] == "coop" else "الابتعاث"
    url = program.get("apply_url") or program.get("source_url") or ""
    text = (f"🔔 تحديث في {label}\n\n<b>{esc(program['name'])}</b>\n🏢 {esc(program.get('provider'))}\n"
            "الصفحة الرسمية للبرنامج تقول الآن إن التقديم متاح. تأكد من التفاصيل والشروط في الرابط:")
    if url.startswith(("https://", "http://")):
        text += f'\n<a href="{html.escape(url, quote=True)}">الصفحة الرسمية</a>'
    sent = 0
    for user in storage.read("users"):
        if not user.get("telegram_chat_id") or program["kind"] not in (user.get("interests") or []):
            continue
        if _room_today(user["id"]) <= 0:
            continue
        try:
            telegram_bot.send(user["telegram_chat_id"], text)
        except Exception as e:
            log.warning("program alert to %s failed: %r", user["id"], e)
            continue
        sent += 1
        today = date.today().isoformat()
        with storage.edit("alerts_daily") as d:
            if d.get("date") != today:
                d.clear()
                d["date"], d["counts"] = today, {}
            d["counts"][user["id"]] = d["counts"].get(user["id"], 0) + 1
        time.sleep(1.1)
    return sent
