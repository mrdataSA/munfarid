"""RIZQ local server (Python stdlib only).

Serves the site (allowlisted static files only) and the JSON API, and runs
background workers: scheduled job collection and the Telegram bot.
Binds to 127.0.0.1 and rejects foreign Host/Origin headers.

Usage:  python server.py [--port 8080] [--no-open] [--no-workers]
"""

import hmac
import json
import logging
import logging.handlers
import os
import re
import secrets
import sys
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from urllib.parse import parse_qs

from backend import (ai, analytics, auth, bot, catalog, config, normalize, notifier, owner, posts, programs, storage,
                     telegram_bot, users, xlsx)

ROOT = config.ROOT
log = logging.getLogger("rizq")

MAX_BODY = 3 * 1024 * 1024
# Defense in depth: no inline scripts, no third-party scripts, no framing
CSP = ("default-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; "
       "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


def _nc(path):
    return os.path.normcase(os.path.realpath(path))


PUBLIC_FILES = {_nc(os.path.join(ROOT, "index.html"))}
PUBLIC_DIRS = [_nc(os.path.join(ROOT, d)) for d in ("css", "js", "assets")]
ROOT_NC = _nc(ROOT)

# No ads anywhere: an old "banner" value in settings.json is ignored.
DEFAULT_SETTINGS = {
    "support": {
        "enabled": True,
        "text": "رزق مبادرة فردية مجانية، وسيبقى مجانياً للجميع. إن نفعك وأحببت أن تساعد في تكاليف تشغيله وتطويره، فدعمك محل تقدير، وهو اختياري تماماً.",
        "links": [],
    },
    "hidden_job_ids": [],
}


def get_settings():
    s = storage.read("settings")
    merged = json.loads(json.dumps(DEFAULT_SETTINGS))
    for k, v in s.items():
        if k == "banner":
            continue
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k].update(v)
        else:
            merged[k] = v
    return merged


def public_settings():
    s = get_settings()
    return {
        "support": {"enabled": s["support"].get("enabled", True), "text": s["support"].get("text", ""),
                    "links": s["support"].get("links", [])},
        "telegram_bot": bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_BOT_USERNAME),
    }


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------- workers

class Workers:
    bot = None
    bot_scheduler = None
    scheduler = None


def _runner():
    """The collectors run on this machine only locally; on Vercel they run on GitHub Actions."""
    if config.ON_VERCEL:
        return None
    from collectors import runner
    return runner


def load_sources():
    with open(os.path.join(ROOT, "collectors", "sources.json"), "r", encoding="utf-8") as f:
        return json.load(f)


def start_collection_async():
    runner = _runner()
    if runner is None or runner.is_running():
        return False

    def job():
        try:
            runner.run_all()
        except Exception:
            log.exception("collection failed")
    threading.Thread(target=job, daemon=True, name="rizq-collect").start()
    return True


def _age_seconds(stamp):
    try:
        return (datetime.now().astimezone() - datetime.fromisoformat(stamp)).total_seconds()
    except (TypeError, ValueError):
        return float("inf")


def scheduler_loop(hours, fast_minutes=0):
    """Collect shortly after start (if stale) and then every N hours.

    Between full runs, a light "fast lane" (sources marked "fast" in sources.json)
    runs every fast_minutes so new matching jobs reach users quickly.
    """
    time.sleep(20)
    while True:
        st = storage.read("sources_status")
        last = max(st.get("_last_attempt", ""), st.get("_last_run", {}).get("finished_at", ""))
        stale = not last or _age_seconds(last) > hours * 3600
        last_fast = max(st.get("_last_fast_attempt", ""), st.get("_last_fast_run", {}).get("finished_at", ""),
                        last)  # a full run also covers the fast sources
        fast_due = fast_minutes > 0 and (not last_fast or _age_seconds(last_fast) > fast_minutes * 60)
        runner = _runner()
        if (stale or fast_due) and not runner.is_running():
            log.info("scheduled %s collection starting", "full" if stale else "fast")
            try:
                runner.run_all(fast=not stale)
            except Exception:
                log.exception("scheduled collection failed")
        time.sleep(300)  # re-check every 5 minutes


def start_workers():
    if config.AUTO_COLLECT_HOURS > 0:
        Workers.scheduler = threading.Thread(target=scheduler_loop,
                                             args=(config.AUTO_COLLECT_HOURS, config.FAST_COLLECT_MINUTES),
                                             daemon=True, name="rizq-scheduler")
        Workers.scheduler.start()
    if telegram_bot.enabled():
        Workers.bot = telegram_bot.BotPoller()
        Workers.bot.start()
        Workers.bot_scheduler = bot.Scheduler()   # daily new-jobs message + group/channel feeds
        Workers.bot_scheduler.start()


# ---------------------------------------------------------------- routing

ROUTES = []


def route(method, pattern):
    rx = re.compile("^" + pattern + "$")

    def deco(fn):
        ROUTES.append((method, rx, fn))
        return fn
    return deco


