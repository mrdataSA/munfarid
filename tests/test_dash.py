"""Owner dashboard test: private site, Telegram-confirmed login, every report, Excel files, and the
tracked job links (/j/...). Real local server on a temporary data directory, no network.

Usage:  python tests/test_dash.py      Exit code 0 = all checks passed.
"""

import http.cookiejar
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="rizq-dash-test-")
os.environ["RIZQ_DATA_DIR"] = TMP
os.environ["RIZQ_NO_ENV_FILE"] = "1"
sys.path.insert(0, ROOT)

from backend import analytics, normalize, owner, storage  # noqa: E402

ADMIN_PW = "dash-test-pw-456"
results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class Client:
    def __init__(self, base):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar), NoRedirect)

    def req(self, method, path, body=None, raw=False):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method)
        r.add_header("Origin", self.base)
        if body is not None:
            r.add_header("Content-Type", "application/json")
        try:
            with self.op.open(r, timeout=20) as resp:
                b = resp.read()
                return resp.status, (b if raw else json.loads(b or b"{}")), resp.headers
        except urllib.error.HTTPError as e:
            b = e.read()
            try:
                return e.code, (b if raw else json.loads(b or b"{}")), e.headers
            except ValueError:
                return e.code, b, e.headers


# ---------------------------------------------------------------- fixture data
now = datetime.now().astimezone()
iso = lambda d: (now - timedelta(days=d)).isoformat(timespec="seconds")  # noqa: E731
jobs = [
    dict(normalize.make_job(title="محاسب عام", source="LinkedIn", url="https://www.linkedin.com/jobs/view/1",
                            city="الرياض", raw_text="محاسب خبرة 2 سنوات"), collected_at=iso(0)),
    dict(normalize.make_job(title="مهندس مدني", source="وظائف السعودية 24", url="https://www.saudijobs24.com/t1-x",
                            city="جدة", raw_text="مهندس"), apply_url="https://careers.example.gov.sa/1", collected_at=iso(1)),
    dict(normalize.make_job(title="ممرض", source="قنوات تيليجرام", url="https://t.me/ch/5", city="الدمام",
                            raw_text="ممرض حديث التخرج"), collected_at=iso(2)),
    dict(normalize.make_job(title="أخصائي موارد بشرية", source="جدارات", url="https://jadarat.sa/JobDetails?Param=9",
                            city="الرياض", raw_text="خبرة 8 سنوات"), via_helper=True, collected_at=iso(0)),
]
storage.write("jobs", jobs)
storage.write("subscribers", {
    "11": {"chat_id": 11, "kind": "user", "created_at": iso(0), "updated_at": iso(0), "mode": "instant", "setup_done": True,
           "titles": [{"title": "محاسب", "variants": ["محاسب"]}], "cities": ["الرياض"], "exp": "junior", "sources": []},
    "12": {"chat_id": 12, "kind": "user", "created_at": iso(3), "updated_at": iso(3), "mode": "daily",
           "titles": [{"title": "طيار", "variants": ["طيار"]}], "city": "جدة", "sources": []},
    "-100": {"chat_id": -100, "kind": "feed", "titles": [], "city": "", "queue": ["x"], "posted": {"d": 3}}})
analytics.log_event("start", 11, new=True)
analytics.log_event("search", 12, titles=["طيار"], cities=["جدة"], results=0)
analytics.log_event("search", 11, titles=["محاسب"], cities=["الرياض"], results=1)

env = dict(os.environ, RIZQ_DATA_DIR=TMP, ADMIN_PASSWORD=ADMIN_PW, AUTO_COLLECT_HOURS="0", PYTHONIOENCODING="utf-8",
           RIZQ_NO_ENV_FILE="1", TELEGRAM_BOT_USERNAME="MUNFARIDsa_bot", PUBLIC_SITE="0")
