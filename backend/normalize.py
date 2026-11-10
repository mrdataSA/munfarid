"""Normalize raw job data from any source into RIZQ's single job schema.

Schema (CLAUDE.md): title, company, city, specialty, skills[], contract_type,
url, source, posted_date, raw_text, hash  (+ id, salary, is_training,
description, requirements, collected_at).

Rule-based detection here is the fallback when Groq is not configured.
"""

import functools
import hashlib
import html
import re
from datetime import date, datetime, timedelta

REMOTE = "عن بعد"
OTHER_SPECIALTY = "أخرى"
CONTRACTS = ("دوام كامل", "دوام جزئي", "عقد مؤقت", "تدريب")

# Specialties, close to LinkedIn's job functions plus Saudi-specific ones, in groups.
# A user's alert matches the exact specialty fully, and other specialties of the same group partially.
SPECIALTY_GROUPS = [
    ("التقنية", ["تطوير البرمجيات", "تقنية المعلومات والدعم الفني", "الأمن السيبراني", "البيانات والذكاء الاصطناعي"]),
    ("الهندسة", ["الهندسة المدنية والمعمارية", "الهندسة الكهربائية والإلكترونية", "الهندسة الميكانيكية والصناعية",
                 "هندسة البترول والكيمياء والتعدين", "الصيانة والفنيون", "السلامة والصحة المهنية"]),
    ("الصحة", ["الطب وطب الأسنان", "التمريض", "الصيدلة", "المختبرات والأشعة والتأهيل"]),
    ("المال والأعمال", ["المحاسبة والمراجعة", "المالية والبنوك والاستثمار", "الاستشارات", "تطوير الأعمال",
                        "إدارة المشاريع", "إدارة المنتجات", "العمليات", "الجودة", "المشتريات", "القانون", "العقار"]),
    ("المبيعات والتسويق", ["المبيعات", "التسويق والإعلان", "خدمة العملاء", "الإعلام والاتصال", "التصميم والفنون"]),
    ("الإدارة والموارد البشرية", ["الموارد البشرية", "الإدارة والسكرتارية"]),
    ("التعليم والبحث", ["التعليم والتدريب", "البحث العلمي"]),
    ("الخدمات والتشغيل", ["اللوجستيات وسلاسل الإمداد", "السائقون والنقل", "المطاعم والضيافة والسياحة", "الطيران",
                          "الخدمات الاجتماعية", "الوظائف العسكرية والأمنية"]),
]
SPECIALTIES = [s for _, specs in SPECIALTY_GROUPS for s in specs]
SPECIALTY_GROUP = {s: g for g, specs in SPECIALTY_GROUPS for s in specs}

# Older specialty names (before 2026-11-07) -> the new specialties they cover
LEGACY_SPECIALTIES = {
    "تقنية المعلومات": ["تطوير البرمجيات", "تقنية المعلومات والدعم الفني"],
    "الهندسة": ["الهندسة المدنية والمعمارية", "الهندسة الكهربائية والإلكترونية", "الهندسة الميكانيكية والصناعية",
                "هندسة البترول والكيمياء والتعدين", "الصيانة والفنيون", "السلامة والصحة المهنية"],
    "الصحة والتمريض": ["الطب وطب الأسنان", "التمريض", "المختبرات والأشعة والتأهيل"],
    "المحاسبة والمالية": ["المحاسبة والمراجعة", "المالية والبنوك والاستثمار"],
    "المبيعات والتسويق": ["المبيعات", "التسويق والإعلان", "تطوير الأعمال"],
    "الإدارة": ["الإدارة والسكرتارية", "إدارة المشاريع", "العمليات"],
    "المطاعم والضيافة": ["المطاعم والضيافة والسياحة"],
    "اللوجستيات وسلاسل الإمداد": ["اللوجستيات وسلاسل الإمداد", "المشتريات", "السائقون والنقل"],
}


def specialties_for(value):
    """The specialties a user's choice covers: itself, a whole group, or a legacy name's new specialties."""
    if value in SPECIALTY_GROUP:
        return [value]
    if value in LEGACY_SPECIALTIES:   # before groups: "المبيعات والتسويق" is both an old name and a group
        return list(LEGACY_SPECIALTIES[value])
    for g, specs in SPECIALTY_GROUPS:
        if value == g:
            return list(specs)
    return list(LEGACY_SPECIALTIES.get(value, []))

