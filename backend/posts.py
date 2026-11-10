"""Direct job posts ("أعلن عن وظيفة"): a simple, moderated, account-free form.

A company submits: company name, job title, description, city, an apply link
OR an apply email, and a private contact email (admin only, never public).
Every post starts as 'pending' and is shown only after the admin approves it.
Approved posts join the public catalog with the "إعلان مباشر" badge; their
original source is the company's own apply link or email (mailto:).

Abuse protection lives here and in server.py: honeypot field, per-IP daily cap,
length limits, URL/email validation and rejection of posts that ask for fees.
"""

import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import date, datetime, timedelta

from . import config, normalize, storage

SOURCE = "إعلان مباشر"
STATUSES = ("pending", "approved", "rejected")
DAILY_PER_IP = 5
MAX_PENDING = 300            # global flood guard: stop accepting when the queue is this long
HONEYPOT_FIELD = "website"   # hidden in the form; real people leave it empty

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,180}\.[A-Za-z]{2,24}$")
URL_RE = re.compile(r"^https?://[A-Za-z0-9.-]+\.[A-Za-z]{2,}(:\d{1,5})?(/[^\s]*)?$")

LIMITS = {"company": (2, 100), "title": (3, 120), "description": (30, 3000)}

# Extra fee wording for free-text posts, on top of normalize.SCAM_WORDS
FEE_WORDS = ["رسوم اشتراك", "رسوم الاشتراك", "رسوم فتح ملف", "رسوم الملف", "دفع مبلغ", "مبلغ رمزي",
             "مبلغ مسترد", "تأمين مسترد", "رسوم التدريب", "رسوم الدورة", "قبل التوظيف تدفع",
             "application fee", "training fee", "deposit"]

# Public fields of an approved post (contact_email, ip_hash, status... stay private)
PUBLIC_FIELDS = ("id", "title", "company", "city", "specialty", "skills", "contract_type", "url",
                 "source", "posted_date", "description", "requirements", "salary", "is_training",
                 "nationality", "gender", "scam_risk", "is_demo", "direct")


class ValidationError(Exception):
    pass


class RateLimited(Exception):
    pass


def _s(v, n):
    return normalize.one_line(v, n) if isinstance(v, str) else ""


# Random per-process key: the stored hash cannot be reversed by trying every IPv4
# address. A restart only resets the per-IP posting limits.
_IP_KEY = secrets.token_bytes(32)


def ip_hash(ip):
    """Only used to count posts per IP; the raw address is never stored."""
    return hmac.new(_IP_KEY, str(ip).encode("utf-8"), hashlib.sha256).hexdigest()[:20]


def asks_for_fees(*texts):
    hay = normalize.norm(" ".join(t for t in texts if t))
    _, _, scam = normalize.detect_tags(*texts)
    return scam or any(normalize.norm(w) in hay for w in FEE_WORDS)


def validate(p):
    """Returns clean fields or raises ValidationError (Arabic message)."""
    company = _s(p.get("company"), 400)
    title = _s(p.get("title"), 400)
    raw_desc = p.get("description") if isinstance(p.get("description"), str) else ""
    description = normalize.strip_phones(normalize.clean_text(raw_desc[:12000]))
    for key, value, label in (("company", company, "اسم الجهة"), ("title", title, "المسمى الوظيفي"),
                              ("description", description, "وصف الوظيفة")):
        lo, hi = LIMITS[key]
        if len(value) < lo:
            raise ValidationError(f"{label} قصير جداً ({lo} أحرف على الأقل).")
        if len(value) > hi:
            raise ValidationError(f"{label} أطول من المسموح ({hi} حرفاً كحد أقصى).")

    city = normalize.canonical_city(_s(p.get("city"), 40))
    if not city:
        raise ValidationError("اختر المدينة.")

    apply_url = _s(p.get("apply_url"), 600)
    apply_email = _s(p.get("apply_email"), 200).lower()
    if apply_url:
        if len(apply_url) > 500 or not URL_RE.match(apply_url):
            raise ValidationError("رابط التقديم غير صحيح. اكتبه كاملاً، ويبدأ بـ https أو http.")
        url = apply_url
    elif apply_email:
        if len(apply_email) > 120 or not EMAIL_RE.match(apply_email):
            raise ValidationError("بريد التقديم غير صحيح.")
        url = "mailto:" + apply_email
    else:
        raise ValidationError("أضف رابط التقديم أو بريد التقديم.")

    contact = _s(p.get("contact_email"), 200).lower()
    if not contact or len(contact) > 120 or not EMAIL_RE.match(contact):
        raise ValidationError("بريد التواصل مع الإدارة غير صحيح.")

    if asks_for_fees(title, description, company):
        raise ValidationError("لا نقبل إعلانات تطلب رسوماً أو مبالغ من المتقدمين.")

    return {"company": company, "title": title, "description": description, "city": city,
            "url": url, "contact_email": contact}


