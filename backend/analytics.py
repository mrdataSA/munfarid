"""Owner dashboard data: every job ever collected, the bot's events, and the reports built on them.

Cloud mode (DATABASE_URL set):
- rizq.jobs_all keeps one row per job ever collected (light columns, no text). The cloud run
  upserts each source's new jobs as soon as they are stored, then the whole live store at the
  end of the run (only changed rows are rewritten). After ARCHIVE_DAYS a job is archived: kept
  for the reports, never shown again.
- rizq.events is an append-only log of what happens in the bot (start, search, titles and cities
  added/removed, alerts sent, link clicks...). A person is a salted hash of their chat id: the
  reports count people without storing who they are.
A dashboard process keeps a copy of both and only fetches rows newer than the last one it saw,
so the live view (polled every few seconds) costs a tiny query.

Local mode: jobs come from the local store, events from data/store/events.json.
"""

import hashlib
import json
import logging
import re
import threading
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone

from . import config, normalize, storage

log = logging.getLogger("rizq.analytics")

ARCHIVE_DAYS = 90
RIYADH = timezone(timedelta(hours=3), "AST")   # Saudi Arabia has no DST; Vercel runs in UTC
EVENTS_DAYS = 120          # events kept in a dashboard's memory
LOCAL_EVENTS_MAX = 50000
BATCH = 1000

SCHEMA = """
create table if not exists rizq.jobs_all (
  id text primary key,
  title text, company text, city text, specialty text, source text, contract_type text,
  link_kind text, link_host text, url text, apply_url text,
  via_jadarat boolean not null default false,
  via_helper boolean not null default false,
  exp_years int,
  posted_date date,
  first_seen timestamptz not null default now(),
  last_seen timestamptz not null default now(),
  archived boolean not null default false,
  updated_at timestamptz not null default now()
);
create index if not exists jobs_all_updated on rizq.jobs_all (updated_at);
alter table rizq.jobs_all add column if not exists resolved_at timestamptz;
create table if not exists rizq.events (
  id bigserial primary key,
  ts timestamptz not null default now(),
  kind text not null,
  who text,
  data jsonb
);
create index if not exists events_ts on rizq.events (ts);
create table if not exists rizq.archive_outbox (
  id text primary key,
  kind text not null,
  data jsonb not null,
  added_at timestamptz not null default now()
);
create index if not exists archive_outbox_added on rizq.archive_outbox (added_at);
revoke all on all tables in schema rizq from public, anon, authenticated;
revoke all on all sequences in schema rizq from public, anon, authenticated
"""

JOB_COLS = ("id", "title", "company", "city", "specialty", "source", "contract_type", "link_kind", "link_host",
            "url", "apply_url", "via_jadarat", "via_helper", "exp_years", "posted_date", "first_seen", "last_seen",
            "archived", "resolved_at")

LINK_KINDS = {
    "direct": "مباشر من المصدر",
    "relay_resolved": "تخطّينا الإعلانات وجبنا الرابط الأصلي",
    "relay_pending": "بانتظار الرابط الأصلي (لم تُضف بعد)",
}
WAITING = "waiting"   # link_kind of a row whose job went back to the waiting room (collectors/waiting.py)
EXP_BUCKETS = ("حديث تخرج (0-1)", "1-3 سنوات", "3-7 سنوات", "أكثر من 7", "غير مذكورة")


def _db():
    return bool(config.DATABASE_URL)


def _pg():
    from . import pgstore
    return pgstore


_schema_ready = [False]


def init_schema():
    if not _db():
        return
    for stmt in [s.strip() for s in SCHEMA.split(";") if s.strip()]:
        _pg()._run(stmt)
    _schema_ready[0] = True


def _ensure_schema():
    """A dashboard or bot process may run before the cloud run created the tables (idempotent)."""
    if _db() and not _schema_ready[0]:
        init_schema()


# ---------------------------------------------------------------- people (hashed)

def who(chat_id):
    """A stable, salted, short hash of a chat id ("g" prefix for groups/channels)."""
    pepper = config.TELEGRAM_WEBHOOK_SECRET or config.TELEGRAM_BOT_TOKEN or "rizq"
    h = hashlib.sha256(f"{pepper}|{chat_id}".encode("utf-8")).hexdigest()[:11]
    try:
        return ("g" if int(chat_id) < 0 else "u") + h
    except (TypeError, ValueError):
        return "u" + h


WHO_RE = re.compile(r"^[ug][0-9a-f]{11}$")


# ---------------------------------------------------------------- events

def log_events(rows):
    """rows: [(kind, chat_id or who, data dict)]. Never raises: the bot must not fail on analytics."""
    rows = [(k, w if isinstance(w, str) and WHO_RE.match(w) else (who(w) if w is not None else None), d or {})
            for k, w, d in rows if k]
    if not rows:
        return
    try:
        if _db():
            _ensure_schema()
            payload = json.dumps([{"kind": k, "who": w, "data": _pg()._no_nul(d)} for k, w, d in rows],
                                 ensure_ascii=False)
            _pg()._run("insert into rizq.events (kind, who, data) select r.kind, r.who, r.data "
                       "from jsonb_to_recordset(cast(:rows as jsonb)) as r(kind text, who text, data jsonb)",
                       rows=payload)
        else:
            now = storage.now_iso()
            with storage.edit("events") as ev:
                last = ev[-1]["id"] if ev else 0
                for i, (k, w, d) in enumerate(rows, 1):
                    ev.append({"id": last + i, "ts": now, "kind": k, "who": w, "data": d})
                del ev[:-LOCAL_EVENTS_MAX]
    except Exception:
        log.warning("analytics event not saved", exc_info=True)


def log_event(kind_, chat_id=None, **data):
    log_events([(kind_, chat_id, data)])


# ---------------------------------------------------------------- jobs history (cloud run)