# canonical city -> aliases (normalized Arabic + lowercase English)
CITY_ALIASES = {
    "الرياض": ["الرياض", "رياض", "riyadh"],
    "جدة": ["جده", "جدة", "jeddah", "jiddah", "jedda"],
    "مكة المكرمة": ["مكه", "مكة", "makkah", "mecca", "makka"],
    "المدينة المنورة": ["المدينه المنوره", "المدينة المنورة", "madinah", "medina", "madina"],
    "الدمام": ["الدمام", "dammam"],
    "الخبر": ["الخبر", "khobar", "al khobar", "alkhobar"],
    "الظهران": ["الظهران", "dhahran"],
    "الجبيل": ["الجبيل", "jubail"],
    "الأحساء": ["الاحساء", "الأحساء", "الهفوف", "ahsa", "al-ahsa", "hofuf", "hufuf"],
    "القطيف": ["القطيف", "qatif"],
    "القصيم": ["القصيم", "بريده", "بريدة", "عنيزه", "عنيزة", "qassim", "buraidah", "buraydah", "unaizah"],
    "الطائف": ["الطائف", "الطايف", "taif"],
    "تبوك": ["تبوك", "tabuk"],
    "أبها": ["ابها", "أبها", "abha"],
    "خميس مشيط": ["خميس مشيط", "khamis mushait", "khamis mushayt"],
    "حائل": ["حائل", "حايل", "hail", "ha'il"],
    "جازان": ["جازان", "جيزان", "jazan", "jizan", "gizan"],
    "نجران": ["نجران", "najran"],
    "ينبع": ["ينبع", "yanbu", "yanbu'"],
    "الباحة": ["الباحه", "الباحة", "al baha", "albaha"],
    "الجوف": ["الجوف", "سكاكا", "sakaka", "al jouf", "jouf"],
    "عرعر": ["عرعر", "arar"],
    "رابغ": ["رابغ", "rabigh"],
    REMOTE: ["عن بعد", "عن بُعد", "من المنزل", "remote", "work from home", "wfh"],
}

