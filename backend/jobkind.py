"""What an ad offers, read by rules (no AI, so every stored job can be re-read at any time):

- category: "job", or one of the other sections (coop training, tamheer, training programmes,
  scholarships, courses, university admission). Only "job" is searched and alerted as a job;
  the others have their own section in the bot ("أخرى").
- role words: the job title without the employer, the city and the ad wording ("وظائف شاغرة لدى X
  بالرياض" -> nothing; "وظائف بصفة مدير حساب مبيعات متوفرة في الشركة الوطنية لأنظمة المعلومات" ->
  "مدير حساب مبيعات"). A job is matched on these words only, never on its employer or its text.
- announcement: an ad of several jobs whose title names no role ("تكامل القابضة تعلن 8 وظائف").
  Its roles are the job titles listed in its text ("الوظائف: 1- محلل بيانات 2- محاسب"); it is
  matched on those roles only, and the bot names the role that matched.
"""

import functools
import re
import threading

from . import normalize

VERSION = 4     # stored "category_v": a job read by older rules is read again

CATEGORIES = {   # key -> (bot button, section title)
    "coop": ("🎓 التدريب التعاوني", "التدريب التعاوني لطلاب الجامعات"),
    "tamheer": ("🧩 تمهير", "برنامج تمهير"),
    "training": ("📘 برامج التدريب والتأهيل", "برامج التدريب والتأهيل (منها المنتهي بالتوظيف)"),
    "scholarship": ("✈️ الابتعاث والمنح", "الابتعاث والمنح الدراسية"),
}
OTHER = tuple(CATEGORIES)
DROP = ("course", "admission")   # not kept at all (Raed, 2026-11-21): read only so they never pass as jobs

# On normalize.norm() text (ا for أإآ, ه for ة, ي for ى)
_RULES = [
    ("tamheer", re.compile(r"تمهير|tamheer")),
    ("scholarship", re.compile(r"ابتعاث|مبتعث|بعثه دراسيه|بعثات دراسيه|منح دراسيه|منحه دراسيه|المنح الدراسيه|"
                               r"scholarships?\b")),
    ("coop", re.compile(r"(?:ال)?تدريب (?:ال)?تعاوني|\bco-?op\b|cooperative (?:training|program)|تدريب صيفي|"
                        r"summer (?:training|internship|program)|\binternships?\b|\binterns?\b|طلاب الجامعات")),
    ("admission", re.compile(r"(?:بدء|فتح|اعلان|يعلن|تعلن|موعد|مواعيد|بدايه)\s+(?:\S+\s+){0,3}?(?:ال)?(?:قبول|تسجيل)|"
                             r"القبول والتسجيل|التسجيل والقبول|\badmissions?\b|"
                             r"(?:قبول|التسجيل في|التقديم علي) (?:\S+ )?(?:ال)?(?:ماجستير|دكتوراه|دبلوم|بكالوريوس|"
                             r"الدراسات العليا)")),
    ("course", re.compile(r"(?<!\w)(?:ال)?(?:دوره|دورات|كورس|كورسات)(?!\w)|\bcourses?\b|\bbootcamps?\b|"
                          r"معسكر|ورشه عمل|ورش عمل|\bworkshops?\b|\bwebinars?\b|محاضره|لقاء افتراضي|"
                          r"(?:برنامج|برامج|دبلوم) (?:ال)?لغه|english (?:language )?(?:course|program)")),
    ("training", re.compile(r"^برنامج|^برامج|برنامج (?:ال)?(?:تدريب|تاهيل|اعداد|تطوير)|برامج (?:ال)?(?:تدريب|تاهيل)|"
                            r"تدريب (?:منتهي|المنتهي) بالتوظيف|(?:ال)?منتهي بالتوظيف|تدريب علي راس العمل|"
                            r"graduate (?:development )?programs?|trainee programs?|management trainee|"
                            r"development programs?|\bacademy program")),
]
# A title that starts with a role is a job even when it names a field above ("أخصائي القبول والتسجيل",
# "مدرب دورات", "Training Coordinator", "منسق برامج التدريب")
_ROLE_START = re.compile(
    r"^(?:اخصايي|اخصاييه|موظف|موظفه|مسوول|مسووله|مدير|مديره|منسق|منسقه|مدرب|مدربه|مشرف|مشرفه|رييس|"
    r"محاضر|محاضره|معلم|معلمه|مستشار|ممثل|مساعد|مساعده|فني|مهندس|محلل|كاتب|سكرتير|"
    r"specialist|officer|coordinator|manager|trainer|instructor|supervisor|head|lead|director|consultant|"
    r"analyst|engineer|assistant|administrator|advisor|lecturer|teacher)(?!\w)")


