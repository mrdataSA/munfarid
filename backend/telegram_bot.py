"""Telegram Bot API (long polling) + sending helpers. stdlib only.

The conversation itself lives in bot.py. handle_message() below is the older
website-link flow (/start <token> from the site's "اربط تيليجرام" button).
"""

import html
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from . import catalog, config, matching, storage, users

log = logging.getLogger("rizq.bot")
MAX_PENDING_PER_CHAT = 5   # extra updates from one busy chat are dropped


class TelegramError(Exception):
    def __init__(self, code, description):
        super().__init__(f"{code}: {description}")
        self.code = code
        self.description = description


def enabled():
    return bool(config.TELEGRAM_BOT_TOKEN)


def call(method, http_timeout=30, **params):
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/{method}"
    req = urllib.request.Request(url, data=json.dumps(params).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=http_timeout) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            data = json.load(e)
        except (ValueError, OSError):
            raise TelegramError(e.code, "HTTP error")
    if not data.get("ok"):
        err = TelegramError(data.get("error_code", 0), data.get("description", ""))
        err.parameters = data.get("parameters") or {}
        err.retry_after = err.parameters.get("retry_after")
        raise err
    return data.get("result")


def send(chat_id, text, reply_markup=None, max_wait=3):
    """Send an HTML message; stops alerts for chats that blocked the bot.

    On a flood limit (429) it waits only up to max_wait seconds: replies to a person keep
    it short so one busy chat can't hold a worker; the scheduler passes a longer wait.
    """
    params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if reply_markup:
        params["reply_markup"] = reply_markup
    for attempt in range(3):
        try:
            return call("sendMessage", **params)
        except TelegramError as e:
            if e.code == 429 and attempt < 2 and int(getattr(e, "retry_after", 5) or 5) <= max_wait:
                time.sleep(int(getattr(e, "retry_after", 5) or 5))
                continue
            if e.code == 403:  # user blocked the bot / bot removed from the group
                users.unlink_chat(chat_id)
                from . import bot  # late import: bot imports this module
                bot.on_blocked(chat_id)
            raise
        except (urllib.error.URLError, OSError) as e:
            if attempt == 2:
                raise TelegramError(0, str(e))
            time.sleep(2)


def edit_message(chat_id, message_id, text, reply_markup=None):
    """Replace a menu message in place. "not modified" is fine; a message that can no longer
    be edited (old / deleted) gets the menu as a new message instead."""
    params = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True}
    if reply_markup:
        params["reply_markup"] = reply_markup
    try:
        return call("editMessageText", **params)
    except TelegramError as e:
        if e.code != 400:
            raise
        if "not modified" in (e.description or ""):
            return None
        return send(chat_id, text, reply_markup=reply_markup)


def quiet(method, **params):
    """Best-effort call (typing indicator, answering a button, deleting a command)."""
    try:
        return call(method, **params)
    except (TelegramError, urllib.error.URLError, OSError, ValueError):
        return None


def download_file(file_id, max_bytes):
    """Bytes of a file the user sent (None if too large or unavailable)."""
    info = call("getFile", file_id=file_id)
    if not info or not info.get("file_path") or (info.get("file_size") or 0) > max_bytes:
        return None
    url = f"https://api.telegram.org/file/bot{config.TELEGRAM_BOT_TOKEN}/{info['file_path']}"
    with urllib.request.urlopen(url, timeout=30) as r:
        data = r.read(max_bytes + 1)
    return data if len(data) <= max_bytes else None


def esc(s):
    return html.escape(str(s or ""), quote=False)


def format_job(job, score=None):
    # Honest relay: always point to the original posting (its apply link first)
    url = job.get("apply_url") or job.get("url") or ""
    lines = [f"<b>{esc(job.get('title'))}</b>",
             f"🏢 {esc(job.get('company'))} · 📍 {esc(job.get('city'))}",
             f"📄 {esc(job.get('contract_type'))}" + (f" · توافق {score}%" if score is not None else "")]
    if job.get("nationality"):
        lines.append(f"🇸🇦 {esc(job['nationality'])}")
    if url.startswith(("https://", "http://")):
        lines.append(f'<a href="{html.escape(url, quote=True)}">اذهب للإعلان الأصلي ({esc(job.get("source"))})</a>')
    elif url.startswith("mailto:"):  # direct posts; Telegram links don't support mailto
        lines.append(f"📧 التقديم بالبريد مباشرة إلى الجهة: {esc(url[7:])}")
    return "\n".join(lines)


def _matches_text(user, limit=5):
    jobs, is_demo = catalog.all_jobs(include_demo=False)
    top = matching.top_matches(jobs, user, limit=limit, min_score=40)
    if not top:
        return "لا توجد حالياً وظائف قريبة من تخصصك. سنرسل لك فور وصول وظائف مناسبة."
    if len(top) == 1:
        parts = ["أنسب وظيفة لك الآن:"]
    elif len(top) == 2:
        parts = ["أنسب وظيفتين لك الآن:"]
    else:
        parts = [f"أنسب {len(top)} وظائف لك الآن:"]
    parts += [format_job(j, s) for s, j in top]
    return "\n\n".join(parts)


