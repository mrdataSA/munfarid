"""RSS 2.0 / Atom collector (aggregator sites, WordPress /feed/ etc.)."""

import xml.etree.ElementTree as ET

from . import base, origin

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc": "http://purl.org/dc/elements/1.1/",
}


def _t(el, path, ns=NS):
    found = el.find(path, ns)
    return (found.text or "").strip() if found is not None and found.text else ""


def parse_feed(text):
    try:
        root = ET.fromstring(text.lstrip("﻿").strip())
    except ET.ParseError as e:
        raise base.FetchError(f"invalid feed XML: {e}")
    items = []
    if root.tag.endswith("rss") or root.find("channel") is not None:
        for it in root.iter("item"):
            body = _t(it, "content:encoded") or _t(it, "description")
            items.append({
                "title": _t(it, "title"),
                "url": _t(it, "link"),
                "posted_date": _t(it, "pubDate") or _t(it, "dc:date"),
                "raw_text": body,
                "categories": [c.text for c in it.findall("category") if c.text],
            })
    else:  # Atom
        for e in root.iter("{%s}entry" % NS["atom"]):
            link = ""
            for l in e.findall("atom:link", NS):
                if l.get("rel") in (None, "alternate"):
                    link = l.get("href") or ""
                    break
            items.append({
                "title": _t(e, "atom:title"),
                "url": link,
                "posted_date": _t(e, "atom:published") or _t(e, "atom:updated"),
                "raw_text": _t(e, "atom:content") or _t(e, "atom:summary"),
                "categories": [c.get("term") for c in e.findall("atom:category", NS) if c.get("term")],
            })
    return items


def collect(source):
    text = base.fetch(source["url"], check_robots=source.get("check_robots", True))
    items = parse_feed(text)
    wanted = source.get("category_filter")  # e.g. general news feeds: keep only the jobs category
    if wanted:
        items = [it for it in items if any(wanted in (c or "") for c in it.get("categories", []))]
    unwanted = source.get("category_exclude") or []   # e.g. a relay's "free courses" posts
    if unwanted:
        items = [it for it in items if not any(u in (c or "") for c in it.get("categories", []) for u in unwanted)]
    for it in items:
        it["needs_ai"] = False
        if source.get("apply_from_feed") and it.get("raw_text") and it.get("url"):
            # relay sites put the official apply link in the post itself: no extra request
            it["apply_url"] = origin.pick(it["raw_text"], it["url"])
        cats = " ".join(it.get("categories", []))
        if "تدريب" in cats and "تدريب" not in it["title"]:
            it["raw_text"] = (it.get("raw_text") or "") + " (تدريب تعاوني)"
    return items[: source.get("max_items", 50)]
