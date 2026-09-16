/**
 * LLMHub Modern Studio (v2) - Single Page Application
 * Production-ready console with deep API integration and error handling.
 */

(function () {
  'use strict';

  // --- State ---
  const state = {
    token: localStorage.getItem('llmhub_token') || '',
    activeTab: 'overview',
    theme: localStorage.getItem('llmhub_theme') || 'dark',
    status: null,
    live: [],
    inFlightTotal: 0,
    apps: [],
    usage: null,
    jobs: [],
    queueDepth: {},
    promos: [],
    promosFilter: 'all',
    scoutStatus: null,
    events: [],
    modelsFilter: {
      status: 'all',
      cap: 'all',
      provider: '',
      search: '',
      view: localStorage.getItem('llmhub_models_view') || 'grid'
    },
    livePollTimer: null,
    statusPollTimer: null,
    playground: {
      inFlight: false,
      abortController: null
    }
  };

  // The page is served at /v2 on this Mac and at /hub/v2 behind the LAN proxy, which strips
  // the /hub prefix. A path rooted at the server misses the hub entirely from the LAN, so
  // every request is resolved against the app root instead.
  const ROOT = window.location.pathname.replace(/\/v2\/?$/, '') + '/';

  function apiUrl(path) {
    return path.startsWith('http') ? path : ROOT + path.replace(/^\//, '');
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
  async function loadStatus() {
    try {
      const res = await api('/api/status');
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
      const res = await api('/api/live?window_min=15');
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
        renderLiveDrawer();
      }
    } catch (e) {
      console.error('Error loading live calls:', e);
    }
  }

  async function loadUsage() {
    try {
      const res = await api('/api/usage?group_by=day');
      if (res.ok) {
        const data = await res.json();
        state.usage = data;
        renderUsage();
      }
    } catch (e) {
      console.error('Error loading usage:', e);
    }
  }

  async function loadJobs() {
    try {
      const [jobsRes, appsRes] = await Promise.all([
        api('/api/jobs'),
        api('/api/apps')
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
        api('/api/promos'),
        api('/api/scout/status')
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
      const res = await api('/api/events?limit=100');
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
    loadStatus();
    loadLive();
    loadUsage();
    loadJobs();
    loadPromos();
    loadEvents();

    // Poll live calls every 2s if calls exist, else 8s
    clearInterval(state.livePollTimer);
    state.livePollTimer = setInterval(() => {
      loadLive();
    }, state.inFlightTotal > 0 ? 2000 : 8000);

    // Poll full status every 15s
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

    if (tabId === 'usage' && !state.usage) loadUsage();
    if (tabId === 'queue') loadJobs();
    if (tabId === 'accounts') renderAccounts();
    if (tabId === 'promos') loadPromos();
    if (tabId === 'events') loadEvents();
  }

  // --- RENDER: OVERVIEW ---
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
        `;
      }
    }
  }

  // --- RENDER: MODELS FLEET ---
  function renderModels() {
    if (!state.status) return;
    const models = state.status.models || [];
    const filter = state.modelsFilter;

    // Filter list
    const filtered = models.filter(m => {
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

    if (filter.view === 'grid') {
      container.innerHTML = `
        <div class="models-grid">
          ${filtered.map(m => renderModelCard(m)).join('')}
        </div>
      `;
    } else {
      container.innerHTML = `
        <div class="table-container">
          <table class="table">
            <thead>
              <tr>
                <th>Model / Key</th>
                <th>Provider</th>
                <th>Account</th>
                <th>Capabilities</th>
                <th>Status</th>
                <th>Today Usage</th>
                <th>Quota Windows</th>
                <th>Latency</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              ${filtered.map(m => renderModelTableRow(m)).join('')}
            </tbody>
          </table>
        </div>
      `;
    }

    // Attach action listeners
    container.querySelectorAll('[data-action]').forEach(btn => {
      btn.addEventListener('click', handleModelAction);
    });
  }

  function renderModelCard(m) {
    const key = m.key;
    const statusClass = m.disabled ? 'disabled' : m.status === 'ok' ? 'ready' : m.status === 'exhausted' ? 'exhausted' : m.status;
    const caps = m.caps || ['text'];

    // Quota windows
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

        ${m.last_error ? `
          <div style="font-size:0.72rem; color:var(--status-depleted); background:var(--status-depleted-bg); padding:4px 8px; border-radius:4px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${escapeHtml(m.last_error)}">
            ⚠️ ${escapeHtml(m.last_error)}
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
        <td>${formatNumber(m.usage_today?.requests || 0)} reqs / ${formatTokens(m.usage_today?.out_tokens || 0)} tok</td>
        <td>
          <div style="font-size:0.75rem">
            ${m.windows?.daily ? `Daily: ${formatNumber(m.windows.daily.used)}/${m.windows.daily.limit || '∞'}` : '-'}
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
      const res = await api(`/api/models/${encodeURIComponent(key)}/forgive`, { method: 'POST' });
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
      const res = await api(`/api/accounts/${encodeURIComponent(provider)}/${encodeURIComponent(account)}/test`, {
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
      const res = await api(`/api/models/${encodeURIComponent(key)}/${endpoint}`, { method: 'POST' });
      if (res.ok) {
        notify(`Model ${isDisabled ? 'enabled' : 'disabled'}`, 'success');
        loadStatus();
      } else {
        notify('Action failed', 'error');
      }
    }
  }

  // --- RENDER: ROUTING & MATRIX & SIMULATOR ---
  function renderRouting() {
    if (!state.status) return;
    const aliases = state.status.aliases || {};
    const models = state.status.models || [];

    const container = document.getElementById('routing-aliases-grid');
    if (!container) return;

    container.innerHTML = Object.entries(aliases).map(([aliasName, cfg]) => {
      const preferList = cfg.prefer || [];
      return `
        <div class="alias-card">
          <div class="alias-card-head">
            <span class="alias-badge">${escapeHtml(aliasName)}</span>
            <span class="text-muted" style="font-size:0.75rem">Spread: LRU across ${cfg.spread || 1}</span>
          </div>
          <div style="font-size:0.8rem; color:var(--text-secondary)">
            ${aliasName === 'auto' ? 'Default text alias. Rotates across free candidates in preference order.' :
              aliasName === 'fast' ? 'Tuned for low-latency quick responses.' :
              aliasName === 'strong' ? 'High quality / reasoning free models.' :
              aliasName === 'vision' ? 'Enforces vision capability requirement.' :
              aliasName === 'extract' ? 'Context >= 24k tokens floor, strict json.' :
              aliasName === 'local' ? 'Prefers local Ollama instances.' : 'Configured routing alias'}
          </div>

          <div style="font-size:0.75rem; font-weight:600; color:var(--text-muted); margin-top:0.25rem">PREFERENCE CHAIN (${preferList.length}):</div>
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

    // Populate simulator alias select
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

  function runRoutingSimulator() {
    if (!state.status) return;
    const aliasName = document.getElementById('sim-alias')?.value || 'auto';
    const estTokens = parseInt(document.getElementById('sim-tokens')?.value || '500', 10);
    const reqVision = document.getElementById('sim-req-vision')?.checked;
    const reqJson = document.getElementById('sim-req-json')?.checked;
    const reqReasoning = document.getElementById('sim-req-reasoning')?.checked;

    const models = state.status.models || [];
    const aliasCfg = state.status.aliases?.[aliasName] || {};
    const preferList = aliasCfg.prefer || [];

    const candidates = [];

    // Filter models
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

      // Ranked by prefer position
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

    // Aliases group
    const aliasGroup = document.createElement('optgroup');
    aliasGroup.label = 'Aliases (Auto-Routed)';
    Object.keys(state.status.aliases || {}).forEach(a => {
      const opt = document.createElement('option');
      opt.value = a;
      opt.textContent = `${a} (alias)`;
      aliasGroup.appendChild(opt);
    });
    select.appendChild(aliasGroup);

    // Specific models group
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
      // Cancel current
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

    // Headers
    const reqHeaders = {
      'Content-Type': 'application/json',
      'X-Hub-App': appHeader
    };
    if (state.token) reqHeaders['Authorization'] = 'Bearer ' + state.token;

    const chatContainer = document.getElementById('playground-chat');
    if (!chatContainer) return;

    // Add user bubble
    const userMsg = document.createElement('div');
    userMsg.className = 'message-bubble message-user';
    userMsg.textContent = text;
    chatContainer.appendChild(userMsg);
    promptInput.value = '';

    // Add assistant bubble with thinking area and content area
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

                // Check reasoning
                const r = delta.reasoning || delta.reasoning_content;
                if (r) {
                  accumulatedReasoning += r;
                  reasoningBox.style.display = 'block';
                  reasoningText.textContent = accumulatedReasoning;
                  chatContainer.scrollTop = chatContainer.scrollHeight;
                }

                // Check content
                if (delta.content) {
                  accumulatedContent += delta.content;
                  streamContent.textContent = accumulatedContent;
                  chatContainer.scrollTop = chatContainer.scrollHeight;
                }
              }
            } catch (e) {
              // Ignore line parse errors
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

  // --- RENDER: ACCOUNTS TAB ---
  function renderAccounts() {
    const container = document.getElementById('accounts-list-content');
    if (!container || !state.status) return;

    const accounts = state.status.accounts || [];
    if (accounts.length === 0) {
      container.innerHTML = '<div class="text-muted" style="padding:2rem; text-align:center">No provider accounts configured in registry (~/.llmhub/providers.yaml).</div>';
      return;
    }

    container.innerHTML = `
      <div class="accounts-grid">
        ${accounts.map(acc => {
          const keyStatus = acc.key_present
            ? '<span class="status-badge status-ready">🔑 Key Set</span>'
            : acc.kind === 'cli'
            ? '<span class="status-badge" style="background:rgba(192,132,252,0.15); color:var(--cap-vision)">CLI Auth</span>'
            : '<span class="status-badge status-depleted">⚠️ No Key</span>';

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
  }

  window.testAccount = async function (provider, account) {
    notify(`Testing connection to ${provider}/${account}...`, 'info');
    const res = await api(`/api/accounts/${encodeURIComponent(provider)}/${encodeURIComponent(account)}/test`, {
      method: 'POST'
    });
    if (res.ok) {
      const json = await res.json();
      notify(`Connection OK! Model: ${json.model}, latency: ${json.latency_ms} ms`, 'success');
    } else {
      const err = await res.json().catch(() => ({}));
      notify(`Test failed: ${err.detail || res.statusText}`, 'error');
    }
  };

  window.discoverAccount = async function (provider, account) {
    notify(`Discovering models for ${provider}...`, 'info');
    const res = await api(`/api/providers/${encodeURIComponent(provider)}/discover`, {
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

  // --- RENDER: QUEUE & APPS ---
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

    // App shares control bar
    const listEl = document.getElementById('queue-jobs-list');
    if (!listEl) return;

    const appsHtml = apps.length > 0 ? `
      <div style="margin-bottom:1.25rem">
        <h3 style="font-size:0.9rem; font-weight:600; margin-bottom:0.6rem; color:var(--text-secondary)">Connected Applications & Fair-Share Queue Control:</h3>
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

    const jobsTableHtml = jobs.length === 0 ? `
      <div class="text-muted" style="padding: 2rem; text-align:center">No active or pending jobs in queue.</div>
    ` : `
      <table class="table">
        <thead>
          <tr>
            <th>Job ID</th>
            <th>App</th>
            <th>Model</th>
            <th>State</th>
            <th>Created</th>
            <th>Action</th>
          </tr>
        </thead>
        <tbody>
          ${jobs.map(j => `
            <tr>
              <td class="font-mono">${escapeHtml(j.id)}</td>
              <td><span class="cap-tag">${escapeHtml(j.app)}</span></td>
              <td class="font-mono">${escapeHtml(j.model || j.alias || 'auto')}</td>
              <td><span class="status-badge status-${j.state === 'running' ? 'ready' : j.state === 'waiting_quota' ? 'cooldown' : 'disabled'}">${escapeHtml(j.state)}</span></td>
              <td class="text-muted">${formatCountdown(j.created_at)} ago</td>
              <td>
                <button type="button" class="btn btn-sm btn-danger" onclick="cancelJob('${j.id}')">Cancel</button>
              </td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    `;

    listEl.innerHTML = appsHtml + jobsTableHtml;
  }

  window.toggleAppPause = async function (appName, isPaused) {
    const endpoint = isPaused ? 'resume' : 'pause';
    const res = await api(`/api/apps/${encodeURIComponent(appName)}/${endpoint}`, { method: 'POST' });
    if (res.ok) {
      notify(`Application ${appName} ${isPaused ? 'resumed' : 'paused'}`, 'success');
      loadJobs();
    } else {
      notify('Failed to update app status', 'error');
    }
  };

  // --- RENDER: PROMOS & SCOUT ---
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

    const listEl = document.getElementById('promos-list');
    if (!listEl) return;

    if (promos.length === 0) {
      listEl.innerHTML = '<div class="text-muted" style="padding:2rem; text-align:center">No promo leads found yet. Click "Run Scout Now" above!</div>';
      return;
    }

    listEl.innerHTML = `
      <div style="display:grid; grid-template-columns:repeat(auto-fill, minmax(340px, 1fr)); gap:1rem">
        ${promos.map(p => `
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
              <span class="text-muted">Source: ${escapeHtml(p.source || 'scout')}</span>
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
  }

  // --- RENDER: EVENTS & USAGE ---
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
              <td class="text-muted" style="font-size:0.75rem">${formatCountdown(ev.created_at)} ago</td>
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

  function renderUsage() {
    const container = document.getElementById('usage-chart-container');
    if (!container || !state.usage) return;

    const rows = state.usage.rows || [];
    if (rows.length === 0) {
      container.innerHTML = '<div class="text-muted" style="padding:2rem; text-align:center">No historical usage data recorded yet.</div>';
      return;
    }

    const maxTokens = Math.max(...rows.map(r => (r.out_tokens || 0) + (r.in_tokens || 0)), 1000);
    const width = 800;
    const height = 180;
    const barWidth = Math.max(12, Math.floor((width - 60) / (rows.length * 2)));

    const bars = rows.slice(-14).map((r, idx) => {
      const total = (r.out_tokens || 0) + (r.in_tokens || 0);
      const barHeight = Math.max(4, Math.round((total / maxTokens) * (height - 40)));
      const x = idx * (barWidth * 2) + 30;
      const y = height - barHeight - 20;
      const label = (r.day || '').slice(5);
      return `
        <rect x="${x}" y="${y}" width="${barWidth}" height="${barHeight}" rx="4" fill="url(#chartGrad)">
          <title>${r.day}: ${formatTokens(total)} tokens (${formatNumber(r.requests)} reqs)</title>
        </rect>
        <text x="${x + barWidth/2}" y="${height - 5}" font-size="10" fill="var(--text-muted)" text-anchor="middle">${label}</text>
      `;
    }).join('');

    container.innerHTML = `
      <svg viewBox="0 0 ${width} ${height}" style="width:100%; height:auto; display:block">
        <defs>
          <linearGradient id="chartGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="#6366f1" />
            <stop offset="100%" stop-color="#38bdf8" />
          </linearGradient>
        </defs>
        ${bars}
      </svg>
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
          <button type="button" class="btn btn-sm btn-danger" onclick="cancelCall('${c.call_id}')">Kill</button>
        </div>
        <div class="font-mono" style="font-size:0.75rem">${escapeHtml(c.model)}</div>
        <div class="text-muted" style="font-size:0.72rem">State: ${c.state} • Running for ${Math.round(c.elapsed_s || 0)}s</div>
      </div>
    `).join('');
  }

  // --- Global Exposed Actions ---
  window.cancelCall = async function (callId) {
    const res = await api(`/api/live/${callId}/cancel`, { method: 'POST' });
    if (res.ok) {
      notify('Call cancelled', 'success');
      loadLive();
    }
  };

  window.triggerScoutRun = async function () {
    const btn = document.getElementById('run-scout-btn');
    if (btn) btn.disabled = true;
    const res = await api('/api/scout/run', { method: 'POST' });
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
    const res = await api(`/jobs/${jobId}`, { method: 'DELETE' });
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
    api(`/api/promos/${promoId}/reject`, {
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
    api(`/api/promos/${promoId}/reopen`, { method: 'POST' }).then(res => {
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

    document.getElementById('models-view-grid')?.addEventListener('click', () => {
      state.modelsFilter.view = 'grid';
      localStorage.setItem('llmhub_models_view', 'grid');
      document.getElementById('models-view-grid')?.classList.add('active');
      document.getElementById('models-view-table')?.classList.remove('active');
      renderModels();
    });

    document.getElementById('models-view-table')?.addEventListener('click', () => {
      state.modelsFilter.view = 'table';
      localStorage.setItem('llmhub_models_view', 'table');
      document.getElementById('models-view-table')?.classList.add('active');
      document.getElementById('models-view-grid')?.classList.remove('active');
      renderModels();
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
      const res = await api('/api/registry/reload', { method: 'POST' });
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
    document.getElementById('qa-submit-btn')?.addEventListener('click', async () => {
      const key = document.getElementById('qa-key')?.value?.trim();
      const source = document.getElementById('qa-source')?.value?.trim();
      if (!source) {
        alert('Please enter a source URL, vendor name or description.');
        return;
      }
      notify('Testing and adding provider...', 'info');
      const res = await api('/api/accounts/quick', {
        method: 'POST',
        body: { api_key: key || undefined, source }
      });
      if (res.ok) {
        notify('Provider account added and verified with 1-token probe!', 'success');
        document.getElementById('modal-quick-add')?.classList.remove('open');
        refreshAll();
      } else {
        const err = await res.json().catch(() => ({}));
        alert('Failed to add: ' + (err.detail || 'Could not verify account.'));
      }
    });

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
