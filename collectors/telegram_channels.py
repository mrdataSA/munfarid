"""Public Telegram channels via the official web preview https://t.me/s/<channel>.

Reads PUBLIC channels only (no login, like a normal visitor). Channels are
listed in collectors/channels.txt (one handle per line, # for comments).
Posts are unstructured, so they are marked needs_ai for Groq extraction.
"""

import os
import re
from datetime import date, timedelta

from . import base, dom

CHANNELS_FILE = os.path.join(os.path.dirname(__file__), "channels.txt")
HANDLE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
MAX_AGE_DAYS = 14
# Share/contact links are not apply links
SKIP_LINK_HOSTS = ("t.me/", "telegram.me/", "wa.me/", "whatsapp.com", "api.whatsapp", "twitter.com/intent",
                   "x.com/intent", "facebook.com/sharer", "instagram.com", "snapchat.com", "tiktok.com")


def load_channels():
    try:
        with open(CHANNELS_FILE, "r", encoding="utf-8") as f:
            lines = [l.split("#", 1)[0].strip().lstrip("@") for l in f]
    except OSError:
        return []
    return [l for l in lines if HANDLE_RE.match(l)]


def parse_channel(html_text, handle):
    root = dom.parse(html_text)
    posts = []
    cutoff = (date.today() - timedelta(days=MAX_AGE_DAYS)).isoformat()
    for msg in root.find_all("div", cls="tgme_widget_message"):
        post_id = msg.attrs.get("data-post", "")
        text_node = msg.find("div", cls="tgme_widget_message_text")
        if not text_node:
            continue
        text = text_node.text(sep="\n")
        when = msg.find("time").attrs.get("datetime", "")[:10]
        if when and when < cutoff:
            continue
        links = [a.attrs.get("href", "") for a in text_node.find_all("a")]
        external = next((l for l in links if l.startswith("http") and not any(
            bad in l for bad in SKIP_LINK_HOSTS)), "")
        first_line = next((l for l in text.split("\n") if len(l.strip()) > 5), text[:80])
        posts.append({
            "title": first_line[:140],
            "raw_text": text,
            "url": external or (f"https://t.me/{post_id}" if post_id else f"https://t.me/s/{handle}"),
            "post_url": f"https://t.me/{post_id}" if post_id else "",
            "posted_date": when,
            "needs_ai": True,
            "channel": handle,
        })
    return posts


def collect(source):
    channels = source.get("channels") or load_channels()
    items, errors, ok = [], [], 0
    for handle in channels[: source.get("max_channels", 30)]:
        try:
            page = base.fetch(f"https://t.me/s/{handle}", min_interval=3.0, jitter=2.0)
            items.extend(parse_channel(page, handle))
            ok += 1
        except base.Blocked:
            raise  # never keep hitting t.me after a 403/429
        except base.FetchError as e:
            errors.append(f"{handle}: {e}")   # one dead channel must not stop the rest
    if not ok and errors:
        raise base.FetchError("; ".join(errors[:3]))
    source["_note"] = f"{ok}/{len(channels)} قناة" + (f"، أخطاء: {len(errors)}" if errors else "")
    return items
