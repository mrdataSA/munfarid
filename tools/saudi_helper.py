"""Saudi helper: fetch what refuses cloud servers, from a home connection in Saudi Arabia.

Some sites (Jadarat behind Cloudflare, Tanqeeb, some relay sites) answer GitHub's servers with
"403 / Attention Required", so the cloud never reads them. Raed's old PC stays on all day, and
Windows runs this ONCE A DAY for up to one hour (tools/install_saudi_helper.ps1):

1. every source marked "cloud": "helper" in collectors/sources.json, plus every source the cloud
   found blocked (403 / challenge page) on its last run (never LinkedIn or X, never a 429 rate
   limit: see collectors/runner.helper_can_fetch), read the same polite way the cloud would
   (robots.txt respected, same pace), with their relay pages followed to the original links;
2. the relay pages the cloud could not open (collectors/origin.py): the original apply links behind them.

Everything goes into the shared database inbox; the next hourly cloud run stores it and sends
the alerts. When the PC is off, nothing breaks: those sources just wait for the next day.

    python tools/saudi_helper.py               # needs DATABASE_URL (the .bat reads it)
    python tools/saudi_helper.py --minutes 20  # shorter window
"""

import logging
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from backend import config, storage  # noqa: E402

KEEP_BATCHES = 6
WINDOW_MINUTES = 55          # one hour a day (the task stops it at 70 minutes in any case)
ORIGIN_SHARE = 0.25          # time kept for the relay pages the cloud could not open
log = logging.getLogger("rizq.helper")


def wanted_sources(all_sources, status):
    """Sources this helper fetches: marked for it, or found blocked by the cloud's last run."""
    from collectors import runner
    out = []
    for src in all_sources:
        if not src.get("enabled", True) or src.get("type") not in runner.COLLECTORS:
            continue
        st = status.get(src["key"]) or {}
        if src.get("cloud") == "helper" or (st.get("status") == "blocked"
                                            and runner.helper_can_fetch(src, st.get("message", ""))):
            out.append(src)
    return out


def run(minutes=WINDOW_MINUTES):
    from collectors import base, origin, runner
    origin.HELPER_MODE = True
    started = time.time()
    deadline = started + minutes * 60
    origin_time = minutes * 60 * ORIGIN_SHARE
    sources = wanted_sources(runner.load_sources(), storage.read("sources_status"))
    report = {"at": storage.now_iso(), "sources": {}, "origin_links": 0}
    for src in sources:
        left = deadline - origin_time - time.time()
        if left <= 0:
            report["sources"][src["key"]] = "no time left"
            continue
        fn = runner.COLLECTORS[src["type"]]
        t0 = time.time()
        try:
            items = fn(src) or []
        except (base.Disallowed, base.Blocked, base.FetchError) as e:
            log.warning("%s: %s", src["key"], e)
            report["sources"][src["key"]] = f"error: {e}"[:120]
            continue
        except Exception:
            log.exception("%s crashed", src["key"])
            report["sources"][src["key"]] = "crashed"
            continue
        try:   # relay pages -> original links, from here (the cloud may be refused by them too)
            origin.resolve(items, src, seconds=min(240, max(30, left / 2)))
        except Exception:
            log.exception("%s: original links failed", src["key"])
        batch = {"at": storage.now_iso(), "items": items, "note": src.pop("_note", ""),
                 "filter_jobs": src.get("filter_jobs")}
        with storage.edit("inbox") as inbox:
            box = inbox.setdefault(src["key"], {"batches": []})
            box["batches"] = (box.get("batches") or [])[-(KEEP_BATCHES - 1):] + [batch]
        report["sources"][src["key"]] = len(items)
        log.info("%s: %d items sent to the cloud (%.0fs)", src["key"], len(items), time.time() - t0)
    left = deadline - time.time()
    if left > 30:
        try:
            report["origin_links"] = origin.resolve_pending(left)
        except Exception:
            log.exception("relay pages failed")
    report["minutes"] = round((time.time() - started) / 60, 1)
    with storage.edit("bot_state") as st:   # shown to the team: when the helper last ran and what it did
        st["helper_last"] = report
    log.info("helper done: %s", report)
    return report


def main():
    os.makedirs(config.LOG_DIR, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.FileHandler(os.path.join(config.LOG_DIR, "saudi_helper.log"), encoding="utf-8"),
                                  logging.StreamHandler()])
    if not config.DATABASE_URL:
        sys.exit("DATABASE_URL is not set")
    minutes = WINDOW_MINUTES
    if "--minutes" in sys.argv:
        minutes = max(5, int(sys.argv[sys.argv.index("--minutes") + 1]))
    run(minutes)


if __name__ == "__main__":
    main()