HELP = ("أهلاً بك في رزق 👋\n"
        "ننقل لك هنا أخبار الوظائف المناسبة لتخصصك ومهاراتك، مع رابط الإعلان الأصلي لكل وظيفة.\n\n"
        "/jobs أنسب الوظائف لك الآن\n"
        "/stop إيقاف التنبيهات\n"
        "/help المساعدة")


def handle_message(msg):
    chat = msg.get("chat") or {}
    if chat.get("type") != "private":
        return
    chat_id = chat.get("id")
    text = (msg.get("text") or "").strip()
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower()

    if cmd == "/start":
        arg = arg.strip()
        if arg:
            user = users.link_chat(arg, chat_id)
            if user:
                send(chat_id, f"تم ربط حسابك يا {esc(user['name'])} ✅\n"
                              "ستصلك الوظائف المناسبة لتخصصك ({}) هنا تلقائياً.\n\n".format(esc(user["specialty"]))
                     + _matches_text(user, limit=3))
            else:
                send(chat_id, "رابط الربط غير صالح أو منتهي. افتح صفحة «تنبيهاتي» في موقع رزق واضغط «اربط تيليجرام» من جديد.")
        else:
            send(chat_id, HELP + "\n\nلتفعيل التنبيهات: اشترك من موقع رزق (اكتب تخصصك ومهاراتك) ثم اضغط «اربط تيليجرام».")
    elif cmd == "/jobs":
        user = users.find_by_chat(chat_id)
        send(chat_id, _matches_text(user) if user else "لم تربط تيليجرام بعد. اشترك في التنبيهات من موقع رزق ثم اضغط «اربط تيليجرام».")
    elif cmd == "/stop":
        users.unlink_chat(chat_id)
        send(chat_id, "تم إيقاف التنبيهات. يمكنك تفعيلها مرة أخرى من موقع رزق في أي وقت.")
    else:
        send(chat_id, HELP)


class BotPoller(threading.Thread):
    """Long-polls getUpdates; stops on 409 (another instance is polling)."""

    def __init__(self):
        super().__init__(daemon=True, name="rizq-bot")
        self.stop_event = threading.Event()
        self.status = "starting"
        # A slow reply (AI reading a CV) must not hold up everyone else, and one chat's updates
        # must run in order: each chat has its own queue, drained by at most ONE pool task at a
        # time, so a person sending many messages can occupy one worker, never all of them.
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="rizq-bot-h")
        self.queues = {}          # chat_id -> deque of pending updates
        self.qlock = threading.Lock()

    def _submit(self, upd):
        from . import bot  # late import: bot imports this module
        chat_id = bot.chat_of(upd)
        with self.qlock:
            q = self.queues.get(chat_id)
            if q is not None:  # a task is already draining this chat
                if len(q) < MAX_PENDING_PER_CHAT:
                    q.append(upd)
                return
            self.queues[chat_id] = deque([upd])
        self.pool.submit(self._drain, chat_id)

    def _drain(self, chat_id):
        from . import bot
        while True:
            with self.qlock:
                q = self.queues.get(chat_id)
                if not q:
                    self.queues.pop(chat_id, None)
                    return
                upd = q.popleft()
            try:
                bot.handle_update(upd)
            except Exception as e:  # one bad update must not kill the bot
                log.warning("bot update failed: %r", e)

    def run(self):
        offset = storage.read("bot_state").get("offset", 0)
        try:
            me = call("getMe")
            log.info("bot @%s polling", me.get("username"))
            if not config.TELEGRAM_BOT_USERNAME and me.get("username"):
                # needed to recognise "/feed@thisbot" in groups and for links to the bot
                config.TELEGRAM_BOT_USERNAME = me["username"]
            self.status = "running"
        except (TelegramError, urllib.error.URLError, OSError) as e:
            self.status = f"error: {e}"
            log.error("bot getMe failed: %s", e)
            return
        while not self.stop_event.is_set():
            try:
                # Telegram long-poll timeout (50s) < HTTP timeout (65s)
                updates = call("getUpdates", http_timeout=65, offset=offset, timeout=50,
                               allowed_updates=["message", "callback_query", "channel_post", "my_chat_member"])
            except TelegramError as e:
                if e.code == 409:
                    self.status = "stopped: another bot instance is running"
                    log.error(self.status)
                    return
                if e.code == 401:
                    self.status = "stopped: bot token rejected (401). Check TELEGRAM_BOT_TOKEN"
                    log.error(self.status)
                    return
                self.status = f"retrying: {e}"
                time.sleep(10)
                continue
            except (urllib.error.URLError, OSError, ValueError) as e:
                self.status = f"retrying: {e}"
                time.sleep(10)
                continue
            self.status = "running"
            for upd in updates or []:
                offset = upd["update_id"] + 1
                self._submit(upd)
            if updates:
                with storage.edit("bot_state") as st:
                    st["offset"] = offset
            time.sleep(1)
