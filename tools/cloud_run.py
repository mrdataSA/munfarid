"""One cloud run on GitHub Actions (every hour): collect, alert, post to feeds, daily message,
then write the read-only snapshot that the next Vercel deployment carries.

    python tools/cloud_run.py            # full run every FULL_EVERY_HOURS, otherwise the fast lane
    python tools/cloud_run.py --full     # force a full run
    python tools/cloud_run.py --restore  # only restore data/store from the database backup
    python tools/cloud_run.py --setup    # create the database tables (once)

The collectors' files (data/store) are kept between runs by the Actions cache; a copy is
saved to the database once a day and restored from there if the cache was lost.
"""

import gzip
import io
import json
import logging
import os
import sys
import tarfile
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend import bot, config, pgstore, storage  # noqa: E402

log = logging.getLogger("rizq.cloud")
SNAPSHOT = os.path.join(ROOT, "snapshot")   # bundled with the bot/site function (read-only)
PUB = os.path.join(ROOT, "pub")             # public static files
FULL_EVERY_HOURS = max(1, config.get_int("FULL_EVERY_HOURS", 3))
FEED_MINUTES = config.get_int("FEED_MINUTES", 6)       # time spent posting queued jobs to groups per run
BACKUP_NAME = "store.tar.gz"
# The collectors' own files go to the snapshot; the shared ones are in the database already
SNAPSHOT_COLLECTIONS = ("jobs", "programs_watch")


def _store_files():
    return [f for f in os.listdir(config.STORE_DIR)
            if not f.startswith(".") and not f.endswith(".tmp")] if os.path.isdir(config.STORE_DIR) else []


def restore_if_missing():
    """The Actions cache can be evicted: fall back to the last database backup."""
    if os.path.exists(os.path.join(config.STORE_DIR, "jobs.json")):
        return False
    data = pgstore.get_blob(BACKUP_NAME)
    if not data:
        log.warning("no store in the cache and no backup in the database: starting empty")
        return False
    os.makedirs(config.STORE_DIR, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for m in tar.getmembers():
            # only plain files directly inside the store folder
            if m.isfile() and "/" not in m.name and "\\" not in m.name and not m.name.startswith("."):
                with tar.extractfile(m) as src, open(os.path.join(config.STORE_DIR, m.name), "wb") as dst:
                    dst.write(src.read())
    log.info("store restored from the database backup (%d bytes)", len(data))
    return True


def backup_daily():
    """Once a day: the collectors' files to the database (a few MB, compressed)."""
    today = datetime.now().date().isoformat()
    with storage.edit("bot_state") as st:
        if st.get("store_backup_date") == today:
            return False
        st["store_backup_date"] = today
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for f in _store_files():
            if f.endswith((".db-wal", ".db-shm")):
                continue
            tar.add(os.path.join(config.STORE_DIR, f), arcname=f)
    pgstore.put_blob(BACKUP_NAME, buf.getvalue())
    log.info("store backed up (%d bytes)", buf.tell())
    return True


def write_snapshot():
    """Read-only copy for the bot and the site on Vercel (bundled with the deployment)."""
    os.makedirs(SNAPSHOT, exist_ok=True)
    for name in SNAPSHOT_COLLECTIONS:
        data = storage.read(name)
        path = os.path.join(SNAPSHOT, storage.COLLECTIONS[name][0] + ".gz")
        with gzip.open(path + ".tmp", "wt", encoding="utf-8", compresslevel=9) as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(path + ".tmp", path)
    with open(os.path.join(SNAPSHOT, "built_at.txt"), "w", encoding="utf-8") as f:
        f.write(storage.now_iso())
    # The site's job list as a static file (served by Vercel's CDN, compressed): a function's
    # answer is limited to 4.5 MB, the full list is bigger. vercel.json maps /api/jobs to it.
    from backend import catalog
    os.makedirs(PUB, exist_ok=True)
    body = dict(catalog.public_jobs(), ok=True)
    with open(os.path.join(PUB, "jobs.json.tmp"), "w", encoding="utf-8") as f:
        json.dump(body, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(os.path.join(PUB, "jobs.json.tmp"), os.path.join(PUB, "jobs.json"))


def post_feeds(minutes):
    """Groups/channels get their queued jobs spaced out (FEED_MIN_GAP), for a few minutes per run."""
    deadline = time.time() + minutes * 60
    posted = 0
    while time.time() < deadline:
        posted += bot.feed_tick()
        subs = storage.read_cached("subscribers")
        if not any(s.get("kind") == "feed" and s.get("queue") for s in subs.values()):
            break
        time.sleep(15)
    return posted


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not config.DATABASE_URL:
        sys.exit("DATABASE_URL is not set")
    if "--setup" in sys.argv:
        pgstore.init_schema()
        print("schema ready")
        return
    restore_if_missing()
    if "--restore" in sys.argv:
        return
    from collectors import runner
    full = "--full" in sys.argv or datetime.now().hour % FULL_EVERY_HOURS == 0
    summary = runner.run_all(fast=not full)
    print(json.dumps(summary, ensure_ascii=False))
    if not bot.flush_alerts(timeout=600):
        log.warning("some instant alerts were still pending (the daily message still has them)")
    report = {"mode": "full" if full else "fast", "collect": summary}
    if config.TELEGRAM_BOT_TOKEN:
        report["feed_posts"] = post_feeds(FEED_MINUTES)
        report["digest_sent"] = bot.digest_tick()
    write_snapshot()
    try:
        backup_daily()
        pgstore.purge_deleted()
    except Exception:
        log.exception("backup failed (the cache still has the store)")
    print(json.dumps(report, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
