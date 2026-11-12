"""RIZQ Telegram bot: the whole service in one chat.

Job seekers (private chat)
- write one or more job titles ("محاسب، accountant") -> every collected job that matches,
  from all sources or only the ones they pick, newest first, PAGE per message with "more".
  Each title is also searched under the other names the same job is posted with (titles.py).
- send a CV (PDF / Word / text) or a short "about me": personal details are stripped first,
  the AI reads tasks, courses and experience and adds the job titles that fit, even when
  named differently. The file and the text are never stored (read in memory), only the
  titles and skills.
- new matching jobs arrive once a day (BOT_DIGEST_HOUR) or as soon as they are found.

Groups / channels
- an admin writes /feed (all jobs) or /feed محاسب، مهندس مدني, and the bot posts each new
  matching job there, one message per job, spaced out and capped per day.

Honest relay: every job links to its original posting. Data is kept in the
"subscribers" collection and fully deleted with /delete.

Concurrency: updates run on the poller's pool (one task per chat at a time), the
Scheduler thread runs feeds + the daily message, and new collected jobs are handed to
the alerts thread (submit_new_jobs). Every subscriber change goes through storage.edit;
no network or AI call ever runs inside it.
"""

import contextlib
import hashlib
import html
import logging
import queue
import re
import secrets
import threading
import time
from collections import Counter
from datetime import date, datetime, timedelta

from . import ai, catalog, config, cvtext, normalize, storage, titles, users
from . import telegram_bot as tg

log = logging.getLogger("rizq.bot")

PAGE = 8
MAX_TITLES = 15
MAX_RESULTS = 300
SENT_KEEP = 400
INSTANT_DAILY_CAP = 30
AI_DAILY_PER_CHAT = 12          # title expansions + CV reads per person per day (Groq free tier)
AI_DAILY_PER_FEED = 3           # title expansions per group/channel per day
BOT_AI_SHARE = 0.3              # the bot never uses more than this share of GROQ_DAILY_LIMIT (collectors need the rest)
CV_MAX_BYTES = 2 * 1024 * 1024
CV_EXT = ("pdf", "docx", "txt")
FEED_MIN_GAP = 45               # seconds between two posts in one group/channel
FEED_QUEUE_MAX = 200
FEED_SEED = 5                   # newest matching jobs posted right after /feed
FEED_SET_EVERY = 600            # one /feed change per group per 10 minutes
MAX_FEEDS = 200
SESSION_DAYS = 3
DIGEST_MARGIN = timedelta(minutes=15)  # jobs stored just before the last digest; "sent" removes repeats
MAX_TEXT = 3900                 # Telegram's limit is 4096 characters

BTN_JOBS = "🔎 وظائفي"
BTN_TITLES = "✏️ مسمياتي"
BTN_CV = "📄 سيرتي"
BTN_SOURCES = "📚 المصادر"
BTN_SETTINGS = "⚙️ التنبيهات"
BTN_HELP = "❓ طريقة الاستخدام"
MAIN_KB = {"keyboard": [[{"text": BTN_JOBS}, {"text": BTN_TITLES}],
                        [{"text": BTN_CV}, {"text": BTN_SOURCES}],
                        [{"text": BTN_SETTINGS}, {"text": BTN_HELP}]],
           "resize_keyboard": True, "is_persistent": True}
# Tap-to-try titles on the welcome message (Arabic and English work the same)
EXAMPLES = ["محاسب", "مهندس مدني", "ممرض", "Data Analyst"]
SUGGEST_MAX = 4
JADARAT = "جدارات"

MODES = {"daily": "مرة يومياً", "instant": "فور نزول الوظيفة", "off": "متوقفة"}
CITIES = [c for c in normalize.CITY_ALIASES if c != normalize.REMOTE][:14]

# Words people add around a title: "ابغى وظيفة محاسب" -> "محاسب"
FILLER = {normalize.norm(w) for w in (
    "ابي", "ابغى", "ابغا", "ابا", "أبي", "اريد", "أريد", "ابحث", "أبحث", "دور", "ودي", "لي", "عن", "على",
    "وظيفة", "وظيفه", "وظائف", "وظايف", "شغل", "عمل", "مطلوب", "في", "فى", "بمدينة", "مدينة", "job", "jobs",
    "find", "me", "a", "in", "أو", "او", "و", "or", "and", "مجال", "بمجال", "تخصص", "بتخصص", "احتاج", "أحتاج",
    "ممكن", "لو", "سمحت", "تكفى", "ابغي", "اشتغل", "كـ", "i", "im", "i'm", "am", "looking", "look", "for",
    "want", "need", "searching", "search", "position", "role", "vacancy", "vacancies", "an", "the", "please",
    "any", "as", "work")}
# A message that is only a greeting or thanks (it names no job)
GREETING_RE = re.compile(r"^\W*(?:السلام عليكم(?: ورحمه الله(?: وبركاته)?)?|سلام|مرحبا|هلا|اهلا|اهلين|صباح الخير|"
                         r"مساء الخير|شكرا|يعطيك العافيه|الله يعطيك العافيه|جزاك الله خير|hi|hello|hey|"
                         r"thanks|thank you|good morning|good evening)\W*$", re.I)
REMOTE_RE = re.compile(r"(?:^|\s)(?:عن\s*بعد|عن\s*بعـد|من\s*المنزل|remote(?:ly)?|work\s*from\s*home|wfh)(?=\s|$)", re.I)
DIGIT_RE = re.compile(r"[0-9]{1,3}")

esc = tg.esc
_notice_ts = {}   # (chat_id, kind) -> last time we answered a non-admin in a group


def _ar_jobs(n):
    if n == 1:
        return "وظيفة واحدة"
    if n == 2:
        return "وظيفتين"
    if 3 <= n <= 10:
        return f"{n} وظائف"
    return f"{n} وظيفة"


def _ar_new_jobs(n):
    """Count + 'new' with correct agreement (the adjective follows the counted noun)."""
    if n == 1:
        return "وظيفة واحدة جديدة"
    if n == 2:
        return "وظيفتان جديدتان"
    return _ar_jobs(n) + " جديدة"


def _ar_sources(n):
    if n == 1:
        return "مصدر واحد"
    if n == 2:
        return "مصدرين"
    if 3 <= n <= 10:
        return f"{n} مصادر"
    return f"{n} مصدراً"


def _ago(d):
    try:
        days = (date.today() - date.fromisoformat((d or "")[:10])).days
    except ValueError:
        return ""
    if days <= 0:
        return "اليوم"
    if days == 1:
        return "أمس"
    if days == 2:
        return "قبل يومين"
    if days <= 10:
        return f"قبل {days} أيام"
    return f"قبل {days} يوماً"


def _h(s):
    return hashlib.sha1(normalize.norm(s).encode("utf-8")).hexdigest()[:10]


def _ts(value):
    """ISO date/time string -> aware datetime (local time when no offset); None if invalid."""
    if not value:
        return None
    try:
        d = datetime.fromisoformat(value if "T" in value else value + "T23:59:00")
    except ValueError:
        return None
    return d if d.tzinfo else d.astimezone()


def _stored_at(job):
    return _ts(job.get("collected_at")) or _ts(job.get("posted_date"))


def chat_of(upd):
    for k in ("message", "channel_post", "my_chat_member"):
        if upd.get(k):
            return (upd[k].get("chat") or {}).get("id")
    cb = upd.get("callback_query") or {}
    return ((cb.get("message") or {}).get("chat") or {}).get("id")


# ---------------------------------------------------------------- subscribers

def _key(chat_id):
    return str(chat_id)


def get_sub(chat_id):
    """Read-only snapshot (do not mutate; change it through _edit_sub)."""
    return storage.read_cached("subscribers").get(_key(chat_id))


def _new_sub(chat_id, kind):
    now = storage.now_iso()
    return {"chat_id": chat_id, "kind": kind, "created_at": now, "updated_at": now, "titles": [],
            "sources": [], "city": "", "mode": "daily" if kind == "user" else "feed", "since": now,
            "last_digest": "", "sent": [], "cv": None, "awaiting": ""}


@contextlib.contextmanager
def _edit_sub(chat_id, create=True, kind="user"):
    """Yields the subscriber dict (None if missing and create=False); saved on exit."""
    with storage.edit("subscribers") as subs:
        s = subs.get(_key(chat_id))
        if s is None and create:
            s = subs[_key(chat_id)] = _new_sub(chat_id, kind)
        yield s
        if s is not None:
            s["updated_at"] = storage.now_iso()