port = 8131
proc = subprocess.Popen([sys.executable, "server.py", "--port", str(port), "--no-open", "--no-workers"], cwd=ROOT,
                        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
base = f"http://localhost:{port}"
try:
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/api/ping", timeout=1)
            break
        except Exception:
            time.sleep(0.1)
    anon, adm = Client(base), Client(base)

    # ------------------------------------------------------------ private site
    for path in ("/api/jobs", "/api/programs", "/api/settings"):
        check(f"private: {path} is gone", anon.req("GET", path)[0] == 404)
    check("private: sign-up API is gone", anon.req("POST", "/api/users", {"name": "x"})[0] == 404)
    check("private: dashboard data needs login", anon.req("GET", "/api/admin/dash/overview")[0] == 401)
    check("private: Excel needs login", anon.req("GET", "/api/admin/export/all", raw=True)[0] == 401)
    s, d, _ = anon.req("GET", "/api/admin/whoami")
    check("whoami: not logged in, password login offered (no owner yet)", s == 200 and not d["logged_in"]
          and d["password_login"] and not d["owner_linked"], d)
    check("page: the dashboard is served", urllib.request.urlopen(base + "/").read().decode().count("لوحة مُنْفَرِد") >= 1)

    # ------------------------------------------------------------ password login (before Telegram is linked)
    check("password login", adm.req("POST", "/api/admin/login", {"password": ADMIN_PW})[0] == 200)
    s, d, _ = adm.req("GET", "/api/admin/dash/overview")
    j, p = d.get("jobs", {}), d.get("people", {})
    check("overview: job counts", s == 200 and j["live"] == 4 and j["today"] == 2 and j["all"] == 4, j)
    check("overview: link kinds", j["kinds"] == {"direct": 2, "relay_resolved": 1, "relay_pending": 1}, j["kinds"])
    check("overview: fetched by my PC counted", j["via_helper"] == 1)
    check("overview: people (tried, subscribed, feeds)", p["tried"] >= 2 and p["subscribers"] == 2 and p["feeds"] == 1, p)
    check("overview: 30-day series", len(j["per_day"]) == 30 and j["per_day"][-1]["n"] == 2)
    check("overview: bot status present", "ok" in d["bot"] and "cloud_ok" in d["bot"])

    s, d, _ = adm.req("GET", "/api/admin/dash/jobs?city=" + urllib.request.quote("الرياض"))
    check("jobs: filter by city", s == 200 and d["total"] == 2 and all(r["city"] == "الرياض" for r in d["rows"]), d.get("total"))
    s, d, _ = adm.req("GET", "/api/admin/dash/jobs?exp=" + urllib.request.quote("أكثر من 7") + "&city="
                      + urllib.request.quote("الرياض|جدة"))
    check("jobs: experience + several cities together", d["total"] == 1 and d["rows"][0]["title"] == "أخصائي موارد بشرية",
          [r["title"] for r in d["rows"]])
    s, d, _ = adm.req("GET", "/api/admin/dash/jobs?q=" + urllib.request.quote("مهندس") + "&kind=relay_resolved")
    check("jobs: text + link kind", d["total"] == 1)
    s, d, _ = adm.req("GET", "/api/admin/dash/jobs?helper=1")
    check("jobs: fetched by my PC only", d["total"] == 1 and d["rows"][0]["via_helper"])
    check("jobs: facets for the selects", {c for c, _ in d["facets"]["cities"]} >= {"الرياض"})

    s, d, _ = adm.req("GET", "/api/admin/dash/sources")
    li = next((x for x in d["sources"] if x["source"] == "LinkedIn"), {})
    check("sources: per source counts", s == 200 and li.get("live") == 1 and li.get("direct") == 1, li)
    check("sources: my PC + Jadarat needs it", d["via_helper"] == 1
          and any(x["source"] == "جدارات" and x["needs_helper"] for x in d["sources"]), d["helper_sources"])

    s, d, _ = adm.req("GET", "/api/admin/dash/users")
    check("users: funnel", s == 200 and d["funnel"][0][1] >= 2 and d["funnel"][3][1] == 2, d.get("funnel"))
    check("users: demand vs supply", any(x["title"] == "طيار" and x["jobs"] == 0 for x in d["demand"])
          and any(x["title"] == "محاسب" and x["jobs"] == 1 for x in d["demand"]), d["demand"])
    check("users: searches with no results", ("طيار", 1) in [tuple(z) for z in d["zero_results"]], d["zero_results"])
    check("users: old single 'city' still counted", ("جدة", 1) in [tuple(c) for c in d["cities"]], d["cities"])

    # ------------------------------------------------------------ tracked link
    jid = jobs[1]["id"]
    w = analytics.who(11)
    s, _, h = anon.req("GET", f"/j/{jid}.{w}", raw=True)
    check("click: redirect to the original apply link", s == 302 and h["Location"] == "https://careers.example.gov.sa/1",
          (s, h.get("Location")))
    s, _, h = anon.req("GET", "/j/nope.u123", raw=True)
    check("click: unknown job is not an open redirect", s in (302, 404) and "example" not in (h.get("Location") or ""))
    s, d, _ = adm.req("GET", "/api/admin/dash/live?since=0")
    check("live: the click shows in the feed", s == 200 and any(e["kind"] == "click" and e["data"].get("job") == jid
                                                                 for e in d["events"]), d.get("events"))
    last = d["last_id"]
    analytics.log_event("title_add", 12, titles=["مضيف"])
    s, d, _ = adm.req("GET", f"/api/admin/dash/live?since={last}")
    check("live: only new events after the last one", [e["kind"] for e in d["events"]] == ["title_add"], d["events"])

    # ------------------------------------------------------------ Excel
    for kind in ("jobs", "sources", "users", "events", "all"):
        s, body, h = adm.req("GET", f"/api/admin/export/{kind}", raw=True)
        okzip = s == 200 and body[:2] == b"PK" and "spreadsheetml" in (h.get("Content-Type") or "")
        names = zipfile.ZipFile(io.BytesIO(body)).namelist() if okzip else []
        check(f"excel: {kind}", okzip and "xl/workbook.xml" in names, (s, h.get("Content-Type")))
    try:
        import openpyxl
        s, body, _ = adm.req("GET", "/api/admin/export/all", raw=True)
        wb = openpyxl.load_workbook(io.BytesIO(body))
        ws = wb["الوظائف"]
        check("excel: readable, Arabic sheets, rows", ws.max_row == 5 and ws.sheet_view.rightToLeft
              and "المستخدمون" in wb.sheetnames, (ws.max_row, wb.sheetnames))
        users_ws = wb["المستخدمون"]
        cells = " ".join(str(c.value) for row in users_ws.iter_rows() for c in row)
        check("excel: people are anonymous codes (no chat ids)", " 11 " not in f" {cells} " and "u" in cells)
    except ImportError:
        print("SKIP openpyxl checks (not installed)")

    # ------------------------------------------------------------ Telegram login
    with storage.edit("bot_state") as st:
        st["owner_chat"] = 777
    s, d, _ = anon.req("POST", "/api/admin/login", {"password": ADMIN_PW})
    check("owner linked: password login closed", s == 403, d)
    ticket, code = owner.new_ticket()
    key, code2 = owner.ticket_for_bot(ticket)
    check("ticket: the bot sees the same code", code == code2 and key)
    check("ticket: someone else can't approve", owner.answer(555, key, True) is False)
    s, d, _ = anon.req("POST", "/api/admin/tg-login/check", {"ticket": ticket})
    check("ticket: pending until the owner taps yes", d.get("state") == "pending")
    check("ticket: the owner approves", owner.answer(777, key, True))
    s, d, _ = anon.req("POST", "/api/admin/tg-login/check", {"ticket": ticket})
    check("ticket: approved -> logged in", d.get("state") == "approved"
          and anon.req("GET", "/api/admin/whoami")[1]["logged_in"])
    check("ticket: used once", owner.check_ticket(ticket) == "expired")
    t2, _ = owner.new_ticket()
    k2, _ = owner.ticket_for_bot(t2)
    owner.answer(777, k2, False)
    check("ticket: 'no' denies", Client(base).req("POST", "/api/admin/tg-login/check", {"ticket": t2})[1].get("state") == "denied")
    # claim code (first owner)
    with storage.edit("bot_state") as st:
        st.pop("owner_chat", None)
    check("claim: armed", owner.set_claim("SECRET-123"))
    check("claim: wrong code refused", not owner.claim(999, "nope"))
    check("claim: right code makes the owner", owner.claim(888, "SECRET-123") and owner.owner_chat() == 888)
    check("claim: can't be used twice", not owner.claim(999, "SECRET-123") and not owner.set_claim("SECRET-123"))

    # ------------------------------------------------------------ archive rule (cloud only) + buckets
    check("experience buckets", [analytics.exp_bucket(x) for x in (0, 2, 5, 9, None)] == list(analytics.EXP_BUCKETS))
    check("archive is 3 months", analytics.ARCHIVE_DAYS == 90)
finally:
    proc.kill()
    shutil.rmtree(TMP, ignore_errors=True)

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
