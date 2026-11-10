"""Job seekers: create/update with a server-issued edit token, delete, Telegram link."""

import base64
import binascii
import hashlib
import os
import re
import secrets
import threading
from datetime import datetime, timedelta

from . import ai, auth, config, cvtext, normalize, storage

MAX_CV_BYTES = 2 * 1024 * 1024
MAX_CV_TEXT = 20000
CV_EXT = ("pdf", "doc", "docx", "txt")
TG_RE = re.compile(r"^@[A-Za-z][A-Za-z0-9_]{4,31}$")
LINK_TOKEN_MINUTES = 15


class ValidationError(Exception):
    pass


class Forbidden(Exception):
    pass


def _s(value, max_len):
    return normalize.one_line(value, max_len) if isinstance(value, str) else ""


def public(user):
    """Fields safe to return to the browser."""
    return {
        "id": user["id"],
        "name": user["name"],
        "specialty": user["specialty"],
        "city": user["city"],
        "cv_file": user.get("cv_file"),
        "cv_skills": user.get("cv_skills") or [],
        "telegram_linked": bool(user.get("telegram_chat_id")),
        "interests": user.get("interests") or [],
    }


def _validate(payload):
    if not isinstance(payload, dict):
        raise ValidationError("بيانات غير صالحة.")
    name = _s(payload.get("name"), 60)
    specialty = _s(payload.get("specialty"), 60)
    city = _s(payload.get("city"), 40)
    if len(name) < 2:
        raise ValidationError("الاسم مطلوب.")
    if not specialty:
        raise ValidationError("التخصص مطلوب.")
    if not city:
        raise ValidationError("المدينة مطلوبة.")
    raw_skills = payload.get("skills")
    if not isinstance(raw_skills, list):
        raise ValidationError("المهارات مطلوبة.")
    skills, seen = [], set()
    for s in raw_skills[:20]:
        s = _s(s, 40)
        if s and normalize.norm(s) not in seen:
            seen.add(normalize.norm(s))
            skills.append(s)
    if not skills:
        raise ValidationError("أضف مهارة واحدة على الأقل.")
    telegram = _s(payload.get("telegram"), 33)
    if telegram and not TG_RE.match(telegram):
        raise ValidationError("معرّف تيليجرام غير صحيح.")
    cv_text = payload.get("cv_text") if isinstance(payload.get("cv_text"), str) else ""
    cv_text = normalize.BIDI_RE.sub("", cv_text).strip()[:MAX_CV_TEXT]
    raw_interests = payload.get("interests") if isinstance(payload.get("interests"), list) else []
    interests = [k for k in ("coop", "scholarship") if k in raw_interests]   # alerts for co-op / scholarships
    return {"name": name, "specialty": specialty, "city": city, "skills": skills,
            "telegram": telegram, "cv_text": cv_text, "interests": interests}


def _decode_cv(cv):
    if not isinstance(cv, dict):
        raise ValidationError("ملف السيرة غير صالح.")
    original = _s(cv.get("name"), 120)
    ext = original.rsplit(".", 1)[-1].lower() if "." in original else ""
    if ext not in CV_EXT:
        raise ValidationError("صيغة الملف غير مدعومة.")
    data = cv.get("data")
    if not isinstance(data, str):
        raise ValidationError("تعذّرت قراءة الملف.")
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise ValidationError("تعذّرت قراءة الملف.")
    if not raw:
        raise ValidationError("الملف فارغ.")
    if len(raw) > MAX_CV_BYTES:
        raise ValidationError("حجم الملف أكبر من 2 ميجابايت.")
    return ext, original, raw


def _remove_cv_files(user_id, keep=None):
    for ext in CV_EXT:
        name = f"{user_id}.{ext}"
        path = os.path.join(config.CV_DIR, name)
        if name != keep and os.path.exists(path):
            os.remove(path)


def _prepare_cv(cv):
    """Decode, write to a temp file and extract text (slow part, done outside the lock)."""
    ext, original, raw = _decode_cv(cv)
    os.makedirs(config.CV_DIR, exist_ok=True)
    tmp = os.path.join(config.CV_DIR, "upload-" + secrets.token_hex(8) + "." + ext)
    with open(tmp, "wb") as f:
        f.write(raw)
    try:
        text = cvtext.extract_text(tmp)
    except Exception:
        text = ""
    return {"ext": ext, "name": original, "size": len(raw), "tmp": tmp, "text": text}


def save(payload):
    """Create or update. Returns (user, raw_edit_token or None when updating)."""
    fields = _validate(payload)
    prepared = _prepare_cv(payload["cv_file"]) if payload.get("cv_file") else None
    new_cv = None
    try:
        return _save_locked(payload, fields, prepared)
    finally:
        if prepared and os.path.exists(prepared["tmp"]):
            os.remove(prepared["tmp"])  # moved into place on success; removed on failure