def _ai_allowed(chat_id, limit=AI_DAILY_PER_CHAT):
    """Charge one AI use to this chat and to the bot's global daily share. False when over.

    Counted in bot_state (not on the subscriber), so /unfeed or /delete can't reset it."""
    today = date.today().isoformat()
    cap = max(30, int(config.GROQ_DAILY_LIMIT * BOT_AI_SHARE))
    with storage.edit("bot_state") as st:
        g = st.get("ai_used") or {}
        if g.get("date") != today:
            g = {"date": today, "n": 0}
        quota = st.get("ai_quota") if st.get("ai_quota_date") == today else {}
        used = quota.get(_key(chat_id), 0)
        ok = g["n"] < cap and used < limit
        if ok:
            g["n"] += 1
            quota[_key(chat_id)] = used + 1
        st["ai_used"], st["ai_quota"], st["ai_quota_date"] = g, quota, today
    return ok


def _feed_recently_set(chat_id, now):
    """True if /feed changed this chat less than FEED_SET_EVERY ago (else records now)."""
    with storage.edit("bot_state") as st:
        sets = {k: v for k, v in (st.get("feed_set") or {}).items() if now - v < FEED_SET_EVERY}
        recent = _key(chat_id) in sets
        if not recent:
            sets[_key(chat_id)] = now
        st["feed_set"] = sets
    return recent


def _mark_sent(s, ids):
    lst = s.setdefault("sent", [])
    have = set(lst)
    lst.extend(i for i in ids if i not in have)
    del lst[:-SENT_KEEP]


def on_blocked(chat_id):
    """The person blocked the bot (alerts off) or a group/channel removed it (feed deleted)."""
    with storage.edit("subscribers") as subs:
        s = subs.get(_key(chat_id))
        if not s:
            return
        if s.get("kind") == "feed":
            del subs[_key(chat_id)]
        else:
            s["mode"] = "off"


def migrate_chat(old_id, new_id):
    """A group became a supergroup: Telegram gives it a new id, the feed moves with it."""
    if not old_id or not new_id or old_id == new_id:
        return
    with storage.edit("subscribers") as subs:
        s = subs.pop(_key(old_id), None)   # the old id is always removed (it can never be posted to again)
        if s and _key(new_id) not in subs:
            s["chat_id"] = new_id
            subs[_key(new_id)] = s


# ---------------------------------------------------------------- search

def _jobs():
    jobs, _ = catalog.all_jobs(include_demo=False)
    return [j for j in jobs if not j.get("scam_risk") and not j.get("is_demo")]


def _query_of(sub):
    q = []
    for t in sub.get("titles") or []:
        q += titles.compile_query(t.get("variants") or [t["title"]])
    return q


def search(sub, jobs=None, all_if_no_titles=False):
    """Matching jobs, newest first (title hits before text hits on the same day)."""
    query = _query_of(sub)
    if not query and not all_if_no_titles:
        return []
    srcs = set(sub.get("sources") or [])
    city = sub.get("city") or ""
    out = []
    for j in (jobs if jobs is not None else _jobs()):
        if srcs and j.get("source") not in srcs and not (j.get("via_jadarat") and JADARAT in srcs):
            continue
        if city and j.get("city") not in (city, normalize.REMOTE, "", None):
            continue
        m = titles.match(j, query) if query else 1
        if m:
            out.append((j.get("posted_date") or "", m, j))
    out.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [j for _, _, j in out]


_CITY_WORDS = None


def _city_words():
    """Every word of every city name ("رياض", "جده", "riyadh", ...) to drop from typed titles."""
    global _CITY_WORDS
    if _CITY_WORDS is None:
        words = set()
        for city, aliases in normalize.CITY_ALIASES.items():
            if city == normalize.REMOTE:
                continue
            for a in [city] + list(aliases):
                words.update(titles.tokens(a))
        _CITY_WORDS = words
    return _CITY_WORDS


def split_query(text):
    """'ابغى وظيفة محاسب بالرياض أو جدة، accountant' -> (['محاسب', 'accountant'], 'الرياض').
    'عن بعد' sets the city to remote work instead of becoming a title."""
    found_city, out = "", []
    city_words = _city_words()
    for part in re.split(r"[\n,،;؛/|]+", text or ""):
        if REMOTE_RE.search(normalize.norm(part)):
            found_city = found_city or normalize.REMOTE
            stripped = REMOTE_RE.sub(" ", part)
            # spelled with diacritics ("عن بُعد"): fall back to the normalized text
            part = stripped if not REMOTE_RE.search(normalize.norm(stripped)) else REMOTE_RE.sub(" ", normalize.norm(part))
        c = normalize.detect_city(part)
        if c and c != normalize.REMOTE:
            found_city = found_city if found_city and found_city != normalize.REMOTE else c
        words = []
        for w in part.split():
            if normalize.norm(w) in FILLER:
                continue
            wt = titles.tokens(w)
            if wt and all(t in city_words for t in wt):
                continue
            words.append(w)
        t = titles.clean_title(" ".join(words))
        if len(t) >= 2 and t not in out:
            out.append(t)
    return out, found_city


# ---------------------------------------------------------------- messages

def card(job):
    """Title, company / city / age, then the direct link: the original apply page when the
    re-posting site gave one, else the ad itself in its source."""
    apply = job.get("apply_url") or ""
    url = apply or job.get("url") or ""
    meta = [x for x in (job.get("company"), job.get("city"), _ago(job.get("posted_date"))) if x]
    lines = [f"<b>{esc(job.get('title'))}</b>"]
    if meta:
        lines.append(esc(" · ".join(meta)))
    src = job.get("source") or ""
    if url.startswith(("https://", "http://")):
        href = html.escape(url, quote=True)
        if job.get("via_jadarat") and apply:
            lines.append(f'🔗 <a href="{href}">رابط التقديم في جدارات</a> · نقلاً عن {esc(src)}')
        elif apply:
            lines.append(f'🔗 <a href="{href}">رابط التقديم</a> · نقلاً عن {esc(src)}')
        else:
            lines.append(f'🔗 <a href="{href}">الإعلان الأصلي في {esc(src)}</a>')
    elif url.startswith("mailto:"):  # direct posts: Telegram links don't support mailto
        lines.append(f"التقديم بالبريد مباشرة: {esc(url[7:])}")
    return "\n".join(lines)


def _hashtag(s):
    s = re.sub(r"[^\w\s]", "", s or "").strip()
    return "#" + re.sub(r"\s+", "_", s) if s else ""


def feed_card(job):
    tags = " ".join(t for t in ("#وظائف", _hashtag(job.get("city")), _hashtag(job.get("specialty"))) if t)
    text = card(job) + "\n" + tags
    if config.TELEGRAM_BOT_USERNAME:
        text += (f'\n\n<a href="https://t.me/{config.TELEGRAM_BOT_USERNAME}">'
                 "وظائف تناسبك أنت فقط، من بوت مُنْفَرِد</a>")
    return text


def _join_cards(parts):
    """Join message parts, dropping whole cards (never cutting an HTML tag) to fit Telegram's limit."""
    while len(parts) > 1 and len("\n\n".join(parts)) > MAX_TEXT:
        parts = parts[:-1]
    return "\n\n".join(parts)


HELP = (
    "أهلاً بك في مُنْفَرِد 👋\n"
    "نجمع لك أخبار الوظائف في السعودية من كل المصادر، مجاناً، وكل وظيفة معها رابطها الأصلي لتقدّم منه مباشرة.\n\n"
    "<b>كيف يعمل؟</b>\n"
    "1️⃣ <b>ابحث:</b> اكتب المسمى الوظيفي بالعربي أو الإنجليزي، مثل: محاسب، مهندس مدني، Data Analyst\n"
    "أو اكتب طلبك بجملة، مثل: «أريد وظيفة محاسب في جدة»\n"
    "2️⃣ <b>النتائج فوراً:</b> نعرض لك كل الوظائف الحالية، الأحدث أولاً، ونبحث أيضاً بالأسماء الأخرى لنفس الوظيفة.\n"
    f"3️⃣ <b>الاشتراك تلقائي ومجاني:</b> أي وظيفة جديدة تناسب مسمياتك تصلك هنا، مرة يومياً في الساعة "
    f"{config.BOT_DIGEST_HOUR} صباحاً، أو فور نزولها (تختار من {BTN_SETTINGS}).\n"
    "4️⃣ <b>قدّم من المصدر:</b> اضغط على الرابط تحت الوظيفة لتفتح إعلانها الأصلي وتقدّم هناك. "
    "مُنْفَرِد ينقل الخبر فقط، ولا يستقبل طلبات التوظيف.\n"
    "💡 <b>اقتراحات:</b> بعد كل بحث نقترح عليك مسميات قريبة فيها وظائف الآن، تضيفها بضغطة.\n\n"
    "📄 أو أرسل سيرتك الذاتية (PDF أو Word) ونستخرج منها المسميات التي تناسب خبرتك.\n\n"
    f"{BTN_JOBS} وظائفك الحالية، {BTN_TITLES} إضافة مسمى أو حذفه، {BTN_SETTINGS} التوقيت والمدينة.\n"
    "/stop إيقاف التنبيهات، /delete حذف كل بياناتك.\n\n"
    "English works too: type a job title such as <b>accountant</b> or <b>nurse in Jeddah</b>.")

