"""Saudi helper: fetch what refuses cloud servers, from a home connection in Saudi Arabia.

Some sites (Jadarat behind Cloudflare, Tanqeeb, some relay sites) answer GitHub's servers with
"403 / Attention Required", so the cloud never reads them. Raed's old PC stays on all day, and
Windows runs this ONCE A DAY for up to one hour (tools/install_saudi_helper.ps1):

1. every source marked "cloud": "helper" in collectors/sources.json, plus every source the cloud
   found blocked (403 / challenge page) on its last run (never LinkedIn or X, never a 429 rate
   limit: see collectors/runner.helper_can_fetch), read the same polite way the cloud would
   (robots.txt respected, same pace), with their relay pages followed to the original links;
2. the relay pages the cloud could not open (collectors/origin.py): the original apply links behind them.

How it talks to the cloud (no database password on the PC):
- normally through the site, with the helper key (%USERPROFILE%\\.munfarid\\helper_key, set by the
  installer; the cloud keeps only its hash, from the HELPER_KEY secret). It reads the cloud's source
  status and the waiting relay pages, works on a private local copy (%USERPROFILE%\\.munfarid\\helper_data),
  and sends each source's jobs as soon as they are fetched;
- or straight to the database when DATABASE_URL is set (the older setup).
The next hourly cloud run stores what arrived and sends the alerts. When the PC is off, nothing
breaks: those sources just wait for the next day.

    python tools/saudi_helper.py               # what the daily task runs
    python tools/saudi_helper.py --minutes 5   # a short test run
    python tools/saudi_helper.py --check       # only checks the connection to the site
"""

import json
import logging
import os
import socket
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRET_DIR = os.path.join(os.path.expanduser("~"), ".munfarid")
sys.path.insert(0, ROOT)
os.chdir(ROOT)


def _secret(name):
    try:
        with open(os.path.join(SECRET_DIR, name), encoding="utf-8-sig") as f:
            return f.read().strip()
    except OSError:
        return ""


# Before the project's modules read their settings: the database link (older setup) or a private
# local copy for the site mode (never the project's own data folder, never the .env file)
if not os.environ.get("DATABASE_URL") and _secret("database_url"):
    os.environ["DATABASE_URL"] = _secret("database_url")
if not os.environ.get("DATABASE_URL"):
    os.environ.setdefault("RIZQ_DATA_DIR", os.path.join(SECRET_DIR, "helper_data"))
os.environ.setdefault("RIZQ_NO_ENV_FILE", "1")

from backend import config, storage  # noqa: E402

KEEP_BATCHES = 6
WINDOW_MINUTES = 55          # one hour a day (the task stops it at 70 minutes in any case)
ORIGIN_SHARE = 0.25          # time kept for the relay pages the cloud could not open
SITE = (os.environ.get("MUNFARID_SITE") or "https://munfaridsa.vercel.app").rstrip("/")
CHUNK_BYTES = 1_500_000      # one upload stays well under the site's 3 MB request limit
log = logging.getLogger("rizq.helper")


# ---------------------------------------------------------------- the site (helper key)

class SiteError(Exception):
    pass


def _api(method, path, body=None, tries=4):
    key = os.environ.get("HELPER_KEY") or _secret("helper_key")
    if not key:
        raise SiteError("no helper key (run tools\\install_saudi_helper.bat again)")
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    for n in range(tries):
        req = urllib.request.Request(SITE + path, data=data, method=method, headers={
            "X-Helper-Key": key, "Content-Type": "application/json", "User-Agent": "MunfaridSaudiHelper/1"})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403, 413):
                raise SiteError(f"{path}: HTTP {e.code}") from None
            err = f"HTTP {e.code}"
        except (urllib.error.URLError, socket.timeout, ConnectionError, ValueError) as e:
            err = str(e)
        time.sleep(5 * (n + 1))
    raise SiteError(f"{path}: {err}")


def _chunks(items):
    """Split a batch's items so each upload stays small."""
    out, cur, size = [], [], 0
    for it in items:
        n = len(json.dumps(it, ensure_ascii=False).encode("utf-8"))
        if cur and size + n > CHUNK_BYTES:
            out.append(cur)
            cur, size = [], 0
        cur.append(it)
        size += n
    if cur or not out:
        out.append(cur)
    return out


