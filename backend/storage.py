"""Storage for every collection: local JSON files, or Postgres in the cloud.

All reads/writes of data go through this module. It is safe across
threads (RLock) and across processes (a lock file), and writes are atomic.

Cloud mode (DATABASE_URL set): the SHARED collections live in Postgres (backend/pgstore.py),
so the bot on Vercel and the collectors on GitHub Actions see the same subscribers,
settings and AI cache. The collectors' own files (jobs, origin_cache, programs_watch) stay
files on the collector machine; Vercel reads a read-only copy of them (SNAPSHOT_DIR).

Usage:
    users = storage.read("users")                 # read-only snapshot
    with storage.edit("users") as users:          # read-modify-write
        users.append({...})
"""

import contextlib
import gzip
import json
import os
import shutil
import threading
import time
from datetime import datetime

from . import config

# name -> (file, default factory)
COLLECTIONS = {
    "jobs": ("jobs.json", list),                  # collected jobs
    "reports": ("reports.json", list),            # user reports of suspicious job posts
    "users": ("users.json", list),
    "direct_posts": ("direct_posts.json", list),  # moderated "أعلن عن وظيفة" posts
    "sessions": ("sessions.json", dict),
    "settings": ("settings.json", dict),
    "sent": ("sent.json", dict),                  # user_id -> [job_id, ...] already notified
    "sources_status": ("sources_status.json", dict),
    "ai_cache": ("ai_cache.json", dict),
    "bot_state": ("bot_state.json", dict),
    "link_tokens": ("link_tokens.json", dict),     # one-time Telegram deep-link tokens
    "origin_cache": ("origin_cache.json", dict),   # article url -> original apply link
    "alerts_daily": ("alerts_daily.json", dict),   # {"date": "YYYY-MM-DD", "counts": {user_id: jobs sent}}
    "programs_watch": ("programs_watch.json", dict),  # "kind:id" -> what the official page last said
    "subscribers": ("subscribers.json", dict),     # Telegram bot: str(chat_id) -> job seeker or group/channel feed
    "bot_sessions": ("bot_sessions.json", dict),   # Telegram bot: last search results per chat (for "more")
}

# Shared between the bot/site and the collectors: Postgres in cloud mode
SHARED = {"reports", "users", "direct_posts", "sessions", "settings", "sent", "sources_status", "ai_cache",
          "bot_state", "link_tokens", "alerts_daily", "subscribers", "bot_sessions"}


def _in_db(name):
    return bool(config.DATABASE_URL) and name in SHARED


def _snapshot(name):
    """Vercel: the collectors' files are a read-only copy bundled with the deployment."""
    return bool(config.SNAPSHOT_DIR) and name not in SHARED


# Caches can be rebuilt; a corrupt cache is reset instead of blocking the app
REBUILDABLE = {"ai_cache", "sources_status", "bot_state", "link_tokens", "origin_cache", "alerts_daily",
               "programs_watch", "bot_sessions"}


class StorageError(Exception):
    pass


_tlock = threading.RLock()
_depth = threading.local()
LOCK_PATH = os.path.join(config.STORE_DIR, ".lock")
LOCK_TIMEOUT = 30


def _path(name):
    if name not in COLLECTIONS:
        raise KeyError(name)
    return os.path.join(config.STORE_DIR, COLLECTIONS[name][0])


def _lock_file(fh):
    """Exclusive inter-process lock on the first byte of the lock file."""
    deadline = time.time() + LOCK_TIMEOUT
    while True:
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError:
            if time.time() > deadline:
                raise StorageError("timed out waiting for data lock")
            time.sleep(0.05)


def _unlock_file(fh):
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


@contextlib.contextmanager
def locked():
    """Hold the global data lock (re-entrant within a thread); in cloud mode also the database lock."""
    if config.DATABASE_URL:
        from . import pgstore
        with _file_locked(), pgstore.locked():
            yield
    else:
        with _file_locked():
            yield


@contextlib.contextmanager
def _file_locked():
    if config.SNAPSHOT_DIR:   # read-only deployment: no local files to protect
        with _tlock:
            yield
        return
    with _tlock:
        depth = getattr(_depth, "n", 0)
        if depth:
            _depth.n = depth + 1
            try:
                yield
            finally:
                _depth.n -= 1
            return
        os.makedirs(config.STORE_DIR, exist_ok=True)
        fh = open(LOCK_PATH, "a+")
        try:
            _lock_file(fh)
            _depth.n = 1
            try:
                yield
            finally:
                _depth.n = 0
                _unlock_file(fh)
        finally:
            fh.close()