class RawFile:
    """A route's answer that is a file download, not JSON."""

    def __init__(self, body, ctype, filename):
        self.body, self.ctype, self.filename = body, ctype, filename


# The old public job site's API: off while the site is the owner's private dashboard (PUBLIC_SITE=0)
PUBLIC_API = ("/api/jobs", "/api/programs", "/api/settings", "/api/users", "/api/reports", "/api/posts")
GO_RE = re.compile(r"^/j/([A-Za-z0-9_-]{1,40})(?:\.([ug][0-9a-f]{11}))?/?$")


def ok(payload=None, cookies=()):
    out = {"ok": True}
    out.update(payload or {})
    return 200, out, cookies


class Handler(SimpleHTTPRequestHandler):
    server_version = "Rizq/1.0"
    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".js": "text/javascript; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".html": "text/html; charset=utf-8",
        ".svg": "image/svg+xml",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", CSP)
        super().end_headers()

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    # --- guards ---
    def _allowed_hosts(self):
        if config.ON_VERCEL:
            return set(config.PUBLIC_HOSTS)
        port = self.server.server_address[1]
        return {f"localhost:{port}", f"127.0.0.1:{port}"} | set(config.PUBLIC_HOSTS)

    def _host_ok(self):
        return (self.headers.get("Host") or "").lower() in self._allowed_hosts()

    def origin_ok(self, required=False):
        origin = self.headers.get("Origin")
        if not origin:
            return not required
        scheme = "https://" if config.ON_VERCEL else "http://"
        return origin.lower() in {scheme + h for h in self._allowed_hosts()}

    def client_ip(self):
        """The visitor's address. On Vercel every request comes through its proxy, which puts
        the real address first in X-Forwarded-For (and replaces any value the visitor sent)."""
        if config.ON_VERCEL:
            fwd = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            if fwd:
                return fwd
        return self.client_address[0]

    def _is_public(self):
        real = _nc(self.translate_path(self.path))
        if real == ROOT_NC or real in PUBLIC_FILES:
            return True
        return any(real != d and os.path.commonpath([real, d]) == d for d in PUBLIC_DIRS)

    def list_directory(self, path):
        self.send_error(404)
        return None

    # --- helpers ---
    def cookie(self, name):
        c = SimpleCookie()
        try:
            c.load(self.headers.get("Cookie") or "")
        except Exception:
            return ""
        return c[name].value if name in c else ""

    def _send_json(self, status, obj, cookies=()):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for c in cookies:
            self.send_header("Set-Cookie", c)
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, f):
        from urllib.parse import quote
        self.send_response(200)
        self.send_header("Content-Type", f.ctype)
        self.send_header("Content-Length", str(len(f.body)))
        self.send_header("Content-Disposition", f"attachment; filename=\"report.xlsx\"; filename*=UTF-8''{quote(f.filename)}")
        self.end_headers()
        self.wfile.write(f.body)

    def query(self):
        q = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "", keep_blank_values=False)
        return {k: v for k, v in q.items()}

    def _go(self, path, count=True):
        """/j/<job>.<person>: count the click for the owner's dashboard, then go to the original link
        (never a relay page: a job whose original link is not known yet goes to the bot)."""
        m = GO_RE.match(path)
        target, job = "", None
        if m:
            job = catalog.find(m.group(1))
            if job is None and config.DATABASE_URL:
                job = analytics.job_row(m.group(1))
            target = normalize.original_link(job or {})
        if not target.startswith(("https://", "http://")) or any(c in target for c in "\r\n"):
            target = f"https://t.me/{config.TELEGRAM_BOT_USERNAME}" if config.TELEGRAM_BOT_USERNAME else ""
            if not target:
                self.send_error(404)
                return
        elif count:
            analytics.log_event("click", m.group(2), job=m.group(1), title=((job or {}).get("title") or "")[:120],
                                source=(job or {}).get("source") or "", city=(job or {}).get("city") or "")
        self.send_response(302)
        self.send_header("Location", target)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def body(self):
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            raise ApiError(415, "نوع البيانات غير مدعوم.")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ApiError(413, "حجم البيانات كبير جداً.")
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(400, "بيانات غير صالحة.")
        if not isinstance(data, dict):
            raise ApiError(400, "بيانات غير صالحة.")
        return data

    def admin(self):
        if not config.ADMIN_PASSWORD and not owner.owner_chat():   # Telegram login needs no password
            raise ApiError(403, "لوحة الإدارة غير مفعّلة. أضف كلمة مرور الإدارة في إعدادات الخادم (env).")
        if not auth.get_session(self.cookie(auth.COOKIE_ADMIN), "admin"):
            raise ApiError(401, "سجّل الدخول أولاً.")
        return True

    @staticmethod
    def session_cookie(name, token, max_age):
        secure = "; Secure" if config.ON_VERCEL else ""
        return f"{name}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}{secure}"

    # --- dispatch ---
    def _dispatch(self, method):
        if not self._host_ok():
            self.send_error(403)
            return
        path = self.path.split("?", 1)[0]
        if path.startswith("/j/") and method in ("GET", "HEAD"):
            return self._go(path, count=method == "GET")   # link previews (HEAD) are not clicks
        if not config.PUBLIC_SITE and path.startswith(PUBLIC_API):
            self._send_json(404, {"ok": False, "error": "غير موجود."})
            return
        if not path.startswith("/api/"):
            if method in ("GET", "HEAD"):
                if not self._is_public():
                    self.send_error(404)
                    return
                return super().do_GET() if method == "GET" else super().do_HEAD()
            self.send_error(405)
            return
        if method == "POST" and not self.origin_ok():
            self._send_json(403, {"ok": False, "error": "طلب غير مسموح."})
            return
        for m, rx, fn in ROUTES:
            match = rx.match(path)
            if m == method and match:
                try:
                    res = fn(self, *match.groups())
                    if isinstance(res, RawFile):
                        self._send_file(res)
                        return
                    status, payload, cookies = res
                    self._send_json(status, payload, cookies)
                except ApiError as e:
                    self._send_json(e.status, {"ok": False, "error": e.message})
                except (users.ValidationError, posts.ValidationError) as e:
                    self._send_json(400, {"ok": False, "error": str(e)})
                except posts.RateLimited as e:
                    self._send_json(429, {"ok": False, "error": str(e)})
                except users.Forbidden as e:
                    self._send_json(403, {"ok": False, "error": str(e)})
                except storage.StorageError as e:
                    log.error("storage error: %s", e)
                    self._send_json(500, {"ok": False, "error": "مشكلة في ملفات البيانات، تم أخذ نسخة احتياطية. راجع السجل."})
                except Exception:
                    log.exception("API %s %s failed", method, path)
                    self._send_json(500, {"ok": False, "error": "حدث خطأ غير متوقع، حاول مرة أخرى."})
                return
        self._send_json(404, {"ok": False, "error": "غير موجود."})

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("HEAD")

    def do_POST(self):
        self._dispatch("POST")


