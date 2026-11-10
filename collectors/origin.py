"""Find the ORIGINAL apply link of a job that an aggregator site re-posted.

Arabic job sites (linkedksa, wdeftksa, ...) publish an article per job with the real
"apply" link inside it. For sources marked "origin_from_article": true we open the
article (robots.txt + pacing as usual), pick the apply link, and store it as the
job's `apply_url`. The job keeps its article URL as identity (no duplicates), and
the site's apply button sends the job seeker straight to the original source.

Links are taken as written. Short links (t.co, bit.ly, weks.link, ...) are NOT
resolved, because their robots.txt forbids automated clients. They still take the
visitor straight to the source in one click.
Results are cached per article, so each article is opened at most once.
"""

import re
from urllib.parse import urljoin, urlsplit

from backend import storage

from . import base, dom

CACHE_KEY = "origin_cache"
CACHE_MAX = 6000

APPLY_WORDS = ("للتقديم", "التقديم", "تقدم", "تقدّم", "قدم الآن", "قدّم الآن", "رابط الوظيفة",
               "بوابة التوظيف", "بوابة ال", "apply", "رابط التسجيل", "التسجيل")
SOURCE_WORDS = ("مصدر الخبر", "المصدر")   # weaker: may point to a news post, not the apply page
# Exact hosts (or their subdomains) that are original job pages
ORIGIN_HOSTS = ("linkedin.com", "jadarat.sa", "oraclecloud.com", "successfactors.com", "successfactors.eu",
                "taleo.net", "myworkdayjobs.com", "smartrecruiters.com", "greenhouse.io", "lever.co",
                "workable.com", "talentera.com", "forms.gle", "forms.office.com", "x.com", "twitter.com",
                "bayt.com", "weks.link", "lik.ad", "t.co", "bit.ly", "lnkd.in", "hr.cloud.sap")
ORIGIN_SUFFIXES = (".gov.sa", ".edu.sa", ".med.sa")
NOT_APPLY = ("facebook.com", "whatsapp", "wa.me", "t.me/", "telegram.me", "instagram.com", "snapchat.com",
             "tiktok.com", "youtube.com", "intent/", "sharer", "linkedin.com/share", "linkedin.com/company",
             "statcounter", "google.com/search", "googleads", "doubleclick", "apple.com", "play.google",
             "ahlamontada", "phpbb", "mailto:", "javascript:")
SOCIAL_PROFILE = re.compile(r"^https?://(www\.)?(x|twitter)\.com/[^/]+/?$")


def _origin_like(url):
    """Is this an employer / official portal / original board link (by host, not substring)?"""
    parts = urlsplit(url)
    host = parts.netloc.split(":")[0].removeprefix("www.")
    if any(host == h or host.endswith("." + h) for h in ORIGIN_HOSTS) or host.endswith(ORIGIN_SUFFIXES):
        return True
    if host == "docs.google.com" and parts.path.startswith("/forms"):
        return True
    first = host.split(".")[0]
    return first in ("careers", "career", "jobs", "recruitment") or "/careers" in parts.path.lower()


def pick(html_text, page_url):
    """Best apply link in an article page, or ''."""
    host = re.sub(r"^www\.", "", urlsplit(page_url).netloc.lower())
    best, best_score = "", 0
    for a in dom.parse(html_text).find_all("a"):
        href = urljoin(page_url, a.attrs.get("href", "").strip())
        low = href.lower()
        if not low.startswith(("http://", "https://")) or host in urlsplit(low).netloc:
            continue
        if any(b in low for b in NOT_APPLY) or SOCIAL_PROFILE.match(low):
            continue
        label = (a.text() or "").lower()
        score = 0
        if any(w in label for w in APPLY_WORDS):
            score += 2
        elif any(w in label for w in SOURCE_WORDS):
            score += 1
        if _origin_like(low):
            score += 1
        if score > best_score:  # the first link with the top score wins (article order)
            best, best_score = href, score
    return best


def resolve(items, source):
    """Fill item["apply_url"] from each item's article page (cached, capped per run)."""
    limit = source.get("origin_per_run", 40)
    cache = dict(storage.read(CACHE_KEY))
    found, opened = 0, 0
    for it in items:
        url = it.get("url") or ""
        if not url:
            continue
        if url in cache:
            if cache[url]:
                it["apply_url"] = cache[url]
                found += 1
            continue
        if opened >= limit:
            continue
        opened += 1
        try:
            link = pick(base.fetch(url, min_interval=2.0, jitter=1.5), url)
        except base.Blocked:
            break           # site refused us: stop opening its pages this run
        except base.FetchError:
            continue        # disallowed / dead article: keep the article link
        cache[url] = link
        if link:
            it["apply_url"] = link
            found += 1
    if opened:
        with storage.edit(CACHE_KEY) as c:
            c.update({k: v for k, v in cache.items() if k not in c})
            if len(c) > CACHE_MAX:  # forget the oldest entries
                for k in list(c)[: len(c) - CACHE_MAX]:
                    del c[k]
    return found
