"""Find the ORIGINAL apply link of a job that a relay site re-posted.

Arabic job sites and channels (saudijobs24, joborr, dlil, linkedksa, wdeftksa, ...) publish an
article per job full of ads, with the real "apply" link inside it, sometimes only after one or
two "التقديم من هنا" / "التالي" pages on the same site. For every job whose link is a relay page
(backend.normalize.RELAY_HOSTS) we open that page the way a visitor would (robots.txt + pacing as
usual), skip the ads, follow the site's own "apply / next" link up to MAX_HOPS pages, and store the
first link that leaves the relay sites as the job's `apply_url`. The job keeps its article URL as
identity (no duplicates); the bot and the site send the visitor straight to the original source.

- Short links (t.co, bit.ly, weks.link, ...) are taken as written, never opened: their robots.txt
  forbids automated clients, and they reach the source in one click anyway.
- Redirect wrappers (".../go?url=https://...") are unwrapped without opening them.
- Sites whose terms forbid automated reading (NO_FETCH) are never opened.
- A relay that refuses cloud servers is left for the Saudi helper (tools/saudi_helper.py): the URL
  goes to the shared inbox ("_origin"), the helper opens it from a Saudi home connection, and the
  next cloud run applies the link it found (apply_helper_results).
Results are cached per article, so each article is opened at most once.
"""

import base64
import binascii
import re
import time
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from backend import normalize, storage

from . import base, dom

CACHE_KEY = "origin_cache"
CACHE_MAX = 8000
MAX_HOPS = 3
PENDING_MAX = 400               # relay pages waiting for the Saudi helper
INBOX_KEY = "_origin"
HELPER_MODE = False             # True inside tools/saudi_helper.py (nobody to hand blocked pages to)

APPLY_WORDS = ("للتقديم", "التقديم", "تقدم", "تقدّم", "قدم الآن", "قدّم الآن", "رابط الوظيفة",
               "بوابة التوظيف", "بوابة ال", "apply", "رابط التسجيل", "التسجيل")
SOURCE_WORDS = ("مصدر الخبر", "المصدر")   # weaker: may point to a news post, not the apply page
# A short link on the relay's OWN site that leads on towards the apply link ("next" pages)
NEXT_WORDS = ("التقديم من هنا", "للتقديم اضغط", "اضغط هنا للتقديم", "اضغط للتقديم", "رابط التقديم", "التالي",
              "الصفحة التالية", "متابعة", "تابع", "انتقل", "الانتقال", "اضغط هنا", "التقديم", "للتقديم",
              "next", "continue", "apply")
NEXT_MAX_LABEL = 30
# Exact hosts (or their subdomains) that are original job pages
ORIGIN_HOSTS = ("linkedin.com", "jadarat.sa", "oraclecloud.com", "successfactors.com", "successfactors.eu",
                "taleo.net", "myworkdayjobs.com", "smartrecruiters.com", "greenhouse.io", "lever.co",
                "workable.com", "talentera.com", "forms.gle", "forms.office.com", "x.com", "twitter.com",
                "bayt.com", "weks.link", "lik.ad", "t.co", "bit.ly", "lnkd.in", "hr.cloud.sap")
ORIGIN_SUFFIXES = (".gov.sa", ".edu.sa", ".med.sa")
NOT_APPLY = ("facebook.com", "whatsapp", "wa.me", "t.me/", "telegram.me", "instagram.com", "snapchat.com",
             "tiktok.com", "youtube.com", "intent/", "sharer", "linkedin.com/share", "linkedin.com/company/",
             "statcounter", "google.com/search", "googleads", "doubleclick", "googlesyndication", "adservice",
             "taboola", "outbrain", "mgid.com", "propellerads", "adsterra", "/aclk", "apple.com", "play.google",
             "ahlamontada", "phpbb", "mailto:", "javascript:", "/wp-login", "/feed", "/tag/", "/category/")
SOCIAL_PROFILE = re.compile(r"^https?://(www\.)?(x|twitter)\.com/[^/]+/?$")
# Sites whose terms forbid automated reading: never opened (their jobs keep waiting for a direct source)
NO_FETCH = ("shagher-jobs.com",)
WRAP_PARAMS = ("url", "u", "link", "target", "redirect", "redirect_url", "goto", "dest", "destination", "out", "to", "r")


def _host(url):
    return normalize.url_host(url)


def _on(host, domains):
    return any(host == d or host.endswith("." + d) for d in domains)