def submit(payload, ip):
    """Validate and store a new pending post. Returns the stored post."""
    fields = validate(payload)
    who = ip_hash(ip)
    since = (datetime.now().astimezone() - timedelta(days=1)).isoformat(timespec="seconds")
    text = fields["title"] + "\n" + fields["description"]
    nationality, gender, _ = normalize.detect_tags(fields["title"], fields["description"])
    contract = normalize.detect_contract(fields["title"], fields["description"][:1500])
    post = {
        "id": "d" + secrets.token_hex(6),
        **fields,
        "specialty": normalize.detect_specialty(fields["title"], fields["description"][:1500]),
        "skills": normalize.detect_skills(fields["title"], text[:2500]),
        "contract_type": contract,
        "is_training": contract == "تدريب",
        "nationality": nationality,
        "gender": gender,
        "status": "pending",
        "ip_hash": who,
        "created_at": storage.now_iso(),
    }
    with storage.edit("direct_posts") as posts:
        for x in posts:  # data minimisation: the IP hash is only kept for one day
            if x.get("ip_hash") and x.get("created_at", "") < since:
                x["ip_hash"] = ""
        recent = [x for x in posts if x.get("ip_hash") == who and x.get("created_at", "") >= since]
        if len(recent) >= DAILY_PER_IP:
            raise RateLimited("وصلت إلى الحد اليومي لنشر الإعلانات من هذا الجهاز. حاول غداً.")
        if sum(1 for x in posts if x.get("status") == "pending") >= MAX_PENDING:
            raise RateLimited("استقبال الإعلانات متوقف مؤقتاً لكثرة الطلبات. حاول لاحقاً.")
        posts.append(post)
    return post


def to_job(post):
    """Public job view of an approved post (whitelisted fields only)."""
    job = {k: post.get(k) for k in PUBLIC_FIELDS}
    job.update({"source": SOURCE, "direct": True, "is_demo": False,
                "skills": post.get("skills") or [], "requirements": post.get("requirements") or [],
                "scam_risk": bool(post.get("scam_risk"))})
    return job


def public_jobs(hidden=()):
    """Approved, non-expired posts as public jobs."""
    out = []
    for p in storage.read_cached("direct_posts"):
        if p.get("status") != "approved" or p.get("id") in hidden:
            continue
        if normalize.is_expired(p, config.JOB_MAX_AGE_DAYS):
            continue
        out.append(to_job(p))
    return out


# ---------------------------------------------------------------- admin

def admin_list(limit_done=60):
    """Pending first (oldest first), then the most recent approved/rejected."""
    posts = storage.read("direct_posts")
    pending = sorted((p for p in posts if p.get("status") == "pending"), key=lambda p: p.get("created_at", ""))
    done = sorted((p for p in posts if p.get("status") != "pending"),
                  key=lambda p: p.get("updated_at") or p.get("created_at", ""), reverse=True)[:limit_done]
    keys = ("id", "company", "title", "description", "city", "url", "contact_email", "status",
            "created_at", "updated_at", "posted_date")
    return [{k: p.get(k) for k in keys} for p in pending + done]


def set_status(post_id, status):
    """Returns (post, newly_approved)."""
    if status not in ("approved", "rejected"):
        raise ValidationError("حالة غير صالحة.")
    with storage.edit("direct_posts") as posts:
        post = next((p for p in posts if p.get("id") == post_id), None)
        if not post:
            raise ValidationError("الإعلان غير موجود.")
        newly = status == "approved" and not post.get("approved_once")
        post["status"] = status
        post["updated_at"] = storage.now_iso()
        if status == "approved":
            # The listing date is the approval date, so it ages out like other jobs
            if newly:
                post["posted_date"] = date.today().isoformat()
                post["approved_once"] = True
        return dict(post), newly


def delete(post_id):
    with storage.edit("direct_posts") as posts:
        before = len(posts)
        posts[:] = [p for p in posts if p.get("id") != post_id]
        if len(posts) == before:
            raise ValidationError("الإعلان غير موجود.")


# ---------------------------------------------------------------- migration

LEGACY_FILES = ("companies.json", "company_jobs.json")


def migrate_legacy():
    """One-time move of the old company portal (accounts + jobs) into direct_posts.

    Active jobs of approved companies stay public (approved); active jobs of
    pending companies wait for moderation; closed jobs and suspended companies
    are dropped. The old files are renamed (kept, not deleted). Returns a summary.
    """
    store = config.STORE_DIR
    paths = {name: os.path.join(store, name) for name in LEGACY_FILES}
    if not any(os.path.exists(p) for p in paths.values()):
        return None

    def load(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except FileNotFoundError:
            return []

    summary = {"approved": 0, "pending": 0, "skipped": 0}
    with storage.locked():
        companies = {c.get("id"): c for c in load(paths["companies.json"]) if isinstance(c, dict)}
        old_jobs = [j for j in load(paths["company_jobs.json"]) if isinstance(j, dict)]
        with storage.edit("direct_posts") as posts:
            have = {p.get("id") for p in posts}
            for j in old_jobs:
                c = companies.get(j.get("company_id")) or {}
                if j.get("id") in have or j.get("status") == "closed" or c.get("status") not in ("approved", "pending"):
                    summary["skipped"] += 1
                    continue
                status = "approved" if c["status"] == "approved" else "pending"
                post = {k: j.get(k) for k in ("id", "title", "description", "city", "url", "specialty", "skills",
                                              "contract_type", "requirements", "salary", "is_training",
                                              "nationality", "gender", "posted_date", "created_at")}
                post.update({"company": j.get("company") or c.get("name") or "", "contact_email": c.get("email", ""),
                             "status": status, "scam_risk": bool(j.get("scam_risk")), "ip_hash": "",
                             "approved_once": status == "approved", "migrated": True,
                             "updated_at": storage.now_iso()})
                posts.append(post)
                summary[status] += 1
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        for p in paths.values():
            if os.path.exists(p):
                os.replace(p, p + ".migrated-" + stamp)
        # Old company login sessions are useless now
        with storage.edit("sessions") as sessions:
            for k in [k for k, v in sessions.items() if v.get("kind") == "company"]:
                del sessions[k]
    return summary
