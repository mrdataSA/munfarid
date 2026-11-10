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
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
