"""Employer career portals, through the job platforms (ATS) they run on.

One adapter per platform covers every employer that uses it. Employers are listed
in collectors/employers.json:
  {"name": "...", "ats": "workday", "host": "x.wd3.myworkdayjobs.com", "tenant": "x", "site": "External"}
  {"name": "...", "ats": "successfactors", "url": "https://careers.x.com"}
  {"name": "...", "ats": "greenhouse", "board": "token"}
  {"name": "...", "ats": "lever", "board": "company"}
  {"name": "...", "ats": "smartrecruiters", "board": "CompanyId"}
  {"name": "...", "ats": "oracle", "host": "xxxx.fa.em2.oraclecloud.com", "site": "CX_1"}
  {"name": "...", "ats": "workable", "board": "account-slug"}
  {"name": "...", "ats": "ashby", "board": "job-board-name"}
  {"name": "...", "ats": "recruitee", "board": "company"}          # company.recruitee.com
  {"name": "...", "ats": "pinpoint", "board": "company"}           # company.pinpointhq.com
  Optional on any entry: "api_host": "eu" (Lever EU host; EU Greenhouse boards use the normal API),
  "strict_location": true (global employer: a job with no location is NOT counted as Saudi).
  {"name": "...", "ats": "sf_csb", "url": "https://careers.x.com"}   # SuccessFactors Career Site Builder
  {"name": "...", "ats": "rendered", "url": "...", "link_contains": "/jobs/", "link_regex": "..."}   # JS page via Playwright
  {"name": "...", "ats": "rss", "url": "https://.../feed"}
  {"name": "...", "ats": "html", ...html_list spec...}
  {"name": "...", "ats": "jsonld", "url": "listing or sitemap", "link_contains": "/job/"}

Every request goes through base (robots.txt, pacing, block detection). A failing
employer is logged and skipped; it never stops the others.
"""

import html as _html
import json
import logging
import re
from datetime import datetime, timezone
from urllib.parse import quote, urljoin, urlsplit

from . import base, dom, html_list, rss

log = logging.getLogger("rizq.collect.ats")

SAUDI_HINTS = ("saudi", "ksa", "riyadh", "jeddah", "dammam", "khobar", "dhahran", "jubail", "yanbu",
               "makkah", "mecca", "madinah", "medina", "tabuk", "abha", "neom", "qassim", "hail",
               "taif", "al ula", "alula", "jizan", "jazan", "najran", "ras tanura", "hofuf", "ahsa",
               "hasa", "qatif", "khamis", "buraid", "unaizah", "kharj", "sakaka", "arar", "baha",
               "hafar", "rabigh", "qurayyat", "bisha", "dawadmi", "majmaah", "zulfi", "sudair",
               "السعودية", "الرياض", "جدة", "مكة", "المدينة", "الدمام", "الخبر", "الظهران", "الجبيل",
               "ينبع", "نيوم", "الأحساء", "الاحساء", "القطيف", "أبها", "ابها", "خميس", "تبوك", "بريدة",
               "القصيم", "حائل", "الطائف", "جازان", "جيزان", "نجران", "الباحة", "سكاكا", "عرعر",
               "الخرج", "حفر الباطن", "العلا")

# Whole words only, so short hints do not match inside other words:
# "hail" in "thailand", "baha" in "bahamas", "ksa" in "aksaray", "bisha" in "bishan",
# "arar" in "ararat", "الخبر" in "الخبرة", "العلا" in "العلاقات", "خميس" in "الخميس".
# Latin: optional glued "al"/"al-" before (alkhobar, al-ahsa) and "ah"/"a"/"h" after (buraidah).
# Arabic: optional one-letter prefix (و/ب/ل/ف) before, no letter after.
_LATIN_HINTS = [h for h in SAUDI_HINTS if h.isascii()]
_ARABIC_HINTS = [h for h in SAUDI_HINTS if not h.isascii()]
_SAUDI_RE = re.compile(
    r"(?<![a-z])(?:al-?)?(?:" + "|".join(map(re.escape, _LATIN_HINTS)) + r")(?:ah|a|h)?(?![a-z])"
    r"|(?<![ء-ي])[وبلف]?(?:" + "|".join(map(re.escape, _ARABIC_HINTS))
    + r")(?![ء-ي])")


