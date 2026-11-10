"""Postgres backend for the shared collections (cloud mode, Supabase).

Each collection is stored as rows of rizq.rows: one row per key for dict collections
(so changing one subscriber writes one row), or a single row "*" for list collections.
Every write takes a new number from the rizq.ver sequence, and all writes happen under
one advisory lock, so numbers grow in commit order. A process keeps a copy of each
collection and only fetches rows newer than the last number it saw.

storage.py decides which collections live here; callers keep using storage.read/edit.
"""

import json
import logging
import os
import ssl
import sys
import threading
import time
import urllib.parse

from . import config

log = logging.getLogger("rizq.pg")

LOCK_ID = 7461001          # pg_advisory_xact_lock key shared by every writer
# Supabase signs its database certificates with its own root CA (public certificate, bundled)
SUPABASE_CA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "certs", "supabase-root-2021.crt")
SCHEMA = """
create schema if not exists rizq;
create sequence if not exists rizq.ver;
create table if not exists rizq.rows (
  coll text not null,
  key text not null,
  value jsonb,
  deleted boolean not null default false,
  ver bigint not null,
  updated_at timestamptz not null default now(),
  primary key (coll, key)
);
create index if not exists rows_coll_ver on rizq.rows (coll, ver);
create table if not exists rizq.blobs (
  name text primary key,
  data bytea not null,
  updated_at timestamptz not null default now()
);
revoke all on schema rizq from public, anon, authenticated;
revoke all on all tables in schema rizq from public, anon, authenticated;
revoke all on all sequences in schema rizq from public, anon, authenticated
"""


class PgError(Exception):
    pass


_conn = None
_conn_lock = threading.RLock()     # one connection per process, used by one thread at a time
_mirror = {}                       # coll -> dict (or list) as last read; replaced, never mutated
_hw = {}                           # coll -> highest ver seen
_tx = threading.local()


def _ssl_context():
    """Full verification (certificate chain + host name): public CAs plus Supabase's root CA."""
    ctx = ssl.create_default_context()
    if os.path.exists(SUPABASE_CA):
        ctx.load_verify_locations(cafile=SUPABASE_CA)
    return ctx


# Nothing may wait forever: a stuck writer would hold everyone's lock (seconds)
LOCK_TIMEOUT, STATEMENT_TIMEOUT, IDLE_TX_TIMEOUT = 20, 40, 60


def _connect():
    import pg8000.native  # only needed in cloud mode
    u = urllib.parse.urlparse(config.DATABASE_URL)
    conn = pg8000.native.Connection(
        user=urllib.parse.unquote(u.username or ""), password=urllib.parse.unquote(u.password or ""),
        host=u.hostname, port=u.port or 5432, database=(u.path or "/postgres").lstrip("/") or "postgres",
        ssl_context=_ssl_context(), timeout=STATEMENT_TIMEOUT + 20, application_name="munfarid")
    conn.run(f"set lock_timeout = '{LOCK_TIMEOUT}s'")
    conn.run(f"set statement_timeout = '{STATEMENT_TIMEOUT}s'")
    conn.run(f"set idle_in_transaction_session_timeout = '{IDLE_TX_TIMEOUT}s'")
    return conn


def _drop_connection():
    global _conn
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
    _conn = None
    _mirror.clear()   # the copies may hold rows of a transaction that never committed
    _hw.clear()


def _run(sql, **params):
    """Run one statement. Outside a transaction a dropped connection is retried once; inside one
    it never is (a new connection would silently be outside the transaction and the lock)."""
    import pg8000.exceptions
    with _conn_lock:
        in_tx = getattr(_tx, "depth", 0) > 0
        for attempt in range(2):
            if _conn is None:
                if in_tx:
                    raise PgError("database connection lost inside a transaction")
                globals()["_conn"] = _connect()
            try:
                return _conn.run(sql, **params)
            except (pg8000.exceptions.InterfaceError, OSError, ssl.SSLError) as e:
                _drop_connection()
                if attempt or in_tx:
                    raise PgError(f"database connection lost: {e}")
                time.sleep(0.5)


def init_schema():
    for stmt in [s.strip() for s in SCHEMA.split(";") if s.strip()]:
        _run(stmt)


# ---------------------------------------------------------------- lock / transaction

class _Locked:
    """Re-entrant: BEGIN + advisory lock on the first entry, COMMIT (or ROLLBACK) on the last exit."""

    def __enter__(self):
        _conn_lock.acquire()
        depth = getattr(_tx, "depth", 0)
        if depth == 0:
            try:
                _run("select 1")              # (re)connect now if an idle connection was closed
                _tx.depth = 1                 # from here on nothing is retried on a new connection
                _run("begin")
                _run("select pg_advisory_xact_lock(:k)", k=LOCK_ID)
            except BaseException:
                _tx.depth = 0
                _abort()
                _conn_lock.release()
                raise
        else:
            _tx.depth = depth + 1
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            _tx.depth -= 1
            if _tx.depth == 0:
                if exc_type is None:
                    try:
                        _tx.depth = 1         # commit is never retried on another connection
                        _run("commit")
                    except BaseException:
                        _abort()
                        raise
                    finally:
                        _tx.depth = 0
                else:
                    _abort()
        finally:
            _conn_lock.release()
        return False