def _save_locked(payload, fields, prepared):
    new_cv = None
    with storage.locked():
        users = storage.read("users")
        incoming_id = payload.get("id")
        idx = next((i for i, u in enumerate(users) if u.get("id") == incoming_id), -1) if incoming_id else -1
        now = storage.now_iso()
        token = None
        if idx >= 0:
            user = users[idx]
            if not auth.token_matches(payload.get("edit_token"), user.get("edit_token_hash")):
                raise Forbidden("انتهت صلاحية بياناتك على هذا الجهاز. اشترك في التنبيهات من جديد.")
        else:
            # The server always issues the id and secret token for new users
            token = auth.new_token()
            user = {"id": "u_" + secrets.token_hex(8), "edit_token_hash": auth.token_hash(token),
                    "created_at": now, "cv_file": None, "cv_file_text": "", "cv_skills": [],
                    "telegram_chat_id": None}
        user.update(fields)
        user["updated_at"] = now

        if prepared:
            new_cv = f"{user['id']}.{prepared['ext']}"
            os.replace(prepared["tmp"], os.path.join(config.CV_DIR, new_cv))
            user["cv_file"] = {"name": prepared["name"], "stored_as": new_cv, "size": prepared["size"]}
            user["cv_file_text"] = prepared["text"]
        elif payload.get("remove_cv_file"):
            user["cv_file"] = None
            user["cv_file_text"] = ""

        if idx >= 0:
            users[idx] = user
        else:
            users.append(user)
        storage.write("users", users)
        if new_cv or payload.get("remove_cv_file"):
            _remove_cv_files(user["id"], keep=new_cv)

    _enrich_async(user["id"])
    return user, token


def _cv_combined(user):
    return ((user.get("cv_text") or "") + "\n" + (user.get("cv_file_text") or "")).strip()


def enrich(user_id):
    """Extract skills from the CV (AI when available). Runs once per CV change."""
    user = get(user_id)
    if not user:
        return
    text = _cv_combined(user)
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest() if text else ""
    if digest == user.get("cv_digest"):
        return
    result = ai.extract_cv_skills(text) if text else {"skills": [], "by": "rules"}
    with storage.edit("users") as users:
        for u in users:
            if u["id"] == user_id:
                u["cv_skills"] = result.get("skills", [])
                u["cv_skills_by"] = result.get("by", "rules")
                u["cv_digest"] = digest


def _enrich_async(user_id):
    threading.Thread(target=_safe_enrich, args=(user_id,), daemon=True).start()


def _safe_enrich(user_id):
    try:
        enrich(user_id)
    except Exception as e:  # enrichment is best-effort
        import logging
        logging.getLogger("rizq").warning("CV enrich failed for %s: %r", user_id, e)


def get(user_id):
    return next((u for u in storage.read("users") if u.get("id") == user_id), None)


def check_token(user_id, token):
    user = get(user_id)
    if not user or not auth.token_matches(token, user.get("edit_token_hash")):
        raise Forbidden("غير مصرح.")
    return user


def delete(user_id, token):
    """Full wipe: record, CV file, notification log, pending link tokens."""
    with storage.locked():
        check_token(user_id, token)
        with storage.edit("users") as users:
            users[:] = [u for u in users if u.get("id") != user_id]
        _remove_cv_files(user_id)
        with storage.edit("sent") as sent:
            sent.pop(user_id, None)
        with storage.edit("link_tokens") as tokens:
            for k in [k for k, v in tokens.items() if v.get("user_id") == user_id]:
                del tokens[k]


# ---------------------------------------------------------------- Telegram link

def create_link_token(user_id, token):
    """One-time deep-link token (15 min). Returns the t.me URL or None if no bot."""
    check_token(user_id, token)
    if not config.TELEGRAM_BOT_USERNAME:
        return None
    raw = secrets.token_urlsafe(24)  # A-Za-z0-9_- , fits Telegram's 64-char start param
    expires = (datetime.now() + timedelta(minutes=LINK_TOKEN_MINUTES)).isoformat(timespec="seconds")
    with storage.edit("link_tokens") as tokens:
        now = datetime.now().isoformat(timespec="seconds")
        for k in [k for k, v in tokens.items() if v.get("expires", "") < now]:
            del tokens[k]
        tokens[auth.token_hash(raw)] = {"user_id": user_id, "expires": expires}
    return f"https://t.me/{config.TELEGRAM_BOT_USERNAME}?start={raw}"


def link_chat(raw_token, chat_id):
    """Consume a deep-link token. Returns the linked user or None."""
    key = auth.token_hash(raw_token)
    with storage.locked():
        tokens = storage.read("link_tokens")
        entry = tokens.pop(key, None)
        storage.write("link_tokens", tokens)  # one-time: removed even if expired
        if not entry or entry.get("expires", "") < datetime.now().isoformat(timespec="seconds"):
            return None
        linked = None
        with storage.edit("users") as users:
            for u in users:
                if u.get("telegram_chat_id") == chat_id and u["id"] != entry["user_id"]:
                    u["telegram_chat_id"] = None  # one chat -> one profile
                if u["id"] == entry["user_id"]:
                    u["telegram_chat_id"] = chat_id
                    u["telegram_linked_at"] = storage.now_iso()
                    linked = dict(u)
        return linked


def unlink_chat(chat_id):
    changed = False
    with storage.edit("users") as users:
        for u in users:
            if u.get("telegram_chat_id") == chat_id:
                u["telegram_chat_id"] = None
                changed = True
    return changed


def find_by_chat(chat_id):
    return next((u for u in storage.read("users") if u.get("telegram_chat_id") == chat_id), None)