# specialty -> keywords. Order matters: more specific first.
# Rule-based detection (fallback before AI). Normalized forms (ة->ه, أ->ا). Trailing space = whole word.
# Order matters on ties: specific before generic.
SPECIALTY_KEYWORDS = [
    ("الأمن السيبراني", ["امن سيبراني", "الامن السيبراني", "سيبراني", "cyber", "security analyst", "soc analyst", "soc ",
                          "information security", "امن المعلومات", "penetration", "grc ", "iam ", "security engineer"]),
    ("البيانات والذكاء الاصطناعي", ["data analyst", "data scientist", "data engineer", "machine learning", "ذكاء اصطناعي",
                                    "تحليل بيانات", "محلل بيانات", "علم البيانات", "business intelligence", " bi ",
                                    "ai engineer", "artificial intelligence", "deep learning", "nlp", "llm", "big data",
                                    "data governance", "حوكمه البيانات", "data management"]),
    ("تطوير البرمجيات", ["developer", "software", "programmer", "مطور", "مبرمج", "برمجه", "frontend", "front-end",
                         "backend", "back-end", "full stack", "fullstack", "devops", "mobile app", "ios ", "android",
                         "flutter", "react", "java ", ".net", "qa engineer", "test automation", "sre ", "cloud engineer",
                         "solutions architect", "software architect"]),
    ("تقنية المعلومات والدعم الفني", ["تقنيه المعلومات", "it support", "دعم فني", "helpdesk", "help desk", "شبكات",
                                      "network", "system admin", "systems admin", "سيستم", "حاسب", "computer", "erp",
                                      "sap ", "oracle", "it ", "infrastructure", "service desk", "desktop support",
                                      "database administrator", "dba ", "it specialist", "it manager"]),
    ("الصيدلة", ["صيدلي", "صيدلاني", "صيدليه", "pharmac"]),
    ("التمريض", ["تمريض", "ممرض", "nurse ", "nurses ", "nursing", "قابله", "midwife"]),
    ("المختبرات والأشعة والتأهيل", ["مختبر", "laboratory", "lab technician", "lab specialist", "اشعه", "radiolog",
                                    "radiographer", "sonograph", "therapist", "physiotherap", "علاج طبيعي", "اخصائي علاج",
                                    "respiratory", "تنفسي", "dietitian", "تغذيه", "optometr", "بصريات", "audiolog"]),
    ("الطب وطب الأسنان", ["طبيب", "doctor ", "doctors ", "physician", "consultant physician", "specialist physician",
                          "resident ", "استشاري", "اخصائي طب", "اسنان", "dentist", "dental", "surgeon", "جراح",
                          "anesthes", "تخدير", "pediatric", "اطفال", "gynec", "نساء وولاده", "cardiolog", "medical officer",
                          "general practitioner", "طب عام", "family medicine", "طب اسره"]),
    ("السلامة والصحة المهنية", ["hse", "safety", "سلامه", "صحه مهنيه", "health and safety", "fire ", "اطفاء",
                                "environmental", "بيئه"]),
    ("هندسة البترول والكيمياء والتعدين", ["petroleum", "بترول", "reservoir", "drilling", "حفر", "chemical engineer",
                                          "هندسه كيميائيه", "مهندس كيميائي", "process engineer", "mining", "تعدين",
                                          "geolog", "جيولوج", "geophysic", "refinery", "مصفاه", "production engineer",
                                          "corrosion", "pipeline", "oil and gas", "oil & gas", "upstream", "downstream"]),
    ("الهندسة الكهربائية والإلكترونية", ["electrical engineer", "مهندس كهرباء", "مهندس كهربائي", "هندسه كهربائيه",
                                         "electronics", "الكترونيات", "power systems", "instrumentation", "control engineer",
                                         "scada", "plc", "telecom engineer", "اتصالات", "rf engineer", "substation",
                                         "protection engineer", "electrical design"]),
    ("الهندسة الميكانيكية والصناعية", ["mechanical engineer", "مهندس ميكانيك", "هندسه ميكانيكيه", "hvac", "industrial engineer",
                                       "هندسه صناعيه", "مهندس صناعي", "manufacturing engineer", "piping", "rotating equipment",
                                       "static equipment", "mep ", "plumbing engineer", "production planning",
                                       "mechanical design", "aerospace engineer"]),
    ("الهندسة المدنية والمعمارية", ["civil engineer", "مهندس مدني", "هندسه مدنيه", "structural", "انشائي", "معماري",
                                    "architect", "مساح", "surveyor", "quantity surveyor", "حصر كميات", "site engineer",
                                    "construction", "انشاءات", "مقاولات", "infrastructure engineer", "roads", "طرق",
                                    "geotechnical", "interior design", "تصميم داخلي", "urban planning", "تخطيط عمراني",
                                    "landscape", "bim", "revit", "autocad"]),
    ("الصيانة والفنيون", ["maintenance", "صيانه", "technician", "فني", "electrician", "كهربائي", "plumber", "سباك",
                          "welder", "لحام", "mechanic", "ميكانيكي", "hvac technician", "تكييف", "operator", "مشغل",
                          "fitter", "carpenter", "نجار", "painter", "دهان", "حداد"]),
    ("إدارة المشاريع", ["project manager", "مدير مشروع", "مدير مشاريع", "project director", "pmo", "project coordinator",
                        "منسق مشاريع", "program manager", "project control", "planning engineer", "planner",
                        "scheduler", "اداره المشاريع", "project engineer"]),
    ("إدارة المنتجات", ["product manager", "product owner", "مدير منتج", "مالك المنتج", "product management",
                        "product lead", "scrum master"]),
    ("الجودة", ["quality", "جوده", "qa/qc", "qa qc", "qc ", "qa ", "iso ", "inspector", "مفتش", "audit quality",
                "quality control", "quality assurance", "ضبط الجوده", "توكيد الجوده"]),
    ("المشتريات", ["procurement", "مشتريات", "purchas", "buyer", "sourcing", "contracts", "عقود",
                   "tender", "مناقصات", "category manager"]),
    ("اللوجستيات وسلاسل الإمداد", ["لوجست", "logistic", "supply chain", "سلاسل الامداد", "مستودع", "warehouse",
                                    "شحن", "shipping", "inventory", "مخازن", "storekeeper", "امين مستودع", "freight",
                                    "customs", "تخليص جمركي", "fleet", "demand planner", "import", "export"]),
    ("السائقون والنقل", ["سائق", "driver", "delivery", "توصيل", "مندوب توصيل", "courier", "transport", "نقل",
                         "chauffeur", "heavy equipment operator"]),
    ("المحاسبة والمراجعة", ["محاسب", "accountant", "accounting", "accounts ", "audit", "مدقق", "تدقيق", "مراجع داخلي", "internal audit", "زكاه",
                            "ضريبه", "tax ", "vat", "payroll", "accounts payable", "accounts receivable", "bookkeep",
                            "socpa", "cpa", "general ledger"]),
    ("المالية والبنوك والاستثمار", ["مالي", "finance", "financial", "treasury", "خزينه", "bank", "بنك", "مصرف",
                                    "مصرفي", "investment", "استثمار", "credit", "ائتمان", "risk", "مخاطر", "compliance",
                                    "التزام", "fp&a", "insurance", "تامين", "actuar", "wealth", "portfolio", "teller",
                                    "aml", "fintech", "cashier supervisor"]),
    ("الاستشارات", ["consultant", "استشاري اداري", "consulting", "advisory", "استشارات", "strategy", "استراتيجي",
                    "management consultant"]),
    ("تطوير الأعمال", ["business development", "تطوير اعمال", "partnership", "شراكات", "bdm", "bd manager",
                       "growth", "commercial manager", "investor relations"]),
    ("القانون", ["legal", "قانوني", "lawyer", "محامي", "مستشار قانوني", "counsel", "paralegal", "شؤون قانونيه",
                 "contracts lawyer", "litigation", "قضايا"]),
    ("العقار", ["real estate", "عقار", "property", "leasing", "تاجير", "facility", "facilities", "مرافق", "تقييم عقاري",
                "valuation", "broker"]),
    ("العمليات", ["operations", "عمليات", "operation manager", "مدير عمليات", "business process", "تحسين العمليات",
                  "excellence", "lean", "six sigma", "شؤون اداريه وتشغيل"]),
    ("المبيعات", ["مبيعات", "sales", "مندوب مبيعات", "sales representative", "account manager", "key account",
                  "retail", "بيع", "بائع", "معرض", "showroom", "store manager", "مدير فرع", "cashier", "كاشير",
                  "merchandiser", "تجزئه", "pre-sales", "presales"]),
    ("التسويق والإعلان", ["تسويق", "marketing", "digital marketing", "social media", "سوشال ميديا", "brand manager",
                          "brand specialist", "علامه تجاريه", "seo", "content creator", "content writer", "صانع محتوى",
                          "كاتب محتوى", "advertising", "حملات اعلانيه", "crm specialist", "growth marketing",
                          "e-commerce specialist", "تجاره الكترونيه"]),
    ("خدمة العملاء", ["خدمه عملاء", "خدمة العملاء", "customer service", "customer care", "call center", "كول سنتر",
                      "مركز اتصال", "استقبال", "receptionist", "front desk", "customer support", "customer experience",
                      "customer success", "تجربه العملاء", "contact center"]),
    ("الإعلام والاتصال", ["اعلام", "media", "journalist", "صحفي", "communication", "اتصال مؤسسي", "علاقات عامه",
                          "public relations", "pr ", "editor", "محرر", "translator", "مترجم", "ترجمه", "copywriter",
                          "spokesperson", "مذيع", "video", "producer", "photograph", "مصور"]),
    ("التصميم والفنون", ["designer", "مصمم", "graphic", "جرافيك", "ui/ux", "ux ", "ui ", "visual", "motion", "موشن",
                         "illustrat", "art director", "فنون", "creative", "ابداعي", "animation", "fashion", "ازياء"]),
    ("الموارد البشرية", ["موارد بشريه", "human resources", "hr ", "hr specialist", "recruit", "talent acquisition",
                         "اخصائي توظيف", "شؤون موظفين", "شوون الموظفين", "شيون موظفين", "شيون الموظفين", "hrbp", "workforce", "people partner",
                         "learning and development", "تطوير الموظفين", "compensation", "تعويضات", "personnel", "توطين",
                         "gro ", "government relations", "علاقات حكوميه", "معقب"]),
    ("التعليم والتدريب", ["معلم", "معلمه", "مدرس", "teacher", "تدريس", "lecturer", "محاضر", "اكاديمي", "tutor",
                          "مدرب", "trainer", "education", "روضه", "kindergarten", "professor", "استاذ", "عضو هيئه تدريس",
                          "curriculum", "مناهج", "instructional"]),
    ("البحث العلمي", ["research", "باحث", "بحث علمي", "researcher", "scientist", "عالم", "postdoc", "r&d", "ابحاث"]),
    ("الطيران", ["pilot", "طيار", "cabin crew", "مضيف", "aviation", "طيران", "aircraft", "طائرات", "airport", "مطار",
                 "flight", "air traffic", "ground handling", "خدمات ارضيه", "aerospace"]),
    ("المطاعم والضيافة والسياحة", ["مطعم", "restaurant", "باريستا", "barista", "فندق", "hotel", "chef", "طباخ", "شيف",
                                   "ضيافه", "hospitality", "waiter", "كوفي", "cafe", "مقهى", "tourism", "سياحه",
                                   "tour guide", "مرشد سياحي", "events", "فعاليات", "guest relations", "housekeeping",
                                   "food and beverage", "f&b", "kitchen", "مطبخ", "حلويات", "bakery"]),
    ("الخدمات الاجتماعية", ["اخصائي اجتماعي", "social worker", "اخصائي نفسي", "psycholog", "counsel", "ارشاد",
                            "مرشد طلابي", "رعايه", "تطوع", "volunteer", "community", "مجتمعي", "nonprofit", "جمعيه خيريه"]),
    ("الوظائف العسكرية والأمنية", ["عسكري", "عسكريه", "military", "حرس", "امن عام", "الجوازات", "الدفاع المدني",
                                   "جندي", "رقيب", "security guard", "حارس امن", "security officer", "امن وسلامه",
                                   "شرطه", "police", "cadet", "كليه عسكريه"]),
    ("الإدارة والسكرتارية", ["سكرتير", "secretary", "مساعد اداري", "administrative assistant", "executive assistant",
                             "office manager", "مدير مكتب", "data entry", "مدخل بيانات", "clerk", "archiv", "ارشيف",
                             "document control", "مراقب وثائق", "اداري", "administrat"]),
]