def is_saudi(text):
    """True when the text names Saudi Arabia or a Saudi city/region (whole words)."""
    return bool(_SAUDI_RE.search(str(text or "").lower()))


def _in_saudi(location, emp):
    """Saudi employers: a job with no location counts as Saudi. Global employers
    ("strict_location": true) must name a Saudi place."""
    if emp.get("saudi_only", True) is False:
        return True
    loc = str(location or "").strip()
    if not loc:
        return not emp.get("strict_location")
    return is_saudi(loc)


def _item(emp, title, url, city="", posted="", text=""):
    return {"title": title, "url": url, "company": emp["name"], "city": city,
            "posted_date": posted, "raw_text": text or title, "needs_ai": False}


# ---------------------------------------------------------------- adapters

def workday(emp, cap):
    """Workday career sites load jobs from their own public JSON search endpoint."""
    host, tenant, site = emp["host"], emp["tenant"], emp["site"]
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    items, offset, total = [], 0, 0
    while len(items) < cap and offset < 200:
        data = base.fetch_json(api, payload={"appliedFacets": emp.get("facets", {}), "limit": 20,
                                             "offset": offset, "searchText": emp.get("search", "")})
        posts = data.get("jobPostings") or []
        total = total or data.get("total") or 0  # Workday sends the total on the first page only
        for p in posts:
            loc = p.get("locationsText", "")
            if _in_saudi(loc, emp) and p.get("title") and p.get("externalPath"):
                items.append(_item(emp, p["title"], f"https://{host}/{site}{p['externalPath']}",
                                   loc, p.get("postedOn", "")))
        offset += 20
        if len(posts) < 20 or offset >= total:
            break
    return items


def successfactors(emp, cap):
    """SAP SuccessFactors career sites publish a public RSS feed of open jobs."""
    url = emp.get("feed") or emp["url"].rstrip("/") + "/services/rss/job/?locale=" + emp.get("locale", "en_US")
    items = rss.parse_feed(base.fetch(url))
    out = []
    for it in items:
        if _in_saudi(it.get("raw_text", "")[:400] + " " + it.get("title", ""), emp) or emp.get("saudi_only") is False:
            it["company"] = emp["name"]
            it["needs_ai"] = False
            out.append(it)
    return out[:cap]


def _html_text(fragment, limit=3000):
    """Plain text of an HTML fragment (job descriptions), entity-escaped or not."""
    if not isinstance(fragment, str) or not fragment:
        return ""
    return dom.parse(_html.unescape(fragment)).text()[:limit]


def _join_loc(*parts):
    return " ".join(str(p).strip() for p in parts if isinstance(p, str) and p.strip())


def greenhouse(emp, cap):
    """Greenhouse job board API. EU-hosted boards are served by the same API host.

    With descriptions (content=true) a big board can pass the 5 MB response cap;
    then the light list (titles + locations only) is used instead.
    """
    api = f"https://boards-api.greenhouse.io/v1/boards/{quote(emp['board'])}/jobs"
    try:
        data = base.fetch_json(api + "?content=true")
    except base.FetchError as e:
        if "too large" not in str(e):
            raise
        data = base.fetch_json(api)
    out = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        offices = " ".join(o.get("location") or o.get("name") or "" for o in j.get("offices") or []
                           if isinstance(o, dict))
        if j.get("title") and j.get("absolute_url") and _in_saudi(_join_loc(loc, offices), emp):
            out.append(_item(emp, j["title"], j["absolute_url"], loc,
                             (j.get("first_published") or j.get("updated_at") or "")[:10],
                             _html_text(j.get("content"))))
    return out[:cap]


