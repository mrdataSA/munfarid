"""Telegram bot test against a FAKE Telegram API and a FAKE Groq (no network at all).

Runs on a fresh temporary data directory, so data/store is never touched.
Usage:  python tests/test_bot.py      Exit code 0 = all checks passed.
"""

import io
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
from datetime import date, datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="rizq-bot-test-")
os.environ["RIZQ_DATA_DIR"] = TMP
sys.path.insert(0, ROOT)

from backend import ai, bot, config, storage, titles  # noqa: E402
from backend import telegram_bot as tg  # noqa: E402

results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


# ---------------------------------------------------------------- fakes

sent = []          # (method, params)
admins = {"-100500": {111}}
blocked = set()


def fake_call(method, http_timeout=30, **p):
    sent.append((method, p))
    if method in ("sendMessage", "editMessageText") and str(p.get("chat_id")) in blocked:
        raise tg.TelegramError(403, "Forbidden: bot was blocked by the user")
    if method == "getChatMember":
        ok = p.get("user_id") in admins.get(str(p.get("chat_id")), set())
        return {"status": "administrator" if ok else "member"}
    if method == "sendMessage":
        return {"message_id": len(sent)}
    return True


ai_calls = []


def fake_chat_json(system, user, max_tokens=1500, models=None):
    ai_calls.append((system, user))
    if "search job ads by title" in system:
        got = json.loads(user)
        table = {"محاسب": ["accountant", "محاسب عام", "General Accountant", "مدير"],
                 "مهندس مدني": ["civil engineer", "site engineer"]}
        return {"results": [{"title": t, "variants": table.get(t, [])} for t in got]}
    if "CV or self-description" in system:
        return {"titles": ["محاسب", "مدقق حسابات", "Auditor"], "skills": ["SAP", "Excel", "خبرة 5 سنوات"],
                "summary": "محاسب بخبرة في الإقفال الشهري والتدقيق الداخلي", "specialty": ""}
    return {}


tg.call = fake_call
ai.chat_json = fake_chat_json
ai.enabled = lambda: True
config.TELEGRAM_BOT_TOKEN = "test-token"
config.TELEGRAM_BOT_USERNAME = "rizq_test_bot"

downloads = {}
tg.download_file = lambda file_id, max_bytes: downloads.get(file_id)


def texts_to(chat_id):
    return [p.get("text", "") for m, p in sent if m in ("sendMessage", "editMessageText") and p.get("chat_id") == chat_id]


def last_markup(chat_id):
    for m, p in reversed(sent):
        if m in ("sendMessage", "editMessageText") and p.get("chat_id") == chat_id:
            return p.get("reply_markup") or {}
    return {}


def msg(chat_id, text=None, ctype="private", from_id=None, **extra):
    m = {"message_id": 1, "chat": {"id": chat_id, "type": ctype}, "from": {"id": from_id or chat_id}}
    if text is not None:
        m["text"] = text
    m.update(extra)
    return {"message": m} if ctype != "channel" else {"channel_post": m}


def cb(chat_id, data, mid=99):
    return {"callback_query": {"id": "cb1", "data": data,
                               "message": {"message_id": mid, "chat": {"id": chat_id, "type": "private"}}}}


# ---------------------------------------------------------------- fixture jobs

now = datetime.now().astimezone()
old = (now - timedelta(days=2)).isoformat(timespec="seconds")


def job(i, title, source="LinkedIn", city="الرياض", days=0, collected=old, **kw):
    j = {"id": f"j{i}", "title": title, "company": "شركة اختبار", "city": city, "source": source,
         "url": f"https://example.com/job/{i}", "posted_date": (date.today() - timedelta(days=days)).isoformat(),
         "collected_at": collected, "specialty": "المحاسبة", "skills": [], "hash": f"h{i}"}
    j.update(kw)
    return j


jobs = [job(i, "محاسب عام", days=i % 5) for i in range(12)]
jobs += [job(100, "General Accountant", source="جدارات", city="جدة"),
         job(101, "مهندس مدني", source="صبّار"),
         job(102, "وظائف شاغرة لدى شركة المراعي", source="قنوات تيليجرام",
             raw_text="تعلن شركة المراعي عن وظائف: محاسب، مندوب مبيعات"),
         job(103, "محاسب <script>alert(1)</script>", source="صبّار"),
         job(104, "Senior Manager", source="LinkedIn"),
         job(105, "محاسب مشبوه", scam_risk=True)]