# Words too common in ad text to decide a specialty: counted in the title only, with weight 1,
# so they settle a job only when nothing more specific matches ("Hotel Manager" stays hospitality).
WEAK_TITLE_WORDS = [
    ("الإدارة والسكرتارية", ["مدير", "manager", "منسق", "coordinator", "مكتب", "office ", "تنفيذي", "executive",
                             "مدير عام", "general manager", "ceo", "director", "assistant", "مساعد", "supervisor", "مشرف"]),
]
# An engineer's title is never a technician job, even if it names the trade ("مهندس ميكانيكي")
ENGINEER_IN_TITLE = ("مهندس", "engineer", "هندسه")

CONTRACT_KEYWORDS = [
    ("تدريب", ["تدريب منتهي بالتوظيف", "تدريب تعاوني", "تعاوني", "internship", "intern ", "trainee", "graduate program",
               "برنامج تدريب", "برنامج تطوير الخريجين", "تمهير", "coop ", "co-op ", "تدريب على رأس العمل"]),
    ("دوام جزئي", ["دوام جزئي", "part time", "part-time", "parttime", "جزئي", "ساعات مرنه", "عمل مرن", "freelance", "مرن"]),
    ("عقد مؤقت", ["عقد مؤقت", "مؤقت", "temporary", "contract role", "contractor", "موسمي", "seasonal", "fixed term"]),
]

