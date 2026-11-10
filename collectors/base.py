"""Shared HTTP for collectors: real User-Agent, robots.txt, per-host pacing.

Rules (CLAUDE.md): jobs only, polite, never evade blocks. A block raises
Blocked so the runner can stop that source quietly and log the reason.
"""

import gzip
import io
import random
import threading
import time
import urllib.error
import urllib.request
import re
from urllib.parse import unquote, urlsplit

from backend import config

DEFAULT_TIMEOUT = 25
MIN_HOST_INTERVAL = 2.0     # seconds between requests to the same host
MAX_BYTES = 5 * 1024 * 1024

_robots = {}            # base url -> (fetched_at, parser)
ROBOTS_TTL = 24 * 3600
BOT_TOKEN = "RizqJobsBot"  # product token matched against robots.txt User-agent lines
_last_hit = {}
_lock = threading.Lock()


class RobotsRules:
    """robots.txt per RFC 9309 (urllib.robotparser gets two things wrong for us:
    it takes the FIRST matching rule instead of the longest one, and it ignores the
    * and $ wildcards, so it can allow paths a site forbids).

    - The group whose user-agent equals our token wins; otherwise the "*" group(s).
    - The longest matching pattern decides; on a tie, Allow wins; no match = allowed.
    """

    def __init__(self, lines=()):
        self.groups = []          # [(set(agents), [(is_allow, pattern), ...])]
        self.parse(lines)

    def parse(self, lines):
        agents, rules, last_was_agent = set(), [], False
        for raw in lines:
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, val = (x.strip() for x in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if not last_was_agent and (agents or rules):
                    self.groups.append((agents, rules))
                    agents, rules = set(), []
                agents.add(val.lower())
                last_was_agent = True
            elif key in ("allow", "disallow"):
                last_was_agent = False
                if agents and val:
                    rules.append((key == "allow", val))
            else:
                last_was_agent = False   # sitemap / crawl-delay etc. do not end a group
        if agents or rules:
            self.groups.append((agents, rules))

    @staticmethod
    def _matches(pattern, path):
        rx = "".join(".*" if ch == "*" else ("$" if ch == "$" and i == len(pattern) - 1 else re.escape(ch))
                     for i, ch in enumerate(pattern))
        return re.match(rx, path) is not None

    def can_fetch(self, agent, url):
        parts = urlsplit(url)
        path = unquote(parts.path or "/") + ("?" + unquote(parts.query) if parts.query else "")
        if path == "/robots.txt":
            return True
        token = agent.lower()
        rules = [r for a, rs in self.groups if token in a for r in rs]
        if not rules and not any(token in a for a, _ in self.groups):
            rules = [r for a, rs in self.groups if "*" in a for r in rs]
        best_len, allowed = -1, True
        for is_allow, pattern in rules:
            pat = unquote(pattern)
            if self._matches(pat, path):
                n = len(pat)
                if n > best_len or (n == best_len and is_allow):
                    best_len, allowed = n, is_allow
        return allowed


class FetchError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class Blocked(FetchError):
    """The site refused us (403/429/999/captcha). Stop this source; never retry harder."""


class Disallowed(FetchError):
    """robots.txt disallows this path."""


class _PoliteRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect to ANOTHER host only if that host's robots.txt allows it.

    Without this, urllib would silently follow e.g. a short link to its target
    without the target (or the shortener) ever being checked.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urlsplit(req.full_url).netloc.lower(), urlsplit(newurl).netloc.lower()
        if old != new and not robots_allowed(newurl):
            raise Disallowed("redirect target disallowed by robots.txt: " + new)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_polite_opener = urllib.request.build_opener(_PoliteRedirects)


def _open(url, headers=None, timeout=DEFAULT_TIMEOUT, check_redirects=True):
    h = {
        "User-Agent": config.USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml,application/rss+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "ar,en;q=0.8",
        "Accept-Encoding": "gzip",
    }
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    try:
        opener = _polite_opener if check_redirects else urllib.request.build_opener()
        with opener.open(req, timeout=timeout) as r:
            raw = r.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise FetchError("response too large")
            if r.headers.get("Content-Encoding") == "gzip":
                with gzip.GzipFile(fileobj=io.BytesIO(raw)) as gz:
                    raw = gz.read(MAX_BYTES + 1)  # cap output (gzip-bomb guard)
                if len(raw) > MAX_BYTES:
                    raise FetchError("response too large")
            charset = r.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 429, 451, 999):
            raise Blocked(f"HTTP {e.code}")
        raise FetchError(f"HTTP {e.code}", e.code)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise FetchError(str(getattr(e, "reason", e)))