def _origin_like(url):
    """Is this an employer / official portal / original board link (by host, not substring)?"""
    parts = urlsplit(url)
    host = _host(url)
    if _on(host, ORIGIN_HOSTS) or host.endswith(ORIGIN_SUFFIXES):
        return True
    if host == "docs.google.com" and parts.path.startswith("/forms"):
        return True
    first = host.split(".")[0]
    return first in ("careers", "career", "jobs", "recruitment") or "/careers" in parts.path.lower()


def unwrap(url):
    """'https://relay/go?url=https%3A%2F%2Fjobs.x.com' -> 'https://jobs.x.com' (also base64), else url."""
    try:
        q = parse_qs(urlsplit(url).query)
    except ValueError:
        return url
    for k in WRAP_PARAMS:
        for v in q.get(k) or []:
            v = unquote(v).strip()
            if v.lower().startswith(("http://", "https://")):
                return v
            try:
                dec = base64.b64decode(v + "=" * (-len(v) % 4), validate=False).decode("utf-8", "strict")
            except (binascii.Error, UnicodeDecodeError, ValueError):
                continue
            if dec.lower().startswith(("http://", "https://")):
                return dec.strip()
    return url


def _usable(href):
    low = href.lower()
    return (low.startswith(("http://", "https://")) and not any(b in low for b in NOT_APPLY)
            and not SOCIAL_PROFILE.match(low))


def pick(html_text, page_url):
    """Best apply link that leaves this site in an article page, or ''. Relay sites score lower,
    so an employer link always wins over a link to another re-posting site."""
    host = _host(page_url)
    best, best_score = "", 0
    for a in dom.parse(html_text).find_all("a"):
        href = unwrap(urljoin(page_url, a.attrs.get("href", "").strip()))
        h = _host(href)
        if not h or h == host or h.endswith("." + host) or not _usable(href):
            continue
        label = (a.text() or "").lower()
        score = 0
        if any(w in label for w in APPLY_WORDS):
            score += 2
        elif any(w in label for w in SOURCE_WORDS):
            score += 1
        if _origin_like(href):
            score += 1
        if score and normalize.is_relay_url(href):
            score -= 1.5
        if score > best_score:  # the first link with the top score wins (article order)
            best, best_score = href, score
    return best


def next_link(html_text, page_url, seen=()):
    """The relay's own short "apply / next" link that leads on to the apply page, or ''."""
    host = _host(page_url)
    here = normalize.canonical_url(page_url)
    for a in dom.parse(html_text).find_all("a"):
        href = urljoin(page_url, a.attrs.get("href", "").strip())
        label = " ".join((a.text() or "").split()).lower()
        if not label or len(label) > NEXT_MAX_LABEL or not any(w in label for w in NEXT_WORDS):
            continue
        h = _host(href)
        if h != host or not _usable(href):
            continue
        canon = normalize.canonical_url(href)
        if canon == here or canon in seen or len(urlsplit(href).path.strip("/")) < 2:
            continue
        return href
    return ""


def follow(url, budget_until=None):
    """Open a relay page and walk to the original link. Returns (link, state):
    ("https://...", "ok") | ("", "none") not found | ("", "blocked") the site refused us |
    ("", "skip") never opened (terms, Telegram post, out of time)."""
    cur, seen = url, set()
    for _ in range(MAX_HOPS + 1):
        cur = unwrap(cur)
        if not normalize.is_relay_url(cur):
            return (cur, "ok") if cur != url else ("", "none")
        host = _host(cur)
        if _on(host, NO_FETCH) or _on(host, ("t.me", "telegram.me")):
            return "", "skip"
        if budget_until and time.time() > budget_until:
            return "", "skip"
        if len(seen) >= MAX_HOPS:
            break
        seen.add(normalize.canonical_url(cur))
        try:
            page = base.fetch(cur, min_interval=2.0, jitter=1.5)
        except base.Blocked:
            return "", "blocked"
        except base.FetchError:   # disallowed by robots.txt / dead page
            return "", "none"
        link = pick(page, cur)
        if link and not normalize.is_relay_url(link):
            return link, "ok"
        cur = link or next_link(page, cur, seen)
        if not cur:
            return "", "none"
    return "", "none"


def _target(item):
    """The relay page to resolve for one collected item, or ''."""
    apply = str(item.get("apply_url") or "")
    if apply:
        return apply if normalize.is_relay_url(apply) else ""
    url = str(item.get("url") or "")
    return url if normalize.is_relay_url(url) else ""