# Known skills vocabulary for rule-based extraction (display form)
SKILL_VOCAB = [
    "Excel", "Word", "PowerPoint", "SAP", "Oracle", "ERP", "CRM", "Salesforce", "Power BI", "Tableau",
    "SQL", "Python", "Java", "JavaScript", "TypeScript", "React", "Angular", "Vue", "Node.js", ".NET", "C#",
    "PHP", "Flutter", "Swift", "Kotlin", "HTML", "CSS", "Git", "Linux", "AWS", "Azure", "Docker", "Kubernetes",
    "AutoCAD", "Revit", "Primavera", "Photoshop", "Illustrator", "Figma",
    "SIEM", "ISO 27001", "CCNA", "PMP", "CPA", "SOCPA", "CMA", "IFRS", "BLS", "ACLS",
    "اللغة الإنجليزية", "رخصة قيادة", "خدمة العملاء", "التواصل", "إدارة المشاريع", "المبيعات",
    "التسويق الرقمي", "المحاسبة", "التوظيف", "إدارة المخزون", "سلاسل الإمداد", "التمريض", "التدريس",
    "تحليل البيانات", "الشبكات", "الصيانة", "السلامة المهنية", "الكاشير", "العمل الجماعي", "حل المشكلات",
]
SKILL_ALIASES = {
    "اللغة الإنجليزية": ["english", "انجليزي", "الانجليزيه", "اللغه الانجليزيه"],
    "رخصة قيادة": ["driving license", "driving licence", "رخصه قياده", "رخصة قيادة"],
    "خدمة العملاء": ["customer service", "خدمه العملاء", "خدمة العملاء"],
    "التواصل": ["communication", "مهارات التواصل", "مهارات تواصل"],
    "إدارة المشاريع": ["project management", "اداره المشاريع"],
    "المبيعات": ["sales ", "مبيعات"],  # whole word: not "Salesforce"
    "التسويق الرقمي": ["digital marketing", "التسويق الرقمي", "تسويق رقمي"],
    "المحاسبة": ["accounting", "محاسبه"],
    "التوظيف": ["recruitment", "توظيف"],
    "إدارة المخزون": ["inventory", "المخزون"],
    "سلاسل الإمداد": ["supply chain", "سلاسل الامداد"],
    "التمريض": ["nursing", "تمريض"],
    "التدريس": ["teaching", "تدريس"],
    "تحليل البيانات": ["data analysis", "تحليل البيانات", "تحليل بيانات"],
    "الشبكات": ["networking", "شبكات"],
    "الصيانة": ["maintenance", "صيانه"],
    "السلامة المهنية": ["hse", "safety", "السلامه المهنيه"],
    "الكاشير": ["cashier", "كاشير"],
    "العمل الجماعي": ["teamwork", "team work", "العمل الجماعي"],
    "حل المشكلات": ["problem solving", "حل المشكلات"],
}

JOB_POSITIVE = ["وظيفه", "وظائف", "مطلوب", "مطلوبه", "توظيف", "تعلن", "يعلن", "شاغر", "فرص عمل", "فرصه عمل",
                "تدريب منتهي", "برنامج تدريب", "hiring", "vacanc", "job", "position", "we are looking", "join our team",
                "career", "قبول", "التقديم", "للتقديم", "رابط التقديم", "apply"]
JOB_NEGATIVE = ["للبيع", "للايجار", "دوره تدريبيه مجانيه", "دورة مجانية", "اشترك في القناه", "تهنئه", "عيد مبارك",
                "اعلان ممول", "خصم", "عرض خاص", "promo code"]

# Saudi-specific tags
SAUDI_ONLY = ["للسعوديين فقط", "للسعوديين", "للسعوديات", "سعودي الجنسيه", "سعوديه الجنسيه", "سعودي الجنسية",
              "ان يكون سعودي", "أن يكون سعودي", "saudi nationals only", "saudi national", "saudis only"]
MEN_ONLY = ["للرجال", "للرجال فقط", "رجال فقط", "ذكور", "male only", "males only"]
WOMEN_ONLY = ["للنساء", "نسائيه", "نسائية", "للسيدات", "للسعوديات", "female only", "females only", "women only"]
BOTH_GENDERS = ["للجنسين", "رجال ونساء", "للرجال والنساء", "both genders"]
# Job posts asking candidates for money are a classic scam in the region
SCAM_WORDS = ["رسوم توظيف", "رسوم التسجيل", "رسوم تسجيل", "ادفع", "تحويل مبلغ", "حول مبلغ", "رسوم رمزيه",
              "رسوم ادارية", "رسوم اداريه", "registration fee", "processing fee", "pay a fee"]