def need_origin(h):
    """Cookie-authenticated or login POSTs must come from our own page."""
    if not h.origin_ok(required=True):
        raise ApiError(403, "طلب غير مسموح.")


# ---------------------------------------------------------------- public API

@route("GET", "/api/ping")
def api_ping(h):
    return ok({"app": "rizq"})


@route("GET", "/api/jobs")
def api_jobs(h):
    return ok(catalog.public_jobs())


@route("GET", r"/api/jobs/([A-Za-z0-9_-]{1,40})")
def api_job(h, job_id):
    job = catalog.public_job(job_id)
    if not job:
        raise ApiError(404, "الوظيفة غير موجودة.")
    return ok({"job": job})


@route("GET", "/api/programs")
def api_programs(h):
    return ok(programs.public_programs())


@route("GET", "/api/settings")
def api_settings(h):
    return ok(public_settings())


_signup_limiter = auth.RateLimiter(max_failures=30, window_seconds=3600)


@route("POST", "/api/users")
def api_users_save(h):
    body = h.body()
    key = "signup:" + h.client_ip()
    is_new = not (body.get("id") and body.get("edit_token"))
    if is_new and _signup_limiter.blocked(key):
        raise ApiError(429, "محاولات تسجيل كثيرة، حاول بعد قليل.")
    user, token = users.save(body)
    if token:
        _signup_limiter.fail(key)
    out = {"user": users.public(user)}
    if token:
        out["edit_token"] = token
    return ok(out)


@route("POST", "/api/users/status")
def api_users_status(h):
    b = h.body()
    return ok({"user": users.public(users.check_token(b.get("id"), b.get("edit_token")))})


@route("POST", "/api/users/delete")
def api_users_delete(h):
    b = h.body()
    users.delete(b.get("id"), b.get("edit_token"))
    return ok()


@route("POST", "/api/users/telegram-link")
def api_users_tg(h):
    b = h.body()
    url = users.create_link_token(b.get("id"), b.get("edit_token"))
    if not url:
        raise ApiError(503, "بوت تيليجرام غير مفعّل بعد.")
    return ok({"url": url})


_report_limiter = auth.RateLimiter(max_failures=20, window_seconds=3600)
REPORT_REASONS = ("احتيال أو طلب رسوم", "الوظيفة منتهية", "معلومات خاطئة", "محتوى غير لائق", "أخرى")


@route("POST", "/api/reports")
def api_report(h):
    b = h.body()
    key = "report:" + h.client_ip()
    if _report_limiter.blocked(key):
        raise ApiError(429, "أرسلت بلاغات كثيرة، حاول لاحقاً.")
    job_id = b.get("job_id")
    if not isinstance(job_id, str) or not re.match(r"^[A-Za-z0-9_-]{1,40}$", job_id) or not catalog.find(job_id):
        raise ApiError(400, "الوظيفة غير موجودة.")
    reason = b.get("reason") if b.get("reason") in REPORT_REASONS else "أخرى"
    note = normalize.one_line(b.get("note") if isinstance(b.get("note"), str) else "", 300)
    _report_limiter.fail(key)
    with storage.edit("reports") as reports:
        reports.append({"id": "r" + secrets.token_hex(5), "job_id": job_id, "reason": reason,
                        "note": note, "created_at": storage.now_iso(), "status": "open"})
    return ok()


