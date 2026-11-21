"""Run all enabled collectors, normalize, dedupe, store, notify.

A failing source never stops the others: its status is recorded
(ok / empty / error / blocked / disallowed / skipped) and the run continues.
Only one run at a time (in-process lock + the storage file lock).
"""

import json
import logging
import os
import re
import threading
import time
import traceback
from urllib.parse import urlsplit

from backend import ai, analytics, bot, catalog, config, dedup, enrich, normalize, notifier, storage

from . import apis, ats, base, html_list, jadarat, linkedin, origin, programs_watch, rss, telegram_channels, waiting, x_api

log = logging.getLogger("rizq.collect")
ENRICH_BEFORE_ALERT = 8    # new jobs per source classified by AI before alerting (one Groq call)
ORIGIN_FULL_SECONDS = 900   # relay pages opened per full run (new items + backfill), all sources together
ORIGIN_FAST_SECONDS = 300
ENRICH_BACKFILL = 120      # older unclassified jobs per full run (capped at half the daily budget)
COMPANY_BEFORE_ALERT = 30  # new jobs without an employer: the AI reads the ad for it before alerting
COMPANY_BACKFILL = 200     # stored jobs without an employer, per full run
DEDUP_BEFORE_ALERT = 1000  # candidate pairs per source checked before adding (beyond: the job waits a run)
DEDUP_BACKFILL = 1200      # stored jobs' candidate pairs per full run (capped by dedup.cleanup_share of the daily AI budget)
SOURCES_FILE =os.path.join(os.path.dirname(__file__), "sources.json")
COLLECT_LOCK = os.path.join(storage.config.STORE_DIR, ".collect.lock")

COLLECTORS = {
    "rss": rss.collect,
    "html": html_list.collect,
    "telegram": telegram_channels.collect,
    "linkedin": linkedin.collect,
    "x": x_api.collect,
    "ats": ats.collect,
    "jadarat": jadarat.collect,
    "careerjet": apis.careerjet,
    "jooble": apis.jooble,
    "programs_watch": programs_watch.collect,   # official pages of co-op / scholarship programs (no jobs)
}

_run_lock = threading.Lock()
COMPANY_FROM_TITLE = re.compile(
    r"^(?:اعلان\s+)?((?:شركة|شركه|مجموعة|مجموعه|بنك|مصرف|مستشفى|جامعة|جامعه|وزارة|وزاره|هيئة|هيئه|مؤسسة|مؤسسه|"
    r"مركز|مدارس|مطاعم|صيدليات|مدينة|مدينه)?\s*[^\s].{1,60}?)\s+(?:تعلن|يعلن|توفر|يوفر|تطلب|يطلب|تفتح|يفتح)\b")


HELPER_MAX_ITEMS = 5000


def from_helper(source):
    """Jobs the Saudi helper (tools/saudi_helper.py, on a home connection in Saudi Arabia) fetched
    from a site that refuses cloud servers. Taken out of the shared inbox; empty when it was off."""
    box = storage.read("inbox").get(source["key"]) or {}
    batches = box.get("batches") or []
    # removed by _helper_done() only after these jobs are stored: a crash keeps them for next run
    source["_helper_consumed"] = [b.get("at") for b in batches]
    items = [it for b in batches for it in (b.get("items") or [])][-HELPER_MAX_ITEMS:]
    for it in items:
        it["via_helper"] = True     # fetched by Raed's PC (shown as such on the dashboard)
    if batches and batches[-1].get("filter_jobs") is not None:
        source["filter_jobs"] = batches[-1]["filter_jobs"]
    if batches:
        source["_note"] = f"من المساعد السعودي: {len(batches)} دفعة، آخرها {batches[-1].get('at', '')[:16]}"
    else:
        source["_note"] = "يُجلب من جهازي (المساعد): لا دفعة جديدة منذ الجولة السابقة"
    return items


# Never fetched from a home connection to get past a refusal: LinkedIn has its own polite pacing
# and pauses, X is API-only, and a rate limit (429) means "slow down", not "wrong network"
HELPER_NEVER = ("linkedin", "x")


