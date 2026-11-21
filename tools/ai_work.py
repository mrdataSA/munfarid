"""Finish the AI work the cloud could not do today (Groq's free daily budget ran out), from Raed's PC.

The cloud publishes it each full run (/api/helper/ai_todo): duplicate pairs to judge and announcements
whose job titles are unknown, with the exact prompts it uses. This tool saves them as files for an
assistant on this PC (Claude) to answer, then sends the answers back (/api/helper/ai_done, helper key).
The collectors take the answers on their next full run.

    python tools/ai_work.py fetch [folder]     # writes pairs_NNN.json / roles_NNN.json (+ prompts)
    python tools/ai_work.py send  [folder]     # sends every *_answers.json in the folder
"""

import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.saudi_helper import _api  # noqa: E402  (the helper key, retries, the site address)

CHUNK = {"pairs": 100, "roles": 25}


def fetch(folder):
    os.makedirs(folder, exist_ok=True)
    for kind in ("pairs", "roles"):
        items, off, prompt = [], 0, ""
        while True:
            r = _api("GET", f"/api/helper/ai_todo?kind={kind}&offset={off}&limit=1000")
            prompt = r.get("prompt") or prompt
            items += r.get("items") or []
            off += 1000
            if off >= (r.get("left") or 0):
                break
        with open(os.path.join(folder, f"{kind}_prompt.txt"), "w", encoding="utf-8") as f:
            f.write(prompt)
        for n in range(0, len(items), CHUNK[kind]):
            with open(os.path.join(folder, f"{kind}_{n // CHUNK[kind]:03d}.json"), "w", encoding="utf-8") as f:
                json.dump(items[n:n + CHUNK[kind]], f, ensure_ascii=False, indent=0)
        print(f"{kind}: {len(items)} to do, in {(len(items) + CHUNK[kind] - 1) // CHUNK[kind]} files")


def send(folder):
    pairs, roles = {}, {}
    for p in sorted(glob.glob(os.path.join(folder, "*_answers.json"))):
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        pairs.update({k: v for k, v in (d.get("pairs") or {}).items() if isinstance(v, bool)})
        roles.update({k: v for k, v in (d.get("roles") or {}).items() if isinstance(v, list)})
    saved = 0
    keys = list(pairs)
    for n in range(0, max(len(keys), 1), 1500):
        part = {"pairs": {k: pairs[k] for k in keys[n:n + 1500]}}
        if n == 0:
            part["roles"] = roles
        saved += (_api("POST", "/api/helper/ai_done", part) or {}).get("saved", 0)
    print(f"sent {len(pairs)} pair answers ({sum(pairs.values())} duplicates) and {len(roles)} announcements; saved {saved}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    where = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.expanduser("~"), ".munfarid", "ai_work")
    if cmd == "fetch":
        fetch(where)
    elif cmd == "send":
        send(where)
    else:
        print(__doc__)