def lever(emp, cap):
    """Lever postings API, read in pages (big boards would pass the 5 MB cap in one go)."""
    host = "api.eu.lever.co" if emp.get("api_host") == "eu" else "api.lever.co"
    out, skip, size = [], 0, 100
    while len(out) < cap and skip < 3000:
        data = base.fetch_json(f"https://{host}/v0/postings/{quote(emp['board'])}"
                               f"?mode=json&limit={size}&skip={skip}")
        page = data if isinstance(data, list) else []
        for j in page:
            cats = j.get("categories") or {}
            loc = cats.get("location", "")
            country = "Saudi Arabia" if str(j.get("country") or "").upper() == "SA" else ""
            every = _join_loc(loc, country, *(cats.get("allLocations") or []))
            if j.get("text") and j.get("hostedUrl") and _in_saudi(every, emp):
                posted = ""
                if isinstance(j.get("createdAt"), (int, float)):
                    posted = datetime.fromtimestamp(j["createdAt"] / 1000, timezone.utc).date().isoformat()
                out.append(_item(emp, j["text"], j["hostedUrl"], loc, posted,
                                 (j.get("descriptionPlain") or "")[:3000]))
        if len(page) < size:
            break
        skip += size
    return out[:cap]


def ashby(emp, cap):
    """Ashby public job board API (keyless).

    api.ashbyhq.com has no robots.txt (it answers 401, i.e. not a crawl rule), and
    Ashby documents this endpoint as its public Job Posting API, meant for publishing
    the employer's open jobs elsewhere. So the robots check is skipped for this one
    documented API only; pacing and block detection still apply.
    """
    # safe="": the board stays one path segment, so the robots skip cannot reach another path
    data = base.fetch_json(f"https://api.ashbyhq.com/posting-api/job-board/{quote(emp['board'], safe='')}",
                           check_robots=False)
    out = []
    for j in data.get("jobs", []) if isinstance(data, dict) else []:
        if j.get("isListed") is False:
            continue
        addr = ((j.get("address") or {}).get("postalAddress") or {})
        secondary = [s.get("location", "") for s in j.get("secondaryLocations") or [] if isinstance(s, dict)]
        every = _join_loc(j.get("location"), addr.get("addressLocality"), addr.get("addressRegion"),
                          addr.get("addressCountry"), *secondary)
        url = j.get("jobUrl") or j.get("applyUrl")
        if j.get("title") and url and _in_saudi(every, emp):
            text = j.get("descriptionPlain") or _html_text(j.get("descriptionHtml"))
            out.append(_item(emp, j["title"], url, j.get("location") or every,
                             (j.get("publishedAt") or "")[:10], (text or "")[:3000]))
    return out[:cap]


def pinpoint(emp, cap):
    """Pinpoint public postings feed: https://{company}.pinpointhq.com/postings.json"""
    board = emp["board"]
    if not re.fullmatch(r"[a-z0-9-]+", board):
        raise ValueError("bad pinpoint board")
    data = base.fetch_json(f"https://{board}.pinpointhq.com/postings.json")
    out = []
    for j in data.get("data", []) if isinstance(data, dict) else []:
        loc = j.get("location") or {}
        every = _join_loc(loc.get("city"), loc.get("province"), loc.get("name")) if isinstance(loc, dict) else ""
        if j.get("title") and j.get("url") and _in_saudi(every, emp):
            out.append(_item(emp, j["title"], j["url"], (loc.get("city") if isinstance(loc, dict) else "") or every,
                             (j.get("published_at") or "")[:10], _html_text(j.get("description"))))
    return out[:cap]


def recruitee(emp, cap):
    """Recruitee public careers API: https://{company}.recruitee.com/api/offers/"""
    board = emp["board"]
    if not re.fullmatch(r"[a-z0-9-]+", board):
        raise ValueError("bad recruitee board")
    data = base.fetch_json(f"https://{board}.recruitee.com/api/offers/")
    out = []
    for j in data.get("offers", []) if isinstance(data, dict) else []:
        if j.get("status") not in (None, "published"):
            continue
        locs = [_join_loc(l.get("city"), l.get("state"), l.get("country"))
                for l in j.get("locations") or [] if isinstance(l, dict)]
        every = _join_loc(j.get("location"), j.get("city"), j.get("country"), *locs)
        url = j.get("careers_url") or j.get("careers_apply_url")
        if j.get("title") and url and _in_saudi(every, emp):
            out.append(_item(emp, j["title"], url, j.get("city") or j.get("location") or every,
                             (j.get("published_at") or j.get("created_at") or "")[:10],
                             _html_text(j.get("description"))))
    return out[:cap]