CV_PROMPT = (
    "أرسل سيرتك الذاتية كملف (PDF أو Word)، أو اكتب هنا نبذة عنك: دوراتك وخبراتك وما تجيده.\n\n"
    "🔒 لا نحتاج اسمك ولا جوالك ولا هويتك ولا عنوانك. ونحذف أي بيانات شخصية تلقائياً قبل القراءة، "
    "ولا نحفظ الملف ولا النص، بل نحفظ فقط المسميات والمهارات المستخرجة منه.")

START_HINT = "اكتب المسمى الوظيفي الذي تبحث عنه، مثل: <b>محاسب</b> أو <b>Data Analyst</b>\nأو أرسل سيرتك الذاتية."
NOT_A_JOB = ("أنا مُنْفَرِد، أبحث لك عن الوظائف 🙂\n"
             "اكتب المسمى الوظيفي الذي تبحث عنه، مثل: <b>محاسب</b> أو <b>Data Analyst</b>، "
             f"أو اضغط {BTN_HELP} لتعرف كيف أعمل.")


def _examples_kb():
    return {"inline_keyboard": [[{"text": "🔎 " + e, "callback_data": f"try:{i}"} for i, e in enumerate(EXAMPLES[:2])],
                                [{"text": "🔎 " + e, "callback_data": f"try:{i + 2}"}
                                 for i, e in enumerate(EXAMPLES[2:4])]]}


def _prune_sessions(ss):
    cutoff = time.time() - SESSION_DAYS * 86400
    for k in [k for k, v in ss.items() if v.get("ts", 0) < cutoff]:
        del ss[k]


def _new_session(prev, ids, footer):
    return dict(prev or {}, ids=ids[:MAX_RESULTS], pos=0, ts=time.time(), footer=footer, sid=secrets.token_hex(3))


def _session_set(chat_id, ids, footer=""):
    """Store a result list for "more"; returns its short id (old "more" buttons stop working)."""
    with storage.edit("bot_sessions") as ss:
        _prune_sessions(ss)
        ss[_key(chat_id)] = sess = _new_session(ss.get(_key(chat_id)), ids, footer)
    return sess["sid"]


def _take_page(sess, by_id):
    """Advance a session by one page (skipping jobs that expired meanwhile): (jobs, jobs left)."""
    ids, i = sess.get("ids") or [], sess.get("pos", 0)
    page = []
    while i < len(ids) and len(page) < PAGE:
        if ids[i] in by_id:
            page.append(by_id[ids[i]])
        i += 1
    sess["pos"] = i
    return page, sum(1 for x in ids[i:] if x in by_id)


def _render_page(header, page, left, footer, sid):
    parts = ([header] if header else []) + [card(j) for j in page]
    if left <= 0 and footer:
        parts.append(footer)
    markup = {"inline_keyboard": [[{"text": f"عرض المزيد ({left})", "callback_data": "more:" + sid}]]} \
        if left > 0 else MAIN_KB
    return _join_cards(parts), markup


def send_page(chat_id, header="", sid=None, by_id=None, max_wait=3):
    """Next PAGE jobs of the chat's current results (jobs that expired meanwhile are skipped)."""
    by_id = by_id if by_id is not None else {j["id"]: j for j in _jobs()}
    page, left, footer, cur_sid = [], 0, "", ""
    with storage.edit("bot_sessions") as ss:
        s = ss.get(_key(chat_id))
        if s and (sid is None or s.get("sid") == sid):   # else: a "more" button from an older search
            page, left = _take_page(s, by_id)
            footer, cur_sid = s.get("footer", ""), s.get("sid", "")
    if not page:
        tg.send(chat_id, f"انتهت نتائج هذا البحث. اضغط {BTN_JOBS} للبحث من جديد.", reply_markup=MAIN_KB,
                max_wait=max_wait)
        return
    text, markup = _render_page(header, page, left, footer, cur_sid)
    tg.send(chat_id, text, reply_markup=markup, max_wait=max_wait)


def _scope_text(sub):
    srcs = sub.get("sources") or []
    bits = ["من كل المصادر" if not srcs else f"من {_ar_sources(len(srcs))} اخترتها" if len(srcs) > 2
            else "من " + " و".join(srcs)]
    if sub.get("city") == normalize.REMOTE:
        bits.append("عن بعد")
    elif sub.get("city"):
        bits.append("في " + sub["city"] + " وعن بعد")
    return "، ".join(bits)


_SHORT_CUT_RE = re.compile(r"\s+[-–|/]\s+|\s*[(\[]|،|,|\s+(?:in|at|for|لدى|في|بمدينة|بشركة)\s+", re.I)


def _short_title(title):
    """'Senior Accountant - Riyadh (Full time)' -> 'Senior Accountant'; '' for a post that names no job."""
    t = titles.clean_title(_SHORT_CUT_RE.split(title or "", maxsplit=1)[0])
    tk = titles.tokens(t)
    if not tk or len(tk) > 4 or titles.GENERIC_TITLE_RE.search(normalize.norm(t)) or not titles.useful(tk):
        return ""
    return t


def suggestions(sub, found, jobs, limit=SUGGEST_MAX):
    """Job titles near the person's own that have jobs now (in their sources and city): [(title, count)].
    From the AI's related titles, then from the commonest titles in the same specialty as their results."""
    mine = sub.get("titles") or []
    query = _query_of(sub)
    cands = [r for t in mine for r in titles.related(t["title"])]
    from_ai = set(cands)
    found_ids = {j.get("id") for j in found}
    # the specialty of the jobs whose TITLE matched (not posts found in their text), and only a
    # specialty that is a real share of them: one stray result must not steer the suggestions
    named = [j for j in found if query and titles.match(j, query) == 2]
    specs = Counter(j.get("specialty") for j in named if j.get("specialty") not in (None, "", "أخرى"))
    top = {sp for sp, n in specs.items() if n >= max(2, 0.25 * len(named))}
    if top:
        counts, label = Counter(), {}
        for j in jobs:
            if j.get("specialty") in top and j.get("id") not in found_ids:
                st = _short_title(j.get("title"))
                tk = titles.tokens(st)
                if 2 <= len(tk) <= 3:   # one word ("Director") is too vague, a long title too specific
                    k = " ".join(tk)
                    counts[k] += 1
                    label.setdefault(k, st)
        cands += [label[k] for k, n in counts.most_common(8) if n >= 3]
    seen = {" ".join(titles.tokens(t["title"])) for t in mine}
    out, shown, tried, from_data = [], [], 0, 0
    for c in cands:
        k = " ".join(titles.tokens(c))
        if not k or k in seen:
            continue
        seen.add(k)
        if query and titles.match({"id": None, "title": c}, query):
            continue   # already found by the person's own titles
        if c not in from_ai and from_data >= 2:
            continue   # titles taken from the data are noisier: at most two of them
        tried += 1
        if tried > 12:   # each check scans the catalog once
            break
        probe = dict(sub, titles=[{"title": c, "variants": [c] + titles.builtin_variants(c)}])
        ids = {j.get("id") for j in search(probe, jobs)} - found_ids
        # the same jobs under another name ("مهندس كهرباء" / "Electrical Engineer"): offer one
        if not ids or any(len(ids & o) >= 0.8 * min(len(ids), len(o)) for o in shown):
            continue
        shown.append(ids)
        out.append((c, len(ids)))
        from_data += c not in from_ai
        if len(out) >= limit:
            break
    return out


