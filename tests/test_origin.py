"""Original-link follower (collectors/origin.py) and the Saudi helper's choice of sources,
against FAKE web pages (no network at all).

Runs on a fresh temporary data directory, so data/store is never touched.
Usage:  python tests/test_origin.py      Exit code 0 = all checks passed.
"""

import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="rizq-origin-test-")
os.environ["RIZQ_DATA_DIR"] = TMP
sys.path.insert(0, ROOT)

from backend import catalog, normalize, storage  # noqa: E402
from collectors import base, html_list, origin, runner, waiting  # noqa: E402

results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


ADS = ('<a href="https://googleads.g.doubleclick.net/aclk?x=1">إعلان</a>'
       '<a href="https://www.snapchat.com/add/x">سناب</a><a href="https://wa.me/966500000000">واتساب</a>')
PAGES = {
    # relay article -> its own "apply from here" page -> the employer
    "https://www.saudijobs24.com/t100-job": ADS + '<a href="/t200-how">التقديم من هنا</a>'
                                              '<a href="/t300-other">وظائف شركة أخرى في الرياض لحملة البكالوريوس</a>',
    "https://www.saudijobs24.com/t200-how": ADS + '<a href="https://careers.zatca.gov.sa/job/55">رابط التقديم</a>',
    # one page, apply link wrapped in the relay's redirect
    "https://dlil.org/1": ADS + '<a href="https://dlil.org/go?url=https%3A%2F%2Fjobs.sabic.com%2Fjob%2F9">اضغط هنا للتقديم</a>',
    # a relay that points to another relay, which has the real link
    "https://linkedksa.com/5/": '<a href="https://www.sahm0.com/77">رابط التقديم</a>',
    "https://www.sahm0.com/77": '<a href="https://forms.gle/xyz">اضغط هنا</a>',
    # nothing to find
    "https://www.joborr.com/t9-topic": ADS + '<a href="/t10-topic">طريقة التقديم على الشركة السعودية للخدمات الأرضية</a>',
}
fetched = []
REFUSING = {"www.wdeftksa.com"}


def fake_fetch(url, headers=None, check_robots=True, min_interval=0, jitter=0.0):
    fetched.append(url)
    if normalize.url_host(url) in {h.removeprefix("www.") for h in REFUSING}:
        raise base.Blocked("HTTP 403")
    if url not in PAGES:
        raise base.FetchError("HTTP 404")
    return PAGES[url]


base.fetch = fake_fetch

