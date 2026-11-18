/* Munfarid owner dashboard (vanilla JS, no build). Login through the owner's Telegram, then
   five pages fed by /api/admin/dash/*. "Live": /api/admin/dash/live is polled every 10 s while the
   tab is visible; new events and jobs refresh the page that is open. */
(function () {
  'use strict';

  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
  const LIVE_MS = 10000;
  const KIND_LABEL = {
    direct: 'مباشر من المصدر',
    relay_resolved: 'تخطّينا الإعلانات وجبنا الأصلي',
    relay_pending: 'بانتظار الرابط الأصلي (مخفية)'
  };
  const STATUS_LABEL = { live: 'حالية', expired: 'منتهية', archived: 'أرشيف' };
  const nf = new Intl.NumberFormat('en-US');

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }
  // Only http(s) links leave the page (mailto when asked); anything else is not a link
  function safeUrl(u, allowMail) {
    const s = String(u || '').trim();
    if (!s) return '';
    if (allowMail && /^mailto:[^\s<>"]+$/i.test(s)) return s;
    try {
      const p = new URL(s, location.href);
      return p.protocol === 'https:' || p.protocol === 'http:' ? p.href : '';
    } catch (e) { return ''; }
  }
  const n = (x) => nf.format(x || 0);

  function ago(iso) {
    if (!iso) return 'لم يحدث بعد';
    const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (s < 60) return 'قبل لحظات';
    if (s < 3600) return `قبل ${Math.round(s / 60)} دقيقة`;
    if (s < 86400) return `قبل ${Math.round(s / 3600)} ساعة`;
    return `قبل ${Math.round(s / 86400)} يوم`;
  }
  // Time of an event: "14:05" today, "11/16 14:05" on another day
  const hhmm = (iso) => {
    if (!iso) return '';
    const d = new Date(iso);
    const t = d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
    return d.toDateString() === new Date().toDateString() ? t : `${d.getMonth() + 1}/${d.getDate()} ${t}`;
  };

  async function api(method, path, body) {
    const opt = { method, headers: {}, credentials: 'same-origin' };
    if (body) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
    const r = await fetch(path, opt);
    let d = {};
    try { d = await r.json(); } catch (e) { /* not JSON */ }
    if (r.status === 401) { showLogin(); throw new Error('login'); }
    if (!r.ok || d.ok === false) throw new Error(d.error || 'تعذّر الاتصال');
    return d;
  }

  // ---------------------------------------------------------------- tooltip
  const tip = $('#tip');
  function showTip(e, html) {
    tip.innerHTML = html;
    tip.hidden = false;
    const x = Math.min(e.clientX + 14, window.innerWidth - tip.offsetWidth - 8);
    tip.style.left = Math.max(8, x) + 'px';
    tip.style.top = (e.clientY + 14) + 'px';
  }
  const hideTip = () => { tip.hidden = true; };

  // ---------------------------------------------------------------- charts
  // Daily bars (one series): thin bars with 2px gaps, rounded tops, recessive grid, hover tooltip.
  function dayBars(el, series, unit) {
    if (!el) return;
    if (!series || !series.length || series.every((d) => !d.n)) { el.innerHTML = '<p class="empty">لا توجد بيانات بعد</p>'; return; }
    const W = 600, H = el.clientHeight || 200, pad = { t: 10, r: 6, b: 22, l: 42 };
    const max = Math.max(...series.map((d) => d.n), 1);
    const step = niceStep(max);
    const top = Math.ceil(max / step) * step;
    const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
    const bw = iw / series.length;
    const y = (v) => pad.t + ih - (v / top) * ih;
    let grid = '', bars = '', hits = '', labels = '';
    for (let v = 0; v <= top; v += step) {
      grid += `<line x1="${pad.l}" x2="${W - pad.r}" y1="${y(v)}" y2="${y(v)}"/>`;
      labels += `<text x="${pad.l - 6}" y="${y(v) + 4}" text-anchor="end">${n(v)}</text>`;
    }
    // RTL page, but time runs left -> right like every chart reader expects
    series.forEach((d, i) => {
      const x = pad.l + i * bw;
      const h = Math.max(0, ih - (y(d.n) - pad.t));
      const w = Math.max(1, bw - 2);
      if (d.n) {
        const r = Math.min(4, w / 2, h);
        bars += `<path class="bar" data-i="${i}" d="M${x + 1},${pad.t + ih} v${-(h - r)} q0,${-r} ${r},${-r} h${w - 2 * r} q${r},0 ${r},${r} v${h - r} z"/>`;
      }
      hits += `<rect class="hit" data-i="${i}" x="${x}" y="${pad.t}" width="${bw}" height="${ih}"/>`;
      if (i % 5 === 0 || i === series.length - 1) {
        labels += `<text x="${x + bw / 2}" y="${H - 6}" text-anchor="middle">${d.day.slice(5).replace('-', '/')}</text>`;
      }
    });
    el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="${esc(unit)} يومياً">
      <g class="grid">${grid}</g><g>${bars}</g><g class="axis">${labels}</g><g>${hits}</g></svg>`;
    $$('.hit', el).forEach((r) => {
      r.addEventListener('mousemove', (e) => {
        const d = series[+r.dataset.i];
        $$('.bar', el).forEach((b) => b.classList.toggle('hl', b.dataset.i === r.dataset.i));
        showTip(e, `<b>${esc(d.day)}</b><br>${n(d.n)} ${esc(unit)}`);
      });
      r.addEventListener('mouseleave', () => { hideTip(); $$('.bar', el).forEach((b) => b.classList.remove('hl')); });
    });
  }
  function niceStep(max) {
    const raw = max / 4;
    const p = Math.pow(10, Math.floor(Math.log10(raw || 1)));
    const m = raw / p;
    return Math.max(1, (m <= 1 ? 1 : m <= 2 ? 2 : m <= 5 ? 5 : 10) * p);
  }
  // Ranked horizontal bars (HTML): label, bar, value
  function hbars(el, pairs, unit) {
    if (!el) return;
    if (!pairs || !pairs.length) { el.innerHTML = '<p class="empty">لا توجد بيانات بعد</p>'; return; }
    const max = Math.max(...pairs.map((p) => p[1]), 1);
    el.innerHTML = '<div class="hbars">' + pairs.map(([label, v]) =>
      `<div class="hbar" title="${esc(label || 'غير محدد')}: ${n(v)} ${esc(unit || '')}"><span class="lbl">${esc(label || 'غير محدد')}</span>` +
      `<span class="track"><span class="fill" style="width:${Math.max(2, (v / max) * 100)}%"></span></span>` +
      `<span class="val">${n(v)}</span></div>`).join('') + '</div>';
  }
  // Part-to-whole: one stacked bar (2px gaps) + a legend with values (identity never by color alone)
  function kindsStack(el, kinds) {
    const keys = Object.keys(KIND_LABEL);
    const total = keys.reduce((s, k) => s + (kinds[k] || 0), 0);
    if (!total) { el.innerHTML = '<p class="empty">لا توجد وظائف حالية بعد</p>'; return; }
    el.innerHTML = '<div class="stack">' + keys.filter((k) => kinds[k]).map((k) =>
      `<span class="k-${k}" style="width:${(kinds[k] / total) * 100}%" title="${esc(KIND_LABEL[k])}: ${n(kinds[k])}"></span>`).join('') +
      '</div><div class="legend">' + keys.map((k) =>
      `<button class="row drill-link" type="button" ${drillAttr(KIND_LABEL[k], { kind: k, sort: k === 'relay_resolved' ? 'resolved' : '' })}>` +
      `<span class="sw k-${k}"></span>${esc(KIND_LABEL[k])}<span class="val">${n(kinds[k])} · ${Math.round(((kinds[k] || 0) / total) * 100)}%</span></button>`).join('') + '</div>';
  }
  function kpi(label, value, sub, cls) {
    return `<div class="kpi"><div class="k-label">${esc(label)}</div><div class="k-value">${value}</div>` +
      `<div class="k-sub ${cls || ''}">${sub || '&nbsp;'}</div></div>`;
  }
  // A number that opens the jobs behind it (see the details dialog)
  function drillAttr(title, params) { return `data-drill="${esc(JSON.stringify({ title, params }))}"`; }
  function dl(value, title, params) {
    return `<button class="drill-link" type="button" ${drillAttr(title, params)}>${value}</button>`;
  }
  function kpiDrill(label, value, sub, title, params) {
    return `<button class="kpi" type="button" ${drillAttr(title, params)}><div class="k-label">${esc(label)}</div>` +
      `<div class="k-value">${value}</div><div class="k-sub">${sub || '&nbsp;'}</div></button>`;
  }
  // The Riyadh calendar day n days ago, YYYY-MM-DD (the server counts days in Riyadh time too)
  function dayAgo(nDays) {
    return new Date(Date.now() - nDays * 86400000).toLocaleDateString('en-CA', { timeZone: 'Asia/Riyadh' });
  }
  const NONE = '__none__';   // the filter value for "not stated" (empty city or source)
  const ALL_STATUS = 'live|expired|archived';
  function table(el, head, rows, numCols) {
    if (!rows.length) { el.innerHTML = '<tbody><tr><td class="empty">لا توجد بيانات بعد</td></tr></tbody>'; return; }
    const nc = new Set(numCols || []);
    el.innerHTML = '<thead><tr>' + head.map((h) => `<th>${esc(h)}</th>`).join('') + '</tr></thead><tbody>' +
      rows.map((r) => '<tr>' + r.map((c, i) => `<td class="${nc.has(i) ? 'n' : ''}">${c}</td>`).join('') + '</tr>').join('') + '</tbody>';
  }
  function badge(state, text) {
    const icon = { good: '●', bad: '■', warn: '▲', idle: '○' }[state] || '○';
    return `<span class="badge ${state}">${icon} ${esc(text)}</span>`;
  }

  // ---------------------------------------------------------------- login
  let pollTimer = null;
  function showLogin() {
    stopLive();
    $('#app').hidden = true;
    $('#login').hidden = false;
    api('GET', '/api/admin/whoami').then((w) => {
      $('#login-tg').hidden = !w.owner_linked;
      $('#login-pw').hidden = !w.password_login;
      $('#login-claim').hidden = w.owner_linked || w.password_login;
    }).catch(() => {});
  }
  $('#tg-start').addEventListener('click', async () => {
    $('#login-msg').textContent = '';
    try {
      const d = await api('POST', '/api/admin/tg-login/start');
      $('#tg-code').textContent = d.code;
      $('#tg-link').href = safeUrl(d.link);
      $('#tg-wait').hidden = false;
      window.open(safeUrl(d.link), '_blank', 'noopener');
      const until = Date.now() + 5 * 60 * 1000;
      clearInterval(pollTimer);
      pollTimer = setInterval(async () => {
        if (Date.now() > until) { clearInterval(pollTimer); $('#login-msg').textContent = 'انتهت المهلة. حاول مرة أخرى.'; $('#tg-wait').hidden = true; return; }
        try {
          const c = await api('POST', '/api/admin/tg-login/check', { ticket: d.ticket });
          if (c.state === 'approved') { clearInterval(pollTimer); start(); }
          else if (c.state !== 'pending') { clearInterval(pollTimer); $('#tg-wait').hidden = true; $('#login-msg').textContent = c.state === 'denied' ? 'رُفضت محاولة الدخول.' : 'انتهت صلاحية المحاولة.'; }
        } catch (e) { /* keep waiting */ }
      }, 2000);
    } catch (e) { $('#login-msg').textContent = e.message; }
  });
  $('#login-pw').addEventListener('submit', async (e) => {
    e.preventDefault();
    try { await api('POST', '/api/admin/login', { password: $('#pw').value }); $('#pw').value = ''; start(); }
    catch (err) { $('#login-msg').textContent = err.message; }
  });
  $('#logout').addEventListener('click', async () => { try { await api('POST', '/api/admin/logout'); } catch (e) { /* ignore */ } showLogin(); });
  $('#link-tg').addEventListener('click', async () => {
    try { const d = await api('POST', '/api/admin/owner-link'); window.open(safeUrl(d.link), '_blank', 'noopener'); banner('افتح البوت واضغط «ابدأ» لربط حسابك. بعدها يكون الدخول عبر تيليجرام فقط.'); }
    catch (e) { banner(e.message); }
  });
  let bannerIsError = false;
  function banner(t, isError) { $('#banner').textContent = t; $('#banner').hidden = !t; bannerIsError = !!(t && isError); }

  // ---------------------------------------------------------------- pages
  let page = 'home';
  const loaded = {};
  $$('#tabs .tab').forEach((b) => b.addEventListener('click', () => go(b.dataset.page)));
  document.addEventListener('click', (e) => { const b = e.target.closest('[data-go]'); if (b) go(b.dataset.go); });
  function go(p) {
    page = p;
    $$('#tabs .tab').forEach((b) => b.classList.toggle('active', b.dataset.page === p));
    $$('.page').forEach((s) => { s.hidden = s.id !== 'page-' + p; });
    try { localStorage.setItem('dash-page', p); } catch (e) { /* private mode */ }
    refresh(true);
  }
  async function refresh(force) {
    const fn = { home: loadHome, jobs: loadJobs, sources: loadSources, users: loadUsers }[page];
    if (!fn) return;
    if (!force && loaded[page] && Date.now() - loaded[page] < 15000) return;
    loaded[page] = Date.now();
    try {
      await fn();
      if (bannerIsError) banner('');
    } catch (e) { if (e.message !== 'login') banner(e.message, true); }
  }

  function statusCards(b) {
    const botState = b.ok === true ? ['good', 'شغّال'] : b.ok === false ? ['bad', 'متوقف'] : ['idle', 'غير معروف'];
    const w = b.webhook || {};
    const botSub = w.error ? 'تعذّر سؤال تيليجرام الآن' :
      `آخر رسالة عالجها ${ago(b.last_update_at)}` + (w.pending ? ` · بانتظار ${n(w.pending)}` : '') +
      (w.last_error ? ` · آخر خطأ: ${esc(w.last_error)}` : '');
    const helper = b.helper || {};
    const got = Object.entries(helper.sources || {}).map(([k, v]) => `${k}: ${typeof v === 'number' ? n(v) : v}`).join('، ');
    return `<div class="status">${badge(...botState)}<div><b>البوت</b><span class="muted small">${botSub}</span></div></div>` +
      `<div class="status">${badge(b.cloud_ok ? 'good' : 'bad', b.cloud_ok ? 'يعمل' : 'متأخر')}<div><b>الجمع السحابي (كل ساعة)</b><span class="muted small">آخر تشغيل ${ago(b.cloud_last_run)}</span></div></div>` +
      `<div class="status">${badge(b.helper_ok ? 'good' : (helper.at ? 'warn' : 'idle'), b.helper_ok ? 'يعمل' : (helper.at ? 'متأخر' : 'لم يبدأ'))}<div><b>جهازي (مرة يومياً)</b><span class="muted small">${helper.at ? 'آخر تشغيل ' + ago(helper.at) + (got ? ' · ' + esc(got) : '') : 'لم يشتغل بعد على الجهاز القديم'}</span></div></div>`;
  }

  async function loadHome() {
    const d = await api('GET', '/api/admin/dash/overview');
    const j = d.jobs, p = d.people, t = d.today;
    $('#status-row').innerHTML = statusCards(d.bot);
    const probs = d.problems || [];
    $('#problems').hidden = !probs.length;
    $('#problems').innerHTML = probs.length ? `⚠️ ${n(probs.length)} مصدر تعثّر في آخر تشغيل: ${esc(probs.slice(0, 6).join('، '))}${probs.length > 6 ? '…' : ''} ` +
      '<button class="drill-link" type="button" data-go="sources">التفاصيل في صفحة المصادر</button>' : '';
    const wk = j.week_change == null ? '' : (j.week_change >= 0 ? `<span class="up">▲ ${j.week_change}%</span>` : `<span class="down">▼ ${Math.abs(j.week_change)}%</span>`) + ' عن الأسبوع السابق';
    $('#kpis').innerHTML =
      kpiDrill('الوظائف الحالية', n(j.live), `كل ما جُمع: ${n(j.all)}`, 'الوظائف الحالية', {}) +
      kpiDrill('جديد اليوم', n(j.today), `أمس: ${n(j.yesterday)}`, 'وظائف جديدة اليوم', { status: ALL_STATUS, from: dayAgo(0) }) +
      kpiDrill('جديد آخر 7 أيام', n(j.week), wk, 'وظائف آخر 7 أيام', { status: ALL_STATUS, from: dayAgo(6) }) +
      kpiDrill('في الأرشيف', n(j.archived), `منتهية (أقل من 3 أشهر): ${n(j.expired)}`, 'الأرشيف (أكثر من 3 أشهر)', { status: 'archived' }) +
      kpi('جرّبوا البوت', n(p.tried), `جدد اليوم: ${n(p.new_today)}`) +
      kpi('مشتركون الآن', n(p.subscribers), `أضافوا مسمى: ${n(p.with_titles)} · مجموعات/قنوات: ${n(p.feeds)}`) +
      kpi('نشطون آخر 7 أيام', n(p.active7), `بحث اليوم: ${n(t.searches)}`) +
      kpi('ضغطات الروابط اليوم', n(t.clicks), `وظائف أُرسلت اليوم: ${n(t.alerts)}`);
    dayBars($('#ch-jobs-day'), j.per_day, 'وظيفة جديدة');
    kindsStack($('#ch-kinds'), j.kinds);
    $('#kinds-extra').textContent = `جابها جهازي: ${n(j.via_helper)} · أصلها جدارات: ${n(j.via_jadarat)}`;
    hbars($('#top-sources'), d.top_sources, 'وظيفة');
    hbars($('#top-titles'), d.top_titles, 'شخص');
    hbars($('#top-cities'), d.top_cities, 'شخص');
    dayBars($('#ch-users-day'), p.per_day, 'مستخدم جديد');
    setLiveStatus(d.bot);
  }

  // ---------------------------------------------------------------- jobs page
  const filt = { city: [], exp: [], source: [], kind: [], status: ['live'] };
  let jobsPage = 0;
  let facets = {};
  function filterQuery(extra) {
    const q = new URLSearchParams();
    const text = $('#f-q').value.trim();
    if (text) q.set('q', text);
    Object.entries(filt).forEach(([k, v]) => { if (v.length) q.set(k, v.join('|')); });
    if ($('#f-from').value) q.set('from', $('#f-from').value);
    if ($('#f-to').value) q.set('to', $('#f-to').value);
    if ($('#f-helper').checked) q.set('helper', '1');
    Object.entries(extra || {}).forEach(([k, v]) => q.set(k, v));
    return q.toString();
  }
  function msOptions(key) {
    const f = facets || {};
    if (key === 'city') return (f.cities || []).map(([v, c]) => [v || NONE, v || 'غير محدد', c]);
    if (key === 'source') return (f.sources || []).map(([v, c]) => [v || NONE, v || 'غير محدد', c]);
    if (key === 'exp') return (f.exp || []).map(([v, c]) => [v, v, c]);
    if (key === 'kind') return (f.kinds || []).map(([v, c]) => [v, KIND_LABEL[v] || v, c]);
    if (key === 'status') return Object.entries(STATUS_LABEL).map(([v, l]) => [v, l, null]);
    return [];
  }
  function renderMs() {
    $$('.ms').forEach((box) => {
      const key = box.dataset.key, sel = filt[key];
      let btn = $('.ms-btn', box);
      if (!btn) {
        box.innerHTML = '<button class="ms-btn" type="button" aria-haspopup="true"></button><div class="ms-panel" hidden></div>';
        btn = $('.ms-btn', box);
        $('.ms-panel', box).addEventListener('click', (e) => e.stopPropagation());
        btn.addEventListener('click', (e) => {
          e.stopPropagation();
          const panel = $('.ms-panel', box);
          const open = panel.hidden;
          $$('.ms-panel').forEach((p) => { p.hidden = true; });
          if (open) { fillPanel(box); panel.hidden = false; const s = $('input[type=search]', panel); if (s) s.focus(); }
        });
      }
      btn.textContent = box.dataset.label + (sel.length ? ` (${sel.length})` : '') + ' ▾';
      btn.classList.toggle('on', sel.length > 0);
    });
  }
  function fillPanel(box) {
    const key = box.dataset.key, panel = $('.ms-panel', box), opts = msOptions(key);
    panel.innerHTML = (opts.length > 8 ? '<input type="search" placeholder="بحث…">' : '') +
      '<div class="opts">' + opts.map(([v, l, c]) =>
        `<label><input type="checkbox" value="${esc(v)}" ${filt[key].includes(v) ? 'checked' : ''}> ${esc(l)}` +
        (c == null ? '' : `<span class="cnt">${n(c)}</span>`) + '</label>').join('') + '</div>';
    const s = $('input[type=search]', panel);
    if (s) s.addEventListener('input', () => $$('label', panel).forEach((l) => { l.hidden = !l.textContent.includes(s.value.trim()); }));
    $$('input[type=checkbox]', panel).forEach((c) => c.addEventListener('change', () => {
      filt[key] = $$('input[type=checkbox]:checked', panel).map((x) => x.value);
      jobsPage = 0; renderMs(); loadJobs();
    }));
  }
  document.addEventListener('click', () => $$('.ms-panel').forEach((p) => { p.hidden = true; }));
  let qTimer = null;
  $('#f-q').addEventListener('input', () => { clearTimeout(qTimer); qTimer = setTimeout(() => { jobsPage = 0; loadJobs(); }, 350); });
  ['#f-from', '#f-to', '#f-helper'].forEach((s) => $(s).addEventListener('change', () => { jobsPage = 0; loadJobs(); }));
  $('#f-clear').addEventListener('click', () => {
    Object.keys(filt).forEach((k) => { filt[k] = k === 'status' ? ['live'] : []; });
    $('#f-q').value = ''; $('#f-from').value = ''; $('#f-to').value = ''; $('#f-helper').checked = false;
    jobsPage = 0; renderMs(); loadJobs();
  });
  $('#f-export').addEventListener('click', () => { location.href = '/api/admin/export/jobs?' + filterQuery(); });

  let jobsSeq = 0;
  async function loadJobs() {
    const seq = ++jobsSeq;
    const d = await api('GET', '/api/admin/dash/jobs?' + filterQuery({ page: jobsPage }));
    if (seq !== jobsSeq) return;   // a newer filter change already asked again
    facets = d.facets;
    renderMs();
    $('#jobs-summary').innerHTML = `<span class="chip">النتيجة: <b>${n(d.total)}</b> وظيفة</span>` +
      d.titles.slice(0, 6).map(([t, c]) => `<span class="chip">${esc(t)} <b>${n(c)}</b></span>`).join('') +
      d.cities.slice(0, 5).map(([t, c]) => `<span class="chip">📍 ${esc(t || 'غير محدد')} <b>${n(c)}</b></span>`).join('');
    table($('#jobs-table'), ['المسمى', 'الجهة', 'المدينة', 'المصدر', 'الخبرة', 'الرابط', 'أول ظهور', 'الحالة'],
      d.rows.map((r) => {
        const link = safeUrl(r.apply_url || r.url);
        const title = link ? `<a href="${esc(link)}" target="_blank" rel="noopener noreferrer">${esc(r.title)}</a>` : esc(r.title);
        const kind = `<span class="sw-inline k-${esc(r.link_kind)}"></span>${esc(KIND_LABEL[r.link_kind] || r.link_kind)}` +
          (r.link_host ? `<br><span class="muted small" dir="ltr">${esc(r.link_host)}</span>` : '') +
          (r.link_kind === 'relay_resolved' ? `<br>${dl('الرابط قبل وبعد ↗', r.title, { q: r.title, kind: 'relay_resolved', source: r.source, status: ALL_STATUS })}` : '') +
          (r.via_helper ? '<br><span class="muted small">جابها جهازي</span>' : '');
        return [title, esc(r.company), esc(r.city || 'غير محدد'), esc(r.source), esc(r.exp_bucket), kind,
          `<span class="num">${esc(r.first_day)}</span>`, esc(STATUS_LABEL[r.status] || r.status)];
      }));
    const pages = Math.ceil(d.total / d.size);
    $('#pager').innerHTML = pages > 1 ?
      `<button class="btn btn-small" type="button" id="pg-prev" ${d.page <= 0 ? 'disabled' : ''}>السابق</button>` +
      `<span class="muted">صفحة ${d.page + 1} من ${pages}</span>` +
      `<button class="btn btn-small" type="button" id="pg-next" ${d.page >= pages - 1 ? 'disabled' : ''}>التالي</button>` : '';
    const prev = $('#pg-prev'), next = $('#pg-next');
    if (prev) prev.addEventListener('click', () => { jobsPage--; loadJobs(); });
    if (next) next.addEventListener('click', () => { jobsPage++; loadJobs(); });
  }

  // ---------------------------------------------------------------- sources page
  async function loadSources() {
    const d = await api('GET', '/api/admin/dash/sources');
    const k = d.kinds;
    const needs = d.helper_sources.length;
    $('#src-kpis').innerHTML =
      kpiDrill('مباشرة من المصدر', n(k.direct), 'بوابات الجهات والشركات والمنصات الأصلية', 'وظائف روابطها مباشرة من المصدر', { kind: 'direct' }) +
      kpiDrill('تخطّينا إعلاناتها', n(k.relay_resolved), 'دخلنا الموقع الناقل وجبنا رابط صاحب الوظيفة', 'تخطّينا إعلاناتها: الرابط قبل وبعد', { kind: 'relay_resolved', sort: 'resolved' }) +
      kpiDrill('بانتظار الرابط الأصلي', n(k.relay_pending), 'مخفية في البوت حتى نجد رابطها', 'بانتظار الرابط الأصلي', { kind: 'relay_pending' }) +
      kpiDrill('جابها جهازي', n(d.via_helper), `منذ البداية: ${n(d.via_helper_all)} · مصادر تحتاجه: ${n(needs)}`, 'وظائف جابها جهازي', { helper: '1', status: ALL_STATUS });
    const h = d.helper || {};
    $('#helper-card').innerHTML = h.at ?
      `<p>آخر تشغيل <b>${ago(h.at)}</b> (${esc(h.minutes)} دقيقة)، روابط أصلية فتحها: <b>${n(h.origin_links)}</b></p>` +
      '<div class="hbars">' + Object.entries(h.sources || {}).map(([s, v]) => `<div class="hbar"><span class="lbl">${esc(s)}</span><span class="muted">${typeof v === 'number' ? n(v) + ' وظيفة' : esc(v)}</span><span></span></div>`).join('') + '</div>'
      : '<p class="empty">لم يشتغل جهازك القديم بعد. شغّل tools\\install_saudi_helper.bat عليه.</p>';
    hbars($('#helper-sources'), d.helper_sources.map((s) => [s.source, s.live]), 'وظيفة حالية');
    table($('#src-table'), ['المصدر', 'الحالية', 'اليوم', '7 أيام', 'كل ما جُمع', 'مباشر', 'تخطّينا الإعلانات', 'بانتظار الرابط', 'عبر جهازي', 'عبر جدارات', 'أكثر المدن'],
      d.sources.map((s) => {
        const src = s.source || 'غير محدد';
        const cell = (v, label, extra) => (v ? dl(n(v), `${src}: ${label}`, Object.assign({ source: s.source || NONE }, extra)) : '0');
        return [esc(src) + (s.needs_helper ? ' <span class="badge warn">▲ يحتاج جهازي</span>' : ''),
          cell(s.live, 'الوظائف الحالية', {}), cell(s.today, 'جديد اليوم', { status: ALL_STATUS, from: dayAgo(0) }),
          cell(s.week, 'آخر 7 أيام', { status: ALL_STATUS, from: dayAgo(6) }), cell(s.all, 'كل ما جُمع', { status: ALL_STATUS }),
          cell(s.direct, 'مباشر من المصدر', { kind: 'direct' }),
          cell(s.relay_resolved, 'تخطّينا الإعلانات: الرابط قبل وبعد', { kind: 'relay_resolved', sort: 'resolved' }),
          cell(s.relay_pending, 'بانتظار الرابط الأصلي', { kind: 'relay_pending' }), cell(s.via_helper, 'عبر جهازي', { helper: '1' }),
          cell(s.via_jadarat, 'أصلها جدارات', { jadarat: '1' }),
          esc(s.top_cities.map(([c, v]) => `${c || 'غير محدد'} ${v}`).join('، '))];
      }), [1, 2, 3, 4, 5, 6, 7, 8, 9]);
    const st = { ok: ['good', 'يعمل'], empty: ['warn', 'لا جديد'], blocked: ['bad', 'محجوب'], error: ['bad', 'خطأ'], disallowed: ['idle', 'ممنوع'], skipped: ['idle', 'متوقف'] };
    table($('#col-table'), ['المجمّع', 'الحالة', 'جُلب', 'أُضيف', 'آخر تشغيل', 'ملاحظة'],
      d.collectors.map((c) => [esc(c.name) + (c.needs_helper ? ' <span class="badge warn">▲ جهازي</span>' : ''),
        badge(...(st[c.status] || ['idle', c.status || '—'])), n(c.fetched), n(c.added), esc(ago(c.last_run)), `<span class="muted small">${esc(c.message)}</span>`]), [2, 3]);
  }

  // ---------------------------------------------------------------- details dialog
  // Any number with data-drill opens the jobs behind it: source, title, the link before (the relay
  // page we were given) and after (the employer's own link), and the dates.
  const drill = { params: {}, page: 0 };
  document.addEventListener('click', (e) => {
    const el = e.target.closest('[data-drill]');
    if (!el) return;
    let spec;
    try { spec = JSON.parse(el.dataset.drill); } catch (err) { return; }
    drill.params = spec.params || {};
    drill.page = 0;
    $('#drill-q').value = drill.params.q || '';
    $('#drill-title').textContent = spec.title;
    const dlg = $('#drill');
    if (!dlg.open) dlg.showModal();
    loadDrill();
  });
  function drillQuery(extra) {
    const q = new URLSearchParams();
    Object.entries(drill.params).forEach(([k, v]) => { if (v && k !== 'q') q.set(k, v); });
    if (!drill.params.status) q.set('status', 'live');
    const text = $('#drill-q').value.trim();
    if (text) q.set('q', text);
    Object.entries(extra || {}).forEach(([k, v]) => q.set(k, v));
    return q.toString();
  }
  function linkCell(url, label) {
    const u = safeUrl(url);
    if (!u) return '<span class="muted">—</span>';
    let host = u;
    try { host = new URL(u).hostname.replace(/^www\./, ''); } catch (e) { /* shown as is */ }
    return `<div class="link-cell"><a href="${esc(u)}" target="_blank" rel="noopener noreferrer" title="${esc(u)}">${esc(host)}</a>` +
      `<span class="muted">${esc(label)}</span></div>`;
  }
  let drillSeq = 0;
  async function loadDrill() {
    const tbl = $('#drill-table');
    const seq = ++drillSeq;
    tbl.innerHTML = '<tbody><tr><td class="empty">جارٍ التحميل…</td></tr></tbody>';
    let d;
    try { d = await api('GET', '/api/admin/dash/jobs?' + drillQuery({ page: drill.page })); }
    catch (e) {
      if (seq === drillSeq && e.message !== 'login') tbl.innerHTML = `<tbody><tr><td class="empty">${esc(e.message)}</td></tr></tbody>`;
      return;
    }
    if (seq !== drillSeq) return;   // a newer request (typing, paging) already replaced this one
    $('#drill-total').innerHTML = `<b>${n(d.total)}</b> وظيفة`;
    table(tbl, ['الوظيفة', 'المصدر', 'المدينة', 'الرابط قبل', '', 'الرابط بعد (الأصلي)', 'أول ظهور', 'جلب الأصلي', 'الحالة'],
      d.rows.map((r) => {
        const before = r.link_kind === 'direct' ? '<span class="muted small">مباشر، بلا ناقل</span>' : linkCell(r.url, 'صفحة الموقع الناقل');
        const after = r.link_kind === 'relay_pending' ? '<span class="muted small">لم نجده بعد</span>' :
          linkCell(r.link_kind === 'direct' ? r.url : r.apply_url, r.link_kind === 'direct' ? 'رابط المصدر' : 'رابط صاحب الوظيفة');
        return [`<b>${esc(r.title)}</b>${r.company ? `<br><span class="muted small">${esc(r.company)}</span>` : ''}`,
          esc(r.source) + (r.via_helper ? '<br><span class="muted small">عبر جهازي</span>' : ''),
          esc(r.city || 'غير محدد'), before, r.link_kind === 'relay_resolved' ? '<span class="arrow">←</span>' : '', after,
          `<span class="num">${esc(r.first_day)}</span>` + (r.posted_date ? `<br><span class="muted small">نُشرت ${esc(r.posted_date)}</span>` : ''),
          r.resolved_at ? `<span class="num">${esc(hhmm(r.resolved_at))}</span>` :
            (r.link_kind === 'relay_resolved' ? '<span class="muted small">قبل بدء التتبّع</span>' : ''),
          esc(STATUS_LABEL[r.status] || r.status)];
      }));
    const pages = Math.ceil(d.total / d.size);
    $('#drill-pager').innerHTML = pages > 1 ?
      `<button class="btn btn-small" type="button" data-dp="-1" ${d.page <= 0 ? 'disabled' : ''}>السابق</button>` +
      `<span class="muted">صفحة ${d.page + 1} من ${pages}</span>` +
      `<button class="btn btn-small" type="button" data-dp="1" ${d.page >= pages - 1 ? 'disabled' : ''}>التالي</button>` : '';
  }
  $('#drill-pager').addEventListener('click', (e) => {
    const b = e.target.closest('[data-dp]');
    if (b && !b.disabled) { drill.page += +b.dataset.dp; loadDrill(); }
  });
  let drillTimer = null;
  $('#drill-q').addEventListener('input', () => { clearTimeout(drillTimer); drillTimer = setTimeout(() => { drill.page = 0; loadDrill(); }, 350); });
  $('#drill-close').addEventListener('click', () => $('#drill').close());
  $('#drill').addEventListener('click', (e) => { if (e.target === $('#drill')) $('#drill').close(); });
  $('#drill-export').addEventListener('click', () => { location.href = '/api/admin/export/jobs?' + drillQuery(); });

  // ---------------------------------------------------------------- users page
  async function loadUsers() {
    const d = await api('GET', '/api/admin/dash/users');
    const f = Object.fromEntries(d.funnel);
    $('#u-kpis').innerHTML =
      kpi('جرّبوا البوت', n(d.funnel[0][1]), 'كل من تحدث مع البوت') +
      kpi('مشتركون الآن', n(d.funnel[3][1]), `أكملوا الخطوات: ${n(f['أكملوا الخطوات الأربع'])}`) +
      kpi('ضغطات الروابط', n(d.clicks), `من ${n(d.click_people)} شخص`) +
      kpi('وظائف أُرسلت', n(d.alerts_sent), 'تنبيهات + منشورات المجموعات');
    hbars($('#funnel'), d.funnel, 'شخص');
    table($('#demand'), ['المسمى', 'يريده', 'وظائف متاحة الآن', ''],
      d.demand.map((r) => [esc(r.title), n(r.people), n(r.jobs), r.jobs === 0 ? badge('bad', 'لا يوجد') : r.jobs < r.people ? badge('warn', 'قليل') : '']), [1, 2]);
    dayBars($('#ch-u-new'), d.new_per_day, 'مستخدم جديد');
    dayBars($('#ch-u-search'), d.searches_per_day, 'عملية بحث');
    dayBars($('#ch-u-click'), d.clicks_per_day, 'ضغطة');
    hbars($('#u-titles'), d.titles.slice(0, 15), 'شخص');
    hbars($('#u-cities'), d.cities.slice(0, 15), 'شخص');
    hbars($('#u-exp'), d.exp, 'شخص');
    $('#u-modes').innerHTML = '<h4 class="muted small">التوقيت</h4>';
    const m = document.createElement('div'); $('#u-modes').appendChild(m); hbars(m, d.modes, 'شخص');
    hbars($('#u-zero'), d.zero_results, 'مرة');
    table($('#u-clicked'), ['الوظيفة', 'المصدر', 'الضغطات'], d.top_clicked.map((c) => [esc(c.title), esc(c.source), n(c.n)]), [2]);
    hbars($('#u-click-src'), d.click_sources, 'ضغطة');
    table($('#u-feeds'), ['ما ينشره', 'المدينة', 'نُشر', 'بالانتظار'], d.feeds.map((x) => [esc(x.titles), esc(x.city || 'كل المدن'), n(x.posted_today), n(x.queue)]), [2, 3]);
  }

  // ---------------------------------------------------------------- live
  let liveTimer = null, lastId = 0, lastJobs = -1, liveOk = Date.now(), ticking = false;
  function setLiveStatus(b) {
    const ok = Date.now() - liveOk < LIVE_MS * 3;
    $('#live').classList.toggle('off', !ok);
    $('#live-text').textContent = ok ? `مباشر · ${hhmm(new Date().toISOString())}` : 'انقطع التحديث';
    if (b && page === 'home') $('#status-row').innerHTML = statusCards(b);
  }
  async function tick() {
    if (document.hidden || ticking) return;   // a slow answer must not overlap the next poll
    ticking = true;
    try {
      const d = await api('GET', '/api/admin/dash/live?since=' + lastId);
      liveOk = Date.now();
      const fresh = d.events.filter((e) => e.id > lastId);
      if (fresh.length || lastId === 0) {
        const ul = $('#feed');
        const items = d.events.map((e) => {
          const data = e.data || {};
          const list = (x) => (Array.isArray(x) && x.length ? x.join('، ') : '');
          const what = list(data.titles) || list(data.cities) || data.title || data.mode || data.exp || (data.n ? n(data.n) + ' وظيفة' : '');
          return `<li class="${lastId && e.id > lastId ? 'new' : ''}"><span class="t">${hhmm(e.ts)}</span><span>${esc(e.label)}${what ? ': ' + esc(what) : ''}</span></li>`;
        });
        if (lastId === 0) {
          ul.innerHTML = items.join('') || d.latest_jobs.map((j) => `<li><span class="t">${hhmm(j.first_seen)}</span><span>وظيفة جديدة: ${esc(j.title)} · ${esc(j.source)}</span></li>`).join('') || '<li class="muted">لا شيء بعد</li>';
        } else if (items.length) {
          ul.insertAdjacentHTML('afterbegin', items.join(''));
          while (ul.children.length > 60) ul.lastChild.remove();
        }
      }
      const changed = (lastId && fresh.length) || (lastJobs >= 0 && d.jobs_total !== lastJobs);
      lastId = d.last_id || lastId;
      lastJobs = d.jobs_total;
      setLiveStatus(d.bot);
      if (changed) refresh(false);
    } catch (e) { setLiveStatus(null); } finally { ticking = false; }
  }
  function startLive() { stopLive(); lastId = 0; lastJobs = -1; tick(); liveTimer = setInterval(tick, LIVE_MS); }
  function stopLive() { clearInterval(liveTimer); liveTimer = null; }
  document.addEventListener('visibilitychange', () => { if (!document.hidden && liveTimer) tick(); });

  // ---------------------------------------------------------------- start
  async function start() {
    let w;
    try { w = await api('GET', '/api/admin/whoami'); } catch (e) { return; }
    if (!w.logged_in) { showLogin(); return; }
    $('#login').hidden = true;
    $('#app').hidden = false;
    $('#link-tg').hidden = w.owner_linked;
    if (!w.owner_linked) banner('لحماية أكبر: اضغط «اربط تيليجرام» ليكون الدخول بتأكيد منك في البوت فقط.');
    let p = 'home';
    try { p = localStorage.getItem('dash-page') || 'home'; } catch (e) { /* private mode */ }
    go(['home', 'jobs', 'sources', 'users', 'reports'].includes(p) ? p : 'home');
    startLive();
  }
  start();
})();