def _send_suggestions(chat_id, sugg):
    if not sugg:
        return
    with storage.edit("bot_sessions") as ss:
        s = ss.setdefault(_key(chat_id), {})
        known = s.get("sug") if isinstance(s.get("sug"), dict) else {}
        known.update({_h(t): t for t, _ in sugg})   # older suggestion messages keep working
        s["sug"], s["ts"] = dict(list(known.items())[-40:]), time.time()
    kb = [[{"text": f"➕ {t[:32]} ({_ar_jobs(n)})", "callback_data": "sug:" + _h(t)}] for t, n in sugg]
    tg.send(chat_id, "💡 <b>مسميات قريبة فيها وظائف الآن.</b> اضغط على أي مسمى لإضافته إلى مسمياتك وعرض وظائفه:",
            reply_markup={"inline_keyboard": kb})


def _sub_note(sub):
    mode = sub.get("mode")
    if mode == "instant":
        when = "فور نزولها"
    elif mode == "daily":
        when = f"مرة يومياً في الساعة {config.BOT_DIGEST_HOUR} صباحاً"
    else:
        return f"🔕 التنبيهات متوقفة الآن. فعّلها من {BTN_SETTINGS} لتصلك الوظائف الجديدة."
    return (f"🔔 <b>تم اشتراكك مجاناً:</b> كل وظيفة جديدة تناسب مسمياتك تصلك هنا {when}، مع رابطها الأصلي.\n"
            f"لتغيير التوقيت أو المدينة: {BTN_SETTINGS}")


def show_jobs(chat_id, suggest=False):
    sub = get_sub(chat_id)
    if not sub or not sub.get("titles"):
        tg.send(chat_id, START_HINT, reply_markup=MAIN_KB)
        return
    stamp = storage.now_iso()
    jobs = _jobs()
    found = search(sub, jobs)
    # Everything shown now is "seen": the daily message only brings jobs stored after this
    since = _ts(sub.get("since"))
    seen_new = [j["id"] for j in found if since is None or (_stored_at(j) and _stored_at(j) > since - DIGEST_MARGIN)]
    with _edit_sub(chat_id, create=False) as s:
        if s is not None:
            s["since"] = stamp
            _mark_sent(s, seen_new[:SENT_KEEP])
    names = "، ".join(t["title"] for t in sub["titles"])
    if not found:
        later = ("وسنرسل لك أي وظيفة مناسبة فور نزولها." if sub.get("mode") in ("daily", "instant")
                 else f"فعّل التنبيهات من {BTN_SETTINGS} لتصلك أي وظيفة مناسبة فور نزولها.")
        tg.send(chat_id, f"لا توجد الآن وظائف لـ: {esc(names)} ({esc(_scope_text(sub))}).\n"
                f"نجمع الوظائف من كل المصادر على مدار الساعة، {later}\n"
                "يمكنك إضافة مسميات أخرى بنفس المعنى، أو إرسال سيرتك لنقترح عليك وظائف.", reply_markup=MAIN_KB)
        _send_suggestions(chat_id, suggestions(sub, found, jobs))
        return
    header = f"🔎 وجدنا {_ar_jobs(len(found))} لـ: <b>{esc(names)}</b>\n{esc(_scope_text(sub))}، الأحدث أولاً."
    if len(found) > MAX_RESULTS:
        header += f"\nنعرض أحدث {MAX_RESULTS}."
    when = MODES.get(sub.get("mode"), "")
    footer = ("هذه كل الوظائف الحالية ✅\n" + (
        f"الجديد يصلك {when}." if sub.get("mode") in ("daily", "instant")
        else f"التنبيهات متوقفة. فعّلها من {BTN_SETTINGS}."))
    sid = _session_set(chat_id, [j["id"] for j in found], footer)
    send_page(chat_id, header, sid=sid, by_id={j["id"]: j for j in found})
    if suggest:
        _send_suggestions(chat_id, suggestions(sub, found, jobs))


def add_titles(chat_id, text="", wanted=None, city=None, ai_charged=False):
    """Add the titles in a message (or `wanted` / `city` read by the AI) and show the jobs.
    ai_charged: this message already paid one AI use (reading it): the expansion is free."""
    if wanted is None:
        wanted, city = split_query(text)
    if not wanted and not city:
        tg.send(chat_id, "اكتب المسمى الوظيفي، مثل: <b>محاسب</b> أو <b>مهندس مدني</b>", reply_markup=MAIN_KB)
        return
    sub = get_sub(chat_id) or {}
    first = not sub.get("titles")
    have = {normalize.norm(t["title"]) for t in sub.get("titles") or []}
    new = [t for t in wanted if normalize.norm(t) not in have]
    room = MAX_TITLES - len(sub.get("titles") or [])
    if new and room <= 0:
        tg.send(chat_id, f"وصلت إلى الحد الأقصى ({MAX_TITLES} مسمى). احذف بعضها من {BTN_TITLES} ثم أضف الجديد.",
                reply_markup=MAIN_KB)
        return
    skipped = new[room:]
    new = new[:room]
    expanded = {}
    if new:
        tg.quiet("sendChatAction", chat_id=chat_id, action="typing")
        use_ai = titles.needs_ai(new) and ai.enabled() and (ai_charged or _ai_allowed(chat_id))
        expanded = titles.expand(new, use_ai=use_ai)
    with _edit_sub(chat_id) as s:
        have = {normalize.norm(t["title"]) for t in s["titles"]}
        for t in new:
            if normalize.norm(t) not in have and len(s["titles"]) < MAX_TITLES:
                s["titles"].append({"title": t, "variants": expanded.get(t, [t]), "from": "user"})
        if city:
            s["city"] = city
        if s.get("awaiting"):
            s["awaiting"] = ""
    msg = []
    if new:
        msg.append("✅ أضفت: " + esc("، ".join(new)))
        extra = [v for t in new for v in expanded.get(t, [])[1:]]
        if extra:
            msg.append("ونبحث أيضاً بالأسماء الأخرى لنفس الوظيفة: " + esc("، ".join(extra[:14])))
    if skipped:
        msg.append(f"لم أضف {len(skipped)} لأن الحد الأقصى {MAX_TITLES} مسمى: " + esc("، ".join(skipped[:10])))
    if city == normalize.REMOTE:
        msg.append(f"🏠 نعرض لك الوظائف عن بعد. يمكنك تغيير ذلك من {BTN_SETTINGS}.")
    elif city:
        msg.append(f"📍 المدينة: {esc(city)} (مع الوظائف عن بعد). يمكنك تغييرها من {BTN_SETTINGS}.")
    if first and new:
        msg.append("\n" + _sub_note(get_sub(chat_id) or {}))
    if msg:
        tg.send(chat_id, "\n".join(msg))
    show_jobs(chat_id, suggest=bool(new))


def _needs_reading(text, wanted):
    """Plain title lists are read locally; sentences, questions and odd results go to the AI."""
    if "?" in text or "؟" in text or not wanted:
        return True
    return len(text.split()) >= 6 or any(len(titles.tokens(t)) >= 4 for t in wanted)


def handle_text(chat_id, text):
    """A free-text message: job titles (Arabic, English or both), a sentence asking for jobs,
    a greeting, or a question."""
    if GREETING_RE.match(normalize.norm(text)):
        sub = get_sub(chat_id) or {}
        return tg.send(chat_id, "أهلاً بك 👋\n" + START_HINT,
                       reply_markup=MAIN_KB if sub.get("titles") else _examples_kb())
    wanted, city = split_query(text)
    if _needs_reading(text, wanted):
        req, charged = titles.cached_request(text), False
        if req is None and ai.enabled() and _ai_allowed(chat_id):
            tg.quiet("sendChatAction", chat_id=chat_id, action="typing")
            req, charged = titles.parse_request(text), True
        if req is not None:
            if req["intent"] == "help" and not req["titles"]:
                return tg.send(chat_id, HELP, reply_markup=MAIN_KB)
            if not req["titles"]:
                return tg.send(chat_id, NOT_A_JOB, reply_markup=MAIN_KB)
            ai_city = normalize.REMOTE if req.get("remote") else normalize.detect_city(req.get("city") or "")
            return add_titles(chat_id, wanted=req["titles"], city=ai_city or city, ai_charged=charged)
        # no AI now: a question or a long sentence must not become a saved "job title"
        if "?" in text or "؟" in text or any(len(titles.tokens(t)) >= 4 for t in wanted):
            return tg.send(chat_id, NOT_A_JOB, reply_markup=MAIN_KB)
    if not wanted and not city:
        return tg.send(chat_id, NOT_A_JOB, reply_markup=MAIN_KB)
    return add_titles(chat_id, wanted=wanted, city=city)