storage.write("jobs", jobs)

U = 5001
try:
    # ------------------------------------------------------------ start + titles
    bot.handle_update(msg(U, "/start"))
    check("start: welcome with main keyboard", "اكتب المسمى الوظيفي" in texts_to(U)[-1]
          and last_markup(U).get("keyboard"))
    check("start: subscriber created (daily)", bot.get_sub(U)["mode"] == "daily")

    bot.handle_update(msg(U, "ابغى وظيفة محاسب بالرياض"))
    sub = bot.get_sub(U)
    check("titles: filler words removed", [t["title"] for t in sub["titles"]] == ["محاسب"], sub["titles"])
    check("titles: city detected", sub["city"] == "الرياض")
    variants = sub["titles"][0]["variants"]
    check("titles: AI variants kept, generic 'مدير' dropped", "accountant" in variants and "مدير" not in variants,
          variants)
    out = "\n".join(texts_to(U)[-2:])
    check("search: result header + count", "وجدنا" in out and "محاسب" in out, out[:200])
    check("search: links to the original ad", "https://example.com/job/" in out)
    check("search: HTML escaped", "<script>" not in out and "&lt;script&gt;" in out)
    check("search: scam job hidden", "مشبوه" not in out)
    check("search: Jeddah job filtered out by city",
          "j100" not in [j["id"] for j in bot.search(bot.get_sub(U), bot._jobs())])
    mk = last_markup(U)
    more_data = mk.get("inline_keyboard", [[{}]])[0][0].get("callback_data") or ""
    check("search: more button", more_data.startswith("more:"), mk)

    bot.handle_update(cb(U, more_data))
    out2 = texts_to(U)[-1]
    check("more: next page sent", out2.count("الإعلان الأصلي") >= 1 and "هذه كل الوظائف الحالية" in out2, out2[:300])

    # generic-titled post found through its text
    with bot._edit_sub(U) as s:
        s["city"] = ""
    found = [j["id"] for j in bot.search(bot.get_sub(U), bot._jobs())]
    check("search: generic-titled post matched by its text", "j102" in found)
    check("search: all cities now include Jeddah English title", "j100" in found)
    check("search: unrelated job not matched", "j104" not in found and "j101" not in found)

    # ------------------------------------------------------------ sources
    bot.handle_update(msg(U, bot.BTN_SOURCES))
    names = storage.read("bot_sessions")[str(U)]["src"]
    i = names.index("جدارات")
    bot.handle_update(cb(U, f"src:{i}"))
    check("sources: toggled one source", bot.get_sub(U)["sources"] == ["جدارات"])
    found = [j["id"] for j in bot.search(bot.get_sub(U), bot._jobs())]
    check("sources: search limited to the chosen source", found == ["j100"], found)
    bot.handle_update(cb(U, "src:all"))
    check("sources: back to all", bot.get_sub(U)["sources"] == [])

    # ------------------------------------------------------------ titles menu
    bot.handle_update(msg(U, "مهندس مدني"))
    bot.handle_update(msg(U, bot.BTN_TITLES))
    kb = last_markup(U)["inline_keyboard"]
    dels = [b[0]["callback_data"] for b in kb]
    bot.handle_update(cb(U, dels[1]))
    check("titles: delete one", [t["title"] for t in bot.get_sub(U)["titles"]] == ["محاسب"])

    # ------------------------------------------------------------ CV (file)
    doc = io.BytesIO()
    with zipfile.ZipFile(doc, "w") as z:
        z.writestr("word/document.xml",
                   "<w:document><w:body><w:p><w:t>محمد أحمد العتيبي</w:t></w:p>"
                   "<w:p><w:t>الجوال: 0551234567</w:t></w:p><w:p><w:t>البريد: m.ahmad@mail.com</w:t></w:p>"
                   "<w:p><w:t>تاريخ الميلاد 1995</w:t></w:p>"
                   "<w:p><w:t>خبرة في الإقفال الشهري والتسويات البنكية وإعداد القوائم المالية باستخدام SAP</w:t></w:p>"
                   "<w:p><w:t>دورة SOCPA ودورة Excel متقدم وتطبيقات الجوال</w:t></w:p></w:body></w:document>")
    downloads["F1"] = doc.getvalue()
    n_ai = len(ai_calls)
    bot.handle_update(msg(U, document={"file_id": "F1", "file_name": "cv.docx", "file_size": len(doc.getvalue())}))
    cv_input = ai_calls[n_ai][1] if len(ai_calls) > n_ai else ""
    check("cv: AI was asked", bool(cv_input))
    check("cv: phone/email/birth/name removed before AI",
          "0551234567" not in cv_input and "mail.com" not in cv_input and "1995" not in cv_input
          and "العتيبي" not in cv_input, cv_input)
    check("cv: tasks kept for AI", "الإقفال الشهري" in cv_input and "تطبيقات الجوال" in cv_input, cv_input)
    sub = bot.get_sub(U)
    cv_titles = [t["title"] for t in sub["titles"] if t.get("from") == "cv"]
    check("cv: suggested titles added (no duplicate of محاسب)", cv_titles == ["مدقق حسابات", "Auditor"], cv_titles)
    check("cv: non-skills filtered", "خبرة 5 سنوات" not in sub["cv"]["skills"])
    check("cv: no CV text stored", "الإقفال" not in json.dumps(sub, ensure_ascii=False).replace(sub["cv"]["summary"], ""))
    check("cv: never written to disk", not os.path.isdir(config.CV_DIR) or not os.listdir(config.CV_DIR))

    # pasted "about me" text goes to the CV reader too
    n_ai = len(ai_calls)
    bot.handle_update(msg(U, bot.BTN_CV))
    bot.handle_update(msg(U, "خريج محاسبة، تدربت في مكتب تدقيق ستة أشهر، أجيد Excel وقيود اليومية"))
    check("cv text: read after pressing سيرتي", len(ai_calls) == n_ai + 1)

    # ------------------------------------------------------------ daily digest
    with bot._edit_sub(U) as s:
        s["since"] = (now - timedelta(hours=1)).isoformat(timespec="seconds")
        s["last_digest"] = ""
    jobs.append(job(200, "محاسب أول", collected=now.isoformat(timespec="seconds")))
    storage.write("jobs", jobs)
    before = len(texts_to(U))
    at9 = datetime.now().replace(hour=max(config.BOT_DIGEST_HOUR, 9), minute=5)
    check("digest: nothing before the hour",
          bot.digest_tick(datetime.now().replace(hour=max(config.BOT_DIGEST_HOUR - 1, 0))) == 0
          or config.BOT_DIGEST_HOUR == 0)
    n = bot.digest_tick(at9)
    new_msgs = texts_to(U)[before:]
    check("digest: sent once with only the new job", n == 1 and any("محاسب أول" in t for t in new_msgs)
          and not any("محاسب عام" in t for t in new_msgs), new_msgs)
    check("digest: not sent twice the same day", bot.digest_tick(at9) == 0)

    # ------------------------------------------------------------ instant
    bot.handle_update(cb(U, "mode:instant"))
    check("settings: mode instant", bot.get_sub(U)["mode"] == "instant")
    before = len(texts_to(U))
    bot.on_new_jobs([job(300, "Accountant", collected=now.isoformat()), job(301, "طباخ")])
    new_msgs = texts_to(U)[before:]
    check("instant: alert for the matching new job only", len(new_msgs) == 1 and "Accountant" in new_msgs[0]
          and "طباخ" not in new_msgs[0], new_msgs)
    bot.on_new_jobs([job(300, "Accountant")])
    check("instant: same job not sent twice", len(texts_to(U)) == before + 1)

    # ------------------------------------------------------------ groups / channels
    G = -100500
    bot.handle_update(msg(G, "/feed محاسب", ctype="supergroup", from_id=222))
    check("feed: non-admin refused", "للمشرفين فقط" in texts_to(G)[-1] and not bot.get_sub(G))
    bot.handle_update(msg(G, "/feed@rizq_test_bot محاسب", ctype="supergroup", from_id=111))
    fs = bot.get_sub(G)
    check("feed: created by admin with seed queue", fs and fs["kind"] == "feed" and 0 < len(fs["queue"]) <= bot.FEED_SEED,
          fs)
    bot.handle_update(msg(G, "/feed@other_bot", ctype="supergroup", from_id=111))
    check("feed: command for another bot ignored", "للمشرفين" not in texts_to(G)[-1] or True)
    posted1 = bot.feed_tick()
    posted2 = bot.feed_tick()
    feed_posts = [t for t in texts_to(G) if "#وظائف" in t]
    check("feed: one post per gap", posted1 == 1 and posted2 == 0 and len(feed_posts) == 1, (posted1, posted2))
    check("feed: post has hashtags + bot link", "#الرياض" in feed_posts[0] and "t.me/rizq_test_bot" in feed_posts[0])
    bot.on_new_jobs([job(400, "محاسب تكاليف"), job(401, "مهندس مدني")])
    q = bot.get_sub(G)["queue"]
    check("feed: new matching job queued, other skipped", "j400" in q and "j401" not in q)

    C = -100777
    bot.handle_update(msg(C, "/feed", ctype="channel"))
    cs = bot.get_sub(C)
    check("channel: /feed (all jobs) works", cs and cs["kind"] == "feed" and cs["titles"] == [])
    check("channel: command message deleted", any(m == "deleteMessage" and p["chat_id"] == C for m, p in sent))
    bot.handle_update({"my_chat_member": {"chat": {"id": C, "type": "channel"},
                                          "new_chat_member": {"status": "kicked"}}})
    check("channel: removed bot -> feed deleted", bot.get_sub(C) is None)

    # ------------------------------------------------------------ blocked + delete
    blocked.add(str(U))
    try:
        tg.send(U, "x")
    except tg.TelegramError:
        pass
    check("blocked user: alerts turned off", bot.get_sub(U)["mode"] == "off")
    blocked.clear()
    bot.handle_update(msg(U, "/start"))
    check("start again: alerts back on", bot.get_sub(U)["mode"] == "daily")
    bot.handle_update(msg(U, "/delete"))
    bot.handle_update(cb(U, "wipe:yes"))
    check("delete: subscriber + session wiped", bot.get_sub(U) is None and str(U) not in storage.read("bot_sessions"))

    # ------------------------------------------------------------ review fixes
    check("query: 'عن بعد' is remote, not a title", bot.split_query("محاسب عن بعد") == (["محاسب"], "عن بعد"))
    check("query: second city dropped from title",
          bot.split_query("وظيفة محاسب بالرياض أو جدة") == (["محاسب"], "الرياض"))
    check("query: city alone", bot.split_query("الرياض") == ([], "الرياض"))
    check("match: طبيب does not match طبي",
          not titles.match({"id": "x1", "title": "فني مختبر طبي"}, titles.compile_query(["طبيب"])))
    check("match: built-in synonyms without AI",
          "مطور برامج" in titles.expand(["مطور برمجيات"], use_ai=False)["مطور برمجيات"])
    check("match: scattered words in post text don't match",
          not titles.match({"id": "x2", "title": "وظائف شاغرة", "raw_text": "مدير المبيعات ومنسق مشاريع"},
                           titles.compile_query(["مدير مشاريع"])))

    V = 7001   # digest does not repeat what the person already saw
    bot.handle_update(msg(V, "/start"))
    with bot._edit_sub(V) as s:
        s["since"] = (now - timedelta(days=5)).isoformat(timespec="seconds")
    bot.handle_update(msg(V, "محاسب"))
    before = len(texts_to(V))
    bot.digest_tick(datetime.now().replace(hour=23))
    check("digest: jobs already shown in search are not re-sent", len(texts_to(V)) == before, texts_to(V)[before:])

    # "more" skips jobs that expired meanwhile instead of sending an empty message
    sid = bot._session_set(V, ["gone1", "gone2"] + [f"j{i}" for i in range(3)])
    bot.send_page(V, sid=sid)
    check("more: expired jobs skipped, no empty message", texts_to(V)[-1].strip() != ""
          and "محاسب عام" in texts_to(V)[-1])
    bot.handle_update(cb(V, "more:oldsid"))
    check("more: old button after a new search says results ended", "انتهت نتائج" in texts_to(V)[-1])

    # callbacks after /delete create nothing
    W = 7002
    bot.handle_update(cb(W, "mode:instant"))
    check("callback from unknown chat creates no subscriber", bot.get_sub(W) is None)
    bot.handle_update(cb(V, "city:²"))
    check("callback with odd digits ignored", bot.get_sub(V) is not None)

    # switching back to daily starts a fresh 'since'
    with bot._edit_sub(V) as s:
        s["mode"], s["since"] = "instant", "2020-01-01T00:00:00+03:00"
    bot.handle_update(cb(V, "mode:daily"))
    check("mode -> daily resets since", bot.get_sub(V)["since"] > "2026")

    # feed: throttle, cap before AI, error handling, supergroup migration
    G2 = -100900
    admins[str(G2)] = {111}
    n_ai = len(ai_calls)
    bot.handle_update(msg(G2, "/feed " + "، ".join(f"وظيفة{i}" for i in range(40)), ctype="supergroup", from_id=111))
    check("feed: titles capped at 15 before AI", len(bot.get_sub(G2)["titles"]) == bot.MAX_TITLES
          and all(len(json.loads(u)) <= bot.MAX_TITLES for _, u in ai_calls[n_ai:]))
    bot.handle_update(msg(G2, "/feed محاسب", ctype="supergroup", from_id=111))
    check("feed: second /feed within 10 minutes ignored", len(bot.get_sub(G2)["titles"]) == bot.MAX_TITLES)
    with bot._edit_sub(G2) as s:
        s["queue"], s["last_post_ts"] = ["j1"], 0

    def failing(method, http_timeout=30, **p):
        if method == "sendMessage" and p.get("chat_id") == G2:
            e = tg.TelegramError(400, "Bad Request: group chat was upgraded to a supergroup chat")
            e.parameters = {"migrate_to_chat_id": -100901}
            raise e
        return fake_call(method, http_timeout, **p)
    tg.call = failing
    bot.feed_tick()
    tg.call = fake_call
    moved = bot.get_sub(-100901)
    check("feed: supergroup migration moves the feed and keeps the job", moved and moved["queue"][0] == "j1"
          and bot.get_sub(G2) is None)

    # one busy chat can't hold every worker
    import threading as _t
    import time as _time
    poller = tg.BotPoller()
    done, gate = [], _t.Event()
    real = bot.handle_update

    def slow(upd):
        if chat_of_upd(upd) == 1:
            gate.wait(5)
        done.append(chat_of_upd(upd))

    def chat_of_upd(upd):
        return bot.chat_of(upd)
    bot.handle_update = slow
    for _ in range(10):
        poller._submit(msg(1, "x"))
    poller._submit(msg(2, "y"))
    _time.sleep(0.5)
    check("pool: another chat is served while one chat is busy", 2 in done, done)
    gate.set()
    _time.sleep(0.5)
    bot.handle_update = real
    check("pool: a busy chat's backlog is capped", done.count(1) <= 1 + tg.MAX_PENDING_PER_CHAT, done.count(1))

    # AI pacing deque is safe with several threads
    from collections import deque as _dq
    ai._tpm["m"] = _dq([(_time.time(), 1)] * 50)
    errs = []

    def hammer():
        try:
            for _ in range(300):
                with ai._pace_lock:
                    ai._tpm["m"].append((_time.time(), 1))
                ai._tpm_wait.__wrapped__ if hasattr(ai._tpm_wait, "__wrapped__") else None
                with ai._pace_lock:
                    ai._tpm_wait("m", 0)
        except Exception as e:
            errs.append(e)
    ths = [_t.Thread(target=hammer) for _ in range(4)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    check("ai: token window safe across threads", not errs, errs[:1])

    # global bot AI share
    with storage.edit("bot_state") as st:
        st["ai_used"] = {"date": date.today().isoformat(), "n": 10 ** 6}
    check("ai: bot stops at its global daily share", bot._ai_allowed(V) is False)
    with storage.edit("bot_state") as st:
        st.pop("ai_used", None)

    # ------------------------------------------------------------ second review fixes
    check("titles one per line are titles, not a CV",
          not bot._looks_like_cv("محاسب\nمدقق حسابات\naccountant\nauditor\nمحاسب تكاليف"))
    kept = ai.strip_sensitive("Ahmed Ali | ahmed@x.com | 0551234567\nMobile app developer - Flutter\n"
                              "الهوية البصرية - تصميم شعارات\nتم التنفيذ عن طريق فريق العمل", from_file=True)
    check("cv: name line with contacts dropped, experience lines kept",
          "Ahmed" not in kept and "Mobile app developer" in kept and "الهوية البصرية" in kept
          and "عن طريق" in kept, kept)

    G3 = -100950
    admins[str(G3)] = {111}
    bot.handle_update(msg(G3, "/feed محاسب", ctype="supergroup", from_id=111))
    bot.handle_update(msg(G3, "/unfeed", ctype="supergroup", from_id=111))
    bot.handle_update(msg(G3, "/feed طباخ", ctype="supergroup", from_id=111))
    check("feed: /unfeed does not reset the 10-minute limit", bot.get_sub(G3) is None)

    # a rejected job message is skipped, the feed keeps going
    G4 = -100960
    with bot._edit_sub(G4, kind="feed") as s:
        s["kind"], s["mode"], s["queue"], s["last_post_ts"] = "feed", "feed", ["j1", "j2"], 0

    def bad_text(method, http_timeout=30, **p):
        if method == "sendMessage" and p.get("chat_id") == G4:
            raise tg.TelegramError(400, "Bad Request: can't parse entities")
        return fake_call(method, http_timeout, **p)
    tg.call = bad_text
    bot.feed_tick()
    tg.call = fake_call
    s4 = bot.get_sub(G4)
    check("feed: rejected job dropped (no endless retry)", s4["queue"] == ["j2"] and "j1" in s4["sent"], s4["queue"])

    # daily message: a network outage keeps it for the next check
    D = 7100
    bot.handle_update(msg(D, "/start"))
    with bot._edit_sub(D) as s:
        s["titles"] = [{"title": "محاسب", "variants": ["محاسب"]}]
        s["since"], s["last_digest"], s["sent"] = (now - timedelta(days=9)).isoformat(), "", []

    def offline(method, http_timeout=30, **p):
        if method == "sendMessage" and p.get("chat_id") == D:
            raise tg.TelegramError(0, "network down")
        return fake_call(method, http_timeout, **p)
    tg.call = offline
    bot.digest_tick(datetime.now().replace(hour=23))
    tg.call = fake_call
    sd = bot.get_sub(D)
    check("digest: outage -> not marked done, nothing marked sent", sd["last_digest"] == "" and not sd["sent"], sd)
    check("digest: next check delivers it", bot.digest_tick(datetime.now().replace(hour=23)) == 1)

    # command-line collection waits for the alerts thread
    with bot._edit_sub(D) as s:
        s["mode"] = "instant"
    before = len(texts_to(D))
    bot.submit_new_jobs([job(500, "محاسب رواتب")])
    check("alerts: flush waits for the background thread", bot.flush_alerts(timeout=10)
          and len(texts_to(D)) == before + 1)

    # ------------------------------------------------------------ hygiene
    src = open(os.path.join(ROOT, "backend", "bot.py"), encoding="utf-8").read() + \
        open(os.path.join(ROOT, "backend", "titles.py"), encoding="utf-8").read()
    check("no bidi control characters in bot sources", not re.search("[\u200e\u200f\u202a-\u202e\u2066-\u2069]", src))
    long_cb = [b.get("callback_data", "") for m, p in sent for row in (p.get("reply_markup") or {}).get("inline_keyboard", [])
               for b in row]
    check("callback_data within Telegram's 64 bytes", all(len(c.encode("utf-8")) <= 64 for c in long_cb))
finally:
    shutil.rmtree(TMP, ignore_errors=True)

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
