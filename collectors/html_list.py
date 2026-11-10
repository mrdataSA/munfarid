"""Generic config-driven HTML job-list collector.

A source in sources.json describes where the job cards are:
  {"type": "html", "url": "...",                 # or "urls": [...] for several pages
   "card":    {"tag": "div", "cls": "job-card"},
   "title":   {"tag": "h2"},                      # text of the first match inside the card
   "link":    {"tag": "a"} | "self",             # href of the match ("self" = the card is the <a>)
   "date":    {"tag": "time", "attr": "datetime"},
   "company": {"tag": "div", "cls": "company"},
   "location":{"tag": "li", "cls": "loc", "attr": "title"},
   "summary": {...} | "self",                     # "self" = all text in the card
   "fixed_company": "...", "fixed_city": "...",
   "render": true, "wait_selector": "css"}       # optional: JS-built page via Playwright
Any field spec may carry "attr" to read an attribute instead of the text.

Paging (big boards): "page_url": "https://.../jobs?page={page}" (or a list of such patterns, one per
section), "pages": N reads the
newest N pages every run; "backfill_pages": M with "max_pages": P also reads M OLDER
pages per run, rotating through the archive (pages N+1..P) so the whole board is
covered over several runs without a heavy single run. Paging stops at an empty page.
"""

from urllib.parse import urljoin

from backend import storage

from . import base, dom, selfheal


def _node(card, spec):
    if spec == "self":
        return card
    if not spec:
        return dom.EMPTY
    return card.find(spec.get("tag"), spec.get("cls"))


def _value(card, spec):
    node = _node(card, spec)
    if not node:
        return ""
    if isinstance(spec, dict) and spec.get("attr"):
        return node.attrs.get(spec["attr"], "").strip()
    return node.text()


def parse_list(html_text, source, page_url):
    root = dom.parse(html_text)
    card_spec = source["card"]
    items = []
    for card in root.find_all(card_spec.get("tag"), card_spec.get("cls")):
        title_node = _node(card, source.get("title"))
        title = title_node.text() if title_node else ""
        if not title:
            continue
        link_spec = source.get("link")
        link_node = _node(card, link_spec) if link_spec else (title_node if title_node.tag == "a" else title_node.find("a"))
        href = link_node.attrs.get("href", "") if link_node else ""
        items.append({
            "title": title,
            "url": urljoin(page_url, href) if href else "",
            "posted_date": _value(card, source.get("date")),
            "company": source.get("fixed_company") or _value(card, source.get("company")),
            "city": source.get("fixed_city") or _value(card, source.get("location")),
            "raw_text": _value(card, source.get("summary")) or title,
            "needs_ai": False,
        })
    return items


def _page_urls(source):
    patterns = source.get("page_url")
    if not patterns:
        return source.get("urls") or [source["url"]], 0
    if isinstance(patterns, str):
        patterns = [patterns]   # several patterns = several sections (e.g. categories)
    fresh = max(1, source.get("pages", 3))
    urls = [p.format(page=i) for p in patterns for i in range(1, fresh + 1)]
    back, max_pages = source.get("backfill_pages", 0), source.get("max_pages", 0)
    if back and max_pages > fresh:
        with storage.edit("sources_status") as st:
            entry = st.setdefault(source["key"], {})
            start = entry.get("backfill_next", fresh + 1)
            if start > max_pages or start <= fresh:
                start = fresh + 1
            entry["backfill_next"] = start + back
        urls += [p.format(page=i) for p in patterns for i in range(start, min(start + back, max_pages + 1))]
    return urls, fresh * len(patterns)


def collect(source):
    urls, fresh = _page_urls(source)
    items, errors, seen = [], [], set()
    for n, url in enumerate(urls):
        try:
            if source.get("render"):  # JS-built list: render with headless Chromium
                text = base.fetch_rendered(url, wait_selector=source.get("wait_selector"),
                                           check_robots=source.get("check_robots", True))
            else:
                text = base.fetch(url, check_robots=source.get("check_robots", True),
                                  jitter=source.get("jitter", 0.0))
            found = parse_list(text, source, url)
            if found:
                if n == 0:  # only the first page is ever relocated, so only its fingerprint matters
                    selfheal.remember(text, source, url)
            elif n == 0:  # first page empty: layout changed? let Scrapling relocate the cards
                found = selfheal.relocate(text, source, url)
                if found:
                    # Relocation is a similarity guess: always run the is-this-a-job filter on it
                    source["filter_jobs"] = True
                    source["_note"] = "تغيّر تصميم الموقع، وتم إيجاد الوظائف تلقائياً (حدّث المحددات)"
            new = [it for it in found if (it["url"] or it["title"]) not in seen]
            seen.update(it["url"] or it["title"] for it in new)
            items.extend(new)
            if source.get("page_url") and not new and n < fresh and isinstance(source["page_url"], str):
                break  # past the last page of the board
        except base.Blocked:
            raise  # stop this source on any block
        except base.FetchError as e:
            errors.append(str(e))
            if e.status in (502, 503, 504):
                break  # the site is overloaded: stop asking it this run
    if not items and errors:
        raise base.FetchError("; ".join(errors[:2]))
    if source.get("page_url"):
        source.setdefault("_note", f"{len(urls)} صفحة")
    return items[: source.get("max_items", 60 if not source.get("page_url") else 5000)]
