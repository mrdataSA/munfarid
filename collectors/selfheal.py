"""Self-healing job-card lookup for HTML sources, using Scrapling's adaptive parser.

When a site is parsed successfully, the fingerprint of its job-card element is saved.
If the site later changes its layout and our selector finds nothing, Scrapling
relocates the card by similarity and we read title/link/text from it.

Only Scrapling's PARSER is used. Its stealth fetchers, Cloudflare solving and proxy
options are never used in RIZQ (no block evasion). Optional: without Scrapling
installed, everything here is a no-op.
"""

import threading
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from backend import config

_lock = threading.Lock()
DB_FILE = Path(config.STORE_DIR) / "scrapling_adaptive.db"  # private dir, never served


def _first(node, css):
    found = node.css(css)
    return found[0] if found else None


def _selector_of(card_spec):
    """CSS selector equivalent of a {"tag", "cls"} card spec (None if not expressible)."""
    if not isinstance(card_spec, dict) or card_spec.get("attr"):
        return None
    tag, cls = card_spec.get("tag") or "", card_spec.get("cls") or ""
    if not tag and not cls:
        return None
    return tag + ("." + cls if cls else "")


def _page(html_text, page_url):
    try:
        from scrapling.parser import Selector
    except Exception:  # not installed / blocked native lib -> feature off
        return None
    key = "{0.scheme}://{0.netloc}{0.path}".format(urlsplit(page_url))
    return Selector(html_text, url=key, adaptive=True,
                    storage_args={"storage_file": str(DB_FILE), "url": key})


def remember(html_text, source, page_url):
    """Save the card fingerprint after a successful parse."""
    sel = _selector_of(source.get("card"))
    if not sel:
        return
    try:
        with _lock:
            page = _page(html_text, page_url)
            if page is not None:
                page.css(sel, auto_save=True)
    except Exception:
        pass  # healing is best-effort; never break a working source


def relocate(html_text, source, page_url):
    """Find job cards again after a layout change. Returns item dicts (maybe empty)."""
    sel = _selector_of(source.get("card"))
    if not sel:
        return []
    try:
        with _lock:
            page = _page(html_text, page_url)
            if page is None:
                return []
            found = page.css(sel, adaptive=True, percentage=30)
            if not found:
                return []
            cards = [found[0]] + list(found[0].find_similar())
    except Exception:
        return []
    items = []
    for card in cards:
        link = card if card.tag == "a" else _first(card, "a[href]")
        if link is None:
            continue
        title_node = _first(card, "h1, h2, h3, h4")
        if title_node is None:
            title_node = link
        title = " ".join(str(title_node.get_all_text(strip=True)).split())
        href = link.attrib.get("href", "")
        if not title or not href:
            continue
        items.append({
            "title": title[:200],
            "url": urljoin(page_url, href),
            "posted_date": "",
            "company": source.get("fixed_company") or "",
            "city": source.get("fixed_city") or "",
            "raw_text": " ".join(str(card.get_all_text(strip=True)).split())[:3000] or title,
            "needs_ai": False,
        })
    return items
