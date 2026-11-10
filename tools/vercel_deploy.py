"""Deploy this folder to Vercel through its REST API (no Vercel CLI needed).

    VERCEL_TOKEN=... VERCEL_ORG_ID=... VERCEL_PROJECT_ID=... python tools/vercel_deploy.py [--preview]

Uploads every file not excluded by .vercelignore (the snapshot and pub/ included), creates the
deployment, and waits until Vercel has built it. Exit code 1 if the build failed.
"""

import fnmatch
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = "https://api.vercel.com"
ALWAYS_SKIP = {".git", ".vercel", "node_modules"}


def _ignore_patterns():
    pats = []
    path = os.path.join(ROOT, ".vercelignore")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#"):
                pats.append(line)
    return pats


def _ignored(rel, pats):
    parts = rel.split("/")
    if parts[0] in ALWAYS_SKIP:
        return True
    for p in pats:
        p = p.lstrip("/")
        if p.endswith("/"):                       # a folder, anywhere if it starts with **/
            d = p.rstrip("/")
            if d.startswith("**/"):
                if d[3:] in parts[:-1]:
                    return True
            elif rel.startswith(d + "/"):
                return True
        elif p.startswith("**/"):
            if fnmatch.fnmatch(parts[-1], p[3:]):
                return True
        elif "/" in p:
            if fnmatch.fnmatch(rel, p) and rel.count("/") == p.count("/"):
                return True
        elif fnmatch.fnmatch(parts[-1], p):   # no slash: a file name at any depth (like .gitignore)
            return True
    return False


def files():
    pats = _ignore_patterns()
    out = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        rel_dir = os.path.relpath(dirpath, ROOT).replace("\\", "/")
        rel_dir = "" if rel_dir == "." else rel_dir + "/"
        dirnames[:] = [d for d in dirnames if not _ignored(rel_dir + d + "/x", pats)]
        for name in filenames:
            rel = rel_dir + name
            if not _ignored(rel, pats):
                out.append(rel)
    return sorted(out)


def _call(method, path, token, team, body=None, headers=None):
    sep = "&" if "?" in path else "?"
    req = urllib.request.Request(f"{API}{path}{sep}teamId={team}", data=body, method=method,
                                 headers={"Authorization": f"Bearer {token}", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Vercel API {method} {path}: HTTP {e.code} {e.read()[:300]!r}")


def main():
    token, team, project = (os.environ.get(k, "") for k in ("VERCEL_TOKEN", "VERCEL_ORG_ID", "VERCEL_PROJECT_ID"))
    if not (token and team and project):
        raise SystemExit("VERCEL_TOKEN, VERCEL_ORG_ID and VERCEL_PROJECT_ID are required")
    manifest = []
    for rel in files():
        data = open(os.path.join(ROOT, rel), "rb").read()
        sha = hashlib.sha1(data).hexdigest()
        _call("POST", "/v2/files", token, team, body=data,
              headers={"Content-Type": "application/octet-stream", "x-vercel-digest": sha,
                       "Content-Length": str(len(data))})
        manifest.append({"file": rel, "sha": sha, "size": len(data)})
    body = {"name": "munfarid", "project": project, "files": manifest,
            "projectSettings": {"framework": None}}
    if "--preview" not in sys.argv:
        body["target"] = "production"
    dep = _call("POST", "/v13/deployments?forceNew=1&skipAutoDetectionConfirmation=1", token, team,
                body=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    dep_id = dep["id"]
    print(f"deployment {dep_id}: {len(manifest)} files uploaded, building...")
    deadline = time.time() + 600
    while time.time() < deadline:
        state = _call("GET", f"/v13/deployments/{dep_id}", token, team).get("readyState")
        if state in ("READY", "ERROR", "CANCELED"):
            print("state:", state)
            sys.exit(0 if state == "READY" else 1)
        time.sleep(5)
    raise SystemExit("deployment did not finish in 10 minutes")


if __name__ == "__main__":
    main()