def smartrecruiters(emp, cap):
    url = (f"https://api.smartrecruiters.com/v1/companies/{quote(emp['board'])}/postings"
           f"?limit=100" + ("&country=sa" if emp.get("saudi_only", True) else ""))
    data = base.fetch_json(url)
    out = []
    for j in data.get("content", []):
        loc = j.get("location") or {}
        city = loc.get("city", "")
        out.append(_item(emp, j.get("name", ""),
                         f"https://jobs.smartrecruiters.com/{emp['board']}/{j.get('id')}",
                         city, (j.get("releasedDate") or "")[:10]))
    return out[:cap]


def oracle(emp, cap):
    """Oracle Recruiting Cloud career sites (the public REST search their own page calls)."""
    host, site = emp["host"], emp["site"]
    job_base = emp.get("job_base") or f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/"
    out, offset = [], 0
    while len(out) < cap and offset < 200:
        api = (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
               f"?onlyData=true&expand=requisitionList.secondaryLocations&finder=findReqs;siteNumber={site},"
               f"limit=25,offset={offset},sortBy=POSTING_DATES_DESC")
        data = base.fetch_json(api)
        block = (data.get("items") or [{}])[0]
        reqs = block.get("requisitionList", [])
        for r in reqs:
            loc = r.get("PrimaryLocation", "")
            if _in_saudi(loc, emp) and r.get("Title") and r.get("Id"):
                out.append(_item(emp, r["Title"], job_base + str(r["Id"]), loc,
                                 (r.get("PostedDate") or "")[:10], r.get("ShortDescriptionStr") or ""))
        offset += 25
        if len(reqs) < 25 or offset >= (block.get("TotalJobsCount") or 0):
            break
    return out[:cap]


def workable(emp, cap):
    """Workable widget API. With descriptions (details=true) a big account can pass the
    5 MB response cap; then the light list (no descriptions) is used instead."""
    api = f"https://apply.workable.com/api/v1/widget/accounts/{quote(emp['board'])}"
    try:
        data = base.fetch_json(api + "?details=true")
    except base.FetchError as e:
        if "too large" not in str(e):
            raise
        data = base.fetch_json(api)
    out = []
    for j in data.get("jobs", []):
        loc = " ".join(filter(None, [j.get("city"), j.get("state"), j.get("country")]))
        extra = [_join_loc(l.get("city"), l.get("region"), l.get("country"))
                 for l in j.get("locations") or [] if isinstance(l, dict) and not l.get("hidden")]
        if _in_saudi(_join_loc(loc, *extra), emp) and j.get("title"):
            url = j.get("url") or f"https://apply.workable.com/{emp['board']}/j/{j.get('shortcode')}/"
            meta = " ".join(filter(None, [j.get("department"), j.get("employment_type")]))
            out.append(_item(emp, j["title"], url, j.get("city") or loc, (j.get("published_on") or "")[:10],
                             _join_loc(meta, _html_text(j.get("description")))))
    return out[:cap]


def sf_csb(emp, cap):
    """SAP SuccessFactors Career Site Builder: server-rendered /search/ result table."""
    root_url = emp["url"].rstrip("/")
    out, start, seen = [], 0, set()
    while len(out) < cap and start < 200:
        page = dom.parse(base.fetch(f"{root_url}/search/?q=&locale={emp.get('locale', 'en_US')}&startrow={start}"))
        rows = page.find_all("tr", "data-row")
        fresh = 0
        for row in rows:
            a = row.find("a", "jobTitle-link")
            title = a.text() if a else ""
            if not title:
                continue
            url = urljoin(root_url + "/", a.attrs.get("href", ""))
            if url in seen:
                continue
            seen.add(url)
            fresh += 1
            loc = row.find("span", "jobLocation").text()
            if _in_saudi(loc, emp):
                out.append(_item(emp, title, url, loc, row.find("span", "jobDate").text()))
        start += len(rows)
        if len(rows) < 10 or not fresh:  # last page, or the site ignored startrow
            break
    return out[:cap]