def category(title, text=""):
    """'job' or one of OTHER, from the title (the start of the text only when the title is all ad wording)."""
    t = normalize.norm(title)
    for key, rx in _RULES:
        if rx.search(t):
            if _ROLE_START.search(t) and key not in ("tamheer", "coop"):
                return "job"
            return key
    return "job"


# ---------------------------------------------------------------- role words

_WORD = re.compile(r"[a-z0-9+#]+|[ء-ي]+")
# Ad wording around a title: never a role
_FILLER = set(normalize.norm(w) for w in """
وظائف وظايف وظيفه وظيفة وظيفيه الوظائف الوظيفه شاغره شاغر شواغر الشاغره فرص فرصه عمل توظيف التوظيف مطلوب مطلوبه
متوفره متاحه متاح بصفه بمسمي مسمي تعلن يعلن تعلنان اعلان عن توفر لتوفر توفير جديده جديد عاجل هام لحمله حمله حملة
البكالوريوس بكالوريوس الدبلوم دبلوم الثانويه ثانويه الماجستير ماجستير الدكتوراه حديثي حديث التخرج الخريجين خريجي
للخريجين للجنسين للرجال للنساء للسعوديين السعوديين للسعوديات السعوديات سعوديين رجال نساء براتب راتب برواتب رواتب يصل
تصل الي الى ريال الاف الف بدون خبره الخبره خبرات سنوات سنه فاعلي فما فوق اعلي تخصصات التخصصات بمختلف مختلف عده عدد
متعدده متنوعه شاغرتين وظيفتين فرصتين اداريه وفنيه اليوم الان فورا فوري مباشر مباشره بشكل دوام كامل جزئي عقد
في لدي لدى ب بال من مع علي على و او ل لل ال اكثر وغيرها وغيره داخل خارج المملكه السعوديه بالمملكه بالسعوديه
ذوي لذوي فاعلي فأعلى الكفاءات المتميزين للعمل العمل للعام العام الدراسي ثول بثول الجبيل ينبع رابغ نيوم
hiring jobs job vacancies vacancy now urgent opening openings we are is available new positions position careers
career opportunity opportunities apply in at for with and the of ksa saudi arabia
""".split())
# Organisation words: the tail of a title that holds them is the employer ("... لدى شركة X")
_ORG_WORDS = set(normalize.norm(w) for w in """
شركة الشركة مجموعة المجموعة هيئة الهيئة وزارة الوزارة جامعة الجامعة بنك البنك مصرف المصرف مستشفى المستشفى مستشفيات
مركز المركز مؤسسة المؤسسة مجمع كلية الكلية معهد المعهد صندوق الصندوق مدينة أمانة الأمانة بلدية ديوان الديوان قطاع القطاع
بقطاع شركات مصنع المصنع جمعية الجمعية
company group bank hospital university authority ministry corporation holding ltd inc co
""".split())


class _Org:
    @staticmethod
    def match(w):
        return w in _ORG_WORDS


_ORG = _Org()
_CUT = {"في", "لدي", "بشركه", "بشركة", "بمجموعه", "بهييه", "ببنك", "بوزاره", "بمستشفي", "بجامعه", "at", "with",
        "لصالح", "داخل", "بقطاع", "في شركه"}
_CITY_WORDS = None
_lock = threading.Lock()


