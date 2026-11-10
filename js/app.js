/* RIZQ — single-page app core + job-seeker views (vanilla JS, hash router).
   RIZQ is a free relay of job news: every job links to its original source,
   and RIZQ never receives applications. The "post a job" form and the admin
   dashboard live in js/portal.js and register their routes through window.Rizq. */
'use strict';

(function () {
  // ---------- Config ----------
  const PROFILE_KEY = 'rizq_profile';
  const MAX_CV_BYTES = 2 * 1024 * 1024;
  const MAX_CV_TEXT = 20000;
  const MAX_SKILLS = 20;
  const CV_EXT = ['pdf', 'doc', 'docx', 'txt'];
  const REMOTE = 'عن بعد';
  const SAUDI = 'للسعوديين';
  const SUBMIT_NEW = 'اشترك في التنبيهات';
  const SUBMIT_EDIT = 'حفظ التعديلات';
  // Invisible bidi control chars: stripped from user input (prevents text spoofing)
  const BIDI_RE = /[\u200E\u200F\u202A-\u202E\u2066-\u2069]/g;

  // Same groups as backend/normalize.py SPECIALTY_GROUPS (close to LinkedIn's job functions)
  const SPECIALTY_GROUPS = [
      [
          "التقنية",
          [
              "تطوير البرمجيات",
              "تقنية المعلومات والدعم الفني",
              "الأمن السيبراني",
              "البيانات والذكاء الاصطناعي"
          ]
      ],
      [
          "الهندسة",
          [
              "الهندسة المدنية والمعمارية",
              "الهندسة الكهربائية والإلكترونية",
              "الهندسة الميكانيكية والصناعية",
              "هندسة البترول والكيمياء والتعدين",
              "الصيانة والفنيون",
              "السلامة والصحة المهنية"
          ]
      ],
      [
          "الصحة",
          [
              "الطب وطب الأسنان",
              "التمريض",
              "الصيدلة",
              "المختبرات والأشعة والتأهيل"
          ]
      ],
      [
          "المال والأعمال",
          [
              "المحاسبة والمراجعة",
              "المالية والبنوك والاستثمار",
              "الاستشارات",
              "تطوير الأعمال",
              "إدارة المشاريع",
              "إدارة المنتجات",
              "العمليات",
              "الجودة",
              "المشتريات",
              "القانون",
              "العقار"
          ]
      ],
      [
          "المبيعات والتسويق",
          [
              "المبيعات",
              "التسويق والإعلان",
              "خدمة العملاء",
              "الإعلام والاتصال",
              "التصميم والفنون"
          ]
      ],
      [
          "الإدارة والموارد البشرية",
          [
              "الموارد البشرية",
              "الإدارة والسكرتارية"
          ]
      ],
      [
          "التعليم والبحث",
          [
              "التعليم والتدريب",
              "البحث العلمي"
          ]
      ],
      [
          "الخدمات والتشغيل",
          [
              "اللوجستيات وسلاسل الإمداد",
              "السائقون والنقل",
              "المطاعم والضيافة والسياحة",
              "الطيران",
              "الخدمات الاجتماعية",
              "الوظائف العسكرية والأمنية"
          ]
      ]
  ];
  const SPECIALTIES = [].concat.apply([], SPECIALTY_GROUPS.map((g) => g[1]));
  const SPECIALTY_GROUP = {};
  SPECIALTY_GROUPS.forEach((g) => g[1].forEach((sp) => { SPECIALTY_GROUP[sp] = g[0]; }));
  // Older names (before 2026-11-07) -> the new specialties they cover
  const LEGACY_SPECIALTIES = {
      "تقنية المعلومات": [
          "تطوير البرمجيات",
          "تقنية المعلومات والدعم الفني"
      ],
      "الهندسة": [
          "الهندسة المدنية والمعمارية",
          "الهندسة الكهربائية والإلكترونية",
          "الهندسة الميكانيكية والصناعية",
          "هندسة البترول والكيمياء والتعدين",
          "الصيانة والفنيون",
          "السلامة والصحة المهنية"
      ],
      "الصحة والتمريض": [
          "الطب وطب الأسنان",
          "التمريض",
          "المختبرات والأشعة والتأهيل"
      ],
      "المحاسبة والمالية": [
          "المحاسبة والمراجعة",
          "المالية والبنوك والاستثمار"
      ],
      "المبيعات والتسويق": [
          "المبيعات",
          "التسويق والإعلان",
          "تطوير الأعمال"
      ],
      "الإدارة": [
          "الإدارة والسكرتارية",
          "إدارة المشاريع",
          "العمليات"
      ],
      "المطاعم والضيافة": [
          "المطاعم والضيافة والسياحة"
      ],
      "اللوجستيات وسلاسل الإمداد": [
          "اللوجستيات وسلاسل الإمداد",
          "المشتريات",
          "السائقون والنقل"
      ]
  };
  function specialtiesFor(value) {
    if (SPECIALTY_GROUP[value]) return [value];
    // before groups: "المبيعات والتسويق" is both an old name and a group (same order as normalize.py)
    if (LEGACY_SPECIALTIES[value]) return LEGACY_SPECIALTIES[value].slice();
    const g = SPECIALTY_GROUPS.find((x) => x[0] === value);
    return g ? g[1].slice() : (LEGACY_SPECIALTIES[value] || []).slice();
  }
  // Specialty score: 50 for one the user chose (or covered by an older name), 30 for the same group
  function specialtyScore(jobSpec, userSpec) {
    if (!jobSpec || !userSpec) return 0;
    if (norm(jobSpec) === norm(userSpec)) return 50;
    const mine = specialtiesFor(userSpec);
    if (mine.indexOf(jobSpec) !== -1) return 50;
    const g = SPECIALTY_GROUP[jobSpec];
    return g && mine.some((m) => SPECIALTY_GROUP[m] === g) ? 30 : 0;
  }
  // <option>s grouped like LinkedIn's job functions
  function specialtyOptions(selected, placeholder, only) {
    let html = placeholder != null ? '<option value="">' + esc(placeholder) + '</option>' : '';
    SPECIALTY_GROUPS.forEach((g) => {
      const items = only ? g[1].filter((sp) => only.has(sp)) : g[1];
      if (!items.length) return;
      html += '<optgroup label="' + esc(g[0]) + '">' + optionList(items, selected) + '</optgroup>';
    });
    if (only && only.has('أخرى')) html += optionList(['أخرى'], selected);
    return html;
  }

  const CITIES = [
    'الرياض', 'جدة', 'مكة المكرمة', 'المدينة المنورة', 'الدمام', 'الخبر', 'الظهران', 'الجبيل',
    'الأحساء', 'القطيف', 'القصيم', 'الطائف', 'تبوك', 'أبها', 'خميس مشيط', 'حائل', 'جازان',
    'نجران', 'ينبع', 'الباحة', 'الجوف', 'عرعر', 'عن بعد'
  ];

  const CONTRACTS = ['دوام كامل', 'دوام جزئي', 'عقد مؤقت', 'تدريب'];

  const SKILL_SUGGESTIONS = {
      "تطوير البرمجيات": [
          "JavaScript",
          "Python",
          "Java",
          "SQL",
          "React",
          "Git"
      ],
      "تقنية المعلومات والدعم الفني": [
          "الشبكات",
          "Linux",
          "Windows Server",
          "الدعم الفني",
          "CCNA"
      ],
      "الأمن السيبراني": [
          "أمن الشبكات",
          "SIEM",
          "اختبار الاختراق",
          "Linux",
          "ISO 27001",
          "Python"
      ],
      "البيانات والذكاء الاصطناعي": [
          "Python",
          "SQL",
          "Power BI",
          "Excel",
          "تعلم الآلة",
          "تحليل البيانات"
      ],
      "الهندسة المدنية والمعمارية": [
          "AutoCAD",
          "Revit",
          "Primavera",
          "حصر الكميات",
          "الإشراف على المواقع"
      ],
      "الهندسة الكهربائية والإلكترونية": [
          "AutoCAD",
          "PLC",
          "SCADA",
          "أنظمة القوى",
          "السلامة المهنية"
      ],
      "الهندسة الميكانيكية والصناعية": [
          "AutoCAD",
          "SolidWorks",
          "HVAC",
          "الصيانة",
          "Lean"
      ],
      "هندسة البترول والكيمياء والتعدين": [
          "هندسة المكامن",
          "الحفر",
          "Aspen HYSYS",
          "السلامة المهنية"
      ],
      "الصيانة والفنيون": [
          "الصيانة",
          "الكهرباء",
          "التكييف",
          "السلامة المهنية",
          "رخصة قيادة"
      ],
      "السلامة والصحة المهنية": [
          "NEBOSH",
          "OSHA",
          "تقييم المخاطر",
          "الإسعافات الأولية"
      ],
      "الطب وطب الأسنان": [
          "BLS",
          "ACLS",
          "رعاية المرضى",
          "السجلات الطبية",
          "التصنيف المهني"
      ],
      "التمريض": [
          "رعاية المرضى",
          "BLS",
          "التمريض",
          "السجلات الطبية",
          "التواصل"
      ],
      "الصيدلة": [
          "صرف الأدوية",
          "الاستشارات الدوائية",
          "إدارة المخزون",
          "خدمة العملاء"
      ],
      "المختبرات والأشعة والتأهيل": [
          "التحاليل المخبرية",
          "الأشعة",
          "العلاج الطبيعي",
          "BLS"
      ],
      "المحاسبة والمراجعة": [
          "Excel",
          "SAP",
          "IFRS",
          "إعداد التقارير المالية",
          "الزكاة والضريبة"
      ],
      "المالية والبنوك والاستثمار": [
          "التحليل المالي",
          "Excel",
          "إدارة المخاطر",
          "الالتزام",
          "CFA"
      ],
      "إدارة المشاريع": [
          "PMP",
          "Primavera",
          "MS Project",
          "التخطيط",
          "إدارة المخاطر"
      ],
      "المبيعات": [
          "التفاوض",
          "CRM",
          "خدمة العملاء",
          "التواصل",
          "إدارة الحسابات"
      ],
      "التسويق والإعلان": [
          "التسويق الرقمي",
          "إدارة المحتوى",
          "SEO",
          "وسائل التواصل الاجتماعي",
          "التحليلات"
      ],
      "الموارد البشرية": [
          "التوظيف",
          "نظام العمل السعودي",
          "الرواتب",
          "Excel",
          "المقابلات"
      ],
      "خدمة العملاء": [
          "التواصل",
          "حل المشكلات",
          "اللغة الإنجليزية",
          "CRM",
          "العمل تحت الضغط"
      ],
      "التعليم والتدريب": [
          "التدريس",
          "إعداد المناهج",
          "التعليم الإلكتروني",
          "إدارة الصف"
      ],
      "اللوجستيات وسلاسل الإمداد": [
          "إدارة المخزون",
          "سلاسل الإمداد",
          "SAP",
          "التخطيط"
      ],
      "الإدارة والسكرتارية": [
          "Excel",
          "التنسيق",
          "إدارة الوقت",
          "التواصل",
          "إدخال البيانات"
      ],
      "المطاعم والضيافة والسياحة": [
          "خدمة العملاء",
          "العمل الجماعي",
          "الكاشير",
          "سلامة الغذاء"
      ],
      "الوظائف العسكرية والأمنية": [
          "اللياقة البدنية",
          "الانضباط",
          "رخصة قيادة",
          "العمل الجماعي"
      ],
      "التصميم والفنون": [
          "Figma",
          "Photoshop",
          "Illustrator",
          "تجربة المستخدم",
          "الهوية البصرية"
      ],
      "القانون": [
          "صياغة العقود",
          "الأنظمة السعودية",
          "التقاضي",
          "الامتثال"
      ],
      "المشتريات": [
          "SAP",
          "التفاوض",
          "إدارة الموردين",
          "العقود"
      ],
      "الجودة": [
          "ISO 9001",
          "ضبط الجودة",
          "التدقيق",
          "Six Sigma"
      ]
  };

  const GENDER_LABEL = { 'رجال': 'للرجال فقط', 'نساء': 'للنساء فقط', 'للجنسين': 'للجنسين' };
  function genderLabel(g) { return GENDER_LABEL[g] || g; }

  const REPORT_REASONS = ['احتيال أو طلب رسوم', 'الوظيفة منتهية', 'معلومات خاطئة', 'محتوى غير لائق', 'أخرى'];

  // Minimal inline icons (stroke = currentColor)
  const S = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"';
  const ICON = {
    pin: S + '><path d="M12 21s-7-6.1-7-11.5A7 7 0 0 1 19 9.5C19 14.9 12 21 12 21z"/><circle cx="12" cy="9.5" r="2.5"/></svg>',
    bag: S + '><rect x="3" y="7" width="18" height="13" rx="2"/><path d="M8 7V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>',
    clock: S + '><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
    tag: S + '><path d="M20.6 13.4 13.4 20.6a2 2 0 0 1-2.8 0L3 13V3h10l7.6 7.6a2 2 0 0 1 0 2.8z"/><circle cx="7.5" cy="7.5" r="1.5"/></svg>',
    money: S + '><rect x="2" y="6" width="20" height="12" rx="2"/><circle cx="12" cy="12" r="2.5"/></svg>',
    search: S + '><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>',
    upload: S + '><path d="M12 16V4"/><path d="m7 9 5-5 5 5"/><path d="M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3"/></svg>',
    check: S.replace('stroke-width="2"', 'stroke-width="2.5"') + '><path d="M20 6 9 17l-5-5"/></svg>',
    filter: S + ' width="18" height="18"><path d="M3 5h18l-7 8v6l-4 2v-8z"/></svg>',
    arrow: S + ' width="18" height="18"><path d="M5 12h14"/><path d="m13 6 6 6-6 6"/></svg>',
    arrowFwd: S + ' width="18" height="18"><path d="M19 12H5"/><path d="m11 6-6 6 6 6"/></svg>',
    external: S + ' width="18" height="18"><path d="M14 4h6v6"/><path d="M10 14 20 4"/><path d="M19 13v6a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h6"/></svg>',
    send: S + ' width="18" height="18"><path d="m22 2-7 20-4-9-9-4z"/><path d="M22 2 11 13"/></svg>',
    shield: S + ' width="20" height="20"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>',
    flag: S + ' width="16" height="16"><path d="M4 22V4"/><path d="M4 4h13l-2 4 2 4H4"/></svg>'
  };

  function defaultFilters(sort) {
    return { q: '', specialty: '', city: '', contract: '', source: '', nationality: '', gender: '', sort: sort || '' };
  }

  // ---------- State ----------
  const state = {
    jobs: [],
    demo: false,
    settings: { support: { enabled: true, text: '', links: [] }, telegram_bot: false },
    loaded: false,
    loadError: null,
    profile: loadProfile(),
    filters: defaultFilters(),
    view: '',
    prevView: '',
    jobsScroll: 0
  };

  const main = document.getElementById('main');
  const searchIndex = new Map();

  // ---------- Helpers ----------
  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, (c) => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
  }

  // Only allow http(s), plus mailto when allowMailto (direct posts). Blocks javascript:, data:, etc.
  function safeUrl(url, allowMailto) {
    // An empty value would resolve to this page's own address: treat it as "no link"
    if (typeof url !== 'string' || !url.trim()) return '';
    try {
      const u = new URL(url, location.href);
      if (u.protocol === 'https:' || u.protocol === 'http:') return u.href;
      if (allowMailto && u.protocol === 'mailto:') return u.href;
    } catch (e) { /* invalid */ }
    return '';
  }

  // Arabic-aware normalization (mirrors backend/normalize.py)
  function norm(s) {
    return String(s || '')
      .toLowerCase()
      .replace(/[ً-ْـ]/g, '')
      .replace(/[أإآ]/g, 'ا')
      .replace(/ة/g, 'ه')
      .replace(/ى/g, 'ي')
      .replace(/ئ/g, 'ي')
      .replace(/ؤ/g, 'و')
      .replace(/[٠-٩]/g, (d) => String(d.charCodeAt(0) - 0x0660))
      .replace(/\s+/g, ' ')
      .trim();
  }

  function unique(arr) {
    return Array.from(new Set(arr.filter(Boolean))).sort((a, b) => a.localeCompare(b, 'ar'));
  }

  function companyInitial(name) {
    const clean = String(name || '').replace(/^(شركة|مؤسسة|مجموعة|مستشفى|مركز|جهة)\s+/, '').trim();
    return clean.charAt(0) || 'ر';
  }

  function parseDate(s) {
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(s || ''));
    return m ? new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])) : null;
  }

  // Relative Arabic date with correct number agreement
  function relativeDate(s) {
    const d = parseDate(s);
    if (!d) return '';
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const days = Math.round((today - d) / 86400000);
    if (days <= 0) return 'اليوم';
    if (days === 1) return 'أمس';
    if (days === 2) return 'قبل يومين';
    if (days <= 10) return 'قبل ' + days + ' أيام';
    if (days <= 30) return 'قبل ' + days + ' يوماً';
    return fullDate(s);
  }

  function fullDate(s) {
    const d = parseDate(s);
    if (!d) return '';
    return d.toLocaleDateString('ar-SA-u-ca-gregory-nu-latn', { year: 'numeric', month: 'long', day: 'numeric' });
  }

  // Arabic counted noun. accusative=true for object position ("وجدنا لك وظيفتين")
  function jobsWord(n, accusative) {
    if (n === 0) return 'لا توجد وظائف';
    if (n === 1) return 'وظيفة واحدة';
    if (n === 2) return accusative ? 'وظيفتين' : 'وظيفتان';
    if (n <= 10) return n + ' وظائف';
    return n + ' وظيفة';
  }

  function cleanText(v) {
    return String(v || '').replace(BIDI_RE, '').replace(/\s+/g, ' ').trim();
  }

  function setTitle(t) {
    document.title = t ? t + ' | رزق' : 'رزق | أخبار الوظائف من كل مكان، مجاناً';
  }

  let toastTimer = 0;
  function toast(msg) {
    const el = document.getElementById('toast');
    el.textContent = msg;
    el.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove('show'), 3600);
  }

  function debounce(fn, ms) {
    let t = 0;
    return function () {
      const args = arguments;
      clearTimeout(t);
      t = setTimeout(() => fn.apply(null, args), ms);
    };
  }

  function optionList(values, selected, placeholder) {
    let html = placeholder != null ? '<option value="">' + esc(placeholder) + '</option>' : '';
    values.forEach((v) => {
      const value = Array.isArray(v) ? v[0] : v;
      const label = Array.isArray(v) ? v[1] : v;
      html += '<option value="' + esc(value) + '"' + (value === selected ? ' selected' : '') + '>' + esc(label) + '</option>';
    });
    return html;
  }

  function busy(btn, on, label) {
    if (!btn) return;
    if (on) { btn.dataset.label = btn.textContent; btn.textContent = label || 'جارٍ الحفظ...'; }
    else { btn.textContent = btn.dataset.label || btn.textContent; }
    btn.disabled = !!on;
  }

  // ---------- API (all server I/O goes through here) ----------
  async function api(method, path, body, timeoutMs) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs || 20000);
    const opts = { method: method, credentials: 'same-origin', cache: 'no-store', signal: ctrl.signal, headers: {} };
    if (method !== 'GET') {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body || {});
    }
    try {
      const res = await fetch(path, opts);
      const data = await res.json().catch(() => ({}));
      return { ok: res.ok && data.ok !== false, status: res.status, data: data };
    } catch (e) {
      return { ok: false, status: 0, data: { error: 'تعذّر الاتصال بالخادم. تأكد أن رزق يعمل (start.bat).' } };
    } finally {
      clearTimeout(timer);
    }
  }

  // ---------- Profile (local copy of the seeker's own profile) ----------
  function loadProfile() {
    try {
      const p = JSON.parse(localStorage.getItem(PROFILE_KEY) || 'null');
      return p && typeof p === 'object' && p.name ? p : null;
    } catch (e) {
      return null;
    }
  }

  function saveProfile(p) {
    state.profile = p;
    matchCache.clear();
    try { localStorage.setItem(PROFILE_KEY, JSON.stringify(p)); } catch (e) { /* storage full or blocked */ }
    updateNav();
  }

  function clearProfile() {
    state.profile = null;
    matchCache.clear();
    try {
      localStorage.removeItem(PROFILE_KEY);
      localStorage.removeItem('rizq_users_local'); // legacy key from the first local version
    } catch (e) { /* ignore */ }
    if (state.filters.sort === 'match') state.filters.sort = '';
    updateNav();
  }

  function profileAuth() {
    const p = state.profile;
    return p && p.id && p.edit_token ? { id: p.id, edit_token: p.edit_token } : null;
  }

  // ---------- Matching (same scoring as backend/matching.py) ----------
  const matchCache = new Map();

  function matchScore(job) {
    const p = state.profile;
    if (!p) return null;
    const key = job.id + '|' + (p.updated_at || '');
    if (matchCache.has(key)) return matchCache.get(key);

    const mine = (p.skills || []).concat(p.cv_skills || []).map(norm).filter(Boolean);
    const cv = norm(p.cv_text || '');
    const hits = [];
    (job.skills || []).forEach((skill) => {
      const s = norm(skill);
      const ok = mine.some((x) => x === s || (x.length > 2 && s.length > 2 && (s.includes(x) || x.includes(s)))) ||
        (s.length > 2 && cv.includes(s));
      if (ok) hits.push(skill);
    });

    let score = 0;
    score += specialtyScore(job.specialty, p.specialty);
    if (job.skills && job.skills.length) score += Math.round(40 * hits.length / job.skills.length);
    if (job.city === p.city || job.city === REMOTE) score += 10;

    const result = { score: Math.min(100, score), hits: hits };
    matchCache.set(key, result);
    return result;
  }

  function matchClass(score) {
    return score >= 70 ? 'match-high' : score >= 40 ? 'match-mid' : 'match-low';
  }

  // ---------- Data ----------
  function validJob(j) {
    return j && typeof j === 'object' && typeof j.id === 'string' && /^[A-Za-z0-9_-]+$/.test(j.id) && j.title;
  }

  async function loadData() {
    const [jobsRes, settingsRes] = await Promise.all([api('GET', '/api/jobs'), api('GET', '/api/settings')]);
    if (!jobsRes.ok) {
      state.loadError = jobsRes.data.error || 'HTTP ' + jobsRes.status;
    } else {
      state.demo = !!jobsRes.data.demo;
      state.jobs = (jobsRes.data.jobs || []).filter(validJob).map((j) => Object.assign({}, j, {
        skills: Array.isArray(j.skills) ? j.skills : []
      }));
      searchIndex.clear();
      state.jobs.forEach((j) => {
        searchIndex.set(j.id, norm([j.title, j.company, j.city, j.specialty, j.source, j.contract_type, j.skills.join(' ')].join(' ')));
      });
    }
    if (settingsRes.ok) state.settings = settingsRes.data;
    const footerSupport = document.getElementById('footer-support');
    if (footerSupport) footerSupport.hidden = !(state.settings.support && state.settings.support.enabled !== false);
    state.loaded = true;
  }

  function findJob(id) {
    return state.jobs.find((j) => j.id === id) || null;
  }

  // ---------- Router ----------
  const ROUTES = [];

  function addRoute(re, view, render, navKey) {
    ROUTES.push({ re: re, view: view, render: render, navKey: navKey || view });
  }

  function currentPath() {
    let raw = location.hash.replace(/^#/, '');
    try { raw = decodeURIComponent(raw); } catch (e) { /* keep raw */ }
    return raw.replace(/\/+$/, '') || '/';
  }

  let currentNavKey = '';

  function router() {
    if (!state.loaded) return;
    if (state.view === 'jobs') state.jobsScroll = window.scrollY;
    closeFilters();
    state.prevView = state.view;

    const path = currentPath();
    const r = ROUTES.find((x) => x.re.test(path));
    if (state.loadError && (!r || !r.standalone)) {
      state.view = 'error';
      currentNavKey = '';
      renderLoadError();
    } else if (r) {
      state.view = r.view;
      currentNavKey = r.navKey;
      r.render.apply(null, (r.re.exec(path) || []).slice(1));
    } else {
      state.view = 'notfound';
      currentNavKey = '';
      renderNotFound();
    }

    updateNav();
    const top = (state.view === 'jobs' && state.prevView === 'job') ? state.jobsScroll : 0;
    window.scrollTo({ top: top, behavior: 'instant' });
    // Move focus to content for screen readers after navigation (not on first load)
    if (state.prevView) main.focus({ preventScroll: true });
  }

  function updateNav() {
    document.querySelectorAll('[data-nav]').forEach((a) => {
      const active = a.getAttribute('data-nav') === currentNavKey;
      a.classList.toggle('active', active);
      if (active) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
      // On narrow screens the single "للطلاب" link stands for both student pages
      a.classList.toggle('active-narrow', a.classList.contains('nav-students') && currentNavKey === 'scholarships');
    });
    const reg = document.getElementById('nav-register');
    if (reg) {
      reg.querySelector('.lbl-long').textContent = state.profile ? 'تنبيهاتي' : 'اشترك في التنبيهات';
      reg.querySelector('.lbl-short').textContent = state.profile ? 'تنبيهاتي' : 'اشترك';
    }
  }

  // ---------- Shared pieces ----------
  function tagChips(j) {
    let html = '';
    if (j.nationality) html += '<li class="chip chip-tag">' + esc(j.nationality) + '</li>';
    if (j.gender) html += '<li class="chip chip-tag">' + esc(genderLabel(j.gender)) + '</li>';
    return html;
  }

  function jobCard(j) {
    const m = matchScore(j);
    const skills = j.skills.slice(0, 4);
    const more = j.skills.length - skills.length;
    const hitSet = new Set(m ? m.hits : []);
    const chips = tagChips(j) +
      skills.map((s) => '<li class="chip' + (hitSet.has(s) ? ' hit' : '') + '">' + esc(s) + '</li>').join('') +
      (more > 0 ? '<li class="chip chip-more">+' + more + '</li>' : '');
    return '' +
      '<a class="job-card' + (j.direct ? ' direct' : '') + '" href="#/job/' + encodeURIComponent(j.id) + '">' +
        '<div class="job-card-top">' +
          '<span class="company-avatar" aria-hidden="true">' + esc(companyInitial(j.company)) + '</span>' +
          '<div class="job-card-titles">' +
            '<h3><bdi>' + esc(j.title) + '</bdi></h3>' +
            '<p class="company"><bdi>' + esc(j.company) + '</bdi></p>' +
          '</div>' +
          (m ? '<span class="match ' + matchClass(m.score) + '">توافق ' + m.score + '%</span>' : '') +
        '</div>' +
        '<ul class="meta">' +
          '<li>' + ICON.pin + '<span>' + esc(j.city) + '</span></li>' +
          '<li>' + ICON.bag + '<span>' + esc(j.contract_type) + '</span></li>' +
          '<li>' + ICON.clock + '<span>' + esc(relativeDate(j.posted_date)) + '</span></li>' +
        '</ul>' +
        (chips ? '<ul class="chips" aria-label="المهارات والشروط">' + chips + '</ul>' : '') +
        '<div class="job-card-foot">' +
          (j.direct ? '<span class="badge badge-direct">إعلان مباشر</span>' : '<span>المصدر: <bdi>' + esc(j.source) + '</bdi></span>') +
          '<span class="badges">' +
            (j.is_training ? '<span class="badge badge-training">برنامج تدريب</span>' : '') +
            (j.scam_risk ? '<span class="badge badge-warn">تحقّق قبل التقديم</span>' : '') +
          '</span>' +
        '</div>' +
      '</a>';
  }

  function demoNotice() {
    return state.demo
      ? '<div class="notice notice-info"><p>هذه وظائف تجريبية للعرض. ستظهر الوظائف الحقيقية تلقائياً بعد أول عملية جمع من المصادر.</p></div>'
      : '';
  }

  // ---------- View: Home ----------
  // Sources we relay from, most jobs first (direct posts are not a "source site")
  function topSources(limit) {
    const counts = new Map();
    state.jobs.forEach((j) => {
      if (j.direct || !j.source) return;
      counts.set(j.source, (counts.get(j.source) || 0) + 1);
    });
    return Array.from(counts.entries()).sort((a, b) => b[1] - a[1]).slice(0, limit).map((x) => x[0]);
  }

  // Arabic counted noun: 1-2 and 100/101/102/200... take the genitive singular,
  // 3-10 (also 103-110) the plural, 11-99 (also 111-199) the accusative singular (e.g. 116 مصدراً)
  function arCount(n, singular, plural, tamyiz) {
    const r = n % 100;
    if (r >= 3 && r <= 10) return plural;
    if (r >= 11) return tamyiz;
    return singular; // 0, 1, 2 (including 100, 101, 102, 1000)
  }

  function renderHome() {
    setTitle('');
    const sources = unique(state.jobs.map((j) => j.source));
    const cities = unique(state.jobs.map((j) => j.city).filter((c) => c !== REMOTE && c !== 'غير محدد'));
    const latest = state.jobs.slice(0, 3);
    const shown = topSources(12);
    const relayFrom = shown.length ? shown : ['جدارات', 'LinkedIn', 'قنوات تيليجرام', 'مواقع الوظائف العربية', 'بوابات الشركات الكبرى'];
    const subscribeLabel = state.profile ? 'تنبيهاتي' : 'اشترك في التنبيهات';

    main.innerHTML = '' +
      '<section class="hero">' +
        '<div class="container hero-inner">' +
          '<h1 class="hero-title"><img class="hero-banner" src="assets/rizq-logo.png" width="621" height="283" ' +
            'alt="رزق، وظيفتك .. مستقبلك" fetchpriority="high" decoding="async"></h1>' +
          '<p class="lead">نجمع لك أخبار الوظائف من كل مكان، مجاناً، لوجه الله ومساعدة للباحثين.</p>' +
          '<p class="hero-sub">مبادرة فردية، بدون إعلانات ولا صفحات مزعجة.</p>' +
          '<div class="hero-actions">' +
            '<a href="#/register" class="btn btn-gold btn-lg">' + ICON.send + subscribeLabel + '</a>' +
            '<a href="#/jobs" class="btn btn-ghost btn-lg">تصفّح الوظائف</a>' +
          '</div>' +
          (state.jobs.length ? '<p class="hero-count">' +
            '<strong>' + state.jobs.length.toLocaleString('en') + '</strong> ' + arCount(state.jobs.length, 'وظيفة', 'وظائف', 'وظيفة') +
            ' متاحة الآن، من <strong>' + sources.length + '</strong> ' + arCount(sources.length, 'مصدر', 'مصادر', 'مصدراً') +
            ' في <strong>' + cities.length + '</strong> ' + arCount(cities.length, 'مدينة', 'مدن', 'مدينة') +
          '</p>' : '') +
        '</div>' +
      '</section>' +

      '<section class="section">' +
        '<div class="container">' +
          '<div class="section-head"><h2>ناقل أمين</h2><p>ننقل الخبر كما هو، والتقديم يبقى عند صاحبه</p></div>' +
          '<ol class="steps">' +
            '<li class="step"><span class="step-num">1</span><h3>نجمع</h3><p>نتابع البوابات الحكومية ومواقع الوظائف وقنوات تيليجرام باستمرار، ونحذف المكرر.</p></li>' +
            '<li class="step"><span class="step-num">2</span><h3>ننقل بأمانة</h3><p>كل وظيفة معها رابط إعلانها الأصلي واسم مصدرها. لا نستقبل طلبات التوظيف، فالتقديم يتم عند الجهة نفسها.</p></li>' +
            '<li class="step"><span class="step-num">3</span><h3>تصلك أولاً بأول</h3><p>اكتب تخصصك ومهاراتك واربط تيليجرام، وتصلك كل وظيفة جديدة تناسبك في مكان واحد.</p></li>' +
          '</ol>' +
        '</div>' +
      '</section>' +

      (latest.length ? '<section class="section">' +
        '<div class="container">' +
          '<div class="section-head"><h2>أحدث الوظائف</h2><p>آخر ما نقلناه من المصادر</p></div>' +
          '<div class="card-grid">' + latest.map(jobCard).join('') + '</div>' +
          '<div class="section-foot"><a href="#/jobs" class="btn btn-ghost">كل الوظائف ' + ICON.arrowFwd + '</a></div>' +
        '</div>' +
      '</section>' : '') +

      '<section class="section">' +
        '<div class="container">' +
          '<div class="section-head"><h2>للطلاب</h2><p>برامج من الحكومة والشركات السعودية والعالمية في المملكة، بمواعيدها وشروطها وروابطها الرسمية</p></div>' +
          '<div class="students-links">' +
            '<a href="#/coop"><h3>التدريب التعاوني</h3><p>متى يفتح تدريب كل جهة، ولمن، وبأي شروط.</p></a>' +
            '<a href="#/scholarships"><h3>الابتعاث</h3><p>برامج الابتعاث والمنح داخل المملكة وخارجها وشروط التقديم.</p></a>' +
          '</div>' +
        '</div>' +
      '</section>' +

      '<section class="section">' +
        '<div class="container">' +
          '<div class="section-head"><h2>ننقل لك من</h2><p>مصادر عامة نتابعها، وكل وظيفة تحمل اسم مصدرها ورابطه</p></div>' +
          '<ul class="sources">' + relayFrom.map((s) => '<li>' + esc(s) + '</li>').join('') + '</ul>' +
        '</div>' +
      '</section>' +

      '<div class="container">' +
        '<div class="cta-band">' +
          '<h2>اجعل الوظائف تصلك</h2>' +
          '<p>اكتب اهتماماتك وتخصصك ومهاراتك، واربط تيليجرام، وتصلك كل وظيفة جديدة تناسبك. مجاناً، وتوقفها متى شئت.</p>' +
          '<a href="#/register" class="btn btn-gold btn-lg">' + ICON.send + subscribeLabel + '</a>' +
        '</div>' +
        '<p class="post-hint">عندك وظيفة شاغرة؟ <a href="#/post">أعلن عنها مجاناً</a>، ونراجعها قبل نشرها.</p>' +
      '</div>';
  }

  // ---------- View: Jobs list ----------
  function filteredJobs() {
    const f = state.filters;
    const q = norm(f.q);
    const terms = q ? q.split(' ') : [];
    let list = state.jobs.filter((j) =>
      (!f.specialty || j.specialty === f.specialty) &&
      (!f.city || j.city === f.city) &&
      (!f.contract || j.contract_type === f.contract) &&
      (!f.source || j.source === f.source) &&
      (!f.nationality || (f.nationality === 'saudi' ? j.nationality === SAUDI : j.nationality !== SAUDI)) &&
      (!f.gender || (f.gender === 'men' ? j.gender !== 'نساء' : j.gender !== 'رجال')) &&
      (!terms.length || terms.every((t) => (searchIndex.get(j.id) || '').includes(t)))
    );
    if (f.sort === 'match' && state.profile) {
      list = list.slice().sort((a, b) => matchScore(b).score - matchScore(a).score);
    }
    return list;
  }

  function activeFilterCount() {
    const f = state.filters;
    return ['specialty', 'city', 'contract', 'source', 'nationality', 'gender'].filter((k) => f[k]).length;
  }

  function renderJobs() {
    setTitle('الوظائف');
    const f = state.filters;
    if (!f.sort) f.sort = state.profile ? 'match' : 'new';
    if (f.sort === 'match' && !state.profile) f.sort = 'new';

    const specialties = unique(state.jobs.map((j) => j.specialty));
    const cities = unique(state.jobs.map((j) => j.city));
    const contracts = unique(state.jobs.map((j) => j.contract_type));
    const sources = unique(state.jobs.map((j) => j.source));
    const p = state.profile;

    main.innerHTML = '' +
      '<section class="page container">' +
        '<div class="page-head">' +
          '<h1>الوظائف</h1>' +
          '<p>وظائف نقلناها من مصادرها، ومع كل وظيفة رابط إعلانها الأصلي. ابحث بالمسمى أو الشركة، وصفِّ النتائج كما تحب.</p>' +
        '</div>' +
        demoNotice() +
        (p
          ? '<div class="notice"><p>أهلاً <strong>' + esc(p.name) + '</strong>، رتّبنا لك الوظائف حسب توافقها مع تخصصك (' + esc(p.specialty) + ') ومهاراتك.</p><a href="#/register" class="btn btn-ghost btn-sm">تنبيهاتي</a></div>'
          : '<div class="notice"><p>اكتب تخصصك ومهاراتك لنرتّب لك الوظائف حسب الأنسب لك، وتصلك الجديدة منها على تيليجرام.</p><a href="#/register" class="btn btn-gold btn-sm">اشترك في التنبيهات</a></div>') +
        '<div class="jobs-layout">' +
          '<aside class="filters" id="filters" aria-label="الفلاتر">' +
            '<div class="filters-head">' +
              '<h2>الفلاتر</h2>' +
              '<button type="button" class="btn btn-ghost btn-sm filters-close" id="filters-close">عرض النتائج</button>' +
            '</div>' +
            '<div class="field"><label for="f-specialty">التخصص</label><select id="f-specialty" data-filter="specialty">' + specialtyOptions(f.specialty, 'كل التخصصات', new Set(specialties)) + '</select></div>' +
            '<div class="field"><label for="f-city">المدينة</label><select id="f-city" data-filter="city">' + optionList(cities, f.city, 'كل المدن') + '</select></div>' +
            '<div class="field"><label for="f-contract">نوع العقد</label><select id="f-contract" data-filter="contract">' + optionList(contracts, f.contract, 'كل الأنواع') + '</select></div>' +
            '<div class="field"><label for="f-nationality">الجنسية</label><select id="f-nationality" data-filter="nationality">' +
              optionList([['saudi', 'للسعوديين فقط'], ['open', 'مفتوحة للجميع']], f.nationality, 'الكل') + '</select></div>' +
            '<div class="field"><label for="f-gender">الجنس</label><select id="f-gender" data-filter="gender">' +
              optionList([['men', 'متاحة للرجال'], ['women', 'متاحة للنساء']], f.gender, 'الكل') + '</select></div>' +
            '<div class="field"><label for="f-source">المصدر</label><select id="f-source" data-filter="source">' + optionList(sources, f.source, 'كل المصادر') + '</select></div>' +
            '<button type="button" class="btn btn-ghost btn-block" id="f-reset">مسح الفلاتر</button>' +
          '</aside>' +
          '<div class="results">' +
            '<div class="search-box">' + ICON.search +
              '<label for="f-q" class="sr-only">ابحث في الوظائف</label>' +
              '<input id="f-q" type="search" placeholder="ابحث بالمسمى أو الشركة أو المهارة..." value="' + esc(f.q) + '" autocomplete="off">' +
            '</div>' +
            '<div class="results-bar">' +
              '<p class="results-count" id="results-count" aria-live="polite"></p>' +
              '<div class="results-tools">' +
                '<button type="button" class="btn btn-ghost btn-sm filters-toggle" id="filters-toggle" aria-controls="filters" aria-expanded="false">' + ICON.filter + '<span>الفلاتر</span><span class="count-badge" id="filters-count" hidden></span></button>' +
                '<label for="f-sort" class="sr-only">الترتيب</label>' +
                '<select id="f-sort">' +
                  (p ? '<option value="match"' + (f.sort === 'match' ? ' selected' : '') + '>الأنسب لي</option>' : '') +
                  '<option value="new"' + (f.sort === 'new' ? ' selected' : '') + '>الأحدث</option>' +
                '</select>' +
              '</div>' +
            '</div>' +
            '<div class="job-list" id="job-list"></div>' +
          '</div>' +
        '</div>' +
      '</section>';

    main.querySelectorAll('[data-filter]').forEach((sel) => {
      sel.addEventListener('change', () => {
        state.filters[sel.getAttribute('data-filter')] = sel.value;
        updateResults();
      });
    });
    const q = document.getElementById('f-q');
    q.addEventListener('input', debounce(() => { state.filters.q = q.value; updateResults(); }, 150));
    document.getElementById('f-sort').addEventListener('change', (e) => { state.filters.sort = e.target.value; updateResults(); });
    document.getElementById('f-reset').addEventListener('click', resetFilters);
    document.getElementById('filters-toggle').addEventListener('click', openFilters);
    document.getElementById('filters-close').addEventListener('click', closeFilters);

    updateResults();
  }

  function resetFilters() {
    state.filters = defaultFilters(state.filters.sort);
    main.querySelectorAll('[data-filter]').forEach((sel) => { sel.value = ''; });
    const q = document.getElementById('f-q');
    if (q) q.value = '';
    updateResults();
  }

  function updateResults() {
    const list = filteredJobs();
    const box = document.getElementById('job-list');
    const count = document.getElementById('results-count');
    if (!box) return;

    count.innerHTML = list.length
      ? '<strong>' + esc(jobsWord(list.length)) + '</strong>' + (list.length === state.jobs.length ? '' : ' من أصل ' + state.jobs.length)
      : 'لا توجد نتائج';

    box.innerHTML = list.length
      ? list.map(jobCard).join('')
      : '<div class="empty"><h3>لم نجد وظائف بهذه الشروط</h3><p>جرّب كلمة بحث أخرى أو خفّف الفلاتر.</p><button type="button" class="btn btn-gold" id="empty-reset">مسح الفلاتر</button></div>';

    const er = document.getElementById('empty-reset');
    if (er) er.addEventListener('click', resetFilters);

    const n = activeFilterCount();
    const badge = document.getElementById('filters-count');
    if (badge) { badge.textContent = n; badge.hidden = n === 0; }
  }

  // Mobile filter sheet
  function openFilters() {
    const panel = document.getElementById('filters');
    if (!panel) return;
    panel.classList.add('open');
    panel.setAttribute('role', 'dialog');
    panel.setAttribute('aria-modal', 'true');
    const back = document.createElement('div');
    back.className = 'backdrop';
    back.id = 'filters-backdrop';
    back.addEventListener('click', closeFilters);
    document.body.appendChild(back);
    document.getElementById('filters-toggle').setAttribute('aria-expanded', 'true');
    const first = panel.querySelector('select');
    if (first) first.focus();
  }

  function closeFilters() {
    const panel = document.getElementById('filters');
    const back = document.getElementById('filters-backdrop');
    if (back) back.remove();
    if (panel && panel.classList.contains('open')) {
      panel.classList.remove('open');
      panel.removeAttribute('role');
      panel.removeAttribute('aria-modal');
      const t = document.getElementById('filters-toggle');
      if (t) { t.setAttribute('aria-expanded', 'false'); t.focus(); }
    }
  }

  // ---------- View: Job detail ----------
  function renderJob(id) {
    const j = findJob(id);
    if (!j) {
      renderNotFound('الوظيفة غير موجودة', 'ربما انتهت الوظيفة أو أن الرابط غير صحيح.');
      return;
    }
    setTitle(j.title);
    // The list endpoint is light; load description/requirements once
    if (!j._full) {
      j._full = true;
      api('GET', '/api/jobs/' + encodeURIComponent(j.id)).then((res) => {
        if (res.ok && res.data.job && currentPath() === '/job/' + j.id) {
          Object.assign(j, res.data.job, { _full: true, _loaded: true });
          renderJob(j.id);
        } else {
          j._loaded = true;
        }
      });
    }
    const m = matchScore(j);
    const hitSet = new Set(m ? m.hits : []);
    // Original apply link (found inside the re-posting site's article) wins over the article page
    const directUrl = safeUrl(j.apply_url || '');
    // mailto: is only accepted for direct posts (the company's own apply email)
    const pageUrl = safeUrl(j.url, !!j.direct);
    const url = directUrl || pageUrl;
    const isMail = url.startsWith('mailto:');
    const paragraphs = String(j.description || '').split(/\n+/).filter(Boolean);
    const reqs = Array.isArray(j.requirements) ? j.requirements : [];
    const similar = state.jobs.filter((x) => x.id !== j.id && x.specialty === j.specialty).slice(0, 3);
    const alsoOn = Array.isArray(j.also_on) ? j.also_on : [];

    main.innerHTML = '' +
      '<section class="page container">' +
        '<a href="#/jobs" class="back-link">' + ICON.arrow + '<span>العودة إلى الوظائف</span></a>' +
        '<div class="detail-layout">' +
          '<article>' +
            '<div class="panel">' +
              '<div class="detail-head">' +
                '<span class="company-avatar" aria-hidden="true">' + esc(companyInitial(j.company)) + '</span>' +
                '<div>' +
                  '<h1><bdi>' + esc(j.title) + '</bdi></h1>' +
                  '<p class="company"><bdi>' + esc(j.company) + '</bdi></p>' +
                  '<div class="detail-badges">' +
                    (j.direct ? '<span class="badge badge-direct">إعلان مباشر</span>' : '<span class="badge badge-source">المصدر: <bdi>' + esc(j.source) + '</bdi></span>') +
                    (j.is_training ? '<span class="badge badge-training">برنامج تدريب</span>' : '') +
                    (j.nationality ? '<span class="badge badge-source">' + esc(j.nationality) + '</span>' : '') +
                    (j.gender ? '<span class="badge badge-source">' + esc(genderLabel(j.gender)) + '</span>' : '') +
                    (j.is_demo ? '<span class="badge badge-source">تجريبية</span>' : '') +
                  '</div>' +
                '</div>' +
              '</div>' +
              '<ul class="meta">' +
                '<li>' + ICON.pin + '<span>' + esc(j.city) + '</span></li>' +
                '<li>' + ICON.bag + '<span>' + esc(j.contract_type) + '</span></li>' +
                '<li>' + ICON.tag + '<span>' + esc(j.specialty) + '</span></li>' +
                (j.salary ? '<li>' + ICON.money + '<span>' + esc(j.salary) + '</span></li>' : '') +
                '<li>' + ICON.clock + '<span>' + esc(relativeDate(j.posted_date)) + '</span></li>' +
              '</ul>' +
            '</div>' +
            (j.direct ? '<div class="notice notice-info mt-12"><p>إعلان نشرته الجهة مباشرة في رزق، وراجعناه قبل نشره. تأكد دائماً من الجهة قبل مشاركة بياناتك.</p></div>' : '') +
            (j.scam_risk ? '<div class="alert alert-warn">' + ICON.shield + '<p><strong>انتبه:</strong> هذا الإعلان يذكر رسوماً أو تحويل مبالغ. جهات التوظيف الموثوقة لا تطلب أي رسوم من المتقدمين.</p></div>' : '') +
            '<div class="panel desc">' +
              '<h2>عن الوظيفة</h2>' +
              (paragraphs.length ? paragraphs.map((t) => '<p dir="auto">' + esc(t) + '</p>').join('')
                : '<p class="muted">' + (j._loaded || j.description === '' ? 'التفاصيل الكاملة متوفرة في موقع المصدر.' : 'جارٍ تحميل التفاصيل...') + '</p>') +
              (reqs.length ? '<h2>المتطلبات</h2><ul class="req-list">' + reqs.map((r) => '<li dir="auto">' + esc(r) + '</li>').join('') + '</ul>' : '') +
            '</div>' +
            (j.skills.length ? '<div class="panel">' +
              '<h2>المهارات المطلوبة</h2>' +
              '<ul class="chips">' + j.skills.map((s) => '<li class="chip' + (hitSet.has(s) ? ' hit' : '') + '">' + (hitSet.has(s) ? ICON.check.replace('<svg ', '<svg width="14" height="14" ') : '') + esc(s) + '</li>').join('') + '</ul>' +
              (m ? '<p class="muted mt-12">المهارات الملوّنة بالذهبي تطابق مهاراتك.</p>' : '') +
            '</div>' : '') +
          '</article>' +
          '<aside>' +
            '<div class="panel apply-box">' +
              (m
                ? '<div class="match-line"><strong>توافقك مع الوظيفة</strong><span class="match ' + matchClass(m.score) + '">' + m.score + '%</span></div>' +
                  '<div class="meter" role="progressbar" aria-label="نسبة التوافق" aria-valuemin="0" aria-valuemax="100" aria-valuenow="' + m.score + '"><span style="width:' + m.score + '%"></span></div>'
                : '<p class="muted mb-14"><a href="#/register">اكتب تخصصك ومهاراتك</a> لتعرف نسبة توافقك مع هذه الوظيفة.</p>') +
              '<ul class="facts">' +
                '<li><span>المصدر</span><strong>' + esc(j.direct ? 'إعلان مباشر من الجهة' : j.source) + '</strong></li>' +
                '<li><span>الجهة</span><strong><bdi>' + esc(j.company) + '</bdi></strong></li>' +
                '<li><span>المدينة</span><strong>' + esc(j.city) + '</strong></li>' +
                '<li><span>نوع العقد</span><strong>' + esc(j.contract_type) + '</strong></li>' +
                '<li><span>تاريخ النشر</span><strong>' + esc(fullDate(j.posted_date)) + '</strong></li>' +
              '</ul>' +
              (url
                ? '<a class="btn btn-gold btn-lg btn-block" href="' + esc(url) + '"' + (isMail ? '' : ' target="_blank" rel="noopener noreferrer"') + '>' +
                    (isMail ? 'راسل الجهة المعلنة ' + ICON.send : 'اذهب للإعلان الأصلي ' + ICON.external) + '</a>' +
                  '<p class="apply-note">' + (isMail ? 'سيُفتح بريدك لترسل سيرتك إلى الجهة المعلنة مباشرة.'
                    : j.direct ? 'سيُفتح رابط التقديم الذي وضعته الجهة المعلنة، في نافذة جديدة.'
                    : directUrl ? 'سيُفتح رابط التقديم الأصلي الذي وجدناه في الإعلان، في نافذة جديدة.'
                    : 'سيُفتح الإعلان في موقع <bdi>' + esc(j.source) + '</bdi>، في نافذة جديدة.') + '</p>' +
                  (directUrl && pageUrl && pageUrl !== directUrl
                    ? '<p class="apply-note"><a href="' + esc(pageUrl) + '" target="_blank" rel="noopener noreferrer">صفحة الإعلان في <bdi>' + esc(j.source) + '</bdi></a></p>' : '')
                : '<p class="apply-note">رابط الإعلان الأصلي غير متوفر حالياً.</p>') +
              '<p class="relay-note">رزق ينقل الإعلان فقط، ولا يستقبل طلبات التوظيف ولا يطّلع عليها.</p>' +
              (alsoOn.length ? '<p class="apply-note">منشورة أيضاً في: ' + alsoOn.map(esc).join('، ') + '</p>' : '') +
              '<div class="safety">' + ICON.shield + '<p>لا تدفع أي مبلغ مقابل التوظيف، ولا تشارك بياناتك البنكية أو رمز التحقق (نفاذ/أبشر) مع أي جهة. وإن رأيت ما يريب فبلّغ عنه.</p></div>' +
              (j.is_demo ? '' :
                '<details class="report" id="report-box"><summary>' + ICON.flag + 'بلّغ عن هذا الإعلان</summary>' +
                  '<form id="report-form" class="report-form">' +
                    '<label for="rp-reason" class="sr-only">سبب البلاغ</label>' +
                    '<select id="rp-reason">' + optionList(REPORT_REASONS, REPORT_REASONS[0]) + '</select>' +
                    '<label for="rp-note" class="sr-only">ملاحظة</label>' +
                    '<input type="text" id="rp-note" maxlength="300" placeholder="ملاحظة (اختياري)">' +
                    '<button type="submit" class="btn btn-ghost btn-sm">إرسال البلاغ</button>' +
                  '</form>' +
                '</details>') +
            '</div>' +
          '</aside>' +
        '</div>' +
        (similar.length ? '<section class="similar"><h2>وظائف مشابهة</h2><div class="card-grid">' + similar.map(jobCard).join('') + '</div></section>' : '') +
      '</section>';

    const rf = document.getElementById('report-form');
    if (rf) {
      rf.addEventListener('submit', async (e) => {
        e.preventDefault();
        const btn = rf.querySelector('button');
        busy(btn, true, 'جارٍ الإرسال...');
        const res = await api('POST', '/api/reports', {
          job_id: j.id,
          reason: document.getElementById('rp-reason').value,
          note: cleanText(document.getElementById('rp-note').value)
        });
        busy(btn, false);
        if (res.ok) {
          document.getElementById('report-box').outerHTML = '<p class="apply-note">شكراً، وصل بلاغك وسنراجعه.</p>';
        } else {
          toast(res.data.error || 'تعذّر إرسال البلاغ.');
        }
      });
    }
  }

  // ---------- View: Register / my profile ----------
  function renderRegister() {
    const p = state.profile;
    setTitle(p ? 'تنبيهاتي' : 'اشترك في التنبيهات');
    // An older specialty name is shown as its first new specialty (the user confirms on save)
    const pSpec = p && LEGACY_SPECIALTIES[p.specialty] ? LEGACY_SPECIALTIES[p.specialty][0] : (p ? p.specialty : '');
    const isOtherSpecialty = !!pSpec && SPECIALTIES.indexOf(pSpec) === -1;
    let skills = p && Array.isArray(p.skills) ? p.skills.slice() : [];
    let cvFile = null;
    let removeSavedFile = false;

    function filePill(name, saved) {
      return '<div class="file-pill"><span>' + (saved ? 'الملف المحفوظ: ' : '') + esc(name) + '</span>' +
        '<button type="button" class="btn-link" id="file-remove">إزالة</button></div>';
    }

    main.innerHTML = '' +
      '<section class="page container">' +
        '<div class="form-wrap">' +
          '<div class="page-head">' +
            '<h1>' + (p ? 'تنبيهاتي' : 'اجعل الوظائف تصلك') + '</h1>' +
            '<p>' + (p ? 'حدّث اهتماماتك متى احتجت، وتتحدّث التنبيهات مباشرة.'
              : 'اكتب تخصصك ومهاراتك ومدينتك، ثم اربط تيليجرام، وتصلك كل وظيفة جديدة تناسبك في مكان واحد. مجاناً، وتوقفها متى شئت.') + '</p>' +
          '</div>' +
          (p ? '<div class="notice"><p>أنت مشترك باسم <strong>' + esc(p.name) + '</strong>.</p><button type="button" class="btn-link" id="delete-profile">إلغاء الاشتراك وحذف بياناتي</button></div>' +
               '<div id="telegram-box"></div><div id="cv-skills-box"></div>' : '') +
          '<form class="form-card" id="register-form" novalidate>' +
            '<div class="form-grid">' +
              '<div class="field full">' +
                '<label for="r-name">الاسم<span class="req" aria-hidden="true">*</span></label>' +
                '<input type="text" id="r-name" name="name" maxlength="60" autocomplete="name" placeholder="مثال: محمد العتيبي" value="' + esc(p ? p.name : '') + '" required aria-describedby="e-name">' +
                '<p class="error" id="e-name"></p>' +
              '</div>' +
              '<div class="field">' +
                '<label for="r-specialty">التخصص<span class="req" aria-hidden="true">*</span></label>' +
                '<select id="r-specialty" name="specialty" required aria-describedby="e-specialty">' +
                  specialtyOptions(isOtherSpecialty ? '' : pSpec, 'اختر تخصصك') +
                  '<option value="__other"' + (isOtherSpecialty ? ' selected' : '') + '>تخصص آخر...</option>' +
                '</select>' +
                '<input type="text" id="r-specialty-other" class="mt-8" maxlength="60" placeholder="اكتب تخصصك" value="' + esc(isOtherSpecialty ? p.specialty : '') + '"' + (isOtherSpecialty ? '' : ' hidden') + ' aria-label="اكتب تخصصك">' +
                '<p class="error" id="e-specialty"></p>' +
              '</div>' +
              '<div class="field">' +
                '<label for="r-city">المدينة<span class="req" aria-hidden="true">*</span></label>' +
                '<select id="r-city" name="city" required aria-describedby="e-city">' + optionList(CITIES, p ? p.city : '', 'اختر مدينتك') + '</select>' +
                '<p class="error" id="e-city"></p>' +
              '</div>' +
              '<div class="field full">' +
                '<label for="r-skill-input">المهارات<span class="req" aria-hidden="true">*</span></label>' +
                '<div class="tag-input" id="tag-input">' +
                  '<span id="tag-list" role="list" class="contents"></span>' +
                  '<input type="text" id="r-skill-input" placeholder="اكتب مهارة واضغط Enter" autocomplete="off" aria-describedby="h-skills e-skills">' +
                '</div>' +
                '<p class="hint" id="h-skills">افصل بين المهارات بفاصلة أو اضغط Enter بعد كل مهارة.</p>' +
                '<div class="suggest" id="skill-suggest"></div>' +
                '<p class="error" id="e-skills"></p>' +
              '</div>' +
              '<div class="field full">' +
                '<label for="r-cv">السيرة الذاتية <span class="opt">(اختياري، تحسّن دقة اختيار الوظائف لك)</span></label>' +
                '<textarea id="r-cv" maxlength="' + MAX_CV_TEXT + '" placeholder="الصق نص سيرتك الذاتية هنا...">' + esc(p && p.cv_text ? p.cv_text : '') + '</textarea>' +
                '<div class="or-sep">أو</div>' +
                '<label class="dropzone" id="dropzone" for="r-cv-file">' +
                  ICON.upload +
                  '<strong>ارفع ملف السيرة الذاتية</strong>' +
                  '<span>PDF أو Word أو TXT، بحد أقصى 2 ميجابايت</span>' +
                '</label>' +
                '<input type="file" id="r-cv-file" class="sr-only" tabindex="-1" accept=".pdf,.doc,.docx,.txt">' +
                '<div id="file-box">' + (p && p.cv_file ? filePill(p.cv_file.name, true) : '') + '</div>' +
                '<p class="hint">نستخدم سيرتك لمطابقة الوظائف فقط، ولا نرسلها لأي جهة.</p>' +
                '<p class="error" id="e-cv"></p>' +
              '</div>' +
              '<fieldset class="field full interests">' +
                '<legend>تنبيهات للطلاب (اختياري)</legend>' +
                '<label class="checkbox"><input type="checkbox" id="r-int-coop" value="coop"' + (p && (p.interests || []).indexOf('coop') !== -1 ? ' checked' : '') + '><span>أرسل لي فرص التدريب التعاوني الجديدة</span></label>' +
                '<label class="checkbox"><input type="checkbox" id="r-int-scholarship" value="scholarship"' + (p && (p.interests || []).indexOf('scholarship') !== -1 ? ' checked' : '') + '><span>أرسل لي إعلانات الابتعاث الجديدة، وأي برنامج تعلن صفحته الرسمية فتح باب التقديم</span></label>' +
              '</fieldset>' +
              '<div class="field full">' +
                '<label class="checkbox"><input type="checkbox" id="r-consent"' + (p ? ' checked' : '') + ' aria-describedby="e-consent"><span>أوافق على استخدام بياناتي وسيرتي الذاتية لاختيار الوظائف المناسبة لي وإرسال تنبيهاتها فقط، ويمكنني حذفها في أي وقت. <a href="#/privacy">سياسة الخصوصية</a></span></label>' +
                '<p class="error" id="e-consent"></p>' +
              '</div>' +
            '</div>' +
            '<div class="form-actions"><button type="submit" class="btn btn-gold btn-lg btn-block" id="r-submit">' + (p ? SUBMIT_EDIT : SUBMIT_NEW) + '</button></div>' +
          '</form>' +
        '</div>' +
      '</section>';

    const form = document.getElementById('register-form');
    const specialtySel = document.getElementById('r-specialty');
    const specialtyOther = document.getElementById('r-specialty-other');
    const skillInput = document.getElementById('r-skill-input');
    const tagBox = document.getElementById('tag-input');
    const fileInput = document.getElementById('r-cv-file');
    const dropzone = document.getElementById('dropzone');
    const cvText = document.getElementById('r-cv');

    function currentSpecialty() {
      return specialtySel.value === '__other' ? specialtyOther.value.trim() : specialtySel.value;
    }

    function renderTags() {
      document.getElementById('tag-list').innerHTML = skills.map((s, i) =>
        '<span class="tag" role="listitem">' + esc(s) +
        '<button type="button" data-remove="' + i + '" aria-label="حذف ' + esc(s) + '">&times;</button></span>'
      ).join('');
      renderSuggestions();
    }

    function renderSuggestions() {
      const box = document.getElementById('skill-suggest');
      const have = new Set(skills.map(norm));
      const list = (SKILL_SUGGESTIONS[specialtySel.value] || []).filter((s) => !have.has(norm(s)));
      box.innerHTML = list.length
        ? '<span class="label">اقتراحات:</span>' + list.map((s) => '<button type="button" data-suggest="' + esc(s) + '">+ ' + esc(s) + '</button>').join('')
        : '';
    }

    function addSkills(raw) {
      let hitLimit = false;
      String(raw).split(/[,،\n]/).forEach((part) => {
        const s = cleanText(part).slice(0, 40);
        if (!s || skills.some((x) => norm(x) === norm(s))) return;
        if (skills.length >= MAX_SKILLS) { hitLimit = true; return; }
        skills.push(s);
      });
      setError('skills', hitLimit ? 'وصلت إلى الحد الأقصى (' + MAX_SKILLS + ' مهارة).' : '');
      renderTags();
    }

    function setError(field, msg) {
      const el = document.getElementById('e-' + field);
      if (el) el.textContent = msg;
      const map = { name: 'r-name', specialty: 'r-specialty', city: 'r-city', consent: 'r-consent' };
      if (map[field]) {
        const input = document.getElementById(map[field]);
        if (msg) input.setAttribute('aria-invalid', 'true'); else input.removeAttribute('aria-invalid');
      }
      if (field === 'skills') tagBox.classList.toggle('invalid', !!msg);
    }

    function setFile(file) {
      setError('cv', '');
      if (!file) return;
      const ext = (file.name.split('.').pop() || '').toLowerCase();
      if (CV_EXT.indexOf(ext) === -1) { setError('cv', 'صيغة الملف غير مدعومة. المسموح: PDF أو Word أو TXT.'); fileInput.value = ''; return; }
      if (file.size > MAX_CV_BYTES) { setError('cv', 'حجم الملف أكبر من 2 ميجابايت.'); fileInput.value = ''; return; }
      if (file.size === 0) { setError('cv', 'الملف فارغ.'); fileInput.value = ''; return; }
      cvFile = file;
      document.getElementById('file-box').innerHTML = filePill(file.name, false);
      if (ext === 'txt' && !cvText.value.trim()) {
        file.text().then((t) => { cvText.value = t.slice(0, MAX_CV_TEXT); }).catch(() => {});
      }
    }

    renderTags();
    if (p) refreshProfileStatus();

    // --- Events ---
    specialtySel.addEventListener('change', () => {
      const other = specialtySel.value === '__other';
      specialtyOther.hidden = !other;
      if (other) specialtyOther.focus();
      setError('specialty', '');
      renderSuggestions();
    });

    tagBox.addEventListener('click', (e) => {
      const rm = e.target.closest('[data-remove]');
      if (rm) {
        skills.splice(Number(rm.getAttribute('data-remove')), 1);
        renderTags();
        skillInput.focus();
        return;
      }
      if (e.target === tagBox) skillInput.focus();
    });

    skillInput.addEventListener('keydown', (e) => {
      if (e.isComposing) return;
      if (e.key === 'Enter' || e.key === ',' || e.key === '،') {
        e.preventDefault();
        addSkills(skillInput.value);
        skillInput.value = '';
      } else if (e.key === 'Backspace' && !skillInput.value && skills.length) {
        skills.pop();
        renderTags();
      }
    });
    // Mobile keyboards may not report the comma key: split on input too
    skillInput.addEventListener('input', () => {
      if (/[,،]/.test(skillInput.value)) {
        addSkills(skillInput.value);
        skillInput.value = '';
      }
    });
    skillInput.addEventListener('blur', () => {
      if (skillInput.value.trim()) { addSkills(skillInput.value); skillInput.value = ''; }
    });

    document.getElementById('skill-suggest').addEventListener('click', (e) => {
      const b = e.target.closest('[data-suggest]');
      if (b) addSkills(b.getAttribute('data-suggest'));
    });

    fileInput.addEventListener('change', () => setFile(fileInput.files[0]));
    ['dragenter', 'dragover'].forEach((ev) => dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.add('drag'); }));
    ['dragleave', 'drop'].forEach((ev) => dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.remove('drag'); }));
    dropzone.addEventListener('click', (e) => { e.preventDefault(); fileInput.click(); });
    dropzone.addEventListener('drop', (e) => {
      const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (f) setFile(f);
    });
    dropzone.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileInput.click(); }
    });
    dropzone.setAttribute('tabindex', '0');
    dropzone.setAttribute('role', 'button');

    document.getElementById('file-box').addEventListener('click', (e) => {
      if (e.target.id !== 'file-remove') return;
      const box = document.getElementById('file-box');
      if (cvFile) {
        cvFile = null;
        fileInput.value = '';
        box.innerHTML = (p && p.cv_file && !removeSavedFile) ? filePill(p.cv_file.name, true) : '';
      } else {
        removeSavedFile = true;
        box.innerHTML = '';
      }
    });

    // Clear errors while typing (not on blur) so the layout doesn't jump under the cursor
    document.getElementById('r-name').addEventListener('input', () => setError('name', ''));
    ['r-city', 'r-consent'].forEach((id) => {
      document.getElementById(id).addEventListener('change', () => setError(id.replace('r-', ''), ''));
    });
    specialtyOther.addEventListener('input', () => setError('specialty', ''));

    const delBtn = document.getElementById('delete-profile');
    if (delBtn) delBtn.addEventListener('click', deleteProfile);

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (skillInput.value.trim()) { addSkills(skillInput.value); skillInput.value = ''; }

      const name = cleanText(document.getElementById('r-name').value);
      const specialty = cleanText(currentSpecialty());
      const city = document.getElementById('r-city').value;
      const consent = document.getElementById('r-consent').checked;
      const cv = cvText.value.replace(BIDI_RE, '').trim().slice(0, MAX_CV_TEXT);

      const errors = {};
      if (name.length < 2) errors.name = 'اكتب اسمك (حرفين على الأقل).';
      if (!specialty) errors.specialty = 'اختر تخصصك أو اكتبه.';
      if (!city) errors.city = 'اختر مدينتك.';
      if (!skills.length) errors.skills = 'أضف مهارة واحدة على الأقل.';
      if (!consent) errors.consent = 'نحتاج موافقتك لنرسل لك التنبيهات.';

      ['name', 'specialty', 'city', 'skills', 'consent'].forEach((k) => setError(k, errors[k] || ''));
      const firstKey = Object.keys(errors)[0];
      if (firstKey) {
        const focusMap = { name: 'r-name', specialty: specialtySel.value === '__other' ? 'r-specialty-other' : 'r-specialty', city: 'r-city', skills: 'r-skill-input', consent: 'r-consent' };
        document.getElementById(focusMap[firstKey]).focus();
        return;
      }

      const submitBtn = document.getElementById('r-submit');
      busy(submitBtn, true);

      const interests = ['coop', 'scholarship'].filter((k) => document.getElementById('r-int-' + k).checked);
      const payload = Object.assign({ name: name, specialty: specialty, skills: skills.slice(), city: city, cv_text: cv, interests: interests }, profileAuth() || {});
      if (cvFile) {
        try {
          payload.cv_file = { name: cvFile.name, size: cvFile.size, data: await fileToBase64(cvFile) };
        } catch (err) {
          setError('cv', 'تعذّرت قراءة الملف، جرّب ملفاً آخر.');
          busy(submitBtn, false);
          return;
        }
      } else if (removeSavedFile) {
        payload.remove_cv_file = true;
      }

      const res = await api('POST', '/api/users', payload, 30000);
      if (!res.ok) {
        busy(submitBtn, false);
        if (res.status === 403) {
          toast(res.data.error || 'انتهت صلاحية بياناتك على هذا الجهاز.');
          clearProfile();
          renderRegister();
          return;
        }
        toast(res.data.error || 'تعذّر الحفظ، حاول مرة أخرى.');
        return;
      }

      const u = res.data.user;
      const wasUpdate = !!p;
      saveProfile({
        id: u.id,
        edit_token: res.data.edit_token || (p && p.edit_token),
        name: u.name, specialty: u.specialty, city: u.city,
        skills: payload.skills, cv_text: cv,
        cv_file: u.cv_file || null, cv_skills: u.cv_skills || [],
        telegram_linked: !!u.telegram_linked,
        interests: u.interests || interests,
        updated_at: new Date().toISOString()
      });
      renderRegisterSuccess(wasUpdate);
    });
  }

  function fileToBase64(file) {
    return new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => resolve(String(r.result).split(',')[1] || '');
      r.onerror = () => reject(r.error);
      r.readAsDataURL(file);
    });
  }

  async function deleteProfile() {
    if (!window.confirm('سيتم إلغاء اشتراكك وحذف بياناتك وسيرتك الذاتية نهائياً من رزق. هل تريد المتابعة؟')) return;
    const auth = profileAuth();
    let serverDeleted = !auth;
    if (auth) {
      const res = await api('POST', '/api/users/delete', auth);
      if (!res.ok && res.status !== 403) {
        toast(res.data.error || 'تعذّر الحذف، حاول مرة أخرى.');
        return;
      }
      serverDeleted = res.ok;
    }
    clearProfile();
    toast(serverDeleted ? 'تم إلغاء اشتراكك وحذف بياناتك نهائياً.' : 'تم مسح بياناتك من هذا الجهاز. تعذّر التحقق من نسختك على الخادم.');
    renderRegister();
  }

  // Refresh server-side profile info (CV skills, Telegram link state)
  async function refreshProfileStatus() {
    const auth = profileAuth();
    if (!auth) { renderTelegramBox(); return; }
    const res = await api('POST', '/api/users/status', auth);
    if (res.ok && state.profile) {
      const u = res.data.user;
      state.profile.cv_skills = u.cv_skills || [];
      state.profile.telegram_linked = !!u.telegram_linked;
      state.profile.cv_file = u.cv_file || null;
      saveProfile(Object.assign({}, state.profile, { updated_at: state.profile.updated_at }));
    }
    renderTelegramBox();
    const box = document.getElementById('cv-skills-box');
    const cvSkills = (state.profile && state.profile.cv_skills) || [];
    if (box && cvSkills.length) {
      box.innerHTML = '<div class="panel panel-tight"><h2>مهارات استخرجناها من سيرتك</h2>' +
        '<ul class="chips">' + cvSkills.map((s) => '<li class="chip hit">' + esc(s) + '</li>').join('') + '</ul>' +
        '<p class="muted mt-12">نستخدمها مع مهاراتك المكتوبة لاختيار الوظائف المناسبة لك.</p></div>';
    }
  }

  function telegramBoxHtml() {
    const p = state.profile;
    if (!p) return '';
    if (!state.settings.telegram_bot) {
      return '<div class="panel panel-tight tg-box"><p class="muted">تنبيهات تيليجرام ستتوفر قريباً، وإلى ذلك الحين رتّبنا لك الوظائف حسب الأنسب لك.</p></div>';
    }
    if (p.telegram_linked) {
      return '<div class="panel panel-tight tg-box tg-on">' + ICON.check.replace('<svg ', '<svg width="20" height="20" ') +
        '<p>التنبيهات مفعّلة على تيليجرام. أرسل <strong class="ltr">/jobs</strong> للبوت لترى أنسب الوظائف الآن، و<strong class="ltr">/stop</strong> لإيقافها.</p></div>';
    }
    return '<div class="panel panel-tight tg-box"><div><strong>الخطوة الأخيرة: اربط تيليجرام</strong><p class="muted">تصلك كل وظيفة جديدة تناسبك فور نقلها.</p></div>' +
      '<button type="button" class="btn btn-gold" id="tg-link">' + ICON.send + 'اربط تيليجرام</button></div>';
  }

  function renderTelegramBox() {
    const box = document.getElementById('telegram-box');
    if (!box) return;
    box.innerHTML = telegramBoxHtml();
    const btn = document.getElementById('tg-link');
    if (btn) btn.addEventListener('click', openTelegramLink);
  }

  async function openTelegramLink() {
    const auth = profileAuth();
    if (!auth) return;
    // Open the tab synchronously (popup blockers), then point it at the one-time link
    const win = window.open('', '_blank');
    const res = await api('POST', '/api/users/telegram-link', auth);
    if (res.ok && res.data.url && safeUrl(res.data.url)) {
      if (win) { win.opener = null; win.location.href = res.data.url; } else { location.href = res.data.url; }
      toast('اضغط «ابدأ» (Start) في تيليجرام لإكمال الربط، ثم ارجع لهذه الصفحة.');
    } else {
      if (win) win.close();
      toast(res.data.error || 'تعذّر إنشاء رابط الربط.');
    }
  }

  function renderRegisterSuccess(wasUpdate) {
    setTitle(wasUpdate ? 'تم الحفظ' : 'تم الاشتراك');
    const user = state.profile;
    const good = state.jobs.filter((j) => matchScore(j).score >= 50).length;
    main.innerHTML = '' +
      '<section class="page container">' +
        '<div class="form-wrap">' +
          '<div class="form-card success">' +
            '<div class="success-icon">' + ICON.check + '</div>' +
            '<h2>' + (wasUpdate ? 'تم تحديث اهتماماتك' : 'تم اشتراكك يا ' + esc(user.name)) + '</h2>' +
            '<p>' + (good
              ? 'وجدنا لك الآن <strong>' + esc(jobsWord(good, true)) + '</strong> تناسب تخصصك ومهاراتك.'
              : 'لا توجد حالياً وظائف قريبة من تخصصك، وننقل وظائف جديدة باستمرار.') + '</p>' +
            '<div id="telegram-box" class="mt-12"></div>' +
            '<div class="actions">' +
              '<a href="#/jobs" class="btn btn-gold btn-lg" id="go-matched">تصفّح الوظائف المناسبة لك</a>' +
              '<a href="#/" class="btn btn-ghost btn-lg">الرئيسية</a>' +
            '</div>' +
          '</div>' +
        '</div>' +
      '</section>';
    renderTelegramBox();
    document.getElementById('go-matched').addEventListener('click', () => {
      state.filters = defaultFilters('match');
    });
    window.scrollTo(0, 0);
    main.focus({ preventScroll: true });
  }

  // ---------- View: Support (optional, not charity) ----------
  function renderSupport() {
    setTitle('ادعم رزق');
    const sp = state.settings.support || {};
    if (sp.enabled === false) { renderNotFound(); return; }
    const links = (sp.links || []).filter((l) => safeUrl(l.url));
    main.innerHTML = '' +
      '<section class="page container">' +
        '<div class="form-wrap">' +
          '<div class="form-card support-card">' +
            '<svg class="support-logo" aria-hidden="true"><use href="#rizq-mark"/></svg>' +
            '<h1>ادعم رزق</h1>' +
            '<p class="lead-sm">' + esc(sp.text || '') + '</p>' +
            (links.length
              ? '<div class="support-links">' + links.map((l) =>
                  '<a class="btn btn-gold btn-lg" href="' + esc(safeUrl(l.url)) + '" target="_blank" rel="noopener noreferrer">' + esc(l.label) + '</a>').join('') + '</div>'
              : '<p class="muted">روابط الدعم ستتوفر قريباً.</p>') +
            '<p class="muted mt-12">رزق مجاني للجميع بكل مزاياه، سواء دعمت أم لم تدعم.</p>' +
          '</div>' +
        '</div>' +
      '</section>';
  }

  // ---------- View: Privacy ----------
  function renderPrivacy() {
    setTitle('سياسة الخصوصية');
    main.innerHTML = '' +
      '<section class="page container">' +
        '<div class="form-wrap">' +
          '<div class="page-head"><h1>سياسة الخصوصية</h1><p>نلتزم بحماية بياناتك وفق نظام حماية البيانات الشخصية في المملكة.</p></div>' +
          '<div class="panel desc prose">' +
            '<h2>من نحن؟</h2>' +
            '<p>رزق مبادرة فردية مجانية تنقل أخبار الوظائف من مصادرها. لسنا جهة توظيف، ولا نستقبل طلبات التوظيف، ولا نرسل بياناتك أو سيرتك لأي جهة.</p>' +
            '<h2>ما الذي نجمعه؟</h2>' +
            '<p>إذا اشتركت في التنبيهات: الاسم، والتخصص، والمهارات، والمدينة، والسيرة الذاتية إن أضفتها، ومعرّف محادثة تيليجرام إذا ربطته.</p>' +
            '<h2>لماذا نجمعه؟</h2>' +
            '<p>لغرض واحد فقط: اختيار الوظائف المناسبة لك وإرسال تنبيهاتها إليك. لا نبيع بياناتك ولا نستخدمها لأي غرض تسويقي، ولا نشاركها إلا مع مزوّدي الخدمة اللازمين لتشغيل رزق (تيليجرام وخدمة الذكاء الاصطناعي).</p>' +
            '<h2>الذكاء الاصطناعي</h2>' +
            '<p>قد نستخدم خدمة ذكاء اصطناعي لاستخراج المهارات من سيرتك، بعد حذف رقم الجوال والبريد ورقم الهوية منها. وتُعالج هذه الخدمة خارج المملكة، ونكتفي بإرسال النص بعد حذف بياناتك التعريفية.</p>' +
            '<h2>الوظائف المعروضة</h2>' +
            '<p>ننقل إعلانات الوظائف فقط من مصادر عامة، ومع كل إعلان اسم مصدره ورابطه الأصلي. ولا نجمع أي بيانات شخصية عن الأفراد. التقديم يتم عند الجهة صاحبة الإعلان مباشرة.</p>' +
            '<h2>الإعلانات المباشرة</h2>' +
            '<p>إذا أعلنت جهة عن وظيفة عبر صفحة «أعلن عن وظيفة»، نحتفظ ببريد التواصل الذي تكتبه لمراجعة الإعلان والتواصل بشأنه فقط، ولا نعرضه للعامة.</p>' +
            '<h2>بلا إعلانات تجارية</h2>' +
            '<p>لا نعرض إعلانات تجارية، ولا نستخدم أدوات تتبع أو تحليل خارجية.</p>' +
            '<h2>حقك في الحذف</h2>' +
            '<p>يمكنك إلغاء اشتراكك وحذف بياناتك وسيرتك نهائياً في أي وقت من صفحة «تنبيهاتي»، ويمكنك إيقاف التنبيهات بإرسال <span class="ltr">/stop</span> للبوت.</p>' +
            '<h2>حقوقك</h2>' +
            '<p>يمكنك الاطلاع على بياناتك وتصحيحها من صفحة «تنبيهاتي»، أو طلب حذفها في أي وقت.</p>' +
            '<h2>تنبيه أمان</h2>' +
            '<p>رزق لا يطلب أي رسوم. إذا طلب منك إعلان دفع مبلغ مقابل التوظيف فلا تدفع، وبلّغ عنه من صفحة الوظيفة.</p>' +
          '</div>' +
        '</div>' +
      '</section>';
  }

  // ---------- View: errors ----------
  function renderNotFound(title, text) {
    setTitle(title || 'الصفحة غير موجودة');
    main.innerHTML = '<div class="container"><div class="state-box">' +
      '<h1>' + esc(title || 'الصفحة غير موجودة') + '</h1>' +
      '<p>' + esc(text || 'الرابط الذي فتحته غير صحيح.') + '</p>' +
      '<a href="#/jobs" class="btn btn-gold">تصفّح الوظائف</a>' +
      '</div></div>';
  }

  function renderLoadError() {
    setTitle('تعذّر التحميل');
    const isFile = location.protocol === 'file:';
    main.innerHTML = '<div class="container"><div class="state-box">' +
      '<h1>تعذّر تحميل الوظائف</h1>' +
      (isFile
        ? '<p>يبدو أنك فتحت الملف مباشرة. شغّل الموقع بالضغط مرتين على الملف <code>start.bat</code> داخل مجلد المشروع، وسيفتح لك المتصفح تلقائياً.</p>'
        : '<p>تعذّر الاتصال بخادم رزق. تأكد أن نافذة <code>start.bat</code> مفتوحة، ثم أعد المحاولة.</p>') +
      '<button type="button" class="btn btn-gold" id="retry">إعادة المحاولة</button>' +
      '</div></div>';
    document.getElementById('retry').addEventListener('click', () => location.reload());
  }

  // ---------- Routes ----------
  addRoute(/^\/$/, 'home', renderHome);
  addRoute(/^\/jobs$/, 'jobs', renderJobs);
  addRoute(/^\/job\/([A-Za-z0-9_-]+)$/, 'job', renderJob, 'jobs');
  addRoute(/^\/register$/, 'register', renderRegister);
  addRoute(/^\/support$/, 'support', renderSupport);
  addRoute(/^\/privacy$/, 'privacy', renderPrivacy);

  // ---------- Public API for js/portal.js ----------
  window.Rizq = {
    addRoute: function (re, view, render, navKey, standalone) {
      addRoute(re, view, render, navKey);
      if (standalone) ROUTES[ROUTES.length - 1].standalone = true;
    },
    api: api, esc: esc, safeUrl: safeUrl, genderLabel: genderLabel, toast: toast, busy: busy, setTitle: setTitle,
    optionList: optionList, cleanText: cleanText, fullDate: fullDate, relativeDate: relativeDate,
    jobsWord: jobsWord, ICON: ICON, SPECIALTIES: SPECIALTIES, CITIES: CITIES, CONTRACTS: CONTRACTS, arCount: arCount,
    main: main, state: state, reloadData: async function () { await loadData(); },
    router: router
  };

  // ---------- Boot ----------
  function boot() {
    document.getElementById('skip-link').addEventListener('click', () => main.focus());
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeFilters(); });
    window.addEventListener('hashchange', router);
    // Clicking a link to the current route (e.g. "تنبيهاتي" on the success screen) re-renders it
    document.addEventListener('click', (e) => {
      const a = e.target.closest && e.target.closest('a[href^="#/"]');
      if (a && a.getAttribute('href') === location.hash && !e.defaultPrevented) {
        e.preventDefault();
        router();
      }
    });
    // A file dropped outside the dropzone must not navigate away and lose the form
    ['dragover', 'drop'].forEach((ev) => window.addEventListener(ev, (e) => {
      if (!(e.target.closest && e.target.closest('.dropzone'))) e.preventDefault();
    }));
    // Pick up profile changes made in another tab
    window.addEventListener('storage', (e) => {
      if (e.key === PROFILE_KEY) { state.profile = loadProfile(); matchCache.clear(); updateNav(); }
    });
    loadData().then(router);
  }

  // portal.js registers its routes before DOMContentLoaded fires
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else setTimeout(boot, 0);
})();