def rss_feed(emp, cap):
    items = rss.parse_feed(base.fetch(emp["url"]))
    for it in items:
        it["company"] = emp.get("company", emp["name"])
    return items[:cap]


def html(emp, cap):
    spec = dict(emp)
    spec.setdefault("fixed_company", emp["name"])
    return html_list.collect(spec)[:cap]


JOB_TYPES = ("JobPosting",)


def _jobpostings(html_text):
    """Yield schema.org JobPosting objects embedded as JSON-LD."""
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html_text, re.S | re.I):
        try:
            data = json.loads(m.group(1).strip())
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            d = stack.pop()
            if isinstance(d, dict):
                if d.get("@type") in JOB_TYPES or (isinstance(d.get("@type"), list) and "JobPosting" in d["@type"]):
                    yield d
                stack.extend(d.get("@graph", []) if isinstance(d.get("@graph"), list) else [])
            elif isinstance(d, list):
                stack.extend(d)


SAUDI_COUNTRY_CODES = ("SA", "SAU")


def _jp_location(jp):
    loc = jp.get("jobLocation")
    loc = loc[0] if isinstance(loc, list) and loc else loc
    addr = loc.get("address", {}) if isinstance(loc, dict) else {}
    addr = addr[0] if isinstance(addr, list) and addr else addr
    if isinstance(addr, str):
        return addr
    if not isinstance(addr, dict):
        return ""
    parts = []
    for k in ("addressLocality", "addressRegion", "addressCountry"):
        v = addr.get(k) or ""
        if isinstance(v, dict):  # schema.org Country object: {"@type": "Country", "name": "SA"}
            v = v.get("name") or ""
        v = str(v).strip()
        # A bare ISO code would not match SAUDI_HINTS, so spell it out
        parts.append("Saudi Arabia" if v.upper() in SAUDI_COUNTRY_CODES else v)
    return " ".join(p for p in parts if p)


def _jp_company(jp, default):
    org = jp.get("hiringOrganization")
    if isinstance(org, dict):
        return org.get("name") or default
    return org if isinstance(org, str) and org else default


def jsonld(emp, cap):
    """Follow job links from a listing page (or sitemap) and read each page's JobPosting JSON-LD."""
    listing = base.fetch_rendered(emp["url"]) if emp.get("render") else base.fetch(emp["url"])
    need = emp.get("link_contains", "/job")
    links = []
    if "<urlset" in listing[:2000] or "<sitemapindex" in listing[:2000]:
        links = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", listing)
    else:
        for a in dom.parse(listing).find_all("a"):
            href = a.attrs.get("href", "")
            if need in href:
                links.append(urljoin(emp["url"], href))
    seen, out = set(), []
    for link in links:
        if need not in link or link in seen:
            continue
        seen.add(link)
        if len(seen) > cap:
            break
        try:
            page = base.fetch(link, min_interval=emp.get("interval", 3.0))
        except base.Blocked:
            raise
        except base.FetchError:
            continue
        for jp in _jobpostings(page):
            loc = _jp_location(jp)
            if not _in_saudi(loc, emp):
                continue
            raw_desc = jp.get("description")
            desc = dom.parse(raw_desc).text() if isinstance(raw_desc, str) and raw_desc else ""
            title = str(jp.get("title") or "")
            url = jp.get("url") if isinstance(jp.get("url"), str) and jp.get("url") else link
            out.append({"title": title, "url": url,
                        "company": _jp_company(jp, emp["name"]), "city": loc,
                        "posted_date": str(jp.get("datePosted") or "")[:10],
                        # facts_only: the site's terms forbid copying its content, so keep
                        # only the facts (title, company, city, date) plus the link back
                        "raw_text": title if emp.get("facts_only") else (desc[:3000] or title),
                        "needs_ai": False})
            break
    return out