def _city_words():
    global _CITY_WORDS
    if _CITY_WORDS is None:
        words = set()
        for city, aliases in normalize.CITY_ALIASES.items():
            for a in [city] + list(aliases):
                for w in _WORD.findall(normalize.norm(a)):
                    words.add(w)
                    words.add("ب" + w)
                    words.add("في" + w)
        words |= {"بالرياض", "بجده", "بالدمام", "بمكه", "بالمدينه", "بالخبر", "بالقصيم", "بالطايف", "بتبوك",
                  "الرياض", "جده", "المنطقه", "منطقه", "بمنطقه", "الشرقيه", "الغربيه", "الوسطي", "الجنوبيه",
                  "الشماليه", "بالمنطقه", "مدن", "مدينه", "بمدينه", "عده", "مختلف", "المدن"}
        _CITY_WORDS = words
    return _CITY_WORDS


def _company_words(company):
    if not normalize.company_known(company):
        return set()
    out = set()
    for w in _WORD.findall(normalize.norm(company)):
        out |= {w, "ب" + w, "ل" + w, "ال" + w}
        if w.startswith("ال"):
            out |= {w[2:], "بال" + w[2:], "لل" + w[2:]}
    return out


@functools.lru_cache(maxsize=60000)
def role_words(title, company=""):
    """The title's own role words (employer, city and ad wording removed), in order."""
    words = _WORD.findall(normalize.norm(title))
    comp = _company_words(company)
    # cut the tail that names the employer: "... لدى شركة X", "... في الشركة الوطنية ...", "... at Aramco"
    for i, w in enumerate(words):
        if i and (w in _CUT or w.startswith(("لدي", "لدى"))):
            tail = words[i + 1:]
            if any(_ORG.match(t) or t in comp for t in tail[:3]) or (comp and sum(t in comp for t in tail) >= 1):
                words = words[:i]
                break
    cities = _city_words()
    out = []
    for w in words:
        if w in _FILLER or w in comp or w in cities or w.isdigit() or len(w) < 2:
            continue
        if _ORG.match(w):      # "شركة تكامل القابضة تعلن ...": the employer at the start
            continue
        out.append(w)
    return tuple(out)


def is_announcement(title, company=""):
    """An ad of jobs whose title names no role: its roles are in the text."""
    from . import titles   # late: titles imports normalize only, but keep the graph simple
    rw = role_words(title, company)
    if titles.useful([titles._strip_prefix(w) for w in rw]):
        return False
    # "Administrator", "Executive Assistant", "مدير" + employer "مطعم": a plain title of generic words is ONE
    # role; only ad wording ("وظائف", "فرص", "تعلن", "hiring", ...) makes it an announcement of several jobs
    return not rw or bool(titles.GENERIC_TITLE_RE.search(normalize.norm(title)))


# ---------------------------------------------------------------- roles listed in the text

_HEAD_ROLES = re.compile(r"(?:ال)?(?:وظايف|وظيفه|الشواغر|شواغر|المسميات|مسميات|الوظايف المتاحه|التخصصات المطلوبه|"
                         r"التخصصات|الفرص|الشواغر الوظيفيه)\b|positions|vacancies|open roles|job titles|roles")
_HEAD_STOP = re.compile(r"شروط|الشروط|المتطلبات|متطلبات|المؤهلات|مؤهلات|المزايا|مميزات|المهام|مهام|طريقه التقديم|"
                        r"التقديم|للتقديم|رابط|موعد|requirements|qualifications|benefits|responsibilities|"
                        r"how to apply|apply|about")
_BULLET = re.compile(r"^\s*(?:\d{1,2}\s*[-.)–:]|[-•●▪*✓✔◾▫◽◼■□➖➤►▶👈🔹🔸🔻⬅️•]+)\s*")
_NOT_ROLE = re.compile(r"https?://|www\.|@|التقديم|رابط|الراتب|راتب|ريال|خبره|سنوات|اجاده|اللغه|مهارات|القدره|"
                       r"شهاده|ان يكون|يشترط|العمر|الجنسيه|سعودي الجنسيه|مقابله|ارسال|السيره|cv|email|salary|"
                       r"experience|years|degree|ability|skills|must|required")


