"""Licensed job-search APIs of big aggregators (legal coverage of Indeed/Bayt/... style boards).

- Careerjet v4: free publisher key (careerjet.com.sa/partners/register/as-publisher).
  GET https://search.api.careerjet.net/v4/query, header Authorization: Basic base64("KEY:").
- Jooble: free key per country (https://sa.jooble.org/api/about). POST JSON to
  https://sa.jooble.org/api/KEY. The free plan is a small TOTAL request budget, so
  every run uses only a few requests and the running total is tracked.

Both are skipped (not errors) until their key is set in .env.

Saudi only: every result whose location is not a Saudi city/region is dropped.
Original source: the job link is the aggregator's own link, which forwards the job
seeker to the original posting when THEY click it. Links are never pre-resolved by
the collector: that would register fake clicks, which both APIs' terms forbid (it is
how they pay for the free key). These sources run LAST, so a job we already have
from its original source keeps the direct link and the copy only adds "also on".
Jooble reports the original board (`source`), shown as "<board> عبر Jooble".
"""

import base64
from urllib.parse import urlencode

from backend import config, storage

from . import base
from .ats import is_saudi

# Boards Jooble may name as the original source -> display name
ORIGIN_NAMES = {"bayt": "بيت.كوم", "linkedin": "LinkedIn", "gulftalent": "GulfTalent",
                "naukrigulf": "Naukrigulf", "indeed": "Indeed", "tanqeeb": "تنقيب",
                "drjobs": "DrJobs", "glassdoor": "Glassdoor", "akhtaboot": "Akhtaboot",
                "sabbar": "صبّار", "jadarat": "جدارات", "wuzzuf": "Wuzzuf"}


def _saudi(location):
    """Saudi Arabia only: an empty location counts (the query itself is Saudi)."""
    loc = str(location or "").strip()
    return not loc or is_saudi(loc)


def _text(value):
    """API fields are expected to be strings; a list/dict/number must not crash make_job."""
    if isinstance(value, list):
        value = ", ".join(str(v) for v in value if v)
    return value.strip() if isinstance(value, str) else ("" if value is None or isinstance(value, dict) else str(value))


def _origin_label(origin, via):
    low = _text(origin).lower()
    for k, name in ORIGIN_NAMES.items():
        if k in low:
            return f"{name} عبر {via}"
    return via


class NotConfigured(Exception):
    pass


def _jobs_of(data):
    """The job dicts of an API answer; an unexpected shape is a FetchError, not a crash."""
    if not isinstance(data, dict):
        raise base.FetchError("unexpected API answer")
    return [j for j in (data.get("jobs") or []) if isinstance(j, dict)]


def _rotate(source, key, values, per_run):
    """Pick the next `per_run` values, rotating across runs."""
    if not values:
        return []
    with storage.edit("sources_status") as st:
        entry = st.setdefault(key, {})
        start = entry.get("rotate", 0) % len(values)
        entry["rotate"] = start + per_run
    return [values[(start + i) % len(values)] for i in range(min(per_run, len(values)))]


# ---------------------------------------------------------------- Careerjet

CAREERJET_URL = "https://search.api.careerjet.net/v4/query"


def careerjet(source):
    if not config.CAREERJET_API_KEY:
        raise NotConfigured("أضف CAREERJET_API_KEY في ملف .env (مجاني من Careerjet).")
    auth = "Basic " + base64.b64encode((config.CAREERJET_API_KEY + ":").encode()).decode()
    queries = _rotate(source, source["key"], source.get("queries", [{"keywords": "", "location": ""}]),
                      source.get("queries_per_run", 4))
    items, dropped = [], 0
    for q in queries:
        params = {"locale_code": source.get("locale", "en_SA"), "keywords": q.get("keywords", ""),
                  "location": q.get("location", ""), "page": 1, "page_size": source.get("page_size", 100),
                  "sort": "date", "user_ip": config.CAREERJET_USER_IP, "user_agent": config.USER_AGENT}
        data = base.fetch_json(CAREERJET_URL + "?" + urlencode(params),
                               headers={"Authorization": auth}, check_robots=False, min_interval=1.5)
        for j in _jobs_of(data):
            title = j.get("title") or ""
            url = j.get("url") or ""
            if not title or not url:
                continue
            city = _text(j.get("locations")) or _text(j.get("location")) or q.get("location", "")
            if not _saudi(city):
                dropped += 1
                continue
            items.append({"title": title, "url": url, "company": j.get("company") or "",
                          "city": city,
                          "posted_date": j.get("date") or "", "salary": j.get("salary") or None,
                          "raw_text": j.get("description") or title, "needs_ai": False})
    source["filter_jobs"] = False
    source["_note"] = f"{len(queries)} استعلامات، استُبعد {dropped} خارج السعودية"
    return items


# ---------------------------------------------------------------- Jooble

def jooble(source):
    if not config.JOOBLE_API_KEY:
        raise NotConfigured("أضف JOOBLE_API_KEY في ملف .env (مجاني من sa.jooble.org/api/about).")
    status = storage.read("sources_status").get(source["key"], {})
    used = status.get("requests_total", 0)
    budget = config.JOOBLE_TOTAL_BUDGET - used
    if budget <= 0:
        raise NotConfigured("انتهى رصيد طلبات Jooble المجاني. اطلب رصيداً أكبر من Jooble.")
    per_run = min(source.get("queries_per_run", 2), budget)
    queries = _rotate(source, source["key"], source.get("queries", [{"keywords": "", "location": ""}]), per_run)
    url = source.get("endpoint", "https://sa.jooble.org/api/") + config.JOOBLE_API_KEY
    items, done, dropped = [], 0, 0
    try:
        for q in queries:
            done += 1  # counted before sending: a failed request may still use the budget
            data = base.fetch_json(url, payload={"keywords": q.get("keywords", ""),
                                                 "location": q.get("location", ""), "page": "1",
                                                 "ResultOnPage": str(source.get("page_size", 50))},
                                   check_robots=False, min_interval=2.0)
            for j in _jobs_of(data):
                if not j.get("title") or not j.get("link"):
                    continue
                city = _text(j.get("location")) or q.get("location", "")
                if not _saudi(city):
                    dropped += 1
                    continue
                items.append({"title": j["title"], "url": j["link"], "company": j.get("company") or "",
                              "city": city, "source": _origin_label(j.get("source"), "Jooble"),
                              "posted_date": str(j.get("updated") or "")[:10], "salary": j.get("salary") or None,
                              "raw_text": j.get("snippet") or j["title"], "needs_ai": False})
    finally:
        with storage.edit("sources_status") as st:
            st.setdefault(source["key"], {})["requests_total"] = used + done
    source["filter_jobs"] = False
    source["_note"] = (f"استُخدم {used + done} من {config.JOOBLE_TOTAL_BUDGET} طلب، "
                       f"استُبعد {dropped} خارج السعودية")
    return items