# ---------------------------------------------------------------- direct posts ("أعلن عن وظيفة")

# Every attempt counts here (valid or not); posts.submit also caps stored posts per IP per day
_post_attempts = auth.RateLimiter(max_failures=30, window_seconds=3600)


@route("POST", "/api/posts")
def api_post_submit(h):
    need_origin(h)
    ip = h.client_ip()
    key = "post:" + ip
    if _post_attempts.blocked(key):
        raise ApiError(429, "محاولات كثيرة، حاول بعد قليل.")
    _post_attempts.fail(key)
    b = h.body()
    trap = b.get(posts.HONEYPOT_FIELD)
    if trap:  # bots fill every field: pretend success, store nothing
        log.info("direct post honeypot triggered")
        return ok({"post": {"id": "d" + secrets.token_hex(6), "status": "pending"}})
    post = posts.submit(b, ip)
    log.info("direct post %s received (pending)", post["id"])
    return ok({"post": {"id": post["id"], "status": post["status"]}})


# ---------------------------------------------------------------- admin API

@route("POST", "/api/admin/login")
def api_admin_login(h):
    need_origin(h)
    if owner.owner_chat():
        raise ApiError(403, "الدخول الآن عبر تيليجرام فقط.")
    if not config.ADMIN_PASSWORD:
        raise ApiError(403, "لوحة الإدارة غير مفعّلة. أضف كلمة مرور الإدارة في إعدادات الخادم (env).")
    key = "admin:" + h.client_ip()
    if auth.login_limiter.blocked(key):
        raise ApiError(429, "محاولات كثيرة. حاول بعد 15 دقيقة.")
    b = h.body()
    pw = b.get("password") if isinstance(b.get("password"), str) else ""
    if not hmac.compare_digest(pw.encode("utf-8"), config.ADMIN_PASSWORD.encode("utf-8")):
        auth.login_limiter.fail(key)
        raise ApiError(401, "كلمة المرور غير صحيحة.")
    auth.login_limiter.reset(key)
    token = auth.create_session("admin", "admin")
    return ok(cookies=[Handler.session_cookie(auth.COOKIE_ADMIN, token, auth.ADMIN_SESSION_HOURS * 3600)])


def _logged_in(h):
    return bool(auth.get_session(h.cookie(auth.COOKIE_ADMIN), "admin"))


@route("GET", "/api/admin/whoami")
def api_admin_whoami(h):
    return ok({"logged_in": _logged_in(h), "owner_linked": bool(owner.owner_chat()),
               "password_login": bool(config.ADMIN_PASSWORD) and not owner.owner_chat(),
               "bot": config.TELEGRAM_BOT_USERNAME})


@route("POST", "/api/admin/tg-login/start")
def api_tg_login_start(h):
    need_origin(h)
    if not owner.owner_chat() or not telegram_bot.enabled():
        raise ApiError(409, "لم يُربط حساب تيليجرام باللوحة بعد.")
    key = "tglogin:" + h.client_ip()
    if auth.login_limiter.blocked(key):
        raise ApiError(429, "محاولات كثيرة. حاول بعد 15 دقيقة.")
    auth.login_limiter.fail(key)       # each ticket counts; a successful login resets it
    ticket, code, bot_token = owner.new_ticket()
    return ok({"ticket": ticket, "code": code, "link": f"https://t.me/{config.TELEGRAM_BOT_USERNAME}?start=login_{bot_token}"})


@route("POST", "/api/admin/tg-login/check")
def api_tg_login_check(h):
    need_origin(h)
    b = h.body()
    ticket = b.get("ticket") if isinstance(b.get("ticket"), str) else ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", ticket):
        raise ApiError(400, "طلب غير صالح.")
    state = owner.check_ticket(ticket)
    if state != "approved":
        return ok({"state": state})
    auth.login_limiter.reset("tglogin:" + h.client_ip())
    token = auth.create_session("admin", "owner")
    analytics.log_event("login", None)   # no person: the reports skip events without one
    try:
        telegram_bot.send(owner.owner_chat(), "🔐 تم تسجيل دخول جديد للوحة مُنْفَرِد الآن. إن لم تكن أنت، أخبرنا فوراً.")
    except Exception:
        pass
    return ok({"state": "approved"}, cookies=[Handler.session_cookie(auth.COOKIE_ADMIN, token, auth.ADMIN_SESSION_HOURS * 3600)])


@route("POST", "/api/admin/owner-link")
def api_admin_owner_link(h):
    _admin_post(h)
    if not telegram_bot.enabled():
        raise ApiError(409, "البوت غير مفعّل.")
    return ok({"link": f"https://t.me/{config.TELEGRAM_BOT_USERNAME}?start=owner_{owner.new_owner_link()}"})


