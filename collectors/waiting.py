"""The waiting room: jobs are only added with their ORIGINAL link.

A job collected from a relay site or channel (saudijobs24, joborr, a Telegram post...) whose
original apply link is not known yet is NOT stored with the other jobs. It waits here (the
collector's own "origin_wait" file) and every run, fast or full, starts with these first:
collectors.origin opens their relay pages (ads and "next" pages skipped), and each job whose
original link is found is added right then, alerts included. A relay page that refuses cloud
servers goes to the Saudi helper (Raed's PC, once a day). A page with no original link behind it
(a Telegram post with no link, a site whose terms forbid reading it, a page without an apply link)
is dropped, and so is anything still waiting after MAX_HOURS: those jobs are never shown.

What waits, and what happened to it, is summarised in sources_status["_origin_wait"] (shared)
for the owner's dashboard.
"""

import logging
from collections import Counter
from datetime import datetime

from backend import catalog, normalize, storage

from . import origin

log = logging.getLogger("rizq.waiting")

KEY = "origin_wait"
MAX_HOURS = 48           # a job never waits longer than this: found by then, or dropped
VIEW_ITEMS = 200         # shown on the dashboard (newest first)
FAST_SECONDS = 600       # every run opens waiting relay pages for up to this long, before anything else
FULL_SECONDS = 900


def needs_origin(job):
    """A job whose only link is a relay page (no original link known yet)."""
    return bool(job) and not normalize.original_link(job) and bool(origin._target(job))


def _can_have_origin(start):
    """A Telegram post without a link, or a site whose terms forbid reading it: never an original link."""
    return bool(start) and not origin._on(origin._host(start), origin.NO_FETCH + ("t.me", "telegram.me"))


def hold(jobs, source_name=""):
    """Keep jobs that wait for their original link (never stored with the other jobs)."""
    if not jobs:
        return 0
    now = storage.now_iso()
    cache = storage.read(origin.CACHE_KEY)
    n = 0
    with storage.edit(KEY) as w:
        for j in jobs:
            start = origin._target(j)
            if j["id"] in w or not _can_have_origin(start) or (start in cache and not cache[start]):
                continue    # waiting already, or known to have no original link behind it
            w[j["id"]] = {"job": j, "start": start, "since": now,     # the 48 hours start now, for every job
                          "source": j.get("source") or source_name, "tries": 0}
            n += 1
    return n


def take_from_store():
    """Jobs stored before this rule (relay link only): moved here, out of the jobs list."""
    moved = []
    with storage.edit("jobs") as jobs:
        keep = []
        for j in jobs:
            (moved if needs_origin(j) else keep).append(j)
        if moved:
            jobs[:] = keep
    if moved:
        hold(moved)
        log.info("moved %d stored jobs without an original link to the waiting room", len(moved))
    return moved


def _hours_since(iso):
    try:
        d = datetime.fromisoformat(str(iso))
    except ValueError:
        return 0.0
    now = datetime.now(d.tzinfo) if d.tzinfo else datetime.now()
    return (now - d).total_seconds() / 3600


def process(seconds):
    """Open the waiting relay pages (oldest first, within `seconds`). Returns (ready_jobs, stats):
    ready_jobs carry their original link and are to be added now."""
    w = storage.read(KEY)
    if not w:
        _publish({}, {"resolved": 0, "dropped": 0})
        return [], {"resolved": 0, "dropped": 0, "waiting": 0}
    cache = storage.read(origin.CACHE_KEY)
    known = catalog.original_links_by_url()
    entries = sorted(w.items(), key=lambda kv: kv[1].get("since") or "")
    ready, dropped, items = {}, {}, []
    for jid, e in entries:
        start = e.get("start") or ""
        job = e.get("job") or {}
        link = cache.get(start) or known.get(normalize.canonical_url(start), "")
        if link:
            ready[jid] = link
        elif start in cache:                      # opened before: no original link behind it
            dropped[jid] = "لا يوجد رابط أصلي في الصفحة"
        elif not _can_have_origin(start):
            dropped[jid] = "منشور بلا رابط أصلي"
        elif _hours_since(e.get("since")) > MAX_HOURS:
            dropped[jid] = f"لم نجده خلال {MAX_HOURS} ساعة"
        elif normalize.is_expired(job, catalog.config.JOB_MAX_AGE_DAYS):
            dropped[jid] = "انتهت الوظيفة"
        else:
            items.append({"url": start, "_id": jid})
    if items and seconds > 0:
        origin.resolve(items, limit=len(items), seconds=seconds)
        cache = storage.read(origin.CACHE_KEY)
        for it in items:
            if it.get("apply_url"):
                ready[it["_id"]] = it["apply_url"]
            elif it["url"] in cache:
                dropped[it["_id"]] = "لا يوجد رابط أصلي في الصفحة"
    stale = [w[jid]["start"] for jid, why in dropped.items() if why.startswith("لم نجده") and w.get(jid, {}).get("start")]
    if stale:   # remembered as "no original link": the same page is not held again next time it is listed
        origin._save_cache({u: "" for u in stale})
    now = storage.now_iso()
    out = []
    with storage.edit(KEY) as w:
        tried = {it["_id"] for it in items}
        for jid in tried:
            if jid in w:
                w[jid]["tries"] = w[jid].get("tries", 0) + 1
        for jid, link in ready.items():
            e = w.pop(jid, None)
            if e:
                # added now: counted as new for alerts and daily digests from this moment
                job = dict(e["job"], apply_url=str(link)[:1000], collected_at=now,
                           first_collected_at=e["job"].get("collected_at", ""))
                out.append(job)
        for jid in dropped:
            w.pop(jid, None)
        left = dict(w)
    stats = {"resolved": len(out), "dropped": len(dropped), "waiting": len(left),
             "dropped_reasons": dict(Counter(dropped.values()))}
    _publish(left, stats)
    if out or dropped:
        log.info("waiting room: %d found their original link, %d dropped, %d still waiting",
                 len(out), len(dropped), len(left))
    return out, stats


def _publish(left, stats):
    """The dashboard's view of the waiting room (shared, small)."""
    pending = set((storage.read("inbox").get(origin.INBOX_KEY) or {}).get("pending") or [])
    by_source = Counter(e.get("source") or "" for e in left.values())
    newest = sorted(left.values(), key=lambda e: e.get("since") or "", reverse=True)[:VIEW_ITEMS]
    today = datetime.now().astimezone().date().isoformat()
    with storage.edit("sources_status") as st:
        prev = st.get("_origin_wait") or {}
        same_day = prev.get("day") == today
        st["_origin_wait"] = {
            "count": len(left), "by_source": dict(by_source),
            "oldest": min((e.get("since") or "" for e in left.values()), default=""),
            "for_helper": sum(1 for e in left.values() if e.get("start") in pending),
            "resolved_last_run": stats.get("resolved", 0), "dropped_last_run": stats.get("dropped", 0),
            "day": today,
            "resolved_today": (prev.get("resolved_today", 0) if same_day else 0) + stats.get("resolved", 0),
            "dropped_today": (prev.get("dropped_today", 0) if same_day else 0) + stats.get("dropped", 0),
            "updated_at": storage.now_iso(),
            "items": [{"id": e["job"].get("id"), "title": e["job"].get("title", ""), "company": e["job"].get("company", ""),
                       "city": e["job"].get("city", ""), "source": e.get("source", ""), "url": e.get("start", ""),
                       "since": e.get("since", ""), "tries": e.get("tries", 0),
                       "for_helper": e.get("start") in pending} for e in newest],
        }


def deadline_seconds(fast):
    return FAST_SECONDS if fast else FULL_SECONDS
