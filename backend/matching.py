"""Job <-> user matching (same scoring as the site's preview in js/app.js).

score = specialty match (50; 30 for another specialty of the same group)
        + share of the job's skills the user has (40) + city match or remote (10).
An older specialty name (before 2026-11-07) fully matches the new specialties it covers.
Alerts need >= ALERT_MIN_SCORE, which deliberately requires a specialty match
(skills + city alone max out at 50).
"""

from . import normalize

ALERT_MIN_SCORE = 60


def _user_skills(user):
    skills = list(user.get("skills") or []) + list(user.get("cv_skills") or [])
    return [s for s in (normalize.norm(x) for x in skills) if s]


def specialty_score(job_spec, user_spec):
    if not job_spec or not user_spec:
        return 0
    if normalize.norm(job_spec) == normalize.norm(user_spec):
        return 50
    mine = normalize.specialties_for(user_spec)
    if job_spec in mine:
        return 50
    group = normalize.SPECIALTY_GROUP.get(job_spec)
    return 30 if group and any(normalize.SPECIALTY_GROUP.get(m) == group for m in mine) else 0


def score(job, user):
    mine = _user_skills(user)
    cv = normalize.norm(user.get("cv_text") or "")
    hits = []
    for skill in job.get("skills") or []:
        s = normalize.norm(skill)
        if not s:
            continue
        ok = any(x == s or (len(x) > 2 and len(s) > 2 and (s in x or x in s)) for x in mine) \
            or (len(s) > 2 and s in cv)
        if ok:
            hits.append(skill)

    total = 0
    total += specialty_score(job.get("specialty"), user.get("specialty"))
    if job.get("skills"):
        total += round(40 * len(hits) / len(job["skills"]))
    if job.get("city") == user.get("city") or job.get("city") == normalize.REMOTE:
        total += 10
    return min(100, total), hits


def top_matches(jobs, user, limit=5, min_score=ALERT_MIN_SCORE):
    scored = []
    for job in jobs:
        if job.get("is_demo") or job.get("scam_risk"):
            continue
        s, _ = score(job, user)
        if s >= min_score:
            scored.append((s, job.get("posted_date") or "", job))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [(s, j) for s, _, j in scored[:limit]]