def robots_allowed(url):
    parts = urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}"
    with _lock:
        cached = _robots.get(base)
    if cached is None or time.time() - cached[0] > ROBOTS_TTL:
        rp = RobotsRules()
        try:
            text = _open(base + "/robots.txt", timeout=15, check_redirects=False)
            rp.parse(text.splitlines())
        except Blocked:
            rp.parse(["User-agent: *", "Disallow: /"])     # refused robots.txt -> disallow
        except FetchError as e:
            if e.status and 400 <= e.status < 500:
                rp.parse([])                                # no robots.txt -> allowed (RFC 9309)
            else:
                rp.parse(["User-agent: *", "Disallow: /"])  # 5xx / network error -> disallow
        cached = (time.time(), rp)
        with _lock:
            _robots[base] = cached
    return cached[1].can_fetch(BOT_TOKEN, url)


def _pace(url, min_interval):
    host = urlsplit(url).netloc
    with _lock:
        wait = min_interval - (time.time() - _last_hit.get(host, 0))
        _last_hit[host] = time.time() + max(wait, 0)
    if wait > 0:
        time.sleep(wait)


CHALLENGE_SIGNS = ("cf-chl", "challenge-platform", "<title>just a moment",
                   "attention required! | cloudflare", "g-recaptcha-response")


def fetch_json(url, payload=None, headers=None, check_robots=True, min_interval=MIN_HOST_INTERVAL):
    """GET (or POST when payload is given) a JSON endpoint politely and decode it."""
    import json
    if check_robots and not robots_allowed(url):
        raise Disallowed("robots.txt disallows " + urlsplit(url).path)
    _pace(url, min_interval)
    h = {"Accept": "application/json", "User-Agent": config.USER_AGENT,
         "Accept-Language": "ar,en;q=0.8"}
    h.update(headers or {})
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method="POST" if data else "GET")
    try:
        with _polite_opener.open(req, timeout=DEFAULT_TIMEOUT) as r:
            raw = r.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 429, 451, 999):
            raise Blocked(f"HTTP {e.code}")
        raise FetchError(f"HTTP {e.code}", e.code)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise FetchError(str(getattr(e, "reason", e)))
    if len(raw) > MAX_BYTES:
        raise FetchError("response too large")
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        if any(sig in raw[:6000].decode("utf-8", "replace").lower() for sig in CHALLENGE_SIGNS):
            raise Blocked("anti-bot challenge page")
        raise FetchError("not JSON")


_render_lock = threading.Lock()


def first_line(exc, limit=200):
    """First line of an exception message (Playwright errors carry long call logs)."""
    lines = str(exc).splitlines()
    return (lines[0] if lines else type(exc).__name__)[:limit]


def fetch_rendered(url, wait_selector=None, check_robots=True, min_interval=4.0, timeout_ms=30000):
    """Render a JavaScript page with headless Chromium (Playwright) and return its HTML.

    For sites that build their job list in the browser. This is NOT a way around
    bot protection: a challenge page raises Blocked like any other fetch.
    """
    if check_robots and not robots_allowed(url):
        raise Disallowed("robots.txt disallows " + urlsplit(url).path)
    try:
        from playwright.sync_api import sync_playwright, Error as PwError
    except ImportError:
        raise FetchError("Playwright غير مثبت (pip install playwright ثم playwright install chromium)")
    _pace(url, min_interval)
    with _render_lock:  # one browser at a time keeps memory and load low
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    page = browser.new_page(user_agent=config.USER_AGENT, locale="ar-SA")
                    resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                    if resp is not None and resp.status in (401, 403, 429, 451, 999):
                        raise Blocked(f"HTTP {resp.status}")
                    # A page that never settles (analytics polling) is still read as-is
                    try:
                        if wait_selector:
                            page.wait_for_selector(wait_selector, timeout=timeout_ms)
                        else:
                            page.wait_for_load_state("networkidle", timeout=timeout_ms)
                    except PwError:
                        pass
                    html = page.content()
                finally:
                    browser.close()
        except Blocked:
            raise
        except Exception as e:  # Playwright timeouts / navigation errors
            raise FetchError("render: " + first_line(e, 200))
    if any(sig in html[:8000].lower() for sig in CHALLENGE_SIGNS):
        raise Blocked("anti-bot challenge page")
    return html


def fetch(url, headers=None, check_robots=True, min_interval=MIN_HOST_INTERVAL, jitter=0.0):
    """GET a URL politely. Raises Disallowed / Blocked / FetchError."""
    if check_robots and not robots_allowed(url):
        raise Disallowed("robots.txt disallows " + urlsplit(url).path)
    _pace(url, min_interval + (random.uniform(0, jitter) if jitter else 0))
    text = _open(url, headers=headers)
    low = text[:6000].lower()
    if any(sig in low for sig in CHALLENGE_SIGNS):
        raise Blocked("anti-bot challenge page")
    return text