def titles_menu(chat_id):
    sub = get_sub(chat_id) or {}
    ts = sub.get("titles") or []
    if not ts:
        return ("لم تضف أي مسمى بعد.\nاكتب المسمى الوظيفي الذي تبحث عنه، مثل: <b>محاسب</b>", None)
    lines = ["<b>مسمياتك الوظيفية:</b>"]
    for t in ts:
        lines.append("• " + esc(t["title"]) + (" (من سيرتك)" if t.get("from") == "cv" else ""))
    lines.append("\nلإضافة مسمى اكتبه هنا. ولحذف مسمى اضغط عليه:")
    kb = [[{"text": "❌ " + t["title"][:40], "callback_data": "del:" + _h(t["title"])}] for t in ts]
    kb.append([{"text": "🗑️ حذف كل المسميات", "callback_data": "del:all"}])
    return "\n".join(lines), {"inline_keyboard": kb}


def sources_menu(chat_id, rebuild=True):
    """rebuild=False keeps the button order of the menu on screen (so a tap toggles what it shows)."""
    sub = get_sub(chat_id) or {}
    chosen = list(sub.get("sources") or [])
    all_jobs = _jobs()
    counts = Counter(j.get("source") for j in all_jobs if j.get("source"))
    relayed = sum(1 for j in all_jobs if j.get("via_jadarat") and j.get("source") != JADARAT)
    if relayed:
        counts[JADARAT] += relayed
    names = (storage.read("bot_sessions").get(_key(chat_id)) or {}).get("src") if not rebuild else None
    if not names:
        names = [n for n, _ in counts.most_common(30)]
        names += [n for n in chosen if n not in names]
        with storage.edit("bot_sessions") as ss:
            s = ss.setdefault(_key(chat_id), {})
            s["src"], s["ts"] = names, time.time()
    head = ("الوظائف تصلك الآن من <b>كل المصادر</b> ✅" if not chosen
            else f"تصلك الوظائف من <b>{_ar_sources(len(chosen))}</b> فقط.")
    text = head + "\nاضغط على مصدر لاختياره أو إلغائه (الرقم بين القوسين هو عدد وظائفه الحالية):"
    kb = [[{"text": ("✅ " if not chosen else "") + "كل المصادر", "callback_data": "src:all"}]]
    row = []
    for i, n in enumerate(names):
        row.append({"text": ("✅ " if n in chosen else "") + f"{n[:24]} ({counts.get(n, 0)})", "callback_data": f"src:{i}"})
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    return text, {"inline_keyboard": kb}


def settings_menu(chat_id):
    sub = get_sub(chat_id) or {}
    mode = sub.get("mode", "daily")
    city = sub.get("city") or ""
    city_label = "عن بعد فقط" if city == normalize.REMOTE else (city or "كل المدن")
    text = (f"<b>الوظائف الجديدة تصلك:</b> {MODES.get(mode, '')}\n"
            f"<b>المدينة:</b> {esc(city_label)}\n\n"
            f"الرسالة اليومية تصل الساعة {config.BOT_DIGEST_HOUR} صباحاً بكل الوظائف الجديدة لمسمياتك.")

    def b(label, m):
        return {"text": ("✅ " if mode == m else "") + label, "callback_data": "mode:" + m}
    kb = [[b("📅 يومياً", "daily"), b("⚡ فوراً", "instant"), b("⏸️ إيقاف", "off")],
          [{"text": "📍 تغيير المدينة", "callback_data": "city:menu"}],
          [{"text": "🗑️ حذف كل بياناتي", "callback_data": "wipe:ask"}]]
    return text, {"inline_keyboard": kb}


def city_menu(chat_id):
    sub = get_sub(chat_id) or {}
    cur = sub.get("city") or ""
    kb = [[{"text": ("✅ " if not cur else "") + "كل المدن", "callback_data": "city:all"},
           {"text": ("✅ " if cur == normalize.REMOTE else "") + "عن بعد فقط", "callback_data": "city:remote"}]]
    row = []
    for i, c in enumerate(CITIES):
        row.append({"text": ("✅ " if c == cur else "") + c, "callback_data": f"city:{i}"})
        if len(row) == 3:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    kb.append([{"text": "رجوع", "callback_data": "settings"}])
    return "اختر المدينة (تصلك معها الوظائف عن بعد والوظائف غير محددة المدينة):", {"inline_keyboard": kb}


def _set_mode(s, mode):
    """Switching into the daily message starts a fresh "new since" point (no backlog of old jobs)."""
    if mode == "daily" and s.get("mode") != "daily":
        s["since"] = storage.now_iso()
    s["mode"] = mode


# ---------------------------------------------------------------- CV

def process_cv(chat_id, text, from_file=False):
    if not ai.enabled():
        tg.send(chat_id, "قراءة السيرة غير متاحة حالياً. اكتب المسميات الوظيفية التي تناسبك مباشرة.",
                reply_markup=MAIN_KB)
        return
    if len(ai.strip_sensitive(text, from_file).strip()) < 30:
        tg.send(chat_id, "تعذّرت قراءة السيرة. أرسلها كملف PDF أو Word فيه نص (ليس صورة)، "
                "أو اكتب نبذة أطول عن خبراتك ودوراتك.", reply_markup=MAIN_KB)
        return
    if not _ai_allowed(chat_id):
        tg.send(chat_id, "وصلت إلى الحد اليومي لقراءة السير. جرّب غداً، أو اكتب المسميات مباشرة.", reply_markup=MAIN_KB)
        return
    tg.quiet("sendChatAction", chat_id=chat_id, action="typing")
    prof = ai.cv_profile(text, from_file=from_file)
    if not prof or not prof.get("titles"):
        tg.send(chat_id, "تعذّرت قراءة السيرة الآن. حاول مرة أخرى بعد قليل، أو اكتب المسميات التي تناسبك مباشرة.",
                reply_markup=MAIN_KB)
        return
    with _edit_sub(chat_id) as s:
        s["titles"] = [t for t in s["titles"] if t.get("from") != "cv"]  # a new CV replaces the old suggestions
        have = {normalize.norm(t["title"]) for t in s["titles"]}
        added = []
        for t in prof["titles"]:
            t = titles.clean_title(t)
            if t and normalize.norm(t) not in have and len(s["titles"]) < MAX_TITLES:
                have.add(normalize.norm(t))
                s["titles"].append({"title": t, "variants": [t] + titles.builtin_variants(t), "from": "cv"})
                added.append(t)
        s["cv"] = {"summary": prof.get("summary", ""), "skills": prof.get("skills", []),
                   "specialty": prof.get("specialty", ""), "at": storage.now_iso()}
        s["awaiting"] = ""
    lines = ["📄 <b>قرأنا سيرتك</b>"]
    if prof.get("summary"):
        lines.append(esc(prof["summary"]))
    if prof.get("skills"):
        lines.append("\n<b>مهاراتك:</b> " + esc("، ".join(prof["skills"])))
    if added:
        lines.append("\n<b>وظائف تناسب خبرتك</b> (أضفناها لمسمياتك):\n" + "\n".join("• " + esc(t) for t in added))
        lines.append(f"\nيمكنك حذف أي منها من {BTN_TITLES}.")
    lines.append("\n🔒 لم نحفظ الملف ولا النص، فقط المسميات والمهارات.")
    tg.send(chat_id, "\n".join(lines))
    show_jobs(chat_id)


def handle_document(chat_id, doc):
    name = (doc.get("file_name") or "").lower()
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    if ext not in CV_EXT:
        tg.send(chat_id, "أرسل السيرة كملف PDF أو Word (docx)، أو اكتبها هنا نصاً.", reply_markup=MAIN_KB)
        return
    if ext == "pdf" and not cvtext.has_pdf_reader():
        tg.send(chat_id, "قراءة ملفات PDF غير متاحة حالياً. أرسل السيرة كملف Word، أو اكتب نبذة عنك هنا.",
                reply_markup=MAIN_KB)
        return
    if (doc.get("file_size") or 0) > CV_MAX_BYTES:
        tg.send(chat_id, "حجم الملف أكبر من 2 ميجابايت. أرسل نسخة أصغر أو اكتب نبذة عنك.", reply_markup=MAIN_KB)
        return
    tg.quiet("sendChatAction", chat_id=chat_id, action="typing")
    try:
        data = tg.download_file(doc.get("file_id"), CV_MAX_BYTES)
    except Exception as e:
        log.warning("CV download failed: %r", e)
        data = None
    if not data:
        tg.send(chat_id, "تعذّر تحميل الملف. حاول مرة أخرى.", reply_markup=MAIN_KB)
        return
    text = cvtext.extract_bytes(data, ext)  # read in memory: the file never touches the disk
    del data
    process_cv(chat_id, text, from_file=True)


