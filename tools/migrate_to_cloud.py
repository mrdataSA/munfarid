"""One-time move of the local data to the cloud database.

    DATABASE_URL=... python tools/migrate_to_cloud.py

Creates the tables, copies the shared collections (subscribers, settings, AI cache...)
and uploads data/store as the backup the first GitHub Actions run starts from.
Refuses to run once the cloud has its own data (it would replace it with the local copy),
unless --force is given.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend import config, pgstore, storage  # noqa: E402

SKIP = {"sessions"}                       # admin logins: log in again in the cloud
DROP_STATE_KEYS = {"offset", "seen_updates"}  # long-polling position: meaningless with a webhook


def main():
    url = config.DATABASE_URL
    if not url:
        sys.exit("set DATABASE_URL first")
    config.DATABASE_URL = ""              # read the local files
    local = {name: storage.read(name) for name in sorted(storage.SHARED - SKIP)}
    local["bot_state"] = {k: v for k, v in local["bot_state"].items() if k not in DROP_STATE_KEYS}
    config.DATABASE_URL = url             # write the database
    pgstore.init_schema()
    if storage.read("subscribers") and "--force" not in sys.argv:
        sys.exit("the cloud already has subscribers: not overwriting it (use --force only if you are sure)")
    for name, data in local.items():
        storage.write(name, data)
        print(f"{name}: {len(data)}")
    from tools import cloud_run
    with storage.edit("bot_state") as st:
        st.pop("store_backup_date", None)
    cloud_run.backup_daily()
    print("store backup uploaded")


if __name__ == "__main__":
    main()
