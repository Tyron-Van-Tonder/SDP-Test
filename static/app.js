/* RAT — dashboard application logic (no framework, no build step).
 *
 * Data flow: one repo is selected at a time.  A global commit filter
 * (all / committer-date range / manual commit list), plus an optional author
 * filter, parameterise every metrics request.  Anything fetched is cached
 * under a signature of the current repo + filters and invalidated on change.
 */
'use strict';

/* ------------------------------------------------------------- helpers --- */

const $ = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

const ESC_MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ESC_MAP[c]);

const fmtInt = (n) => Number(n || 0).toLocaleString('en-US');
const fmtFrac = (x) => {
  x = Number(x || 0);
  if (x === 0) return '0';
  const a = Math.abs(x);
  if (a >= 100) return x.toFixed(0);
  if (a >= 1) return x.toFixed(2);
  return x.toFixed(3);
};
const fmtDate = (epoch) => (epoch ? new Date(epoch * 1000).toISOString().slice(0, 10) : '');
const shortHash = (h) => String(h || '').slice(0, 8);
const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);

function dateToEpoch(v) {
  if (!v) return null;
  const ts = Date.parse(v + 'T00:00:00Z');
  return Number.isFinite(ts) ? Math.floor(ts / 1000) : null;
}
const epochToDate = (e) => (e ? new Date(e * 1000).toISOString().slice(0, 10) : '');

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

