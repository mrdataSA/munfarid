"""Jadarat (jadarat.sa), the national jobs portal, read from its PUBLIC "Explore jobs" page.

The page is a JavaScript app, so it is opened in headless Chromium and read the way a
visitor sees it: job cards, then the "next page" button. No login, no private API,
no personal data (jobs only).

Politeness: a few pages per run (newest first), a random 3-7 s pause between pages,
one browser at a time. A challenge/verification page stops the run quietly.
"""

import random
import re
from datetime import date

from backend import config

from . import base

EXPLORE_URL = "https://jadarat.sa/Jadarat/ExploreJobs"
LINK_SEL = "a[href*='JobDetails?Param=']"

# For each job link, climb to the largest ancestor that holds only this one job
CARDS_JS = """els => els.map(a => {
  let card = a;
  for (let i = 0; i < 12 && card.parentElement &&
       card.parentElement.querySelectorAll("a[href*='JobDetails?Param=']").length === 1; i++) {
    card = card.parentElement;
  }
  return {href: a.href, title: (a.innerText || '').trim(), card: (card.innerText || '').trim()};
})"""


def hijri_to_gregorian(y, m, d):
    """Arithmetic (tabular) Islamic calendar -> Gregorian. Within 1-2 days of Umm al-Qura."""
    import math
    jd = d + math.ceil(29.5 * (m - 1)) + (y - 1) * 354 + (3 + 11 * y) // 30 + 1948439
    # Julian day -> Gregorian (Fliegel & Van Flandern)
    l = jd + 68569
    n = 4 * l // 146097
    l = l - (146097 * n + 3) // 4
    i = 4000 * (l + 1) // 1461001
    l = l - 1461 * i // 4 + 31
    j = 80 * l // 2447
    day = l - 2447 * j // 80
    l = j // 11
    month = j + 2 - 12 * l
    year = 100 * (n - 49) + i + l
    return date(year, month, day)


DATE_RE = re.compile(r"تاريخ النشر\s*:?\s*(\d{1,2})/(\d{1,2})/(\d{4})")


def parse_card(c):
    lines = [x.strip() for x in c["card"].splitlines() if x.strip()]
    title = c["title"] or (lines[0] if lines else "")
    if not title:
        return None
    company = lines[1] if len(lines) > 1 and lines[1] != title else ""
    posted, city, sector, contract = "", "", "", ""
    for i, line in enumerate(lines):
        m = DATE_RE.search(line)
        if m:
            d, mo, y = map(int, m.groups())
            try:
                g = hijri_to_gregorian(y, mo, d) if y < 1600 else date(y, mo, d)
                posted = min(g, date.today()).isoformat()  # tabular calendar can run a day ahead
            except ValueError:
                posted = ""
            tail = lines[i + 1:i + 4]
            city = tail[0] if len(tail) > 0 else ""
            sector = tail[1] if len(tail) > 1 else ""
            contract = tail[2] if len(tail) > 2 else ""
            break
    desc_lines = [x for x in lines[2:] if not DATE_RE.search(x) and not x.startswith("الوظائف المتاحة")
                  and x not in ("التفاصيل", "سجل الدخول للتقديم", city, sector, contract)]
    text = "\n".join(desc_lines[:6] + [sector, contract])
    return {"title": title, "company": company, "city": city, "posted_date": posted,
            "url": c["href"], "raw_text": text, "sector": sector, "needs_ai": False}


# Jadarat splits jobs into tabs. The default tab is the private sector; the others
# hold civil-service (ministries), semi-government and contract jobs, which open in
# announcement windows (often empty between them).
EXTRA_TABS = ((2, "الخدمة المدنية"), (3, "شبه حكومي"), (4, "التعاقدية"))


def _read_tab(page, url, pages, seen, items, first_wait_ms, sector_label=""):
    """Read up to `pages` pages of one Jadarat tab into items. Returns jobs added."""
    from playwright.sync_api import Error as PwError
    resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)
    if resp is not None and resp.status in (401, 403, 429, 451, 999):
        raise base.Blocked(f"HTTP {resp.status}")
    added = 0
    for n in range(pages):
        try:
            page.wait_for_selector(LINK_SEL, state="attached", timeout=first_wait_ms if n == 0 else 45000)
            page.wait_for_timeout(1200)
        except PwError:
            html = page.content()[:8000].lower()
            if any(sig in html for sig in base.CHALLENGE_SIGNS):
                raise base.Blocked("صفحة تحقق من جدارات")
            break  # empty tab, or no more pages
        cards = [c for c in page.eval_on_selector_all(LINK_SEL, CARDS_JS) if c["title"]]
        new = 0
        for c in cards:
            if c["href"] in seen:
                continue
            seen.add(c["href"])
            job = parse_card(c)
            if job:
                if sector_label and not job.get("sector"):
                    job["sector"] = sector_label
                items.append(job)
                new += 1
        added += new
        if not new or n == pages - 1:
            break
        first = cards[0]["href"] if cards else ""
        page.wait_for_timeout(int(random.uniform(3000, 7000)))  # polite pause
        nxt = page.locator("button[aria-label='go to next page']")
        if not nxt.count() or nxt.first.is_disabled():
            break
        nxt.first.click()
        try:
            page.wait_for_function(
                "f => { const a = document.querySelector(\"" + LINK_SEL.replace('"', '\\"') + "\");"
                " return a && a.href !== f; }", arg=first, timeout=30000)
        except PwError:
            break
    return added


def collect(source):
    try:
        from playwright.sync_api import sync_playwright, Error as PwError
    except ImportError:
        raise base.FetchError("Playwright غير مثبت (pip install playwright ثم playwright install chromium)")
    if not base.robots_allowed(EXPLORE_URL):
        raise base.Disallowed("robots.txt disallows /Jadarat/ExploreJobs")
    pages = source.get("pages", 12)
    tab_pages = source.get("tab_pages", 15)
    items, seen, per_tab = [], set(), {}
    with base._render_lock:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=config.USER_AGENT, locale="ar-SA")
                per_tab["القطاع الخاص"] = _read_tab(page, EXPLORE_URL, pages, seen, items, 45000)
                for tab, label in EXTRA_TABS:
                    page.wait_for_timeout(int(random.uniform(3000, 7000)))
                    try:
                        per_tab[label] = _read_tab(page, f"{EXPLORE_URL}?JobTab={tab}", tab_pages,
                                                   seen, items, 20000, label)
                    except PwError:
                        per_tab[label] = 0  # one tab failing must not lose the others
            except base.Blocked:
                raise
            except PwError as e:
                if not items:
                    raise base.FetchError("جدارات: " + base.first_line(e, 150))
            finally:
                browser.close()
    source["filter_jobs"] = False
    source["_note"] = f"{len(items)} وظيفة: " + "، ".join(f"{k} {v}" for k, v in per_tab.items())
    return items
