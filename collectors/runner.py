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

from backend import ai, bot, catalog, config, enrich, normalize, notifier, storage

from . import apis, ats, base, html_list, jadarat, linkedin, origin, programs_watch, rss, telegram_channels, x_api

log = logging.getLogger("rizq.collect")
ENRICH_BEFORE_ALERT = 8    # new jobs per source classified by AI before alerting (one Groq call)
ENRICH_BACKFILL = 120      # older unclassified jobs per full run (capped at half the daily budget)
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
    if batches and batches[-1].get("filter_jobs") is not None:
        source["filter_jobs"] = batches[-1]["filter_jobs"]
    if batches:
        source["_note"] = f"من المساعد السعودي: {len(batches)} دفعة، آخرها {batches[-1].get('at', '')[:16]}"
    else:
        source["_note"] = "يحجب خوادم السحابة: يُجلب فقط عبر مساعد بعنوان سعودي (غير مفعّل حالياً)"
    return items


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
    added = catalog.merge_collected(jobs) if jobs else []
    _set_status(src["key"], jobs=len(jobs), added=len(added))
    totals["items"] += len(items)
    totals["jobs"] += len(jobs)
    totals["added"] += len(added)
    totals["ai_used"] = totals["ai_used"] or any(r is not None for r in ai_results)
    if added:
        # Fill specialty/skills the rules missed before matching, so these jobs can alert
        try:
            enrich.enrich_jobs(added, ENRICH_BEFORE_ALERT)
        except Exception:
            log.warning("enrichment failed for %s:\n%s", src["key"], traceback.format_exc())
        totals["sent"] += notifier.notify_new_jobs(added, sent_per_user)
        if config.ON_CI:
            catalog.remember_recent(added)   # searchable on Vercel right away (before the next deploy)
        bot.submit_new_jobs(added)  # Telegram bot alerts + feeds, sent from the bot's own thread


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
        sent_per_user = {}  # one alert budget per user for the whole run
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
                if items and src.get("origin_from_article"):
                    try:  # optional extra: a failure here must not drop the collected items
                        origin.resolve(items, src)  # original apply links from the article pages
                    except Exception:
                        log.warning("origin links for %s failed:\n%s", src["key"], traceback.format_exc())
                status, message = "ok", src.pop("_note", "")
            except base.Disallowed as e:
                items, status, message = [], "disallowed", str(e)
            except base.Blocked as e:
                items, status, message = [], "blocked", str(e)
            except (base.FetchError, x_api.NotConfigured, linkedin.Disabled, apis.NotConfigured) as e:
                items, status, message = [], ("skipped" if isinstance(e, (x_api.NotConfigured, linkedin.Disabled, apis.NotConfigured)) else "error"), str(e)
            except Exception as e:  # a broken collector must not stop the run
                items, status, message = [], "error", repr(e)
                log.error("collector %s crashed:\n%s", src["key"], traceback.format_exc())
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
            try:  # older jobs the rules left as "أخرى" / without skills, newest first, within budget
                totals["enriched"] = enrich.backfill(ENRICH_BACKFILL)
            except Exception:
                log.warning("enrichment backfill failed:\n%s", traceback.format_exc())

        try:
            storage.backup_daily()
        except OSError as e:
            log.warning("backup failed: %s", e)

        summary = {"status": "done", "sources": len(sources), "items": totals["items"],
                   "jobs": totals["jobs"], "added": totals["added"], "notified_users": sum(1 for n in sent_per_user.values() if n),
                   "ai_used": totals["ai_used"], "enriched": totals.get("enriched", 0),
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
