"""Saudi helper: fetch the sources that refuse cloud servers, from a home connection in Saudi Arabia.

Jadarat (Cloudflare) and Tanqeeb answer GitHub's servers with "403 / Attention Required", so
the cloud never reads them. When Raed's PC is on, Windows runs this every 2 hours
(tools/saudi_helper.bat): it reads those sites the same way the local server always did
(robots.txt respected, same pace), and puts the raw results in the shared database inbox.
The next cloud run stores them and sends the alerts. When the PC is off, nothing breaks:
those two sources just wait.

    python tools/saudi_helper.py           # needs DATABASE_URL (the .bat reads it)
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
log = logging.getLogger("rizq.helper")


def main():
    os.makedirs(config.LOG_DIR, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.FileHandler(os.path.join(config.LOG_DIR, "saudi_helper.log"), encoding="utf-8"),
                                  logging.StreamHandler()])
    if not config.DATABASE_URL:
        sys.exit("DATABASE_URL is not set")
    from collectors import base, runner
    sources = [s for s in runner.load_sources() if s.get("enabled", True) and s.get("cloud") == "helper"]
    for src in sources:
        fn = runner.COLLECTORS.get(src.get("type"))
        t0 = time.time()
        try:
            items = fn(src) or []
        except (base.Disallowed, base.Blocked, base.FetchError) as e:
            log.warning("%s: %s", src["key"], e)
            continue
        except Exception:
            log.exception("%s crashed", src["key"])
            continue
        batch = {"at": storage.now_iso(), "items": items, "note": src.get("_note", ""),
                 "filter_jobs": src.get("filter_jobs")}
        with storage.edit("inbox") as inbox:
            box = inbox.setdefault(src["key"], {"batches": []})
            box["batches"] = (box.get("batches") or [])[-(KEEP_BATCHES - 1):] + [batch]
        log.info("%s: %d items sent to the cloud (%.0fs)", src["key"], len(items), time.time() - t0)


if __name__ == "__main__":
    main()