try:
    # ------------------------------------------------------------ follow
    link, state = origin.follow("https://www.saudijobs24.com/t100-job")
    check("follow: two relay pages, ads skipped, employer link found",
          (link, state) == ("https://careers.zatca.gov.sa/job/55", "ok"), (link, state))
    check("follow: the long 'related post' link is not followed", "https://www.saudijobs24.com/t300-other" not in fetched)
    check("follow: redirect wrapper unwrapped without opening it",
          origin.follow("https://dlil.org/1") == ("https://jobs.sabic.com/job/9", "ok"))
    check("follow: relay -> another relay -> employer",
          origin.follow("https://linkedksa.com/5/") == ("https://forms.gle/xyz", "ok"))
    check("follow: nothing found", origin.follow("https://www.joborr.com/t9-topic") == ("", "none"))
    check("follow: Telegram posts are never opened", origin.follow("https://t.me/chan/5") == ("", "skip"))
    n = len(fetched)
    check("follow: sites whose terms forbid it are never opened",
          origin.follow("https://shagher-jobs.com/job/1") == ("", "skip") and len(fetched) == n)
    check("follow: a refusing relay reports blocked",
          origin.follow("https://www.wdeftksa.com/sa/jobs/1") == ("", "blocked"))

    # ------------------------------------------------------------ resolve (collector items)
    items = [{"url": "https://www.saudijobs24.com/t100-job"},
             {"url": "https://careers.stc.com.sa/job/1"},                        # already original
             {"url": "https://wzaifs.com/a", "apply_url": "https://jadarat.sa/JobDetails?Param=1"},
             {"url": "https://www.wdeftksa.com/sa/jobs/2"},                       # refuses the cloud
             {"url": "https://www.wdeftksa.com/sa/jobs/3"}]
    n = len(fetched)
    origin.resolve(items, {"origin_per_run": 10})
    check("resolve: relay item gets the employer link", items[0].get("apply_url") == "https://careers.zatca.gov.sa/job/55")
    check("resolve: original links untouched", "apply_url" not in items[1]
          and items[2]["apply_url"] == "https://jadarat.sa/JobDetails?Param=1")
    opened = fetched[n:]
    check("resolve: a refusing site is tried once per run",
          sum(1 for u in opened if "wdeftksa" in u) == 1, opened)
    pending = (storage.read("inbox").get(origin.INBOX_KEY) or {}).get("pending") or []
    check("resolve: refused pages queued for the Saudi helper",
          set(pending) == {"https://www.wdeftksa.com/sa/jobs/2", "https://www.wdeftksa.com/sa/jobs/3"}, pending)
    n = len(fetched)
    again = [{"url": "https://www.saudijobs24.com/t100-job"}]
    origin.resolve(again)
    check("resolve: cached, the article is not opened twice", again[0].get("apply_url") and len(fetched) == n)

    # ------------------------------------------------------------ the helper resolves, the cloud applies
    storage.write("jobs", [normalize.make_job(title="محاسب عام", source="وظيفتك علينا",
                                              url="https://www.wdeftksa.com/sa/jobs/2", raw_text="وظيفة محاسب")])
    check("stored relay job has no original link yet", not normalize.original_link(storage.read("jobs")[0]))
    REFUSING.clear()
    PAGES["https://www.wdeftksa.com/sa/jobs/2"] = '<a href="https://weks.link/AB1?via=wdeftksa">اضغط هنا للانتقال إلى بوابة التقديم</a>'
    PAGES["https://www.wdeftksa.com/sa/jobs/3"] = "<p>لا شيء</p>"
    origin.HELPER_MODE = True
    found = origin.resolve_pending(60)
    origin.HELPER_MODE = False
    box = storage.read("inbox").get(origin.INBOX_KEY) or {}
    check("helper: pending pages resolved into the inbox", found == 1 and not box.get("pending")
          and box["done"]["https://www.wdeftksa.com/sa/jobs/2"].startswith("https://weks.link/"), box)
    changed = origin.apply_helper_results()
    job = storage.read("jobs")[0]
    check("cloud: helper links applied to stored jobs", changed == 1
          and job["apply_url"].startswith("https://weks.link/") and normalize.original_link(job), job.get("apply_url"))
    check("cloud: inbox entry cleared", origin.INBOX_KEY not in storage.read("inbox"))

    # ------------------------------------------------------------ the waiting room (original links only)
    storage.write("jobs", [normalize.make_job(title="مهندس", source="ساحة الوظائف", url="https://linkedksa.com/5/",
                                              raw_text="وظيفة مهندس"),
                           normalize.make_job(title="محاسب", source="بوابات", url="https://careers.example.com/1",
                                              raw_text="وظيفة محاسب")])
    with storage.edit(origin.CACHE_KEY) as c:
        c.pop("https://linkedksa.com/5/", None)
    moved = waiting.take_from_store()
    check("waiting: a stored job without its original link leaves the job list", len(moved) == 1
          and [j["title"] for j in storage.read("jobs")] == ["محاسب"] and len(storage.read(waiting.KEY)) == 1)
    tg = normalize.make_job(title="مندوب", source="قنوات تيليجرام", url="https://t.me/somechannel/55", raw_text="وظيفة مندوب")
    refused = normalize.make_job(title="فني", source="وظيفتك علينا", url="https://www.wdeftksa.com/sa/jobs/77", raw_text="وظيفة فني")
    old = normalize.make_job(title="سائق", source="ساحة الوظائف", url="https://linkedksa.com/old/", raw_text="وظيفة سائق")
    old["collected_at"] = "2020-01-01T00:00:00+03:00"
    REFUSING.add("www.wdeftksa.com")
    check("waiting: relay jobs are held, not stored; a Telegram post with no link is never held",
          waiting.hold([tg, refused, old]) == 2 and len(storage.read("jobs")) == 1 and tg["id"] not in storage.read(waiting.KEY))
    with storage.edit(waiting.KEY) as w0:     # waited more than 48 hours already
        w0[old["id"]]["since"] = "2020-01-01T00:00:00+03:00"
    ready, stats = waiting.process(60)
    w = storage.read(waiting.KEY)
    check("waiting: original link found -> ready to add, with it", [j["title"] for j in ready] == ["مهندس"]
          and ready[0]["apply_url"] == "https://forms.gle/xyz", [(j["title"], j.get("apply_url")) for j in ready])
    check("waiting: a job not found in time is dropped", stats["dropped"] == 1 and old["id"] not in w, stats)
    check("waiting: a resolved job counts as new from now (alerts, digests)", ready[0]["collected_at"] > "2026"
          and ready[0].get("first_collected_at") is not None)
    check("waiting: a stale page is remembered, never held again", waiting.hold([old]) == 0)
    check("waiting: a page that refuses the cloud waits for the helper", refused["id"] in w
          and "https://www.wdeftksa.com/sa/jobs/77" in (storage.read("inbox").get(origin.INBOX_KEY) or {}).get("pending", []))
    view = storage.read("sources_status").get("_origin_wait") or {}
    check("waiting: dashboard summary", view.get("count") == 1 and view.get("resolved_today") == 1
          and view.get("for_helper") == 1 and view["items"][0]["title"] == "فني", {k: v for k, v in view.items() if k != "items"})
    with storage.edit(origin.CACHE_KEY) as c:
        c["https://linkedksa.com/dead/"] = ""
    dead = normalize.make_job(title="نجار", source="ساحة الوظائف", url="https://linkedksa.com/dead/", raw_text="وظيفة نجار")
    check("waiting: a known dead page is never held again", waiting.hold([dead]) == 0)
    check("catalog: an already-original job is never overwritten",
          catalog.set_apply_links({"https://careers.example.com/1": "https://other.example.com/x"}) == 0)

    totals = {"items": 0, "jobs": 0, "added": 0, "sent": 0, "ai_used": False}
    runner._store_source({"key": "t", "name": "اختبار", "filter_jobs": False},
                         [{"title": "مصمم جرافيك", "url": "https://www.saudijobs24.com/t555-x", "raw_text": "وظيفة مصمم"},
                          {"title": "كاتب محتوى", "url": "https://careers.example.com/9", "raw_text": "وظيفة كاتب"}],
                         totals, {})
    titles = [j["title"] for j in storage.read("jobs")]
    check("runner: only jobs with their original link are added; the rest wait",
          "كاتب محتوى" in titles and "مصمم جرافيك" not in titles and totals["held"] == 1
          and any(e["job"]["title"] == "مصمم جرافيك" for e in storage.read(waiting.KEY).values()), (titles, totals))

    # ------------------------------------------------------------ employers (company names)
    card = ('<div class="card job-card"><a href="/ar/jobs/r-x/id-1"><h2>مدير مبيعات</h2><p>Wethaq</p>'
            '<img alt="شعار Wethaq" class="partner-logo" src="x.svg"/></a></div>')
    sabbar = next(s for s in runner.load_sources() if s["key"] == "sabbar")
    items = html_list.parse_list(card, sabbar, "https://sabbar.com/ar/jobs?page=1")
    check("sabbar: the employer is read from the card", items and items[0]["company"] == "Wethaq", items)
    check("company: placeholders are not employers", not normalize.company_known("جهة غير مذكورة")
          and not normalize.company_known("شركة كبرى") and normalize.company_known("Wethaq"))
    j1 = normalize.make_job(title="مدير مبيعات", source="صبّار", url="https://sabbar.com/ar/jobs/r-x/id-1", raw_text="x")
    storage.write("jobs", [j1])
    j2 = normalize.make_job(title="مدير مبيعات", source="صبّار", url="https://sabbar.com/ar/jobs/r-x/id-1", raw_text="x",
                            company="Wethaq")
    catalog.merge_collected([j2])
    check("company: a job seen again fills the stored employer (same id)", storage.read("jobs")[0]["company"] == "Wethaq"
          and storage.read("jobs")[0]["id"] == j1["id"])
    from backend import enrich
    check("company: the AI's answer must be written in the ad", enrich._employer_ok("Wethaq", "تعلن Wethaq عن وظيفة")
          and not enrich._employer_ok("أرامكو", "وظيفة في شركة كبرى") and not enrich._employer_ok("صبّار", "صبّار وظائف"))

    # ------------------------------------------------------------ helper: which sources
    from tools import saudi_helper
    srcs = [{"key": "jadarat", "type": "jadarat", "cloud": "helper"},
            {"key": "tanqeeb", "type": "html"},
            {"key": "linkedin", "type": "linkedin"},
            {"key": "sabbar", "type": "html"},
            {"key": "rate", "type": "rss"},
            {"key": "off", "type": "rss", "enabled": False}]
    status = {"tanqeeb": {"status": "blocked", "message": "HTTP 403"},
              "linkedin": {"status": "blocked", "message": "HTTP 403"},
              "sabbar": {"status": "ok"},
              "rate": {"status": "blocked", "message": "HTTP 429"},
              "off": {"status": "blocked", "message": "HTTP 403"}}
    keys = [s["key"] for s in saudi_helper.wanted_sources(srcs, status)]
    check("helper: marked sources + sources the cloud found blocked", keys == ["jadarat", "tanqeeb"], keys)
    big = [{"raw_text": "x" * 400_000} for _ in range(10)]
    parts = saudi_helper._chunks(big)
    check("helper: big batches are sent in small parts", len(parts) >= 3 and sum(len(p) for p in parts) == 10)
    check("helper: never LinkedIn, never a rate limit", not runner.helper_can_fetch({"key": "linkedin", "type": "linkedin"})
          and not runner.helper_can_fetch({"key": "a", "type": "rss"}, "HTTP 429"))

    # ------------------------------------------------------------ experience detection
    cases = [("محاسب حديث التخرج", "", 0), ("Senior Accountant", "", 5),
             ("Accountant", "Minimum 3-5 years of experience", 3), ("محاسب", "خبرة لا تقل عن 5 سنوات", 5),
             ("محاسب", "مطلوب خبرة سنتين", 2), ("مهندس", "الرياض 2026 سنة", None), ("محاسب", "سنوات الخبرة: 4", 4),
             ("محاسب", "ان لا يتجاوز العمر 30 سنة. يفضل وجود خبرة", None), ("x", "خبرة لا تقل عن 5 سنوات", 5)]
    bad = [(t, b, normalize.detect_experience(t, b)) for t, b, want in cases if normalize.detect_experience(t, b) != want]
    check("experience: years read from titles and text", not bad, bad)
    check("experience: fits", normalize.experience_fits("entry", 0) and not normalize.experience_fits("entry", 3)
          and normalize.experience_fits("senior", 5) and not normalize.experience_fits("senior", 1)
          and normalize.experience_fits("mid", None))
    check("cities: every city in a sentence", normalize.detect_cities("الرياض وجدة والدمام") == ["الرياض", "جدة", "الدمام"])
finally:
    shutil.rmtree(TMP, ignore_errors=True)

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