# ---------------------------------------------------------------- private chat

def _looks_like_cv(text):
    """A long text, or many lines of sentences. A list of titles, one per line, is not a CV."""
    lines = [l for l in text.splitlines() if l.strip()]
    return len(text) > 220 or (len(lines) >= 4 and sum(len(l.split()) for l in lines) / len(lines) > 4)


def _wipe_question():
    return ("هل تريد حذف كل بياناتك من مُنْفَرِد (المسميات والمهارات والإعدادات)؟",
            {"inline_keyboard": [[{"text": "نعم، احذفها", "callback_data": "wipe:yes"},
                                  {"text": "لا", "callback_data": "wipe:no"}]]})


def handle_private(msg):
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    if msg.get("document"):
        return handle_document(chat_id, msg["document"])
    if msg.get("photo"):
        return tg.send(chat_id, "أرسل السيرة كملف PDF أو Word، أو اكتبها هنا نصاً (الصور لا تُقرأ).",
                       reply_markup=MAIN_KB)
    if not text:
        return
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower() if cmd.startswith("/") else ""

    if cmd == "/start" and arg.strip():
        return tg.handle_message(msg)  # older website-link flow (one-time token)
    if cmd == "/start":
        with _edit_sub(chat_id) as s:
            if s.get("mode") == "off":
                _set_mode(s, "daily")
            new_user = not s.get("titles")
        tg.send(chat_id, HELP, reply_markup=MAIN_KB)
        if new_user:
            tg.send(chat_id, "جرّب الآن، اضغط على مثال أو اكتب المسمى الذي تبحث عنه:", reply_markup=_examples_kb())
        return
    if cmd == "/help" or text == BTN_HELP:
        return tg.send(chat_id, HELP, reply_markup=MAIN_KB)
    if cmd == "/jobs" or text == BTN_JOBS:
        return show_jobs(chat_id)
    if cmd == "/titles" or text == BTN_TITLES:
        t, kb = titles_menu(chat_id)
        return tg.send(chat_id, t, reply_markup=kb or MAIN_KB)
    if cmd == "/cv" or text == BTN_CV:
        with _edit_sub(chat_id) as s:
            s["awaiting"] = "cv"
            cv = s.get("cv")
        extra = ""
        if cv and cv.get("summary"):
            extra = "\n\nآخر سيرة قرأناها: " + esc(cv["summary"])
        return tg.send(chat_id, CV_PROMPT + extra, reply_markup=MAIN_KB)
    if cmd == "/sources" or text == BTN_SOURCES:
        t, kb = sources_menu(chat_id)
        return tg.send(chat_id, t, reply_markup=kb)
    if cmd == "/settings" or text == BTN_SETTINGS:
        t, kb = settings_menu(chat_id)
        return tg.send(chat_id, t, reply_markup=kb)
    if cmd == "/stop":
        with _edit_sub(chat_id) as s:
            _set_mode(s, "off")
        return tg.send(chat_id, f"أوقفنا التنبيهات. يمكنك البحث في أي وقت، وإعادة تفعيلها من {BTN_SETTINGS}.",
                       reply_markup=MAIN_KB)
    if cmd == "/delete":
        t, kb = _wipe_question()
        return tg.send(chat_id, t, reply_markup=kb)
    if cmd:
        return tg.send(chat_id, HELP, reply_markup=MAIN_KB)

    sub = get_sub(chat_id) or {}
    if (sub.get("awaiting") == "cv" and len(text) >= 40) or _looks_like_cv(text):
        return process_cv(chat_id, text)
    return handle_text(chat_id, text)


def _show(chat_id, message_id, menu):
    text, kb = menu
    if message_id:
        tg.edit_message(chat_id, message_id, text, kb)
    else:
        tg.send(chat_id, text, reply_markup=kb)


def _index(arg, size):
    return int(arg) if DIGIT_RE.fullmatch(arg or "") and int(arg) < size else None


def handle_callback(cb):
    data = cb.get("data") or ""
    msg = cb.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id, mid = chat.get("id"), msg.get("message_id")
    tg.quiet("answerCallbackQuery", callback_query_id=cb.get("id"))
    if not chat_id or chat.get("type") != "private":
        return
    if data == "more" or data.startswith("more:"):
        return send_page(chat_id, sid=data[5:] if data.startswith("more:") else None)
    if data == "wipe:no":
        return _show(chat_id, mid, ("لم يُحذف شيء.", None))
    if data.startswith("try:"):
        i = _index(data[4:], len(EXAMPLES))
        return add_titles(chat_id, wanted=[EXAMPLES[i]], city="") if i is not None else None
    if data.startswith("sug:"):
        known = (storage.read("bot_sessions").get(_key(chat_id)) or {}).get("sug")
        title = known.get(data[4:]) if isinstance(known, dict) else None
        if not title:
            return tg.send(chat_id, f"انتهت صلاحية هذا الاقتراح. اضغط {BTN_JOBS} للبحث من جديد.", reply_markup=MAIN_KB)
        return add_titles(chat_id, wanted=[title], city="")
    if not get_sub(chat_id):  # a button from before /delete: nothing to change
        return tg.send(chat_id, START_HINT, reply_markup=MAIN_KB)
    if data.startswith("del:"):
        h = data[4:]
        with _edit_sub(chat_id, create=False) as s:
            if s is not None:
                s["titles"] = [] if h == "all" else [t for t in s["titles"] if _h(t["title"]) != h]
        return _show(chat_id, mid, titles_menu(chat_id))
    if data.startswith("src:"):
        arg = data[4:]
        names = (storage.read("bot_sessions").get(_key(chat_id)) or {}).get("src") or []
        i = _index(arg, len(names))
        with _edit_sub(chat_id, create=False) as s:
            if s is not None:
                if arg == "all":
                    s["sources"] = []
                elif i is not None:
                    n = names[i]
                    cur = s.get("sources") or []
                    s["sources"] = [x for x in cur if x != n] if n in cur else cur + [n]
        return _show(chat_id, mid, sources_menu(chat_id, rebuild=False))
    if data.startswith("mode:") and data[5:] in MODES:
        with _edit_sub(chat_id, create=False) as s:
            if s is not None:
                _set_mode(s, data[5:])
        return _show(chat_id, mid, settings_menu(chat_id))
    if data == "settings":
        return _show(chat_id, mid, settings_menu(chat_id))
    if data.startswith("city:"):
        arg = data[5:]
        if arg == "menu":
            return _show(chat_id, mid, city_menu(chat_id))
        i = _index(arg, len(CITIES))
        with _edit_sub(chat_id, create=False) as s:
            if s is not None:
                if arg == "all":
                    s["city"] = ""
                elif arg == "remote":
                    s["city"] = normalize.REMOTE
                elif i is not None:
                    s["city"] = CITIES[i]
        return _show(chat_id, mid, settings_menu(chat_id))
    if data == "wipe:ask":
        t, kb = _wipe_question()
        return tg.send(chat_id, t, reply_markup=kb)
    if data == "wipe:yes":
        wipe(chat_id)
        return _show(chat_id, mid, ("تم حذف كل بياناتك ✅\nللعودة في أي وقت اكتب /start", None))


def wipe(chat_id):
    with storage.edit("subscribers") as subs:
        subs.pop(_key(chat_id), None)
    with storage.edit("bot_sessions") as ss:
        ss.pop(_key(chat_id), None)
    users.unlink_chat(chat_id)


# ---------------------------------------------------------------- groups / channels

GROUP_HELP = ("مرحباً، هذا بوت مُنْفَرِد لأخبار الوظائف في السعودية.\n"
              "لينشر البوت هنا كل وظيفة جديدة، يكتب أحد المشرفين: /feed\n"
              "أو لمسميات معينة فقط: <code>/feed محاسب، مهندس مدني</code>\n"
              "وللإيقاف: /unfeed")