BIDI_RE = re.compile(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]")
TAG_RE = re.compile(r"<[^>]+>")
URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")


def norm(s):
    """Arabic-aware normalization for matching (mirrors norm() in js/app.js)."""
    s = str(s or "").lower()
    s = re.sub(r"[ً-ْـ]", "", s)
    s = re.sub(r"[أإآ]", "ا", s)
    s = s.replace("ة", "ه").replace("ى", "ي").replace("ئ", "ي").replace("ؤ", "و")
    s = re.sub(r"[٠-٩]", lambda m: str(ord(m.group()) - 0x0660), s)
    return re.sub(r"\s+", " ", s).strip()


PHONE_RE = re.compile(r"(?:\+?966|00966|0)?\s?5\d(?:[\s-]?\d){7}")


def strip_phones(s):
    """Jobs only, no personal data: drop mobile numbers from collected post text."""
    return PHONE_RE.sub("[رقم محذوف]", s)


def clean_text(s, max_len=None):
    """Strip HTML, entities, bidi controls; collapse whitespace."""
    s = str(s or "")[: (max_len * 4 if max_len else 60000)]  # bound regex work on huge inputs
    s = html.unescape(TAG_RE.sub(" ", s))
    s = BIDI_RE.sub("", s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s).strip()
    return s[:max_len] if max_len else s


def clean_title(s, max_len=140):
    t = one_line(s, 300).replace("#", " ").replace("_", " ")
    t = re.sub(r"^[\W_]+", "", t)                      # leading emoji / symbols / brackets
    t = re.sub(r"^(عاجل|جديد|هام|مهم)\s*[|:\-–]*\s*", "", t)
    t = re.sub(r"^[\W_]+", "", t)
    t = re.sub(r"[\s|:\-–]+$", "", t)
    return re.sub(r"\s+", " ", t).strip()[:max_len]


def one_line(s, max_len=160):
    return re.sub(r"\s+", " ", clean_text(s)).strip()[:max_len]


@functools.lru_cache(maxsize=8192)
def _kw_pattern(needle, whole_word=False):
    """Compiled pattern for one keyword (cached: ~1,000 keywords x thousands of jobs)."""
    n = norm(needle)
    if not n:
        return None
    # Latin keywords must start a word ("account" matches "accountant", not "subaccount");
    # a trailing space in the keyword ("hr ", "intern ") or whole_word=True forces a whole-word match
    if re.fullmatch(r"[a-z0-9 .+#'&/-]+", n):
        tail = r"(?![a-z])" if whole_word or needle.endswith(" ") else ""
        return re.compile(r"(?<![a-z])" + re.escape(n.strip()) + tail)
    # Arabic: must start a word, allowing common prefixes (و ف ب ل ك + ال/لل),
    # so that "حاسب" does not match inside "محاسب"
    if n.startswith("ال"):
        n = n[2:]
    return re.compile(r"(?:^|[^\w])(?:[وفبلك])?(?:ال|لل)?" + re.escape(n))


def _contains(hay_norm, needle, whole_word=False):
    pat = _kw_pattern(needle, whole_word)
    return pat is not None and pat.search(hay_norm) is not None


# Aliases that are also common words ("الخبر" = "the news") need location context
AMBIGUOUS_CITY_CONTEXT = {
    "الخبر": ["في الخبر", "بالخبر", "مدينه الخبر", "محافظه الخبر", "الخبر،", "الخبر -", "الخبر/", "و الخبر", "والخبر",
              "khobar", "الدمام الخبر", "الخبر الدمام"],
}


def _first_pos(hay, alias):
    n = norm(alias)
    if re.fullmatch(r"[a-z0-9 .+#'-]+", n):
        m = re.search(r"(?<![a-z])" + re.escape(n.strip()) + r"(?![a-z])", hay)
    else:
        core = n[2:] if n.startswith("ال") else n
        m = re.search(r"(?:^|[^\w])(?:[وفبلك])?(?:ال|لل)?" + re.escape(core) + r"(?![\w])", hay)
    return m.start() if m else -1


def detect_city(*texts):
    """The city mentioned first wins (posts often list HQ + branches later)."""
    hay = norm(" ".join(t for t in texts if t))
    best, best_pos = "", None
    for city, aliases in CITY_ALIASES.items():
        if city == REMOTE:
            continue
        if city in AMBIGUOUS_CITY_CONTEXT:
            positions = [hay.find(norm(c)) for c in AMBIGUOUS_CITY_CONTEXT[city]]
        else:
            positions = [_first_pos(hay, a) for a in aliases]
        positions = [p for p in positions if p >= 0]
        if positions and (best_pos is None or min(positions) < best_pos):
            best, best_pos = city, min(positions)
    if best:
        return best
    if any(_contains(hay, a) for a in CITY_ALIASES[REMOTE]):
        return REMOTE
    return ""