const csvCell = (s) => (/[",\n]/.test(String(s)) ? '"' + String(s).replace(/"/g, '""') + '"' : String(s));

function download(filename, text) {
  const blob = new Blob([text], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
}

/* ------------------------------------------------------------ api client -- */

async function api(path, opts = {}) {
  const init = { method: opts.method || 'GET' };
  if (opts.json !== undefined) {
    init.method = opts.method || 'POST';
    init.headers = { 'Content-Type': 'application/json' };
    init.body = JSON.stringify(opts.json);
  }
  if (opts.form) {
    init.method = opts.method || 'POST';
    init.body = opts.form;
  }
  const res = await fetch(path, init);
  if (!res.ok) {
    let detail = 'HTTP ' + res.status;
    try {
      const j = await res.json();
      if (j && j.detail) detail = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail);
    } catch (e) { /* keep the status text */ }
    throw new Error(detail);
  }
  return res.json();
}

/* ---------------------------------------------------------------- toasts -- */

function toast(msg, kind = 'info', ms = 4600) {
  const box = $('#toasts');
  const el = document.createElement('div');
  el.className = 'toast ' + (kind === 'info' ? '' : kind);
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => el.remove(), ms);
}

/* ---------------------------------------------------------------- state --- */

const PROCESSING = new Set(['queued', 'cloning', 'parsing']);

const state = {
  repos: [],
  currentId: null,
  detail: null,
  tab: 'overview',
  bucket: 'month',
  drawerBucket: 'month',
  filter: { mode: 'all', start: null, end: null, hashes: [] },
  authorIds: [],
  allAuthors: [],
  authorsPanelQuery: '',
  cache: {},
  inflight: new Map(),
  filesSort: { key: 'churn', dir: -1 },
  dirsSort: { key: 'churn', dir: -1 },
  filesQuery: '',
  dirsQuery: '',
  authorsQuery: '',
  currentItems: { files: [], dirs: [] },
  currentAuthors: [],
  authorsExpanded: new Set(),
  includeUntouched: false,
  commits: { page: 0, search: '', selected: new Map(), total: 0, items: [] },
  pollTimer: null,
  addMode: 'url',
};

const mainEl = $('#main');
const drawerEl = $('#drawer');
const drawerBackdrop = $('#drawerBackdrop');
const modalBackdrop = $('#modalBackdrop');

/* ---------------------------------------------------- cache + filter sig -- */

function sig() {
  return JSON.stringify([state.currentId, state.filter, state.authorIds.slice().sort((a, b) => a - b)]);
}
function clearCache() { state.cache = {}; }

function cached(key, fetcher) {
  if (key in state.cache) return Promise.resolve(state.cache[key]);
  if (state.inflight.has(key)) return state.inflight.get(key);
  const p = fetcher().then(
    (v) => { state.cache[key] = v; state.inflight.delete(key); return v; },
    (e) => { state.inflight.delete(key); throw e; }
  );
  state.inflight.set(key, p);
  return p;
}

function filterParams(extra) {
  const p = new URLSearchParams();
  p.set('mode', state.filter.mode);
  if (state.filter.mode === 'range') {
    if (state.filter.start != null) p.set('start', String(state.filter.start));
    if (state.filter.end != null) p.set('end', String(state.filter.end));
  }
  if (state.filter.mode === 'list' && state.filter.hashes.length) {
    p.set('hashes', state.filter.hashes.join(','));
  }
  if (state.authorIds.length) p.set('authors', state.authorIds.join(','));
  for (const k of Object.keys(extra || {})) p.set(k, String(extra[k]));
  return p.toString();
}

/* -------------------------------------------------------------- fetchers -- */

const repoUrl = () => '/api/repos/' + state.currentId;

const fetchTotals = () => cached('totals|' + sig(), () => api(repoUrl() + '/metrics?' + filterParams({ kind: 'repo' })));
const fetchFiles = (untouched) => cached(
  'files|' + !!untouched + '|' + sig(),
  () => api(repoUrl() + '/metrics?' + filterParams({ kind: 'file', include_untouched: untouched }))
);
const fetchDirs = (untouched) => cached(
  'dirs|' + !!untouched + '|' + sig(),
  () => api(repoUrl() + '/metrics?' + filterParams({ kind: 'dir', include_untouched: untouched }))
);
const fetchAuthors = () => cached('authors|' + sig(), () => api(repoUrl() + '/authors?' + filterParams({})));
const fetchSeries = (bucket) => cached(
  'series|' + bucket + '|' + sig(),
  () => api(repoUrl() + '/series?' + filterParams({ bucket }))
);

async function ensureAllAuthors(force = false) {
  if (!force && state.allAuthors.length) return state.allAuthors;
  const data = await api(repoUrl() + '/authors');
  state.allAuthors = data.authors;
  return state.allAuthors;
}

/* -------------------------------------------------------- repos + polling -- */

async function loadRepos() {
  const data = await api('/api/repos');
  state.repos = data.repos;
  const qb = $('#queueBadge');
  if (qb) {
    qb.hidden = !data.queue;
    qb.textContent = data.queue ? data.queue + ' queued' : '';
  }
  renderSidebar();
  return data;
}

function needPoll() {
  if (state.repos.some((r) => PROCESSING.has(r.status))) return true;
  return !!(state.detail && PROCESSING.has(state.detail.status));
}

function schedulePoll(delay = 1000) {
  if (state.pollTimer) return;
  state.pollTimer = setTimeout(pollTick, delay);
}

async function pollTick() {
  state.pollTimer = null;
  let data = null;
  try { data = await loadRepos(); } catch (e) { /* transient */ }
  const cur = data && data.repos.find((r) => r.id === state.currentId);
  if (cur && state.detail) {
    if (PROCESSING.has(state.detail.status) && cur.status !== state.detail.status) {
      await selectRepo(cur.id, true);
    } else if (PROCESSING.has(cur.status)) {
      state.detail = Object.assign({}, state.detail, cur);
      renderIngest(cur);
    }
  }
  if (needPoll()) schedulePoll(1300);
}

/* ------------------------------------------------------------- rendering -- */

function renderTopbar() {
  const el = $('#repoLabel');
  if (!el) return;
  const r = state.detail;
  el.innerHTML = r
    ? `<span class="name">${esc(r.name)}</span><code>${esc(shortHash(r.ref_hash))}</code>
       <span class="chip ${esc(r.status)} dot">${esc(r.status)}</span>
       <span>· ${fmtInt(r.commits_count)} commits</span>`
    : '';
}

function renderSidebar() {
  const list = $('#repoList');
  if (!list) return;
  if (!state.repos.length) {
    list.innerHTML = '<div class="side-empty">No repositories yet.</div>';
    return;
  }
  list.innerHTML = state.repos.map((r) => {
    const active = r.id === state.currentId ? ' active' : '';
    const processing = PROCESSING.has(r.status);
    const meta = r.status === 'ready'
      ? `${fmtInt(r.commits_count)} commits · ${fmtInt(r.authors_count)} authors`
      : (r.status === 'error' ? esc((r.error || 'failed').slice(0, 60)) : esc(r.progress_msg || r.status));
    return `
      <div class="repo-item${active}" data-act="select-repo" data-id="${r.id}">
        <div class="r-name" title="${esc(r.name)}">${esc(r.name)}</div>
        <div class="r-meta"><span class="chip ${esc(r.status)} dot">${esc(r.status)}</span><span>${meta}</span></div>
        ${processing ? `<div class="progress"><i style="width:${Math.round((r.progress || 0) * 100)}%"></i></div>` : ''}
        ${processing ? '' : `<button class="r-del" data-act="del-repo" data-id="${r.id}" title="Remove repository">×</button>`}
      </div>`;
  }).join('');
}

function renderEmpty() {
  mainEl.innerHTML = `
    <div class="empty">
      <h2>Analyse a repository's history</h2>
      <p>RAT ingests a repository once, precomputes every metric, then serves an instant dashboard.</p>
      <ul>
        <li><b>From a URL</b> — any public repo is deep-cloned with full history.</li>
        <li><b>From a zip</b> — upload an archive that includes the <code>.git</code> directory.</li>
        <li>Filter by commit range, manual commit list, author, file and directory.</li>
        <li>Per object: l+, l−, δ, λ, n, η, ρ — plus ownership ω per author.</li>
      </ul>
      <button class="btn primary" data-act="open-add">＋ Add your first repository</button>
    </div>`;
}

function renderIngest(repo) {
  const pct = Math.round((repo.progress || 0) * 100);
  const processing = PROCESSING.has(repo.status);
  mainEl.innerHTML = `
    <div class="ingest">
      <h2>${esc(repo.name)}</h2>
      <div class="sub">${repo.source_type === 'url' ? esc(repo.source) : 'uploaded archive'}
        · analysing <code>${esc(repo.ref || 'HEAD')}</code></div>
      ${repo.status === 'error' ? `
        <div class="chip error dot">ingestion failed</div>
        <div class="error-box">${esc(repo.error || 'Ingestion failed.')}</div>
        <div style="margin-top:16px">
          <button class="btn danger" data-act="del-repo" data-id="${repo.id}">Remove repository</button>
        </div>`
      : `
        ${repo.status === 'queued'
          ? '<div class="chip queued dot">queued — waiting for the analysis worker</div>'
          : `<div class="big-pct">${pct}%</div><div class="progress"><i style="width:${pct}%"></i></div>`}
        <div class="msg">${esc(repo.progress_msg || 'Waiting to start…')}</div>
        <div style="margin-top:16px">
          <button class="btn danger" data-act="del-repo" data-id="${repo.id}" disabled
            title="Wait for the analysis to finish">Remove repository</button>
        </div>`}
    </div>`;
}

function renderDashboard() {
  mainEl.innerHTML = `
    <div class="filterbar" id="filterbar">
      <div class="fl">
        <span class="group-label">Commits</span>
        <div class="seg" id="fModeSeg">
          <button data-act="f-mode" data-mode="all">All</button>
          <button data-act="f-mode" data-mode="range">Date range</button>
          <button data-act="f-mode" data-mode="list">Commit list</button>
        </div>
      </div>
      <div class="fl" id="fRange" hidden>
        <input type="date" id="fFrom" title="Start date (inclusive)">
        <span class="group-label">to</span>
        <input type="date" id="fTo" title="End date (inclusive)">
      </div>
      <div class="fl" id="fList" hidden>
        <textarea id="fHashes" placeholder="Paste full commit hashes, space or comma separated"></textarea>
        <span class="chip queued" id="hashWarn" hidden></span>
      </div>
      <div class="sep"></div>
      <div class="fl authors-pop" id="authorsPop">
        <button class="btn sm" data-act="toggle-authors">Authors <span class="tag" id="authorBadge" hidden>0</span> ▾</button>
        <div class="authors-panel" id="authorsPanel" hidden></div>
      </div>
      <div class="sep"></div>
      <span class="h-chip" id="hChip">H = …</span>
      <span style="flex:1"></span>
      <button class="btn sm ghost" data-act="f-reset">Reset filters</button>
    </div>
    <nav class="tabs" id="tabsNav">
      ${['overview', 'files', 'dirs', 'authors', 'commits'].map((t) => `
        <button data-act="tab" data-tab="${t}" class="${state.tab === t ? 'active' : ''}">${tabLabel(t)}</button>`).join('')}
    </nav>
    <div id="tabContent"><div class="skeleton"><span class="spinner"></span> Loading…</div></div>`;
  syncFilterbar();
  refreshTotals();
  renderTab();
}

function tabLabel(t) {
  return { overview: 'Overview', files: 'Files', dirs: 'Directories', authors: 'Authors', commits: 'Commits' }[t] || t;
}

function syncTabs() {
  for (const b of $$('#tabsNav button')) b.classList.toggle('active', b.dataset.tab === state.tab);
}

function syncFilterbar() {
  const seg = $('#fModeSeg');
  if (seg) for (const b of $$('button', seg)) b.classList.toggle('active', b.dataset.mode === state.filter.mode);
  const range = $('#fRange');
  if (range) range.hidden = state.filter.mode !== 'range';
  const list = $('#fList');
  if (list) list.hidden = state.filter.mode !== 'list';
  const fFrom = $('#fFrom');
  if (fFrom && document.activeElement !== fFrom) {
    fFrom.value = state.filter.start != null ? epochToDate(state.filter.start) : '';
  }
  const fTo = $('#fTo');
  if (fTo && document.activeElement !== fTo) {
    fTo.value = state.filter.end != null ? epochToDate(state.filter.end - 1) : '';
  }
  const ta = $('#fHashes');
  if (ta && document.activeElement !== ta) ta.value = state.filter.hashes.join(' ');
  const badge = $('#authorBadge');
  if (badge) {
    badge.hidden = !state.authorIds.length;
    badge.textContent = String(state.authorIds.length);
  }
  const panel = $('#authorsPanel');
  if (panel && !panel.hidden) renderAuthorsPanelList();
}

async function refreshTotals() {
  try {
    const payload = await fetchTotals();
    const chip = $('#hChip');
    if (chip) chip.textContent = `H = ${fmtInt(payload.commits)} commit${payload.commits === 1 ? '' : 's'}`;
    const warn = $('#hashWarn');
    if (warn) {
      const unknown = payload.unknown_hashes || [];
      warn.hidden = !unknown.length;
      warn.textContent = unknown.length ? `${unknown.length} not found` : '';
      warn.title = unknown.slice(0, 12).join('\n');
    }
  } catch (e) { toast(e.message, 'error'); }
}

function onFilterChange() {
  clearCache();
  syncFilterbar();
  refreshTotals();
  renderTab();
}

function resetFilters(apply = true) {
  state.filter = { mode: 'all', start: null, end: null, hashes: [] };
  state.authorIds = [];
  if (apply) { syncFilterbar(); onFilterChange(); }
}

/* ------------------------------------------------------------ tab dispatch */

async function renderTab() {
  const el = $('#tabContent');
  if (!el) return;
  const t = state.tab;
  el.innerHTML = `<div class="skeleton"><span class="spinner"></span> Loading ${esc(tabLabel(t))}…</div>`;
  try {
    if (t === 'overview') await renderOverview(el);
    else if (t === 'files') await renderFilesTab(el);
    else if (t === 'dirs') await renderDirsTab(el);
    else if (t === 'authors') await renderAuthorsTab(el);
    else if (t === 'commits') await renderCommitsTab(el);
  } catch (e) {
    el.innerHTML = `<div class="card"><b class="neg">Could not load this view.</b><p class="hint">${esc(e.message)}</p></div>`;
  }
}

/* --------------------------------------------------------------- overview */

function statCards(cards) {
  return `<div class="cards">` + cards.map(([k, v, cls, formula]) => `
    <div class="stat" title="${esc(formula)}">
      <div class="v ${cls}">${v}</div>
      <div class="k">${esc(k)}</div>
      <div class="f">${esc(formula.split(' — ')[0])}</div>
    </div>`).join('') + `</div>`;
}

function miniStat(label, value, cls) {
  return `<div class="mini-stat"><b class="${cls || ''}">${value}</b><span>${esc(label)}</span></div>`;
}

function miniObjects(kind, items, emptyMsg) {
  const list = items.filter((i) => i.churn > 0).slice(0, 8);
  if (!list.length) return `<div class="empty-note">${esc(emptyMsg)}</div>`;
  return `<div class="mini-list">` + list.map((it) => `
    <div class="mini-item" data-act="open-object" data-kind="${kind}" data-path="${esc(it.path)}" title="${esc(it.path || 'repository root')}">
      <span class="p">${esc(it.path || '⟨root⟩')}</span>
      <span class="n">λ ${fmtInt(it.churn)}</span>
    </div>`).join('') + `</div>`;
}

function miniAuthors(items) {
  const list = items.filter((a) => a.churn > 0).slice(0, 8);
  if (!list.length) return '<div class="empty-note">No author activity in this range.</div>';
  return `<div class="mini-list">` + list.map((a) => `
    <div class="mini-item" data-act="apply-author" data-id="${a.id}" title="Filter all metrics by ${esc(a.name)}">
      <span class="p" style="font-family:inherit">${esc(a.name || a.email)}</span>
      <span class="n">λ ${fmtInt(a.churn)}</span>
    </div>`).join('') + `</div>`;
}

async function renderOverview(el) {
  const [totalsP, seriesP, filesP, dirsP, authorsP] = await Promise.all([
    fetchTotals(),
    fetchSeries(state.bucket),
    fetchFiles(false),
    fetchDirs(false),
    fetchAuthors(),
  ]);
  const t = totalsP.totals;
  const cards = [
    ['Commits in H', fmtInt(totalsP.commits), '', '|H| — commits in the selected set'],
    ['Added', '+' + fmtInt(t.added), 'pos', 'l+ — lines added'],
    ['Removed', '−' + fmtInt(t.removed), 'neg', 'l− — lines removed'],
    ['Growth', (t.growth >= 0 ? '+' : '−') + fmtInt(Math.abs(t.growth)), t.growth >= 0 ? 'pos' : 'neg', 'δ = l+ − l−'],
    ['Churn', fmtInt(t.churn), 'blue', 'λ = l+ + l−'],
    ['Modifications', fmtInt(t.modifications), '', 'n — commits with λ > 0'],
    ['Mod. frequency', fmtFrac(t.mod_freq), 'teal', 'η = n / |H|'],
    ['Churn rate', fmtFrac(t.churn_rate), 'teal', 'ρ = λ / |H|'],
  ];
  const bucketSeg = ['day', 'week', 'month'].map((b) =>
    `<button data-act="bucket" data-bucket="${b}" class="${state.bucket === b ? 'active' : ''}">${cap(b)}</button>`).join('');
  el.innerHTML = `
    ${statCards(cards)}
    <div class="card">
      <header>
        <h3>Activity over time <span class="tag">committer date · UTC</span></h3>
        <span class="spacer"></span>
        <div class="seg">${bucketSeg}</div>
      </header>
      <div id="overviewChart"></div>
    </div>
    <div class="grid-3">
      <div class="card">
        <header><h3>Top files</h3><span class="spacer"></span><button class="link" data-act="tab" data-tab="files">view all →</button></header>
        ${miniObjects('file', filesP.items, 'No file activity in this range.')}
      </div>
      <div class="card">
        <header><h3>Top directories</h3><span class="spacer"></span><button class="link" data-act="tab" data-tab="dirs">view all →</button></header>
        ${miniObjects('dir', dirsP.items.filter((i) => i.path !== ''), 'No directory activity in this range.')}
      </div>
      <div class="card">
        <header><h3>Top authors</h3><span class="spacer"></span><button class="link" data-act="tab" data-tab="authors">view all →</button></header>
        ${miniAuthors(authorsP.authors)}
      </div>
    </div>`;
  RATCharts.timeSeries($('#overviewChart'), seriesP.points, { lineKey: 'commits' });
}

/* ------------------------------------------------------- files + dirs tabs */

function sortItems(items, sort) {
  const { key, dir } = sort;
  return items.slice().sort((a, b) => {
    const va = a[key];
    const vb = b[key];
    if (typeof va === 'string' || typeof vb === 'string') {
      return dir * String(va).localeCompare(String(vb));
    }
    return dir * ((va || 0) - (vb || 0));
  });
}

const OBJECT_COLS = [
  ['path', 'Path', 'path'],
  ['added', 'l+', 'num'],
  ['removed', 'l−', 'num'],
  ['growth', 'δ', 'num'],
  ['churn', 'λ', 'num'],
  ['modifications', 'n', 'num'],
  ['mod_freq', 'η', 'num'],
  ['churn_rate', 'ρ', 'num'],
];

function renderCurrentTable(scope) {
  const cont = document.getElementById(scope + 'Table');
  if (!cont) return;
  const kind = scope === 'files' ? 'file' : 'dir';
  const query = (scope === 'files' ? state.filesQuery : state.dirsQuery).trim().toLowerCase();
  const sortState = scope === 'files' ? state.filesSort : state.dirsSort;
  let items = state.currentItems[scope] || [];
  if (query) items = items.filter((it) => it.path.toLowerCase().includes(query));
  items = sortItems(items, sortState);

  const countEl = document.getElementById(scope + 'Count');
  if (countEl) {
    const n = items.length;
    const noun = scope === 'files'
      ? (n === 1 ? 'file' : 'files')
      : (n === 1 ? 'directory' : 'directories');
    countEl.textContent = `${fmtInt(n)} ${noun}`;
  }

  const head = OBJECT_COLS.map(([key, label, cls]) => {
    const active = sortState.key === key;
    const arrow = active ? (sortState.dir < 0 ? ' ▾' : ' ▴') : '';
    return `<th class="sortable ${cls === 'num' ? 'num' : ''}" data-act="sort" data-key="${key}" data-scope="${scope}">${label}${arrow}</th>`;
  }).join('') + '<th></th>';

  const rows = items.map((it) => {
    const growthCls = it.growth > 0 ? 'pos' : (it.growth < 0 ? 'neg' : '');
    const growthSign = it.growth > 0 ? '+' : (it.growth < 0 ? '−' : '');
    return `
      <tr class="clickable" data-act="open-object" data-kind="${kind}" data-path="${esc(it.path)}">
        <td class="path" title="${esc(it.path)}">${esc(it.path || '⟨root⟩')}</td>
        <td class="num pos">${it.added ? '+' + fmtInt(it.added) : '0'}</td>
        <td class="num ${it.removed ? 'neg' : ''}">${it.removed ? '−' + fmtInt(it.removed) : '0'}</td>
        <td class="num ${growthCls}">${growthSign}${fmtInt(Math.abs(it.growth))}</td>
        <td class="num">${fmtInt(it.churn)}</td>
        <td class="num">${fmtInt(it.modifications)}</td>
        <td class="num">${fmtFrac(it.mod_freq)}</td>
        <td class="num">${fmtFrac(it.churn_rate)}</td>
        <td>${it.is_binary ? '<span class="tag">binary</span>' : ''}</td>
      </tr>`;
  }).join('');

  cont.innerHTML = `
    <table class="tbl">
      <thead><tr>${head}</tr></thead>
      <tbody>${rows || `<tr><td colspan="9" class="empty-note">No matching objects.</td></tr>`}</tbody>
    </table>`;
}

function objectTabToolbar(scope) {
  const noun = scope === 'files' ? 'files' : 'directories';
  return `
    <div class="toolbar">
      <input type="search" id="${scope}Search" placeholder="Filter by path…" value="${esc(scope === 'files' ? state.filesQuery : state.dirsQuery)}" style="width:260px">
      <label style="display:flex;gap:6px;align-items:center;color:var(--muted);font-size:12.5px">
        <input type="checkbox" id="untouchedBox" ${state.includeUntouched ? 'checked' : ''}>
        include ${scope === 'files' ? 'files' : 'directories'} with no changes in this range
      </label>
      <span class="spacer"></span>
      <span class="count" id="${scope}Count"></span>
      <button class="btn sm" data-act="csv" data-scope="${scope}">Export CSV</button>
    </div>`;
}

async function renderFilesTab(el) {
  const payload = await fetchFiles(state.includeUntouched);
  state.currentItems.files = payload.items;
  el.innerHTML = objectTabToolbar('files') + '<div class="table-wrap" id="filesTable"></div>';
  renderCurrentTable('files');
}

async function renderDirsTab(el) {
  const payload = await fetchDirs(state.includeUntouched);
  state.currentItems.dirs = payload.items.filter((i) => i.path !== '');
  el.innerHTML = objectTabToolbar('dirs') + '<div class="table-wrap" id="dirsTable"></div>';
  renderCurrentTable('dirs');
}

/* ---------------------------------------------------------------- authors */

function memberRows(group) {
  const rows = group.members.map((m) => `
    <div class="own-row" style="grid-template-columns: minmax(150px,1.3fr) auto auto">
      <div class="who">${esc(m.name || '(no name)')}<small>${esc(m.email)}</small></div>
      <div class="num">${fmtInt(m.commits)} commits · λ ${fmtInt(m.churn)}</div>
      ${m.id !== group.id
        ? `<button class="btn sm ghost" data-act="unmerge" data-from="${m.id}">unmerge</button>`
        : '<span class="tag">primary</span>'}
    </div>`).join('');
  return `<tr><td colspan="10" style="background:var(--panel2);padding:8px 14px">${rows}</td></tr>`;
}

function renderAuthorsTable() {
  const cont = document.getElementById('authorsTable');
  if (!cont) return;
  const q = state.authorsQuery.trim().toLowerCase();
  const all = state.currentAuthors || [];
  let groups = all;
  if (q) {
    groups = all.filter((g) => (g.name + ' ' + g.email + ' ' + g.members.map((m) => m.name + ' ' + m.email).join(' ')).toLowerCase().includes(q));
  }
  const countEl = document.getElementById('authorsCount');
  if (countEl) countEl.textContent = `${fmtInt(groups.length)} author${groups.length === 1 ? '' : 's'}`;

  const rows = groups.map((g) => {
    const expanded = state.authorsExpanded.has(g.id);
    const options = all.filter((o) => o.id !== g.id)
      .map((o) => `<option value="${o.id}">${esc(o.name || o.email)}</option>`).join('');
    return `
      <tr>
        <td>
          <b>${esc(g.name || '(no name)')}</b>
          ${g.merged ? `<span class="tag">${g.members.length} identities</span>` : ''}
          <div style="font-size:11.5px;color:var(--muted)">${esc(g.email)}</div>
          ${g.merged ? `<button class="link" data-act="expand-members" data-id="${g.id}">${expanded ? '▾' : '▸'} members (${g.members.length})</button>` : ''}
        </td>
        <td class="num">${fmtInt(g.commits)}</td>
        <td class="num pos">+${fmtInt(g.added)}</td>
        <td class="num neg">−${fmtInt(g.removed)}</td>
        <td class="num">${fmtInt(g.churn)}</td>
        <td class="num">${fmtInt(g.modifications)}</td>
        <td class="num">${fmtFrac(g.mod_freq)}</td>
        <td class="num">${fmtFrac(g.churn_rate)}</td>
        <td class="num">
          <div style="display:flex;gap:6px;justify-content:flex-end">
            <select id="mergeTarget-${g.id}" style="max-width:160px"><option value="">merge into…</option>${options}</select>
            <button class="btn sm" data-act="merge" data-from="${g.id}">Merge</button>
          </div>
        </td>
      </tr>
      ${expanded ? memberRows(g) : ''}`;
  }).join('');

  cont.innerHTML = `
    <table class="tbl">
      <thead><tr>
        <th>Author</th><th class="num">|H|</th><th class="num">l+</th><th class="num">l−</th>
        <th class="num">λ</th><th class="num">n</th><th class="num">η</th><th class="num">ρ</th><th></th>
      </tr></thead>
      <tbody>${rows || '<tr><td colspan="9" class="empty-note">No authors to show.</td></tr>'}</tbody>
    </table>`;
}

async function renderAuthorsTab(el) {
  const payload = await fetchAuthors();
  state.currentAuthors = payload.authors;
  el.innerHTML = `
    <div class="toolbar">
      <input type="search" id="authorsSearch" placeholder="Filter by name or email…" value="${esc(state.authorsQuery)}" style="width:280px">
      <span class="spacer"></span>
      <span class="count" id="authorsCount"></span>
    </div>
    <div class="table-wrap" id="authorsTable"></div>
    <p class="empty-note" style="margin-top:8px">
      Automatic identity merging via <code>.mailmap</code> is applied at ingestion; manual merges below
      take effect immediately in every metric, including ownership shares (ω).
    </p>`;
  renderAuthorsTable();
}

/* ---------------------------------------------------------------- commits */

function updateSelectionUI() {
  const n = state.commits.selected.size;
  const applyBtn = document.getElementById('applySelBtn');
  if (applyBtn) {
    applyBtn.hidden = !n;
    applyBtn.textContent = `Use ${n} selected as filter`;
  }
  const clearBtn = document.getElementById('clearSelBtn');
  if (clearBtn) clearBtn.hidden = !n;
  const pageBox = document.getElementById('pickPage');
  if (pageBox) {
    pageBox.checked = state.commits.items.length > 0
      && state.commits.items.every((i) => state.commits.selected.has(i.hash));
  }
}

function renderCommitsTable() {
  const cont = document.getElementById('commitsTable');
  if (!cont) return;
  const items = state.commits.items;
  const rows = items.map((it) => `
    <tr class="clickable" data-row-hash="${it.hash}">
      <td style="width:30px"><input type="checkbox" data-pick="${it.hash}" ${state.commits.selected.has(it.hash) ? 'checked' : ''}></td>
      <td><code class="h" title="${esc(it.hash)}">${esc(shortHash(it.hash))}</code></td>
      <td>${esc(fmtDate(it.cdate))}</td>
      <td class="sub" title="${esc(it.author_name + ' <' + it.author_email + '>')}">${esc(it.author_name || it.author_email)}</td>
      <td class="sub" title="${esc(it.subject)}">${esc(it.subject || '(no subject)')}</td>
      <td class="num pos">${it.added ? '+' + fmtInt(it.added) : '0'}</td>
      <td class="num ${it.removed ? 'neg' : ''}">${it.removed ? '−' + fmtInt(it.removed) : '0'}</td>
      <td class="num">${fmtInt(it.churn)}</td>
    </tr>`).join('');
  cont.innerHTML = `
    <table class="tbl">
      <thead><tr>
        <th><input type="checkbox" id="pickPage" title="Select this page"></th>
        <th>Hash</th><th>Date</th><th>Author</th><th>Subject</th>
        <th class="num">l+</th><th class="num">l−</th><th class="num">λ</th>
      </tr></thead>
      <tbody>${rows || '<tr><td colspan="8" class="empty-note">No commits match.</td></tr>'}</tbody>
    </table>`;
  const countEl = document.getElementById('commitsCount');
  if (countEl) countEl.textContent = `${fmtInt(state.commits.total)} commits`;
  const pageLabel = document.getElementById('pageLabel');
  const limit = 50;
  const pages = Math.max(1, Math.ceil(state.commits.total / limit));
  if (pageLabel) pageLabel.textContent = `Page ${state.commits.page + 1} of ${pages}`;
  updateSelectionUI();
}

async function loadCommitsPage() {
  const limit = 50;
  const params = new URLSearchParams({
    search: state.commits.search,
    limit: String(limit),
    offset: String(state.commits.page * limit),
  });
  const data = await api(repoUrl() + '/commits?' + params.toString());
  state.commits.total = data.total;
  state.commits.items = data.items;
  const pages = Math.max(1, Math.ceil(data.total / limit));
  if (state.commits.page >= pages && state.commits.page > 0) {
    state.commits.page = pages - 1;
    return loadCommitsPage();
  }
  renderCommitsTable();
}

async function renderCommitsTab(el) {
  el.innerHTML = `
    <div class="toolbar">
      <input type="search" id="commitsSearch" placeholder="Search hash or subject…" value="${esc(state.commits.search)}" style="width:280px">
      <span class="spacer"></span>
      <span class="count" id="commitsCount"></span>
      <button class="btn sm primary" id="applySelBtn" data-act="apply-selected" hidden>Use selected as filter</button>
      <button class="btn sm ghost" id="clearSelBtn" data-act="clear-selection" hidden>Clear selection</button>
    </div>
    <div class="table-wrap" id="commitsTable"><div class="skeleton"><span class="spinner"></span> Loading…</div></div>
    <div class="pager">
      <button class="btn sm" data-act="page" data-delta="-1">‹ Prev</button>
      <span id="pageLabel"></span>
      <button class="btn sm" data-act="page" data-delta="1">Next ›</button>
      <span class="spacer"></span>
      <span class="empty-note">Select commits here, then apply them as a manual commit-list filter.</span>
    </div>`;
  await loadCommitsPage();
}

function togglePick(hash) {
  if (!hash) return;
  if (state.commits.selected.has(hash)) state.commits.selected.delete(hash);
  else {
    const item = state.commits.items.find((i) => i.hash === hash) || { hash };
    state.commits.selected.set(hash, item);
  }
  renderCommitsTable();
}

function applySelectedCommits() {
  const hashes = Array.from(state.commits.selected.keys());
  if (!hashes.length) return;
  state.filter = { mode: 'list', start: null, end: null, hashes };
  state.tab = 'overview';
  syncTabs();
  syncFilterbar();
  onFilterChange();
  toast(`${hashes.length} commit${hashes.length === 1 ? '' : 's'} applied as the commit-list filter.`, 'success');
}

/* ----------------------------------------------------------- authors panel */

function renderAuthorsPanelStructure() {
  const panel = $('#authorsPanel');
  if (!panel) return;
  panel.innerHTML = `
    <div class="search-row"><input type="search" id="authorsPanelSearch" placeholder="Search authors…" value="${esc(state.authorsPanelQuery)}"></div>
    <div id="authorsPanelList"></div>
    <div class="panel-note">Filters every metric to the selected authors' commits (merged identities included).</div>`;
  renderAuthorsPanelList();
  const search = $('#authorsPanelSearch');
  if (search) search.focus();
}

function renderAuthorsPanelList() {
  const list = $('#authorsPanelList');
  if (!list) return;
  const q = state.authorsPanelQuery.trim().toLowerCase();
  const groups = state.allAuthors.filter((g) => !q || (g.name + ' ' + g.email).toLowerCase().includes(q));
  list.innerHTML = groups.length ? groups.map((g) => `
    <label class="author-opt">
      <input type="checkbox" data-aid="${g.id}" ${state.authorIds.includes(g.id) ? 'checked' : ''}>
      <span class="a-name">${esc(g.name || '(no name)')}<br><span class="a-mail">${esc(g.email)}</span></span>
      <span class="a-count">${fmtInt(g.commits)}</span>
    </label>`).join('') : '<div class="empty-note">No authors match.</div>';
}

function toggleAuthorsPanel(force) {
  const panel = $('#authorsPanel');
  if (!panel) return;
  const next = force === undefined ? panel.hidden : force;
  panel.hidden = !next;
  if (next) renderAuthorsPanelStructure();
}

/* ----------------------------------------------------------------- drawer */

function closeDrawer() {
  drawerEl.hidden = true;
  drawerBackdrop.hidden = true;
  drawerEl.innerHTML = '';
  state.drawerTarget = null;
}

async function openObject(kind, path) {
  state.drawerTarget = { kind, path };
  drawerEl.hidden = false;
  drawerBackdrop.hidden = false;
  drawerEl.innerHTML = '<div class="skeleton"><span class="spinner"></span> Loading object…</div>';
  try {
    const data = await api(repoUrl() + '/object?' + filterParams({ kind, path, bucket: state.drawerBucket }));
    renderDrawer(data);
  } catch (e) {
    drawerEl.innerHTML = `<div class="skeleton neg">${esc(e.message)}</div>`;
  }
}

function renderDrawer(d) {
  const t = d.totals;
  const isDir = d.kind === 'dir';
  const ownership = d.authors.map((a) => {
    const pct = Math.round((a.ownership || 0) * 1000) / 10;
    const primary = a.members[0] || { name: '', email: '' };
    const all = a.members.map((m) => `${m.name} <${m.email}>`).join(', ');
    return `
      <div class="own-row">
        <div class="who" title="${esc(all)}">
          ${esc(primary.name || primary.email || 'unknown')}
          ${a.members.length > 1 ? `<span class="tag">${a.members.length}</span>` : ''}
          <small>${esc(primary.email || '')}</small>
        </div>
        <div class="share-bar" title="ω = ${pct}%"><i style="width:${Math.max(1, pct)}%"></i></div>
        <div class="num">
          ω ${pct}% · λ ${fmtInt(a.churn)} · n ${fmtInt(a.modifications)}
          <button class="link" data-act="apply-author" data-id="${a.id}" data-close-drawer="1" title="Filter the dashboard by this author">filter</button>
        </div>
      </div>`;
  }).join('') || '<div class="empty-note">No authors touched this object in the selected range.</div>';

  const bucketSeg = ['day', 'week', 'month'].map((b) =>
    `<button data-act="drawer-bucket" data-bucket="${b}" class="${state.drawerBucket === b ? 'active' : ''}">${cap(b)}</button>`).join('');

  drawerEl.innerHTML = `
    <div class="d-head">
      <div class="d-path">${esc(isDir ? (d.path || '⟨root⟩') + '/' : d.path)}</div>
      <button class="icon-btn" data-act="drawer-close" title="Close">×</button>
    </div>
    <div class="d-chips">
      <span class="tag">${isDir ? 'directory' : 'file'}</span>
      ${d.is_binary ? '<span class="tag">binary — lines not measured</span>' : ''}
      ${!isDir && d.known === false ? '<span class="tag">not in the analysed tree</span>' : ''}
      <span class="tag">H = ${fmtInt(d.commits)} commits</span>
    </div>
    <div class="mini-stats">
      ${miniStat('l+ added', '+' + fmtInt(t.added), 'pos')}
      ${miniStat('l− removed', '−' + fmtInt(t.removed), 'neg')}
      ${miniStat('δ growth', (t.growth >= 0 ? '+' : '−') + fmtInt(Math.abs(t.growth)))}
      ${miniStat('λ churn', fmtInt(t.churn))}
      ${miniStat('n modifications', fmtInt(t.modifications))}
      ${miniStat('η frequency', fmtFrac(t.mod_freq))}
      ${miniStat('ρ churn rate', fmtFrac(t.churn_rate))}
    </div>
    <h4>Activity over time</h4>
    <div class="seg" style="margin-bottom:8px">${bucketSeg}</div>
    <div id="drawerChart"></div>
    <h4>Ownership over H — ω = λ(H,o,a) / λ(H,o)</h4>
    <div class="ownership">${ownership}</div>
    ${isDir ? '<h4>Contents</h4><div id="drawerChildren" class="child-grid"><div class="empty-note">Loading…</div></div>' : ''}
    <div style="margin-top:20px;display:flex;gap:8px">
      <button class="btn sm" data-act="copy-path" data-path="${esc(d.path)}">Copy path</button>
      <button class="btn sm ghost" data-act="drawer-close">Close</button>
    </div>`;
  RATCharts.timeSeries($('#drawerChart'), d.series, { lineKey: 'modifications', lineLabel: 'Modifications' });
  if (isDir) loadDrawerChildren(d.path);
}

async function loadDrawerChildren(prefix) {
  const el = document.getElementById('drawerChildren');
  if (!el) return;
  try {
    const data = await api(repoUrl() + '/objects?' + new URLSearchParams({ prefix: prefix || '' }).toString());
    const items = data.dirs.map((p) => ({ kind: 'dir', path: p, name: p.split('/').pop() + '/' }))
      .concat(data.files.map((p) => ({ kind: 'file', path: p, name: p.split('/').pop() })));
    el.innerHTML = items.length ? items.map((it) => `
      <div class="child-item" data-act="open-object" data-kind="${it.kind}" data-path="${esc(it.path)}">
        <span class="c-name">${esc(it.name)}</span><span class="c-num">${it.kind}</span>
      </div>`).join('') : '<div class="empty-note">Empty directory.</div>';
  } catch (e) {
    el.innerHTML = `<div class="empty-note">${esc(e.message)}</div>`;
  }
}

/* ------------------------------------------------------------- add modal -- */

function setAddMode(mode) {
  state.addMode = mode;
  for (const b of $$('#addMode button')) b.classList.toggle('active', b.dataset.mode === mode);
  const fu = $('#fieldUrl');
  const fz = $('#fieldZip');
  if (fu) fu.hidden = mode !== 'url';
  if (fz) fz.hidden = mode !== 'zip';
}

function openModal() {
  modalBackdrop.hidden = false;
  setAddMode(state.addMode || 'url');
  const url = $('#inpUrl');
  if (url) url.focus();
}

function closeModal() {
  modalBackdrop.hidden = true;
}

async function submitAddForm(ev) {
  ev.preventDefault();
  const btn = $('#addSubmit');
  btn.disabled = true;
  btn.textContent = 'Starting…';
  try {
    let payload;
    if (state.addMode === 'url') {
      const url = $('#inpUrl').value.trim();
      if (!url) throw new Error('Enter a repository URL.');
      payload = await api('/api/repos', {
        json: { url, name: $('#inpName').value.trim(), ref: $('#inpRef').value.trim() },
      });
    } else {
      const f = $('#inpFile').files[0];
      if (!f) throw new Error('Choose a .zip archive to upload.');
      const form = new FormData();
      form.append('file', f);
      form.append('name', $('#inpName').value.trim());
      form.append('ref', $('#inpRef').value.trim());
      payload = await api('/api/repos/upload', { form });
    }
    closeModal();
    toast(`Analysis queued for “${payload.name}”.`, 'success');
    await loadRepos();
    await selectRepo(payload.id, true);
    schedulePoll(500);
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Analyse repository';
  }
}

/* ------------------------------------------------------------ repo select -- */

async function selectRepo(id, force = false) {
  const switched = state.currentId !== id;
  if (!switched && !force) return;
  state.currentId = id;
  try {
    state.detail = await api(repoUrl());
  } catch (e) {
    toast(e.message, 'error');
    return;
  }
  if (switched) {
    clearCache();
    state.allAuthors = [];
    state.authorIds = [];
    state.filter = { mode: 'all', start: null, end: null, hashes: [] };
    state.commits = { page: 0, search: '', selected: new Map(), total: 0, items: [] };
    state.filesQuery = '';
    state.dirsQuery = '';
    state.authorsQuery = '';
    state.authorsExpanded = new Set();
    state.currentItems = { files: [], dirs: [] };
    state.tab = 'overview';
  }
  renderTopbar();
  renderSidebar();
  if (state.detail.status === 'ready') {
    try { await ensureAllAuthors(); } catch (e) { /* non-fatal */ }
    renderDashboard();
  } else {
    renderIngest(state.detail);
  }
  if (needPoll()) schedulePoll(800);
}

/* ------------------------------------------------------- global handlers -- */

const filterFromHashes = debounce(() => {
  const ta = $('#fHashes');
  if (!ta) return;
  const raw = ta.value.replace(/[,;\n\r\t]+/g, ' ').trim();
  const tokens = raw ? raw.split(/\s+/) : [];
  const hashes = [];
  for (const token of tokens) {
    if (/^[0-9a-fA-F]{4,40}$/.test(token)) hashes.push(token.toLowerCase());
  }
  state.filter.hashes = Array.from(new Set(hashes)).slice(0, 2000);
  onFilterChange();
}, 420);

const filterFromDates = debounce(() => {
  const start = dateToEpoch($('#fFrom') ? $('#fFrom').value : '');
  const endDay = dateToEpoch($('#fTo') ? $('#fTo').value : '');
  if (start != null && endDay != null && start > endDay) {
    toast('Start date is after the end date.', 'error');
    return;
  }
  state.filter.start = start;
  state.filter.end = endDay != null ? endDay + 86400 : null; // end date inclusive
  onFilterChange();
}, 260);

const commitsSearch = debounce((value) => {
  state.commits.search = value.trim();
  state.commits.page = 0;
  loadCommitsPage().catch((e) => toast(e.message, 'error'));
}, 380);

async function unmergeMember(fromId) {
  try {
    await api(repoUrl() + '/authors/unmerge', { json: { from_id: fromId } });
    toast('Identity split out.', 'success');
    state.allAuthors = [];
    clearCache();
    await ensureAllAuthors();
    renderTab();
  } catch (e) {
    toast(e.message, 'error');
  }
}

async function splitAll(groupId) {
  const group = (state.currentAuthors || []).find((g) => g.id === groupId);
  if (!group) return;
  const members = group.members.filter((m) => m.id !== group.id).map((m) => m.id);
  try {
    for (const id of members) {
      await api(repoUrl() + '/authors/unmerge', { json: { from_id: id } });
    }
    toast(`${members.length} identities split out.`, 'success');
    state.allAuthors = [];
    clearCache();
    await ensureAllAuthors();
    renderTab();
  } catch (e) {
    toast(e.message, 'error');
  }
}

document.addEventListener('click', async (ev) => {
  const actEl = ev.target.closest('[data-act]');

  if (!ev.target.closest('#authorsPop')) toggleAuthorsPanel(false);

  const rowEl = ev.target.closest('tr[data-row-hash]');
  if (rowEl && !ev.target.closest('input,button,select,a')) {
    togglePick(rowEl.dataset.rowHash);
    return;
  }
  if (!actEl) return;
  const act = actEl.dataset.act;

  if (act === 'select-repo') {
    await selectRepo(Number(actEl.dataset.id));
  } else if (act === 'del-repo') {
    const id = Number(actEl.dataset.id);
    const repo = state.repos.find((r) => r.id === id);
    if (!window.confirm(`Remove “${repo ? repo.name : id}” and all of its analysed data?`)) return;
    try {
      await api('/api/repos/' + id, { method: 'DELETE' });
      toast('Repository removed.', 'success');
      const data = await loadRepos();
      if (state.currentId === id) {
        state.currentId = null;
        state.detail = null;
        if (data.repos.length) await selectRepo(data.repos[0].id);
        else { renderEmpty(); renderTopbar(); }
      }
    } catch (e) { toast(e.message, 'error'); }
  } else if (act === 'open-add') {
    openModal();
  } else if (act === 'tab') {
    state.tab = actEl.dataset.tab;
    syncTabs();
    renderTab();
  } else if (act === 'bucket') {
    state.bucket = actEl.dataset.bucket;
    renderTab();
  } else if (act === 'drawer-bucket') {
    state.drawerBucket = actEl.dataset.bucket;
    if (state.drawerTarget) openObject(state.drawerTarget.kind, state.drawerTarget.path);
  } else if (act === 'f-mode') {
    state.filter.mode = actEl.dataset.mode;
    if (state.filter.mode === 'range' && state.filter.start == null && state.filter.end == null) {
      const ov = state.detail && state.detail.overview;
      if (ov && ov.first_cdate) {
        state.filter.start = ov.first_cdate;
        state.filter.end = ov.last_cdate + 1;
      }
    }
    syncFilterbar();
    onFilterChange();
  } else if (act === 'f-reset') {
    resetFilters(true);
    toast('Filters reset.', 'info', 2200);
  } else if (act === 'toggle-authors') {
    toggleAuthorsPanel();
  } else if (act === 'apply-author') {
    const id = Number(actEl.dataset.id);
    if (!state.authorIds.includes(id)) state.authorIds.push(id);
    if (actEl.dataset.closeDrawer) closeDrawer();
    syncFilterbar();
    onFilterChange();
  } else if (act === 'sort') {
    const scope = actEl.dataset.scope;
    const key = actEl.dataset.key;
    const sortState = scope === 'files' ? state.filesSort : state.dirsSort;
    if (sortState.key === key) sortState.dir = -sortState.dir;
    else { sortState.key = key; sortState.dir = key === 'path' ? 1 : -1; }
    renderCurrentTable(scope);
  } else if (act === 'csv') {
    exportCsv(actEl.dataset.scope);
  } else if (act === 'open-object') {
    openObject(actEl.dataset.kind, actEl.dataset.path);
  } else if (act === 'drawer-close') {
    closeDrawer();
  } else if (act === 'copy-path') {
    try {
      await navigator.clipboard.writeText(actEl.dataset.path);
      toast('Path copied.', 'success', 2000);
    } catch (e) { toast('Clipboard unavailable.', 'error'); }
  } else if (act === 'expand-members') {
    const id = Number(actEl.dataset.id);
    if (state.authorsExpanded.has(id)) state.authorsExpanded.delete(id);
    else state.authorsExpanded.add(id);
    renderAuthorsTable();
  } else if (act === 'merge') {
    const from = Number(actEl.dataset.from);
    const sel = document.getElementById('mergeTarget-' + from);
    const to = sel ? Number(sel.value) : 0;
    if (!to) { toast('Choose a target author to merge into.', 'error'); return; }
    try {
      await api(repoUrl() + '/authors/merge', { json: { from_id: from, to_id: to } });
      toast('Authors merged.', 'success');
      state.allAuthors = [];
      clearCache();
      await ensureAllAuthors();
      renderTab();
    } catch (e) { toast(e.message, 'error'); }
  } else if (act === 'unmerge') {
    await unmergeMember(Number(actEl.dataset.from));
  } else if (act === 'page') {
    const delta = Number(actEl.dataset.delta);
    const pages = Math.max(1, Math.ceil(state.commits.total / 50));
    state.commits.page = Math.min(Math.max(0, state.commits.page + delta), pages - 1);
    try { await loadCommitsPage(); } catch (e) { toast(e.message, 'error'); }
  } else if (act === 'apply-selected') {
    applySelectedCommits();
  } else if (act === 'clear-selection') {
    state.commits.selected.clear();
    renderCommitsTable();
  }
});

document.addEventListener('change', async (ev) => {
  const t = ev.target;
  if (t.matches('input[data-pick]')) {
    const hash = t.dataset.pick;
    if (t.checked) {
      const item = state.commits.items.find((i) => i.hash === hash) || { hash };
      state.commits.selected.set(hash, item);
    } else {
      state.commits.selected.delete(hash);
    }
    updateSelectionUI();
  } else if (t.id === 'pickPage') {
    for (const item of state.commits.items) {
      if (t.checked) state.commits.selected.set(item.hash, item);
      else state.commits.selected.delete(item.hash);
    }
    renderCommitsTable();
  } else if (t.matches('input[data-aid]')) {
    const id = Number(t.dataset.aid);
    if (t.checked) {
      if (!state.authorIds.includes(id)) state.authorIds.push(id);
    } else {
      state.authorIds = state.authorIds.filter((x) => x !== id);
    }
    syncFilterbar();
    onFilterChange();
  } else if (t.id === 'untouchedBox') {
    state.includeUntouched = t.checked;
    clearCache();
    renderTab();
  }
});

document.addEventListener('input', (ev) => {
  const t = ev.target;
  if (t.id === 'fHashes') {
    filterFromHashes();
  } else if (t.id === 'fFrom' || t.id === 'fTo') {
    filterFromDates();
  } else if (t.id === 'filesSearch') {
    state.filesQuery = t.value;
    renderCurrentTable('files');
  } else if (t.id === 'dirsSearch') {
    state.dirsQuery = t.value;
    renderCurrentTable('dirs');
  } else if (t.id === 'authorsSearch') {
    state.authorsQuery = t.value;
    renderAuthorsTable();
  } else if (t.id === 'authorsPanelSearch') {
    state.authorsPanelQuery = t.value;
    renderAuthorsPanelList();
  } else if (t.id === 'commitsSearch') {
    commitsSearch(t.value);
  }
});

document.addEventListener('keydown', (ev) => {
  if (ev.key !== 'Escape') return;
  if (!drawerEl.hidden) closeDrawer();
  else if (!modalBackdrop.hidden) closeModal();
  else toggleAuthorsPanel(false);
});

/* --------------------------------------------------------------- actions -- */

function exportCsv(scope) {
  const items = state.currentItems[scope] || [];
  const headers = ['path', 'added', 'removed', 'growth', 'churn', 'modifications', 'mod_freq', 'churn_rate', 'is_binary'];
  const lines = [headers.join(',')];
  for (const it of items) {
    lines.push([
      csvCell(it.path), it.added, it.removed, it.growth, it.churn, it.modifications,
      Number(it.mod_freq).toFixed(4), Number(it.churn_rate).toFixed(4), it.is_binary == null ? '' : it.is_binary,
    ].join(','));
  }
  const name = (state.detail ? state.detail.name : 'repo').replace(/[^\w.-]+/g, '_');
  download(`rat-${name}-${scope}.csv`, lines.join('\n'));
  toast(`Exported ${items.length} rows to CSV.`, 'success');
}

/* ------------------------------------------------------------------ wire -- */

function wireStaticEvents() {
  const closeBtn = $('#modalClose');
  if (closeBtn) closeBtn.addEventListener('click', closeModal);
  const cancelBtn = $('#modalCancel');
  if (cancelBtn) cancelBtn.addEventListener('click', closeModal);
  const form = $('#addForm');
  if (form) form.addEventListener('submit', submitAddForm);
  for (const b of $$('#addMode button')) {
    b.addEventListener('click', () => setAddMode(b.dataset.mode));
  }
  drawerBackdrop.addEventListener('click', closeDrawer);
  modalBackdrop.addEventListener('click', (ev) => {
    if (ev.target === modalBackdrop) closeModal();
  });
}

/* ------------------------------------------------------------------ boot -- */

async function boot() {
  wireStaticEvents();
  mainEl.innerHTML = '<div class="skeleton"><span class="spinner"></span> Loading repositories…</div>';
  try {
    const data = await loadRepos();
    if (data.repos.length) await selectRepo(data.repos[0].id);
    else { renderEmpty(); renderTopbar(); }
  } catch (e) {
    mainEl.innerHTML = `<div class="card"><b class="neg">Could not reach the API.</b><p class="hint">${esc(e.message)}</p></div>`;
  }
  if (needPoll()) schedulePoll(700);
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
else boot();