def _is_admin(msg):
    chat = msg.get("chat") or {}
    if chat.get("type") == "channel":
        return True  # only channel admins can post
    if (msg.get("sender_chat") or {}).get("id") == chat.get("id"):
        return True  # anonymous group admin
    user_id = (msg.get("from") or {}).get("id")
    m = tg.quiet("getChatMember", chat_id=chat.get("id"), user_id=user_id) if user_id else None
    return bool(m) and m.get("status") in ("creator", "administrator")


def _notice_once(chat_id, kind, text, every=600):
    """Answer in a group at most once per `every` seconds per kind (no reply spam)."""
    now = time.time()
    if now - _notice_ts.get((chat_id, kind), 0) < every:
        return
    if len(_notice_ts) > 5000:
        _notice_ts.clear()
    _notice_ts[(chat_id, kind)] = now
    tg.quiet("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML")


def start_feed(chat_id, arg):
    wanted, city = split_query(arg) if arg.strip() else ([], "")
    wanted = wanted[:MAX_TITLES]  # cap before any AI call
    now = time.time()
    if _feed_recently_set(chat_id, now):
        return _notice_once(chat_id, "feed_wait", "تم ضبط النشر هنا قبل قليل. يمكن تغييره بعد 10 دقائق.")
    expanded = {}
    if wanted:
        use_ai = titles.needs_ai(wanted) and ai.enabled() and _ai_allowed(chat_id, limit=AI_DAILY_PER_FEED)
        expanded = titles.expand(wanted, use_ai=use_ai)
    with storage.edit("subscribers") as subs:
        s = subs.get(_key(chat_id))
        if s is None and sum(1 for x in subs.values() if x.get("kind") == "feed") >= MAX_FEEDS:
            full = True
        else:
            full = False
            if s is None:
                s = subs[_key(chat_id)] = _new_sub(chat_id, "feed")
            s["kind"], s["mode"] = "feed", "feed"
            s["titles"] = [{"title": t, "variants": expanded.get(t, [t]), "from": "admin"} for t in wanted]
            s["city"] = city
            s.setdefault("queue", [])
            snapshot = dict(s)
    if full:
        return tg.send(chat_id, "عدد القنوات والمجموعات وصل للحد الحالي. تواصل معنا.")
    jobs = _jobs()
    seed = [j["id"] for j in search(snapshot, jobs, all_if_no_titles=True)[:FEED_SEED]]
    with _edit_sub(chat_id, create=False) as s:
        if s is not None:
            done = set(s.get("sent") or [])
            # jobs already queued stay only if they still fit the new titles / city
            queued = set(s.get("queue") or [])
            keep = {j["id"] for j in search(snapshot, [j for j in jobs if j["id"] in queued], all_if_no_titles=True)}
            q = [i for i in s.get("queue") or [] if i in keep and i not in done]
            s["queue"] = q + [i for i in reversed(seed) if i not in done and i not in q]
    what = ("كل وظيفة جديدة" if not wanted else "كل وظيفة جديدة لـ: " + esc("، ".join(wanted)))
    if city == normalize.REMOTE:
        what += " عن بعد"
    elif city:
        what += f" في {esc(city)}"
    tg.send(chat_id, f"✅ سينشر مُنْفَرِد هنا {what}، بحد أقصى {config.BOT_FEED_DAILY_CAP} يومياً، "
                     "مع رابط الإعلان الأصلي لكل وظيفة.\nللإيقاف: /unfeed")


def handle_group(msg):
    chat_id = msg["chat"]["id"]
    if msg.get("migrate_to_chat_id"):
        return migrate_chat(chat_id, msg["migrate_to_chat_id"])
    text = (msg.get("text") or "").strip()
    cmd, _, arg = text.partition(" ")
    if not cmd.startswith("/"):
        return
    cmd, _, target = cmd.lower().partition("@")
    me = (config.TELEGRAM_BOT_USERNAME or "").lower()
    if target and target != me:
        return  # a command for another bot
    if cmd not in ("/feed", "/unfeed", "/start", "/help"):
        return
    if cmd in ("/start", "/help"):
        if target or msg["chat"].get("type") == "channel":  # bare /start in a group may be for another bot
            _notice_once(chat_id, "help", GROUP_HELP, every=120)
        return
    if not _is_admin(msg):
        return _notice_once(chat_id, "admins", "هذا الأمر للمشرفين فقط.")
    if msg["chat"].get("type") == "channel":
        tg.quiet("deleteMessage", chat_id=chat_id, message_id=msg.get("message_id"))
    if cmd == "/feed":
        return start_feed(chat_id, arg)
    with storage.edit("subscribers") as subs:
        subs.pop(_key(chat_id), None)
    tg.send(chat_id, "أوقفنا نشر الوظائف هنا. لإعادته: /feed")


def handle_member(upd):
    chat = upd.get("chat") or {}
    status = (upd.get("new_chat_member") or {}).get("status")
    if status in ("left", "kicked"):
        on_blocked(chat.get("id"))
    elif status in ("member", "administrator") and chat.get("type") in ("group", "supergroup") \
            and (upd.get("old_chat_member") or {}).get("status") in ("left", "kicked", None):
        _notice_once(chat.get("id"), "help", GROUP_HELP, every=120)


def handle_update(upd):
    if upd.get("callback_query"):
        return handle_callback(upd["callback_query"])
    if upd.get("my_chat_member"):
        return handle_member(upd["my_chat_member"])
    msg = upd.get("message") or upd.get("channel_post")
    if not msg:
        return
    ctype = (msg.get("chat") or {}).get("type")
    if ctype == "private":
        return handle_private(msg)
    if ctype in ("group", "supergroup", "channel"):
        return handle_group(msg)


def already_handled(update_id):
    """Webhook mode: Telegram re-sends an update it thinks failed; one already answered is skipped."""
    return update_id in (storage.read_cached("bot_state").get("seen_updates") or [])


def mark_handled(update_id):
    with storage.edit("bot_state") as st:
        seen = st.get("seen_updates") or []
        if update_id not in seen:
            st["seen_updates"] = (seen + [update_id])[-300:]


# ---------------------------------------------------------------- new jobs

_new_jobs_q = queue.Queue(maxsize=500)
_alerts_thread = [None]
_alerts_lock = threading.Lock()


def submit_new_jobs(added):
    """Hand new jobs to the alerts thread (the collector never waits on Telegram)."""
    if not tg.enabled() or not added:
        return
    if config.ON_VERCEL:   # no background threads there (a frozen one could hold the database lock)
        try:
            on_new_jobs(list(added))
        except Exception:
            log.exception("bot alerts failed")
        return
    with _alerts_lock:
        if _alerts_thread[0] is None or not _alerts_thread[0].is_alive():
            _alerts_thread[0] = threading.Thread(target=_alerts_loop, daemon=True, name="rizq-bot-alerts")
            _alerts_thread[0].start()
    try:
        _new_jobs_q.put_nowait(list(added))
    except queue.Full:
        log.warning("bot alerts queue full: %d new jobs not alerted instantly (the daily message still has them)",
                    len(added))


def _alerts_loop():
    while True:
        batch = _new_jobs_q.get()
        try:
            on_new_jobs(batch)
        except Exception:
            log.exception("bot alerts failed")
        finally:
            _new_jobs_q.task_done()


def flush_alerts(timeout=600):
    """Wait until queued alerts are handled (run_collectors.py exits right after collecting,
    which would kill the alerts thread). True if everything was handled in time."""
    deadline = time.time() + timeout
    while _new_jobs_q.unfinished_tasks and time.time() < deadline:
        time.sleep(0.5)
    return not _new_jobs_q.unfinished_tasks