def _read_snapshot(name):
    factory = COLLECTIONS[name][1]
    base = os.path.join(config.SNAPSHOT_DIR, COLLECTIONS[name][0])
    for path, opener in ((base + ".gz", gzip.open), (base, open)):
        if os.path.exists(path):
            with opener(path, "rt", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, factory) else factory()
    return factory()


def read(name):
    if _in_db(name):
        from . import pgstore
        return pgstore.read(name, COLLECTIONS[name][1])
    if _snapshot(name):
        return _read_snapshot(name)
    path = _path(name)
    factory = COLLECTIONS[name][1]
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return factory()
    except (json.JSONDecodeError, UnicodeDecodeError):
        backup = path + ".corrupt-" + datetime.now().strftime("%Y%m%d%H%M%S")
        folder = os.path.dirname(path)
        already = any(f.startswith(os.path.basename(path) + ".corrupt-") for f in os.listdir(folder))
        if not already:  # back up once, not on every read
            shutil.copy2(path, backup)
        if name in REBUILDABLE:
            return factory()
        raise StorageError(f"{os.path.basename(path)} is corrupt; backed up to {os.path.basename(backup)}")
    if not isinstance(data, factory):
        if name in REBUILDABLE:
            return factory()
        raise StorageError(f"{os.path.basename(path)} has the wrong shape")
    return data


_cache = {}
_cache_lock = threading.Lock()
_writes = {}   # name -> writes by this process: two same-size writes within one clock tick still differ


def read_cached(name):
    """Read-only snapshot cached by file mtime/size. Callers must NOT mutate it."""
    if _in_db(name):
        from . import pgstore
        return pgstore.read_cached(name, COLLECTIONS[name][1])
    if _snapshot(name):
        with _cache_lock:
            hit = _cache.get(name)
        if hit is None:
            hit = ("snapshot", _read_snapshot(name))   # never changes during a deployment
            with _cache_lock:
                _cache[name] = hit
        return hit[1]
    path = _path(name)
    try:
        st = os.stat(path)
        key = (st.st_mtime_ns, st.st_size, _writes.get(name, 0))
    except FileNotFoundError:
        return COLLECTIONS[name][1]()
    with _cache_lock:
        hit = _cache.get(name)
        if hit and hit[0] == key:
            return hit[1]
    data = read(name)
    with _cache_lock:
        _cache[name] = (key, data)
    return data


def atomic_write_bytes(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    for attempt in range(5):
        try:
            os.replace(tmp, path)  # atomic on the same volume
            return
        except PermissionError:
            # Windows: a reader (antivirus, editor) may briefly hold the file
            if attempt == 4:
                raise
            time.sleep(0.1 * (attempt + 1))


# Rewritten often by the Telegram bot: no indentation keeps them small and fast to write
COMPACT = {"subscribers", "bot_sessions"}


def write(name, data):
    if _in_db(name):
        from . import pgstore
        return pgstore.write(name, COLLECTIONS[name][1], data)
    if _snapshot(name):
        raise StorageError(f"{name} is read-only here (collected on the collector machine)")
    if name in COMPACT:
        text = json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n"
    else:
        text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    with _file_locked():
        atomic_write_bytes(_path(name), text.encode("utf-8"))
        _writes[name] = _writes.get(name, 0) + 1


@contextlib.contextmanager
def edit(name):
    """Read-modify-write under the lock. Mutate the yielded object in place."""
    if _in_db(name):
        from . import pgstore
        with _tlock, pgstore.edit(name, COLLECTIONS[name][1]) as data:
            yield data
        return
    if _snapshot(name):
        raise StorageError(f"{name} is read-only here (collected on the collector machine)")
    with _file_locked():
        data = read(name)
        yield data
        write(name, data)


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def backup_daily(keep=7):
    """Copy data/store to data/backups/YYYY-MM-DD once a day; keep the last N.
    Not on GitHub Actions: there the store is kept between runs and backed up to the database."""
    if config.ON_CI or config.SNAPSHOT_DIR:
        return False
    day = datetime.now().strftime("%Y-%m-%d")
    target = os.path.join(config.BACKUP_DIR, day)
    if os.path.isdir(target):
        return False
    with _file_locked():
        os.makedirs(config.BACKUP_DIR, exist_ok=True)
        shutil.copytree(config.STORE_DIR, target, ignore=shutil.ignore_patterns(".lock", "*.tmp"))
    old = sorted(d for d in os.listdir(config.BACKUP_DIR) if os.path.isdir(os.path.join(config.BACKUP_DIR, d)))
    for d in old[:-keep]:
        shutil.rmtree(os.path.join(config.BACKUP_DIR, d), ignore_errors=True)
    return True