def helper_can_fetch(source, message=""):
    """May the Saudi helper fetch this source when the cloud is refused? Sites that answer cloud
    servers with 403 / a challenge page only."""
    if source.get("cloud") == "helper":
        return True
    if source.get("type") in HELPER_NEVER or source.get("key") in HELPER_NEVER or source.get("helper") is False:
        return False
    return "429" not in (message or "")


def _helper_done(source):
    done = set(source.pop("_helper_consumed", None) or [])
    if not done:
        return
    with storage.edit("inbox") as inbox:
        box = inbox.get(source["key"])
        if box:
            box["batches"] = [b for b in box.get("batches") or [] if b.get("at") not in done]
            if not box["batches"]:
                del inbox[source["key"]]


def load_sources():
    with open(SOURCES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def is_running():
    return _run_lock.locked()


COMPANY_AFTER = re.compile(r"(?:متوفره?|متاحه?|متوفرة|متاحة|لدى|في)\s+\(?((?:شركة|شركه|مجموعة|مجموعه|بنك|مصرف|مستشفى|جامعة|جامعه|هيئة|هيئه|وزارة|وزاره|مؤسسة|مؤسسه)\s+[^|،,()]{2,60})")


def company_from_title(title):
    title = title or ""
    m = COMPANY_FROM_TITLE.match(title)
    if m:
        return m.group(1).strip()
    m = COMPANY_AFTER.search(title)
    return m.group(1).strip() if m else ""


def _to_job(source, item, ai_result=None):
    text = item.get("raw_text") or ""
    if ai_result is not None:
        if not ai_result.get("is_job"):
            return None
        job = normalize.make_job(
            title=ai_result.get("title") or item.get("title"),
            company=ai_result.get("company") or item.get("company") or company_from_title(item.get("title")),
            city=ai_result.get("city") or item.get("city") or "",
            specialty=ai_result.get("specialty"),
            contract_type=ai_result.get("contract_type"),
            skills=ai_result.get("skills"),
            is_training=ai_result.get("is_training"),
            url=item.get("url"),          # URLs always come from the source, never the model
            source=item.get("source") or source["name"], posted_date=item.get("posted_date"), raw_text=text,
        )
        if job:
            job["nationality"] = ai_result.get("nationality") or job["nationality"]
            job["gender"] = ai_result.get("gender") or job["gender"]
            job["ai_checked"] = True  # the model already classified this post: enrich must not pay twice
        return _with_apply(job, item)
    if item.get("needs_ai") or source.get("filter_jobs", True):
        if not normalize.looks_like_job((item.get("title") or "") + " " + text[:1500]):
            return None
    job = normalize.make_job(
        title=item.get("title"),
        company=item.get("company") or company_from_title(item.get("title")),
        city=item.get("city") or "", url=item.get("url"), source=item.get("source") or source["name"],
        posted_date=item.get("posted_date"), raw_text=text or item.get("title"),
        salary=item.get("salary"),
    )
    return _with_apply(job, item)


def _jadarat_link(url):
    host = urlsplit(url or "").netloc.lower().split(":")[0]
    return host == "jadarat.sa" or host.endswith(".jadarat.sa")


def _with_apply(job, item):
    """Attach the original apply link found by collectors.origin (http/https only).
    A relayed post whose apply link goes to Jadarat is marked "via_jadarat" (shown as
    "جدارات (عبر ناقل)"): Jadarat refuses cloud servers, so its jobs reach us through
    public sites that re-post them, never by fetching Jadarat itself."""
    link = str(item.get("apply_url") or "").strip()
    if job and item.get("via_helper"):
        job["via_helper"] = True
    if job and link.lower().startswith(("http://", "https://")) and link != job.get("url"):
        job["apply_url"] = link[:1000]
        if _jadarat_link(link) and not _jadarat_link(job.get("url")):
            job["via_jadarat"] = True
    return job


def _set_status(key, **fields):
    with storage.edit("sources_status") as st:
        entry = st.setdefault(key, {})
        if fields.get("status") in ("ok",) and fields.get("fetched", 1) == 0:
            fields["status"] = "empty"
        if fields.get("status") == "empty" or fields.get("fetched") == 0:
            entry["zero_streak"] = entry.get("zero_streak", 0) + 1
        elif "fetched" in fields:
            entry["zero_streak"] = 0
        entry.update(fields)


class _CollectLock:
    """Non-blocking inter-process lock so two servers / the CLI never collect at once."""

    def __init__(self):
        self.fh = None

    def acquire(self):
        os.makedirs(os.path.dirname(COLLECT_LOCK), exist_ok=True)
        self.fh = open(COLLECT_LOCK, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self.fh.close()
            self.fh = None
            return False

    def release(self):
        if not self.fh:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self.fh.close()
        self.fh = None


def _store_source(src, items, totals, sent_per_user):
    """Normalize, dedupe-merge and alert for one source's items (called as soon as it finishes).

    Sources run in sources.json order, so direct sources are merged before the
    re-posting aggregators (Careerjet / Jooble last) and keep their own links.
    A job is added only with its original link: one whose only link is a relay page waits in
    collectors/waiting.py until that link is found.
    """
    ai_idx = [i for i, it in enumerate(items) if it.get("needs_ai")]
    ai_results = ai.extract_posts([items[i].get("raw_text") or "" for i in ai_idx]) if ai_idx else []
    ai_map = dict(zip(ai_idx, ai_results))
    jobs = []
    for i, it in enumerate(items):
        try:  # one malformed item must not lose the whole source
            job = _to_job(src, it, ai_map.get(i))
        except Exception:
            log.warning("skipped a malformed item from %s:\n%s", src["key"], traceback.format_exc())
            continue
        if job:
            jobs.append(job)
    known = catalog.original_links_by_url() if any(waiting.needs_origin(j) for j in jobs) else {}
    ready, held = [], []
    for j in jobs:
        if waiting.needs_origin(j):
            link = known.get(normalize.canonical_url(origin._target(j)))
            if link:                       # the same relay page is stored already, with its original link
                j["apply_url"] = link
            else:
                held.append(j)
                continue
        ready.append(j)
    totals["held"] = totals.get("held", 0) + waiting.hold(held, src["name"])
    _set_status(src["key"], jobs=len(ready), held=len(held))
    totals["items"] += len(items)
    _add_jobs(src["key"], ready, totals, sent_per_user)
    totals["ai_used"] = totals["ai_used"] or any(r is not None for r in ai_results)


def _add_jobs(key, jobs, totals, sent_per_user, check_duplicates=True):
    """Store jobs that carry their original link, then alert (shared by sources and the waiting room).
    Every new job is checked for a twin from another source BEFORE it counts as added (dashboard,
    alerts, the bot's copy): a duplicate is removed, a job the AI could not judge waits for the next run."""
    added = catalog.merge_collected(jobs) if jobs else []
    if added:
        try:   # the employer, when the ad names it but the site did not say (before the dashboard and alerts)
            enrich.fill_companies(added, COMPANY_BEFORE_ALERT)
        except Exception:
            log.warning("company search failed for %s:\n%s", key, traceback.format_exc())
    if added and check_duplicates:
        try:   # the same vacancy already stored from another source, in other words or language: one copy
            dupes, unchecked = dedup.check(added, DEDUP_BEFORE_ALERT)
        except Exception:   # the check itself failed: nothing is added unchecked, all wait for the next run
            log.warning("duplicate check failed for %s:\n%s", key, traceback.format_exc())
            dupes, unchecked = [], list(added)
        gone = set(dedup.remove(dupes))
        dedup.hold(unchecked, dedup.HELD_SINCE)
        out = gone | {j["id"] for j in unchecked}
        if out:
            added = [j for j in added if j["id"] not in out]
            totals["duplicates"] = totals.get("duplicates", 0) + len(gone)
            totals["dedup_wait"] = totals.get("dedup_wait", 0) + len(unchecked)
    if added and config.DATABASE_URL:
        try:  # the owner's dashboard sees each new job as soon as it is stored
            analytics.sync_jobs(added)
        except Exception:
            log.warning("dashboard sync failed for %s:\n%s", key, traceback.format_exc())
    if not key.startswith("_"):     # a source (the waiting room has its own summary)
        _set_status(key, added=len(added))
    totals["jobs"] += len(jobs)
    totals["added"] += len(added)
    if added:
        # Fill specialty/skills the rules missed before matching, so these jobs can alert
        try:
            enrich.enrich_jobs(added, ENRICH_BEFORE_ALERT)
        except Exception:
            log.warning("enrichment failed for %s:\n%s", key, traceback.format_exc())
        totals["sent"] += notifier.notify_new_jobs(added, sent_per_user)
        if config.ON_CI:
            catalog.remember_recent(added)   # searchable on Vercel right away (before the next deploy)
        bot.submit_new_jobs(added)  # Telegram bot alerts + feeds, sent from the bot's own thread


def _process_waiting(fast, totals, sent_per_user):
    """Jobs waiting for their original link: moved out of the stored jobs (once), opened first
    in every run, and added (with alerts) as soon as their link is found."""
    moved = waiting.take_from_store()
    if moved and config.DATABASE_URL:
        try:   # the dashboard counts them as waiting now, not as stored jobs
            analytics.forget_jobs([j["id"] for j in moved])
        except Exception:
            log.warning("dashboard cleanup failed:\n%s", traceback.format_exc())
    origin.RUN_BUDGET["until"] = 0.0
    ready, stats = waiting.process(waiting.deadline_seconds(fast))
    totals["origin_found"] = stats["resolved"]
    totals["origin_dropped"] = stats["dropped"]
    if ready:
        _add_jobs("_waiting", ready, totals, sent_per_user)


def _process_dedup_wait(totals, sent_per_user):
    """New jobs the AI could not check last run: checked again first (or added after 24 h)."""
    again, late = dedup.release()
    try:
        if again:
            _add_jobs("_dedup", again, totals, sent_per_user)
        if late:
            log.warning("%d jobs waited 24 h for the duplicate check: added without it", len(late))
            _add_jobs("_dedup", late, totals, sent_per_user, check_duplicates=False)
    except Exception:
        dedup.hold(again + late, dedup.HELD_SINCE)   # nothing lost: back in the queue for the next run
        raise


def run_all(only=None, fast=False):
    """Returns a summary dict. Safe to call from a thread.

    fast=True runs only the sources marked "fast": true in sources.json (quick, light
    sources such as Telegram and the newest LinkedIn jobs), so alerts go out between
    the full runs. It keeps its own timestamps and does not count as a full run.
    """
    if not _run_lock.acquire(blocking=False):
        return {"status": "busy"}
    plock = _CollectLock()
    if not plock.acquire():
        _run_lock.release()
        return {"status": "busy"}
    started = time.time()
    mark = "_last_fast" if fast else ""
    try:
        # Recorded up front so a crashing run does not make the scheduler retry every 10 minutes
        with storage.edit("sources_status") as st:
            st[(mark or "_last") + "_attempt"] = storage.now_iso()
        sources = [s for s in load_sources() if s.get("enabled", True)]
        if fast:
            # the helper's inbox is cheap to read: its jobs go out on every run, not only full ones
            sources = [dict(s, _fast=True) for s in sources
                       if s.get("fast") or (config.ON_CI and s.get("cloud") == "helper")]
        if only:
            sources = [s for s in sources if s["key"] in only]
        totals = {"items": 0, "jobs": 0, "added": 0, "sent": 0, "ai_used": False}
        if config.DATABASE_URL:
            try:  # original links the Saudi helper found behind relay pages that refuse the cloud
                totals["origin_from_helper"] = origin.apply_helper_results()
            except Exception:
                log.warning("helper origin links failed:\n%s", traceback.format_exc())
        sent_per_user = {}  # one alert budget per user for the whole run
        if not only:
            try:   # first, before any source: jobs waiting for their original link (top priority)
                _process_waiting(fast, totals, sent_per_user)
            except Exception:
                log.warning("waiting room failed:\n%s", traceback.format_exc())
            try:   # then the jobs that wait for the AI duplicate check
                _process_dedup_wait(totals, sent_per_user)
            except Exception:
                log.warning("duplicate check queue failed:\n%s", traceback.format_exc())
        # opening relay pages for their original links: at most this long per run (all sources together)
        origin.RUN_BUDGET["until"] = time.time() + (ORIGIN_FAST_SECONDS if fast else ORIGIN_FULL_SECONDS)
        for src in sources:
            t0 = time.time()
            fn = COLLECTORS.get(src.get("type"))
            if src.get("cloud") == "helper" and config.ON_CI:
                fn = from_helper   # the site refuses cloud servers: a Saudi connection fetched it
            if not fn:
                _set_status(src["key"], name=src["name"], status="error", message="unknown type")
                continue
            try:
                items = fn(src) or []
                status, message = "ok", src.pop("_note", "")
            except base.Disallowed as e:
                items, status, message = [], "disallowed", str(e)
            except base.Blocked as e:
                items, status, message = [], "blocked", str(e)
                if config.ON_CI and helper_can_fetch(src, message):
                    # the site refuses cloud servers: the Saudi helper fetches it once a day from home
                    items = from_helper(src)
                    message = f"{message} · {src.pop('_note', '')}"
            except (base.FetchError, x_api.NotConfigured, linkedin.Disabled, apis.NotConfigured) as e:
                items, status, message = [], ("skipped" if isinstance(e, (x_api.NotConfigured, linkedin.Disabled, apis.NotConfigured)) else "error"), str(e)
            except Exception as e:  # a broken collector must not stop the run
                items, status, message = [], "error", repr(e)
                log.error("collector %s crashed:\n%s", src["key"], traceback.format_exc())
            if items:
                try:  # optional extra: a failure here must not drop the collected items
                    # relay pages (ads + "next" pages) -> the original apply link behind them
                    origin.resolve(items, src)
                except Exception:
                    log.warning("origin links for %s failed:\n%s", src["key"], traceback.format_exc())
            _set_status(src["key"], name=src["name"], type=src["type"], status=status, message=message[:300],
                        fetched=len(items), last_run=storage.now_iso(), seconds=round(time.time() - t0, 1))
            log.info("source %s: %s (%d items) %s", src["key"], status, len(items), message)
            if items:
                try:  # store + alert right away, so a new job reaches users without waiting for the run
                    _store_source(src, items, totals, sent_per_user)
                    _helper_done(src)
                except Exception:
                    log.error("storing %s failed:\n%s", src["key"], traceback.format_exc())
            elif src.get("_helper_consumed") is not None:
                _helper_done(src)

        if not fast:
            try:  # cross-source duplicates already stored (AI), newest first, within budget (first: it shares the AI budget)
                gone = dedup.backfill(DEDUP_BACKFILL)
                totals["duplicates"] = totals.get("duplicates", 0) + len(gone)
                if gone and config.DATABASE_URL:
                    analytics.drop_rows(gone)
            except Exception:
                log.warning("duplicate backfill failed:\n%s", traceback.format_exc())
            try:  # stored jobs without an employer: the AI reads the whole ad for it
                totals["companies"] = enrich.backfill_companies(COMPANY_BACKFILL)
            except Exception:
                log.warning("company backfill failed:\n%s", traceback.format_exc())
            try:  # older jobs the rules left as "أخرى" / without skills, newest first, within budget
                totals["enriched"] = enrich.backfill(ENRICH_BACKFILL)
            except Exception:
                log.warning("enrichment backfill failed:\n%s", traceback.format_exc())

        try:
            storage.backup_daily()
        except OSError as e:
            log.warning("backup failed: %s", e)

        summary = {"status": "done", "sources": len(sources), "items": totals["items"],
                   "origin_from_helper": totals.get("origin_from_helper", 0),
                   "origin_found": totals.get("origin_found", 0), "origin_dropped": totals.get("origin_dropped", 0),
                   "held": totals.get("held", 0),
                   "jobs": totals["jobs"], "added": totals["added"], "notified_users": sum(1 for n in sent_per_user.values() if n),
                   "ai_used": totals["ai_used"], "enriched": totals.get("enriched", 0),
                   "companies": totals.get("companies", 0), "duplicates": totals.get("duplicates", 0),
                   "dedup_wait": totals.get("dedup_wait", 0),
                   "seconds": round(time.time() - started, 1), "finished_at": storage.now_iso()}
        if fast:
            summary["fast"] = True
        with storage.edit("sources_status") as st:
            st["_last_fast_run" if fast else "_last_run"] = summary
        log.info("collection done: %s", summary)
        return summary
    finally:
        plock.release()
        _run_lock.release()