def on_new_jobs(added):
    """Instant alerts + group/channel feed queues for newly stored jobs. Returns messages sent."""
    if not tg.enabled() or not added:
        return 0
    fresh = [j for j in added if not j.get("is_demo") and not j.get("scam_risk")]
    if not fresh:
        return 0
    subs = storage.read("subscribers")
    today = date.today().isoformat()
    enqueue, delivered, sent_msgs = {}, {}, 0
    for key, s in subs.items():
        if s.get("kind") == "feed":
            hits = search(s, fresh, all_if_no_titles=True)
            if hits:
                enqueue[key] = [j["id"] for j in reversed(hits)]  # oldest first in the queue
            continue
        if s.get("mode") != "instant" or not s.get("titles"):
            continue
        q = s.get("instant_count") or {}
        used = q.get("n", 0) if q.get("date") == today else 0
        already = set(s.get("sent") or [])
        hits = [j for j in search(s, fresh) if j["id"] not in already][:max(0, INSTANT_DAILY_CAP - used)]
        if not hits:
            continue
        shown = hits[:PAGE]
        parts = ["🔔 " + f"{_ar_new_jobs(len(hits))} لك"] + [card(j) for j in shown]
        if len(hits) > len(shown):
            parts.append(f"والمزيد في {BTN_JOBS}.")
        try:
            tg.send(s["chat_id"], _join_cards(parts), max_wait=30)
        except Exception as e:  # one failed chat must not stop the others
            log.warning("instant alert to %s failed: %r", key, e)
            continue
        sent_msgs += 1
        delivered[key] = [j["id"] for j in hits]
        time.sleep(0.05)
    if enqueue or delivered:  # one write for everyone
        with storage.edit("subscribers") as all_subs:
            for key, ids in delivered.items():
                s = all_subs.get(key)
                if not s:
                    continue
                _mark_sent(s, ids)
                q = s.get("instant_count") or {}
                n = q.get("n", 0) if q.get("date") == today else 0
                s["instant_count"] = {"date": today, "n": n + len(ids)}
            for key, ids in enqueue.items():
                s = all_subs.get(key)
                if not s:
                    continue
                done = set(s.get("sent") or [])
                q = s.setdefault("queue", [])
                q.extend(i for i in ids if i not in done and i not in q)
                del q[:-FEED_QUEUE_MAX]
    return sent_msgs


def _feed_gone(e):
    return e.code == 403 or (e.code == 400 and re.search(
        r"chat not found|not enough rights|CHAT_WRITE_FORBIDDEN|have no rights|bot was kicked|chat was deleted",
        e.description or "", re.I))


def feed_tick():
    """Post the next queued job in each due group/channel (spaced out, capped per day).
    One write takes the jobs off the queues, one write records the results."""
    now, today = time.time(), date.today().isoformat()
    subs = storage.read_cached("subscribers")
    due_keys = [k for k, s in subs.items() if s.get("kind") == "feed" and s.get("queue")
                and now - s.get("last_post_ts", 0) >= FEED_MIN_GAP
                and (s.get("posted") or {}).get(today, 0) < config.BOT_FEED_DAILY_CAP]
    if not due_keys:
        return 0
    by_id = {j["id"]: j for j in _jobs()}
    picked = {}
    with storage.edit("subscribers") as all_subs:
        for k in due_keys:
            s = all_subs.get(k)
            if not s:
                continue
            while s.get("queue"):
                jid = s["queue"].pop(0)
                if jid in by_id:  # expired / hidden jobs are skipped
                    picked[k] = (s["chat_id"], by_id[jid])
                    break
    results = {}   # key -> "ok" | "retry" | "gone" | ("moved", new_id)
    for k, (chat_id, job) in picked.items():
        try:
            tg.send(chat_id, feed_card(job), max_wait=30)
            results[k] = "ok"
        except tg.TelegramError as e:
            log.warning("feed post to %s failed: %r", chat_id, e)
            new_id = (getattr(e, "parameters", None) or {}).get("migrate_to_chat_id")
            if new_id:
                results[k] = ("moved", new_id)
            elif _feed_gone(e):
                results[k] = "gone"   # chat deleted or the bot lost its rights: stop this feed
            elif e.code in (0, 429) or e.code >= 500:
                results[k] = "retry"  # temporary: keep the job for a later tick
                if e.code == 0:       # network down: stop this tick
                    for k2 in picked:
                        results.setdefault(k2, "retry")
                    break
            else:
                results[k] = "skip"   # this job's message is rejected (e.g. bad text): never retry it
    posted = 0
    with storage.edit("subscribers") as all_subs:
        for k, res in results.items():
            s = all_subs.get(k)
            if not s:
                continue
            job = picked[k][1]
            if res == "gone":
                del all_subs[k]
                continue
            s["last_post_ts"] = now   # also a back-off after a failure
            if res == "ok":
                posted += 1
                s["posted"] = {today: (s.get("posted") or {}).get(today, 0) + 1}
                _mark_sent(s, [job["id"]])
            elif res == "skip":
                _mark_sent(s, [job["id"]])
            elif isinstance(res, tuple):  # group became a supergroup: move the feed to its new id
                s.setdefault("queue", []).insert(0, job["id"])
                moved = all_subs.pop(k)
                if _key(res[1]) not in all_subs:
                    moved["chat_id"] = res[1]
                    all_subs[_key(res[1])] = moved
            else:
                s.setdefault("queue", []).insert(0, job["id"])  # keep it for the next try
    return posted


def digest_tick(now=None):
    """Once a day after BOT_DIGEST_HOUR: each daily subscriber gets the jobs stored since they
    last looked (since - DIGEST_MARGIN; ids already sent or shown are skipped)."""
    now = now or datetime.now()
    if now.hour < config.BOT_DIGEST_HOUR:
        return 0
    today = now.date().isoformat()
    subs = storage.read("subscribers")
    todo = [s for s in subs.values() if s.get("kind", "user") == "user" and s.get("mode") == "daily"
            and s.get("titles") and s.get("last_digest") != today]
    if not todo:
        return 0
    stamp = storage.now_iso()      # one stamp, taken before reading the jobs: nothing falls in between
    jobs = _jobs()
    plans = []
    for s in todo:
        since = _ts(s.get("since"))
        fresh = [j for j in jobs if since is None or (_stored_at(j) and _stored_at(j) > since - DIGEST_MARGIN)]
        already = set(s.get("sent") or [])
        hits = [j for j in search(s, fresh) if j["id"] not in already]
        plans.append((s, hits))
    with storage.edit("subscribers") as all_subs:   # marked first, in one write: a crash must not resend
        mine = []
        for s, hits in plans:
            cur = all_subs.get(_key(s["chat_id"]))
            if cur is None or cur.get("last_digest") == today:   # another run already sent it
                continue
            cur["last_digest"], cur["since"] = today, stamp
            _mark_sent(cur, [j["id"] for j in hits[:MAX_RESULTS]])
            mine.append((s, hits))
        plans = mine
    by_id = {j["id"]: j for j in jobs}
    outbox = []
    with storage.edit("bot_sessions") as ss:   # every recipient's results + first page in one write
        _prune_sessions(ss)
        for s, hits in plans:
            if not hits:
                continue
            names = "، ".join(t["title"] for t in s["titles"])
            sess = _new_session(ss.get(_key(s["chat_id"])), [j["id"] for j in hits], "هذه كل الوظائف الجديدة اليوم ✅")
            page, left = _take_page(sess, by_id)
            ss[_key(s["chat_id"])] = sess
            header = f"🆕 <b>{_ar_new_jobs(len(hits))} لك اليوم</b>\nلـ: {esc(names)}"
            outbox.append((s, hits, _render_page(header, page, left, sess["footer"], sess["sid"])))
    sent, failed = 0, []
    for n, (s, hits, (text, markup)) in enumerate(outbox):
        try:
            tg.send(s["chat_id"], text, reply_markup=markup, max_wait=30)
            sent += 1
        except tg.TelegramError as e:
            log.warning("digest to %s failed: %r", s["chat_id"], e)
            if e.code == 0:                 # network down: nobody else gets it now either
                failed += [(x[0], x[1]) for x in outbox[n:]]
                break
            if e.code == 429 or e.code >= 500:
                failed.append((s, hits))    # temporary: try again on the next check
            # 403 (blocked the bot, alerts already off) or another rejection: not retried
        except Exception as e:
            log.warning("digest to %s failed: %r", s["chat_id"], e)
        time.sleep(0.05)
    if failed:   # not delivered: undo the marks so the next check (a minute later) tries again
        with storage.edit("subscribers") as all_subs:
            for s, hits in failed:
                cur = all_subs.get(_key(s["chat_id"]))
                if cur is None:
                    continue
                cur["last_digest"], cur["since"] = s.get("last_digest", ""), s.get("since") or stamp
                gone = {j["id"] for j in hits}
                cur["sent"] = [i for i in cur.get("sent") or [] if i not in gone]
    return sent


class Scheduler(threading.Thread):
    """Feeds every 15 s, the daily message check every minute."""

    def __init__(self):
        super().__init__(daemon=True, name="rizq-bot-scheduler")
        self.stop_event = threading.Event()

    def run(self):
        last_digest_check = 0
        while not self.stop_event.is_set():
            try:
                feed_tick()
                if time.time() - last_digest_check >= 60:
                    last_digest_check = time.time()
                    digest_tick()
            except Exception:
                log.exception("bot scheduler tick failed")
            self.stop_event.wait(15)
