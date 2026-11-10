"""LinkedIn public guest job listings (no login, jobs only, never personal data).

Raed holds LinkedIn's written permission (CLAUDE.md); keep it saved. Because of
that permission this collector does not apply robots.txt, so it stays OFF until
LINKEDIN_ENABLED=1 is set in .env.

Coverage: every job posted in Saudi Arabia, swept by time window (newest first,
no keywords), every hour in the fast lane. Saudi Arabia gets roughly a few hundred
to ~1,000 new LinkedIn jobs a day, i.e. a handful of pages an hour.

Politeness rules: 20-40 s between pages (slower than a person scrolling), a daily
request cap, and on any block/verification page it STOPS quietly and logs the
reason (HTTP 429: pause 3 h, doubling if it repeats; login wall: 24 h). No proxies,
no evasion, no login.
"""

import re
import time
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

from backend import config, storage

from . import base, dom

# Newest first (sortBy=DD), posted within the time window (f_TPR, seconds; default 24 h)
GUEST_URL = ("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
             "?keywords={kw}&location=Saudi%20Arabia&f_TPR=r{window}&sortBy=DD&start={start}")
COUNT_URL = "https://www.linkedin.com/jobs/search?keywords=&location=Saudi%20Arabia&f_TPR=r86400"
COUNT_RE = re.compile(r'results-context-header__job-count[^>]*>\s*([\d,.+]+)')
JOB_ID = re.compile(r"-(\d{6,})/?$|/view/(\d{6,})")
COMPANIES_KEEP = 400   # hiring companies seen on LinkedIn (names only), to find their own portals later
PAGE_SIZE = 10
PAGE_GAP, PAGE_JITTER = 20.0, 20.0     # 20-40 s between pages
MAX_PAGES_PER_RUN = 25                 # one run never asks more than this
MIN_WINDOW = 3600                      # always look back at least 1 h
WINDOW_MARGIN = 1800                   # overlap between sweeps; repeats are skipped by job id
FIRST_WINDOW = 86400                   # first sweep: the last 24 h
MAX_WINDOW = 3 * 86400                 # after a long pause, look back up to 3 days
GUEST_MAX_START = 990                  # LinkedIn's guest search stops paging around 1,000 results
SEEN_KEEP = 6000                       # recent job ids remembered to know where the last sweep ended
BACKLOG_OVERLAP = 5                    # re-read this many jobs where a backlog resumes (closed jobs shift the list up)
RATE_LIMIT_PAUSE_HOURS = 3


class Disabled(Exception):
    pass


BLOCK_COOLDOWN_HOURS = 24


def _blocked_until():
    return storage.read("sources_status").get("linkedin", {}).get("blocked_until", "")


def _record_block(reason):
    with storage.edit("sources_status") as st:
        entry = st.setdefault("linkedin", {})
        n = entry.get("blocks_in_row", 0) + 1
        hours = _block_hours(reason, n)
        entry["blocked_until"] = (datetime.now() + timedelta(hours=hours)).isoformat(timespec="seconds")
        entry["blocks_in_row"] = n
        entry["last_block_reason"] = reason
    msg = "حظر من LinkedIn (" + reason + "). توقف " + str(hours) + " ساعات."
    if n >= 2:
        msg += " تكرر الحظر " + str(n) + " مرات: راجع الإذن أو أوقف LinkedIn."
    return msg


def _clear_blocks():
    with storage.edit("sources_status") as st:
        entry = st.setdefault("linkedin", {})
        entry["blocks_in_row"] = 0
        entry.pop("blocked_until", None)


def _today_count():
    st = storage.read("sources_status").get("linkedin", {})
    daily = st.get("daily", {})
    return daily.get(date.today().isoformat(), 0)


def _add_count(n):
    with storage.edit("sources_status") as st:
        entry = st.setdefault("linkedin", {})
        today = date.today().isoformat()
        entry["daily"] = {today: entry.get("daily", {}).get(today, 0) + n}


def _remember_companies(items):
    """Count which companies hire in Saudi Arabia on LinkedIn (company names only, no people)."""
    names = [c["company"] for c in items if c.get("company")]
    if not names:
        return
    with storage.edit("sources_status") as st:
        seen = st.setdefault("linkedin", {}).setdefault("companies", {})
        for n in names:
            seen[n] = seen.get(n, 0) + 1
        if len(seen) > COMPANIES_KEEP:
            for n, _ in sorted(seen.items(), key=lambda kv: kv[1])[: len(seen) - COMPANIES_KEEP]:
                del seen[n]


