"""The public job catalog: collected jobs + approved direct posts (+ demo fallback)."""

import json

from . import config, normalize, posts, storage

# Heavy/internal fields not sent to the browser
PRIVATE_FIELDS = ("raw_text", "merge_key", "hash", "collected_at", "ai_checked", "contact_email", "ip_hash", "status")
# The list endpoint stays light; the detail endpoint returns the full text
LIST_ONLY_SKIP = ("description", "requirements")


def _demo_jobs():
    try:
        with open(config.DEMO_JOBS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    for j in data:
        j["is_demo"] = True
    return data


_ids_cache = [None, frozenset()]


def _snapshot_ids(jobs):
    if _ids_cache[0] is not jobs:   # the snapshot list never changes within a deployment
        _ids_cache[0], _ids_cache[1] = jobs, frozenset(j.get("id") for j in jobs)
    return _ids_cache[1]


RECENT_DAYS = 3


def remember_recent(added):
    """Cloud collector: keep new jobs in the database so the bot and site see them before the
    next deployment carries them (older than RECENT_DAYS are dropped)."""
    if not (config.DATABASE_URL and added):
        return
    from datetime import datetime, timedelta
    cutoff = (datetime.now().astimezone() - timedelta(days=RECENT_DAYS)).isoformat(timespec="seconds")
    with storage.edit("recent_jobs") as recent:
        for j in added:
            if j.get("id"):
                recent[j["id"]] = j
        for k in [k for k, j in recent.items() if (j.get("collected_at") or "") < cutoff]:
            del recent[k]


def hidden_ids():
    return set(storage.read_cached("settings").get("hidden_job_ids", []))


def all_jobs(include_demo=True):
    """Returns (jobs, is_demo), newest first. Admin-hidden and expired jobs excluded.

    Approved direct posts age out like collected jobs (dated on approval).
    """
    hidden = hidden_ids()
    direct = posts.public_jobs(hidden)
    stored = storage.read_cached("jobs")
    if config.SNAPSHOT_DIR and config.DATABASE_URL:
        # Vercel: the snapshot is from the last deployment; newer jobs come from the database
        have = _snapshot_ids(stored)
        stored = stored + [j for j in storage.read_cached("recent_jobs").values() if j.get("id") not in have]
    collected = [j for j in stored
                 if j["id"] not in hidden and not normalize.is_expired(j, config.JOB_MAX_AGE_DAYS)]
    jobs = direct + collected
    is_demo = False
    if not jobs and include_demo:
        jobs, is_demo = _demo_jobs(), True
    jobs.sort(key=lambda j: j.get("posted_date") or "", reverse=True)
    return jobs, is_demo


def public_jobs():
    jobs, is_demo = all_jobs()
    out = [{k: v for k, v in j.items() if k not in PRIVATE_FIELDS and k not in LIST_ONLY_SKIP} for j in jobs]
    return {"jobs": out, "demo": is_demo}


def public_job(job_id):
    j = find(job_id)
    return {k: v for k, v in j.items() if k not in PRIVATE_FIELDS} if j else None


def _url_key(url):
    """Same specific ad URL from two sources = same job. Bare home/search pages don't count."""
    from urllib.parse import urlsplit
    if not url or not url.startswith(("http://", "https://")):
        return ""
    canon = normalize.canonical_url(url)
    path = urlsplit(canon).path
    if len(path.strip("/")) < 3 or "/search" in path or path.rstrip("/").endswith(("/jobs", "/s")):
        return ""
    return canon


JADARAT_SOURCE = "جدارات"


def _jadarat_ad_key(job):
    """The Jadarat ad page a relayed job links to (JobDetails?Param=...), else ''. Portal pages
    such as ExploreJobs are shared by many posts and must never merge them."""
    from urllib.parse import parse_qs, urlsplit
    if not job.get("via_jadarat"):
        return ""
    link = job.get("apply_url") or ""
    parts = urlsplit(link)
    if "jobdetails" not in parts.path.lower() or not parse_qs(parts.query).get("Param"):
        return ""
    return _url_key(link)


def merge_collected(new_jobs):
    """Insert collected jobs, dedupe (id, then cross-source merge key), prune.

    Returns the list of newly added job dicts.
    """
    added = []
    with storage.edit("jobs") as jobs:
        by_id = {j["id"]: j for j in jobs}
        by_merge = {j["merge_key"]: j for j in jobs if j.get("merge_key")}
        by_url = {}
        for j in jobs:
            for key in (_url_key(j.get("url")), _jadarat_ad_key(j)):
                if key:
                    by_url.setdefault(key, j)
        for job in new_jobs:
            if not job or normalize.is_expired(job, config.JOB_MAX_AGE_DAYS):
                continue
            url_key = _url_key(job.get("url"))
            # a relay of a Jadarat ad links that ad's own page: same page = same job
            jad_key = _jadarat_ad_key(job)
            existing = (by_id.get(job["id"])
                        or (by_merge.get(job["merge_key"]) if job.get("merge_key") else None)
                        or (by_url.get(url_key) if url_key else None)
                        or (by_url.get(jad_key) if jad_key else None))
            if existing and existing.get("via_jadarat") and job.get("source") == JADARAT_SOURCE \
                    and not job.get("via_jadarat"):
                # the original Jadarat ad arrived after its relay: keep the original, note the relay
                # (not counted as new, so nobody is alerted twice)
                job["also_on"] = [s for s in existing.get("also_on") or [] if s != job["source"]][:7] + [existing["source"]]
                jobs[jobs.index(existing)] = job
                by_id.pop(existing["id"], None)
                by_id[job["id"]] = job
                for index in (by_url, by_merge):
                    for k in [k for k, v in index.items() if v is existing]:
                        index[k] = job
                continue
            if existing:
                if job.get("apply_url") and not existing.get("apply_url") and existing["source"] == job["source"]:
                    existing["apply_url"] = job["apply_url"]
                if existing["source"] != job["source"]:
                    also = existing.setdefault("also_on", [])
                    if job["source"] not in also and len(also) < 8:
                        also.append(job["source"])
                continue
            jobs.append(job)
            by_id[job["id"]] = job
            if job.get("merge_key"):
                by_merge[job["merge_key"]] = job
            if url_key:
                by_url.setdefault(url_key, job)
            if jad_key:
                by_url.setdefault(jad_key, job)
            added.append(job)
        # Prune expired and cap size (keep newest)
        keep = [j for j in jobs if not normalize.is_expired(j, config.JOB_MAX_AGE_DAYS)]
        keep.sort(key=lambda j: j.get("posted_date") or "", reverse=True)
        jobs[:] = keep[:config.MAX_STORED_JOBS]
        kept = {j["id"] for j in jobs}
    return [j for j in added if j["id"] in kept]


def find(job_id):
    jobs, _ = all_jobs()
    return next((j for j in jobs if j.get("id") == job_id), None)