def link_kind(job):
    url = job.get("url") or ""
    if normalize.is_relay_url(url):
        return "relay_resolved" if normalize.original_link(job) else "relay_pending"
    return "direct"


def _job_row(j):
    link = normalize.original_link(j) or j.get("url") or ""
    return {"id": j["id"], "title": (j.get("title") or "")[:200], "company": (j.get("company") or "")[:120],
            "city": j.get("city") or "", "specialty": j.get("specialty") or "", "source": j.get("source") or "",
            "contract_type": j.get("contract_type") or "", "link_kind": link_kind(j),
            "link_host": normalize.url_host(link) if link.startswith("http") else ("email" if link else ""),
            "url": (j.get("url") or "")[:1000], "apply_url": (j.get("apply_url") or "")[:1000],
            "via_jadarat": bool(j.get("via_jadarat")), "via_helper": bool(j.get("via_helper")),
            "exp_years": j.get("exp_years") if isinstance(j.get("exp_years"), int) else
            normalize.detect_experience(j.get("title") or "", (j.get("raw_text") or "")[:2500]),
            "posted_date": normalize.parse_date(j.get("posted_date")) or None,
            "collected_at": j.get("collected_at") or None}


UPSERT = """
insert into rizq.jobs_all (id, title, company, city, specialty, source, contract_type, link_kind, link_host, url,
  apply_url, via_jadarat, via_helper, exp_years, posted_date, first_seen, last_seen, updated_at, resolved_at)
select r.id, r.title, r.company, r.city, r.specialty, r.source, r.contract_type, r.link_kind, r.link_host, r.url,
  r.apply_url, r.via_jadarat, r.via_helper, r.exp_years, r.posted_date, coalesce(r.collected_at, now()), now(), now(),
  case when r.link_kind = 'relay_resolved' then now() end
from jsonb_to_recordset(cast(:rows as jsonb)) as r(id text, title text, company text, city text, specialty text,
  source text, contract_type text, link_kind text, link_host text, url text, apply_url text, via_jadarat boolean,
  via_helper boolean, exp_years int, posted_date date, collected_at timestamptz)
on conflict (id) do update set title = excluded.title, company = excluded.company, city = excluded.city,
  specialty = excluded.specialty, link_kind = excluded.link_kind, link_host = excluded.link_host,
  url = excluded.url, apply_url = excluded.apply_url, via_jadarat = excluded.via_jadarat,
  via_helper = rizq.jobs_all.via_helper or excluded.via_helper, exp_years = excluded.exp_years,
  archived = case when rizq.jobs_all.link_kind = 'waiting' then false else rizq.jobs_all.archived end,
  resolved_at = case when excluded.link_kind <> 'relay_resolved' then null
                     else coalesce(rizq.jobs_all.resolved_at, now()) end,
  last_seen = now(), updated_at = now()
where (rizq.jobs_all.title, rizq.jobs_all.city, rizq.jobs_all.specialty, rizq.jobs_all.link_kind,
       rizq.jobs_all.apply_url, rizq.jobs_all.via_jadarat, rizq.jobs_all.exp_years, rizq.jobs_all.company)
  is distinct from (excluded.title, excluded.city, excluded.specialty, excluded.link_kind, excluded.apply_url,
       excluded.via_jadarat, excluded.exp_years, excluded.company)
   or (excluded.via_helper and not rizq.jobs_all.via_helper)
"""


def sync_jobs(jobs, touch=False):
    """Upsert jobs into rizq.jobs_all (only changed rows are rewritten). touch=True also marks
    every given job as still live (last_seen; the row counts as changed once a day)."""
    if not _db() or not jobs:
        return 0
    pg = _pg()
    for i in range(0, len(jobs), BATCH):
        chunk = [_job_row(j) for j in jobs[i:i + BATCH] if j.get("id")]
        pg._run(UPSERT, rows=json.dumps(pg._no_nul(chunk), ensure_ascii=False))
    if touch:
        ids = [j["id"] for j in jobs if j.get("id")]
        for i in range(0, len(ids), 5000):
            pg._run("update rizq.jobs_all set last_seen = now(), updated_at = case when last_seen < now() - "
                    "interval '20 hours' then now() else updated_at end where id = any(cast(:ids as text[]))",
                    ids=ids[i:i + 5000])
    return len(jobs)


def forget_jobs(ids):
    """Jobs moved back to the waiting room (no original link yet): out of every report until they
    are added again (the upsert then brings the row back)."""
    if not _db() or not ids:
        return 0
    ids = list(ids)
    for i in range(0, len(ids), 5000):
        _pg()._run("update rizq.jobs_all set link_kind = 'waiting', archived = true, updated_at = now() "
                   "where id = any(cast(:ids as text[]))", ids=ids[i:i + 5000])
    return len(ids)


# ---------------------------------------------------------------- the PC archive (Raed's E: drive)
# Full jobs that leave the live list ("job:<id>"), and, once the database nears the free plan's
# 500 MB, the dashboard's archived rows ("row:<id>"), wait in rizq.archive_outbox until the helper on
# Raed's PC has saved them (tools/pc_archive.py). Only what the PC confirmed is deleted here.

OUTBOX_MAX = 60000                          # ~75 MB: about 20 days of the PC being off
OFFLOAD_AT_BYTES = 350 * 1024 * 1024        # database size from which archived rows move to the PC
OFFLOAD_BATCH = 5000


def outbox_put(jobs):
    """Queue full jobs for the PC archive. Returns how many were queued."""
    if not _db() or not jobs:
        return 0
    pg = _pg()
    have = (pg._run("select count(*) from rizq.archive_outbox") or [[0]])[0][0]
    room = max(0, OUTBOX_MAX - have)
    if room < len(jobs):
        log.warning("PC archive outbox full (%d waiting): %d jobs not queued", have, len(jobs) - room)
    rows = [{"id": "job:" + j["id"], "data": j} for j in jobs[-room:] if room and j.get("id")]
    for i in range(0, len(rows), BATCH):
        pg._run("insert into rizq.archive_outbox (id, kind, data) select x.id, 'job', x.data "
                "from jsonb_to_recordset(cast(:rows as jsonb)) as x(id text, data jsonb) "
                "on conflict (id) do update set data = excluded.data, added_at = now()",
                rows=json.dumps(pg._no_nul(rows[i:i + BATCH]), ensure_ascii=False))
    return len(rows)