def _clean_link(href):
    p = urlsplit(href or "")
    return urlunsplit((p.scheme, p.netloc, p.path, "", "")) if p.scheme else ""


def parse_cards(html_text):
    root = dom.parse(html_text)
    items = []
    for card in root.find_all("div", cls="base-search-card"):
        title = card.find("h3", cls="base-search-card__title").text()
        if not title:
            continue
        items.append({
            "title": title,
            "company": card.find("h4", cls="base-search-card__subtitle").text(),
            "city": card.find("span", cls="job-search-card__location").text(),
            "posted_date": card.find("time").attrs.get("datetime", ""),
            "url": _clean_link(card.find("a", cls="base-card__full-link").attrs.get("href")),
            "raw_text": title,
            "needs_ai": False,
        })
    return items


def _job_id(url):
    m = JOB_ID.search(url or "")
    return (m.group(1) or m.group(2)) if m else (url or "")


def _block_hours(reason, n):
    """429 = "slow down": short pause that doubles if it repeats. Login wall / 999 / captcha: a full day."""
    if "429" in reason:
        return min(RATE_LIMIT_PAUSE_HOURS * (2 ** max(n - 1, 0)), BLOCK_COOLDOWN_HOURS)
    return BLOCK_COOLDOWN_HOURS


def _fetch_page(window, start):
    url = GUEST_URL.format(kw="", window=window, start=start)
    try:
        html_text = base.fetch(url, check_robots=False, min_interval=PAGE_GAP, jitter=PAGE_JITTER)
    except base.Blocked as e:
        raise base.Blocked(_record_block(str(e)))  # stop quietly; no retries
    cards = parse_cards(html_text)
    low = html_text[:20000].lower()
    if not cards and ("authwall" in low or "checkpoint/" in low or "uas/login" in low):
        raise base.Blocked(_record_block("login wall"))
    return cards


def measure_daily_total():
    """One request a day: LinkedIn's own count of Saudi jobs posted in the last 24 h (for coverage)."""
    with storage.edit("sources_status") as st:
        # marked before asking: a page without the count (e.g. a login wall) is not asked again today
        st.setdefault("linkedin", {})["count_tried"] = date.today().isoformat()
    text = base.fetch(COUNT_URL, check_robots=False, min_interval=PAGE_GAP, jitter=PAGE_JITTER)
    m = COUNT_RE.search(text)
    if not m:
        return None
    total = int(re.sub(r"[^\d]", "", m.group(1)) or 0)
    with storage.edit("sources_status") as st:
        entry = st.setdefault("linkedin", {})
        entry["daily_total"] = {date.today().isoformat(): total}
    return total