def canonical_city(value):
    """Map a free-form location string to a canonical city ('' if unknown)."""
    return detect_city(value)


@functools.lru_cache(maxsize=1)
def _specialty_patterns():
    """One combined regex per specialty (same word rules as _contains), so a job is scanned
    ~2 x 40 times instead of ~2 x 1,000. Returns [(specialty, latin_re, arabic_re)]."""
    out = []
    for spec, words in SPECIALTY_KEYWORDS:
        latin, arabic = [], []
        for w in words:
            n = norm(w)
            if not n:
                continue
            if re.fullmatch(r"[a-z0-9 .+#'&/-]+", n):
                latin.append(re.escape(n.strip()) + (r"(?![a-z])" if w.endswith(" ") else ""))
            else:
                arabic.append(re.escape(n[2:] if n.startswith("ال") else n))
        latin.sort(key=len, reverse=True)
        arabic.sort(key=len, reverse=True)
        lat = re.compile(r"(?<![a-z])(" + "|".join(latin) + ")") if latin else None
        ara = re.compile(r"(?:^|[^\w])(?:[وفبلك])?(?:ال|لل)?(" + "|".join(arabic) + ")") if arabic else None
        out.append((spec, lat, ara))
    return out


def _distinct_hits(hay, lat, ara):
    found = set()
    for pat in (lat, ara):
        if pat is not None and hay:
            found.update(m.group(1) for m in pat.finditer(hay))
    return len(found)


@functools.lru_cache(maxsize=1)
def _weak_patterns():
    out = {}
    for spec, words in WEAK_TITLE_WORDS:
        out[spec] = [w for w in words]
    return out


def detect_specialty(title, text=""):
    t, x = norm(title), norm(text)
    engineer = any(_contains(t, w) for w in ENGINEER_IN_TITLE) and not t.startswith(("فني", "technician"))
    weak = _weak_patterns()
    best, best_score = OTHER_SPECIALTY, 0
    for spec, lat, ara in _specialty_patterns():
        title_hits = _distinct_hits(t, lat, ara)
        if engineer and spec == "الصيانة والفنيون":
            title_hits = 0
        score = 3 * title_hits + _distinct_hits(x, lat, ara)
        score += sum(1 for w in weak.get(spec, ()) if _contains(t, w))
        if score > best_score:
            best, best_score = spec, score
    return best


def detect_contract(*texts):
    hay = norm(" ".join(t for t in texts if t))
    for contract, words in CONTRACT_KEYWORDS:
        if any(_contains(hay, w) for w in words):
            return contract
    return "دوام كامل"


def detect_skills(*texts, limit=8):
    hay = norm(" ".join(t for t in texts if t))
    found = []
    for skill in SKILL_VOCAB:
        # Skill names are whole words: "Excel" must not match "excellent", "Java" not "JavaScript"
        if _contains(hay, skill, whole_word=True) or any(_contains(hay, a) for a in SKILL_ALIASES.get(skill, [])):
            found.append(skill)
        if len(found) >= limit:
            break
    return found


TITLE_NEGATIVE = ["دورات", "دوره", "دورة", "ملخص", "نصائح", "نصيحه", "مقال", "كورس", "course", "webinar",
                  "ورشه عمل", "ورشة عمل", "لقاء", "مسابقه", "مسابقة", "منحه", "منحة", "عروض"]


def looks_like_job(text):
    """Cheap rule-based 'is this a job post?' used when Groq is unavailable."""
    t = norm(text)
    if len(t) < 30:
        return False
    if any(_contains(t, w) for w in JOB_NEGATIVE):
        return False
    first_line = norm(str(text).strip().splitlines()[0] if str(text).strip() else "")[:160]
    if any(_contains(first_line, w) for w in TITLE_NEGATIVE) and not any(
            _contains(first_line, w) for w in ("وظيفه", "وظائف", "مطلوب", "توظيف", "شاغر")):
        return False
    return sum(1 for w in JOB_POSITIVE if _contains(t, w)) >= 1


def detect_tags(*texts):
    """Returns (nationality, gender, scam_risk) from rules."""
    hay = norm(" ".join(t for t in texts if t))
    nationality = "للسعوديين" if any(_contains(hay, w) for w in SAUDI_ONLY) else ""
    if any(_contains(hay, w) for w in BOTH_GENDERS):
        gender = "للجنسين"
    else:
        men = any(_contains(hay, w) for w in MEN_ONLY)
        women = any(_contains(hay, w) for w in WOMEN_ONLY)
        gender = "للجنسين" if men and women else "رجال" if men else "نساء" if women else ""
    scam = any(_contains(hay, w) for w in SCAM_WORDS)
    return nationality, gender, scam


