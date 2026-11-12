"""Cloud-mode pieces that run without a database: the Saudi helper inbox and the read-only snapshot.

    python tests/test_cloud.py
"""

import gzip
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rizq-cloud-test-")
os.environ["RIZQ_DATA_DIR"] = TMP

from backend import catalog, config, storage  # noqa: E402
from collectors import runner  # noqa: E402

config.DATABASE_URL = ""
results = []


def check(name, cond, info=""):
    results.append(cond)
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  {info}"))


try:
    # --- helper inbox: batches stay until their jobs are stored
    with storage.edit("inbox") as inbox:
        inbox["jadarat"] = {"batches": [{"at": "2026-11-10T20:00:00+03:00", "items": [{"title": "a"}]},
                                        {"at": "2026-11-10T22:00:00+03:00", "items": [{"title": "b"}],
                                         "filter_jobs": False}]}
    src = {"key": "jadarat"}
    items = runner.from_helper(src)
    check("helper items returned in order", [i["title"] for i in items] == ["a", "b"], items)
    check("inbox kept until stored", len(storage.read("inbox")["jadarat"]["batches"]) == 2)
    check("filter flag from the last batch", src.get("filter_jobs") is False)
    with storage.edit("inbox") as inbox:   # a new batch arrives while the run is storing
        inbox["jadarat"]["batches"].append({"at": "2026-11-10T23:00:00+03:00", "items": [{"title": "c"}]})
    runner._helper_done(src)
    left = storage.read("inbox").get("jadarat", {}).get("batches", [])
    check("only consumed batches removed", [b["at"][11:16] for b in left] == ["23:00"], left)
    src2 = {"key": "jadarat"}
    runner.from_helper(src2)
    runner._helper_done(src2)
    check("inbox entry removed when empty", "jadarat" not in storage.read("inbox"))
    src3 = {"key": "tanqeeb"}
    check("empty inbox gives nothing", runner.from_helper(src3) == [] and "مساعد" in src3["_note"])

    # --- read-only snapshot (Vercel)
    snap = os.path.join(TMP, "snap")
    os.makedirs(snap)
    jobs = [{"id": "j1", "title": "محاسب", "posted_date": "2026-11-10", "collected_at": "2026-11-10T10:00:00+03:00"}]
    with gzip.open(os.path.join(snap, "jobs.json.gz"), "wt", encoding="utf-8") as f:
        json.dump(jobs, f, ensure_ascii=False)
    config.SNAPSHOT_DIR = snap
    check("snapshot jobs readable", [j["id"] for j in storage.read_cached("jobs")] == ["j1"])
    try:
        with storage.edit("jobs"):
            pass
        check("snapshot is read-only", False)
    except storage.StorageError:
        check("snapshot is read-only", True)
    check("catalog works on the snapshot", catalog.find("j1") is not None)
    config.SNAPSHOT_DIR = ""

    # --- relay sites: official apply link from the feed, Jadarat relays marked and merged
    from collectors import base, rss
    feed = """<?xml version="1.0"?><rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><title>t</title>
<item><title>وزارة تعلن وظائف إدارية</title><link>https://relay.example/1</link><pubDate>Thu, 12 Nov 2026 06:00:00 +0000</pubDate>
<category>وظائف حكومية</category>
<content:encoded><![CDATA[<p>تعلن الوزارة عن وظائف إدارية.</p><a href="https://relay.example/tag/x">وسم</a>
<a href="https://jadarat.sa/JobDetails?Param=Q1">رابط التقديم</a>]]></content:encoded></item>
<item><title>دورة مجانية في الإكسل</title><link>https://relay.example/2</link>
<category>كورسات مجانية</category><content:encoded><![CDATA[<a href="https://x.example">التسجيل</a>]]></content:encoded></item>
</channel></rss>"""
    real_fetch = base.fetch
    base.fetch = lambda url, **kw: feed
    try:
        got = rss.collect({"key": "relay", "name": "ناقل", "url": "https://relay.example/feed",
                           "apply_from_feed": True, "category_exclude": ["كورسات"]})
    finally:
        base.fetch = real_fetch
    check("relay feed: courses excluded", [i["title"] for i in got] == ["وزارة تعلن وظائف إدارية"], got)
    check("relay feed: official apply link taken from the post",
          got and got[0].get("apply_url") == "https://jadarat.sa/JobDetails?Param=Q1", got)
    rel = runner._to_job({"key": "relay", "name": "ناقل"}, got[0])
    check("relay job: marked as relayed from Jadarat", rel and rel.get("via_jadarat") is True, rel)
    own = {"id": "jad1", "title": "أخصائي إداري", "company": "وزارة", "city": "الرياض", "source": "جدارات",
           "url": "https://jadarat.sa/JobDetails?Param=Q1", "posted_date": "2026-11-12"}
    storage.write("jobs", [own])
    added = catalog.merge_collected([rel])
    kept = storage.read("jobs")
    check("relay job: same Jadarat ad is not added twice", added == [] and len(kept) == 1
          and kept[0].get("also_on") == ["ناقل"], kept)

    # jobs stored before the marking existed get marked on the next store
    storage.write("jobs", [{"id": "o1", "title": "قديمة", "company": "جهة", "city": "الرياض", "source": "ناقل",
                            "url": "https://relay.example/old", "apply_url": "https://jadarat.sa/JobDetails?Param=OLD",
                            "posted_date": "2026-11-12"}])
    catalog.merge_collected([])
    check("older relay of Jadarat marked on the next store", storage.read("jobs")[0].get("via_jadarat") is True)

    # portal pages (ExploreJobs) are shared by many posts: they never merge different jobs
    def relay(i, link):
        return {"id": f"r{i}", "title": f"وظيفة {i}", "company": f"جهة {i}", "city": "الرياض", "source": "ناقل",
                "url": f"https://relay.example/{i}", "apply_url": link, "via_jadarat": True,
                "posted_date": "2026-11-12"}
    storage.write("jobs", [])
    added = catalog.merge_collected([relay(1, "https://jadarat.sa/Jadarat/ExploreJobs?JobTab=1"),
                                     relay(2, "https://jadarat.sa/Jadarat/ExploreJobs?JobTab=1")])
    check("relay jobs: a shared portal link does not merge them", len(added) == 2, added)
    # the original arrives after its relay: the original is kept, the relay noted
    storage.write("jobs", [relay(3, "https://jadarat.sa/JobDetails?Param=Z9")])
    orig = dict(own, id="jad9", url="https://jadarat.sa/JobDetails?Param=Z9")
    added = catalog.merge_collected([orig])
    kept = storage.read("jobs")
    check("original after relay: original kept, relay noted, no new alert",
          added == [] and [j["id"] for j in kept] == ["jad9"] and kept[0].get("also_on") == ["ناقل"], kept)
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
