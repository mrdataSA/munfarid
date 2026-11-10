"""Co-op training (التدريب التعاوني) and scholarship (الابتعاث) programs.

Two kinds of content:
- curated programs: collectors/research/coop_programs.json and scholarships.json,
  researched from official pages (who offers it, usual opening months, conditions,
  official link, date verified). The opening months are the *usual* window, never
  a promise; collectors/programs_watch.py reads each official page every few days
  and records when it says applications are open.
- live posts: collected jobs whose title/text announces a co-op or scholarship
  (kind_of() below), shown on top of each section and sent to users who asked.
"""

import json
import os
import re
import threading

from . import normalize, storage

RESEARCH_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "collectors", "research")
FILES = {"coop": "coop_programs.json", "scholarship": "scholarships.json"}
KINDS = ("coop", "scholarship")
PROVIDER_TYPES = ("government", "semi_gov", "saudi_private", "foreign_in_ksa")

SCHOLARSHIP_RE = re.compile(r"ابتعاث|مبتعث|scholarship|منح[هة]? دراسي|منح دراسي|cadet|طيار متدرب|sponsorship program", re.I)
COOP_RE = re.compile(r"تدريب تعاوني|التدريب التعاوني|تعاوني للطلاب|co-?op\b|coop\b|internship|\bintern\b|تدريب صيفي|summer (?:training|internship)|"
                     r"تدريب ميداني|طلاب وطالبات الجامع", re.I)

_cache = {"mtimes": None, "items": []}
_lock = threading.Lock()


def kind_of(job):
    """'scholarship', 'coop' or None for a collected job/post."""
    text = (job.get("title") or "") + " " + (job.get("raw_text") or job.get("description") or "")[:400]
    if SCHOLARSHIP_RE.search(text):
        return "scholarship"
    if COOP_RE.search(text):
        return "coop"
    return None


def _iso(v):
    return isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) is not None


def _http(url):
    url = url.strip() if isinstance(url, str) else ""
    return url if url.startswith(("https://", "http://")) and len(url) < 500 else ""


def _str_list(v, n=20, each=200):
    if not isinstance(v, list):
        return []
    return [normalize.one_line(x, each) for x in v[:n] if isinstance(x, str) and x.strip()]


def _clean(item, kind):
    if not isinstance(item, dict) or not item.get("name") or not item.get("id"):
        return None
    months = [m for m in (item.get("open_months") or []) if isinstance(m, int) and 1 <= m <= 12]
    ptype = item.get("provider_type") if item.get("provider_type") in PROVIDER_TYPES else "saudi_private"
    return {
        "id": re.sub(r"[^a-z0-9-]", "", str(item["id"]).lower())[:60],
        "kind": kind,
        "subkind": item.get("kind") if item.get("kind") in ("coop", "training", "scholarship") else kind,
        "name": normalize.one_line(item.get("name"), 160),
        "name_en": normalize.one_line(item.get("name_en") or "", 160),
        "provider": normalize.one_line(item.get("provider") or "", 120),
        "provider_type": ptype,
        "summary": normalize.one_line(item.get("summary") or "", 600),
        "who": normalize.one_line(item.get("who") or "", 300),
        "conditions": _str_list(item.get("conditions")),
        "benefits": _str_list(item.get("benefits")),
        "majors": _str_list(item.get("majors"), 30, 80),
        "destinations": _str_list(item.get("destinations"), 20, 80),
        "cities": _str_list(item.get("cities"), 20, 60),
        "duration": normalize.one_line(item.get("duration") or "", 160),
        "opens": normalize.one_line(item.get("opens") or "", 300),
        "open_months": sorted(set(months)),
        "rolling": bool(item.get("rolling")),
        # A confirmed current window (exact dates from the official page / news), when known
        "open_from": item.get("open_from") if _iso(item.get("open_from")) else "",
        "open_until": item.get("open_until") if _iso(item.get("open_until")) else "",
        "last_opened": normalize.one_line(item.get("last_opened") or "", 300),
        "apply_url": _http(item.get("apply_url")),
        "source_url": _http(item.get("source_url")),
        "verified": normalize.one_line(item.get("verified") or "", 20),
        "confidence": item.get("confidence") if item.get("confidence") in ("high", "medium", "low") else "medium",
    }


def _load():
    paths = {k: os.path.join(RESEARCH_DIR, f) for k, f in FILES.items()}
    mtimes = tuple(os.path.getmtime(p) if os.path.exists(p) else 0 for p in paths.values())
    with _lock:
        if _cache["mtimes"] == mtimes:
            return _cache["items"]
        items, seen = [], set()
        for kind, path in paths.items():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            for raw in data if isinstance(data, list) else []:
                it = _clean(raw, kind)
                if it and it["id"] and (kind, it["id"]) not in seen:
                    seen.add((kind, it["id"]))
                    items.append(it)
        _cache.update(mtimes=mtimes, items=items)
        return items


def all_programs():
    """Curated programs + what the watcher last saw on each official page."""
    watch = storage.read_cached("programs_watch")
    out = []
    for it in _load():
        w = watch.get(it["kind"] + ":" + it["id"]) or {}
        out.append(dict(it, open_signal=bool(w.get("open_signal")), open_seen_at=w.get("open_seen_at", ""),
                        checked_at=w.get("checked_at", "")))
    return out


def public_programs(kind=None):
    items = [p for p in all_programs() if not kind or p["kind"] == kind]
    return {"programs": items, "kinds": list(KINDS)}