# ---------------------------------------------------------------- the Saudi helper (Raed's PC)
# The helper sends its key in "X-Helper-Key" (no cookies, no browser). It reads the cloud's source
# status and the relay pages waiting for it, and hands back what it fetched. Only these few writes
# are possible with the key: it never sees subscribers, sessions or anything else.

HELPER_BATCHES_MAX = 60
ORIGIN_INBOX = "_origin"   # = collectors.origin.INBOX_KEY (collectors/*.py are not deployed to Vercel)


HELPER_NEVER = ("linkedin", "x")   # = collectors.runner.HELPER_NEVER


def _helper_may_fetch(src):
    """The same rule as collectors.runner.helper_can_fetch: marked for the helper, or refused by
    the cloud with a 403 / challenge (never LinkedIn or X, never a 429)."""
    if src.get("cloud") == "helper":
        return True
    st = storage.read("sources_status").get(src.get("key")) or {}
    return (st.get("status") == "blocked" and src.get("type") not in HELPER_NEVER and src.get("key") not in HELPER_NEVER
            and src.get("helper") is not False and "429" not in str(st.get("message") or ""))


HELPER_ITEM_FIELDS = {"title": 300, "url": 1000, "apply_url": 1000, "company": 200, "city": 100, "posted_date": 40,
                      "raw_text": 6000, "salary": 100, "source": 120}


def _helper_item(it):
    """One job from the helper, reduced to plain text fields; links must be http(s)."""
    out = {k: str(it.get(k) or "")[:n] for k, n in HELPER_ITEM_FIELDS.items() if it.get(k)}
    for k in ("url", "apply_url"):
        if out.get(k) and not out[k].lower().startswith(("http://", "https://")):
            out.pop(k)
    if it.get("needs_ai") is True:
        out["needs_ai"] = True
    return out if out.get("title") else None


def _helper(h):
    key = "helper:" + h.client_ip()
    if auth.login_limiter.blocked(key):
        raise ApiError(429, "too many attempts")
    if not owner.helper_key_ok(h.headers.get("X-Helper-Key") or ""):
        auth.login_limiter.fail(key)
        raise ApiError(401, "bad helper key")


@route("GET", "/api/helper/work")
def api_helper_work(h):
    _helper(h)
    box = storage.read("inbox").get(ORIGIN_INBOX) or {}
    status = {k: v for k, v in storage.read("sources_status").items() if not k.startswith("_")}
    return ok({"sources_status": status, "pending": list(box.get("pending") or [])})


@route("POST", "/api/helper/batch")
def api_helper_batch(h):
    _helper(h)
    b = h.body()
    key, batch = b.get("key"), b.get("batch")
    src = next((s for s in analytics.sources_config() if s.get("key") == key), None)
    if not src or not _helper_may_fetch(src) or not isinstance(batch, dict) or not isinstance(batch.get("items"), list):
        raise ApiError(400, "bad batch")
    items = [_helper_item(it) for it in batch["items"][:5000] if isinstance(it, dict)]
    # filter_jobs always comes from sources.json (the cloud's own setting), never from the PC
    batch = {"at": str(batch.get("at") or storage.now_iso())[:60], "items": [it for it in items if it],
             "note": str(batch.get("note") or "")[:300], "filter_jobs": None}
    with storage.edit("inbox") as inbox:
        box = inbox.setdefault(key, {"batches": []})
        box["batches"] = [x for x in box.get("batches") or [] if x.get("at") != batch["at"]][-(HELPER_BATCHES_MAX - 1):] + [batch]
    return ok({"stored": len(batch["items"])})


@route("POST", "/api/helper/origin")
def api_helper_origin(h):
    _helper(h)
    b = h.body()
    tried = {u for u in (b.get("tried") or [])[:2000] if isinstance(u, str)}
    done = b.get("done") if isinstance(b.get("done"), dict) else {}
    with storage.edit("inbox") as inbox:
        box = inbox.setdefault(ORIGIN_INBOX, {})
        asked = set(box.get("pending") or [])
        # only pages the cloud asked for, and only real links
        done = {u: (l if isinstance(l, str) and l.lower().startswith(("http://", "https://", "mailto:")) else "")[:1000]
                for u, l in list(done.items())[:2000] if u in asked}
        tried &= asked
        box["pending"] = [u for u in box.get("pending") or [] if u not in tried]
        box["done"] = dict(box.get("done") or {}, **done)
    return ok({"done": sum(1 for v in done.values() if v)})


