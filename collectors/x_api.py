"""X (Twitter) job posts via the official API v2 recent search.

Scraping x.com is not an option: its robots.txt disallows all bots ("User-agent: *
Disallow: /"), and logged-out pages show nothing, so a scraper would have to log
in / evade protection, which RIZQ's rules forbid. The official API is the only route.

Since February 2026 the API is pay-per-use (about $0.005 per post read), so this
collector keeps the cost bounded:
- it only runs when X_BEARER_TOKEN is set;
- each query remembers the newest post id it saw (since_id), so the same post is
  never paid for twice;
- X_DAILY_POST_CAP caps the posts read per day (default 300, about $1.5/day).
Posts are unstructured -> marked needs_ai. The post itself is the job's source
link; a link inside the post (the employer's apply page) becomes apply_url.
"""

import json
import time
from datetime import date
from urllib.parse import quote, urlsplit

from backend import config, storage

from . import base

SEARCH_URL = ("https://api.twitter.com/2/tweets/search/recent?query={q}&max_results={n}"
              "&tweet.fields=created_at,entities,author_id&expansions=author_id&user.fields=username")
X_HOSTS = ("x.com", "twitter.com", "t.co")
TWITTER_EPOCH_MS = 1288834974657   # post ids are snowflakes: (id >> 22) + epoch = creation time (ms)
SINCE_MAX_AGE_DAYS = 6             # recent search answers 400 to a since_id older than its 7-day window


class NotConfigured(Exception):
    pass


def _is_x_link(url):
    host = urlsplit(url).netloc.lower().rsplit("@", 1)[-1].split(":")[0]
    return host in X_HOSTS or host.endswith(tuple("." + h for h in X_HOSTS))


def _fresh_since(post_id):
    """The stored since_id, or "" when it is too old for recent search (it would fail every run)."""
    try:
        created_ms = (int(post_id) >> 22) + TWITTER_EPOCH_MS
    except (TypeError, ValueError):
        return ""
    return str(post_id) if time.time() * 1000 - created_ms < SINCE_MAX_AGE_DAYS * 86400 * 1000 else ""


def _state():
    return storage.read("sources_status").get("x", {})


def collect(source):
    if not config.X_BEARER_TOKEN:
        raise NotConfigured("لا يوجد مفتاح X API (القراءة مدفوعة حسب الاستخدام).")
    today = date.today().isoformat()
    st = _state()
    used = (st.get("daily") or {}).get(today, 0)
    since = dict(st.get("since_ids") or {})
    budget = max(0, config.X_DAILY_POST_CAP - used)
    if budget < 10:
        raise NotConfigured("وصل الحد اليومي لقراءة منشورات X.")
    items, read = [], 0
    try:
        for query in source.get("queries", []):
            n = max(10, min(100, source.get("max_results", 50), budget - read))
            if budget - read < 10:
                break
            url = SEARCH_URL.format(q=quote(query + " -is:retweet -is:reply"), n=n)
            since_id = _fresh_since(since.get(query))
            if since_id:
                url += "&since_id=" + since_id
            text = base.fetch(url, headers={"Authorization": "Bearer " + config.X_BEARER_TOKEN},
                              check_robots=False, min_interval=2.0)
            data = json.loads(text)
            posts = data.get("data") or []
            read += len(posts)
            newest = (data.get("meta") or {}).get("newest_id")
            if newest:
                since[query] = newest
            users = {u["id"]: u.get("username", "") for u in (data.get("includes") or {}).get("users", [])}
            for t in posts:
                urls = [u.get("expanded_url", "") for u in (t.get("entities") or {}).get("urls", [])]
                # Host check, not substring: "x.com" is inside netflix.com / dropbox.com
                ext = next((u for u in urls if u and not _is_x_link(u)), "")
                handle = users.get(t.get("author_id"), "i")
                items.append({
                    "title": t.get("text", "").split("\n")[0][:140],
                    "raw_text": t.get("text", ""),
                    "url": f"https://x.com/{handle}/status/{t['id']}",
                    "apply_url": ext,
                    "posted_date": t.get("created_at", ""),
                    "needs_ai": True,
                })
    finally:
        with storage.edit("sources_status") as s:
            entry = s.setdefault("x", {})
            entry["daily"] = {today: used + read}
            entry["since_ids"] = since
    source["_note"] = f"{used + read} منشور مقروء اليوم من {config.X_DAILY_POST_CAP}"
    return items
