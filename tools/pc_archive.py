"""The PC archive: every job that leaves the live list is kept here in full, on Raed's PC (E: drive).

The cloud keeps the live jobs (the bot searches them); when a job leaves the live list (over 45 days
old, or over the size cap), and when the free database nears its 500 MB, the cloud queues it in
rizq.archive_outbox. The helper on the PC (tools/saudi_helper.py, every 30 minutes) calls sync():

  1. takes a batch from the site (/api/helper/archive, helper key)
  2. writes it to today's journal file (append-only, flushed to disk)       <- second copy
  3. saves it in the archive database in one transaction (SQLite, WAL, synchronous=FULL)
  4. only then tells the site to delete it (/api/helper/archive/ack)

A power cut at any step loses nothing: before step 4 the cloud still has the batch and sends it
again (saving is idempotent: same id = same row); after it, the journal and the database have it.
Once a day the database is checked (quick_check) and copied to backup/ (7 copies kept). If it is
ever damaged, the archive stops taking batches (they wait in the cloud) and says so on the dashboard;
`python tools/pc_archive.py --rebuild` rebuilds it from the journal files.

Folder: E:\\Munfarid\\archive (MUNFARID_ARCHIVE_DIR overrides; without an E: drive,
%USERPROFILE%\\.munfarid\\archive).
"""

import datetime
import gzip
import json
import os
import shutil
import sqlite3
import sys
import time

BACKUPS_KEPT = 7
TAKE = 1500


def archive_dir():
    d = os.environ.get("MUNFARID_ARCHIVE_DIR")
    if not d:
        d = r"E:\Munfarid\archive" if os.path.isdir("E:\\") else os.path.join(
            os.path.expanduser("~"), ".munfarid", "archive")
    os.makedirs(os.path.join(d, "journal"), exist_ok=True)
    os.makedirs(os.path.join(d, "backup"), exist_ok=True)
    return d


SCHEMA = """
create table if not exists jobs (
  id text primary key, title text, company text, city text, source text, posted_date text,
  url text, apply_url text, data text not null, saved_at text not null);
create index if not exists jobs_posted on jobs (posted_date);
create index if not exists jobs_title on jobs (title);
create index if not exists jobs_source on jobs (source);
create table if not exists rows (id text primary key, data text not null, saved_at text not null);
create table if not exists meta (k text primary key, v text)
"""


def connect(d=None):
    con = sqlite3.connect(os.path.join(d or archive_dir(), "munfarid_archive.db"), timeout=60)
    con.execute("pragma journal_mode=wal")
    con.execute("pragma synchronous=full")   # a commit is on the disk before it returns
    con.executescript(SCHEMA)
    return con


def _now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def save(con, items):
    """One transaction: all of the batch or nothing."""
    now = _now()
    with con:
        for it in items:
            iid, kind, data = it.get("id") or "", it.get("kind"), it.get("data")
            if not isinstance(data, dict) or not iid:
                continue
            text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            if kind == "job":
                con.execute("insert or replace into jobs values (?,?,?,?,?,?,?,?,?,?)",
                            (iid[4:], data.get("title"), data.get("company"), data.get("city"), data.get("source"),
                             data.get("posted_date"), data.get("url"), data.get("apply_url"), text, now))
            elif kind == "row":
                con.execute("insert or replace into rows values (?,?,?)", (iid[4:], text, now))


def journal(d, items):
    """Append the batch to today's journal (gzip members can be appended; a member cut by a power
    failure only loses that batch, which the cloud still holds until the ack)."""
    path = os.path.join(d, "journal", datetime.date.today().isoformat() + ".jsonl.gz")
    with open(path, "ab") as raw:
        with gzip.GzipFile(fileobj=raw, mode="ab") as gz:
            for it in items:
                gz.write((json.dumps(it, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
        raw.flush()
        os.fsync(raw.fileno())


def _meta(con, k, v=None):
    if v is None:
        r = con.execute("select v from meta where k=?", (k,)).fetchone()
        return r[0] if r else None
    with con:
        con.execute("insert or replace into meta values (?,?)", (k, v))


def daily_care(con, d):
    """Once a day: integrity check, then a backup copy (SQLite's own online backup)."""
    today = datetime.date.today().isoformat()
    if _meta(con, "care_day") == today:
        return _meta(con, "backup") or ""
    ok = con.execute("pragma quick_check").fetchone()[0]
    if ok != "ok":
        raise RuntimeError("قاعدة الأرشيف متضررة (" + str(ok)[:80] + "): شغّل pc_archive.py --rebuild")
    dest = os.path.join(d, "backup", f"munfarid_archive-{today}.db")
    tmp = dest + ".tmp"
    b = sqlite3.connect(tmp)
    try:
        con.backup(b)
    finally:
        b.close()   # closed before the rename (Windows refuses to rename an open file)
    os.replace(tmp, dest)
    for old in sorted(f for f in os.listdir(os.path.join(d, "backup")) if f.endswith(".db"))[:-BACKUPS_KEPT]:
        os.remove(os.path.join(d, "backup", old))
    _meta(con, "care_day", today)
    _meta(con, "backup", dest)
    return dest


def stats(con, d):
    free = shutil.disk_usage(d).free // (1024 ** 3)
    return {"jobs": con.execute("select count(*) from jobs").fetchone()[0],
            "rows": con.execute("select count(*) from rows").fetchone()[0], "free_gb": int(free), "path": d}


def sync(api, seconds=180, log=None):
    """Move what the cloud queued into the archive. api(method, path, body=None) -> dict.
    Returns the numbers for the dashboard (or {"error": ...})."""
    d = archive_dir()
    con = connect(d)
    saved = 0
    try:
        backup = daily_care(con, d)
        deadline = time.time() + seconds
        while time.time() < deadline:
            items = (api("GET", f"/api/helper/archive?limit={TAKE}") or {}).get("items") or []
            if not items:
                break
            journal(d, items)
            save(con, items)
            api("POST", "/api/helper/archive/ack", {"ids": [it["id"] for it in items if it.get("id")]})
            saved += len(items)
            if len(items) < TAKE:
                break
        out = dict(stats(con, d), saved=saved)
        if backup:
            out["backup"] = os.path.basename(backup)
        return out
    finally:
        con.close()


def rebuild():
    """Rebuild the archive database from the journal files (after damage). The old file is kept."""
    d = archive_dir()
    db = os.path.join(d, "munfarid_archive.db")
    if os.path.exists(db):
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(db + ext):
                os.replace(db + ext, db + ext + f".damaged-{stamp}")
    con = connect(d)
    n = 0
    for f in sorted(os.listdir(os.path.join(d, "journal"))):
        batch = []
        try:
            with gzip.open(os.path.join(d, "journal", f), "rt", encoding="utf-8") as gz:
                for line in gz:
                    try:
                        batch.append(json.loads(line))
                    except ValueError:
                        pass
        except (EOFError, OSError):
            pass   # a member cut by a power failure: keep what was read
        save(con, batch)
        n += len(batch)
    print("rebuilt from the journal:", n, "records;", stats(con, d))
    con.close()


if __name__ == "__main__":
    if "--rebuild" in sys.argv:
        rebuild()
    else:
        c = connect()
        print(stats(c, archive_dir()))
        c.close()
