/* RIZQ — co-op training (#/coop) and scholarships (#/scholarships).
   Curated programs from /api/programs (official links, usual opening months, conditions)
   plus the newest collected posts that announce a co-op or a scholarship.
   Uses the shared helpers exposed by js/app.js as window.Rizq. */
'use strict';

(function () {
  const R = window.Rizq;
  const esc = R.esc;
  const main = R.main;

  const MONTHS = ['يناير', 'فبراير', 'مارس', 'أبريل', 'مايو', 'يونيو', 'يوليو', 'أغسطس', 'سبتمبر', 'أكتوبر', 'نوفمبر', 'ديسمبر'];
  const PROVIDERS = [
    ['', 'الكل'], ['government', 'حكومية'], ['semi_gov', 'شبه حكومية'],
    ['saudi_private', 'شركات سعودية'], ['foreign_in_ksa', 'شركات عالمية في السعودية']
  ];
  const PROVIDER_LABEL = {
    government: 'جهة حكومية', semi_gov: 'جهة شبه حكومية',
    saudi_private: 'شركة سعودية', foreign_in_ksa: 'شركة عالمية في السعودية'
  };

  // Mirrors backend/programs.py kind_of() (titles only: the list API has no full text)
  const SCHOLARSHIP_RE = /ابتعاث|مبتعث|scholarship|منح[هة]? دراسي|cadet|طيار متدرب/i;
  const COOP_RE = /تدريب تعاوني|التدريب التعاوني|تعاوني للطلاب|co-?op\b|coop\b|internship|\bintern\b|تدريب صيفي|summer (training|internship)|تدريب ميداني/i;

  const PAGES = {
    coop: {
      path: '/coop', title: 'التدريب التعاوني',
      intro: 'برامج التدريب التعاوني لطلاب الجامعات والكليات في الجهات الحكومية والشركات السعودية والعالمية داخل المملكة: متى يفتح التقديم عادة، وشروطه، ورابطه الرسمي.',
      alert: 'يصلك كل إعلان تدريب تعاوني جديد على تيليجرام فور نقلنا له.',
      postsTitle: 'إعلانات تدريب تعاوني منشورة حديثاً', empty: 'لا توجد برامج تطابق اختيارك.'
    },
    scholarship: {
      path: '/scholarships', title: 'الابتعاث',
      intro: 'برامج الابتعاث والمنح للدراسة داخل المملكة وخارجها من الحكومة والجهات شبه الحكومية والشركات السعودية والعالمية: متى يفتح التقديم عادة، وشروطه، ورابطه الرسمي.',
      alert: 'يصلك كل إعلان ابتعاث جديد، وتنبيه فور إعلان الصفحة الرسمية لأي برنامج عن فتح باب التقديم.',
      postsTitle: 'إعلانات ابتعاث منشورة حديثاً', empty: 'لا توجد برامج تطابق اختيارك.'
    }
  };

  let cache = null;          // {programs: [...]}
  const filters = { coop: { type: '', when: '', q: '' }, scholarship: { type: '', when: '', q: '' } };

  async function loadPrograms() {
    if (cache) return cache;
    const res = await R.api('GET', '/api/programs');
    if (!res.ok) throw new Error(res.data.error || 'HTTP ' + res.status);
    cache = res.data;
    return cache;
  }

  function monthList(months) {
    const names = months.map((m) => MONTHS[m - 1]);
    return names.length > 1 ? names.slice(0, -1).join('، ') + ' و' + names[names.length - 1] : names[0];
  }

  // Status from the usual opening months (and what the watcher saw on the official page)
  function status(p) {
    const now = new Date().getMonth() + 1;
    const d = new Date();   // local date (Riyadh), not UTC
    const today = d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
    if (p.open_until && p.open_until >= today && (!p.open_from || p.open_from <= today)) {
      return { cls: 'open', rank: 0, text: 'التقديم مفتوح الآن حتى ' + R.fullDate(p.open_until) };
    }
    if (p.open_from && p.open_from > today) {
      return { cls: 'soon', rank: 2, text: 'يفتح التقديم في ' + R.fullDate(p.open_from) };
    }
    if (p.open_signal) return { cls: 'open', rank: 0, text: 'الصفحة الرسمية تقول إن التقديم متاح الآن' };
    if (p.rolling) return { cls: 'open', rank: 1, text: 'التقديم متاح على مدار السنة' };
    const months = p.open_months || [];
    if (!months.length) return { cls: 'unknown', rank: 4, text: 'موعد الفتح غير ثابت، تابع الرابط الرسمي' };
    if (months.indexOf(now) !== -1) return { cls: 'open', rank: 1, text: 'يفتح التقديم عادة في هذا الشهر' };
    const ahead = months.map((m) => (m - now + 12) % 12).sort((a, b) => a - b)[0];
    if (ahead <= 2) return { cls: 'soon', rank: 2, text: 'يفتح عادة قريباً: ' + monthList(months) };
    return { cls: 'later', rank: 3, text: 'يفتح عادة في ' + monthList(months) };
  }

  function livePosts(kind) {
    return (R.state.jobs || []).filter((j) => {
      const t = j.title || '';
      return kind === 'scholarship' ? SCHOLARSHIP_RE.test(t) : (!SCHOLARSHIP_RE.test(t) && COOP_RE.test(t));
    }).slice(0, 6);
  }

  // Arabic text with an English term first ("IELTS 6 أو ...") must stay right-to-left
  function dirOf(text) { return /[؀-ۿ]/.test(text || '') ? 'rtl' : 'auto'; }

  function list(items) {
    return items && items.length ? '<ul class="prog-list">' + items.map((x) => '<li dir="' + dirOf(x) + '">' + esc(x) + '</li>').join('') + '</ul>' : '';
  }

  function row(label, value) {
    return value ? '<div class="prog-row"><dt>' + label + '</dt><dd dir="' + dirOf(value) + '">' + esc(value) + '</dd></div>' : '';
  }

  function card(p) {
    const st = status(p);
    const apply = R.safeUrl(p.apply_url) || R.safeUrl(p.source_url);
    const source = R.safeUrl(p.source_url);
    return '<article class="prog-card" id="p-' + esc(p.id) + '">' +
      '<div class="prog-top">' +
        '<div><h3 dir="' + dirOf(p.name) + '">' + esc(p.name) + '</h3><p class="prog-provider" dir="' + dirOf(p.provider) + '">' + esc(p.provider) + '</p></div>' +
        '<span class="badge badge-source">' + esc(PROVIDER_LABEL[p.provider_type] || '') + '</span>' +
      '</div>' +
      '<p class="prog-status st-' + st.cls + '"><span class="dot" aria-hidden="true"></span>' + esc(st.text) + '</p>' +
      (p.summary ? '<p class="prog-summary" dir="' + dirOf(p.summary) + '">' + esc(p.summary) + '</p>' : '') +
      '<details class="prog-details"><summary>الشروط والتفاصيل</summary>' +
        '<dl>' +
          row('لمن', p.who) + row('موعد الفتح المعتاد', p.opens) + row('آخر فتح رصدناه', p.last_opened) +
          row('المدة', p.duration) + row('التخصصات', (p.majors || []).join('، ')) +
          row('وجهات الدراسة', (p.destinations || []).join('، ')) + row('المدن', (p.cities || []).join('، ')) +
        '</dl>' +
        (p.conditions && p.conditions.length ? '<h4>الشروط</h4>' + list(p.conditions) : '') +
        (p.benefits && p.benefits.length ? '<h4>المزايا</h4>' + list(p.benefits) : '') +
        '<p class="prog-meta">' + (p.verified ? 'تحققنا من المعلومات في ' + esc(R.fullDate(p.verified)) : '') +
          (source && source !== apply ? ' · <a href="' + esc(source) + '" target="_blank" rel="noopener noreferrer">مصدر المعلومة</a>' : '') +
        '</p>' +
      '</details>' +
      (apply ? '<div class="prog-actions"><a class="btn btn-gold btn-sm" href="' + esc(apply) + '" target="_blank" rel="noopener noreferrer">الصفحة الرسمية ' + R.ICON.external + '</a></div>' : '') +
    '</article>';
  }

  function applyFilters(all, f) {
    const q = (f.q || '').trim().toLowerCase();
    return all.filter((p) => {
      if (f.type && p.provider_type !== f.type) return false;
      if (f.when === 'now' && status(p).rank > 2) return false;
      if (q) {
        const hay = [p.name, p.name_en, p.provider, p.summary, (p.majors || []).join(' ')].join(' ').toLowerCase();
        if (hay.indexOf(q) === -1) return false;
      }
      return true;
    }).sort((a, b) => status(a).rank - status(b).rank || a.name.localeCompare(b.name, 'ar'));
  }

  function renderResults(kind, all) {
    if (!document.getElementById('prog-results')) return;   // navigated away meanwhile
    const f = filters[kind];
    const shown = applyFilters(all, f);
    document.getElementById('prog-count').textContent =
      shown.length + ' ' + R.arCount(shown.length, 'برنامج', 'برامج', 'برنامجاً');
    document.getElementById('prog-results').innerHTML = shown.length
      ? shown.map(card).join('')
      : '<div class="state-box"><p>' + esc(PAGES[kind].empty) + '</p></div>';
  }

  async function renderPage(kind) {
    const page = PAGES[kind];
    R.setTitle(page.title);
    const f = filters[kind];
    const posts = livePosts(kind);
    const subscribed = R.state.profile && (R.state.profile.interests || []).indexOf(kind) !== -1;
    main.innerHTML = '' +
      '<section class="page container programs-page">' +
        '<div class="page-head">' +
          '<h1>' + esc(page.title) + '</h1>' +
          '<p>' + esc(page.intro) + '</p>' +
        '</div>' +
        '<nav class="prog-tabs" aria-label="أقسام الطلاب">' +
          '<a href="#/coop"' + (kind === 'coop' ? ' aria-current="page"' : '') + '>التدريب التعاوني</a>' +
          '<a href="#/scholarships"' + (kind === 'scholarship' ? ' aria-current="page"' : '') + '>الابتعاث</a>' +
        '</nav>' +
        (posts.length ? '<div class="prog-posts"><h2>' + esc(page.postsTitle) + '</h2><ul>' +
          posts.map((j) => '<li><a href="#/job/' + esc(j.id) + '" dir="auto">' + esc(j.title) + '</a>' +
            '<span class="muted">' + esc(j.company || j.source || '') + (j.posted_date ? ' · ' + esc(R.relativeDate(j.posted_date)) : '') + '</span></li>').join('') +
          '</ul></div>' : '') +
        '<div class="notice prog-alert"><p>' + esc(page.alert) + '</p>' +
          '<a href="#/register" class="btn btn-ghost btn-sm">' + (subscribed ? 'تنبيهاتي' : 'فعّل التنبيه') + '</a></div>' +
        '<div class="prog-filters" role="group" aria-label="تصفية البرامج">' +
          '<div class="prog-seg" role="group" aria-label="نوع الجهة">' +
            PROVIDERS.map((p) => '<button type="button" data-type="' + p[0] + '" aria-pressed="' + (f.type === p[0]) + '">' + esc(p[1]) + '</button>').join('') +
          '</div>' +
          '<div class="prog-filter-row">' +
            '<label class="checkbox"><input type="checkbox" id="prog-now"' + (f.when === 'now' ? ' checked' : '') + '><span>المفتوح الآن أو قريباً</span></label>' +
            '<input type="search" id="prog-q" placeholder="ابحث باسم البرنامج أو الجهة أو التخصص" value="' + esc(f.q) + '" aria-label="ابحث في البرامج">' +
          '</div>' +
        '</div>' +
        '<p class="prog-count" id="prog-count" aria-live="polite"></p>' +
        '<div class="prog-grid" id="prog-results"><div class="loading" role="status"><span class="spinner" aria-hidden="true"></span><span>جارٍ التحميل...</span></div></div>' +
        '<p class="prog-note">مواعيد الفتح المذكورة هي المعتادة حسب السنوات السابقة وقد تتغير. نراجع الصفحة الرسمية لكل برنامج كل 3 أيام، والمرجع النهائي دائماً هو الرابط الرسمي. رزق لا يستقبل طلبات التقديم.</p>' +
      '</section>';

    let all;
    try {
      all = (await loadPrograms()).programs.filter((p) => p.kind === kind);
    } catch (e) {
      document.getElementById('prog-results').innerHTML = '<div class="state-box"><p>تعذّر تحميل البرامج. أعد تحميل الصفحة.</p></div>';
      return;
    }
    if (R.state.view !== (kind === 'coop' ? 'coop' : 'scholarships')) return;   // user navigated away
    renderResults(kind, all);

    main.querySelectorAll('.prog-seg button').forEach((b) => b.addEventListener('click', () => {
      f.type = b.dataset.type;
      main.querySelectorAll('.prog-seg button').forEach((x) => x.setAttribute('aria-pressed', String(x === b)));
      renderResults(kind, all);
    }));
    document.getElementById('prog-now').addEventListener('change', (e) => {
      f.when = e.target.checked ? 'now' : '';
      renderResults(kind, all);
    });
    let t = null;
    document.getElementById('prog-q').addEventListener('input', (e) => {
      clearTimeout(t);
      t = setTimeout(() => { f.q = e.target.value; renderResults(kind, all); }, 150);
    });
  }

  R.addRoute(/^\/coop$/, 'coop', () => renderPage('coop'), 'coop');
  R.addRoute(/^\/scholarships$/, 'scholarships', () => renderPage('scholarship'), 'scholarships');
})();
