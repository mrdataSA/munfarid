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
from collectors import base, origin, runner  # noqa: E402

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

    # backfill of jobs stored before
    storage.write("jobs", [normalize.make_job(title="مهندس", source="ساحة الوظائف", url="https://linkedksa.com/5/",
                                              raw_text="وظيفة مهندس")])
    with storage.edit(origin.CACHE_KEY) as c:
        c.pop("https://linkedksa.com/5/", None)
    check("backfill: stored relay job gets its original link", origin.backfill_stored() == 1
          and storage.read("jobs")[0]["apply_url"] == "https://forms.gle/xyz")
    check("catalog: an already-original job is never overwritten",
          catalog.set_apply_links({"https://linkedksa.com/5/": "https://other.example.com/x"}) == 0)

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