@route("POST", "/api/helper/report")
def api_helper_report(h):
    _helper(h)
    r = h.body().get("report")
    if not isinstance(r, dict):
        raise ApiError(400, "bad report")
    report = {"at": str(r.get("at") or storage.now_iso())[:40], "minutes": r.get("minutes"),
              "origin_links": r.get("origin_links") if isinstance(r.get("origin_links"), int) else 0,
              "sources": {str(k)[:40]: (v if isinstance(v, int) else str(v)[:120])
                          for k, v in (r.get("sources") or {}).items()} if isinstance(r.get("sources"), dict) else {},
              "host": str(r.get("host") or "")[:60], "error": str(r.get("error") or "")[:300],
              "notes": {str(k)[:40]: str(v)[:300] for k, v in (r.get("notes") or {}).items()}
              if isinstance(r.get("notes"), dict) else {}}
    arch = r.get("archive")
    if isinstance(arch, dict):   # the PC archive: numbers, its folder, its last error
        report["archive"] = {k: arch[k] for k in ("jobs", "rows", "saved", "free_gb") if isinstance(arch.get(k), int)}
        report["archive"].update({k: str(arch[k])[:200] for k in ("path", "error", "backup") if arch.get(k)})
    with storage.edit("bot_state") as st:   # the LinkedIn task (every 30 min) and the daily run, apart
        st["helper_linkedin" if r.get("mode") == "linkedin" else "helper_last"] = report
        if "archive" in report:
            st["pc_archive"] = dict(report["archive"], at=report["at"])
    return ok()


ARCHIVE_TAKE_MAX = 1500   # ~2 MB an answer (a function's answer is limited to 4.5 MB)


@route("GET", "/api/helper/archive")
def api_helper_archive(h):
    """Jobs waiting for the PC archive, oldest first. They stay here until /ack."""
    _helper(h)
    try:
        n = max(1, min(ARCHIVE_TAKE_MAX, int((h.query().get("limit") or [ARCHIVE_TAKE_MAX])[0])))
    except ValueError:
        n = ARCHIVE_TAKE_MAX
    return ok({"items": analytics.outbox_take(n)})


@route("POST", "/api/helper/archive/ack")
def api_helper_archive_ack(h):
    """The PC saved these (committed on its disk): only now are they deleted here."""
    _helper(h)
    ids = h.body().get("ids")
    if not isinstance(ids, list):
        raise ApiError(400, "bad ids")
    return ok({"deleted": analytics.outbox_ack(ids)})


# ---------------------------------------------------------------- owner dashboard

NONE_VALUE = "__none__"   # the dashboard's value for "not stated" (an empty city or source)


def _list(q, key):
    return ["" if v == NONE_VALUE else v for x in q.get(key, []) for v in x.split("|") if v][:60]


def _job_filter(q):
    return {"q": (q.get("q") or [""])[0][:100], "cities": _list(q, "city"), "exp": _list(q, "exp"),
            "sources": _list(q, "source"), "kinds": _list(q, "kind"), "status": _list(q, "status") or ["live"],
            "companies": set(_list(q, "company")),
            "from": (q.get("from") or [""])[0][:10], "to": (q.get("to") or [""])[0][:10],
            "helper": (q.get("helper") or [""])[0] == "1", "jadarat": (q.get("jadarat") or [""])[0] == "1",
            "sort": "resolved" if (q.get("sort") or [""])[0] == "resolved" else ""}


@route("GET", "/api/admin/dash/overview")
def api_dash_overview(h):
    h.admin()
    return ok(analytics.overview())


@route("GET", "/api/admin/dash/jobs")
def api_dash_jobs(h):
    h.admin()
    q = h.query()
    try:
        page = int((q.get("page") or ["0"])[0])
    except ValueError:
        page = 0
    return ok(analytics.jobs_page(_job_filter(q), page))


@route("GET", "/api/admin/dash/sources")
def api_dash_sources(h):
    h.admin()
    return ok(analytics.sources_report())


@route("GET", "/api/admin/dash/catalog")
def api_dash_catalog(h):
    h.admin()
    return ok(analytics.sources_catalog())


@route("GET", "/api/admin/dash/waiting")
def api_dash_waiting(h):
    h.admin()
    return ok(analytics.waiting_summary())


@route("GET", "/api/admin/dash/users")
def api_dash_users(h):
    h.admin()
    return ok(analytics.users_report())


@route("GET", "/api/admin/dash/live")
def api_dash_live(h):
    h.admin()
    try:
        since = int((h.query().get("since") or ["0"])[0])
    except ValueError:
        since = 0
    out = analytics.live_feed(since)
    out["bot"] = analytics.bot_status()
    return ok(out)


XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@route("GET", r"/api/admin/export/(jobs|sources|users|events|all)")
def api_dash_export(h, kind):
    h.admin()
    a = analytics
    today = datetime.now().strftime("%Y-%m-%d")
    sheets = []
    if kind in ("jobs", "all"):
        sheets.append(a.sheet_jobs(a.filter_jobs(_job_filter(h.query()) if kind == "jobs" else
                                                 {"status": ["live", "expired", "archived"]})))
    if kind in ("sources", "all"):
        rep = a.sources_report()
        sheets.append(a.sheet_sources(rep))
        sheets.append(("حالة المجمّعات", ["المصدر", "الحالة", "الرسالة", "آخر تشغيل", "جُلب", "أُضيف", "يحتاج جهازي"],
                       [[c["name"], c["status"], c["message"], c["last_run"], c["fetched"], c["added"],
                         "نعم" if c["needs_helper"] else ""] for c in rep["collectors"]]))
    if kind in ("users", "all"):
        u = a.users_report()
        sheets.append(a.sheet_users())
        sheets.append(a.sheet_pairs("الطلب والعرض", ["المسمى", "عدد الأشخاص", "الوظائف الحالية"],
                                    [(d["title"], d["people"], d["jobs"]) for d in u["demand"]]))
        sheets.append(a.sheet_pairs("المدن المطلوبة", ["المدينة", "عدد الأشخاص"], u["cities"]))
        sheets.append(a.sheet_pairs("بحث بلا نتائج", ["المسميات", "مرات البحث"], u["zero_results"]))
        sheets.append(a.sheet_pairs("أكثر الوظائف ضغطاً", ["الوظيفة", "المصدر", "الضغطات"],
                                    [(c["title"], c["source"], c["n"]) for c in u["top_clicked"]]))
        sheets.append(a.sheet_pairs("القمع", ["المرحلة", "عدد الأشخاص"], u["funnel"]))
    if kind in ("events", "all"):
        sheets.append(a.sheet_events())
    names = {"jobs": "الوظائف", "sources": "المصادر", "users": "المستخدمون", "events": "الأحداث", "all": "تقرير-شامل"}
    return RawFile(xlsx.workbook(sheets), XLSX, f"منفرد-{names[kind]}-{today}.xlsx")


@route("POST", "/api/admin/logout")
def api_admin_logout(h):
    auth.delete_session(h.cookie(auth.COOKIE_ADMIN))
    return ok(cookies=[Handler.session_cookie(auth.COOKIE_ADMIN, "", 0)])


def _admin_post(h):
    need_origin(h)
    return h.admin()


@route("GET", "/api/admin/overview")
def api_admin_overview(h):
    h.admin()
    jobs, _ = catalog.all_jobs(include_demo=False)
    all_users = storage.read("users")
    by_source = {}
    for j in jobs:
        by_source[j.get("source", "?")] = by_source.get(j.get("source", "?"), 0) + 1
    status = storage.read("sources_status")
    try:
        sources = load_sources()
    except (OSError, ValueError):
        sources = []
    source_rows = []
    for s in sources:
        st = status.get(s["key"], {})
        row = {"key": s["key"], "name": s["name"], "type": s["type"], "enabled": s.get("enabled", True)}
        row.update({k: st.get(k) for k in ("status", "message", "fetched", "jobs", "added",
                                           "last_run", "zero_streak", "seconds")})
        source_rows.append(row)
    post_rows = posts.admin_list()
    reports = [dict(r) for r in storage.read("reports") if r.get("status") == "open"]
    by_id = {j["id"]: j for j in jobs}
    for r in reports:
        j = by_id.get(r["job_id"])
        r["job_title"] = j.get("title") if j else "(محذوفة)"
        r["job_source"] = j.get("source") if j else ""
    return ok({
        "stats": {
            "jobs": len(jobs), "by_source": by_source,
            "users": len(all_users),
            "telegram_linked": len([u for u in all_users if u.get("telegram_chat_id")]),
            "posts_pending": len([p for p in post_rows if p["status"] == "pending"]),
            "posts_live": len([j for j in jobs if j.get("direct")]),
        },
        "sources": source_rows,
        "last_run": status.get("_last_run"),
        "collecting": bool(_runner() and _runner().is_running()),
        "features": config.features(),
        "bot_status": ("webhook" if config.ON_VERCEL and telegram_bot.enabled() else
                       Workers.bot.status if Workers.bot else ("off" if not telegram_bot.enabled() else "not started")),
        "ai_usage": ai.usage_summary(),
        "posts": post_rows,
        "reports": reports,
        "settings": get_settings(),
    })


@route("POST", "/api/admin/collect")
def api_admin_collect(h):
    _admin_post(h)
    if config.ON_VERCEL:
        raise ApiError(409, "الجمع يعمل تلقائياً كل ساعة على GitHub Actions.")
    return ok({"started": start_collection_async()})


# ---------------------------------------------------------------- Telegram webhook (cloud)

@route("POST", r"/api/telegram/([A-Za-z0-9_-]{16,128})")
def api_telegram_webhook(h, secret):
    """Telegram sends each bot update here (setWebhook with the same secret in the URL and header)."""
    want = config.TELEGRAM_WEBHOOK_SECRET
    got_header = h.headers.get("X-Telegram-Bot-Api-Secret-Token") or ""
    if not (want and telegram_bot.enabled() and hmac.compare_digest(secret, want)
            and hmac.compare_digest(got_header, config.webhook_header_secret())):
        raise ApiError(404, "غير موجود.")
    upd = h.body()
    if isinstance(upd, dict) and isinstance(upd.get("update_id"), int) and not bot.already_handled(upd["update_id"]):
        try:
            bot.handle_update(upd)
        except Exception:
            # answered 200 anyway: a failing update must not be re-sent by Telegram forever
            log.exception("bot update %s failed", upd.get("update_id"))
        # marked after handling: if the function is stopped half-way, Telegram's retry is handled
        bot.mark_handled(upd["update_id"])
    return ok()