def send_batch_to_site(key, batch):
    parts = _chunks(batch["items"])
    for i, part in enumerate(parts):
        at = batch["at"] if len(parts) == 1 else f"{batch['at']}#{i + 1}"
        _api("POST", "/api/helper/batch", {"key": key, "batch": dict(batch, items=part, at=at)})


def keep_batch_locally(key, batch):
    with storage.edit("inbox") as inbox:
        box = inbox.setdefault(key, {"batches": []})
        box["batches"] = (box.get("batches") or [])[-(KEEP_BATCHES - 1):] + [batch]


# ---------------------------------------------------------------- the work

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


def run(minutes=WINDOW_MINUTES, send=keep_batch_locally, status=None):
    """Fetch every wanted source, then the waiting relay pages. `send(key, batch)` hands each
    source's jobs to the cloud as soon as they are fetched."""
    from collectors import base, origin, runner
    origin.HELPER_MODE = True
    started = time.time()
    deadline = started + minutes * 60
    origin_time = minutes * 60 * ORIGIN_SHARE
    status = storage.read("sources_status") if status is None else status
    sources = wanted_sources(runner.load_sources(), status)
    report = {"at": storage.now_iso(), "sources": {}, "origin_links": 0, "host": socket.gethostname()[:60]}
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
        try:
            send(src["key"], batch)
            report["sources"][src["key"]] = len(items)
        except Exception as e:
            log.exception("%s: sending failed", src["key"])
            report["sources"][src["key"]] = f"not sent: {e}"[:120]
        log.info("%s: %d items (%.0fs)", src["key"], len(items), time.time() - t0)
    left = deadline - time.time()
    if left > 30:
        try:
            report["origin_links"] = origin.resolve_pending(left)
        except Exception:
            log.exception("relay pages failed")
    report["minutes"] = round((time.time() - started) / 60, 1)
    return report


def run_via_site(minutes):
    """The normal mode: the site hands out the work and takes the results (helper key)."""
    from collectors import origin
    work = _api("GET", "/api/helper/work")
    pending = list(work.get("pending") or [])
    storage.write("sources_status", work.get("sources_status") or {})
    storage.write("inbox", {origin.INBOX_KEY: {"pending": pending}} if pending else {})
    report = run(minutes, send=send_batch_to_site, status=work.get("sources_status") or {})
    box = storage.read("inbox").get(origin.INBOX_KEY) or {}
    left = set(box.get("pending") or [])
    tried = [u for u in pending if u not in left]
    if tried or box.get("done"):
        _api("POST", "/api/helper/origin", {"tried": tried, "done": box.get("done") or {}})
    _api("POST", "/api/helper/report", {"report": report})
    return report


def run_with_database(minutes):
    report = run(minutes)
    with storage.edit("bot_state") as st:   # shown on the dashboard: when the helper last ran and what it did
        st["helper_last"] = report
    return report


def main():
    os.makedirs(config.LOG_DIR, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.FileHandler(os.path.join(config.LOG_DIR, "saudi_helper.log"), encoding="utf-8"),
                                  logging.StreamHandler()])
    if "--check" in sys.argv:
        if config.DATABASE_URL:
            print(f"OK: connected to the database ({len(storage.read('sources_status'))} sources)")
            return
        work = _api("GET", "/api/helper/work")
        print(f"OK: connected to {SITE} ({len(work.get('sources_status') or {})} sources, "
              f"{len(work.get('pending') or [])} relay pages waiting)")
        return
    minutes = WINDOW_MINUTES
    if "--minutes" in sys.argv:
        minutes = max(2, int(sys.argv[sys.argv.index("--minutes") + 1]))
    try:
        report = run_with_database(minutes) if config.DATABASE_URL else run_via_site(minutes)
    except SiteError as e:
        log.error("helper stopped: %s", e)
        try:   # the dashboard shows why (when the site itself is reachable)
            _api("POST", "/api/helper/report", {"report": {"at": storage.now_iso(), "error": str(e),
                                                             "host": socket.gethostname()[:60]}}, tries=1)
        except SiteError:
            pass
        sys.exit(1)
    log.info("helper done: %s", report)


if __name__ == "__main__":
    main()