def rendered(emp, cap):
    """JS-built careers page: render it, then take the links that point at job pages."""
    page = base.fetch_rendered(emp["url"], wait_selector=emp.get("wait_selector"))
    need = emp.get("link_contains", "job")
    must = re.compile(emp["link_regex"]) if emp.get("link_regex") else None  # e.g. Talentera job pages
    out, seen = [], set()
    for a in dom.parse(page).find_all("a"):
        href, title = a.attrs.get("href", ""), a.text()
        if need not in href or len(title) < 3 or title in emp.get("skip_titles", ()):
            continue
        if must and not must.search(href.split("?")[0]):
            continue
        url = urljoin(emp["url"], href)
        if url in seen:
            continue
        seen.add(url)
        out.append(_item(emp, title, url, emp.get("city", "")))
    return out[:cap]


ADAPTERS = {"workday": workday, "successfactors": successfactors, "greenhouse": greenhouse,
            "lever": lever, "smartrecruiters": smartrecruiters, "oracle": oracle,
            "workable": workable, "ashby": ashby, "recruitee": recruitee, "pinpoint": pinpoint, "sf_csb": sf_csb, "rendered": rendered,
            "rss": rss_feed, "html": html, "jsonld": jsonld}

EMPLOYERS_FILE = __file__.rsplit("ats.py", 1)[0] + "employers.json"


def load_employers(path=None):
    try:
        with open(path or EMPLOYERS_FILE, encoding="utf-8") as f:
            return [e for e in json.load(f) if e.get("enabled", True)]
    except FileNotFoundError:
        return []


# Platforms where every employer is served from one shared API host
SHARED_HOSTS = {"greenhouse": "boards-api.greenhouse.io", "lever": "api.lever.co",
                "smartrecruiters": "api.smartrecruiters.com", "workable": "apply.workable.com",
                "ashby": "api.ashbyhq.com"}


def _emp_host(emp):
    """The host an employer's requests go to (used to stop calling a host that blocked us)."""
    if emp.get("ats") in ("recruitee", "pinpoint"):
        return f"{emp.get('board', '')}.{emp['ats']}{'.com' if emp['ats'] == 'recruitee' else 'hq.com'}"
    if emp.get("api_host") == "eu" and emp.get("ats") == "lever":
        return "api.eu.lever.co"
    return (SHARED_HOSTS.get(emp.get("ats")) or emp.get("host")
            or urlsplit(emp.get("feed") or emp.get("url") or "").netloc)


GENERIC_TITLE = re.compile(r"general application|open application|spontaneous application|talent (pool|community)"
                           r"|join our talent|طلب توظيف عام|التقديم العام", re.I)


def collect(source):
    """Collect from every enabled employer of a group (source["group"] filters, e.g. 'company'/'gov').

    A block from one employer skips only that employer, plus any later employer on the
    same host, so a refusal from a shared platform host is never retried in this run.
    """
    employers = [e for e in load_employers(source.get("file"))
                 if not source.get("group") or e.get("group") == source["group"]]
    cap = source.get("per_employer", 40)
    items, ok, failed, notes = [], 0, 0, []
    blocked_hosts = set()
    for emp in employers:
        fn = ADAPTERS.get(emp.get("ats"))
        if not fn:
            continue
        host = _emp_host(emp)
        if host and host in blocked_hosts:
            failed += 1
            notes.append(f"{emp['name']}: تم التخطي لأن الخادم نفسه رفض طلباتنا")
            continue
        try:
            got = [it for it in fn(emp, cap) if not GENERIC_TITLE.search(it.get("title") or "")]
            for it in got:
                it["company"] = it.get("company") or emp["name"]
                it["source"] = emp.get("source_label") or emp["name"]
            items.extend(got)
            ok += 1
        except base.Blocked as e:
            failed += 1
            if host:
                blocked_hosts.add(host)
            notes.append(f"{emp['name']}: {str(e)[:60]}")
            log.warning("employer %s blocked us (%s); host %s skipped for this run", emp["name"], e, host)
        except (base.FetchError, KeyError, ValueError, TypeError, AttributeError, IndexError) as e:
            # AttributeError/IndexError: the API answered with an unexpected JSON shape
            failed += 1
            notes.append(f"{emp['name']}: {str(e)[:60]}")
            log.info("employer %s failed: %s", emp["name"], e)
    source["filter_jobs"] = False
    source["_note"] = f"{ok} جهة نجحت، {failed} فشلت" + (" | " + " ; ".join(notes[:4]) if notes else "")
    return items
