/* RIZQ — "post a job" form (#/post) and admin dashboard (#/admin).
   Uses the shared helpers exposed by js/app.js as window.Rizq. */
'use strict';

(function () {
  const R = window.Rizq;
  const esc = R.esc;
  const main = R.main;

  const POST_STATUS = { pending: ['بانتظار المراجعة', 'badge-source'], approved: ['منشور', 'badge-ok'], rejected: ['مرفوض', 'badge-warn'] };
  const SOURCE_STATUS = {
    ok: ['نجح', 'st-ok'], empty: ['بدون نتائج', 'st-warn'], error: ['خطأ', 'st-bad'],
    blocked: ['محجوب', 'st-bad'], disallowed: ['غير مسموح (robots)', 'st-warn'], skipped: ['متوقف', 'st-off']
  };
  // Mirrors backend/posts.py limits
  const LIMITS = { company: [2, 100], title: [3, 120], description: [30, 3000] };
  const EMAIL_RE = /^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,180}\.[A-Za-z]{2,24}$/;
  const URL_RE = /^https?:\/\/[A-Za-z0-9.-]+\.[A-Za-z]{2,}(:\d{1,5})?(\/\S*)?$/;

  // "label" and "hint" are trusted static markup; never pass user data into them
  function field(id, label, input, hint, full) {
    return '<div class="field' + (full ? ' full' : '') + '"><label for="' + id + '">' + label + '</label>' + input +
      (hint ? '<p class="hint" id="h-' + id + '">' + hint + '</p>' : '') + '<p class="error" id="e-' + id + '"></p></div>';
  }

  function val(id) {
    const el = document.getElementById(id);
    return el ? R.cleanText(el.value) : '';
  }

  // =====================================================================
  // Post a job (no accounts, moderated)
  // =====================================================================
  function renderPost() {
    R.setTitle('أعلن عن وظيفة');
    main.innerHTML = '' +
      '<section class="page container">' +
        '<div class="form-wrap">' +
          '<div class="page-head">' +
            '<h1>أعلن عن وظيفة</h1>' +
            '<p>عندك وظيفة شاغرة؟ انشرها مجاناً، وتظهر مع بقية الوظائف بعلامة «إعلان مباشر».</p>' +
          '</div>' +
          '<ul class="post-rules">' +
            '<li>' + R.ICON.check + '<span>مجاني وبدون حساب.</span></li>' +
            '<li>' + R.ICON.check + '<span>نراجع كل إعلان قبل نشره.</span></li>' +
            '<li>' + R.ICON.check + '<span>يتقدم الباحث إليك مباشرة عبر رابطك أو بريدك، ورزق لا يستقبل الطلبات.</span></li>' +
            '<li>' + R.ICON.shield + '<span>لا نقبل أي إعلان يطلب رسوماً أو مبالغ من المتقدمين.</span></li>' +
          '</ul>' +
          '<form class="form-card" id="post-form" novalidate>' +
            '<div class="form-grid">' +
              field('p-company', 'اسم الجهة<span class="req" aria-hidden="true">*</span>', '<input type="text" id="p-company" maxlength="100" autocomplete="organization" placeholder="مثال: شركة النخبة للتجارة">') +
              field('p-city', 'المدينة<span class="req" aria-hidden="true">*</span>', '<select id="p-city">' + R.optionList(R.CITIES, '', 'اختر المدينة') + '</select>') +
              field('p-title', 'المسمى الوظيفي<span class="req" aria-hidden="true">*</span>', '<input type="text" id="p-title" maxlength="120" placeholder="مثال: محاسب">', '', true) +
              field('p-desc', 'وصف الوظيفة<span class="req" aria-hidden="true">*</span>', '<textarea id="p-desc" maxlength="3000" aria-describedby="h-p-desc" placeholder="المهام، والمتطلبات، وساعات العمل..."></textarea>',
                'من 30 إلى 3000 حرف.', true) +
              '<fieldset class="field full apply-kind">' +
                '<legend class="label">طريقة التقديم<span class="req" aria-hidden="true">*</span></legend>' +
                '<div class="seg" role="radiogroup">' +
                  '<label><input type="radio" name="p-kind" value="url" checked><span>رابط تقديم</span></label>' +
                  '<label><input type="radio" name="p-kind" value="email"><span>بريد تقديم</span></label>' +
                '</div>' +
              '</fieldset>' +
              '<div class="full" id="kind-url">' +
                field('p-url', 'رابط التقديم', '<input type="url" id="p-url" class="ltr-input" maxlength="500" placeholder="https://" inputmode="url">', 'صفحة التقديم في موقعكم أو أي رابط تقديم رسمي.', true) +
              '</div>' +
              '<div class="full" id="kind-email" hidden>' +
                field('p-email', 'بريد التقديم', '<input type="email" id="p-email" class="ltr-input" maxlength="120" placeholder="jobs@company.sa" inputmode="email">', 'يظهر للباحثين ليرسلوا سيرهم إليه مباشرة.', true) +
              '</div>' +
              field('p-contact', 'بريد التواصل مع إدارة رزق<span class="req" aria-hidden="true">*</span>',
                '<input type="email" id="p-contact" class="ltr-input" maxlength="120" autocomplete="email" placeholder="you@company.sa" aria-describedby="h-p-contact">',
                'خاص بالإدارة ولا يظهر للعامة. نستخدمه فقط إذا احتجنا التواصل معك بخصوص الإعلان.', true) +
              // Honeypot: hidden from people and assistive tech; bots tend to fill it
              '<div class="hp" aria-hidden="true"><label for="p-website">الموقع</label><input type="text" id="p-website" name="website" tabindex="-1" autocomplete="off"></div>' +
              '<div class="field full">' +
                '<label class="checkbox"><input type="checkbox" id="p-pledge"><span>أؤكد أن الإعلان حقيقي، وأن التقديم مجاني ولا يُطلب فيه أي مبلغ من المتقدمين.</span></label>' +
                '<p class="error" id="e-p-pledge"></p>' +
              '</div>' +
            '</div>' +
            '<p class="error" id="post-error" role="alert"></p>' +
            '<div class="form-actions"><button type="submit" class="btn btn-gold btn-lg btn-block" id="post-submit">أرسل الإعلان للمراجعة</button></div>' +
          '</form>' +
        '</div>' +
      '</section>';

    const form = document.getElementById('post-form');
    function kind() { return form.querySelector('input[name="p-kind"]:checked').value; }
    form.querySelectorAll('input[name="p-kind"]').forEach((r) => r.addEventListener('change', () => {
      document.getElementById('kind-url').hidden = kind() !== 'url';
      document.getElementById('kind-email').hidden = kind() !== 'email';
    }));

    function setError(id, msg) {
      const el = document.getElementById('e-' + id);
      if (el) el.textContent = msg;
      const input = document.getElementById(id);
      if (input) { if (msg) input.setAttribute('aria-invalid', 'true'); else input.removeAttribute('aria-invalid'); }
    }
    ['p-company', 'p-title', 'p-desc', 'p-url', 'p-email', 'p-contact'].forEach((id) => {
      document.getElementById(id).addEventListener('input', () => setError(id, ''));
    });
    document.getElementById('p-city').addEventListener('change', () => setError('p-city', ''));
    document.getElementById('p-pledge').addEventListener('change', () => setError('p-pledge', ''));

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      document.getElementById('post-error').textContent = '';
      const description = document.getElementById('p-desc').value.replace(/[\u200E\u200F\u202A-\u202E\u2066-\u2069]/g, '').trim();
      const body = {
        company: val('p-company'), title: val('p-title'), city: val('p-city'), description: description,
        apply_url: kind() === 'url' ? val('p-url') : '', apply_email: kind() === 'email' ? val('p-email') : '',
        contact_email: val('p-contact'), website: document.getElementById('p-website').value
      };
      const errors = {};
      const chars = (n) => (n === 2 ? 'حرفان' : n <= 10 ? n + ' أحرف' : n + ' حرفاً');
      const len = (k, v, label) => {
        if (v.length < LIMITS[k][0]) return label + ' قصير جداً (' + chars(LIMITS[k][0]) + ' على الأقل).';
        if (v.length > LIMITS[k][1]) return label + ' أطول من المسموح.';
        return '';
      };
      errors['p-company'] = len('company', body.company, 'اسم الجهة');
      errors['p-title'] = len('title', body.title, 'المسمى الوظيفي');
      errors['p-desc'] = len('description', body.description, 'وصف الوظيفة');
      errors['p-city'] = body.city ? '' : 'اختر المدينة.';
      if (kind() === 'url') errors['p-url'] = URL_RE.test(body.apply_url) ? '' : 'اكتب رابط التقديم كاملاً، ويبدأ بـ https أو http.';
      else errors['p-email'] = EMAIL_RE.test(body.apply_email) ? '' : 'اكتب بريد تقديم صحيحاً.';
      errors['p-contact'] = EMAIL_RE.test(body.contact_email) ? '' : 'اكتب بريد تواصل صحيحاً.';
      errors['p-pledge'] = document.getElementById('p-pledge').checked ? '' : 'نحتاج تأكيدك قبل الإرسال.';
      Object.keys(errors).forEach((k) => setError(k, errors[k]));
      const first = Object.keys(errors).find((k) => errors[k]);
      if (first) { document.getElementById(first).focus(); return; }

      const btn = document.getElementById('post-submit');
      R.busy(btn, true, 'جارٍ الإرسال...');
      const res = await R.api('POST', '/api/posts', body);
      R.busy(btn, false);
      if (!res.ok) { document.getElementById('post-error').textContent = res.data.error || 'تعذّر الإرسال، حاول مرة أخرى.'; return; }
      renderPostSent();
    });
  }

  function renderPostSent() {
    R.setTitle('وصل إعلانك');
    main.innerHTML = '' +
      '<section class="page container">' +
        '<div class="form-wrap">' +
          '<div class="form-card success">' +
            '<div class="success-icon">' + R.ICON.check + '</div>' +
            '<h2>وصلنا إعلانك، شكراً لك</h2>' +
            '<p>نراجعه قريباً، وبعد اعتماده يظهر مع بقية الوظائف بعلامة «إعلان مباشر».</p>' +
            '<p>إذا احتجنا شيئاً نتواصل معك على البريد الذي كتبته.</p>' +
            '<div class="actions">' +
              '<a href="#/jobs" class="btn btn-gold btn-lg">تصفّح الوظائف</a>' +
              '<button type="button" class="btn btn-ghost btn-lg" id="post-another">أعلن عن وظيفة أخرى</button>' +
            '</div>' +
          '</div>' +
        '</div>' +
      '</section>';
    document.getElementById('post-another').addEventListener('click', renderPost);
    window.scrollTo(0, 0);
    main.focus({ preventScroll: true });
  }

  // =====================================================================
  // Admin dashboard
  // =====================================================================
  let pollTimer = 0;

  async function renderAdmin() {
    R.setTitle('لوحة الإدارة');
    clearTimeout(pollTimer);
    const res = await R.api('GET', '/api/admin/overview');
    if (res.status === 401) { renderAdminLogin(); return; }
    if (!res.ok) {
      main.innerHTML = '<div class="container"><div class="state-box"><h1>لوحة الإدارة</h1><p>' + esc(res.data.error || 'تعذّر التحميل.') + '</p></div></div>';
      return;
    }
    renderAdminDashboard(res.data);
  }

  function renderAdminLogin() {
    main.innerHTML = '' +
      '<section class="page container"><div class="form-wrap narrow">' +
        '<div class="page-head"><h1>لوحة الإدارة</h1><p>كلمة المرور موجودة في ملف الإعدادات <span class="ltr">.env</span> داخل مجلد المشروع.</p></div>' +
        '<form class="form-card" id="ad-form">' +
          field('ad-pass', 'كلمة المرور', '<input type="password" id="ad-pass" class="ltr-input" autocomplete="current-password">', '', true) +
          '<p class="error" id="ad-error" role="alert"></p>' +
          '<div class="form-actions"><button type="submit" class="btn btn-gold btn-lg btn-block" id="ad-submit">دخول</button></div>' +
        '</form>' +
      '</div></section>';
    document.getElementById('ad-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const btn = document.getElementById('ad-submit');
      R.busy(btn, true, 'لحظة...');
      const res = await R.api('POST', '/api/admin/login', { password: document.getElementById('ad-pass').value });
      R.busy(btn, false);
      if (!res.ok) { document.getElementById('ad-error').textContent = res.data.error || 'تعذّر الدخول.'; return; }
      renderAdmin();
    });
    document.getElementById('ad-pass').focus();
  }

  function statusBadge(s) {
    const v = SOURCE_STATUS[s] || ['لم يعمل بعد', 'st-off'];
    return '<span class="st ' + v[1] + '">' + v[0] + '</span>';
  }

  function hoursLabel(h) {
    return { 1: 'كل ساعة', 2: 'كل ساعتين' }[h] || ('كل ' + h + (h <= 10 ? ' ساعات' : ' ساعة'));
  }

  // "how" may contain trusted static markup (ltr spans); never pass data into it
  function featureRow(label, on, how) {
    return '<li><span>' + label + '</span>' + (on ? '<span class="st st-ok">مفعّل</span>' : '<span class="st st-off">غير مفعّل</span>') +
      (!on && how ? '<small class="muted">' + how + '</small>' : '') + '</li>';
  }

  function postRow(x) {
    const st = POST_STATUS[x.status] || [x.status, 'badge-source'];
    const isMail = String(x.url || '').startsWith('mailto:');
    const target = R.safeUrl(x.url || '', true);
    const id = esc(x.id);
    return '<li class="row-card">' +
      '<div class="row-main"><strong>' + esc(x.title) + '</strong>' +
        '<span class="muted">' + esc(x.company) + ' · ' + esc(x.city) + ' · ' + esc(R.fullDate(x.created_at)) + '</span>' +
        '<span class="muted">التقديم: ' + (target
          ? '<a href="' + esc(target) + '"' + (isMail ? '' : ' target="_blank" rel="noopener noreferrer"') + '><bdi dir="ltr">' + esc(isMail ? x.url.slice(7) : x.url) + '</bdi></a>'
          : '<bdi dir="ltr">' + esc(x.url) + '</bdi>') + '</span>' +
        '<span class="muted">بريد التواصل (خاص): <bdi dir="ltr">' + esc(x.contact_email) + '</bdi></span>' +
        '<details class="post-desc"><summary>وصف الوظيفة</summary><p>' + esc(x.description) + '</p></details>' +
        '<span class="badges"><span class="badge ' + st[1] + '">' + esc(st[0]) + '</span></span></div>' +
      '<div class="row-actions">' +
        (x.status !== 'approved' ? '<button type="button" class="btn btn-gold btn-sm" data-post="' + id + '" data-status="approved">اعتماد</button>' : '') +
        (x.status === 'approved' ? '<a class="btn btn-ghost btn-sm" href="#/job/' + encodeURIComponent(x.id) + '">عرض</a>' : '') +
        (x.status === 'pending' ? '<button type="button" class="btn-link" data-post="' + id + '" data-status="rejected">رفض</button>' : '') +
        (x.status === 'approved' ? '<button type="button" class="btn-link" data-post="' + id + '" data-status="rejected">إيقاف النشر</button>' : '') +
        '<button type="button" class="btn-link danger" data-post-del="' + id + '">حذف</button>' +
      '</div></li>';
  }

  function renderAdminDashboard(d) {
    const st = d.stats;
    const last = d.last_run;
    const f = d.features;
    const s = d.settings;
    const sp = s.support || { links: [] };
    const links = (sp.links || []).concat([{ label: '', url: '' }, { label: '', url: '' }]).slice(0, 3);

    main.innerHTML = '' +
      '<section class="page container admin">' +
        '<div class="page-head head-row">' +
          '<div><h1>لوحة الإدارة</h1><p>نظرة شاملة على رزق</p></div>' +
          '<div class="head-actions"><button type="button" class="btn btn-gold" id="ad-collect"' + (d.collecting ? ' disabled' : '') + '>' +
            (d.collecting ? 'جارٍ جمع الوظائف...' : 'اجمع الوظائف الآن') + '</button>' +
            '<button type="button" class="btn btn-ghost" id="ad-logout">خروج</button></div>' +
        '</div>' +

        '<ul class="stats stats-left">' +
          '<li><strong>' + st.jobs + '</strong><span>الوظائف الحقيقية</span></li>' +
          '<li><strong>' + st.users + '</strong><span>المشتركون في التنبيهات</span></li>' +
          '<li><strong>' + st.telegram_linked + '</strong><span>مفعّلو تيليجرام</span></li>' +
          '<li><strong>' + st.posts_pending + '</strong><span>إعلانات بانتظار المراجعة</span></li>' +
        '</ul>' +

        '<div class="admin-grid">' +
          '<div class="panel"><h2>آخر عملية جمع</h2>' +
            (last ? '<ul class="facts">' +
              '<li><span>الوقت</span><strong class="ltr">' + esc(String(last.finished_at || '').replace('T', ' ').slice(0, 16)) + '</strong></li>' +
              '<li><span>العناصر المقروءة</span><strong>' + last.items + '</strong></li>' +
              '<li><span>وظائف صالحة</span><strong>' + last.jobs + '</strong></li>' +
              '<li><span>وظائف جديدة</span><strong>' + last.added + '</strong></li>' +
              '<li><span>تنبيهات أُرسلت</span><strong>' + last.notified_users + '</strong></li>' +
              '<li><span>المدة</span><strong>' + last.seconds + ' ث</strong></li></ul>'
              : '<p class="muted">لم يتم الجمع بعد. اضغط «اجمع الوظائف الآن».</p>') +
          '</div>' +
          '<div class="panel"><h2>الخدمات</h2><ul class="facts feature-list">' +
            featureRow('الذكاء الاصطناعي (Groq)', f.groq, 'أضف المفتاح <span class="ltr">GROQ_API_KEY</span> في ملف الإعدادات') +
            featureRow('بوت تيليجرام', f.telegram_bot, 'أضف <span class="ltr">TELEGRAM_BOT_TOKEN</span> و<span class="ltr">TELEGRAM_BOT_USERNAME</span> في ملف الإعدادات') +
            featureRow('LinkedIn', f.linkedin, 'اضبط <span class="ltr">LINKEDIN_ENABLED=1</span> بوجود الإذن المكتوب') +
            featureRow('Careerjet', f.careerjet, 'مفتاح مجاني: أضف <span class="ltr">CAREERJET_API_KEY</span>') +
            featureRow('Jooble', f.jooble, 'مفتاح مجاني: أضف <span class="ltr">JOOBLE_API_KEY</span>') +
            featureRow('X (تويتر)', f.x_api, 'يتطلب مفتاحاً مدفوعاً <span class="ltr">X_BEARER_TOKEN</span>') +
            featureRow('قراءة السير بصيغة PDF', f.pdf_cv, 'ثبّت مكتبة pypdf لقراءة سير PDF (اختياري)') +
            '<li><span>الجمع التلقائي</span><strong>' + (f.auto_collect_hours ? hoursLabel(f.auto_collect_hours) : 'متوقف') + '</strong></li>' +
            '<li><span>حالة البوت</span><strong class="ltr">' + esc(d.bot_status) + '</strong></li>' +
            '<li><span>استهلاك Groq اليوم</span><strong>' + d.ai_usage.requests + ' من ' + d.ai_usage.request_limit + '</strong></li>' +
          '</ul></div>' +
        '</div>' +

        '<div class="panel"><h2>المصادر</h2><div class="table-wrap"><table class="table">' +
          '<thead><tr><th>المصدر</th><th>الحالة</th><th>مقروء</th><th>وظائف</th><th>جديد</th><th>آخر تشغيل</th><th>ملاحظة</th></tr></thead><tbody>' +
          d.sources.map((x) => '<tr' + (x.enabled ? '' : ' class="off"') + '><td>' + esc(x.name) + '</td><td>' + (x.enabled ? statusBadge(x.status) : '<span class="st st-off">معطّل</span>') +
            (x.zero_streak >= 3 ? ' <span class="st st-warn">تشغيلات متتالية بلا نتائج: ' + x.zero_streak + '</span>' : '') + '</td>' +
            '<td>' + (x.fetched == null ? '-' : x.fetched) + '</td><td>' + (x.jobs == null ? '-' : x.jobs) + '</td><td>' + (x.added == null ? '-' : x.added) + '</td>' +
            '<td class="ltr">' + esc(String(x.last_run || '-').replace('T', ' ').slice(0, 16)) + '</td><td class="note">' + esc(x.message || '') + '</td></tr>').join('') +
          '</tbody></table></div></div>' +

        '<div class="panel"><h2>إعلانات الوظائف المباشرة</h2>' +
          '<p class="muted mb-14">كل إعلان يبقى مخفياً حتى تعتمده. تأكد أنه حقيقي ولا يطلب أي رسوم من المتقدمين.</p>' +
          (d.posts.length ? '<ul class="row-list">' + d.posts.map(postRow).join('') + '</ul>' : '<p class="muted">لا توجد إعلانات بعد.</p>') +
        '</div>' +

        '<div class="panel"><h2>البلاغات</h2>' +
          (d.reports.length ? '<ul class="row-list">' + d.reports.map((r) =>
            '<li class="row-card"><div class="row-main"><strong>' + esc(r.job_title) + '</strong>' +
              '<span class="muted">' + esc(r.reason) + (r.note ? ' · ' + esc(r.note) : '') + ' · ' + esc(r.job_source) + '</span></div>' +
              '<div class="row-actions">' +
                '<a class="btn btn-ghost btn-sm" href="#/job/' + encodeURIComponent(r.job_id) + '">عرض</a>' +
                '<button type="button" class="btn btn-gold btn-sm" data-hide="' + esc(r.job_id) + '">إخفاء الوظيفة</button>' +
                '<button type="button" class="btn-link" data-dismiss="' + esc(r.id) + '">تجاهل</button>' +
              '</div></li>').join('') + '</ul>' : '<p class="muted">لا توجد بلاغات مفتوحة.</p>') +
        '</div>' +

          '<form class="panel" id="ad-support"><h2>صفحة «ادعم رزق»</h2>' +
            '<p class="muted mb-14">صفحة اختيارية وهادئة لدعم مبادرة فردية، والنص أدناه هو ما يراه الزائر.</p>' +
            '<label class="checkbox"><input type="checkbox" id="sp-on"' + (sp.enabled ? ' checked' : '') + '><span>إظهار الصفحة</span></label>' +
            field('sp-text', 'النص', '<textarea id="sp-text" class="short" maxlength="600">' + esc(sp.text || '') + '</textarea>') +
            links.map((l, i) => '<div class="link-pair">' +
              '<input type="text" id="sp-l' + i + '" maxlength="40" placeholder="اسم الرابط" value="' + esc(l.label) + '" aria-label="اسم الرابط ' + (i + 1) + '">' +
              '<input type="url" id="sp-u' + i + '" class="ltr-input" maxlength="300" placeholder="https://" value="' + esc(l.url) + '" aria-label="رابط ' + (i + 1) + '"></div>').join('') +
            '<button type="submit" class="btn btn-gold mt-12">حفظ</button>' +
          '</form>' +
      '</section>';

    document.getElementById('ad-logout').addEventListener('click', async () => {
      await R.api('POST', '/api/admin/logout');
      renderAdminLogin();
    });
    document.getElementById('ad-collect').addEventListener('click', async (e) => {
      const btn = e.currentTarget;
      R.busy(btn, true, 'جارٍ جمع الوظائف...');
      const res = await R.api('POST', '/api/admin/collect');
      if (!res.ok) { R.busy(btn, false); R.toast(res.data.error || 'تعذّر البدء.'); return; }
      R.toast(res.data.started ? 'بدأ جمع الوظائف. قد يستغرق بضع دقائق.' : 'الجمع يعمل حالياً.');
      pollCollect();
    });
    main.querySelector('.admin').addEventListener('click', onAdminAction);
    document.getElementById('ad-support').addEventListener('submit', async (e) => {
      e.preventDefault();
      const ls = [0, 1, 2].map((i) => ({ label: val('sp-l' + i), url: val('sp-u' + i) })).filter((l) => l.label || l.url);
      await saveSettings({ support: { enabled: document.getElementById('sp-on').checked, text: document.getElementById('sp-text').value.trim(), links: ls } });
    });
    if (d.collecting) pollCollect();
  }

  async function saveSettings(body) {
    const res = await R.api('POST', '/api/admin/settings', body);
    if (!res.ok) { R.toast(res.data.error || 'تعذّر الحفظ.'); return; }
    R.toast('تم الحفظ.');
    await R.reloadData();
    renderAdmin();
  }

  function pollCollect() {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(async () => {
      if (!location.hash.startsWith('#/admin')) return;
      const res = await R.api('GET', '/api/admin/overview');
      if (!res.ok) return;
      if (res.data.collecting) { pollCollect(); return; }
      const last = res.data.last_run;
      R.toast(last ? 'انتهى الجمع. الوظائف الجديدة: ' + last.added + '.' : 'انتهى الجمع.');
      await R.reloadData();
      renderAdminDashboard(res.data);
    }, 4000);
  }

  async function onAdminAction(e) {
    const t = e.target.closest('[data-post],[data-post-del],[data-hide],[data-dismiss]');
    if (!t) return;
    let res;
    if (t.dataset.post) {
      res = await R.api('POST', '/api/admin/posts/' + encodeURIComponent(t.dataset.post) + '/status', { status: t.dataset.status });
    } else if (t.dataset.postDel) {
      if (!window.confirm('حذف هذا الإعلان نهائياً؟')) return;
      res = await R.api('POST', '/api/admin/posts/' + encodeURIComponent(t.dataset.postDel) + '/delete');
    } else if (t.dataset.hide) {
      if (!window.confirm('إخفاء هذه الوظيفة من الموقع؟')) return;
      res = await R.api('POST', '/api/admin/jobs/' + encodeURIComponent(t.dataset.hide) + '/hide');
    } else if (t.dataset.dismiss) {
      res = await R.api('POST', '/api/admin/reports/' + encodeURIComponent(t.dataset.dismiss) + '/dismiss');
    }
    if (!res.ok) { R.toast(res.data.error || 'تعذّر تنفيذ العملية.'); return; }
    await R.reloadData();
    renderAdmin();
  }

  // ---------- Routes ----------
  R.addRoute(/^\/post$/, 'post', renderPost, 'post', true);
  // Old company-portal links land on the new form
  R.addRoute(/^\/company(?:\/.*)?$/, 'post', renderPost, 'post', true);
  R.addRoute(/^\/admin$/, 'admin', renderAdmin, '', true);
})();
