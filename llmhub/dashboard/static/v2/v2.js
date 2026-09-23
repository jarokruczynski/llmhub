/**
 * LLMHub Modern Studio (v2) - Single Page Application
 * Production-ready console with deep API integration and error handling.
 */

(function () {
  'use strict';

  // Baseline prices are served by the hub (GET api/baselines) rather than written here:
  // one table, with the date it was read off each vendor's pricing page and a link to it.
  // Until it arrives there is no baseline, and a cost is shown as unknown rather than guessed.

  // --- State ---
  const state = {
    baselines: null,
    token: localStorage.getItem('llmhub_token') || '',
    activeTab: 'overview',
    theme: localStorage.getItem('llmhub_theme') || 'dark',
    status: null,
    live: [],
    inFlightTotal: 0,
    apps: [],
    usageModelRows: [],
    usagePeriod: 'all',
    savingsBaseline: null,
    usageSearch: '',
    jobs: [],
    queueDepth: {},
    promos: [],
    promosFilter: {
      status: 'all',
      provider: '',
      search: ''
    },
    scoutStatus: null,
    events: [],
    views: {
      models: localStorage.getItem('llmhub_view_models') || 'cards',
      queue: localStorage.getItem('llmhub_view_queue') || 'table',
      accounts: localStorage.getItem('llmhub_view_accounts') || 'cards',
      promos: localStorage.getItem('llmhub_view_promos') || 'cards'
    },
    modelsFilter: {
      status: 'all',
      cap: 'all',
      provider: '',
      search: ''
    },
    sort: {
      models: { col: null, asc: true },
      queue: { col: null, asc: true },
      accounts: { col: null, asc: true },
      promos: { col: null, asc: true },
      usage: { col: 'total_tokens', asc: false }
    },
    editingAlias: null,
    livePollTimer: null,
    statusPollTimer: null,
    playground: {
      inFlight: false,
      abortController: null
    }
  };

  // Never root a request at the server. The console is served at / on this Mac and at /hub/
  // behind the LAN proxy, which strips that prefix, so every path is resolved against the
  // document base (<base href="./">) and inherits whatever prefix the page was opened under.
  function apiUrl(path) {
    if (path.startsWith('http')) return path;
    return path.replace(/^\/+/, '');
  }

  // --- API Fetch Helper ---
  async function api(path, options = {}) {
    const url = apiUrl(path);
    const headers = {
      'Accept': 'application/json',
      ...options.headers
    };
    if (state.token) {
      headers['Authorization'] = 'Bearer ' + state.token;
    }
    if (options.body && typeof options.body === 'object' && !(options.body instanceof FormData)) {
      headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(options.body);
    }
    const res = await fetch(url, { ...options, headers });
    if (res.status === 401) {
      notify('Authentication required (401). Set token in top bar.', 'error');
    }
    return res;
  }

  // --- Notifications / Toast ---
  function notify(message, type = 'info') {
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.style.cssText = `
      position: fixed; bottom: 24px; right: 24px; z-index: 9999;
      background: ${type === 'error' ? 'var(--status-depleted)' : type === 'success' ? 'var(--status-ready)' : 'var(--bg-card)'};
      color: white; padding: 12px 20px; border-radius: 8px; font-size: 0.88rem;
      box-shadow: var(--shadow-lg); transition: all 0.3s cubic-bezier(0.16, 1, 0.3, 1);
      display: flex; align-items: center; gap: 8px; border: 1px solid var(--border-subtle);
      max-width: 450px; word-break: break-word;
    `;
    toast.innerHTML = `<span>${escapeHtml(message)}</span>`;
    document.body.appendChild(toast);
    setTimeout(() => {
      toast.style.opacity = '0';
      toast.style.transform = 'translateY(10px)';
      setTimeout(() => toast.remove(), 300);
    }, 4000);
  }

  function escapeHtml(str) {
    if (!str) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  function formatNumber(num) {
    if (num === null || num === undefined) return '-';
    return Number(num).toLocaleString();
  }

  function formatTokens(num) {
    if (!num) return '0';
    if (num >= 1000000) return (num / 1000000).toFixed(1) + 'M';
    if (num >= 1000) return (num / 1000).toFixed(1) + 'k';
    return String(num);
  }

  function formatCountdown(isoString) {
    if (!isoString) return '';
    const target = new Date(isoString).getTime();
    const now = Date.now();
    const diff = Math.max(0, Math.floor((target - now) / 1000));
    if (diff === 0) return 'now';
    const m = Math.floor(diff / 60);
    const s = diff % 60;
    if (m > 60) {
      const h = Math.floor(m / 60);
      return `${h}h ${m % 60}m`;
    }
    return `${m}m ${s}s`;
  }

  function formatTimeAgo(isoString) {
    if (!isoString) return 'Never';
    const target = new Date(isoString).getTime();
    const now = Date.now();
    const diff = Math.max(0, Math.floor((now - target) / 1000));
    if (diff < 60) return 'Just now';
    const m = Math.floor(diff / 60);
    if (m < 60) return `${m}m ago`;
    const h = Math.floor(m / 60);
    if (h < 24) return `${h}h ago`;
    const d = Math.floor(h / 24);
    return `${d}d ago`;
  }

  // --- Universal Table Sorting (Item 3) ---
  function renderSortHeader(tableId, colKey, label) {
    const current = state.sort[tableId] || {};
    const isSorted = current.col === colKey;
    const sortClass = isSorted ? (current.asc ? 'sorted-asc' : 'sorted-desc') : '';
    return `<th class="th-sortable ${sortClass}" onclick="handleTableSort('${tableId}', '${colKey}')">${escapeHtml(label)}<span class="sort-arrow"></span></th>`;
  }

  window.handleTableSort = function (tableId, colKey) {
    const current = state.sort[tableId] || { col: null, asc: true };
    if (current.col === colKey) {
      current.asc = !current.asc;
    } else {
      current.col = colKey;
      const descCols = ['tokens', 'requests', 'latency', 'in_tokens', 'out_tokens', 'cached_tokens', 'total_tokens', 'est_cost', 'created_at', 'last_checked_at'];
      current.asc = !descCols.some(c => colKey.toLowerCase().includes(c));
    }
    state.sort[tableId] = current;

    if (tableId === 'models') renderModels();
    else if (tableId === 'queue') renderJobs();
    else if (tableId === 'accounts') renderAccounts();
    else if (tableId === 'promos') renderPromos();
    else if (tableId === 'usage') renderUsage();
  };

  function applySort(items, tableId, keyExtractor) {
    const sort = state.sort[tableId];
    if (!sort || !sort.col) return items;

    return [...items].sort((a, b) => {
      let valA = keyExtractor(a, sort.col);
      let valB = keyExtractor(b, sort.col);

      if (valA === undefined || valA === null) valA = '';
      if (valB === undefined || valB === null) valB = '';

      if (typeof valA === 'number' && typeof valB === 'number') {
        return sort.asc ? valA - valB : valB - valA;
      }
      const cmp = String(valA).localeCompare(String(valB), undefined, { numeric: true, sensitivity: 'base' });
      return sort.asc ? cmp : -cmp;
    });
  }

  // --- Theme Management ---
  function initTheme() {
    if (state.theme === 'light') {
      document.documentElement.classList.add('light-theme');
    } else {
      document.documentElement.classList.remove('light-theme');
    }
    const themeBtn = document.getElementById('theme-toggle');
    if (themeBtn) {
      themeBtn.innerHTML = state.theme === 'light' ? '🌙 Dark' : '☀️ Light';
    }
  }

  function toggleTheme() {
    state.theme = state.theme === 'light' ? 'dark' : 'light';
    localStorage.setItem('llmhub_theme', state.theme);
    initTheme();
  }

  // --- Data Fetching ---
  function timeAgo(ts) {
    if (!ts) return '';
    const then = new Date(ts).getTime();
    if (!Number.isFinite(then)) return '';
    const secs = Math.max(0, Math.round((Date.now() - then) / 1000));
    if (secs < 90) return `${secs}s ago`;
    const mins = Math.round(secs / 60);
    if (mins < 90) return `${mins}m ago`;
    const hours = Math.round(mins / 60);
    if (hours < 36) return `${hours}h ago`;
    return `${Math.round(hours / 24)}d ago`;
  }

  // The newest thing that happened to this pair, not merely the newest failure on record. A
  // success after a refusal retires the refusal, so a card stops flagging a quota error from
  // three days ago while the model is serving traffic today.
  //
  // Whether an unretired refusal still counts is the hub's call, not the page's: past its
  // recent-failure window the hub reports "ok", and a red banner under a green badge is the
  // contradiction this is here to avoid. Such a refusal stays on the card as muted history.
  function currentFailure(m) {
    if (!m.last_error) return null;
    if (m.last_ok_at && m.last_error_at && m.last_ok_at >= m.last_error_at) return null;
    return { text: m.last_error, when: timeAgo(m.last_error_at), stale: m.status === 'ok' };
  }

  function baselineById(id) {
    return (state.baselines?.baselines || []).find((b) => b.id === id) || null;
  }

  function priceFor(modelName, baselineKey) {
    const table = state.baselines;
    if (!table) return null;
    const tier = table.tier_matched;
    if (tier && baselineKey === tier.id) {
      const name = (modelName || '').toLowerCase();
      const small = (tier.markers || []).some((marker) => name.includes(marker));
      return baselineById(small ? tier.small : tier.large);
    }
    return baselineById(baselineKey) || baselineById(table.default);
  }

  async function loadBaselines() {
    try {
      const res = await api('api/baselines');
      if (res.ok) {
        state.baselines = await res.json();
        populateBaselineOptions();
      }
    } catch (e) {
      console.error('Error loading baselines:', e);
    }
  }

  function populateBaselineOptions() {
    const select = document.getElementById('savings-baseline-select');
    const table = state.baselines;
    if (!select || !table) return;
    const known = new Set((table.baselines || []).map((b) => b.id));
    if (table.tier_matched) known.add(table.tier_matched.id);
    const chosen = known.has(state.savingsBaseline) ? state.savingsBaseline : table.default;
    const options = (table.baselines || []).map(
      (b) => `<option value="${b.id}">${b.label} ($${b.input} / $${b.output})</option>`
    );
    if (table.tier_matched) {
      options.push(`<option value="${table.tier_matched.id}">${table.tier_matched.label}</option>`);
    }
    select.innerHTML = options.join('');
    select.value = chosen;
    state.savingsBaseline = select.value;
  }

  // --- Prompt recorder -------------------------------------------------
  // Deliberately not polled with the rest of the console: it only runs while the tab is open
  // and something is still coming in, because this is the one view that pulls prompt text over
  // HTTP. Each app is a session with its own chat pane; up to four are shown side by side.
  //
  // Nothing already on screen is redrawn. A poll asks only for what changed since the last one
  // (`since=rev`), new requests are appended, and only the answers of a request still in flight
  // are replaced. Redrawing the whole list every two seconds is what threw every scroll back to
  // the top.
  const REC_MAX_PANES = 4;
  const REC_POLL_MS = 1500;
  const REC_CLAMP_CHARS = 1400;
  const REC_CLAMP_LINES = 18;
  const REC_ACTIVE_MS = 60000;
  const REC_OPEN = ['queued', 'pending', 'streaming'];

  function readStoredList(key) {
    try {
      const raw = JSON.parse(localStorage.getItem(key) || '[]');
      return Array.isArray(raw) ? raw.map(String) : [];
    } catch (e) {
      return [];
    }
  }

  function writeStoredList(key, values) {
    try {
      localStorage.setItem(key, JSON.stringify([...values]));
    } catch (e) {
      /* a private window: the choice lasts until reload */
    }
  }

  const rec = {
    timer: null,
    ticker: null,
    rev: null,
    startedAt: undefined,
    status: null,
    entries: new Map(),
    visible: new Set(readStoredList('llmhub_rec_visible')),
    hidden: new Set(readStoredList('llmhub_rec_hidden')),
    panes: new Map(),
    expanded: new Set(),
    loading: false
  };
  // the recorder list used to be global; kept so switchTab can stop it the same way
  let recorderTimer = null;

  function saveRecorderChoice() {
    writeStoredList('llmhub_rec_visible', rec.visible);
    writeStoredList('llmhub_rec_hidden', rec.hidden);
  }

  function clockOf(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d)) return '';
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }

  function secondsOf(ms) {
    if (ms === null || ms === undefined) return '';
    if (ms < 1000) return `${ms} ms`;
    if (ms < 60000) return `${(ms / 1000).toFixed(1)} s`;
    return minutesOf(ms);
  }

  // how long something took, read at a glance: whole seconds, minutes once there are any
  function minutesOf(ms) {
    const total = Math.max(0, Math.round((ms || 0) / 1000));
    const m = Math.floor(total / 60);
    const s = total % 60;
    return m ? `${m} min ${String(s).padStart(2, '0')} s` : `${s} s`;
  }

  function statusClass(status) {
    const s = String(status || '');
    if (s === 'ok') return 'ok';
    if (s === 'pending') return 'pending';
    if (s === 'queued') return 'queued';
    if (s === 'streaming') return 'streaming';
    if (s === 'cancelled') return 'cancelled';
    return 'failed';
  }

  function sessionsOf() {
    const sessions = new Map();
    for (const entry of rec.entries.values()) {
      let session = sessions.get(entry.app);
      if (!session) {
        session = { app: entry.app, requests: new Map(), firstId: entry.id, lastAt: 0, open: 0, tokIn: 0, tokOut: 0, estimated: false, latencies: [] };
        sessions.set(entry.app, session);
      }
      session.firstId = Math.min(session.firstId, entry.id);
      const attempts = session.requests.get(entry.request_id) || [];
      attempts.push(entry);
      session.requests.set(entry.request_id, attempts);
      const at = new Date(entry.ts || entry.sent_at).getTime();
      if (at > session.lastAt) session.lastAt = at;
      if (REC_OPEN.includes(entry.status)) session.open += 1;
      if (entry.tokens) {
        session.tokIn += entry.tokens.in || 0;
        session.tokOut += entry.tokens.out || 0;
        if (entry.tokens.estimated) session.estimated = true;
      }
      if (entry.status === 'ok' && entry.latency_ms) session.latencies.push(entry.latency_ms);
    }
    for (const session of sessions.values()) {
      for (const attempts of session.requests.values()) attempts.sort((a, b) => a.attempt - b.attempt || a.id - b.id);
    }
    return [...sessions.values()].sort((a, b) => a.firstId - b.firstId);
  }

  function sessionSummary(session) {
    const bits = [`${session.requests.size} req`];
    if (session.tokIn || session.tokOut) {
      const approx = session.estimated ? '~' : '';
      bits.push(`in ${approx}${formatTokens(session.tokIn)} · out ${approx}${formatTokens(session.tokOut)} tok`);
    }
    if (session.latencies.length) {
      const avg = session.latencies.reduce((a, b) => a + b, 0) / session.latencies.length;
      bits.push(`avg ${secondsOf(Math.round(avg))}`);
    }
    if (session.open) bits.push(`${session.open} waiting`);
    return bits.join(' · ');
  }

  function isActive(session) {
    return session.open > 0 || Date.now() - session.lastAt < REC_ACTIVE_MS;
  }

  // a session nobody has decided about yet is shown while there is room for it
  function adoptNewSessions(sessions) {
    let changed = false;
    const present = new Set(sessions.map((s) => s.app));
    let shown = [...rec.visible].filter((app) => present.has(app)).length;
    for (const session of sessions) {
      if (rec.visible.has(session.app) || rec.hidden.has(session.app)) continue;
      if (shown >= REC_MAX_PANES) break;
      rec.visible.add(session.app);
      shown += 1;
      changed = true;
    }
    if (changed) saveRecorderChoice();
  }

  function toggleSession(app) {
    if (rec.visible.has(app)) {
      rec.visible.delete(app);
      rec.hidden.add(app);
    } else {
      rec.hidden.delete(app);
      const present = new Set(sessionsOf().map((s) => s.app));
      const shown = [...rec.visible].filter((a) => present.has(a));
      if (shown.length >= REC_MAX_PANES) {
        // the one shown longest makes room; the set keeps insertion order
        const oldest = shown[0];
        rec.visible.delete(oldest);
        rec.hidden.add(oldest);
        notify(`At most ${REC_MAX_PANES} sessions side by side: hid ${oldest}`, 'info');
      }
      rec.visible.add(app);
    }
    saveRecorderChoice();
    renderRecorderView();
  }

  function renderSessionBar(sessions) {
    const bar = document.getElementById('recorder-sessions');
    if (!bar) return;
    if (!sessions.length) {
      bar.innerHTML = '<span class="text-muted rec-bar-empty">No sessions yet. Each app that calls the hub while recording gets its own.</span>';
      return;
    }
    bar.innerHTML = sessions
      .map((session) => {
        const on = rec.visible.has(session.app);
        const active = isActive(session);
        return `<button type="button" class="rec-chip${on ? ' on' : ''}" data-app="${escapeHtml(session.app)}" aria-pressed="${on}" title="${on ? 'Hide' : 'Show'} this session">
          <span class="rec-dot${session.open ? ' waiting' : active ? ' active' : ''}"></span>
          <span class="rec-chip-name">${escapeHtml(session.app)}</span>
          <span class="rec-chip-meta">${escapeHtml(sessionSummary(session))}</span>
          <span class="rec-chip-eye">${on ? 'visible' : 'hidden'}</span>
        </button>`;
      })
      .join('');
  }

  // --- one message, one request ---
  function messageBlock(key, role, text, extraClass = '') {
    const body = text || '(empty)';
    const long = body.length > REC_CLAMP_CHARS || body.split('\n').length > REC_CLAMP_LINES;
    const open = rec.expanded.has(key);
    return `<div class="rec-msg rec-role-${escapeHtml(role)} ${extraClass}">
      <div class="rec-role">${escapeHtml(role)}</div>
      <div class="rec-text${long && !open ? ' clamped' : ''}" data-key="${escapeHtml(key)}">${escapeHtml(body)}</div>
      ${long ? `<button type="button" class="rec-more" data-key="${escapeHtml(key)}">${open ? 'Show less' : `Show all · ${formatNumber(body.length)} chars`}</button>` : ''}
    </div>`;
  }

  function sameMessage(a, b) {
    return a && b && a.role === b.role && a.text === b.text;
  }

  // Apps resend the whole conversation on every call. The part a previous request of the same
  // session already showed is folded away, so a chat reads as a chat and a batch job with one
  // fixed system prompt shows only what changed.
  function repeatedPrefix(session, requestId, messages) {
    let best = { count: 0, from: null };
    const earlier = [...session.requests.keys()].filter((id) => id < requestId).sort((a, b) => b - a).slice(0, 20);
    for (const id of earlier) {
      const previous = session.requests.get(id)[0];
      const before = previous.messages || [];
      let n = 0;
      while (n < before.length && n < messages.length && sameMessage(before[n], messages[n])) n += 1;
      if (n === before.length && n < messages.length) {
        const answered = session.requests.get(id).find((row) => row.status === 'ok');
        if (answered && messages[n].role === 'assistant' && messages[n].text === answered.answer) n += 1;
      }
      if (n > best.count) best = { count: n, from: previous };
      if (n === messages.length) break;
    }
    if (best.count) {
      // answers this pane already shows under their own request, echoed back as history
      const answers = new Set();
      for (const id of earlier) {
        for (const row of session.requests.get(id)) if (row.status === 'ok' && row.answer) answers.add(row.answer);
      }
      while (best.count < messages.length && messages[best.count].role === 'assistant' && answers.has(messages[best.count].text)) {
        best.count += 1;
      }
    }
    return best.count >= messages.length ? { count: Math.max(0, messages.length - 1), from: best.from } : best;
  }

  function promptBubble(session, requestId, first) {
    const messages = first.messages || [];
    const fold = repeatedPrefix(session, requestId, messages);
    const foldKey = `${requestId}:fold`;
    const folded = fold.count && !rec.expanded.has(foldKey);
    const shownFrom = folded ? fold.count : 0;
    const parts = [];
    if (fold.count) {
      parts.push(`<button type="button" class="rec-fold" data-key="${foldKey}">${
        folded
          ? `⋯ ${fold.count} earlier message${fold.count > 1 ? 's' : ''} same as the request sent ${clockOf(fold.from.sent_at)} · show`
          : 'hide the repeated messages'
      }</button>`);
    }
    messages.forEach((m, i) => {
      if (i < shownFrom) return;
      parts.push(messageBlock(`${requestId}:m${i}`, m.role, m.text, i < fold.count ? 'rec-repeated' : ''));
    });
    if (!messages.length) parts.push(messageBlock(`${requestId}:m0`, 'prompt', first.prompt));
    return `<div class="rec-bubble rec-prompt">${parts.join('')}</div>`;
  }

  function promptHtml(session, requestId, first) {
    const meta = [
      `sent ${clockOf(first.sent_at)}`,
      first.model_request && `asked for ${first.model_request}`,
      first.kind,
      first.truncated ? 'clipped by the recorder' : ''
    ].filter(Boolean);
    return `${promptBubble(session, requestId, first)}
      <div class="rec-stamp rec-stamp-right">${escapeHtml(meta.join(' · '))}</div>`;
  }

  function tokensLine(tokens) {
    if (!tokens) return '';
    const approx = tokens.estimated ? '~' : '';
    const bits = [`in ${approx}${formatNumber(tokens.in)}`, `out ${approx}${formatNumber(tokens.out)}`];
    if (tokens.cached) bits.push(`cached ${formatNumber(tokens.cached)}`);
    if (tokens.reasoning) bits.push(`reasoning ${formatNumber(tokens.reasoning)}`);
    return `${bits.join(' · ')} tok${tokens.estimated ? ' (estimated)' : ''}`;
  }

  function answerHtml(row, first = row) {
    const cls = statusClass(row.status);
    const who = [row.model, row.account].filter(Boolean).join(' · ');
    const queue = row.queued_ms >= 1000 ? `queued ${minutesOf(row.queued_ms)}` : '';
    if (cls === 'queued') {
      return `<div class="rec-answer rec-queued">
        <div class="rec-bubble rec-reply"><span class="rec-spinner"></span> queued, waiting for a free slot</div>
        <div class="rec-stamp"><span class="rec-took rec-wait" data-since="${escapeHtml(row.sent_at)}" data-label="in queue">0 s</span> · asked for ${escapeHtml(row.model_request || 'auto')}</div>
      </div>`;
    }
    if (cls === 'pending' || cls === 'streaming') {
      const label = cls === 'pending' ? 'waiting for' : 'streaming from';
      return `<div class="rec-answer rec-${cls}">
        <div class="rec-bubble rec-reply"><span class="rec-spinner"></span> ${label} ${escapeHtml(row.model || 'the model')}</div>
        <div class="rec-stamp"><span class="rec-took rec-wait" data-since="${escapeHtml(row.started_at || row.sent_at)}" data-label="${cls === 'pending' ? 'thinking' : 'streaming'}">0 s</span>${
          queue ? ` · ${queue}` : ''
        }${row.first_ms != null ? ` · headers after ${secondsOf(row.first_ms)}` : ''} · attempt ${row.attempt} · ${escapeHtml(who)}</div>
      </div>`;
    }
    const reasoning = row.reasoning
      ? `<details class="rec-reasoning"><summary>reasoning · ${formatNumber(row.reasoning.length)} chars</summary>${messageBlock(`a${row.id}:r`, 'reasoning', row.reasoning)}</details>`
      : '';
    const took = cls === 'ok' ? 'answered in' : cls === 'cancelled' || row.status === 'not sent' ? 'ended after' : 'refused after';
    // from the moment the app sent the request, not from this attempt: queue and earlier
    // refusals are part of how long the app waited
    const wall = new Date(row.ts).getTime() - new Date(first.sent_at).getTime();
    const total = Number.isFinite(wall) && wall >= 0 ? wall : (row.latency_ms || 0) + (row.queued_ms || 0);
    const split = total - (row.latency_ms || 0) >= 1000;
    const stamp = [
      clockOf(row.ts),
      split ? `${queue || (row.attempt > 1 ? `after ${row.attempt - 1} refused` : 'waiting')} + model ${minutesOf(row.latency_ms)}` : '',
      row.first_ms != null && row.kind === 'stream' ? `first byte ${secondsOf(row.first_ms)}` : '',
      cls !== 'ok' ? row.status : '',
      `attempt ${row.attempt}`,
      who,
      tokensLine(row.tokens)
    ].filter(Boolean);
    return `<div class="rec-answer rec-${cls}">
      <div class="rec-bubble rec-reply">${reasoning}${messageBlock(`a${row.id}:t`, cls === 'ok' ? 'assistant' : row.status, row.answer)}</div>
      <div class="rec-stamp"><span class="rec-took">${took} ${minutesOf(total)}</span> · ${escapeHtml(stamp.filter(Boolean).join(' · '))}</div>
    </div>`;
  }

  function answersSignature(attempts) {
    return attempts.map((row) => `${row.id}:${row.rev}`).join(',');
  }

  // --- panes ---
  function createPane(app) {
    const root = document.createElement('div');
    root.className = 'rec-pane';
    root.dataset.app = app;
    root.innerHTML = `
      <div class="rec-pane-head">
        <span class="rec-dot"></span>
        <span class="rec-pane-title">${escapeHtml(app)}</span>
        <span class="rec-pane-meta"></span>
        <button type="button" class="rec-pane-close" title="Hide this session">✕</button>
      </div>
      <div class="rec-pane-body"></div>
      <button type="button" class="rec-jump" hidden>↓ new</button>`;
    const pane = {
      app,
      root,
      body: root.querySelector('.rec-pane-body'),
      meta: root.querySelector('.rec-pane-meta'),
      dot: root.querySelector('.rec-dot'),
      jump: root.querySelector('.rec-jump'),
      nodes: new Map(),
      unseen: 0,
      // follows the newest message until the reader scrolls away from the bottom
      stuck: true
    };
    root.querySelector('.rec-pane-close').addEventListener('click', () => toggleSession(app));
    pane.jump.addEventListener('click', () => {
      pane.body.scrollTop = pane.body.scrollHeight;
    });
    pane.body.addEventListener('scroll', () => {
      pane.stuck = nearBottom(pane.body);
      if (pane.stuck) {
        pane.unseen = 0;
        pane.jump.hidden = true;
      }
    });
    pane.body.addEventListener('click', onRecorderClick);
    return pane;
  }

  function nearBottom(el) {
    return el.scrollHeight - el.scrollTop - el.clientHeight < 48;
  }

  function syncPane(pane, session) {
    let added = 0;
    const ids = [...session.requests.keys()].sort((a, b) => a - b);
    for (const id of ids) {
      const attempts = session.requests.get(id);
      let node = pane.nodes.get(id);
      if (!node) {
        node = document.createElement('div');
        node.className = 'rec-req';
        node.dataset.req = String(id);
        node.innerHTML = `${promptHtml(session, id, attempts[0])}<div class="rec-answers"></div>`;
        // requests can begin out of order when one waited for a slot; keep them by id
        const after = [...pane.nodes.keys()].filter((other) => other > id).sort((a, b) => a - b)[0];
        pane.body.insertBefore(node, after !== undefined ? pane.nodes.get(after) : null);
        pane.nodes.set(id, node);
        added += 1;
      }
      const signature = answersSignature(attempts);
      if (node.dataset.sig !== signature) {
        node.dataset.sig = signature;
        node.querySelector('.rec-answers').innerHTML = attempts.map((row) => answerHtml(row, attempts[0])).join('');
      }
    }
    // entries the ring pushed out
    for (const [id, node] of pane.nodes) {
      if (!session.requests.has(id)) {
        node.remove();
        pane.nodes.delete(id);
      }
    }
    pane.meta.textContent = sessionSummary(session);
    pane.dot.className = `rec-dot${session.open ? ' waiting' : isActive(session) ? ' active' : ''}`;
    if (pane.stuck) {
      pane.body.scrollTop = pane.body.scrollHeight;
    } else if (added) {
      pane.unseen += added;
      pane.jump.hidden = false;
      pane.jump.textContent = `↓ ${pane.unseen} new`;
    }
  }

  function keepStuckPanesAtBottom() {
    for (const pane of rec.panes.values()) {
      if (pane.stuck) pane.body.scrollTop = pane.body.scrollHeight;
    }
  }

  function renderPanes(sessions) {
    const grid = document.getElementById('recorder-grid');
    if (!grid) return;
    const shown = sessions.filter((s) => rec.visible.has(s.app));
    // a pane that stays keeps its node: moving a node in the DOM would reset its scroll
    for (const [app, pane] of rec.panes) {
      if (!shown.some((s) => s.app === app)) {
        pane.root.remove();
        rec.panes.delete(app);
      }
    }
    const empty = document.getElementById('recorder-empty');
    for (const session of shown) {
      if (!rec.panes.has(session.app)) {
        const pane = createPane(session.app);
        rec.panes.set(session.app, pane);
        grid.appendChild(pane.root);
      }
    }
    // the layout settles before any pane decides whether it sits at the bottom
    grid.dataset.n = String(Math.min(rec.panes.size, REC_MAX_PANES));
    fitRecorderGrid();
    for (const session of shown) syncPane(rec.panes.get(session.app), session);
    tickWaits();
    if (empty) {
      const on = rec.status && rec.status.recording;
      empty.hidden = rec.panes.size > 0;
      empty.textContent = sessions.length
        ? 'Every session is hidden. Pick one in the bar above.'
        : on
          ? 'Recording. Nothing has gone through the hub yet.'
          : 'Nothing recorded. Press start, then use an app.';
    }
    fitRecorderGrid();
  }

  function fitRecorderGrid() {
    const grid = document.getElementById('recorder-grid');
    if (!grid || !grid.offsetParent) return;
    if (document.body.classList.contains('rec-focus') || window.innerWidth <= 900) {
      grid.style.height = '';
      keepStuckPanesAtBottom();
      return;
    }
    const top = grid.getBoundingClientRect().top + window.scrollY;
    grid.style.height = `${Math.max(360, window.innerHeight - top - 20)}px`;
    keepStuckPanesAtBottom();
  }

  function renderRecorderHeader() {
    const data = rec.status || {};
    const stateEl = document.getElementById('recorder-state');
    const metaEl = document.getElementById('recorder-meta');
    const toggle = document.getElementById('recorder-toggle');
    const on = !!data.recording;
    if (stateEl) {
      stateEl.textContent = on ? `recording · ${Math.ceil((data.seconds_left || 0) / 60)} min left` : 'off';
      stateEl.classList.toggle('rec-on', on);
      stateEl.dataset.on = on ? '1' : '';
    }
    if (toggle) toggle.textContent = on ? 'Stop recording' : 'Start recording';
    if (metaEl && data.max_entries) {
      const bits = [`${formatNumber(data.count)} of ${formatNumber(data.max_entries)} attempts kept`];
      if (data.dropped) bits.push(`${formatNumber(data.dropped)} dropped`);
      metaEl.textContent = bits.join(' · ');
    }
  }

  function renderRecorderView() {
    const sessions = sessionsOf();
    adoptNewSessions(sessions);
    renderSessionBar(sessions);
    renderPanes(sessions);
  }

  function resetRecorderView() {
    rec.rev = null;
    rec.entries.clear();
    rec.expanded.clear();
    for (const pane of rec.panes.values()) pane.root.remove();
    rec.panes.clear();
  }

  function onRecorderClick(event) {
    const more = event.target.closest('.rec-more');
    if (more) {
      const key = more.dataset.key;
      const text = more.parentElement.querySelector('.rec-text');
      const open = !rec.expanded.has(key);
      if (open) rec.expanded.add(key);
      else rec.expanded.delete(key);
      text.classList.toggle('clamped', !open);
      more.textContent = open ? 'Show less' : `Show all · ${formatNumber(text.textContent.length)} chars`;
      return;
    }
    const fold = event.target.closest('.rec-fold');
    if (fold) {
      const key = fold.dataset.key;
      if (rec.expanded.has(key)) rec.expanded.delete(key);
      else rec.expanded.add(key);
      const node = fold.closest('.rec-req');
      const pane = rec.panes.get(fold.closest('.rec-pane').dataset.app);
      const session = sessionsOf().find((s) => s.app === pane.app);
      const id = Number(node.dataset.req);
      if (!session || !session.requests.has(id)) return;
      node.querySelector('.rec-prompt').outerHTML = promptBubble(session, id, session.requests.get(id)[0]);
    }
  }

  function tickWaits() {
    const now = Date.now();
    document.querySelectorAll('#recorder-grid .rec-wait').forEach((el) => {
      const since = new Date(el.dataset.since).getTime();
      if (!isNaN(since)) el.textContent = `${el.dataset.label || 'waiting'} ${minutesOf(now - since)}`;
    });
  }

  async function loadRecorder() {
    if (rec.loading) return;
    rec.loading = true;
    try {
      const url = Number.isInteger(rec.rev) ? `api/recorder?since=${rec.rev}` : 'api/recorder';
      const res = await api(url);
      if (res.status === 401) {
        resetRecorderView();
        const empty = document.getElementById('recorder-empty');
        if (empty) {
          empty.hidden = false;
          empty.textContent = 'This tab needs the token. Set it in the top bar.';
        }
        scheduleRecorderPoll(false);
        return;
      }
      if (!res.ok) {
        // an answer the next poll cannot build on: start again from a full read
        rec.rev = null;
        scheduleRecorderPoll(state.activeTab === 'recorder');
        return;
      }
      const data = await res.json();
      if (data.started_at !== rec.startedAt) {
        // a new run cleared the buffer; what is on screen belongs to the old one
        const partial = Number.isInteger(rec.rev);
        resetRecorderView();
        rec.startedAt = data.started_at;
        if (partial) {
          rec.rev = null;
          rec.loading = false;
          return loadRecorder();
        }
      }
      for (const row of data.entries || []) rec.entries.set(row.id, row);
      if (data.first_id != null) {
        for (const id of rec.entries.keys()) if (id < data.first_id) rec.entries.delete(id);
      } else if (!data.count) {
        rec.entries.clear();
      }
      rec.rev = Number.isInteger(data.rev) ? data.rev : null;
      rec.status = data;
      renderRecorderHeader();
      renderRecorderView();
      const open = [...rec.entries.values()].some((row) => REC_OPEN.includes(row.status));
      scheduleRecorderPoll(state.activeTab === 'recorder' && (data.recording || open));
    } catch (e) {
      console.error('Error loading recorder:', e);
      scheduleRecorderPoll(state.activeTab === 'recorder');
    } finally {
      rec.loading = false;
    }
  }

  function scheduleRecorderPoll(keepGoing) {
    if (recorderTimer) {
      clearTimeout(recorderTimer);
      recorderTimer = null;
    }
    if (keepGoing) recorderTimer = setTimeout(loadRecorder, REC_POLL_MS);
    if (keepGoing && !rec.ticker) rec.ticker = setInterval(tickWaits, 1000);
    if (!keepGoing && rec.ticker) {
      clearInterval(rec.ticker);
      rec.ticker = null;
    }
  }

  async function toggleRecorder() {
    const on = !!(rec.status && rec.status.recording);
    const minutes = parseInt(document.getElementById('recorder-minutes')?.value || '20', 10);
    const res = on
      ? await api('api/recorder/stop', { method: 'POST' })
      : await api('api/recorder/start', { method: 'POST', body: { minutes } });
    if (res.status === 401) {
      notify('Recorder needs the token. Set it in the top bar.', 'error');
      return;
    }
    if (!res.ok) {
      notify('Recorder did not respond', 'error');
      return;
    }
    notify(on ? 'Recording stopped' : `Recording for ${minutes} min`, 'success');
    loadRecorder();
  }

  function toggleRecorderFocus(force) {
    const on = typeof force === 'boolean' ? force : !document.body.classList.contains('rec-focus');
    document.body.classList.toggle('rec-focus', on);
    const btn = document.getElementById('recorder-focus');
    if (btn) btn.textContent = on ? 'Exit full screen' : 'Full screen';
    fitRecorderGrid();
  }

  function setupRecorder() {
    document.getElementById('recorder-toggle')?.addEventListener('click', toggleRecorder);
    document.getElementById('recorder-focus')?.addEventListener('click', () => toggleRecorderFocus());
    document.getElementById('recorder-sessions')?.addEventListener('click', (event) => {
      const chip = event.target.closest('.rec-chip');
      if (chip) toggleSession(chip.dataset.app);
    });
    window.addEventListener('resize', fitRecorderGrid);
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && document.body.classList.contains('rec-focus')) toggleRecorderFocus(false);
    });
  }

  async function loadStatus() {
    try {
      const res = await api('api/status');
      if (res.ok) {
        state.status = await res.json();
        renderOverview();
        renderModels();
        renderRouting();
        renderAccounts();
        populatePlaygroundModels();
        updateTopMetrics();
      }
    } catch (e) {
      console.error('Error loading status:', e);
    }
  }

  async function loadLive() {
    try {
      const res = await api('api/live?window_min=15');
      if (res.ok) {
        const data = await res.json();
        state.inFlightTotal = data.in_flight_total || 0;
        const calls = [];
        (data.apps || []).forEach(a => {
          (a.in_flight || []).forEach(c => {
            calls.push({ ...c, app: a.app });
          });
        });
        state.live = calls;
        updateLiveIndicator();
        renderOverviewLive();
        renderLiveDrawer();
      }
    } catch (e) {
      console.error('Error loading live calls:', e);
    }
  }

  async function loadUsage() {
    try {
      let sinceParam = '';
      if (state.usagePeriod === 'today') {
        sinceParam = '&since=' + new Date().toISOString().slice(0, 10);
      } else if (state.usagePeriod === '7d') {
        sinceParam = '&since=' + new Date(Date.now() - 7 * 86400000).toISOString().slice(0, 10);
      } else if (state.usagePeriod === '30d') {
        sinceParam = '&since=' + new Date(Date.now() - 30 * 86400000).toISOString().slice(0, 10);
      }

      const res = await api('api/usage?group_by=model' + sinceParam);
      if (res.ok) {
        const data = await res.json();
        state.usageModelRows = data.rows || [];
        renderUsage();
      }
    } catch (e) {
      console.error('Error loading usage:', e);
    }
  }

  async function loadJobs() {
    try {
      const [jobsRes, appsRes] = await Promise.all([
        api('api/jobs'),
        api('api/apps')
      ]);
      if (jobsRes.ok) {
        const data = await jobsRes.json();
        state.jobs = data.jobs || [];
        state.queueDepth = data.queue?.depth_by_state || {};
      }
      if (appsRes.ok) {
        const data = await appsRes.json();
        state.apps = data.apps || [];
      }
      renderJobs();
    } catch (e) {
      console.error('Error loading jobs/apps:', e);
    }
  }

  async function loadPromos() {
    try {
      const [promosRes, scoutRes] = await Promise.all([
        api('api/promos'),
        api('api/scout/status')
      ]);
      if (promosRes.ok) state.promos = (await promosRes.json()).promos || [];
      if (scoutRes.ok) state.scoutStatus = await scoutRes.json();
      renderPromos();
    } catch (e) {
      console.error('Error loading promos:', e);
    }
  }

  async function loadEvents() {
    try {
      const res = await api('api/events?limit=100');
      if (res.ok) {
        const data = await res.json();
        state.events = data.events || [];
        renderEvents();
      }
    } catch (e) {
      console.error('Error loading events:', e);
    }
  }

  async function refreshAll() {
    const btn = document.getElementById('refresh-btn');
    if (btn) btn.classList.add('spinning');
    await Promise.all([
      loadBaselines(),
      loadStatus(),
      loadLive(),
      loadUsage(),
      loadJobs(),
      loadPromos(),
      loadEvents()
    ]);
    if (btn) setTimeout(() => btn.classList.remove('spinning'), 400);
    notify('Dashboard updated', 'success');
  }

  // --- Polling Loops ---
  function startPolling() {
    loadBaselines();
    loadStatus();
    loadLive();
    loadUsage();
    loadJobs();
    loadPromos();
    loadEvents();

    clearInterval(state.livePollTimer);
    state.livePollTimer = setInterval(() => {
      loadLive();
    }, state.inFlightTotal > 0 ? 2000 : 8000);

    clearInterval(state.statusPollTimer);
    state.statusPollTimer = setInterval(() => {
      loadStatus();
      if (state.activeTab === 'queue') loadJobs();
      if (state.activeTab === 'promos') loadPromos();
      if (state.activeTab === 'events') loadEvents();
    }, 15000);
  }

  // --- Topbar Updates ---
  function updateTopMetrics() {
    if (!state.status) return;
    const models = state.status.models || [];
    const ready = models.filter(m => m.status === 'ok').length;
    const total = models.length;

    const kpiEl = document.getElementById('top-models-summary');
    if (kpiEl) {
      kpiEl.textContent = `${ready}/${total} Ready`;
      kpiEl.style.color = ready > 0 ? 'var(--status-ready)' : 'var(--status-depleted)';
    }
  }

  function updateLiveIndicator() {
    const pill = document.getElementById('live-indicator-pill');
    const dot = document.getElementById('live-pulse-dot');
    const text = document.getElementById('live-text');
    if (!pill || !dot || !text) return;

    const count = state.inFlightTotal;
    if (count > 0) {
      dot.className = 'pulse-dot active';
      text.textContent = `${count} stream${count > 1 ? 's' : ''} active`;
      pill.style.borderColor = '#38bdf8';
    } else {
      dot.className = 'pulse-dot';
      text.textContent = 'Gateway Idle';
      pill.style.borderColor = 'var(--border-subtle)';
    }
  }

  // --- Tabs Switching ---
  function switchTab(tabId) {
    state.activeTab = tabId;
    document.querySelectorAll('.nav-item').forEach(el => {
      el.classList.toggle('active', el.dataset.tab === tabId);
    });
    document.querySelectorAll('.panel').forEach(el => {
      el.classList.toggle('active', el.dataset.panel === tabId);
    });

    if (tabId === 'usage') loadUsage();
    if (tabId === 'queue') loadJobs();
    if (tabId === 'accounts') renderAccounts();
    if (tabId === 'promos') loadPromos();
    if (tabId === 'events') loadEvents();
    document.body.classList.toggle('rec-wide', tabId === 'recorder');
    if (tabId === 'recorder') {
      loadRecorder();
      fitRecorderGrid();
    }
    if (tabId !== 'recorder') {
      scheduleRecorderPoll(false);
      toggleRecorderFocus(false);
    }
  }
  window.switchTab = switchTab;

  // --- RENDER: OVERVIEW & LIVE (Item 1) ---
  function renderOverview() {
    if (!state.status) return;
    const models = state.status.models || [];
    const ready = models.filter(m => m.status === 'ok').length;
    const cooldown = models.filter(m => m.status === 'cooldown').length;
    const exhausted = models.filter(m => m.status === 'exhausted').length;
    const disabled = models.filter(m => m.disabled).length;

    const reqsToday = models.reduce((acc, m) => acc + (m.usage_today?.requests || 0), 0);
    const tokensOutToday = models.reduce((acc, m) => acc + (m.usage_today?.out_tokens || 0), 0);
    const tokensInToday = models.reduce((acc, m) => acc + (m.usage_today?.in_tokens || 0), 0);

    const kpiContainer = document.getElementById('overview-kpis');
    if (kpiContainer) {
      kpiContainer.innerHTML = `
        <div class="kpi-card">
          <div class="kpi-title">Fleet Readiness <span>⚡</span></div>
          <div class="kpi-value" style="color: var(--status-ready)">${ready} <span style="font-size:1rem; color:var(--text-muted)">/ ${models.length}</span></div>
          <div class="kpi-sub">
            <span style="color:var(--status-cooldown)">${cooldown} cooldown</span> •
            <span style="color:var(--status-depleted)">${exhausted} exhausted</span> •
            <span>${disabled} disabled</span>
          </div>
        </div>

        <div class="kpi-card">
          <div class="kpi-title">Today's Traffic <span>📊</span></div>
          <div class="kpi-value">${formatNumber(reqsToday)} <span style="font-size:0.9rem; color:var(--text-muted)">reqs</span></div>
          <div class="kpi-sub">
            <span>${formatTokens(tokensOutToday)} out</span> • <span>${formatTokens(tokensInToday)} in</span>
          </div>
        </div>

        <div class="kpi-card">
          <div class="kpi-title">In-Flight Activity <span>🛰️</span></div>
          <div class="kpi-value" style="color: ${state.inFlightTotal > 0 ? '#38bdf8' : 'var(--text-primary)'}">
            ${state.inFlightTotal} <span style="font-size:0.9rem; color:var(--text-muted)">active streams</span>
          </div>
          <div class="kpi-sub">
            <span>Fair-share workers: 6 slots</span>
          </div>
        </div>

        <div class="kpi-card">
          <div class="kpi-title">Scout Intel <span>🎯</span></div>
          <div class="kpi-value">${state.promos ? state.promos.filter(p => p.status === 'new').length : 0} <span style="font-size:0.9rem; color:var(--text-muted)">new leads</span></div>
          <div class="kpi-sub">
            <span>Next: ${state.scoutStatus?.next_run_at ? formatCountdown(state.scoutStatus.next_run_at) : 'Daily 08:00'}</span>
          </div>
        </div>
      `;
    }

    // Top models table
    const topModels = [...models]
      .filter(m => (m.usage_today?.requests || 0) > 0)
      .sort((a, b) => (b.usage_today?.requests || 0) - (a.usage_today?.requests || 0))
      .slice(0, 6);

    const topModelsEl = document.getElementById('overview-top-models');
    if (topModelsEl) {
      if (topModels.length === 0) {
        topModelsEl.innerHTML = '<div class="text-muted" style="padding: 1.25rem 0;">No model activity recorded today yet. Try the Playground to test routing!</div>';
      } else {
        topModelsEl.innerHTML = `
          <div class="table-container">
            <table class="table">
              <thead>
                <tr>
                  <th>Model</th>
                  <th>Provider</th>
                  <th>Today Reqs</th>
                  <th>Tokens Out</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                ${topModels.map(m => `
                  <tr>
                    <td class="font-mono" style="font-weight:600">${escapeHtml(m.model || m.key)}</td>
                    <td><span class="cap-tag">${escapeHtml(m.provider)}</span></td>
                    <td style="font-weight:600">${formatNumber(m.usage_today?.requests || 0)}</td>
                    <td>${formatTokens(m.usage_today?.out_tokens || 0)}</td>
                    <td><span class="status-badge status-${m.status === 'ok' ? 'ready' : m.status === 'exhausted' ? 'exhausted' : m.status}">${escapeHtml(m.status)}</span></td>
                  </tr>
                `).join('')}
              </tbody>
            </table>
          </div>
        `;
      }
    }

    renderOverviewLive();
  }

  function renderOverviewLive() {
    const liveContainer = document.getElementById('overview-live-container');
    if (!liveContainer) return;

    if (!state.live || state.live.length === 0) {
      liveContainer.innerHTML = `
        <div class="text-muted" style="padding: 1.5rem 0; text-align:center; font-size:0.88rem">
          Gateway is currently idle. No active in-flight streams.
        </div>
      `;
      return;
    }

    liveContainer.innerHTML = state.live.map(c => `
      <div class="live-stream-row ${c.state === 'running' ? 'running' : 'waiting'}">
        <div>
          <div style="font-weight:600; display:flex; align-items:center; gap:6px">
            <span class="cap-tag">${escapeHtml(c.app || 'default')}</span>
            <span class="font-mono" style="font-size:0.82rem">${escapeHtml(c.model)}</span>
          </div>
          <div class="text-muted" style="font-size:0.75rem; margin-top:3px">
            State: <strong style="color:var(--text-primary)">${escapeHtml(c.state || 'running')}</strong> •
            Running for <strong>${Math.round(c.elapsed_s || 0)}s</strong> •
            ID: <code class="font-mono" style="font-size:0.72rem">${escapeHtml(c.call_id || '-')}</code>
          </div>
        </div>
        <div>
          <button type="button" class="btn btn-sm btn-danger" onclick="cancelCall('${escapeHtml(c.call_id)}')">Kill</button>
        </div>
      </div>
    `).join('');
  }

  // --- RENDER: MODELS FLEET (Items 3 & 5) ---
  function renderModels() {
    if (!state.status) return;
    const models = state.status.models || [];
    const filter = state.modelsFilter;

    // Filter list
    let filtered = models.filter(m => {
      if (filter.status !== 'all') {
        if (filter.status === 'ready' && m.status !== 'ok') return false;
        if (filter.status === 'cooldown' && m.status !== 'cooldown') return false;
        if (filter.status === 'exhausted' && m.status !== 'exhausted') return false;
        if (filter.status === 'disabled' && !m.disabled) return false;
      }
      if (filter.cap !== 'all') {
        if (!m.caps || !m.caps.includes(filter.cap)) return false;
      }
      if (filter.provider && m.provider !== filter.provider) return false;
      if (filter.search) {
        const q = filter.search.toLowerCase();
        const str = `${m.provider} ${m.key} ${m.model} ${m.account} ${m.status} ${m.last_error || ''}`.toLowerCase();
        if (!str.includes(q)) return false;
      }
      return true;
    });

    const countEl = document.getElementById('models-filter-count');
    if (countEl) countEl.textContent = `${filtered.length} of ${models.length}`;

    // Populate provider select
    const provSelect = document.getElementById('models-provider-select');
    if (provSelect && provSelect.options.length <= 1) {
      const providers = [...new Set(models.map(m => m.provider))].sort();
      providers.forEach(p => {
        const opt = document.createElement('option');
        opt.value = p;
        opt.textContent = p;
        provSelect.appendChild(opt);
      });
    }

    const container = document.getElementById('models-container');
    if (!container) return;

    if (state.views.models === 'cards') {
      container.innerHTML = `
        <div class="models-grid">
          ${filtered.map(m => renderModelCard(m)).join('')}
        </div>
      `;
    } else {
      // Sort table
      const sorted = applySort(filtered, 'models', (m, col) => {
        if (col === 'model') return m.model || m.key;
        if (col === 'provider') return m.provider;
        if (col === 'account') return m.account;
        if (col === 'status') return m.disabled ? 'disabled' : m.status;
        if (col === 'reqs') return m.usage_today?.requests || 0;
        if (col === 'tokens') return m.usage_today?.out_tokens || 0;
        if (col === 'latency') return m.avg_latency_ms || 0;
        return m[col];
      });

      container.innerHTML = `
        <div class="table-container">
          <table class="table">
            <thead>
              <tr>
                ${renderSortHeader('models', 'model', 'Model / Key')}
                ${renderSortHeader('models', 'provider', 'Provider')}
                ${renderSortHeader('models', 'account', 'Account')}
                <th>Capabilities</th>
                ${renderSortHeader('models', 'status', 'Status')}
                ${renderSortHeader('models', 'reqs', 'Today Reqs')}
                ${renderSortHeader('models', 'tokens', 'Tokens Out')}
                <th>Daily Limit</th>
                ${renderSortHeader('models', 'latency', 'Avg Latency')}
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              ${sorted.map(m => renderModelTableRow(m)).join('')}
            </tbody>
          </table>
        </div>
      `;
    }

    container.querySelectorAll('[data-action]').forEach(btn => {
      btn.addEventListener('click', handleModelAction);
    });
  }

  function renderModelCard(m) {
    const key = m.key;
    const statusClass = m.disabled ? 'disabled' : m.status === 'ok' ? 'ready' : m.status === 'exhausted' ? 'exhausted' : m.status;
    const caps = m.caps || ['text'];
    const failure = currentFailure(m);

    let quotaHtml = '';
    if (m.windows) {
      for (const [winType, win] of Object.entries(m.windows)) {
        if (!win) continue;
        const used = win.used || 0;
        const limit = win.limit;
        const metric = win.metric === 'out_tokens' ? 'out tok' : win.metric === 'in_tokens' ? 'in tok' : 'req';
        const pct = limit ? Math.min(100, Math.round((used / limit) * 100)) : 0;
        const barClass = pct > 90 ? 'danger' : pct > 70 ? 'warning' : '';
        quotaHtml += `
          <div class="quota-bar-wrapper">
            <div class="quota-bar-info">
              <span>${winType} (${metric})</span>
              <span>${formatNumber(used)}${limit ? ' / ' + formatNumber(limit) : ''}</span>
            </div>
            ${limit ? `
              <div class="quota-bar-track">
                <div class="quota-bar-fill ${barClass}" style="width: ${pct}%"></div>
              </div>
            ` : ''}
            ${win.resets_at ? `<div style="font-size:0.68rem; color:var(--text-muted); text-align:right">Resets in ${formatCountdown(win.resets_at)}</div>` : ''}
          </div>
        `;
      }
    }

    return `
      <div class="model-card">
        <div class="model-card-head">
          <div class="model-identity">
            <span class="model-provider">${escapeHtml(m.provider)}</span>
            <span class="model-name" title="${escapeHtml(m.model || m.key)}">${escapeHtml(m.model || m.key)}</span>
            <span class="text-muted" style="font-size:0.75rem">${escapeHtml(m.account)}</span>
          </div>
          <span class="status-badge status-${statusClass}">${m.disabled ? 'Disabled' : escapeHtml(m.status)}</span>
        </div>

        <div class="caps-row">
          ${caps.map(c => `<span class="cap-tag ${c}">${c}</span>`).join('')}
          ${m.context ? `<span class="cap-tag">${formatTokens(m.context)} ctx</span>` : ''}
          ${m.opt_in_only ? `<span class="cap-tag" style="color:var(--accent-primary)">opt-in</span>` : ''}
        </div>

        <div style="display:flex; flex-direction:column; gap:0.5rem; margin:0.25rem 0;">
          ${quotaHtml || '<div class="text-muted" style="font-size:0.75rem">Declared limits unmetered / unlimited</div>'}
        </div>

        ${failure && failure.stale ? `
          <div class="text-muted" style="font-size:0.72rem; padding:0 2px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="Refused ${escapeHtml(failure.when)} and not retried with success since; old enough that the hub no longer holds it against the model (${escapeHtml(failure.text)})">
            last refusal ${escapeHtml(failure.when)}: ${escapeHtml(failure.text)}
          </div>
        ` : failure ? `
          <div style="font-size:0.72rem; color:var(--status-depleted); background:var(--status-depleted-bg); padding:4px 8px; border-radius:4px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${escapeHtml(failure.text)} (${escapeHtml(failure.when)})">
            ⚠️ ${escapeHtml(failure.text)}${failure.when ? ` <span class="text-muted">· ${escapeHtml(failure.when)}</span>` : ''}
          </div>
        ` : ''}

        <div class="model-card-footer">
          <div>
            <span>${formatNumber(m.usage_today?.requests || 0)} reqs</span> •
            <span>${formatTokens(m.usage_today?.out_tokens || 0)} tok</span>
          </div>
          <div class="model-card-actions">
            <button type="button" class="btn btn-sm" data-action="playground" data-key="${escapeHtml(key)}" title="Test in Playground">▶ Play</button>
            <button type="button" class="btn btn-sm" data-action="forgive" data-key="${escapeHtml(key)}" title="Forgive Quota Window">Forgive</button>
            <button type="button" class="btn btn-sm" data-action="test-model" data-provider="${escapeHtml(m.provider)}" data-account="${escapeHtml(m.account)}" data-model="${escapeHtml(m.model)}" title="Test Single Call">Test</button>
            <button type="button" class="btn btn-sm" data-action="toggle-disable" data-key="${escapeHtml(key)}" data-disabled="${m.disabled ? '1' : '0'}">
              ${m.disabled ? 'Enable' : 'Disable'}
            </button>
          </div>
        </div>
      </div>
    `;
  }

  function renderModelTableRow(m) {
    const key = m.key;
    const statusClass = m.disabled ? 'disabled' : m.status === 'ok' ? 'ready' : m.status === 'exhausted' ? 'exhausted' : m.status;
    const caps = m.caps || ['text'];

    return `
      <tr>
        <td>
          <div style="font-weight:600; font-family:var(--font-mono)">${escapeHtml(m.model || m.key)}</div>
          <div class="text-muted" style="font-size:0.72rem">${escapeHtml(key)}</div>
        </td>
        <td><span class="cap-tag">${escapeHtml(m.provider)}</span></td>
        <td class="text-secondary">${escapeHtml(m.account)}</td>
        <td>
          <div class="caps-row">
            ${caps.map(c => `<span class="cap-tag ${c}">${c}</span>`).join('')}
          </div>
        </td>
        <td><span class="status-badge status-${statusClass}">${m.disabled ? 'Disabled' : escapeHtml(m.status)}</span></td>
        <td>${formatNumber(m.usage_today?.requests || 0)}</td>
        <td>${formatTokens(m.usage_today?.out_tokens || 0)}</td>
        <td>
          <div style="font-size:0.75rem">
            ${m.windows?.daily ? `${formatNumber(m.windows.daily.used)} / ${m.windows.daily.limit || '∞'}` : '-'}
          </div>
        </td>
        <td>${m.avg_latency_ms ? Math.round(m.avg_latency_ms) + ' ms' : '-'}</td>
        <td>
          <div style="display:flex; gap:4px">
            <button type="button" class="btn btn-sm" data-action="playground" data-key="${escapeHtml(key)}">Play</button>
            <button type="button" class="btn btn-sm" data-action="forgive" data-key="${escapeHtml(key)}">Forgive</button>
            <button type="button" class="btn btn-sm" data-action="toggle-disable" data-key="${escapeHtml(key)}" data-disabled="${m.disabled ? '1' : '0'}">
              ${m.disabled ? 'Enable' : 'Disable'}
            </button>
          </div>
        </td>
      </tr>
    `;
  }

  async function handleModelAction(e) {
    const action = e.currentTarget.dataset.action;
    const key = e.currentTarget.dataset.key;

    if (action === 'playground') {
      switchTab('playground');
      const modelSelect = document.getElementById('playground-model');
      if (modelSelect) {
        modelSelect.value = key;
      }
      return;
    }

    if (action === 'forgive') {
      const res = await api(`api/models/${encodeURIComponent(key)}/forgive`, { method: 'POST' });
      if (res.ok) {
        notify(`Quota markers forgiven for ${key}`, 'success');
        loadStatus();
      } else {
        notify(`Failed to forgive: HTTP ${res.status}`, 'error');
      }
    }

    if (action === 'test-model') {
      const provider = e.currentTarget.dataset.provider;
      const account = e.currentTarget.dataset.account;
      const model = e.currentTarget.dataset.model;
      notify(`Testing connection to ${provider}/${model}...`, 'info');
      const res = await api(`api/accounts/${encodeURIComponent(provider)}/${encodeURIComponent(account)}/test`, {
        method: 'POST',
        body: { model }
      });
      if (res.ok) {
        const json = await res.json();
        notify(`Test successful! Latency: ${json.latency_ms} ms (${json.status})`, 'success');
      } else {
        const err = await res.json().catch(() => ({}));
        notify(`Test failed: ${err.detail || res.statusText}`, 'error');
      }
    }

    if (action === 'toggle-disable') {
      const isDisabled = e.currentTarget.dataset.disabled === '1';
      const endpoint = isDisabled ? 'enable' : 'disable';
      const res = await api(`api/models/${encodeURIComponent(key)}/${endpoint}`, { method: 'POST' });
      if (res.ok) {
        notify(`Model ${isDisabled ? 'enabled' : 'disabled'}`, 'success');
        loadStatus();
      } else {
        notify('Action failed', 'error');
      }
    }
  }

  // --- Helper: Normalized Aliases Map ---
  function getAliasesMap() {
    if (!state.status || !state.status.aliases) return {};
    if (Array.isArray(state.status.aliases)) {
      const map = {};
      state.status.aliases.forEach(a => {
        if (a && a.alias) map[a.alias] = a;
      });
      return map;
    }
    return state.status.aliases;
  }

  // --- RENDER: ROUTING & MATRIX & ALIAS EDITING (Item 4) ---
  function renderRouting() {
    if (!state.status) return;
    const aliases = getAliasesMap();
    const models = state.status.models || [];

    const container = document.getElementById('routing-aliases-grid');
    if (!container) return;

    container.innerHTML = Object.entries(aliases).map(([aliasName, cfg]) => {
      const preferList = cfg.prefer || [];
      return `
        <div class="alias-card">
          <div class="alias-card-head">
            <div>
              <span class="alias-badge">${escapeHtml(aliasName)}</span>
              <span class="text-muted" style="font-size:0.75rem; margin-left:0.5rem">
                Spread: LRU across top ${cfg.spread || 1}
              </span>
            </div>
            <button type="button" class="btn btn-sm" onclick="openEditAliasModal('${escapeHtml(aliasName)}')">
              ✏️ Edit Profile
            </button>
          </div>

          <div style="font-size:0.8rem; color:var(--text-secondary)">
            ${aliasName === 'auto' ? 'Default text alias. Rotates across free candidates in preference order.' :
              aliasName === 'fast' ? 'Tuned for low-latency quick responses across responsive models.' :
              aliasName === 'strong' ? 'High quality / reasoning capable models with deep reasoning.' :
              aliasName === 'vision' ? 'Enforces multimodal vision capability requirement.' :
              aliasName === 'extract' ? 'Context >= 24k tokens floor, strict JSON extraction.' :
              aliasName === 'local' ? 'Prefers local Ollama instances for private on-prem execution.' :
              'Configured routing alias'}
          </div>

          <div style="font-size:0.75rem; font-weight:600; color:var(--text-muted); margin-top:0.25rem">
            PREFERENCE CHAIN (${preferList.length} candidate${preferList.length !== 1 ? 's' : ''}):
          </div>
          <div class="candidates-list">
            ${preferList.slice(0, 6).map((pref, idx) => {
              const matched = models.find(m => m.key === pref);
              const status = matched ? matched.status : 'not registered';
              const dotColor = status === 'ok' ? 'var(--status-ready)' : status === 'cooldown' ? 'var(--status-cooldown)' : 'var(--status-depleted)';
              return `
                <div class="candidate-item">
                  <span><strong style="color:var(--text-muted)">#${idx + 1}</strong> ${escapeHtml(pref)}</span>
                  <span style="display:flex; align-items:center; gap:6px;">
                    <span style="width:6px; height:6px; border-radius:50%; background:${dotColor}"></span>
                    <span style="font-size:0.72rem; color:var(--text-muted)">${status}</span>
                  </span>
                </div>
              `;
            }).join('')}
            ${preferList.length > 6 ? `<div class="text-muted" style="font-size:0.72rem; text-align:center">+ ${preferList.length - 6} more candidates in registry</div>` : ''}
          </div>
        </div>
      `;
    }).join('');

    // Simulator alias select
    const simAlias = document.getElementById('sim-alias');
    if (simAlias && simAlias.options.length === 0) {
      Object.keys(aliases).forEach(a => {
        const opt = document.createElement('option');
        opt.value = a;
        opt.textContent = a;
        simAlias.appendChild(opt);
      });
    }
  }

  // --- Alias Editor Modal Logic (Item 4) ---
  window.openEditAliasModal = function (aliasName) {
    if (!state.status) return;
    const cfg = getAliasesMap()[aliasName] || {};
    state.editingAlias = {
      name: aliasName,
      spread: cfg.spread || 4,
      prefer: [...(cfg.prefer || [])]
    };

    const titleEl = document.getElementById('mea-title');
    if (titleEl) titleEl.textContent = `Edit Routing Profile: ${aliasName}`;

    const spreadEl = document.getElementById('mea-spread');
    if (spreadEl) spreadEl.value = state.editingAlias.spread;

    renderEditAliasPreferList();

    const modal = document.getElementById('modal-edit-alias');
    if (modal) modal.classList.add('open');
  };

  function renderEditAliasPreferList() {
    const listEl = document.getElementById('mea-prefer-list');
    if (!listEl || !state.editingAlias) return;

    if (state.editingAlias.prefer.length === 0) {
      listEl.innerHTML = '<div class="text-muted" style="padding:1rem; text-align:center">No models in preference list. Add one below.</div>';
    } else {
      listEl.innerHTML = state.editingAlias.prefer.map((key, idx) => `
        <div class="prefer-item-row">
          <span>
            <strong style="color:var(--text-muted); margin-right:6px">#${idx + 1}</strong>
            <span class="font-mono">${escapeHtml(key)}</span>
          </span>
          <div style="display:flex; gap:4px">
            <button type="button" class="btn btn-sm" onclick="moveAliasPrefer(${idx}, -1)" ${idx === 0 ? 'disabled' : ''} title="Move Up">▲</button>
            <button type="button" class="btn btn-sm" onclick="moveAliasPrefer(${idx}, 1)" ${idx === state.editingAlias.prefer.length - 1 ? 'disabled' : ''} title="Move Down">▼</button>
            <button type="button" class="btn btn-sm btn-danger" onclick="removeAliasPrefer(${idx})" title="Remove">✕</button>
          </div>
        </div>
      `).join('');
    }

    // Populate add-model select with models from registry not yet in prefer list
    const selectEl = document.getElementById('mea-add-model');
    if (selectEl && state.status) {
      selectEl.innerHTML = '';
      const models = state.status.models || [];
      const unused = models.filter(m => !state.editingAlias.prefer.includes(m.key));
      unused.forEach(m => {
        const opt = document.createElement('option');
        opt.value = m.key;
        opt.textContent = `${m.key} (${m.status})`;
        selectEl.appendChild(opt);
      });
      const addBtn = document.getElementById('mea-add-btn');
      if (addBtn) addBtn.disabled = unused.length === 0;
    }
  }

  window.moveAliasPrefer = function (idx, direction) {
    if (!state.editingAlias) return;
    const targetIdx = idx + direction;
    if (targetIdx < 0 || targetIdx >= state.editingAlias.prefer.length) return;
    const temp = state.editingAlias.prefer[idx];
    state.editingAlias.prefer[idx] = state.editingAlias.prefer[targetIdx];
    state.editingAlias.prefer[targetIdx] = temp;
    renderEditAliasPreferList();
  };

  window.removeAliasPrefer = function (idx) {
    if (!state.editingAlias) return;
    state.editingAlias.prefer.splice(idx, 1);
    renderEditAliasPreferList();
  };

  async function saveEditedAlias() {
    if (!state.editingAlias) return;
    const spreadInput = document.getElementById('mea-spread');
    const spreadVal = parseInt(spreadInput?.value || '4', 10);
    if (isNaN(spreadVal) || spreadVal < 1) {
      alert('Spread must be a positive integer (min 1).');
      return;
    }

    if (state.editingAlias.prefer.length === 0) {
      alert('Preference list cannot be empty. Add at least one model.');
      return;
    }

    const saveBtn = document.getElementById('mea-save-btn');
    if (saveBtn) saveBtn.disabled = true;

    try {
      const res = await api(`api/aliases/${encodeURIComponent(state.editingAlias.name)}`, {
        method: 'PUT',
        body: {
          prefer: state.editingAlias.prefer,
          spread: spreadVal
        }
      });

      if (res.ok) {
        notify(`Profile '${state.editingAlias.name}' updated and registry reloaded!`, 'success');
        document.getElementById('modal-edit-alias')?.classList.remove('open');
        await loadStatus();
      } else {
        const err = await res.json().catch(() => ({}));
        alert(`Failed to save alias: ${err.detail || res.statusText}`);
      }
    } catch (e) {
      alert(`Network error saving alias: ${e.message}`);
    } finally {
      if (saveBtn) saveBtn.disabled = false;
    }
  }

  function runRoutingSimulator() {
    if (!state.status) return;
    const aliasName = document.getElementById('sim-alias')?.value || 'auto';
    const estTokens = parseInt(document.getElementById('sim-tokens')?.value || '500', 10);
    const reqVision = document.getElementById('sim-req-vision')?.checked;
    const reqJson = document.getElementById('sim-req-json')?.checked;
    const reqReasoning = document.getElementById('sim-req-reasoning')?.checked;

    const models = state.status.models || [];
    const aliasCfg = getAliasesMap()[aliasName] || {};
    const preferList = aliasCfg.prefer || [];

    const candidates = [];

    for (const m of models) {
      if (m.disabled) {
        candidates.push({ model: m, eligible: false, reason: 'disabled' });
        continue;
      }
      if (m.opt_in_only && !preferList.includes(m.key)) {
        candidates.push({ model: m, eligible: false, reason: 'opt_in_only' });
        continue;
      }
      const caps = m.caps || ['text'];
      if (reqVision && !caps.includes('vision')) {
        candidates.push({ model: m, eligible: false, reason: 'missing vision cap' });
        continue;
      }
      if (reqJson && !caps.includes('json')) {
        candidates.push({ model: m, eligible: false, reason: 'missing json cap' });
        continue;
      }
      if (reqReasoning && !caps.includes('reasoning')) {
        candidates.push({ model: m, eligible: false, reason: 'missing reasoning cap' });
        continue;
      }
      if (m.context && estTokens > m.context) {
        candidates.push({ model: m, eligible: false, reason: `too large (${estTokens} > ${m.context} ctx)` });
        continue;
      }
      if (m.status === 'exhausted') {
        candidates.push({ model: m, eligible: false, reason: 'quota depleted (exhausted)' });
        continue;
      }
      if (m.status === 'cooldown') {
        candidates.push({ model: m, eligible: false, reason: 'in cooldown' });
        continue;
      }

      const prefIdx = preferList.indexOf(m.key);
      const rank = prefIdx >= 0 ? prefIdx : 999;
      candidates.push({ model: m, eligible: true, rank });
    }

    const eligible = candidates.filter(c => c.eligible).sort((a, b) => a.rank - b.rank);
    const resultBox = document.getElementById('sim-result');
    if (!resultBox) return;

    if (eligible.length === 0) {
      resultBox.innerHTML = `
        <div style="color:var(--status-depleted); font-weight:600">❌ No eligible candidates would match this request!</div>
        <div class="text-muted" style="font-size:0.8rem; margin-top:4px">Gateway would answer with HTTP 429 or 413.</div>
      `;
    } else {
      const winner = eligible[0];
      const spreadPool = eligible.slice(0, aliasCfg.spread || 4);
      resultBox.innerHTML = `
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:0.75rem">
          <div>
            <div style="font-size:0.75rem; color:var(--text-muted); text-transform:uppercase">Winning Candidate:</div>
            <div style="font-size:1.15rem; font-weight:700; color:var(--status-ready); font-family:var(--font-mono)">
              ${escapeHtml(winner.model.key)}
            </div>
          </div>
          <span class="status-badge status-ready">Rank #1 Eligible</span>
        </div>
        <div style="font-size:0.82rem; color:var(--text-secondary)">
          Selected via alias <strong>${aliasName}</strong>. Top ${spreadPool.length} candidates eligible for LRU spread rotation:
        </div>
        <div style="margin-top:0.5rem; display:flex; flex-direction:column; gap:4px">
          ${spreadPool.map((c, i) => `
            <div style="font-family:var(--font-mono); font-size:0.78rem; background:var(--bg-input); padding:4px 8px; border-radius:4px; display:flex; justify-content:space-between">
              <span>#${i + 1} ${escapeHtml(c.model.key)}</span>
              <span class="text-muted">${escapeHtml(c.model.account)}</span>
            </div>
          `).join('')}
        </div>
      `;
    }
  }

  // --- RENDER: PLAYGROUND (SSE Streamer + Telemetry) ---
  function populatePlaygroundModels() {
    const select = document.getElementById('playground-model');
    if (!select || !state.status) return;
    const currentVal = select.value;
    select.innerHTML = '';

    const aliasGroup = document.createElement('optgroup');
    aliasGroup.label = 'Aliases (Auto-Routed)';
    Object.keys(getAliasesMap()).forEach(a => {
      const opt = document.createElement('option');
      opt.value = a;
      opt.textContent = `${a} (alias)`;
      aliasGroup.appendChild(opt);
    });
    select.appendChild(aliasGroup);

    const modelGroup = document.createElement('optgroup');
    modelGroup.label = 'Registered Models (Pinned)';
    const models = state.status.models || [];
    models.forEach(m => {
      const opt = document.createElement('option');
      opt.value = m.key;
      opt.textContent = `${m.key} [${m.status}]`;
      modelGroup.appendChild(opt);
    });
    select.appendChild(modelGroup);

    if (currentVal) select.value = currentVal;
  }

  async function sendPlaygroundPrompt() {
    if (state.playground.inFlight) {
      if (state.playground.abortController) {
        state.playground.abortController.abort();
      }
      return;
    }

    const promptInput = document.getElementById('playground-input');
    const text = promptInput?.value?.trim();
    if (!text) return;

    const modelTarget = document.getElementById('playground-model')?.value || 'auto';
    const appHeader = document.getElementById('playground-app')?.value || 'studio-playground';
    const temperature = parseFloat(document.getElementById('playground-temp')?.value || '0.7');
    const maxTokens = parseInt(document.getElementById('playground-max-tokens')?.value || '1024', 10);
    const systemPrompt = document.getElementById('playground-system')?.value?.trim();
    const isStream = document.getElementById('playground-stream-toggle')?.checked ?? true;

    const reqHeaders = {
      'Content-Type': 'application/json',
      'X-Hub-App': appHeader
    };
    if (state.token) reqHeaders['Authorization'] = 'Bearer ' + state.token;

    const chatContainer = document.getElementById('playground-chat');
    if (!chatContainer) return;

    const userMsg = document.createElement('div');
    userMsg.className = 'message-bubble message-user';
    userMsg.textContent = text;
    chatContainer.appendChild(userMsg);
    promptInput.value = '';

    const assistantMsg = document.createElement('div');
    assistantMsg.className = 'message-bubble message-assistant streaming-cursor';
    assistantMsg.innerHTML = `
      <details open class="reasoning-box" style="display:none;">
        <summary>💭 Thinking Process</summary>
        <div class="reasoning-text"></div>
      </details>
      <div class="stream-content"></div>
    `;
    chatContainer.appendChild(assistantMsg);
    chatContainer.scrollTop = chatContainer.scrollHeight;

    const reasoningBox = assistantMsg.querySelector('.reasoning-box');
    const reasoningText = assistantMsg.querySelector('.reasoning-text');
    const streamContent = assistantMsg.querySelector('.stream-content');

    const sendBtn = document.getElementById('playground-send-btn');
    if (sendBtn) {
      sendBtn.textContent = '⏹ Stop';
      sendBtn.className = 'btn btn-danger';
    }

    state.playground.inFlight = true;
    state.playground.abortController = new AbortController();

    const messages = [];
    if (systemPrompt) messages.push({ role: 'system', content: systemPrompt });
    messages.push({ role: 'user', content: text });

    const startTime = performance.now();
    let firstTokenTime = null;
    let accumulatedContent = '';
    let accumulatedReasoning = '';
    let resolvedModel = modelTarget;

    try {
      const res = await fetch(apiUrl('v1/chat/completions'), {
        method: 'POST',
        headers: reqHeaders,
        body: JSON.stringify({
          model: modelTarget,
          messages,
          temperature,
          max_tokens: maxTokens,
          stream: isStream
        }),
        signal: state.playground.abortController.signal
      });

      if (!res.ok) {
        const errJson = await res.json().catch(() => ({}));
        throw new Error(errJson?.error?.message || errJson?.detail || `HTTP ${res.status}: ${res.statusText}`);
      }

      if (!isStream) {
        const json = await res.json();
        const duration = Math.round(performance.now() - startTime);
        const msg = json.choices?.[0]?.message || {};
        accumulatedContent = msg.content || '';
        accumulatedReasoning = msg.reasoning || msg.reasoning_content || '';
        resolvedModel = json.model || modelTarget;

        if (accumulatedReasoning) {
          reasoningBox.style.display = 'block';
          reasoningText.textContent = accumulatedReasoning;
        }
        streamContent.textContent = accumulatedContent;

        updateTelemetry({
          model: resolvedModel,
          duration,
          ttft: duration,
          promptTokens: json.usage?.prompt_tokens || '?',
          completionTokens: json.usage?.completion_tokens || '?'
        });
      } else {
        const reader = res.body.getReader();
        const decoder = new TextDecoder('utf-8');
        let buffer = '';

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split('\n');
          buffer = lines.pop() || '';

          for (const line of lines) {
            const trimmed = line.trim();
            if (!trimmed || !trimmed.startsWith('data:')) continue;
            const dataStr = trimmed.slice(5).trim();
            if (dataStr === '[DONE]') break;

            try {
              const chunk = JSON.parse(dataStr);
              if (chunk.model) resolvedModel = chunk.model;
              const delta = chunk.choices?.[0]?.delta;
              if (delta) {
                if (!firstTokenTime) firstTokenTime = performance.now();

                const r = delta.reasoning || delta.reasoning_content;
                if (r) {
                  accumulatedReasoning += r;
                  reasoningBox.style.display = 'block';
                  reasoningText.textContent = accumulatedReasoning;
                  chatContainer.scrollTop = chatContainer.scrollHeight;
                }

                if (delta.content) {
                  accumulatedContent += delta.content;
                  streamContent.textContent = accumulatedContent;
                  chatContainer.scrollTop = chatContainer.scrollHeight;
                }
              }
            } catch (e) {
              // Ignore line parse error
            }
          }
        }

        const totalDuration = Math.round(performance.now() - startTime);
        const ttft = firstTokenTime ? Math.round(firstTokenTime - startTime) : totalDuration;
        updateTelemetry({
          model: resolvedModel,
          duration: totalDuration,
          ttft,
          promptTokens: 'est. ' + Math.round(text.length / 4),
          completionTokens: 'est. ' + Math.round((accumulatedContent.length + accumulatedReasoning.length) / 4)
        });
      }
    } catch (err) {
      if (err.name === 'AbortError') {
        streamContent.textContent += '\n[Request cancelled by user]';
      } else {
        streamContent.innerHTML = `<span style="color:var(--status-depleted); font-weight:600">Error: ${escapeHtml(err.message)}</span>`;
      }
    } finally {
      assistantMsg.classList.remove('streaming-cursor');
      state.playground.inFlight = false;
      state.playground.abortController = null;
      if (sendBtn) {
        sendBtn.textContent = 'Send Prompt';
        sendBtn.className = 'btn btn-primary';
      }
      loadStatus();
      loadLive();
    }
  }

  function updateTelemetry(data) {
    const card = document.getElementById('playground-telemetry');
    if (!card) return;
    card.style.display = 'flex';
    document.getElementById('tel-model').textContent = data.model;
    document.getElementById('tel-latency').textContent = `${data.duration} ms`;
    document.getElementById('tel-ttft').textContent = `${data.ttft} ms`;
    document.getElementById('tel-tokens').textContent = `${data.promptTokens} in / ${data.completionTokens} out`;
  }

  // --- RENDER: ACCOUNTS TAB (Items 3, 5 & 6) ---
  function renderAccounts() {
    const container = document.getElementById('accounts-list-content');
    if (!container || !state.status) return;

    const accounts = state.status.accounts || [];
    if (accounts.length === 0) {
      container.innerHTML = '<div class="text-muted" style="padding:2rem; text-align:center">No provider accounts configured in registry (~/.llmhub/providers.yaml).</div>';
      return;
    }

    if (state.views.accounts === 'cards') {
      container.innerHTML = `
        <div class="accounts-grid">
          ${accounts.map(acc => {
            const keyStatus = acc.key_present
              ? '<span class="status-badge status-ready">🔑 Key Set</span>'
              : acc.kind === 'cli'
              ? '<span class="status-badge" style="background:rgba(192,132,252,0.15); color:var(--cap-vision)">CLI Auth</span>'
              : '<span class="status-badge status-depleted">⚠️ No Key</span>';

            const lastProbe = acc.last_checked_at
              ? `${formatTimeAgo(acc.last_checked_at)} (${acc.last_status || 'unknown'})`
              : 'Never probed';

            return `
              <div class="account-card">
                <div class="account-card-head">
                  <div>
                    <span class="model-provider">${escapeHtml(acc.provider)}</span>
                    <div style="font-weight:700; font-size:1.05rem">${escapeHtml(acc.account)}</div>
                    <div class="text-muted" style="font-size:0.75rem">${escapeHtml(acc.base_url || acc.command || 'custom')}</div>
                  </div>
                  ${keyStatus}
                </div>

                <div>
                  <div style="font-size:0.75rem; font-weight:600; color:var(--text-muted); margin-bottom:4px">
                    REGISTERED MODELS (${(acc.models || []).length}):
                  </div>
                  <div class="account-models-list">
                    ${(acc.models || []).map(m => `<span class="cap-tag">${escapeHtml(m)}</span>`).join('')}
                  </div>
                </div>

                <div style="font-size:0.75rem; color:var(--text-secondary); background:var(--bg-input); padding:4px 8px; border-radius:4px">
                  <span>🛡️ Last Health Probe: <strong>${escapeHtml(lastProbe)}</strong></span>
                  ${acc.last_error ? `<div style="color:var(--status-depleted); margin-top:2px">⚠️ ${escapeHtml(acc.last_error)}</div>` : ''}
                </div>

                <div style="display:flex; justify-content:space-between; align-items:center; margin-top:0.5rem; padding-top:0.5rem; border-top:1px solid var(--border-subtle)">
                  <span class="text-muted" style="font-size:0.75rem">Env: <code>${escapeHtml(acc.api_key_env || 'none')}</code></span>
                  <div style="display:flex; gap:6px">
                    <button type="button" class="btn btn-sm" onclick="testAccount('${escapeHtml(acc.provider)}', '${escapeHtml(acc.account)}')">⚡ Test</button>
                    <button type="button" class="btn btn-sm" onclick="discoverAccount('${escapeHtml(acc.provider)}', '${escapeHtml(acc.account)}')">🔍 Discover</button>
                  </div>
                </div>
              </div>
            `;
          }).join('')}
        </div>
      `;
    } else {
      const sorted = applySort(accounts, 'accounts', (acc, col) => {
        if (col === 'provider') return acc.provider;
        if (col === 'account') return acc.account;
        if (col === 'kind') return acc.kind;
        if (col === 'key') return acc.key_present ? 1 : 0;
        if (col === 'models') return (acc.models || []).length;
        if (col === 'last_checked_at') return acc.last_checked_at || '';
        if (col === 'status') return acc.last_status || '';
        return acc[col];
      });

      container.innerHTML = `
        <div class="table-container">
          <table class="table">
            <thead>
              <tr>
                ${renderSortHeader('accounts', 'provider', 'Provider')}
                ${renderSortHeader('accounts', 'account', 'Account ID')}
                ${renderSortHeader('accounts', 'kind', 'Kind')}
                ${renderSortHeader('accounts', 'key', 'Key Status')}
                ${renderSortHeader('accounts', 'models', 'Models')}
                <th>Base URL / Command</th>
                ${renderSortHeader('accounts', 'last_checked_at', 'Last Health Probe')}
                ${renderSortHeader('accounts', 'status', 'Health Status')}
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              ${sorted.map(acc => {
                const keyStatus = acc.key_present
                  ? '<span class="status-badge status-ready">🔑 Set</span>'
                  : acc.kind === 'cli'
                  ? '<span class="status-badge" style="background:rgba(192,132,252,0.15); color:var(--cap-vision)">CLI</span>'
                  : '<span class="status-badge status-depleted">⚠️ No Key</span>';

                const probeStatus = acc.last_status === 'ok'
                  ? '<span class="status-badge status-ready">OK</span>'
                  : acc.last_status === 'failed'
                  ? '<span class="status-badge status-depleted">Failed</span>'
                  : '<span class="status-badge status-disabled">Unchecked</span>';

                return `
                  <tr>
                    <td><span class="cap-tag">${escapeHtml(acc.provider)}</span></td>
                    <td style="font-weight:600">${escapeHtml(acc.account)}</td>
                    <td class="text-secondary">${escapeHtml(acc.kind || 'http')}</td>
                    <td>${keyStatus}</td>
                    <td style="font-weight:600">${(acc.models || []).length}</td>
                    <td class="font-mono text-muted" style="font-size:0.75rem">${escapeHtml(acc.base_url || acc.command || '-')}</td>
                    <td class="text-muted" style="font-size:0.78rem">${escapeHtml(formatTimeAgo(acc.last_checked_at))}</td>
                    <td>${probeStatus}</td>
                    <td>
                      <div style="display:flex; gap:4px">
                        <button type="button" class="btn btn-sm" onclick="testAccount('${escapeHtml(acc.provider)}', '${escapeHtml(acc.account)}')">⚡ Test</button>
                        <button type="button" class="btn btn-sm" onclick="discoverAccount('${escapeHtml(acc.provider)}', '${escapeHtml(acc.account)}')">🔍 Discover</button>
                      </div>
                    </td>
                  </tr>
                `;
              }).join('')}
            </tbody>
          </table>
        </div>
      `;
    }
  }

  // --- Availability Health Sweep (Item 6) ---
  window.runAvailabilitySweep = async function () {
    const btn1 = document.getElementById('btn-health-sweep');
    const btn2 = document.getElementById('btn-run-sweep-accounts');
    if (btn1) { btn1.disabled = true; btn1.textContent = '⏳ Checking...'; }
    if (btn2) { btn2.disabled = true; btn2.textContent = '⏳ Checking...'; }

    notify('Starting paced availability sweep (1 probe/active account)...', 'info');

    try {
      const res = await api('api/health/sweep', { method: 'POST' });
      if (res.ok) {
        const data = await res.json();
        const probed = data.probed_count || 0;
        const skipped = data.skipped_count || 0;
        const results = data.results || [];
        const passed = results.filter(r => r.ok).length;
        const failed = results.filter(r => !r.ok).length;

        notify(`Sweep completed: ${probed} checked (${passed} ok, ${failed} failed). Skipped ${skipped} inactive/exhausted.`, 'success');

        const summaryText = document.getElementById('sweep-summary-text');
        if (summaryText) {
          summaryText.innerHTML = `Last sweep completed just now: <strong>${passed}/${probed} accounts verified</strong> (skipped ${skipped}).`;
        }
        await loadStatus();
      } else {
        notify('Health sweep failed: HTTP ' + res.status, 'error');
      }
    } catch (e) {
      notify('Health sweep request error: ' + e.message, 'error');
    } finally {
      if (btn1) { btn1.disabled = false; btn1.textContent = '⚡ Check Availability'; }
      if (btn2) { btn2.disabled = false; btn2.textContent = '⚡ Run Availability Sweep'; }
    }
  };

  window.testAccount = async function (provider, account) {
    notify(`Testing connection to ${provider}/${account}...`, 'info');
    const res = await api(`api/accounts/${encodeURIComponent(provider)}/${encodeURIComponent(account)}/test`, {
      method: 'POST'
    });
    if (res.ok) {
      const json = await res.json();
      notify(`Connection OK! Model: ${json.model}, latency: ${json.latency_ms} ms`, 'success');
      loadStatus();
    } else {
      const err = await res.json().catch(() => ({}));
      notify(`Test failed: ${err.detail || res.statusText}`, 'error');
    }
  };

  window.discoverAccount = async function (provider, account) {
    notify(`Discovering models for ${provider}...`, 'info');
    const res = await api(`api/providers/${encodeURIComponent(provider)}/discover`, {
      method: 'POST',
      body: { account_id: account }
    });
    if (res.ok) {
      const json = await res.json();
      const models = json.models || [];
      notify(`Discovered ${models.length} models for ${provider}!`, 'success');
      loadStatus();
    } else {
      const err = await res.json().catch(() => ({}));
      notify(`Discovery failed: ${err.detail || res.statusText}`, 'error');
    }
  };

  // --- RENDER: QUEUE & JOBS (Items 3 & 5) ---
  function renderJobs() {
    const jobs = state.jobs || [];
    const depth = state.queueDepth || {};
    const apps = state.apps || [];

    const summaryEl = document.getElementById('queue-summary-bar');
    if (summaryEl) {
      summaryEl.innerHTML = `
        <div class="kpi-card" style="padding:0.75rem 1rem">
          <div class="kpi-title">Workers Total</div>
          <div class="kpi-value" style="font-size:1.4rem">6 slots</div>
        </div>
        <div class="kpi-card" style="padding:0.75rem 1rem">
          <div class="kpi-title">Waiting Quota</div>
          <div class="kpi-value" style="font-size:1.4rem; color:var(--status-cooldown)">${depth.waiting_quota || 0}</div>
        </div>
        <div class="kpi-card" style="padding:0.75rem 1rem">
          <div class="kpi-title">Running</div>
          <div class="kpi-value" style="font-size:1.4rem; color:#38bdf8">${depth.running || 0}</div>
        </div>
        <div class="kpi-card" style="padding:0.75rem 1rem">
          <div class="kpi-title">Queued</div>
          <div class="kpi-value" style="font-size:1.4rem">${depth.queued || 0}</div>
        </div>
      `;
    }

    const listEl = document.getElementById('queue-jobs-list');
    if (!listEl) return;

    const appsHtml = apps.length > 0 ? `
      <div style="margin-bottom:1.25rem">
        <h3 style="font-size:0.9rem; font-weight:600; margin-bottom:0.6rem; color:var(--text-secondary)">
          Connected Applications & Fair-Share Queue Control:
        </h3>
        <div class="app-shares-row">
          ${apps.map(a => `
            <div class="app-share-card">
              <div>
                <strong style="color:var(--accent-primary)">${escapeHtml(a.app)}</strong>
                <div class="text-muted" style="font-size:0.72rem">
                  Queued: ${a.queued || 0} • Running: ${a.running || 0} • Bans: ${a.bans || 0}
                </div>
              </div>
              <button type="button" class="btn btn-sm ${a.paused ? 'btn-primary' : ''}" onclick="toggleAppPause('${escapeHtml(a.app)}', ${a.paused ? 'true' : 'false'})">
                ${a.paused ? 'Resume App' : 'Pause App'}
              </button>
            </div>
          `).join('')}
        </div>
      </div>
    ` : '';

    let jobsContentHtml = '';

    if (jobs.length === 0) {
      jobsContentHtml = '<div class="text-muted" style="padding: 2rem; text-align:center">No active or pending jobs in queue.</div>';
    } else if (state.views.queue === 'cards') {
      jobsContentHtml = `
        <div class="jobs-grid">
          ${jobs.map(j => `
            <div class="model-card">
              <div class="model-card-head">
                <div>
                  <span class="cap-tag">${escapeHtml(j.app || 'app')}</span>
                  <div class="font-mono" style="font-weight:600; margin-top:2px">${escapeHtml(j.id)}</div>
                </div>
                <span class="status-badge status-${j.state === 'running' ? 'ready' : j.state === 'waiting_quota' ? 'cooldown' : 'disabled'}">
                  ${escapeHtml(j.state)}
                </span>
              </div>
              <div style="font-size:0.82rem; color:var(--text-secondary)">
                Target: <strong class="font-mono">${escapeHtml(j.model || j.alias || 'auto')}</strong>
              </div>
              <div class="model-card-footer">
                <span class="text-muted" style="font-size:0.75rem">${formatTimeAgo(j.created_at)}</span>
                <button type="button" class="btn btn-sm btn-danger" onclick="cancelJob('${escapeHtml(j.id)}')">Cancel</button>
              </div>
            </div>
          `).join('')}
        </div>
      `;
    } else {
      const sorted = applySort(jobs, 'queue', (j, col) => {
        if (col === 'id') return j.id;
        if (col === 'app') return j.app;
        if (col === 'model') return j.model || j.alias || '';
        if (col === 'state') return j.state;
        if (col === 'created_at') return j.created_at || '';
        return j[col];
      });

      jobsContentHtml = `
        <div class="table-container">
          <table class="table">
            <thead>
              <tr>
                ${renderSortHeader('queue', 'id', 'Job ID')}
                ${renderSortHeader('queue', 'app', 'App')}
                ${renderSortHeader('queue', 'model', 'Target Model')}
                ${renderSortHeader('queue', 'state', 'State')}
                ${renderSortHeader('queue', 'created_at', 'Created')}
                <th>Action</th>
              </tr>
            </thead>
            <tbody>
              ${sorted.map(j => `
                <tr>
                  <td class="font-mono">${escapeHtml(j.id)}</td>
                  <td><span class="cap-tag">${escapeHtml(j.app)}</span></td>
                  <td class="font-mono">${escapeHtml(j.model || j.alias || 'auto')}</td>
                  <td><span class="status-badge status-${j.state === 'running' ? 'ready' : j.state === 'waiting_quota' ? 'cooldown' : 'disabled'}">${escapeHtml(j.state)}</span></td>
                  <td class="text-muted">${formatTimeAgo(j.created_at)}</td>
                  <td>
                    <button type="button" class="btn btn-sm btn-danger" onclick="cancelJob('${escapeHtml(j.id)}')">Cancel</button>
                  </td>
                </tr>
              `).join('')}
            </tbody>
          </table>
        </div>
      `;
    }

    listEl.innerHTML = appsHtml + jobsContentHtml;
  }

  window.toggleAppPause = async function (appName, isPaused) {
    const endpoint = isPaused ? 'resume' : 'pause';
    const res = await api(`api/apps/${encodeURIComponent(appName)}/${endpoint}`, { method: 'POST' });
    if (res.ok) {
      notify(`Application ${appName} ${isPaused ? 'resumed' : 'paused'}`, 'success');
      loadJobs();
    } else {
      notify('Failed to update app status', 'error');
    }
  };

  // --- RENDER: PROMOS & SCOUT (Items 3, 5 & 7) ---
  function renderPromos() {
    const promos = state.promos || [];
    const scout = state.scoutStatus || {};

    const scoutBox = document.getElementById('scout-status-card');
    if (scoutBox) {
      scoutBox.innerHTML = `
        <div style="display:flex; justify-content:space-between; align-items:center;">
          <div>
            <div style="font-size:0.75rem; color:var(--text-muted); text-transform:uppercase">Scout Daemon Status</div>
            <div style="font-size:1.1rem; font-weight:700">${scout.last_run ? `Last run: ${scout.last_run.status} (${scout.last_run.promos_found || 0} offers found)` : 'Scheduled Daily (08:00)'}</div>
            <div class="text-secondary" style="font-size:0.8rem">Next scan: ${scout.next_run_at ? formatCountdown(scout.next_run_at) : 'scheduled'}</div>
          </div>
          <button type="button" class="btn btn-primary" id="run-scout-btn" onclick="triggerScoutRun()">⚡ Run Scout Now</button>
        </div>
      `;
    }

    // Populate providers in promo select filter
    const provSelect = document.getElementById('promos-provider-select');
    if (provSelect && provSelect.options.length <= 1) {
      const providers = [...new Set(promos.map(p => p.provider))].sort();
      providers.forEach(p => {
        const opt = document.createElement('option');
        opt.value = p;
        opt.textContent = p;
        provSelect.appendChild(opt);
      });
    }

    // Filter promos
    const filter = state.promosFilter;
    const filtered = promos.filter(p => {
      if (filter.status !== 'all' && p.status !== filter.status) return false;
      if (filter.provider && p.provider !== filter.provider) return false;
      if (filter.search) {
        const q = filter.search.toLowerCase();
        const str = `${p.provider} ${p.identity || ''} ${p.note || ''} ${p.url || ''} ${p.source || ''}`.toLowerCase();
        if (!str.includes(q)) return false;
      }
      return true;
    });

    const countEl = document.getElementById('promos-filter-count');
    if (countEl) countEl.textContent = `${filtered.length} of ${promos.length}`;

    const listEl = document.getElementById('promos-list');
    if (!listEl) return;

    if (filtered.length === 0) {
      listEl.innerHTML = '<div class="text-muted" style="padding:2rem; text-align:center">No promo leads match current filters.</div>';
      return;
    }

    if (state.views.promos === 'cards') {
      listEl.innerHTML = `
        <div class="promos-grid">
          ${filtered.map(p => `
            <div class="model-card">
              <div class="model-card-head">
                <div>
                  <span class="model-provider">${escapeHtml(p.provider)}</span>
                  <div style="font-weight:600">${escapeHtml(p.identity || p.provider)}</div>
                </div>
                <span class="status-badge status-${p.status === 'new' ? 'ready' : p.status === 'rejected' ? 'depleted' : 'disabled'}">${escapeHtml(p.status)}</span>
              </div>
              <div style="font-size:0.82rem; color:var(--text-secondary); line-height:1.4">${escapeHtml(p.note || 'Free Tier Offer')}</div>
              ${p.url ? `<div style="font-size:0.75rem"><a href="${escapeHtml(p.url)}" target="_blank" rel="noreferrer" style="color:#38bdf8">${escapeHtml(p.url)}</a></div>` : ''}
              ${p.rejected_reason ? `<div style="font-size:0.75rem; color:var(--status-depleted); background:var(--status-depleted-bg); padding:4px 8px; border-radius:4px">Reason: ${escapeHtml(p.rejected_reason)}</div>` : ''}
              <div class="model-card-footer">
                <span class="text-muted" style="font-size:0.72rem">Discovered: ${escapeHtml(formatTimeAgo(p.created_at))} (${escapeHtml(p.source || 'scout')})</span>
                <div style="display:flex; gap:4px">
                  ${p.status !== 'rejected' ? `
                    <button type="button" class="btn btn-sm" onclick="openRejectModal('${p.id}')">Reject</button>
                    <button type="button" class="btn btn-sm btn-primary" onclick="openQuickAddFromPromo('${escapeHtml(p.provider)}', '${escapeHtml(p.url || '')}')">+ Add Key</button>
                  ` : `
                    <button type="button" class="btn btn-sm" onclick="reopenPromo('${p.id}')">Reopen</button>
                  `}
                </div>
              </div>
            </div>
          `).join('')}
        </div>
      `;
    } else {
      const sorted = applySort(filtered, 'promos', (p, col) => {
        if (col === 'provider') return p.provider;
        if (col === 'identity') return p.identity || p.provider;
        if (col === 'status') return p.status;
        if (col === 'created_at') return p.created_at || '';
        return p[col];
      });

      listEl.innerHTML = `
        <div class="table-container">
          <table class="table">
            <thead>
              <tr>
                ${renderSortHeader('promos', 'provider', 'Provider')}
                ${renderSortHeader('promos', 'identity', 'Identity / Key Name')}
                ${renderSortHeader('promos', 'status', 'Status')}
                <th>Offer Details / Note</th>
                <th>URL</th>
                ${renderSortHeader('promos', 'created_at', 'Discovered')}
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              ${sorted.map(p => `
                <tr>
                  <td><span class="cap-tag">${escapeHtml(p.provider)}</span></td>
                  <td style="font-weight:600">${escapeHtml(p.identity || p.provider)}</td>
                  <td><span class="status-badge status-${p.status === 'new' ? 'ready' : p.status === 'rejected' ? 'depleted' : 'disabled'}">${escapeHtml(p.status)}</span></td>
                  <td style="max-width:320px; overflow:hidden; text-overflow:ellipsis; font-size:0.8rem" title="${escapeHtml(p.note || '')}">${escapeHtml(p.note || '-')}</td>
                  <td style="font-size:0.75rem">${p.url ? `<a href="${escapeHtml(p.url)}" target="_blank" rel="noreferrer" style="color:#38bdf8">${escapeHtml(p.url)}</a>` : '-'}</td>
                  <td class="text-muted" style="font-size:0.75rem">${formatTimeAgo(p.created_at)}</td>
                  <td>
                    <div style="display:flex; gap:4px">
                      ${p.status !== 'rejected' ? `
                        <button type="button" class="btn btn-sm" onclick="openRejectModal('${p.id}')">Reject</button>
                        <button type="button" class="btn btn-sm btn-primary" onclick="openQuickAddFromPromo('${escapeHtml(p.provider)}', '${escapeHtml(p.url || '')}')">+ Add</button>
                      ` : `
                        <button type="button" class="btn btn-sm" onclick="reopenPromo('${p.id}')">Reopen</button>
                      `}
                    </div>
                  </td>
                </tr>
              `).join('')}
            </tbody>
          </table>
        </div>
      `;
    }
  }

  // --- RENDER: USAGE & ESTIMATED SAVINGS (Items 8 & 9) ---
  function computeRowCost(r, baselineKey) {
    const inTokens = r.in_tokens || 0;
    const cachedTokens = r.cached_tokens || 0;
    const regularIn = Math.max(0, inTokens - cachedTokens);
    const outTokens = r.out_tokens || 0;

    const price = priceFor(r.model || r.bucket || '', baselineKey);
    if (!price) return null;

    return ((regularIn * price.input) + (cachedTokens * price.cached) + (outTokens * price.output)) / 1000000;
  }

  function renderUsage() {
    const container = document.getElementById('usage-table-container');
    if (!container) return;

    let rows = state.usageModelRows || [];

    // Filter by search query
    if (state.usageSearch) {
      const q = state.usageSearch.toLowerCase();
      rows = rows.filter(r => (r.model || '').toLowerCase().includes(q));
    }

    // Compute savings totals and row costs
    let totalIn = 0;
    let totalCached = 0;
    let totalOut = 0;
    let totalReqs = 0;
    let totalCost = 0;
    // null means no baseline table arrived: the cost is unknown, which is not the same as zero
    let costKnown = true;

    rows = rows.map(r => {
      const modelName = r.model || r.bucket || 'unknown';
      const inTok = r.in_tokens || 0;
      const cachedTok = r.cached_tokens || 0;
      const outTok = r.out_tokens || 0;
      const reqs = r.requests || 0;
      const cost = computeRowCost({ ...r, model: modelName }, state.savingsBaseline);
      const totalTok = inTok + outTok;

      totalIn += inTok;
      totalCached += cachedTok;
      totalOut += outTok;
      totalReqs += reqs;
      if (cost === null) {
        costKnown = false;
      } else {
        totalCost += cost;
      }

      return {
        ...r,
        model: modelName,
        in_tokens: inTok,
        cached_tokens: cachedTok,
        out_tokens: outTok,
        total_tokens: totalTok,
        requests: reqs,
        est_cost: cost
      };
    });

    // Update headline savings
    const headlineEl = document.getElementById('savings-headline-amount');
    if (headlineEl) {
      headlineEl.textContent = costKnown
        ? `$${totalCost.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
        : '-';
    }

    const descEl = document.getElementById('savings-baseline-description');
    if (descEl) {
      const table = state.baselines;
      if (!table) {
        descEl.textContent = 'Baseline prices unavailable, so no cost is estimated.';
      } else if (state.savingsBaseline === table.tier_matched?.id) {
        const small = baselineById(table.tier_matched.small);
        const large = baselineById(table.tier_matched.large);
        descEl.innerHTML =
          `Small models priced as <strong>${small?.label || table.tier_matched.small}</strong>, ` +
          `the rest as <strong>${large?.label || table.tier_matched.large}</strong>. ` +
          `List prices read ${table.as_of}.`;
      } else {
        const b = baselineById(state.savingsBaseline) || baselineById(table.default);
        descEl.innerHTML = b
          ? `Priced as <strong>${b.label}</strong> ($${b.input}/1M in, $${b.cached}/1M cached, ` +
            `$${b.output}/1M out), ${b.vendor} list prices read ${table.as_of} ` +
            `(<a href="${b.source}" target="_blank" rel="noopener">source</a>).`
          : 'Baseline prices unavailable, so no cost is estimated.';
      }
    }

    if (rows.length === 0) {
      container.innerHTML = '<div class="text-muted" style="padding:2rem; text-align:center">No token usage recorded for the selected period.</div>';
      return;
    }

    // Sort usage rows
    const sorted = applySort(rows, 'usage', (r, col) => {
      if (col === 'model') return r.model || '';
      if (col === 'in_tokens') return r.in_tokens;
      if (col === 'cached_tokens') return r.cached_tokens;
      if (col === 'out_tokens') return r.out_tokens;
      if (col === 'total_tokens') return r.total_tokens;
      if (col === 'requests') return r.requests;
      if (col === 'est_cost') return r.est_cost;
      return r[col];
    });

    container.innerHTML = `
      <table class="table">
        <thead>
          <tr>
            ${renderSortHeader('usage', 'model', 'Model Target')}
            ${renderSortHeader('usage', 'in_tokens', 'Input Tokens')}
            ${renderSortHeader('usage', 'cached_tokens', 'Cached Tokens')}
            ${renderSortHeader('usage', 'out_tokens', 'Output Tokens')}
            ${renderSortHeader('usage', 'total_tokens', 'Total Tokens')}
            ${renderSortHeader('usage', 'requests', 'Total Requests')}
            ${renderSortHeader('usage', 'est_cost', 'Est. Baseline Cost')}
          </tr>
        </thead>
        <tbody>
          ${sorted.map(r => `
            <tr>
              <td class="font-mono" style="font-weight:600">${escapeHtml(r.model)}</td>
              <td>${formatTokens(r.in_tokens)}</td>
              <td class="text-muted">${formatTokens(r.cached_tokens)}</td>
              <td>${formatTokens(r.out_tokens)}</td>
              <td style="font-weight:600">${formatTokens(r.total_tokens)}</td>
              <td>${formatNumber(r.requests)}</td>
              <td style="font-weight:700; color:var(--status-ready)">
                ${r.est_cost === null ? '-' : `$${r.est_cost < 0.01 && r.est_cost > 0 ? r.est_cost.toFixed(4) : r.est_cost.toFixed(2)}`}
              </td>
            </tr>
          `).join('')}
        </tbody>
        <tfoot style="border-top:2px solid var(--border-strong); font-weight:700">
          <tr>
            <td>Total (${rows.length} models)</td>
            <td>${formatTokens(totalIn)}</td>
            <td class="text-muted">${formatTokens(totalCached)}</td>
            <td>${formatTokens(totalOut)}</td>
            <td>${formatTokens(totalIn + totalOut)}</td>
            <td>${formatNumber(totalReqs)}</td>
            <td style="color:var(--status-ready); font-size:1.05rem">
              $${totalCost.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
            </td>
          </tr>
        </tfoot>
      </table>
    `;
  }

  // --- RENDER: EVENTS LOG ---
  function renderEvents() {
    const events = state.events || [];
    const listEl = document.getElementById('events-list');
    if (!listEl) return;

    if (events.length === 0) {
      listEl.innerHTML = '<div class="text-muted" style="padding:2rem; text-align:center">No error or quota events recorded.</div>';
      return;
    }

    listEl.innerHTML = `
      <table class="table">
        <thead>
          <tr>
            <th>Time</th>
            <th>Type</th>
            <th>Model / Key</th>
            <th>App</th>
            <th>Message / Error Details</th>
          </tr>
        </thead>
        <tbody>
          ${events.map(ev => `
            <tr>
              <td class="text-muted" style="font-size:0.75rem">${formatTimeAgo(ev.created_at)}</td>
              <td><span class="status-badge status-${ev.kind === 'quota' ? 'depleted' : ev.kind === 'retry' ? 'cooldown' : 'disabled'}">${escapeHtml(ev.kind)}</span></td>
              <td class="font-mono">${escapeHtml(ev.model || '-')}</td>
              <td><span class="cap-tag">${escapeHtml(ev.app || '-')}</span></td>
              <td style="max-width:350px; overflow:hidden; text-overflow:ellipsis; font-size:0.78rem" title="${escapeHtml(ev.message || ev.error_body || '')}">
                ${escapeHtml(ev.message || ev.error_body || '-')}
              </td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    `;
  }

  // --- RENDER: LIVE DRAWER ---
  function renderLiveDrawer() {
    const list = document.getElementById('live-drawer-calls');
    if (!list) return;

    if (state.live.length === 0) {
      list.innerHTML = '<div class="text-muted" style="font-size:0.8rem; text-align:center; padding:1.5rem">No active in-flight calls</div>';
      return;
    }

    list.innerHTML = state.live.map(c => `
      <div class="live-call-item">
        <div style="display:flex; justify-content:space-between; align-items:center">
          <span style="font-weight:600; color:var(--accent-primary)">${escapeHtml(c.app)}</span>
          <button type="button" class="btn btn-sm btn-danger" onclick="cancelCall('${escapeHtml(c.call_id)}')">Kill</button>
        </div>
        <div class="font-mono" style="font-size:0.75rem">${escapeHtml(c.model)}</div>
        <div class="text-muted" style="font-size:0.72rem">State: ${escapeHtml(c.state)} • Running for ${Math.round(c.elapsed_s || 0)}s</div>
      </div>
    `).join('');
  }

  // --- Global Exposed Actions ---
  window.cancelCall = async function (callId) {
    const res = await api(`api/live/${encodeURIComponent(callId)}/cancel`, { method: 'POST' });
    if (res.ok) {
      notify('Call cancelled', 'success');
      loadLive();
    }
  };

  window.triggerScoutRun = async function () {
    const btn = document.getElementById('run-scout-btn');
    if (btn) btn.disabled = true;
    const res = await api('api/scout/run', { method: 'POST' });
    if (res.status === 202) {
      notify('Scout run initiated in background', 'success');
    } else if (res.status === 409) {
      notify('Scout is already running', 'info');
    } else {
      notify('Failed to trigger scout', 'error');
    }
    if (btn) btn.disabled = false;
  };

  window.cancelJob = async function (jobId) {
    const res = await api(`jobs/${encodeURIComponent(jobId)}`, { method: 'DELETE' });
    if (res.ok) {
      notify(`Job ${jobId} cancelled`, 'success');
      loadJobs();
    } else {
      notify('Failed to cancel job', 'error');
    }
  };

  window.openRejectModal = function (promoId) {
    const reason = prompt('Please enter rejection reason (min. 8 characters):\nThis teaches Scout to avoid similar offers.');
    if (!reason || reason.trim().length < 8) {
      if (reason) alert('Rejection reason must be at least 8 characters.');
      return;
    }
    api(`api/promos/${encodeURIComponent(promoId)}/reject`, {
      method: 'POST',
      body: { reason: reason.trim() }
    }).then(res => {
      if (res.ok) {
        notify('Promo rejected and rule persisted', 'success');
        loadPromos();
      } else {
        notify('Failed to reject promo', 'error');
      }
    });
  };

  window.reopenPromo = function (promoId) {
    api(`api/promos/${encodeURIComponent(promoId)}/reopen`, { method: 'POST' }).then(res => {
      if (res.ok) {
        notify('Promo reopened', 'success');
        loadPromos();
      }
    });
  };

  window.openQuickAddFromPromo = function (provider, url) {
    const modal = document.getElementById('modal-quick-add');
    if (modal) {
      document.getElementById('qa-source').value = url || provider;
      modal.classList.add('open');
    }
  };

  // --- Setup Event Listeners ---
  function setupEventListeners() {
    // Navigation tabs
    document.querySelectorAll('.nav-item').forEach(btn => {
      btn.addEventListener('click', () => switchTab(btn.dataset.tab));
    });

    // Theme toggle
    setupRecorder();

    document.getElementById('theme-toggle')?.addEventListener('click', toggleTheme);

    // Refresh button
    document.getElementById('refresh-btn')?.addEventListener('click', refreshAll);

    // Live indicator opens drawer
    document.getElementById('live-indicator-pill')?.addEventListener('click', () => {
      const drawer = document.getElementById('live-drawer');
      drawer?.classList.toggle('open');
    });
    document.getElementById('live-drawer-close')?.addEventListener('click', () => {
      document.getElementById('live-drawer')?.classList.remove('open');
    });

    // Models filter controls
    document.querySelectorAll('#models-status-chips .filter-chip').forEach(btn => {
      btn.addEventListener('click', (e) => {
        document.querySelectorAll('#models-status-chips .filter-chip').forEach(c => c.classList.remove('active'));
        e.currentTarget.classList.add('active');
        state.modelsFilter.status = e.currentTarget.dataset.status;
        renderModels();
      });
    });

    document.querySelectorAll('#models-cap-chips .filter-chip').forEach(btn => {
      btn.addEventListener('click', (e) => {
        document.querySelectorAll('#models-cap-chips .filter-chip').forEach(c => c.classList.remove('active'));
        e.currentTarget.classList.add('active');
        state.modelsFilter.cap = e.currentTarget.dataset.cap;
        renderModels();
      });
    });

    document.getElementById('models-provider-select')?.addEventListener('change', (e) => {
      state.modelsFilter.provider = e.target.value;
      renderModels();
    });

    document.getElementById('models-search-input')?.addEventListener('input', (e) => {
      state.modelsFilter.search = e.target.value;
      renderModels();
    });

    // View Switchers (Item 5)
    // 1. Models view
    const initModelsView = state.views.models;
    document.getElementById('models-view-grid')?.classList.toggle('active', initModelsView === 'cards');
    document.getElementById('models-view-table')?.classList.toggle('active', initModelsView === 'table');

    document.getElementById('models-view-grid')?.addEventListener('click', () => {
      state.views.models = 'cards';
      localStorage.setItem('llmhub_view_models', 'cards');
      document.getElementById('models-view-grid')?.classList.add('active');
      document.getElementById('models-view-table')?.classList.remove('active');
      renderModels();
    });

    document.getElementById('models-view-table')?.addEventListener('click', () => {
      state.views.models = 'table';
      localStorage.setItem('llmhub_view_models', 'table');
      document.getElementById('models-view-table')?.classList.add('active');
      document.getElementById('models-view-grid')?.classList.remove('active');
      renderModels();
    });

    // 2. Queue view
    const initQueueView = state.views.queue;
    document.getElementById('queue-view-table')?.classList.toggle('active', initQueueView === 'table');
    document.getElementById('queue-view-cards')?.classList.toggle('active', initQueueView === 'cards');

    document.getElementById('queue-view-table')?.addEventListener('click', () => {
      state.views.queue = 'table';
      localStorage.setItem('llmhub_view_queue', 'table');
      document.getElementById('queue-view-table')?.classList.add('active');
      document.getElementById('queue-view-cards')?.classList.remove('active');
      renderJobs();
    });

    document.getElementById('queue-view-cards')?.addEventListener('click', () => {
      state.views.queue = 'cards';
      localStorage.setItem('llmhub_view_queue', 'cards');
      document.getElementById('queue-view-cards')?.classList.add('active');
      document.getElementById('queue-view-table')?.classList.remove('active');
      renderJobs();
    });

    // 3. Accounts view
    const initAccountsView = state.views.accounts;
    document.getElementById('accounts-view-cards')?.classList.toggle('active', initAccountsView === 'cards');
    document.getElementById('accounts-view-table')?.classList.toggle('active', initAccountsView === 'table');

    document.getElementById('accounts-view-cards')?.addEventListener('click', () => {
      state.views.accounts = 'cards';
      localStorage.setItem('llmhub_view_accounts', 'cards');
      document.getElementById('accounts-view-cards')?.classList.add('active');
      document.getElementById('accounts-view-table')?.classList.remove('active');
      renderAccounts();
    });

    document.getElementById('accounts-view-table')?.addEventListener('click', () => {
      state.views.accounts = 'table';
      localStorage.setItem('llmhub_view_accounts', 'table');
      document.getElementById('accounts-view-table')?.classList.add('active');
      document.getElementById('accounts-view-cards')?.classList.remove('active');
      renderAccounts();
    });

    // 4. Promos view
    const initPromosView = state.views.promos;
    document.getElementById('promos-view-cards')?.classList.toggle('active', initPromosView === 'cards');
    document.getElementById('promos-view-table')?.classList.toggle('active', initPromosView === 'table');

    document.getElementById('promos-view-cards')?.addEventListener('click', () => {
      state.views.promos = 'cards';
      localStorage.setItem('llmhub_view_promos', 'cards');
      document.getElementById('promos-view-cards')?.classList.add('active');
      document.getElementById('promos-view-table')?.classList.remove('active');
      renderPromos();
    });

    document.getElementById('promos-view-table')?.addEventListener('click', () => {
      state.views.promos = 'table';
      localStorage.setItem('llmhub_view_promos', 'table');
      document.getElementById('promos-view-table')?.classList.add('active');
      document.getElementById('promos-view-cards')?.classList.remove('active');
      renderPromos();
    });

    // Promos Filters (Item 7)
    document.querySelectorAll('#promos-status-chips .filter-chip').forEach(btn => {
      btn.addEventListener('click', (e) => {
        document.querySelectorAll('#promos-status-chips .filter-chip').forEach(c => c.classList.remove('active'));
        e.currentTarget.classList.add('active');
        state.promosFilter.status = e.currentTarget.dataset.status;
        renderPromos();
      });
    });

    document.getElementById('promos-provider-select')?.addEventListener('change', (e) => {
      state.promosFilter.provider = e.target.value;
      renderPromos();
    });

    document.getElementById('promos-search-input')?.addEventListener('input', (e) => {
      state.promosFilter.search = e.target.value;
      renderPromos();
    });

    // Usage Filters & Baseline Controls (Items 8 & 9)
    document.querySelectorAll('#usage-period-chips .filter-chip').forEach(btn => {
      btn.addEventListener('click', (e) => {
        document.querySelectorAll('#usage-period-chips .filter-chip').forEach(c => c.classList.remove('active'));
        e.currentTarget.classList.add('active');
        state.usagePeriod = e.currentTarget.dataset.period;
        loadUsage();
      });
    });

    document.getElementById('savings-baseline-select')?.addEventListener('change', (e) => {
      state.savingsBaseline = e.target.value;
      renderUsage();
    });

    document.getElementById('usage-search-input')?.addEventListener('input', (e) => {
      state.usageSearch = e.target.value;
      renderUsage();
    });

    // Playground interactions
    document.getElementById('playground-send-btn')?.addEventListener('click', sendPlaygroundPrompt);
    document.getElementById('playground-input')?.addEventListener('keydown', (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
        e.preventDefault();
        sendPlaygroundPrompt();
      }
    });

    // Presets
    document.querySelectorAll('.chip-preset').forEach(chip => {
      chip.addEventListener('click', () => {
        const input = document.getElementById('playground-input');
        if (input) input.value = chip.dataset.prompt;
      });
    });

    // Simulator
    document.getElementById('sim-run-btn')?.addEventListener('click', runRoutingSimulator);
    document.getElementById('sim-alias')?.addEventListener('change', runRoutingSimulator);

    // Global Quick Actions
    document.getElementById('btn-quick-add')?.addEventListener('click', () => {
      document.getElementById('modal-quick-add')?.classList.add('open');
    });
    document.getElementById('btn-reload-registry')?.addEventListener('click', async () => {
      const res = await api('api/registry/reload', { method: 'POST' });
      if (res.ok) {
        notify('Registry reloaded from ~/.llmhub/providers.yaml', 'success');
        loadStatus();
      }
    });

    // Token Modal
    document.getElementById('token-btn')?.addEventListener('click', () => {
      const t = prompt('Enter Bearer token for LAN access (leave empty for loopback):', state.token);
      if (t !== null) {
        state.token = t.trim();
        localStorage.setItem('llmhub_token', state.token);
        notify('Bearer token saved in browser', 'success');
        refreshAll();
      }
    });

    // Quick Add Modal Submit
    document.getElementById('qa-submit-btn')?.addEventListener('click', async (event) => {
      const btn = event.currentTarget;
      // The call ends with a live probe against the vendor, which can take a while. Without a
      // busy state the dialog looks dead, and every further click starts another probe.
      if (btn.disabled) return;
      const key = document.getElementById('qa-key')?.value?.trim();
      const source = document.getElementById('qa-source')?.value?.trim();
      if (!source) {
        alert('Please enter a source URL, vendor name or description.');
        return;
      }
      const label = btn.textContent;
      btn.disabled = true;
      btn.textContent = 'Verifying with the vendor...';
      notify('Adding the key, then probing the vendor. This can take a minute.', 'info');
      try {
        const res = await api('api/accounts/quick', {
          method: 'POST',
          body: { api_key: key || undefined, source }
        });
        if (res.ok) {
          notify('Provider account added and verified with 1-token probe!', 'success');
          document.getElementById('modal-quick-add')?.classList.remove('open');
          refreshAll();
        } else {
          const err = await res.json().catch(() => ({}));
          alert('Failed to add: ' + (err.detail || `Could not verify account (HTTP ${res.status}).`));
        }
      } catch (e) {
        // a rejected fetch used to end the handler silently, leaving the dialog open and mute
        alert('Failed to add: ' + (e && e.message ? e.message : e));
      } finally {
        btn.disabled = false;
        btn.textContent = label;
      }
    });

    // Edit Alias Modal Listeners
    document.getElementById('mea-add-btn')?.addEventListener('click', () => {
      const select = document.getElementById('mea-add-model');
      const val = select?.value;
      if (val && state.editingAlias && !state.editingAlias.prefer.includes(val)) {
        state.editingAlias.prefer.push(val);
        renderEditAliasPreferList();
      }
    });

    document.getElementById('mea-save-btn')?.addEventListener('click', saveEditedAlias);

    // Modal Close
    document.querySelectorAll('.modal-close').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('.modal-backdrop').forEach(m => m.classList.remove('open'));
      });
    });
  }

  // --- Entrypoint ---
  document.addEventListener('DOMContentLoaded', () => {
    initTheme();
    setupEventListeners();
    startPolling();
  });
})();