def _str(v, n):
    return normalize.one_line(v if isinstance(v, str) else "", n)


@route("POST", "/api/admin/settings")
def api_admin_settings(h):
    _admin_post(h)
    b = h.body()
    with storage.edit("settings") as s:
        s.pop("banner", None)  # ads were removed; drop any old value
        if isinstance(b.get("support"), dict):
            sp = b["support"]
            links = []
            for l in (sp.get("links") or [])[:4]:
                if not isinstance(l, dict):
                    continue
                label, url = _str(l.get("label"), 40), _str(l.get("url"), 300)
                if label and re.match(r"^https://", url):
                    links.append({"label": label, "url": url})
            s["support"] = {"enabled": bool(sp.get("enabled", True)),
                            "text": normalize.clean_text(sp.get("text") if isinstance(sp.get("text"), str) else "", 600),
                            "links": links}
    return ok({"settings": get_settings()})


def _notify_async(job):
    """Alert matching subscribers about a newly approved direct post (if the bot is on)."""
    def run():
        try:
            notifier.notify_new_jobs([job])
        except Exception:
            log.exception("notify for direct post failed")
        bot.submit_new_jobs([job])  # Telegram bot: instant alerts + group/channel feeds
    if config.ON_VERCEL:
        run()   # a background thread may be frozen once the answer is sent
        return
    threading.Thread(target=run, daemon=True, name="rizq-notify-post").start()


@route("POST", r"/api/admin/posts/([A-Za-z0-9_-]{1,40})/status")
def api_admin_post_status(h, post_id):
    _admin_post(h)
    post, newly = posts.set_status(post_id, h.body().get("status"))
    if newly and telegram_bot.enabled():
        _notify_async(posts.to_job(post))
    return ok({"post": {"id": post["id"], "status": post["status"]}})


@route("POST", r"/api/admin/posts/([A-Za-z0-9_-]{1,40})/delete")
def api_admin_post_delete(h, post_id):
    _admin_post(h)
    posts.delete(post_id)
    return ok()


@route("POST", r"/api/admin/jobs/([A-Za-z0-9_-]{1,40})/hide")
def api_admin_hide(h, job_id):
    _admin_post(h)
    with storage.edit("settings") as s:
        hidden = s.setdefault("hidden_job_ids", [])
        if job_id not in hidden:
            hidden.append(job_id)
    with storage.edit("reports") as reports:
        for r in reports:
            if r.get("job_id") == job_id:
                r["status"] = "resolved"
    return ok()


@route("POST", r"/api/admin/reports/(r[a-f0-9]{10})/dismiss")
def api_admin_report_dismiss(h, report_id):
    _admin_post(h)
    with storage.edit("reports") as reports:
        for r in reports:
            if r.get("id") == report_id:
                r["status"] = "dismissed"
    return ok()


# ---------------------------------------------------------------- startup

def setup_logging():
    os.makedirs(config.LOG_DIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(os.path.join(config.LOG_DIR, "rizq.log"),
                                              maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    ch.setLevel(logging.INFO)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(fh)
    root.addHandler(ch)


def already_running(port):
    """Returns the port of a running RIZQ instance (checks port..port+9), or None."""
    for p in range(port, port + 10):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{p}/api/ping", timeout=1) as r:
                if json.load(r).get("app") == "rizq":
                    return p
        except Exception:
            continue
    return None


def main():
    port = config.PORT
    if "--port" in sys.argv:
        try:
            port = int(sys.argv[sys.argv.index("--port") + 1])
        except (IndexError, ValueError):
            pass
    no_open = "--no-open" in sys.argv

    # Single instance: a second double-click just opens the browser
    running = already_running(port)
    if running:
        print(f"RIZQ is already running at http://localhost:{running}/")
        if not no_open:
            webbrowser.open(f"http://localhost:{running}/")
        return

    setup_logging()
    os.makedirs(config.STORE_DIR, exist_ok=True)
    try:
        migrated = posts.migrate_legacy()
        if migrated:
            log.info("migrated old company portal data: %s", migrated)
    except Exception:
        log.exception("migration of old company portal data failed (old files left untouched)")
    server = None
    for p in range(port, port + 10):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            port = p
            break
        except OSError:
            continue
    if server is None:
        print("Could not find a free port.")
        sys.exit(1)

    url = f"http://localhost:{port}/"
    feats = config.features()
    print("=" * 60)
    print(f" RIZQ is running:   {url}")
    print(f" Admin dashboard:   {url}#/admin   (password in .env)")
    print(f" Groq AI:           {'ON' if feats['groq'] else 'off (add GROQ_API_KEY in .env)'}")
    print(f" Telegram bot:      {'ON' if feats['telegram_bot'] else 'off (add TELEGRAM_BOT_TOKEN in .env)'}")
    print(f" Auto collection:   {'every %dh' % feats['auto_collect_hours'] if feats['auto_collect_hours'] else 'off'}")
    print(" Close this window to stop.")
    print("=" * 60)

    if "--no-workers" not in sys.argv:
        start_workers()
    if not no_open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