_WANTED = re.compile(r"^(?:مطلوب|مطلوبه|نبحث عن|نحتاج|بحاجه الي|we are hiring|hiring|looking for)\s+", re.I)


def roles_in_text(text, limit=30):
    """Job titles an announcement lists ("الوظائف المتاحة:" then one per line, or numbered lines)."""
    out, mode = [], None
    for raw in (text or "").splitlines()[:150]:
        line = raw.strip()
        if not line:
            continue
        n = normalize.norm(line)
        bullet = bool(_BULLET.match(line))
        body = normalize.one_line(_BULLET.sub("", line), 120).strip(" :.-–،")
        nb = normalize.norm(body)
        words = _WORD.findall(nb)
        if not bullet and len(words) <= 6 and _HEAD_STOP.search(n):
            mode = "stop"
            continue
        head, colon, rest = line.partition(":")
        if colon and rest.strip() and _HEAD_ROLES.search(normalize.norm(head)) and not _HEAD_STOP.search(normalize.norm(head)):
            # "تعلن شركة X عن وظائف: محاسب، مندوب مبيعات" (the list on the same line)
            for part in re.split(r"\s*[،,؛;/|]\s*|\s+-\s+", rest):
                role = normalize.one_line(part, 60).strip(" .-–:")
                if len(role) >= 3 and role not in out and role_words(role) and not _NOT_ROLE.search(normalize.norm(role)):
                    out.append(role)
            mode = "roles"
            continue
        m = _WANTED.match(nb)
        if m and not bullet and len(words) <= 8:
            role = normalize.one_line(" ".join(body.split()[len(m.group(0).split()):]), 60).strip(" .-–:")
            role = re.split(r"\s*[(:\[]|\s+(?:في|لدى|لدي|بمدينة|براتب|للعمل)\s+", role)[0]
            if len(role) >= 3 and role not in out and role_words(role):
                out.append(role)
            continue
        if not bullet and _HEAD_ROLES.search(n) and (n.rstrip().endswith(":") or len(words) <= 5):
            mode = "roles"
            continue
        if mode == "stop":   # requirements / benefits / how to apply: no roles there
            continue
        if not (1 <= len(words) <= 7) or _NOT_ROLE.search(nb):
            continue
        if mode == "roles" or bullet:
            role = re.split(r"\s*[(:\[]|\s+[-–|]\s+", body)[0].strip(" -–:،.")
            if len(role) >= 3 and role not in out and role_words(role):
                out.append(role[:60])
        if len(out) >= limit:
            break
    return out


def describe(job):
    """{"category", "announce", "roles"} for a job: stored by the collector (category_v current), else read now."""
    if job.get("category_v") == VERSION and "category" in job:
        return {"category": job["category"], "announce": bool(job.get("announce")), "roles": job.get("roles") or []}
    key = (job.get("id"), job.get("title"), job.get("company"))
    with _lock:
        hit = _cache.get(key)
    if hit is None:
        hit = read(job)
        with _lock:
            if len(_cache) > 60000:
                _cache.clear()
            _cache[key] = hit
    return hit


_cache = {}


def read(job):
    title, company = job.get("title") or "", job.get("company") or ""
    text = job.get("raw_text") or job.get("description") or ""
    cat = category(title, text)
    announce = cat == "job" and is_announcement(title, company)
    roles = [r for r in (job.get("roles") or []) if isinstance(r, str)] if job.get("roles_ai") else []
    if announce and not roles:
        roles = roles_in_text(text)
    return {"category": cat, "announce": announce, "roles": roles if announce else []}


def stamp(job):
    """Store the reading on the job (collector side): the bot then never reads the text again."""
    d = read(job)
    job["category"], job["announce"], job["category_v"] = d["category"], d["announce"], VERSION
    if d["announce"]:
        job["roles"] = d["roles"]
    else:
        job.pop("roles", None)
        job.pop("roles_ai", None)
    return job
