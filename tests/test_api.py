"""End-to-end API test for RIZQ (Python stdlib only).

Starts its own server on a free port against a fresh, EMPTY temporary data
directory (RIZQ_DATA_DIR), so the real data in data/store is never touched.

Usage:  python tests/test_api.py [--port 8091] [--keep]
Exit code 0 = all checks passed.
"""

import base64
import http.cookiejar
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADMIN_PW = "test-admin-pw-123"
results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


def free_port(start):
    for p in range(start, start + 50):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise SystemExit("no free port")


def make_opener():
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


class Client:
    def __init__(self, base):
        self.base = base
        self.opener = make_opener()

    def req(self, method, path, body=None, origin=True, ctype="application/json"):
        data = json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None)
        r = urllib.request.Request(self.base + path, data=data, method=method)
        if method == "POST":
            r.add_header("Content-Type", ctype)
        if origin:
            r.add_header("Origin", self.base)
        try:
            with self.opener.open(r, timeout=30) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {}

    def status_of(self, path):
        try:
            with self.opener.open(self.base + path, timeout=10) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code


def start_server(data_dir, port):
    env = dict(os.environ, RIZQ_DATA_DIR=data_dir, ADMIN_PASSWORD=ADMIN_PW, AUTO_COLLECT_HOURS="0",
               PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen([sys.executable, "server.py", "--port", str(port), "--no-open", "--no-workers"],
                            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://localhost:{port}"
    for _ in range(100):
        try:
            with urllib.request.urlopen(base + "/api/ping", timeout=1) as r:
                if json.load(r).get("app") == "rizq":
                    return proc, base
        except Exception:
            time.sleep(0.1)
    proc.kill()
    raise SystemExit("server did not start")


# ---------------------------------------------------------------- API checks

GOOD_POST = {
    "company": "شركة الاختبار للتقنية",
    "title": "محلل بيانات",
    "description": "نبحث عن محلل بيانات للانضمام لفريقنا في الرياض. خبرة في SQL وPython وPower BI.",
    "city": "الرياض",
    "apply_url": "https://example.com/careers/123",
    "contact_email": "HR@Example.com",
    "website": "",
}


def run_api_checks(base):
    anon = Client(base)
    s, d = anon.req("GET", "/api/ping")
    check("ping", s == 200 and d.get("app") == "rizq")
    s, d = anon.req("GET", "/api/jobs")
    check("jobs demo fallback on empty data dir", s == 200 and d["demo"] is True and len(d["jobs"]) > 0, str(d)[:200])
    check("demo jobs hide raw_text", "raw_text" not in d["jobs"][0])
    s, d = anon.req("GET", "/api/settings")
    check("settings: no banner key, support on", s == 200 and "banner" not in d and d["support"]["enabled"], str(d)[:200])

    # --- old company portal is gone
    for path in ("/api/company/register", "/api/company/login", "/api/company/jobs"):
        s, _ = anon.req("POST", path, {"x": 1})
        check(f"removed route {path} -> 404", s == 404, str(s))
    s, _ = anon.req("GET", "/api/company/me")
    check("removed route /api/company/me -> 404", s == 404, str(s))

    # --- users (alerts subscription)
    cv = base64.b64encode("خبرة في Python و SQL و Power BI".encode()).decode()
    payload = {"name": "سارة", "specialty": "البيانات والذكاء الاصطناعي", "city": "الرياض", "skills": ["Excel"],
               "cv_text": "", "cv_file": {"name": "cv.txt", "data": cv}, "id": "u_forged000000000"}
    s, d = anon.req("POST", "/api/users", payload)
    check("user create", s == 200 and d.get("edit_token") and d["user"]["id"] != "u_forged000000000", str(d)[:200])
    uid, tok = d["user"]["id"], d.get("edit_token")
    s, d = anon.req("POST", "/api/users", dict(payload, id=uid, edit_token="wrong", cv_file=None))
    check("user update wrong token -> 403", s == 403)
    s, d = anon.req("POST", "/api/users", dict(payload, id=uid, edit_token=tok, name="سارة محمد", cv_file=None))
    check("user update ok, no new token", s == 200 and "edit_token" not in d and d["user"]["name"] == "سارة محمد")
    s, d = anon.req("POST", "/api/users/telegram-link", {"id": uid, "edit_token": tok})
    # The answer depends on whether this machine's .env has a bot token
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from backend import config as _cfg
    if _cfg.TELEGRAM_BOT_TOKEN:
        check("telegram link with bot -> t.me link", s == 200 and "t.me/" in str(d), str(d)[:200])
    else:
        check("telegram link without bot -> 503", s == 503)
    # interests round-trip: only known kinds kept, update replaces them
    s, d = anon.req("POST", "/api/users", dict(payload, id=uid, edit_token=tok, cv_file=None,
                                               interests=["coop", "scholarship", "evil", 5]))
    check("user interests stored (unknown dropped)", s == 200 and d["user"]["interests"] == ["coop", "scholarship"], str(d)[:200])
    s, d = anon.req("POST", "/api/users", dict(payload, id=uid, edit_token=tok, cv_file=None, interests="coop"))
    check("user interests non-list -> empty", s == 200 and d["user"]["interests"] == [], str(d)[:200])
    s, d = anon.req("POST", "/api/users", dict(payload, id=uid, edit_token=tok, cv_file=None, interests=["scholarship"]))
    check("user interests update", s == 200 and d["user"]["interests"] == ["scholarship"], str(d)[:200])

    # --- co-op / scholarship programs
    s, d = anon.req("GET", "/api/programs")
    progs = d.get("programs", []) if s == 200 else []
    check("programs endpoint has both kinds", s == 200 and {p["kind"] for p in progs} == {"coop", "scholarship"}, str(d)[:200])
    check("programs fields + safe urls", progs and all(
        isinstance(p["open_months"], list) and isinstance(p["rolling"], bool) and "open_signal" in p
        and all(not p[k] or p[k].startswith(("https://", "http://")) for k in ("apply_url", "source_url")) for p in progs))
    check("programs ids unique per kind", len({(p["kind"], p["id"]) for p in progs}) == len(progs))
    s, d = anon.req("POST", "/api/programs", {"x": 1})
    check("POST /api/programs not allowed", s in (404, 405), str(s))

    s, d = anon.req("POST", "/api/users", payload, ctype="text/plain")
    check("text/plain POST -> 415", s == 415)
    s, d = anon.req("POST", "/api/users", {"name": "x"})
    check("invalid user -> 400", s == 400)

    # --- direct posts: rejection paths
    bad_cases = [
        ("javascript: apply url", {"apply_url": "javascript:alert(1)"}),
        ("data: apply url", {"apply_url": "data:text/html,<script>alert(1)</script>"}),
        ("ftp apply url", {"apply_url": "ftp://example.com/x"}),
        ("apply url without host", {"apply_url": "https://"}),
        ("bad apply email", {"apply_url": "", "apply_email": "not-an-email"}),
        ("no apply link or email", {"apply_url": "", "apply_email": ""}),
        ("missing contact email", {"contact_email": ""}),
        ("bad contact email", {"contact_email": "hr@"}),
        ("short company", {"company": "x"}),
        ("short title", {"title": "ab"}),
        ("long title", {"title": "و" * 121}),
        ("short description", {"description": "قصير"}),
        ("long description", {"description": "وصف " * 800}),
        ("unknown city", {"city": "مدينة خيالية"}),
        ("asks for fees (SCAM_WORDS)", {"description": GOOD_POST["description"] + " يتم دفع رسوم توظيف قبل المقابلة."}),
        ("asks for fees (post words)", {"description": GOOD_POST["description"] + " مع رسوم فتح ملف بسيطة."}),
        ("asks for fees (english)", {"title": "Data analyst - small application fee"}),
    ]
    for name, override in bad_cases:
        s, d = anon.req("POST", "/api/posts", dict(GOOD_POST, **override))
        check(f"post rejected: {name} -> 400", s == 400 and d.get("error"), f"{s} {d}")
    s, d = anon.req("POST", "/api/posts", GOOD_POST, origin=False)
    check("post without Origin -> 403", s == 403, str(s))
    s, d = anon.req("POST", "/api/posts", GOOD_POST, ctype="text/plain")
    check("post as text/plain -> 415", s == 415, str(s))

    # --- honeypot: pretends success, stores nothing
    s, d = anon.req("POST", "/api/posts", dict(GOOD_POST, website="http://spam.example"))
    check("honeypot -> 200 (silent)", s == 200 and d["post"]["status"] == "pending", str(d))
    trap_id = d.get("post", {}).get("id")

    # --- valid posts
    s, d = anon.req("POST", "/api/posts", GOOD_POST)
    check("post with apply url -> pending", s == 200 and d["post"]["status"] == "pending", str(d))
    pid_url = d["post"]["id"]
    s, d = anon.req("POST", "/api/posts", dict(GOOD_POST, title="أخصائي موارد بشرية", apply_url="",
                                               apply_email="Jobs@Example.com", city="جدة"))
    check("post with apply email -> pending", s == 200 and d["post"]["status"] == "pending", str(d))
    pid_mail = d["post"]["id"]
    check("post response hides contact email", "contact_email" not in d["post"])

    s, d = anon.req("GET", "/api/jobs")
    check("pending posts are not public", all(j["id"] not in (pid_url, pid_mail) for j in d["jobs"]) and d["demo"] is True)
    s, d = anon.req("GET", f"/api/jobs/{pid_url}")
    check("pending post detail -> 404", s == 404)

    # --- admin moderation
    adm = Client(base)
    s, d = adm.req("GET", "/api/admin/overview")
    check("admin overview needs login -> 401", s == 401)
    s, d = adm.req("POST", "/api/admin/login", {"password": "nope"})
    check("admin wrong password -> 401", s == 401)
    s, d = adm.req("POST", "/api/admin/login", {"password": ADMIN_PW})
    check("admin login", s == 200)
    s, d = adm.req("GET", "/api/admin/overview")
    ids = [p["id"] for p in d.get("posts", [])]
    check("overview lists both pending posts", s == 200 and d["stats"]["posts_pending"] == 2 and pid_url in ids and pid_mail in ids, str(d.get("stats")))
    check("honeypot post was not stored", trap_id not in ids)
    row = next((p for p in d.get("posts", []) if p["id"] == pid_url), {})
    check("admin sees private contact email (lower-cased)", row.get("contact_email") == "hr@example.com", str(row))
    check("overview has no companies/featured keys", "companies" not in d and "feature_requests" not in d)

    s, d = anon.req("POST", f"/api/admin/posts/{pid_url}/status", {"status": "approved"})
    check("anonymous cannot approve -> 401", s == 401)
    s, d = adm.req("POST", f"/api/admin/posts/{pid_url}/status", {"status": "approved"}, origin=False)
    check("approve without Origin -> 403", s == 403)
    s, d = adm.req("POST", f"/api/admin/posts/{pid_url}/status", {"status": "bogus"})
    check("approve with bad status -> 400", s == 400)
    s, d = adm.req("POST", "/api/admin/posts/dnope00000000/status", {"status": "approved"})
    check("approve unknown post -> 400", s == 400)
    s, d = adm.req("POST", f"/api/admin/posts/{pid_url}/status", {"status": "approved"})
    check("approve post (url)", s == 200 and d["post"]["status"] == "approved", str(d))
    s, d = adm.req("POST", f"/api/admin/posts/{pid_mail}/status", {"status": "approved"})
    check("approve post (email)", s == 200)

    s, d = anon.req("GET", "/api/jobs")
    by_id = {j["id"]: j for j in d["jobs"]}
    j = by_id.get(pid_url, {})
    check("approved posts are public, demo gone", d["demo"] is False and pid_url in by_id and pid_mail in by_id, str(d)[:300])
    check("direct badge + source", j.get("direct") is True and j.get("source") == "إعلان مباشر", str(j))
    check("apply url is the original source", j.get("url") == GOOD_POST["apply_url"])
    check("apply email becomes mailto:", by_id.get(pid_mail, {}).get("url") == "mailto:jobs@example.com", str(by_id.get(pid_mail)))
    leaked = [k for k in ("contact_email", "ip_hash", "status", "approved_once") if k in j]
    check("public list hides private fields", not leaked, str(leaked))
    check("contact email not anywhere in public list", "hr@example.com" not in json.dumps(d).lower())
    s, d = anon.req("GET", f"/api/jobs/{pid_url}")
    check("public detail ok, private fields hidden",
          s == 200 and d["job"]["description"] and "contact_email" not in d["job"] and "ip_hash" not in d["job"], str(d)[:300])

    # reports + hide still work on direct posts
    s, d = anon.req("POST", "/api/reports", {"job_id": pid_url, "reason": "احتيال أو طلب رسوم", "note": "<b>x</b>"})
    check("report a direct post", s == 200)

    # reject (unpublish), delete
    s, d = adm.req("POST", f"/api/admin/posts/{pid_url}/status", {"status": "rejected"})
    check("reject (unpublish) post", s == 200)
    s, d = anon.req("GET", "/api/jobs")
    check("rejected post disappears", all(j["id"] != pid_url for j in d["jobs"]))
    s, d = adm.req("POST", f"/api/admin/posts/{pid_mail}/delete")
    check("delete post", s == 200)
    s, d = adm.req("POST", f"/api/admin/posts/{pid_mail}/delete")
    check("delete again -> 400", s == 400)
    s, d = adm.req("GET", "/api/admin/overview")
    check("deleted post gone from admin", all(p["id"] != pid_mail for p in d["posts"]))

    # --- per-IP daily cap (5 stored posts per day; the rejected one still counts)
    accepted, last = 0, None
    for i in range(6):
        last, _ = anon.req("POST", "/api/posts", dict(GOOD_POST, title=f"وظيفة رقم {i + 1} للاختبار"))
        if last != 200:
            break
        accepted += 1
    check("daily cap: 4 more accepted then 429", accepted == 4 and last == 429, f"accepted={accepted} last={last}")

    # --- settings: banner can no longer be set
    s, d = adm.req("POST", "/api/admin/settings", {"banner": {"enabled": True, "text": "إعلان", "url": "https://x.example"},
                                                   "support": {"enabled": True, "text": "ادعم المبادرة",
                                                               "links": [{"label": "قهوة", "url": "https://buymeacoffee.com/x"},
                                                                         {"label": "bad", "url": "http://x"}]}})
    check("settings saved, banner ignored", s == 200 and "banner" not in d["settings"] and len(d["settings"]["support"]["links"]) == 1, str(d)[:300])
    s, d = anon.req("GET", "/api/settings")
    check("public settings still without banner", "banner" not in d)

    # --- user delete
    s, d = anon.req("POST", "/api/users/delete", {"id": uid, "edit_token": "bad"})
    check("delete user wrong token -> 403", s == 403)
    s, d = anon.req("POST", "/api/users/delete", {"id": uid, "edit_token": tok})
    check("delete user", s == 200)

    # --- static allowlist
    for p, want in [("/", 200), ("/js/app.js", 200), ("/js/portal.js", 200), ("/data/jobs.json", 404),
                    ("/data/store/direct_posts.json", 404), ("/.env", 404), ("/backend/posts.py", 404),
                    ("/tests/test_api.py", 404), ("//data/store/users.json", 404)]:
        code = anon.status_of(p)
        check(f"static {p} -> {want}", code == want, str(code))


# ---------------------------------------------------------------- migration check

def run_migration_check():
    """Old company portal data (companies + company_jobs) becomes direct posts."""
    tmp = tempfile.mkdtemp(prefix="rizq-migrate-")
    try:
        store = os.path.join(tmp, "store")
        os.makedirs(store)
        today = time.strftime("%Y-%m-%d")
        companies = [
            {"id": "co_aaaaaaaaaaaa", "name": "شركة معتمدة", "email": "a@a.sa", "status": "approved"},
            {"id": "co_bbbbbbbbbbbb", "name": "شركة معلقة", "email": "b@b.sa", "status": "pending"},
            {"id": "co_cccccccccccc", "name": "شركة موقوفة", "email": "c@c.sa", "status": "suspended"},
        ]

        def job(jid, cid, status="active"):
            return {"id": jid, "company_id": cid, "title": "وظيفة " + jid, "city": "الرياض", "specialty": "الإدارة",
                    "contract_type": "دوام كامل", "description": "وصف وظيفة مهاجرة للاختبار.", "skills": [],
                    "url": "https://example.com/" + jid, "status": status, "posted_date": today,
                    "featured": True, "feature_requested": True, "source": "رزق - مباشر"}
        jobs = [job("maaaaaaaaaaa1", "co_aaaaaaaaaaaa"), job("maaaaaaaaaaa2", "co_aaaaaaaaaaaa", "closed"),
                job("mbbbbbbbbbbb1", "co_bbbbbbbbbbbb"), job("mcccccccccccc1", "co_cccccccccccc")]
        for name, data in (("companies.json", companies), ("company_jobs.json", jobs)):
            with open(os.path.join(store, name), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        with open(os.path.join(store, "sessions.json"), "w", encoding="utf-8") as f:
            json.dump({"h1": {"kind": "company", "sub": "co_aaaaaaaaaaaa", "expires": "2999-01-01T00:00:00"},
                       "h2": {"kind": "admin", "sub": "admin", "expires": "2999-01-01T00:00:00"}}, f)

        code = ("import json; from backend import posts, catalog; "
                "print(json.dumps({'summary': posts.migrate_legacy(), 'again': posts.migrate_legacy(), "
                "'public': [j['id'] for j in catalog.all_jobs(include_demo=False)[0]]}))")
        env = dict(os.environ, RIZQ_DATA_DIR=tmp, PYTHONIOENCODING="utf-8")
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True,
                             encoding="utf-8", timeout=60)
        try:
            res = json.loads(out.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            check("migration ran", False, out.stderr[-400:])
            return
        check("migration summary", res["summary"] == {"approved": 1, "pending": 1, "skipped": 2}, str(res["summary"]))
        check("migration runs only once", res["again"] is None)
        check("approved company job stays public with same id", res["public"] == ["maaaaaaaaaaa1"], str(res["public"]))
        with open(os.path.join(store, "direct_posts.json"), encoding="utf-8") as f:
            migrated = {p["id"]: p for p in json.load(f)}
        check("pending company job awaits moderation", migrated.get("mbbbbbbbbbbb1", {}).get("status") == "pending")
        check("contact email carried over", migrated.get("maaaaaaaaaaa1", {}).get("contact_email") == "a@a.sa")
        check("featured flags dropped", not any("featured" in p for p in migrated.values()))
        left = sorted(os.listdir(store))
        check("old files renamed, not deleted",
              not os.path.exists(os.path.join(store, "companies.json"))
              and any(n.startswith("companies.json.migrated-") for n in left)
              and any(n.startswith("company_jobs.json.migrated-") for n in left), str(left))
        with open(os.path.join(store, "sessions.json"), encoding="utf-8") as f:
            sessions = json.load(f)
        check("company sessions purged, admin kept", list(sessions) == ["h2"], str(list(sessions)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- front-end URL sanitiser

def run_safeurl_check():
    node = shutil.which("node")
    if not node:
        print("SKIP safeUrl check (node not installed)")
        return
    with open(os.path.join(ROOT, "js", "app.js"), encoding="utf-8") as f:
        src = f.read()
    start = src.index("  function safeUrl(")
    end = src.index("\n  }\n", start) + 4
    script = (src[start:end] + "\nconst location = { href: 'http://localhost:8080/' };\n"
              "const cases = [['https://a.sa/x', false], ['http://a.sa', false], ['mailto:hr@a.sa', true], "
              "['mailto:hr@a.sa', false], ['javascript:alert(1)', true], [' javascript:alert(1)', true], "
              "['JaVaScRiPt:alert(1)', true], ['data:text/html,x', true], ['vbscript:x', true], ['', true]];\n"
              "console.log(JSON.stringify(cases.map(([u, m]) => safeUrl(u, m))));\n")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    try:
        got = json.loads(out.stdout.strip())
    except ValueError:
        check("safeUrl script ran", False, out.stderr[-300:])
        return
    check("safeUrl keeps https/http", got[0] == "https://a.sa/x" and got[1] == "http://a.sa/")
    check("safeUrl allows mailto only when asked", got[2] == "mailto:hr@a.sa" and got[3] == "")
    check("safeUrl blocks javascript:/data:/vbscript:", all(x == "" for x in got[4:9]), str(got))
    check("safeUrl treats empty url as no link (not this page)", got[9] == "", str(got[9]))


def main():
    port = 8091
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    port = free_port(port)
    data_dir = tempfile.mkdtemp(prefix="rizq-test-")
    proc = None
    try:
        proc, base = start_server(data_dir, port)
        print(f"server: {base}  data: {data_dir}\n")
        run_api_checks(base)
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        if "--keep" not in sys.argv:
            shutil.rmtree(data_dir, ignore_errors=True)
    run_migration_check()
    run_safeurl_check()
    passed = sum(ok for _, ok in results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
