"""Sessions, secret tokens and rate limiting."""

import hashlib
import hmac
import secrets
import threading
import time
from datetime import datetime, timedelta

from . import config, storage

SESSION_DAYS = 7
ADMIN_SESSION_HOURS = 12
COOKIE_ADMIN = "rizq_admin"


# ---------------------------------------------------------------- tokens

def new_token():
    return secrets.token_urlsafe(32)


def token_hash(token):
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def token_matches(token, stored_hash):
    return bool(token) and bool(stored_hash) and hmac.compare_digest(token_hash(token), stored_hash)


# ---------------------------------------------------------------- sessions

def create_session(kind, subject, hours=None):
    """Returns the raw token (only its SHA-256 is stored)."""
    token = new_token()
    hours = hours or (ADMIN_SESSION_HOURS if kind == "admin" else SESSION_DAYS * 24)
    expires = (datetime.now() + timedelta(hours=hours)).isoformat(timespec="seconds")
    with storage.edit("sessions") as sessions:
        now = datetime.now().isoformat(timespec="seconds")
        for k in [k for k, v in sessions.items() if v.get("expires", "") < now]:
            del sessions[k]  # purge expired
        sessions[token_hash(token)] = {"kind": kind, "sub": subject, "expires": expires}
    return token


def get_session(token, kind):
    if not token:
        return None
    s = storage.read("sessions").get(token_hash(token))
    if not s or s.get("kind") != kind:
        return None
    if s.get("expires", "") < datetime.now().isoformat(timespec="seconds"):
        return None
    return s


def delete_session(token):
    if not token:
        return
    with storage.edit("sessions") as sessions:
        sessions.pop(token_hash(token), None)


# ---------------------------------------------------------------- rate limiting

class RateLimiter:
    """N failures per key within a window -> locked out for the window."""

    def __init__(self, max_failures=5, window_seconds=900):
        self.max = max_failures
        self.window = window_seconds
        self.fails = {}
        self.lock = threading.Lock()

    def _shared(self):
        return bool(config.DATABASE_URL and config.ON_VERCEL)

    @staticmethod
    def _hk(key):
        """Keys hold IP addresses: only a hash is stored in the database."""
        return hashlib.sha256((config.webhook_header_secret() + "|rl|" + key).encode()).hexdigest()[:24]

    def blocked(self, key):
        now = time.time()
        if self._shared():
            hits = storage.read_cached("rate_limits").get(self._hk(key)) or []
            return len([t for t in hits if now - t < self.window]) >= self.max
        with self.lock:
            recent = [t for t in self.fails.get(key, []) if now - t < self.window]
            self.fails[key] = recent
            return len(recent) >= self.max

    def fail(self, key):
        now = time.time()
        if self._shared():
            with storage.edit("rate_limits") as rl:
                k0 = self._hk(key)
                rl[k0] = [t for t in rl.get(k0) or [] if now - t < self.window][-self.max * 2:] + [now]
                for k in [k for k, v in rl.items() if not v or now - max(v) > 86400]:
                    del rl[k]   # forget keys idle for a day
            return
        with self.lock:
            self.fails.setdefault(key, []).append(now)

    def reset(self, key):
        if self._shared():
            with storage.edit("rate_limits") as rl:
                rl.pop(self._hk(key), None)
            return
        with self.lock:
            self.fails.pop(key, None)


login_limiter = RateLimiter()