def canonical_url(url):
    """Drop tracking params/fragments so the same post from one source dedupes."""
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
    try:
        p = urlsplit(url)
    except ValueError:
        return url
    keep = [(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith(("utm_", "ref", "tracking", "trk", "fbclid"))]
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), urlencode(keep), ""))


def merge_key(title, company, city):
    """Cross-source duplicate key; '' when the company is unknown (too weak to merge on)."""
    if not norm(company):
        return ""
    return hashlib.sha1("|".join(norm(x) for x in (title, company, city)).encode("utf-8")).hexdigest()


def job_hash(title, company, city, fallback=""):
    """Dedup key across sources: same title+company+city = same job."""
    key = "|".join(norm(x) for x in (title, company, city))
    if not norm(company):
        key += "|" + norm(fallback)[:300]
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


# "منذ 99999999 يوم" from a scraped page must not overflow date arithmetic
MAX_RELATIVE_DAYS = 3650


def parse_date(value):
    """Accepts ISO strings, RFC-822 (RSS), datetime/date. Returns YYYY-MM-DD or ''."""
    if not value:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    s = str(value).strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:  # "2026-02-30" would later crash date.fromisoformat in is_expired
            return date(*map(int, m.groups())).isoformat()
        except ValueError:
            return ""
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(s).date().isoformat()
    except (TypeError, ValueError, IndexError):
        pass
    # "Nov 5, 2026" / "21 October 2026" (SuccessFactors, Tanqeeb)
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%d %B %Y", "%d %b %Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    # Arabic relative ("منذ يوم واحد", "منذ 3 أيام", "منذ ساعتين")
    if s.startswith("منذ"):
        m = re.search(r"(\d+)", s)
        n = int(m.group(1)) if m else (2 if any(w in s for w in ("يومين", "أسبوعين", "اسبوعين", "شهرين")) else 1)
        if "ساع" in s or "دقيق" in s:
            return date.today().isoformat()
        if any(w in s for w in ("أسبوع", "اسبوع", "أسابيع", "اسابيع")):
            n *= 7
        elif "شهر" in s:
            n *= 30
        return (date.today() - timedelta(days=min(n, MAX_RELATIVE_DAYS))).isoformat()
    # Relative English ("3 days ago") from some job boards
    m = re.match(r"(\d+)\s+(minute|hour|day|week|month)s?\s+ago", s.lower())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        days = {"minute": 0, "hour": 0, "day": n, "week": 7 * n, "month": 30 * n}[unit]
        return (date.today() - timedelta(days=min(days, MAX_RELATIVE_DAYS))).isoformat()
    return ""


def first_url(text):
    m = URL_RE.search(text or "")
    return m.group(0).rstrip(".,،") if m else ""


def make_job(*, title, source, url="", company="", city="", specialty="", skills=None,
             contract_type="", posted_date="", raw_text="", description="", salary=None,
             is_training=None, requirements=None):
    """Build one normalized job record. Returns None if it is unusable."""
    title = clean_title(title)
    if len(title) < 3:
        return None
    company = one_line(company, 100)
    raw_text = strip_phones(clean_text(raw_text, 4000))
    description = strip_phones(clean_text(description or raw_text, 3000))
    city = canonical_city(city) or detect_city(title, city, raw_text[:600])
    specialty = specialty if specialty in SPECIALTIES else detect_specialty(title, raw_text[:1500])
    contract_type = contract_type if contract_type in CONTRACTS else detect_contract(title, raw_text[:1500])
    if is_training is None:
        is_training = contract_type == "تدريب"
    skills = [one_line(s, 40) for s in (skills or []) if one_line(s, 40)][:10] or detect_skills(title, raw_text[:2500])
    url = url if re.match(r"^https?://", url or "") else first_url(raw_text)
    # Primary identity: source + canonical URL; fallback: content hash
    if url:
        h = hashlib.sha1((source + "|" + canonical_url(url)).encode("utf-8")).hexdigest()
    else:
        h = job_hash(title, company, city, fallback=raw_text)
    nationality, gender, scam = detect_tags(title, raw_text[:2500])
    return {
        "id": "c" + h[:12],
        "title": title,
        "company": company or "جهة غير مذكورة",
        "city": city or "غير محدد",
        "specialty": specialty,
        "skills": skills,
        "contract_type": contract_type,
        "url": url,
        "source": source,
        "posted_date": parse_date(posted_date) or date.today().isoformat(),
        "salary": one_line(salary, 60) if salary else None,
        "is_training": bool(is_training),
        "description": description,
        "requirements": [one_line(r, 200) for r in (requirements or []) if one_line(r, 200)][:12],
        "raw_text": raw_text,
        "hash": h,
        "merge_key": merge_key(title, company, city),
        "nationality": nationality,
        "gender": gender,
        "scam_risk": scam,
        "is_demo": False,
        "collected_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def is_expired(job, max_age_days):
    d = parse_date(job.get("posted_date")) or parse_date(job.get("collected_at"))
    if not d:
        return False
    return (date.today() - date.fromisoformat(d)).days > max_age_days