def _abort():
    """Roll back; if even that fails, drop the connection. The copies are cleared either way."""
    try:
        if _conn is not None:
            _tx.depth = 1
            _conn.run("rollback")
    except Exception:
        _drop_connection()
    finally:
        _tx.depth = 0
        _mirror.clear()
        _hw.clear()


def locked():
    return _Locked()


# ---------------------------------------------------------------- reading

def _sync(coll, factory):
    """Bring this process's copy of coll up to date; returns it (do not mutate)."""
    with _conn_lock:
        hw = _hw.get(coll, 0)
        rows = _run("select key, value::text, deleted, ver from rizq.rows "
                    "where coll = :c and ver > :hw order by ver", c=coll, hw=hw)
        cur = _mirror.get(coll)
        if cur is None:
            cur = factory()
        if not rows:
            _mirror[coll] = cur
            return cur
        if factory is list:
            for key, value, deleted, ver in rows:
                if key == "*":
                    cur = [] if deleted or value is None else json.loads(value)
                hw = max(hw, ver)
        else:
            cur = dict(cur)   # copy-on-write: readers holding the old dict are not disturbed
            for key, value, deleted, ver in rows:
                if deleted:
                    cur.pop(key, None)
                else:
                    cur[key] = json.loads(value)
                hw = max(hw, ver)
        _mirror[coll], _hw[coll] = cur, hw
        return cur


def read_cached(coll, factory):
    return _sync(coll, factory)


def read(coll, factory):
    return json.loads(json.dumps(_sync(coll, factory), ensure_ascii=False))   # a private deep copy


# ---------------------------------------------------------------- writing

def _store(coll, factory, new):
    """Write the difference between this process's copy and `new` (call under locked())."""
    old = _sync(coll, factory)
    if factory is list:
        if new != old:
            _upsert(coll, [("*", new)])
    else:
        changed = [(k, v) for k, v in new.items() if k not in old or old[k] != v]
        gone = [k for k in old if k not in new]
        if changed:
            _upsert(coll, changed)
        if gone:
            _run("update rizq.rows set deleted = true, value = null, ver = nextval('rizq.ver'), "
                 "updated_at = now() where coll = :c and key = any(:keys)", c=coll, keys=gone)
    _sync(coll, factory)


def _no_nul(v):
    """Postgres jsonb refuses the NUL character (PDF text and scraped pages sometimes have it)."""
    if isinstance(v, str):
        return v.replace("\x00", "") if "\x00" in v else v
    if isinstance(v, dict):
        return {_no_nul(k): _no_nul(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_no_nul(x) for x in v]
    return v


def _upsert(coll, items):
    for start in range(0, len(items), 500):
        chunk = [{"k": _no_nul(str(k)), "v": _no_nul(v)} for k, v in items[start:start + 500]]
        _run("insert into rizq.rows (coll, key, value, deleted, ver, updated_at) "
             "select :c, e->>'k', e->'v', false, nextval('rizq.ver'), now() "
             "from jsonb_array_elements(cast(:items as jsonb)) as e "
             "on conflict (coll, key) do update set value = excluded.value, deleted = false, "
             "ver = excluded.ver, updated_at = now()",
             c=coll, items=json.dumps(chunk, ensure_ascii=False))


class _Edit:
    def __init__(self, coll, factory):
        self.coll, self.factory = coll, factory
        self.lock = _Locked()

    def __enter__(self):
        self.lock.__enter__()
        try:
            self.data = read(self.coll, self.factory)
        except BaseException:
            self.lock.__exit__(*sys.exc_info())
            raise
        return self.data

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            try:
                _store(self.coll, self.factory, self.data)
            except BaseException:
                self.lock.__exit__(*sys.exc_info())
                raise
        return self.lock.__exit__(exc_type, exc, tb)


def edit(coll, factory):
    return _Edit(coll, factory)


def write(coll, factory, data):
    with _Locked():
        _store(coll, factory, data)


def purge_deleted(days=3):
    """Drop rows deleted more than N days ago (every running copy has seen them by then)."""
    _run("delete from rizq.rows where deleted and updated_at < now() - make_interval(days => :d)", d=days)


# ---------------------------------------------------------------- blobs (backups)

def put_blob(name, data):
    _run("insert into rizq.blobs (name, data, updated_at) values (:n, :d, now()) "
         "on conflict (name) do update set data = excluded.data, updated_at = now()", n=name, d=data)


def get_blob(name):
    rows = _run("select data from rizq.blobs where name = :n", n=name)
    return bytes(rows[0][0]) if rows else None
