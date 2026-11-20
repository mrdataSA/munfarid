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

    # --- the PC archive: jobs leaving the live list are queued in full, never just dropped
    storage.write("jobs", [])
    storage.write("archive_out", [])
    cap = config.MAX_STORED_JOBS
    config.MAX_STORED_JOBS = 2
    try:
        def job(i, day):
            return {"id": f"a{i}", "title": f"وظيفة أرشيف {i}", "company": f"جهة {i}", "city": "جدة",
                    "source": "مصدر", "url": f"https://ex.example/{i}", "posted_date": day,
                    "raw_text": "نص كامل " * 200}
        catalog.merge_collected([job(1, "2026-11-18"), job(2, "2026-11-19"), job(3, "2026-11-20")])
        live = [j["id"] for j in storage.read("jobs")]
        out = storage.read("archive_out")
        check("over the cap: newest kept live, the oldest queued for the PC",
              live == ["a3", "a2"] and [j["id"] for j in out] == ["a1"], (live, out))
        check("archived job kept in full (whole ad text)", len(out[0].get("raw_text", "")) > 1000)
    finally:
        config.MAX_STORED_JOBS = cap

    # --- AI duplicates across sources (the model is faked: it answers "same" for STC/stc pairs)
    from backend import ai, dedup
    old = {"id": "d1", "title": "محاسب", "company": "الاتصالات السعودية", "city": "الرياض", "source": "بيت",
           "posted_date": "2026-11-18", "collected_at": "2026-11-18T10:00:00+03:00", "url": "https://b.example/1"}
    same_src = dict(old, id="d0", title="محاسب عام", url="https://b.example/0")
    other = {"id": "d9", "title": "مهندس مدني", "company": "STC", "city": "Riyadh", "source": "LinkedIn",
             "posted_date": "2026-11-19", "url": "https://l.example/9"}
    new = {"id": "d2", "title": "Accountant", "company": "STC", "city": "Riyadh", "source": "LinkedIn",
           "posted_date": "2026-11-19", "collected_at": "2026-11-19T10:00:00+03:00",
           "url": "https://l.example/2", "apply_url": "https://careers.stc.example/2"}
    storage.write("jobs", [old, same_src, other, new])
    dedup._index[0] = None
    asked = []

    def fake_chat(system, user, **kw):
        pairs = json.loads(user)
        asked.extend(pairs)
        return {"results": [{"key": p["key"], "same": {p["a"]["employer"], p["b"]["employer"]} ==
                             {"STC", "الاتصالات السعودية"} and "Accountant" in (p["a"]["title"], p["b"]["title"])}
                            for p in pairs]}
    real = (ai.enabled, ai.chat_json, ai.usage_summary)
    ai.enabled, ai.chat_json = (lambda: True), fake_chat
    ai.usage_summary = lambda: {"requests": 0, "request_limit": 100, "tokens": 0, "token_limit": 1000}
    try:
        pairs = dedup.candidates([new], storage.read("jobs"))
        ids = sorted(s["id"] for _, s in pairs)
        check("dedup candidates: Arabic/English title + employer in another language, never the same source",
              ids == ["d0", "d1"] or ids == ["d1"], ids)
        dupes, unchecked = dedup.check([new])
        gone = dedup.remove(dupes)
        kept = {j["id"]: j for j in storage.read("jobs")}
        check("dedup: the new copy removed, the stored one kept with both sources",
              gone == ["d2"] and "d2" not in kept and kept["d1"].get("also_on") == ["LinkedIn"]
              and kept["d1"].get("apply_url") == "https://careers.stc.example/2", (gone, kept.get("d1")))
        check("dedup: an engineer at the same employer is not a duplicate of an accountant",
              "d9" in kept and not any(p["a"]["title"] == "مهندس مدني" or p["b"]["title"] == "مهندس مدني"
                                       for p in asked))
        n_asked = len(asked)
        dedup.check([new])
        check("dedup: a pair is asked once (cached)", len(asked) == n_asked, len(asked))
        check("dedup: counted for the dashboard", storage.read("bot_state")["ai_duplicates"]["total"] == 1)

        # the AI is down: a new job with a possible twin is NOT added; it waits and is checked next run
        from collectors import runner

        def down(*a, **kw):
            raise ai.AIUnavailable("daily Groq budget reached")
        ai.chat_json = down
        twin = dict(new, id="d3", url="https://l.example/3", title="Accountant", company="STC", city="Riyadh")
        storage.write("dedup_wait", [])
        runner.bot.submit_new_jobs = lambda jobs: None
        runner.notifier.notify_new_jobs = lambda jobs, sent: 0
        totals = {"jobs": 0, "added": 0, "sent": 0}
        runner._add_jobs("_test", [twin], totals, {})
        stored_ids = {j["id"] for j in storage.read("jobs")}
        wait = storage.read("dedup_wait")
        check("dedup: AI down -> the job waits, not added (no alert, not stored)",
              totals["added"] == 0 and "d3" not in stored_ids and [w["job"]["id"] for w in wait] == ["d3"], (totals, wait))
        lone = {"id": "d5", "title": "طيار", "company": "طيران أديل", "city": "جدة", "source": "LinkedIn",
                "posted_date": "2026-11-19", "url": "https://l.example/5"}
        totals = {"jobs": 0, "added": 0, "sent": 0}
        runner._add_jobs("_test", [lone], totals, {})
        check("dedup: a job without any possible twin is added even with the AI down", totals["added"] == 1, totals)
        ai.chat_json = fake_chat   # the AI is back: the waiting job is checked and found to be a duplicate
        totals = {"jobs": 0, "added": 0, "sent": 0}
        runner._process_dedup_wait(totals, {})
        stored_ids = {j["id"] for j in storage.read("jobs")}
        check("dedup: next run, the waiting job is checked (a duplicate: removed, not added)",
              totals["added"] == 0 and totals.get("duplicates") == 1 and "d3" not in stored_ids
              and storage.read("dedup_wait") == [], totals)
        # cleanup of stored jobs: walks past pairs judged before, records what is left
        ai.chat_json = fake_chat
        base_jobs = [{"id": f"x{i}", "title": "محاسب", "company": f"شركة رقم {i}", "city": "الدمام", "source": "بيت",
                      "posted_date": "2026-11-19", "collected_at": f"2026-11-19T0{i}:00:00+03:00",
                      "url": f"https://b.example/x{i}"} for i in range(4)]
        twins = [dict(j, id="y" + j["id"][1:], source="LinkedIn", title="Accountant", url=j["url"] + "y",
                      collected_at=j["collected_at"].replace("T0", "T1")) for j in base_jobs]
        dup_a = dict(base_jobs[0], id="z1", company="STC", source="LinkedIn", title="Accountant",
                     collected_at="2026-11-19T20:00:00+03:00", url="https://l.example/z1")
        dup_b = dict(base_jobs[0], id="z0", company="الاتصالات السعودية", url="https://b.example/z0")
        storage.write("jobs", base_jobs + twins + [dup_a, dup_b])
        dedup._index[0] = None
        first = dedup.backfill(limit_pairs=3)
        bl1 = storage.read("bot_state")["ai_duplicates"]["backlog"]
        second = dedup.backfill(limit_pairs=50)
        bl2 = storage.read("bot_state")["ai_duplicates"]["backlog"]
        check("dedup cleanup: continues past pairs judged before, until nothing is left",
              bl1 > 0 and bl2 == 0 and "z1" in first + second and len(first + second) == 1, (first, second, bl1, bl2))
        ai.chat_json = down   # down for 24 h: the job is added anyway (never lost)
        storage.write("dedup_wait", [{"since": "2026-11-01T00:00:00+03:00", "job": dict(twin, id="d4", url="https://l.example/4")}])
        totals = {"jobs": 0, "added": 0, "sent": 0}
        runner._process_dedup_wait(totals, {})
        check("dedup: after 24 h of waiting the job is added (never lost)", totals["added"] == 1
              and storage.read("dedup_wait") == [], totals)
    finally:
        ai.enabled, ai.chat_json, ai.usage_summary = real

    # --- the bot's slim copy: 700 characters of text, experience read from the whole text first
    from tools import cloud_run
    s = cloud_run.slim_job({"id": "s1", "title": "محاسب", "description": "أ" * 1500 + " خبرة 5 سنوات " + "ب" * 1500,
                            "merge_key": "k"})
    check("slim job: short text, no description/merge key, experience from the full text",
          len(s["raw_text"]) == 700 and "description" not in s and "merge_key" not in s and s["exp_years"] == 5, s)
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