def offload_rows_if_full():
    """Near the free plan's limit, the oldest archived dashboard rows are queued for the PC."""
    if not _db():
        return 0
    pg = _pg()
    size = (pg._run("select pg_database_size(current_database())") or [[0]])[0][0]
    if size < OFFLOAD_AT_BYTES:
        return 0
    rows = pg._run("insert into rizq.archive_outbox (id, kind, data) select 'row:' || j.id, 'row', to_jsonb(j) "
                   "from rizq.jobs_all j where j.archived and j.link_kind is distinct from 'waiting' "
                   "order by j.updated_at limit :n on conflict (id) do nothing returning id", n=OFFLOAD_BATCH)
    log.info("database %d MB: %d archived rows queued for the PC", size // 1048576, len(rows or []))
    return len(rows or [])


def outbox_take(limit=1000):
    if not _db():
        return []
    rows = _pg()._run("select id, kind, data from rizq.archive_outbox order by added_at, id limit :n", n=limit)
    return [{"id": r[0], "kind": r[1], "data": r[2]} for r in rows or []]


def outbox_count():
    return (_pg()._run("select count(*) from rizq.archive_outbox") or [[0]])[0][0] if _db() else 0


def outbox_ack(ids):
    """The PC saved these: delete them from the outbox (and archived rows from the dashboard table)."""
    if not _db() or not ids:
        return 0
    pg = _pg()
    ids = [i for i in ids if isinstance(i, str) and i.startswith(("job:", "row:"))][:5000]
    gone = pg._run("delete from rizq.archive_outbox where id = any(cast(:ids as text[])) returning id, kind",
                   ids=ids) or []
    row_ids = [r[0][4:] for r in gone if r[1] == "row"]
    if row_ids:
        moved = pg._run("delete from rizq.jobs_all where archived and id = any(cast(:ids as text[])) returning id",
                        ids=row_ids) or []
        if moved:
            with storage.edit("bot_state") as st:   # still counted in "everything collected"
                st["rows_on_pc"] = int(st.get("rows_on_pc") or 0) + len(moved)
    return len(gone)


def archive_old(days=ARCHIVE_DAYS):
    """Jobs older than `days` (posted, else first seen) are archived: kept for reports only."""
    if not _db():
        return 0
    rows = _pg()._run("update rizq.jobs_all set archived = true, updated_at = now() where not archived and "
                      "coalesce(posted_date::timestamptz, first_seen) < now() - make_interval(days => :d) "
                      "returning id", d=days)
    return len(rows or [])


# ---------------------------------------------------------------- dashboard copies

_lock = threading.RLock()
_jobs = {"rows": {}, "hw": None, "at": 0.0}
_events = {"rows": [], "last": 0, "at": 0.0}


def _iso(v):
    if isinstance(v, datetime):
        return v.astimezone(RIYADH).isoformat(timespec="seconds")
    if isinstance(v, date):
        return v.isoformat()
    return v


def jobs_rows(max_age=5.0):
    """Every job row (archived included), refreshed incrementally at most every `max_age` seconds."""
    if not _db():
        out = {}
        for j in storage.read("jobs"):
            r = _job_row(j)
            r.update(first_seen=j.get("collected_at") or "", last_seen=j.get("collected_at") or "", archived=False,
                     resolved_at="")
            out[r["id"]] = r
        return out
    with _lock:
        if time.time() - _jobs["at"] < max_age:
            return _jobs["rows"]
        _ensure_schema()
        cols = ", ".join(JOB_COLS) + ", updated_at"
        if _jobs["hw"] is None:
            rows = _pg()._run(f"select {cols} from rizq.jobs_all")
        else:
            rows = _pg()._run(f"select {cols} from rizq.jobs_all where updated_at > :t", t=_jobs["hw"])
        new = dict(_jobs["rows"])
        for r in rows or []:
            d = {c: _iso(v) for c, v in zip(JOB_COLS, r)}
            if d["link_kind"] == WAITING:
                new.pop(d["id"], None)
            else:
                new[d["id"]] = d
            if _jobs["hw"] is None or r[-1] > _jobs["hw"]:
                _jobs["hw"] = r[-1]
        _jobs["rows"], _jobs["at"] = new, time.time()
        return new


def waiting_summary():
    """The waiting room (jobs not added until their original link is found), from the collector."""
    w = storage.read("sources_status").get("_origin_wait")
    return w if isinstance(w, dict) else {}


def job_row(job_id):
    """One job's dashboard row (cloud: a single-row query; local: the store)."""
    if not _db():
        return jobs_rows().get(job_id)
    with _lock:
        if job_id in _jobs["rows"]:
            return _jobs["rows"][job_id]
    _ensure_schema()
    rows = _pg()._run(f"select {', '.join(JOB_COLS)} from rizq.jobs_all where id = :i", i=job_id)
    return {c: _iso(v) for c, v in zip(JOB_COLS, rows[0])} if rows else None


def events_rows(max_age=3.0):
    """Recent events (EVENTS_DAYS), oldest first: [{"id", "ts", "kind", "who", "data"}]."""
    if not _db():
        return list(storage.read("events"))
    with _lock:
        if time.time() - _events["at"] < max_age:
            return _events["rows"]
        _ensure_schema()
        if not _events["last"]:
            rows = _pg()._run("select id, ts, kind, who, data from rizq.events where ts > now() - "
                              "make_interval(days => :d) order by id", d=EVENTS_DAYS)
        else:
            rows = _pg()._run("select id, ts, kind, who, data from rizq.events where id > :i order by id",
                              i=_events["last"])
        new = [{"id": r[0], "ts": _iso(r[1]), "kind": r[2], "who": r[3],
                "data": r[4] if isinstance(r[4], dict) else json.loads(r[4] or "{}")} for r in rows or []]
        cutoff = (datetime.now(RIYADH) - timedelta(days=EVENTS_DAYS)).isoformat()
        merged = [e for e in _events["rows"] if e["ts"] >= cutoff] + new
        if new:
            _events["last"] = new[-1]["id"]
        _events["rows"], _events["at"] = merged, time.time()
        return merged


# ---------------------------------------------------------------- helpers for reports

def _day(ts):
    if not ts:
        return ""
    try:
        d = datetime.fromisoformat(str(ts))
    except ValueError:
        return str(ts)[:10]
    return (d.astimezone(RIYADH) if d.tzinfo else d).date().isoformat()


def _today():
    """Today in Riyadh (the owner's calendar), whatever the server's own time zone is."""
    return datetime.now(RIYADH).date()


def exp_bucket(years):
    if years is None:
        return EXP_BUCKETS[4]
    if years <= 1:
        return EXP_BUCKETS[0]
    if years <= 3:
        return EXP_BUCKETS[1]
    if years <= 7:
        return EXP_BUCKETS[2]
    return EXP_BUCKETS[3]


def _series(days, counter):
    today = _today()
    return [{"day": (today - timedelta(days=i)).isoformat(),
             "n": counter.get((today - timedelta(days=i)).isoformat(), 0)} for i in range(days - 1, -1, -1)]


def _pct(now, before):
    if not before:
        return None
    return round((now - before) * 100.0 / before, 1)


def live_ids():
    from . import catalog
    jobs, _ = catalog.all_jobs(include_demo=False)
    return {j["id"] for j in jobs}


def job_status(row, live):
    if row.get("archived"):
        return "archived"
    return "live" if row["id"] in live else "expired"


STATUS_LABELS = {"live": "حالية", "expired": "منتهية (أقل من 3 أشهر)", "archived": "أرشيف (أكثر من 3 أشهر)"}


def sources_config():
    try:
        with open(config.ROOT + "/collectors/sources.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


# ---------------------------------------------------------------- every source, one by one

CATEGORIES = ("جهات حكومية", "شركات (توظيف مباشر)", "منصات توظيف", "مواقع ناقلة", "قنوات تيليجرام",
              "منصات تواصل", "برامج تدريب")
SOURCE_CATEGORY = {
    "jadarat": "منصات توظيف", "sabbar": "منصات توظيف", "tanqeeb": "منصات توظيف", "linkedin": "منصات توظيف",
    "careerjet": "منصات توظيف", "jooble": "منصات توظيف", "rizqsa": "منصات توظيف",
    "saudijobs24": "مواقع ناقلة", "wzaifs": "مواقع ناقلة", "jadaratnews": "مواقع ناقلة", "linkedksa": "مواقع ناقلة",
    "almowaten": "مواقع ناقلة", "wdeftksa": "مواقع ناقلة", "ewdifh": "مواقع ناقلة",
    "telegram": "قنوات تيليجرام", "x": "منصات تواصل", "programs_watch": "برامج تدريب",
}
GROUP_CATEGORY = {"gov": "جهات حكومية", "company": "شركات (توظيف مباشر)"}
METHOD = {"rss": "RSS", "html": "صفحات الموقع", "telegram": "تيليجرام (قناة عامة)", "linkedin": "LinkedIn (بإذن مكتوب)",
          "careerjet": "API رسمي", "jooble": "API رسمي", "x": "X API الرسمي", "jadarat": "بوابة جدارات",
          "programs_watch": "الصفحات الرسمية", "ats": "بوابة التوظيف"}
ATS_LABEL = {"workable": "Workable", "greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby",
             "sf_csb": "SAP SuccessFactors", "oracle": "Oracle Cloud", "recruitee": "Recruitee", "pinpoint": "Pinpoint",
             "smartrecruiters": "SmartRecruiters", "rendered": "صفحة الوظائف (متصفح)", "jsonld": "صفحة الوظائف",
             "html": "صفحة الوظائف", "rss": "RSS"}


def _employers():
    try:
        with open(config.ROOT + "/collectors/employers.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def _channels():
    try:
        with open(config.ROOT + "/collectors/channels.txt", "r", encoding="utf-8") as f:
            return [ln.strip().lstrip("@") for ln in f if ln.strip() and not ln.strip().startswith("#")]
    except OSError:
        return []


def _site(url):
    url = str(url or "")
    return url if url.startswith(("http://", "https://")) else ""


def sources_catalog():
    """Every place jobs come from, one row each: the big boards, every company and government
    portal, every Telegram channel. With its category, how we read it, its state and live jobs."""
    rows = jobs_rows()
    live = live_ids()
    status = storage.read("sources_status")
    live_src = Counter(r.get("source") or "" for r in rows.values() if r["id"] in live)
    employers = _employers()
    out = []
    for s in sources_config():
        st = status.get(s.get("key")) or {}
        on = s.get("enabled", True)
        state = st.get("status") or ("" if on else "disabled")
        group = s.get("group")
        if s.get("type") == "ats" and group in GROUP_CATEGORY:
            failed = str(st.get("message") or "")
            for e in employers:
                if e.get("group", "company") != group:
                    continue
                e_on = on and e.get("enabled", True)
                e_state = "disabled" if not e_on else ("error" if f"{e['name']}:" in failed else (state or ""))
                label = e.get("source_label") or e["name"]     # = the jobs' "source" (collectors/ats.py)
                out.append({"name": e["name"], "name_en": e.get("name_en", ""), "category": GROUP_CATEGORY[group],
                            "method": ATS_LABEL.get(e.get("ats"), e.get("ats") or ""), "via": s["name"],
                            "url": _site(e.get("url") or e.get("job_base") or (f"https://{e['host']}" if e.get("host") else "")),
                            "status": e_state, "note": e.get("_why_disabled", ""), "jobs_source": label,
                            "company": "", "live": live_src.get(label, 0),
                            "needs_helper": False, "last_run": st.get("last_run", "")})
            continue
        if s.get("type") == "telegram":
            for h in _channels():
                out.append({"name": "@" + h, "name_en": "", "category": "قنوات تيليجرام", "method": METHOD["telegram"],
                            "via": s["name"], "url": f"https://t.me/s/{h}", "status": state if on else "disabled",
                            "note": "", "jobs_source": s["name"], "company": "", "live": None, "needs_helper": False,
                            "last_run": st.get("last_run", "")})
            continue
        url = s.get("url") or s.get("page_url") or s.get("feed") or ""
        if isinstance(url, list):
            url = url[0] if url else ""
        url = str(url).replace("{page}", "1")
        out.append({"name": s["name"], "name_en": "", "category": SOURCE_CATEGORY.get(s["key"], "منصات توظيف"),
                    "method": METHOD.get(s.get("type"), s.get("type", "")), "via": "", "url": _site(url),
                    "status": state, "note": st.get("message", ""), "jobs_source": s["name"], "company": "",
                    "live": live_src.get(s["name"], 0),
                    "needs_helper": s.get("cloud") == "helper" or st.get("status") == "blocked",
                    "last_run": st.get("last_run", "")})
    cats = Counter(r["category"] for r in out)
    working = Counter(r["category"] for r in out if r["status"] not in ("disabled", "skipped", "error", "blocked", "disallowed"))
    return {"sources": out, "total": len(out),
            "working": sum(1 for r in out if r["status"] not in ("disabled", "skipped", "error", "blocked", "disallowed")),
            "categories": [{"name": c, "count": cats.get(c, 0), "working": working.get(c, 0)} for c in CATEGORIES if cats.get(c)]}


# ---------------------------------------------------------------- reports

def bot_status():
    """Is the bot answering now? Telegram's own webhook report + our last handled update."""
    from . import telegram_bot as tg
    st = storage.read("bot_state")
    out = {"enabled": tg.enabled(), "last_update_at": st.get("last_update_at", ""), "webhook": None}
    if tg.enabled():
        try:
            info = _cached("webhook", 30, lambda: tg.call("getWebhookInfo", http_timeout=8))
            err_at = info.get("last_error_date") or 0
            out["webhook"] = {"set": bool(info.get("url")), "pending": info.get("pending_update_count", 0),
                              "last_error": info.get("last_error_message") if err_at else "",
                              "last_error_at": datetime.fromtimestamp(err_at).astimezone().isoformat(timespec="seconds")
                              if err_at else ""}
            recent_err = err_at and time.time() - err_at < 1800
            out["ok"] = bool(info.get("url")) and not recent_err and info.get("pending_update_count", 0) < 50
        except Exception as e:   # Telegram unreachable from here: say so, don't guess
            out["webhook"] = {"error": str(e)[:200]}
            out["ok"] = None
    else:
        out["ok"] = False
    status = storage.read("sources_status")
    runs = [r for r in (status.get("_last_run"), status.get("_last_fast_run")) if isinstance(r, dict)]
    last = max((r.get("finished_at") or "" for r in runs), default="")
    out["cloud_last_run"] = last
    out["cloud_ok"] = bool(last) and (datetime.now().astimezone() - datetime.fromisoformat(last)).total_seconds() < 2.5 * 3600
    helper = _helper_named(st.get("helper_last"))
    out["helper"] = helper
    li = st.get("helper_linkedin") or {}
    out["linkedin"] = li
    out["pc_archive"] = st.get("pc_archive") or {}           # what the PC's E: archive holds
    out["archive_outbox"] = st.get("archive_outbox") or {}   # jobs waiting in the cloud for the PC
    out["linkedin_ok"] = bool(li.get("at")) and not li.get("error") and (
        datetime.now().astimezone() - datetime.fromisoformat(li["at"])).total_seconds() < 2 * 3600
    out["helper_ok"] = bool(helper.get("at")) and (
        datetime.now().astimezone() - datetime.fromisoformat(helper["at"])).total_seconds() < 30 * 3600
    return out


_cache = {}


def _helper_named(helper):
    """The helper's last run with its sources shown by name ("جدارات"), not by key ("jadarat")."""
    helper = dict(helper or {})
    names = {s.get("key"): s.get("name") for s in sources_config()}
    if isinstance(helper.get("sources"), dict):
        helper["sources"] = {names.get(k) or k: v for k, v in helper["sources"].items()}
    return helper


def _cached(key, seconds, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < seconds:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


def _owner_who():
    oc = storage.read("bot_state").get("owner_chat")
    return who(oc) if oc else ""


def _subs():
    """Job seekers and group feeds. The owner (Raed testing his own bot) is not counted."""
    subs = storage.read("subscribers")
    oc = storage.read("bot_state").get("owner_chat")
    users = [s for s in subs.values() if s.get("kind", "user") == "user" and s.get("chat_id") != oc]
    feeds = [s for s in subs.values() if s.get("kind") == "feed"]
    return users, feeds


def _person(e, skip=None):
    """A real person's event (not a group, not the dashboard's own login, which has no who;
    not the owner when `skip` is his code)."""
    w = str(e.get("who") or "")
    return w.startswith("u") and w != skip


def _cities_of(s):
    if isinstance(s.get("cities"), list):
        return [c for c in s["cities"] if c]
    return [s["city"]] if s.get("city") else []


def overview():
    rows = jobs_rows()
    live = live_ids()
    events = events_rows()
    t0 = _today()
    today, yday = t0.isoformat(), (t0 - timedelta(days=1)).isoformat()
    week_ago, two_weeks_ago = (t0 - timedelta(days=7)).isoformat(), (t0 - timedelta(days=14)).isoformat()
    added = Counter(_day(r.get("first_seen")) for r in rows.values())
    week = sum(n for d, n in added.items() if d > week_ago)
    prev_week = sum(n for d, n in added.items() if two_weeks_ago < d <= week_ago)
    status = Counter(job_status(r, live) for r in rows.values())
    live_rows = [r for r in rows.values() if r["id"] in live]
    kinds = Counter(r["link_kind"] for r in live_rows)
    wait = waiting_summary()
    kinds["relay_pending"] = wait.get("count", 0)
    users, feeds = _subs()
    subscribed = [s for s in users if s.get("titles") and s.get("mode") in ("daily", "instant")]
    me = _owner_who()
    events = [e for e in events if not me or e.get("who") != me]   # the owner's own tries are not counted
    ev_today = [e for e in events if _day(e["ts"]) == today]
    people_ever = {e["who"] for e in events if _person(e, me)} | {who(s["chat_id"]) for s in users}
    active7 = {e["who"] for e in events if _person(e, me) and _day(e["ts"]) > week_ago}
    new_users = Counter(_day(s.get("created_at")) for s in users)
    titles = Counter(t["title"] for s in users for t in s.get("titles") or [])
    cities = Counter(c for s in users for c in _cities_of(s))
    return {
        "jobs": {"live": len(live_rows), "today": added.get(today, 0), "yesterday": added.get(yday, 0),
                 "week": week, "week_change": _pct(week, prev_week), "expired": status.get("expired", 0),
                 "archived": status.get("archived", 0),
                 "all": len(rows) + int(storage.read("bot_state").get("rows_on_pc") or 0),
                 "kinds": {k: kinds.get(k, 0) for k in LINK_KINDS},
                 "waiting": {k: wait.get(k, 0) for k in ("count", "resolved_today", "dropped_today", "for_helper")},
                 "via_helper": sum(1 for r in live_rows if r.get("via_helper")),
                 "via_jadarat": sum(1 for r in live_rows if r.get("via_jadarat")),
                 "per_day": _series(30, added)},
        "people": {"tried": len(people_ever), "subscribers": len(subscribed), "with_titles": sum(1 for s in users if s.get("titles")),
                   "active7": len(active7), "new_today": new_users.get(today, 0), "feeds": len(feeds),
                   "per_day": _series(30, new_users)},
        "today": {"searches": sum(1 for e in ev_today if e["kind"] == "search"),
                  "clicks": sum(1 for e in ev_today if e["kind"] == "click"),
                  "alerts": sum(int((e.get("data") or {}).get("n") or 1) for e in ev_today if e["kind"] == "alert"),
                  "starts": sum(1 for e in ev_today if e["kind"] == "start")},
        "problems": _problems(),
        "top_sources": Counter(r["source"] for r in live_rows).most_common(8),
        "top_titles": titles.most_common(8),
        "top_cities": cities.most_common(8),
        "bot": bot_status(),
    }


def _problems():
    """Collectors that failed in their last run (blocked ones the helper takes over are not a problem)."""
    status = storage.read("sources_status")
    helper_keys = {s.get("key") for s in sources_config() if s.get("cloud") == "helper"}
    return sorted(v.get("name") or k for k, v in status.items()
                  if not k.startswith("_") and isinstance(v, dict) and v.get("status") == "error" and k not in helper_keys)


def _match_text(r, q):
    if not q:
        return True
    hay = normalize.norm(" ".join(str(r.get(k) or "") for k in ("title", "company", "source", "city")))
    return all(w in hay for w in normalize.norm(q).split())


def filter_jobs(f):
    """f: {q, cities[], exp[], sources[], kinds[], status[], from, to, helper, jadarat, sort}. Returns rows,
    newest first (sort="resolved": newest original link found first)."""
    rows = jobs_rows()
    live = live_ids()
    cities, exps, srcs = set(f.get("cities") or []), set(f.get("exp") or []), set(f.get("sources") or [])
    kinds, stats = set(f.get("kinds") or []), set(f.get("status") or ["live"])
    d_from, d_to = f.get("from") or "", f.get("to") or ""
    out = []
    for r in rows.values():
        st = job_status(r, live)
        if stats and st not in stats:
            continue
        if cities and r.get("city") not in cities:
            continue
        if srcs and r.get("source") not in srcs:
            continue
        if f.get("companies") and r.get("company") not in f["companies"]:
            continue
        if kinds and r.get("link_kind") not in kinds:
            continue
        if exps and exp_bucket(r.get("exp_years")) not in exps:
            continue
        if f.get("helper") and not r.get("via_helper"):
            continue
        if f.get("jadarat") and not r.get("via_jadarat"):
            continue
        day = _day(r.get("first_seen"))
        if (d_from and day < d_from) or (d_to and day > d_to):
            continue
        if not _match_text(r, f.get("q") or ""):
            continue
        out.append(dict(r, status=st, exp_bucket=exp_bucket(r.get("exp_years")), first_day=day))
    if f.get("sort") == "resolved":
        out.sort(key=lambda r: (r.get("resolved_at") or "", r.get("first_seen") or ""), reverse=True)
    else:
        out.sort(key=lambda r: (r.get("first_seen") or "", r.get("posted_date") or ""), reverse=True)
    return out


def jobs_page(f, page=0, size=50):
    rows = filter_jobs(f)
    facets_src = filter_jobs(dict(f, cities=[], exp=[], sources=[], kinds=[]))
    facets = {"cities": Counter(r["city"] for r in facets_src).most_common(40),
              "sources": Counter(r["source"] for r in facets_src).most_common(60),
              "exp": [(b, sum(1 for r in facets_src if r["exp_bucket"] == b)) for b in EXP_BUCKETS],
              "kinds": [(k, sum(1 for r in facets_src if r["link_kind"] == k)) for k in LINK_KINDS]}
    page = max(0, int(page or 0))
    return {"total": len(rows), "page": page, "size": size, "rows": rows[page * size:(page + 1) * size],
            "facets": facets, "titles": Counter(r["title"] for r in rows).most_common(15),
            "cities": Counter(r["city"] for r in rows).most_common(15)}


def sources_report():
    rows = jobs_rows()
    live = live_ids()
    status = storage.read("sources_status")
    cfg = {s["name"]: s for s in sources_config()}
    week_ago = (_today() - timedelta(days=7)).isoformat()
    today = _today().isoformat()
    per = defaultdict(lambda: {"live": 0, "today": 0, "week": 0, "all": 0, "direct": 0, "relay_resolved": 0,
                               "relay_pending": 0, "via_helper": 0, "via_jadarat": 0, "cities": Counter()})
    for r in rows.values():
        p = per[r.get("source") or ""]
        p["all"] += 1
        d = _day(r.get("first_seen"))
        p["today"] += d == today
        p["week"] += d > week_ago
        if r["id"] in live:
            p["live"] += 1
            p[r.get("link_kind") or "direct"] += 1
            p["via_helper"] += bool(r.get("via_helper"))
            p["via_jadarat"] += bool(r.get("via_jadarat"))
            p["cities"][r.get("city") or ""] += 1
    by_key = {s["key"]: s for s in sources_config()}
    wait = waiting_summary()
    for name, n in (wait.get("by_source") or {}).items():
        per[name or ""]["relay_pending"] = n
    out = []
    for name, p in per.items():
        s = cfg.get(name) or {}
        st = status.get(s.get("key"), {}) if s else {}
        out.append({"source": name, "key": s.get("key", ""), "type": s.get("type", "ats" if not s else ""),
                    "needs_helper": s.get("cloud") == "helper" or st.get("status") == "blocked",
                    "status": st.get("status", ""), "message": st.get("message", ""), "last_run": st.get("last_run", ""),
                    "fetched": st.get("fetched"), **{k: v for k, v in p.items() if k != "cities"},
                    "top_cities": p["cities"].most_common(5)})
    out.sort(key=lambda x: x["live"], reverse=True)
    st_rows = [{"key": k, "name": v.get("name", k), "status": v.get("status", ""), "message": v.get("message", ""),
                "last_run": v.get("last_run", ""), "fetched": v.get("fetched"), "added": v.get("added"),
                "needs_helper": (by_key.get(k) or {}).get("cloud") == "helper" or v.get("status") == "blocked"}
               for k, v in status.items() if not k.startswith("_") and isinstance(v, dict)]
    live_rows = [r for r in rows.values() if r["id"] in live]
    helper = _helper_named(storage.read("bot_state").get("helper_last"))
    return {"sources": out, "collectors": sorted(st_rows, key=lambda x: x["name"]),
            "kinds": dict({k: sum(1 for r in live_rows if r["link_kind"] == k) for k in LINK_KINDS},
                          relay_pending=wait.get("count", 0)),
            "waiting": {k: v for k, v in wait.items() if k != "items"},
            "via_helper": sum(1 for r in live_rows if r.get("via_helper")),
            "via_helper_all": sum(1 for r in rows.values() if r.get("via_helper")),
            "via_jadarat": sum(1 for r in live_rows if r.get("via_jadarat")),
            "helper": helper,
            "helper_sources": [s for s in out if s["needs_helper"]]}


def users_report():
    users, feeds = _subs()
    events = events_rows()
    rows = jobs_rows()
    live = live_ids()
    me = _owner_who()
    events = [e for e in events if not me or e.get("who") != me]
    people_ever = {e["who"] for e in events if _person(e, me)} | {who(s["chat_id"]) for s in users}
    subscribed = [s for s in users if s.get("titles") and s.get("mode") in ("daily", "instant")]
    clicked = {e["who"] for e in events if e["kind"] == "click" and _person(e, me)}
    searches = [e for e in events if e["kind"] == "search"]
    zero = Counter("، ".join((e.get("data") or {}).get("titles") or []) for e in searches
                   if not (e.get("data") or {}).get("results"))
    titles = Counter(t["title"] for s in users for t in s.get("titles") or [])
    cities = Counter(c for s in users for c in _cities_of(s))
    exp = Counter(normalize.EXPERIENCE.get(s.get("exp"), ("أي خبرة",))[0] for s in users if s.get("titles"))
    modes = Counter({"daily": "يومياً", "instant": "فوراً", "off": "متوقفة"}.get(s.get("mode"), "؟") for s in users
                    if s.get("titles"))
    live_titles = [normalize.norm(r.get("title") or "") for r in rows.values() if r["id"] in live]
    demand = []
    for t, n in titles.most_common(25):
        words = normalize.norm(t).split()
        supply = sum(1 for lt in live_titles if all(w in lt for w in words))
        demand.append({"title": t, "people": n, "jobs": supply})
    by_day = defaultdict(Counter)
    for e in events:
        by_day[e["kind"]][_day(e["ts"])] += 1
    clicks = [e for e in events if e["kind"] == "click"]
    click_src = Counter((e.get("data") or {}).get("source") or "" for e in clicks)
    click_jobs = Counter(((e.get("data") or {}).get("title") or "", (e.get("data") or {}).get("source") or "")
                         for e in clicks)
    alerts = sum(int((e.get("data") or {}).get("n") or 1) for e in events if e["kind"] in ("alert", "feed_post"))
    funnel = [("جرّبوا البوت", len(people_ever)),
              ("أضافوا مسمى", sum(1 for s in users if s.get("titles"))),
              ("أكملوا الخطوات الأربع", sum(1 for s in users if s.get("setup_done"))),
              ("مشتركون الآن (تنبيهات مفعّلة)", len(subscribed)),
              ("ضغطوا رابط وظيفة", len(clicked))]
    return {"funnel": funnel, "titles": titles.most_common(30), "cities": cities.most_common(25),
            "exp": exp.most_common(), "modes": modes.most_common(), "demand": demand,
            "zero_results": [(t, n) for t, n in zero.most_common(25) if t],
            "new_per_day": _series(30, Counter(_day(s.get("created_at")) for s in users)),
            "searches_per_day": _series(30, by_day["search"]), "clicks_per_day": _series(30, by_day["click"]),
            "clicks": len(clicks), "click_people": len(clicked), "alerts_sent": alerts,
            "click_sources": click_src.most_common(15),
            "top_clicked": [{"title": t, "source": s, "n": n} for (t, s), n in click_jobs.most_common(15)],
            "feeds": [{"titles": "، ".join(t["title"] for t in f.get("titles") or []) or "كل الوظائف",
                       "city": f.get("city") or "", "posted_today": sum((f.get("posted") or {}).values()),
                       "queue": len(f.get("queue") or [])} for f in feeds],
            "sources_chosen": Counter(x for s in users for x in s.get("sources") or []).most_common(10)}


def live_feed(since_id=0, limit=30):
    events = events_rows()
    new = [e for e in events if e["id"] > since_id][-limit:]
    rows = jobs_rows()
    latest = sorted(rows.values(), key=lambda r: r.get("first_seen") or "", reverse=True)[:12]
    return {"events": [dict(e, label=EVENT_LABELS.get(e["kind"], e["kind"])) for e in reversed(new)],
            "last_id": events[-1]["id"] if events else since_id,
            "latest_jobs": [{"id": r["id"], "title": r["title"], "source": r["source"], "city": r["city"],
                             "first_seen": r.get("first_seen")} for r in latest],
            "jobs_total": len(rows)}


EVENT_LABELS = {"start": "بدأ البوت", "search": "بحث", "title_add": "أضاف مسمى", "title_remove": "حذف مسمى",
                "city_add": "أضاف مدينة", "city_remove": "حذف مدينة", "exp_set": "اختار الخبرة",
                "mode_set": "غيّر التوقيت", "setup_done": "أكمل الخطوات", "alert": "وصلته وظائف",
                "feed_post": "نشر في مجموعة/قناة", "click": "ضغط رابط وظيفة", "delete": "حذف بياناته",
                "feed_on": "فعّل النشر في مجموعة/قناة", "feed_off": "أوقف النشر", "login": "دخول للوحة"}


# ---------------------------------------------------------------- Excel sheets

def sheet_jobs(rows):
    head = ["الرقم", "المسمى", "الجهة", "المدينة", "التخصص", "المصدر", "نوع الرابط", "موقع الرابط", "الخبرة",
            "عبر جهازي", "عبر جدارات", "الحالة", "أول ظهور", "تاريخ النشر", "تاريخ جلب الرابط الأصلي",
            "الرابط الأصلي (بعد)", "رابط الإعلان (قبل)"]
    body = [[r["id"], r["title"], r["company"], r["city"], r["specialty"], r["source"],
             LINK_KINDS.get(r["link_kind"], r["link_kind"]), r.get("link_host", ""), exp_bucket(r.get("exp_years")),
             "نعم" if r.get("via_helper") else "", "نعم" if r.get("via_jadarat") else "",
             STATUS_LABELS.get(r.get("status"), r.get("status", "")), _day(r.get("first_seen")),
             r.get("posted_date") or "", _day(r.get("resolved_at")), r.get("apply_url") or "", r.get("url") or ""]
            for r in rows]
    return ("الوظائف", head, body)


def sheet_sources(rep):
    head = ["المصدر", "الحالية", "جديد اليوم", "جديد 7 أيام", "كل ما جُمع", "مباشر", "تخطّينا الإعلانات",
            "بانتظار الرابط", "عبر جهازي", "عبر جدارات", "يحتاج جهازي", "آخر حالة", "آخر تشغيل", "أكثر المدن"]
    body = [[s["source"], s["live"], s["today"], s["week"], s["all"], s["direct"], s["relay_resolved"],
             s["relay_pending"], s["via_helper"], s["via_jadarat"], "نعم" if s["needs_helper"] else "",
             s["status"], s["last_run"], "، ".join(f"{c or 'غير محدد'} ({n})" for c, n in s["top_cities"])]
            for s in rep["sources"]]
    return ("المصادر", head, body)


def sheet_users():
    users, _ = _subs()
    clicks = Counter(e["who"] for e in events_rows() if e["kind"] == "click")
    head = ["المستخدم (رمز مجهول)", "تاريخ البدء", "آخر نشاط", "المسميات", "المدن", "الخبرة", "التوقيت",
            "أكمل الخطوات", "عدد المصادر المختارة", "ضغطات الروابط"]
    body = []
    for s in sorted(users, key=lambda s: s.get("created_at") or ""):
        w = who(s["chat_id"])
        body.append([w, _day(s.get("created_at")), _day(s.get("updated_at")),
                     "، ".join(t["title"] for t in s.get("titles") or []), "، ".join(_cities_of(s)),
                     normalize.EXPERIENCE.get(s.get("exp"), ("أي خبرة",))[0],
                     {"daily": "يومياً", "instant": "فوراً", "off": "متوقفة"}.get(s.get("mode"), ""),
                     "نعم" if s.get("setup_done") else "", len(s.get("sources") or []), clicks.get(w, 0)])
    return ("المستخدمون", head, body)


def sheet_events():
    head = ["الوقت", "الحدث", "المستخدم (رمز مجهول)", "التفاصيل"]
    body = [[e["ts"], EVENT_LABELS.get(e["kind"], e["kind"]), e.get("who") or "",
             json.dumps(e.get("data") or {}, ensure_ascii=False)[:500]] for e in reversed(events_rows())]
    return ("الأحداث", head, body)


def sheet_pairs(name, head, pairs):
    return (name, head, [list(p) for p in pairs])