def _save_cache(new):
    if not new:
        return
    with storage.edit(CACHE_KEY) as c:
        c.update(new)
        if len(c) > CACHE_MAX:  # forget the oldest entries
            for k in list(c)[: len(c) - CACHE_MAX]:
                del c[k]


def queue_for_helper(urls):
    """Relay pages that refused the cloud: the Saudi helper opens them from a home connection."""
    urls = [u for u in urls if u]
    if not urls:
        return
    with storage.edit("inbox") as inbox:
        box = inbox.setdefault(INBOX_KEY, {})
        pending = [u for u in box.get("pending") or [] if u not in urls]
        box["pending"] = (pending + urls)[-PENDING_MAX:]


RUN_BUDGET = {"until": 0.0}      # one time budget shared by all sources of a cloud run (set by the runner)


def resolve(items, source=None, limit=None, seconds=240):
    """Fill item["apply_url"] for every item whose link is a relay page (cached, capped per run).
    Returns how many items now have an original link."""
    if RUN_BUDGET["until"]:
        seconds = min(seconds, max(0.0, RUN_BUDGET["until"] - time.time()))
    limit = limit if limit is not None else (source or {}).get("origin_per_run", 40)
    cache = storage.read(CACHE_KEY)
    found, opened, new, blocked_hosts, for_helper = 0, 0, {}, set(), []
    until = time.time() + seconds
    for it in items:
        start = _target(it)
        if not start:
            continue
        if start in cache or start in new:
            link = cache.get(start, new.get(start))
            if link:
                it["apply_url"] = link
                found += 1
            continue
        host = _host(start)
        if _on(host, NO_FETCH) or _on(host, ("t.me", "telegram.me")):
            new[start] = ""          # never opened: no original link behind it
            continue
        if opened >= limit or time.time() > until:
            continue
        if host in blocked_hosts:
            for_helper.append(start)
            continue
        opened += 1
        link, state = follow(start, until)
        if state == "blocked":
            blocked_hosts.add(host)
            for_helper.append(start)
            continue
        if state == "skip":
            continue
        new[start] = link
        if link:
            it["apply_url"] = link
            found += 1
    _save_cache(new)
    if for_helper and not HELPER_MODE:
        queue_for_helper(for_helper)
    return found


def resolve_pending(seconds):
    """Saudi helper: open the relay pages the cloud could not, for up to `seconds`.
    Results go back through the shared inbox. Returns how many links were found."""
    box = storage.read("inbox").get(INBOX_KEY) or {}
    pending = list(box.get("pending") or [])
    until, done, tried = time.time() + seconds, {}, []
    for url in pending:
        if time.time() > until:
            break
        link, state = follow(url, until)
        if state == "skip" and time.time() > until:
            break
        tried.append(url)
        if state != "blocked":
            done[url] = link
    if tried:
        with storage.edit("inbox") as inbox:
            b = inbox.setdefault(INBOX_KEY, {})
            b["pending"] = [u for u in b.get("pending") or [] if u not in tried]
            b["done"] = dict(b.get("done") or {}, **done)
    return sum(1 for v in done.values() if v)


def apply_helper_results():
    """Cloud: store the links the Saudi helper found (cache + jobs already stored)."""
    box = storage.read("inbox").get(INBOX_KEY) or {}
    done = box.get("done") or {}
    if not done:
        return 0
    _save_cache(done)
    from backend import catalog
    n = catalog.set_apply_links({u: l for u, l in done.items() if l})
    with storage.edit("inbox") as inbox:
        b = inbox.get(INBOX_KEY) or {}
        left = {k: v for k, v in (b.get("done") or {}).items() if k not in done}
        if left or b.get("pending"):
            b["done"] = left
            inbox[INBOX_KEY] = b
        else:
            inbox.pop(INBOX_KEY, None)
    return n


def backfill_stored(limit=100, seconds=420):
    """Jobs stored before their original link was known (newest first): resolve and store it."""
    from backend import catalog
    todo = []
    for j in storage.read("jobs"):          # kept newest first
        start = _target(j)
        if start and not normalize.original_link(j):
            todo.append({"url": start, "_start": start})
    if not todo:
        return 0
    resolve(todo, limit=limit, seconds=seconds)
    links = {it["_start"]: it["apply_url"] for it in todo if it.get("apply_url")}
    return catalog.set_apply_links(links) if links else 0