def collect(source):
    """Sweep every LinkedIn job posted in Saudi Arabia, newest first, by time window (no keywords).

    Keywords can never cover everything; a time window can. Each run asks for the jobs
    posted since the previous sweep (plus a margin), page by page, and stops at the first
    page that is mostly jobs already seen. If a run hits its page cap first (e.g. after a
    pause) or is stopped by a block/error, the rest is kept as a backlog and continued on
    the next runs.
    """
    if not config.LINKEDIN_ENABLED:
        raise Disabled("معطّل. فعّله من ملف .env (LINKEDIN_ENABLED=1) بوجود الإذن المكتوب.")
    until = _blocked_until()
    if until and until > datetime.now().isoformat(timespec="seconds"):
        raise Disabled("متوقف مؤقتاً بعد حظر حتى " + until.replace("T", " ")[:16])
    budget = max(0, config.LINKEDIN_DAILY_CAP - _today_count())
    if budget == 0:
        raise Disabled("وصل الحد اليومي للطلبات.")
    st = storage.read("sources_status").get("linkedin", {})
    seen = set(st.get("seen_ids") or [])
    now = time.time()
    last = st.get("last_sweep") or 0
    window = int(min(max(now - last + WINDOW_MARGIN, MIN_WINDOW), MAX_WINDOW)) if last else FIRST_WINDOW
    max_pages = min(budget, int(source.get("max_pages_per_run", MAX_PAGES_PER_RUN)))
    items, new_ids, taken = [], [], set()
    requests = pages_ok = 0   # requests sent (counted even when they fail) / pages read
    reached_seen = False
    backlog = st.get("backlog") or None
    old_w = int(min(backlog["window"] + (now - backlog["at"]), MAX_WINDOW)) if backlog else 0
    next_backlog = resume = stopped = None
    empty_first = False

    def keep(cards):
        # skip jobs read before, and repeats inside this run (new jobs push a card onto the next page)
        for c in cards:
            jid = _job_id(c["url"])
            if jid not in seen and jid not in taken:
                taken.add(jid)
                new_ids.append(jid)
                items.append(c)

    try:
        # 1) newest jobs, until we reach what the last sweep already saw
        start = 0
        while requests < max_pages:
            # if this page fails, the rest of the window becomes the backlog (paging continues
            # downward, so the wider of the two windows also covers an older backlog)
            resume = {"start": start, "window": int(min(max(window, old_w), MAX_WINDOW)), "at": now}
            requests += 1
            cards = _fetch_page(window, start)
            pages_ok += 1
            if start == 0 and not cards:
                empty_first = True   # nothing at all: maybe a quiet hour, maybe a silent throttle
            keep(cards)
            known = sum(1 for c in cards if _job_id(c["url"]) in seen)
            if known and known * 2 >= len(cards):
                reached_seen = True   # half the page was read before: the last sweep ended here
                break                 # (one seen card alone may be a reposted job, not the boundary)
            if len(cards) < PAGE_SIZE:
                reached_seen = True   # end of the window: nothing older to fetch
                break
            start += PAGE_SIZE
        newest_count = len(items)
        if not reached_seen:
            # page cap hit before reaching known jobs: keep the rest of this window for later runs
            next_backlog = {"start": start, "window": int(min(max(window, old_w), MAX_WINDOW)), "at": now}
        # 2) continue an older backlog (offsets moved down by the jobs published since; re-read a
        # little before that point because jobs closed meanwhile move the list up)
        elif backlog and requests < max_pages:
            b_window = old_w
            b_start = max(0, int(backlog["start"] + newest_count - BACKLOG_OVERLAP))
            done = False
            while requests < max_pages:
                if b_start > GUEST_MAX_START:
                    done = True   # the guest search cannot page deeper; the rest is out of reach
                    break
                resume = {"start": b_start, "window": b_window, "at": now}
                requests += 1
                cards = _fetch_page(b_window, b_start)
                pages_ok += 1
                keep(cards)
                if len(cards) < PAGE_SIZE:
                    done = True
                    break
                b_start += PAGE_SIZE
            if not done:
                next_backlog = {"start": b_start, "window": b_window, "at": now}
        elif backlog:
            # no requests left this run: the backlog waits, shifted by the jobs published since
            next_backlog = {"start": int(backlog["start"] + newest_count), "window": old_w, "at": now}
    except base.FetchError as e:
        if not pages_ok:
            raise   # nothing read: state untouched, the next run repeats the same sweep
        # stopped mid-sweep (a block is already recorded): keep what was read, resume at the failed page
        next_backlog, stopped = resume, e
    finally:
        if requests:
            _add_count(requests)
    with storage.edit("sources_status") as s:
        entry = s.setdefault("linkedin", {})
        # saved together with the jobs returned now, so an id is never "seen" for a dropped job
        entry["seen_ids"] = ((entry.get("seen_ids") or []) + [i for i in new_ids if i])[-SEEN_KEEP:]
        if not empty_first:
            # an empty first page does not move the window start: the next sweep looks back
            # further instead, so a silently throttled (empty) answer never loses jobs
            entry["last_sweep"] = now
        if next_backlog:
            entry["backlog"] = next_backlog
        else:
            entry.pop("backlog", None)
        daily_new = entry.setdefault("daily_new", {})
        today = date.today().isoformat()
        entry["daily_new"] = {today: daily_new.get(today, 0) + len(items)}
    if stopped is None:
        _clear_blocks()
    _remember_companies(items)
    for c in items:
        c["filter_jobs"] = False
    if stopped is None and not source.get("_fast") and st.get("count_tried") != date.today().isoformat() \
            and _today_count() < config.LINKEDIN_DAILY_CAP:
        try:  # coverage check, once a day; never fails the run
            measure_daily_total()
        except base.Blocked as e:
            _record_block(str(e))   # pause the next sweeps too; this run's jobs are kept
        except base.FetchError:
            pass
        _add_count(1)
    source["filter_jobs"] = False  # LinkedIn listings are always jobs
    note = f"{len(items)} وظيفة جديدة، {requests} طلبات"
    st = storage.read("sources_status").get("linkedin", {})
    today = date.today().isoformat()
    total = (st.get("daily_total") or {}).get(today)
    if total:
        got = (st.get("daily_new") or {}).get(today, 0)
        note += f"، التغطية اليوم: {got} من {total} وظيفة يعلنها LinkedIn لآخر 24 ساعة"
    if stopped is not None:
        note += "، توقف قبل الإكمال: " + str(stopped)
    if next_backlog:
        note += "، وباقي متأخرات تكمل في الجولة الجاية"
    source["_note"] = note
    return items
