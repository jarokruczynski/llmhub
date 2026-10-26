/**
 * Event Log: every attempt the gateway made (usage rows) and every event it wrote, on one
 * timeline. Filters, a brushable chart, a per-model leaderboard and expandable rows.
 */
(function () {
  'use strict';

  const RANGES = [
    { key: '15m', label: '15m', ms: 15 * 60e3 },
    { key: '1h', label: '1h', ms: 3600e3 },
    { key: '6h', label: '6h', ms: 6 * 3600e3 },
    { key: '24h', label: '24h', ms: 24 * 3600e3 },
    { key: '3d', label: '3d', ms: 3 * 86400e3 },
    { key: '7d', label: '7d', ms: 7 * 86400e3 }
  ];
  const BUCKETS_MS = [60e3, 2 * 60e3, 5 * 60e3, 10 * 60e3, 15 * 60e3, 30 * 60e3, 3600e3, 2 * 3600e3, 3 * 3600e3, 6 * 3600e3];
  const STATUS_ORDER = ['ok', 'quota', 'error', 'truncated', 'abandoned'];
  const PAGE = 150;
  const LIVE_MS = 5000;
  const CHAIN_GAP_MS = 5 * 60e3;
  const STORE_KEY = 'llmhub_eventlog';

  const saved = readSaved();
  const ev = {
    range: saved.range || '6h',
    kind: saved.kind || 'all',
    app: '',
    model: '',
    search: '',
    live: saved.live !== false,
    chains: !!saved.chains,
    zoom: null,
    sort: { col: 'ts', asc: false },
    boardSort: { col: 'calls', asc: false },
    shown: PAGE,
    open: new Set(),
    calls: [],
    events: [],
    truncated: { calls: false, events: false },
    seen: null,
    fresh: new Set(),
    timer: null,
    loading: false,
    loadedAt: null,
    bound: false
  };

  function readSaved() {
    try {
      return JSON.parse(localStorage.getItem(STORE_KEY) || '{}') || {};
    } catch (e) {
      return {};
    }
  }

  function persist() {
    try {
      localStorage.setItem(STORE_KEY, JSON.stringify({ range: ev.range, kind: ev.kind, live: ev.live, chains: ev.chains }));
    } catch (e) { /* private window */ }
  }

  function api(path) {
    return window.LLMHub.api(path);
  }

  function esc(value) {
    if (value === null || value === undefined) return '';
    return String(value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  function rangeMs() {
    return (RANGES.find(r => r.key === ev.range) || RANGES[2]).ms;
  }

  // --- formatting ---
  function fmtTokens(n) {
    if (!n) return '0';
    if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(n >= 1e4 ? 0 : 1) + 'k';
    return String(n);
  }

  function fmtMs(ms) {
    if (ms === null || ms === undefined) return '-';
    if (ms < 1000) return `${Math.round(ms)} ms`;
    const s = ms / 1000;
    if (s < 59.95) return `${s.toFixed(s < 10 ? 2 : 1)} s`;
    const whole = Math.round(s);
    const m = Math.floor(whole / 60);
    if (m < 60) return `${m}m ${pad(whole % 60)}s`;
    return `${Math.floor(m / 60)}h ${pad(m % 60)}m`;
  }

  function pad(n, w = 2) {
    return String(n).padStart(w, '0');
  }

  function fmtClock(t, withMs) {
    const d = new Date(t);
    const base = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    return withMs ? `${base}<span class="ev-ms">.${pad(d.getMilliseconds(), 3)}</span>` : base;
  }

  function fmtDay(t) {
    const d = new Date(t);
    const today = new Date();
    if (d.toDateString() === today.toDateString()) return '';
    const yesterday = new Date(today.getTime() - 86400e3);
    if (d.toDateString() === yesterday.toDateString()) return 'yesterday';
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  }

  function fmtAgo(t) {
    const s = Math.max(0, Math.round((Date.now() - t) / 1000));
    if (s < 60) return `${s}s ago`;
    const m = Math.round(s / 60);
    if (m < 60) return `${m}m ago`;
    const h = Math.round(m / 60);
    if (h < 48) return `${h}h ago`;
    return `${Math.round(h / 24)}d ago`;
  }

  function fmtBucketLabel(t, bucketMs) {
    const d = new Date(t);
    if (bucketMs >= 86400e3) return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    const day = fmtDay(t);
    return `${day ? day + ' ' : ''}${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  function percentile(sorted, p) {
    if (!sorted.length) return null;
    const idx = Math.min(sorted.length - 1, Math.max(0, Math.ceil((p / 100) * sorted.length) - 1));
    return sorted[idx];
  }

  // --- data shaping ---
  function shapeCall(row) {
    const t = Date.parse(row.ts);
    const latency = row.latency_ms || 0;
    const out = row.out_tokens || 0;
    // a sync row is written when the answer is in; a stream row when its headers arrive
    const startT = row.stream ? t : t - latency;
    return {
      uid: 'c' + row.id,
      type: 'call',
      t,
      app: row.app || '',
      model: row.model || '',
      provider: row.provider || '',
      account: row.account || '',
      status: row.status || 'ok',
      error_code: row.error_code || '',
      attempt: row.attempt || 1,
      latency_ms: latency,
      in_tokens: row.in_tokens || 0,
      out_tokens: out,
      cached_tokens: row.cached_tokens || 0,
      total_tokens: row.total_tokens || 0,
      estimated: !!row.estimated,
      stream: !!row.stream,
      tps: latency > 0 && out > 0 ? out / (latency / 1000) : null,
      startT,
      endT: startT + latency,
      raw: row
    };
  }

  function shapeEvent(row) {
    return {
      uid: 'e' + row.id,
      type: 'event',
      t: Date.parse(row.ts),
      app: row.app || '',
      model: row.model || '',
      account: row.account || '',
      kind: row.kind || '',
      message: row.message || '',
      raw: row
    };
  }

  // Attempt n of one request follows attempt n-1 of the same app. The hub does not store a
  // request id on usage rows, so the chain is inferred: nearest open chain, same app, next number.
  function buildChains(calls) {
    const asc = calls.slice().sort((a, b) => a.startT - b.startT);
    const open = new Map();
    const chains = [];
    for (const c of asc) {
      let chain = null;
      if (c.attempt > 1) {
        const list = open.get(c.app) || [];
        for (let i = list.length - 1; i >= 0; i--) {
          const cand = list[i];
          const last = cand.attempts[cand.attempts.length - 1];
          if (last.attempt === c.attempt - 1 && c.startT >= last.startT && c.startT - last.endT <= CHAIN_GAP_MS) {
            chain = cand;
            list.splice(i, 1);
            break;
          }
        }
      }
      if (!chain) {
        chain = { attempts: [] };
        chains.push(chain);
      }
      chain.attempts.push(c);
      if (!open.has(c.app)) open.set(c.app, []);
      if (c.status !== 'ok') open.get(c.app).push(chain);
    }
    return chains.map(ch => {
      const last = ch.attempts[ch.attempts.length - 1];
      const first = ch.attempts[0];
      if (ch.attempts.length === 1) return first;
      return {
        ...last,
        uid: 'r' + first.uid,
        type: 'chain',
        attempts: ch.attempts,
        startT: first.startT,
        latency_ms: last.endT - first.startT,
        in_tokens: ch.attempts.reduce((s, a) => s + a.in_tokens, 0),
        out_tokens: ch.attempts.reduce((s, a) => s + a.out_tokens, 0),
        cached_tokens: ch.attempts.reduce((s, a) => s + a.cached_tokens, 0)
      };
    });
  }

  function isProblem(item) {
    if (item.type === 'event') return true;
    if (item.attempts) return item.attempts.some(a => a.status !== 'ok');
    return item.status !== 'ok';
  }

  function matchesSearch(item, q) {
    if (!q) return true;
    const hay = [item.app, item.model, item.provider, item.account, item.status, item.error_code, item.kind, item.message]
      .filter(Boolean).join(' ').toLowerCase();
    return q.split(/\s+/).every(part => hay.includes(part));
  }

  function baseFiltered() {
    const q = ev.search.trim().toLowerCase();
    const keep = item => {
      if (ev.app && item.app !== ev.app) return false;
      if (ev.model && item.model !== ev.model) return false;
      if (ev.zoom && (item.t < ev.zoom[0] || item.t >= ev.zoom[1])) return false;
      return matchesSearch(item, q);
    };
    return { calls: ev.calls.filter(keep), events: ev.events.filter(keep) };
  }

  function logRows(base) {
    let calls = base.calls;
    if (ev.chains) calls = buildChains(calls);
    let rows;
    if (ev.kind === 'calls') rows = calls;
    else if (ev.kind === 'events') rows = base.events;
    else if (ev.kind === 'problems') rows = calls.filter(isProblem).concat(base.events);
    else rows = calls.concat(base.events);
    return sortRows(rows, ev.sort, logKey);
  }

  function logKey(item, col) {
    switch (col) {
      case 'ts': return item.t;
      case 'type': return item.type === 'event' ? 'z' + item.kind : item.status;
      case 'app': return item.app;
      case 'model': return item.model;
      case 'attempt': return item.attempts ? item.attempts.length : item.attempt || 0;
      case 'latency': return item.latency_ms ?? -1;
      case 'tokens': return (item.in_tokens || 0) + (item.out_tokens || 0);
      case 'tps': return item.tps ?? -1;
      default: return '';
    }
  }

  function sortRows(rows, sort, keyOf) {
    const dir = sort.asc ? 1 : -1;
    return rows.slice().sort((a, b) => {
      const ka = keyOf(a, sort.col);
      const kb = keyOf(b, sort.col);
      if (ka < kb) return -dir;
      if (ka > kb) return dir;
      return b.t - a.t;
    });
  }

  // --- loading ---
  async function load(quiet) {
    if (ev.loading) return;
    ev.loading = true;
    const since = new Date(Date.now() - rangeMs()).toISOString();
    const limit = rangeMs() > 6 * 3600e3 ? 5000 : 2000;
    try {
      const res = await api(`api/activity?since=${encodeURIComponent(since)}&limit=${limit}`);
      if (!res.ok) {
        if (!quiet) renderError(`HTTP ${res.status} from api/activity`);
        return;
      }
      const data = await res.json();
      const calls = (data.calls || []).map(shapeCall);
      const events = (data.events || []).map(shapeEvent);
      const ids = new Set(calls.map(c => c.uid).concat(events.map(e => e.uid)));
      ev.fresh = new Set();
      if (ev.seen) for (const id of ids) if (!ev.seen.has(id)) ev.fresh.add(id);
      ev.seen = ids;
      ev.calls = calls;
      ev.events = events;
      ev.truncated = data.truncated || { calls: false, events: false };
      ev.loadedAt = Date.now();
      render();
    } catch (e) {
      if (!quiet) renderError(String(e));
    } finally {
      ev.loading = false;
    }
  }

  function schedule() {
    clearInterval(ev.timer);
    ev.timer = null;
    if (ev.live && window.LLMHub.activeTab() === 'events' && !document.hidden) {
      ev.timer = setInterval(() => load(true), LIVE_MS);
    }
  }

  function renderError(text) {
    const box = document.getElementById('events-list');
    if (box) box.innerHTML = `<div class="ev-empty">Could not load the event log: ${esc(text)}</div>`;
  }

  // --- rendering ---
  function render() {
    const base = baseFiltered();
    renderControls(base);
    renderKpis(base);
    renderChart();
    renderBoard(base);
    renderLog(base);
  }

  function renderControls() {
    const ranges = document.getElementById('ev-ranges');
    if (ranges) {
      ranges.innerHTML = RANGES.map(r =>
        `<button type="button" class="filter-chip ${r.key === ev.range ? 'active' : ''}" data-range="${r.key}">${r.label}</button>`
      ).join('');
    }
    document.querySelectorAll('#ev-kinds .filter-chip').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.kind === ev.kind);
    });
    const counts = { all: 0, calls: 0, problems: 0, events: ev.events.length };
    counts.calls = ev.calls.length;
    counts.problems = ev.calls.filter(isProblem).length + ev.events.length;
    counts.all = counts.calls + counts.events;
    document.querySelectorAll('#ev-kinds .filter-chip').forEach(btn => {
      const n = counts[btn.dataset.kind];
      const span = btn.querySelector('.ev-count');
      if (span) span.textContent = n === undefined ? '' : fmtTokens(n);
    });
    fillSelect('ev-app', 'All apps', uniq(ev.calls.concat(ev.events).map(i => i.app)), ev.app);
    fillSelect('ev-model', 'All models', uniq(ev.calls.concat(ev.events).map(i => i.model)), ev.model);
    const live = document.getElementById('ev-live');
    if (live) {
      live.classList.toggle('on', ev.live);
      live.setAttribute('aria-pressed', String(ev.live));
    }
    const chains = document.getElementById('ev-chains');
    if (chains) chains.checked = ev.chains;
    const stamp = document.getElementById('ev-stamp');
    if (stamp) stamp.textContent = ev.loadedAt ? `updated ${fmtClock(ev.loadedAt, false)}` : '';
    const zoom = document.getElementById('ev-zoom');
    if (zoom) {
      zoom.hidden = !ev.zoom;
      if (ev.zoom) {
        zoom.querySelector('span').textContent = `${fmtBucketLabel(ev.zoom[0], 60e3)} → ${fmtBucketLabel(ev.zoom[1], 60e3)}`;
      }
    }
    renderActiveFilters();
  }

  function renderActiveFilters() {
    const box = document.getElementById('ev-active');
    if (!box) return;
    const pills = [];
    if (ev.app) pills.push(['app', `app: ${ev.app}`]);
    if (ev.model) pills.push(['model', `model: ${ev.model}`]);
    if (ev.search) pills.push(['search', `“${ev.search}”`]);
    box.innerHTML = pills.map(([k, label]) =>
      `<button type="button" class="ev-pill" data-clear="${k}" title="Remove filter">${esc(label)} <b>×</b></button>`
    ).join('');
  }

  function uniq(values) {
    return Array.from(new Set(values.filter(Boolean))).sort();
  }

  function fillSelect(id, allLabel, values, current) {
    const sel = document.getElementById(id);
    if (!sel) return;
    if (current && !values.includes(current)) values = values.concat([current]);
    const html = `<option value="">${allLabel} (${values.length})</option>` +
      values.map(v => `<option value="${esc(v)}" ${v === current ? 'selected' : ''}>${esc(v)}</option>`).join('');
    if (sel.dataset.html !== html) {
      sel.innerHTML = html;
      sel.dataset.html = html;
    }
    sel.value = current || '';
  }

  function renderKpis(base) {
    const box = document.getElementById('ev-kpis');
    if (!box) return;
    const calls = base.calls;
    const ok = calls.filter(c => c.status === 'ok');
    const failed = calls.filter(c => c.status !== 'ok' && c.status !== 'abandoned');
    const quota = calls.filter(c => c.status === 'quota').length;
    const lat = ok.map(c => c.latency_ms).sort((a, b) => a - b);
    const tin = calls.reduce((s, c) => s + c.in_tokens, 0);
    const tout = calls.reduce((s, c) => s + c.out_tokens, 0);
    const tcached = calls.reduce((s, c) => s + c.cached_tokens, 0);
    const busy = calls.reduce((s, c) => s + c.latency_ms, 0);
    const retries = calls.filter(c => c.attempt > 1).length;
    const tpsList = ok.map(c => c.tps).filter(v => v !== null).sort((a, b) => a - b);
    const rate = calls.length ? (100 * ok.length) / calls.length : null;
    const models = uniq(calls.map(c => c.model)).length;
    const card = (title, value, sub, tone) => `
      <div class="ev-kpi ${tone || ''}">
        <div class="ev-kpi-title">${title}</div>
        <div class="ev-kpi-value">${value}</div>
        <div class="ev-kpi-sub">${sub}</div>
      </div>`;
    box.innerHTML = [
      card('Calls', fmtTokens(calls.length), `${models} model${models === 1 ? '' : 's'} · ${retries} retr${retries === 1 ? 'y' : 'ies'}`),
      card('Success', rate === null ? '-' : `${rate.toFixed(rate >= 99.95 || rate < 10 ? 0 : 1)}%`,
        `${failed.length} failed · ${quota} quota`, rate === null ? '' : rate >= 95 ? 'good' : rate >= 80 ? 'warn' : 'bad'),
      card('Tokens', `${fmtTokens(tin)} <small>in</small> · ${fmtTokens(tout)} <small>out</small>`,
        tcached ? `${fmtTokens(tcached)} cached (${tin ? Math.round((100 * tcached) / tin) : 0}% of in)` : 'no cache hits'),
      card('Latency p50 / p95', lat.length ? `${fmtMs(percentile(lat, 50))} <small>/</small> ${fmtMs(percentile(lat, 95))}` : '-',
        lat.length ? `max ${fmtMs(lat[lat.length - 1])}` : 'no served calls'),
      card('Speed', tpsList.length ? `${Math.round(percentile(tpsList, 50))} <small>tok/s</small>` : '-',
        tpsList.length ? `p90 ${Math.round(percentile(tpsList, 90))} tok/s` : 'median output speed'),
      card('Model time', fmtMs(busy), `${base.events.length} event${base.events.length === 1 ? '' : 's'} in view`)
    ].join('');
    const warn = document.getElementById('ev-truncated');
    if (warn) {
      const t = ev.truncated || {};
      warn.hidden = !(t.calls || t.events);
      warn.textContent = 'Only the newest rows of this range were loaded. Pick a shorter range to see all of them.';
    }
  }

  // --- chart ---
  function chartModel() {
    const end = Date.now();
    const start = end - rangeMs();
    const target = rangeMs() / 60;
    const bucketMs = BUCKETS_MS.find(b => b >= target) || 86400e3;
    const first = Math.floor(start / bucketMs) * bucketMs;
    const n = Math.ceil((end - first) / bucketMs);
    const buckets = Array.from({ length: n }, (_, i) => ({
      t0: first + i * bucketMs, t1: first + (i + 1) * bucketMs,
      ok: 0, quota: 0, error: 0, truncated: 0, abandoned: 0, events: 0,
      tokens: 0, latency: 0, served: 0
    }));
    const q = ev.search.trim().toLowerCase();
    const keep = i => (!ev.app || i.app === ev.app) && (!ev.model || i.model === ev.model) && matchesSearch(i, q);
    for (const c of ev.calls) {
      if (!keep(c)) continue;
      const b = buckets[Math.floor((c.t - first) / bucketMs)];
      if (!b) continue;
      const key = STATUS_ORDER.includes(c.status) ? c.status : 'error';
      b[key] += 1;
      b.tokens += c.in_tokens + c.out_tokens;
      if (c.status === 'ok') {
        b.latency += c.latency_ms;
        b.served += 1;
      }
    }
    for (const e of ev.events) {
      if (!keep(e)) continue;
      const b = buckets[Math.floor((e.t - first) / bucketMs)];
      if (b) b.events += 1;
    }
    return { buckets, bucketMs, start: first, end: first + n * bucketMs };
  }

  function renderChart() {
    const host = document.getElementById('ev-chart');
    if (!host) return;
    const m = chartModel();
    ev.chart = m;
    const W = Math.max(320, host.clientWidth || 900);
    const H = 150;
    const padL = 34, padR = 8, padT = 14, padB = 22;
    const iw = W - padL - padR;
    const ih = H - padT - padB;
    const maxCalls = Math.max(1, ...m.buckets.map(b => STATUS_ORDER.reduce((s, k) => s + b[k], 0)));
    const bw = iw / m.buckets.length;
    const x = t => padL + ((t - m.start) / (m.end - m.start)) * iw;
    const y = v => padT + ih - (v / maxCalls) * ih;
    const parts = [];
    const ticks = niceTicks(maxCalls);
    for (const v of ticks) {
      parts.push(`<line class="ev-grid" x1="${padL}" x2="${W - padR}" y1="${y(v)}" y2="${y(v)}"/>`);
      parts.push(`<text class="ev-axis" x="${padL - 6}" y="${y(v) + 3}" text-anchor="end">${fmtTokens(v)}</text>`);
    }
    m.buckets.forEach((b, i) => {
      let acc = 0;
      const bx = padL + i * bw + Math.min(1, bw * 0.1);
      const w = Math.max(1, bw - Math.min(2, bw * 0.2));
      for (const k of STATUS_ORDER) {
        if (!b[k]) continue;
        const y1 = y(acc + b[k]);
        const y0 = y(acc);
        parts.push(`<rect class="ev-bar ev-s-${k}" x="${bx.toFixed(1)}" y="${y1.toFixed(1)}" width="${w.toFixed(1)}" height="${Math.max(1, y0 - y1).toFixed(1)}"/>`);
        acc += b[k];
      }
      if (b.events) {
        parts.push(`<circle class="ev-dot" cx="${(bx + w / 2).toFixed(1)}" cy="${padT - 6}" r="${Math.min(4, 1.5 + Math.log2(1 + b.events))}"/>`);
      }
    });
    const labelEvery = Math.ceil(m.buckets.length / Math.max(2, Math.floor(iw / 90)));
    m.buckets.forEach((b, i) => {
      if (i % labelEvery) return;
      parts.push(`<text class="ev-axis" x="${(padL + i * bw).toFixed(1)}" y="${H - 6}">${esc(fmtBucketLabel(b.t0, m.bucketMs))}</text>`);
    });
    if (ev.zoom) {
      const zx0 = Math.max(padL, x(ev.zoom[0]));
      const zx1 = Math.min(W - padR, x(ev.zoom[1]));
      parts.push(`<rect class="ev-zoom-shade" x="${padL}" y="${padT}" width="${Math.max(0, zx0 - padL)}" height="${ih}"/>`);
      parts.push(`<rect class="ev-zoom-shade" x="${zx1}" y="${padT}" width="${Math.max(0, W - padR - zx1)}" height="${ih}"/>`);
      parts.push(`<rect class="ev-zoom-frame" x="${zx0}" y="${padT}" width="${Math.max(1, zx1 - zx0)}" height="${ih}"/>`);
    }
    parts.push(`<rect id="ev-brush" class="ev-brush" x="0" y="${padT}" width="0" height="${ih}" visibility="hidden"/>`);
    parts.push(`<line id="ev-cursor" class="ev-cursor" x1="0" x2="0" y1="${padT}" y2="${padT + ih}" visibility="hidden"/>`);
    host.innerHTML = `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="Calls per ${fmtMs(m.bucketMs)} by status">${parts.join('')}</svg>`;
    ev.chartGeom = { W, H, padL, padR, padT, ih, iw, bw, x };
    const legend = document.getElementById('ev-legend');
    if (legend) {
      const totals = {};
      for (const k of STATUS_ORDER) totals[k] = m.buckets.reduce((s, b) => s + b[k], 0);
      const evTotal = m.buckets.reduce((s, b) => s + b.events, 0);
      legend.innerHTML = STATUS_ORDER.filter(k => totals[k] || k === 'ok').map(k =>
        `<span class="ev-leg"><i class="ev-sw ev-s-${k}"></i>${k} <b>${fmtTokens(totals[k])}</b></span>`
      ).join('') + `<span class="ev-leg"><i class="ev-sw ev-sw-dot"></i>events <b>${fmtTokens(evTotal)}</b></span>` +
        `<span class="ev-leg text-muted">bar = ${fmtMs(m.bucketMs)} · drag to zoom · click a bar to pick it</span>`;
    }
  }

  function niceTicks(max) {
    const step = Math.pow(10, Math.floor(Math.log10(max)));
    const unit = max / step > 5 ? step * 2 : max / step > 2 ? step : step / 2;
    const out = [];
    for (let v = 0; v <= max + 1e-9; v += Math.max(1, unit)) out.push(Math.round(v));
    return out.slice(0, 6);
  }

  function chartBucketAt(clientX) {
    const svg = document.querySelector('#ev-chart svg');
    if (!svg || !ev.chart || !ev.chartGeom) return null;
    const rect = svg.getBoundingClientRect();
    const px = clientX - rect.left;
    const g = ev.chartGeom;
    const i = Math.floor((px - g.padL) / g.bw);
    if (i < 0 || i >= ev.chart.buckets.length) return null;
    return { i, px, bucket: ev.chart.buckets[i] };
  }

  function bindChart() {
    const host = document.getElementById('ev-chart');
    const tip = document.getElementById('ev-tip');
    if (!host || !tip) return;
    let drag = null;
    host.addEventListener('mousemove', (e) => {
      const hit = chartBucketAt(e.clientX);
      const cursor = document.getElementById('ev-cursor');
      if (drag) {
        const brush = document.getElementById('ev-brush');
        const a = Math.min(drag.px, hit ? hit.px : drag.px);
        const b = Math.max(drag.px, hit ? hit.px : drag.px);
        brush.setAttribute('x', a);
        brush.setAttribute('width', Math.max(1, b - a));
        brush.setAttribute('visibility', 'visible');
        drag.moved = drag.moved || Math.abs((hit ? hit.px : drag.px) - drag.px) > 4;
      }
      if (!hit) {
        tip.hidden = true;
        if (cursor) cursor.setAttribute('visibility', 'hidden');
        return;
      }
      if (cursor) {
        cursor.setAttribute('x1', hit.px);
        cursor.setAttribute('x2', hit.px);
        cursor.setAttribute('visibility', 'visible');
      }
      const b = hit.bucket;
      const total = STATUS_ORDER.reduce((s, k) => s + b[k], 0);
      tip.innerHTML = `
        <div class="ev-tip-head">${esc(fmtBucketLabel(b.t0, ev.chart.bucketMs))} – ${esc(fmtClock(b.t1, false).slice(0, 5))}</div>
        <div><b>${total}</b> call${total === 1 ? '' : 's'}${b.events ? ` · <b>${b.events}</b> event${b.events === 1 ? '' : 's'}` : ''}</div>
        ${STATUS_ORDER.filter(k => b[k]).map(k => `<div><i class="ev-sw ev-s-${k}"></i>${k}: ${b[k]}</div>`).join('')}
        ${b.tokens ? `<div>tokens: ${fmtTokens(b.tokens)}</div>` : ''}
        ${b.served ? `<div>avg latency: ${fmtMs(b.latency / b.served)}</div>` : ''}`;
      tip.hidden = false;
      const hostRect = host.getBoundingClientRect();
      const left = Math.min(hostRect.width - 180, Math.max(0, hit.px + 12));
      tip.style.left = `${left}px`;
      tip.style.top = '8px';
    });
    host.addEventListener('mouseleave', () => {
      tip.hidden = true;
      const cursor = document.getElementById('ev-cursor');
      if (cursor) cursor.setAttribute('visibility', 'hidden');
    });
    host.addEventListener('mousedown', (e) => {
      const hit = chartBucketAt(e.clientX);
      if (!hit) return;
      e.preventDefault();
      drag = { px: hit.px, i: hit.i, moved: false };
    });
    window.addEventListener('mouseup', (e) => {
      if (!drag) return;
      const start = drag;
      drag = null;
      const hit = chartBucketAt(e.clientX);
      const end = hit || { i: start.i };
      const i0 = Math.min(start.i, end.i);
      const i1 = Math.max(start.i, end.i);
      const b = ev.chart.buckets;
      setZoom([b[i0].t0, b[i1].t1]);
    });
  }

  function setZoom(zoom) {
    ev.zoom = zoom;
    ev.shown = PAGE;
    render();
  }

  // --- leaderboard ---
  function boardRows(calls) {
    const by = new Map();
    for (const c of calls) {
      let r = by.get(c.model);
      if (!r) {
        r = { model: c.model, provider: c.provider, calls: 0, ok: 0, lat: [], tps: [], in_tokens: 0, out_tokens: 0, busy: 0, apps: new Set() };
        by.set(c.model, r);
      }
      r.calls += 1;
      r.in_tokens += c.in_tokens;
      r.out_tokens += c.out_tokens;
      r.busy += c.latency_ms;
      r.apps.add(c.app);
      if (c.status === 'ok') {
        r.ok += 1;
        r.lat.push(c.latency_ms);
        if (c.tps !== null) r.tps.push(c.tps);
      }
    }
    return Array.from(by.values()).map(r => {
      r.lat.sort((a, b) => a - b);
      r.tps.sort((a, b) => a - b);
      return {
        model: r.model,
        provider: r.provider,
        calls: r.calls,
        rate: r.calls ? r.ok / r.calls : 0,
        p50: percentile(r.lat, 50),
        p95: percentile(r.lat, 95),
        tps: percentile(r.tps, 50),
        in_tokens: r.in_tokens,
        out_tokens: r.out_tokens,
        busy: r.busy,
        apps: Array.from(r.apps).sort()
      };
    });
  }

  function renderBoard(base) {
    const box = document.getElementById('ev-board');
    if (!box) return;
    const rows = sortRows(boardRows(base.calls), ev.boardSort, (r, col) => r[col] ?? -1);
    if (!rows.length) {
      box.innerHTML = '<div class="ev-empty">No calls in this view.</div>';
      return;
    }
    const maxCalls = Math.max(...rows.map(r => r.calls));
    const head = (col, label, title) => {
      const active = ev.boardSort.col === col;
      const aria = active ? (ev.boardSort.asc ? 'ascending' : 'descending') : 'none';
      return `<th aria-sort="${aria}" class="${active ? (ev.boardSort.asc ? 'sorted-asc' : 'sorted-desc') : ''}">
        <button type="button" class="ev-th" data-board-sort="${col}" title="${esc(title || '')}">${label}<span class="sort-arrow"></span></button></th>`;
    };
    box.innerHTML = `
      <table class="table ev-board-table">
        <thead><tr>
          ${head('model', 'Model')}${head('calls', 'Calls')}${head('rate', 'OK %')}
          ${head('p50', 'p50', 'median latency of served calls')}${head('p95', 'p95')}
          ${head('tps', 'tok/s', 'median output tokens per second')}
          ${head('in_tokens', 'In')}${head('out_tokens', 'Out')}${head('busy', 'Time', 'sum of latency')}
        </tr></thead>
        <tbody>
          ${rows.map(r => `
            <tr class="ev-board-row ${ev.model === r.model ? 'picked' : ''}" data-pick-model="${esc(r.model)}" title="Apps: ${esc(r.apps.join(', '))}">
              <td class="font-mono ev-model-cell">${esc(r.model)}</td>
              <td><div class="ev-inline-bar"><i style="width:${((100 * r.calls) / maxCalls).toFixed(1)}%"></i><span>${r.calls}</span></div></td>
              <td><span class="ev-rate ${r.rate >= 0.95 ? 'good' : r.rate >= 0.8 ? 'warn' : 'bad'}">${Math.round(r.rate * 100)}%</span></td>
              <td>${fmtMs(r.p50)}</td>
              <td>${fmtMs(r.p95)}</td>
              <td>${r.tps === null ? '-' : Math.round(r.tps)}</td>
              <td>${fmtTokens(r.in_tokens)}</td>
              <td>${fmtTokens(r.out_tokens)}</td>
              <td>${fmtMs(r.busy)}</td>
            </tr>`).join('')}
        </tbody>
      </table>`;
  }

  // --- log table ---
  function typeBadge(item) {
    if (item.type === 'event') return `<span class="ev-badge ev-k-event" title="event">${esc(item.kind)}</span>`;
    return `<span class="ev-badge ev-s-${STATUS_ORDER.includes(item.status) ? item.status : 'error'}">${esc(item.status)}</span>`;
  }

  function latencyCell(item, maxLat) {
    if (item.type === 'event') return '<span class="text-muted">-</span>';
    const pct = maxLat ? Math.max(2, (100 * item.latency_ms) / maxLat) : 0;
    const tone = item.latency_ms < 3000 ? 'fast' : item.latency_ms < 15000 ? 'mid' : 'slow';
    return `<div class="ev-lat ev-lat-${tone}"><i style="width:${pct.toFixed(1)}%"></i><span>${fmtMs(item.latency_ms)}</span></div>`;
  }

  function tokensCell(item) {
    if (item.type === 'event') return '<span class="text-muted">-</span>';
    if (!item.in_tokens && !item.out_tokens) return '<span class="text-muted">-</span>';
    const est = item.estimated ? '<span class="ev-est" title="estimated: the vendor reported no usage">≈</span>' : '';
    const cached = item.cached_tokens ? `<span class="ev-cached" title="cached input tokens">⚡${fmtTokens(item.cached_tokens)}</span>` : '';
    return `${est}<span title="${item.in_tokens} in / ${item.out_tokens} out">${fmtTokens(item.in_tokens)} → <b>${fmtTokens(item.out_tokens)}</b></span>${cached}`;
  }

  function detailCell(item) {
    if (item.type === 'event') return `<span class="ev-msg" title="${esc(item.message)}">${esc(item.message)}</span>`;
    const bits = [];
    if (item.error_code) bits.push(`<span class="ev-code">${esc(item.error_code)}</span>`);
    if (item.stream) bits.push('<span class="ev-tag">stream</span>');
    if (item.type === 'chain') {
      bits.push(`<span class="ev-chain">${item.attempts.map(a =>
        `<i class="ev-s-${STATUS_ORDER.includes(a.status) ? a.status : 'error'}" title="#${a.attempt} ${esc(a.model)} ${esc(a.status)}"></i>`
      ).join('')}</span>`);
    }
    if (item.account) bits.push(`<span class="text-muted">${esc(item.provider || '')}${item.provider ? '/' : ''}${esc(item.account)}</span>`);
    return bits.join(' ');
  }

  function attemptCell(item) {
    if (item.type === 'event') return '';
    if (item.type === 'chain') return `<span class="ev-attempt multi" title="${item.attempts.length} attempts, probable chain">${item.attempts.length}×</span>`;
    return item.attempt > 1 ? `<span class="ev-attempt" title="attempt ${item.attempt}: an earlier candidate failed">#${item.attempt}</span>` : '<span class="text-muted">1</span>';
  }

  function renderLog(base) {
    const box = document.getElementById('events-list');
    if (!box) return;
    const rows = logRows(base);
    const countEl = document.getElementById('ev-rowcount');
    if (countEl) countEl.textContent = `${rows.length.toLocaleString()} row${rows.length === 1 ? '' : 's'}`;
    if (!rows.length) {
      box.innerHTML = `<div class="ev-empty">${ev.calls.length || ev.events.length ? 'Nothing matches these filters.' : 'No calls or events in this range.'}</div>`;
      return;
    }
    const visible = rows.slice(0, ev.shown);
    const maxLat = Math.max(1, ...visible.filter(r => r.type !== 'event').map(r => r.latency_ms));
    const head = (col, label) => {
      if (!col) return `<th>${label}</th>`;
      const active = ev.sort.col === col;
      const aria = active ? (ev.sort.asc ? 'ascending' : 'descending') : 'none';
      return `<th aria-sort="${aria}" class="${active ? (ev.sort.asc ? 'sorted-asc' : 'sorted-desc') : ''}">
        <button type="button" class="ev-th" data-sort="${col}">${label}<span class="sort-arrow"></span></button></th>`;
    };
    box.innerHTML = `
      <table class="table ev-table">
        <thead><tr>
          ${head('ts', 'Time')}${head('type', 'Type')}${head('app', 'App')}${head('model', 'Model')}
          ${head('attempt', 'Try')}${head('latency', 'Duration')}${head('tokens', 'Tokens in → out')}${head('tps', 'tok/s')}
          ${head(null, 'Details')}
        </tr></thead>
        <tbody>${visible.map(item => rowHtml(item, maxLat)).join('')}</tbody>
      </table>
      ${rows.length > ev.shown ? `<div class="ev-more"><button type="button" class="btn btn-sm" id="ev-more">Show ${Math.min(PAGE, rows.length - ev.shown)} more</button> <span class="text-muted">${ev.shown} of ${rows.length}</span></div>` : ''}`;
  }

  function rowHtml(item, maxLat) {
    const day = fmtDay(item.t);
    const open = ev.open.has(item.uid);
    const fresh = ev.fresh.has(item.uid) || (item.attempts && item.attempts.some(a => ev.fresh.has(a.uid)));
    const cls = ['ev-row', `ev-row-${item.type}`, open ? 'open' : '', fresh ? 'fresh' : '', isProblem(item) ? 'problem' : ''].join(' ');
    let html = `
      <tr class="${cls}" data-uid="${esc(item.uid)}" tabindex="0" aria-expanded="${open}">
        <td class="ev-time" title="${esc(new Date(item.t).toString())} · ${fmtAgo(item.t)}">
          <span class="ev-caret">▸</span>${day ? `<span class="ev-day">${esc(day)}</span> ` : ''}${fmtClock(item.t, true)}
        </td>
        <td>${typeBadge(item)}</td>
        <td>${item.app ? `<button type="button" class="cap-tag ev-link" data-filter-app="${esc(item.app)}" title="Show only ${esc(item.app)}">${esc(item.app)}</button>` : '<span class="text-muted">-</span>'}</td>
        <td class="font-mono">${item.model ? `<button type="button" class="ev-link ev-model" data-filter-model="${esc(item.model)}" title="Show only this model">${esc(item.model)}</button>` : '<span class="text-muted">-</span>'}</td>
        <td>${attemptCell(item)}</td>
        <td>${latencyCell(item, maxLat)}</td>
        <td class="ev-tokens">${tokensCell(item)}</td>
        <td>${item.tps ? Math.round(item.tps) : '<span class="text-muted">-</span>'}</td>
        <td class="ev-details">${detailCell(item)}</td>
      </tr>`;
    if (open) html += `<tr class="ev-detail-row"><td colspan="9">${detailHtml(item)}</td></tr>`;
    return html;
  }

  function related(item) {
    const lo = (item.startT ?? item.t) - 15e3;
    const hi = (item.endT ?? item.t) + 15e3;
    return ev.events.filter(e => e.uid !== item.uid && e.t >= lo && e.t <= hi &&
      ((item.model && e.model === item.model) || (item.app && e.app === item.app)))
      .sort((a, b) => b.t - a.t).slice(0, 8);
  }

  function nearbyAttempts(item) {
    if (item.type === 'event') return [];
    const lo = item.startT - 60e3;
    const hi = item.endT + 60e3;
    return ev.calls.filter(c => c.uid !== item.uid && c.app === item.app && c.endT >= lo && c.startT <= hi)
      .sort((a, b) => a.startT - b.startT).slice(0, 12);
  }

  function kv(label, value) {
    return `<div class="ev-kv"><span>${label}</span><b>${value}</b></div>`;
  }

  function detailHtml(item) {
    const parts = [];
    const started = item.type === 'event' ? null : item.startT;
    const facts = [
      kv('Recorded', `${esc(new Date(item.t).toISOString())} <span class="text-muted">(${fmtAgo(item.t)})</span>`)
    ];
    if (item.type === 'event') {
      facts.push(kv('Kind', esc(item.kind)));
      if (item.account) facts.push(kv('Account', esc(item.account)));
    } else {
      facts.push(kv('Started ≈', `${fmtClock(started, true)}`));
      facts.push(kv(item.type === 'chain' ? 'Wall time, first to last attempt' : 'Duration',
        `${fmtMs(item.latency_ms)} <span class="text-muted">(${item.latency_ms.toLocaleString()} ms)</span>`));
      facts.push(kv('Provider / account', `${esc(item.provider || '-')} / ${esc(item.account || '-')}`));
      facts.push(kv('Tokens', `in ${item.in_tokens.toLocaleString()} · out ${item.out_tokens.toLocaleString()} · cached ${item.cached_tokens.toLocaleString()}${item.estimated ? ' <span class="ev-est">estimated</span>' : ''}`));
      if (item.tps) facts.push(kv('Output speed', `${item.tps.toFixed(1)} tok/s`));
      facts.push(kv('Mode', item.stream ? 'stream' : 'sync / job'));
      if (item.error_code) facts.push(kv('Error code', `<span class="ev-code">${esc(item.error_code)}</span>`));
      if (item.type !== 'chain') facts.push(kv('Attempt', `#${item.attempt}`));
    }
    parts.push(`<div class="ev-facts">${facts.join('')}</div>`);
    if (item.type === 'event') {
      parts.push(`<pre class="ev-pre">${esc(item.message)}</pre>`);
    }
    if (item.type === 'chain') {
      parts.push(`<div class="ev-sub">Attempts of this request <span class="text-muted">(matched by app and attempt number)</span></div>`);
      parts.push(waterfall(item.attempts));
    } else if (item.type === 'call') {
      const near = nearbyAttempts(item);
      if (near.length) {
        parts.push(`<div class="ev-sub">Same app, ±60 s</div>`);
        parts.push(waterfall(near.concat([item]).sort((a, b) => a.startT - b.startT), item.uid));
      }
    }
    const rel = related(item);
    if (rel.length) {
      parts.push(`<div class="ev-sub">Events around it</div><ul class="ev-rel">${rel.map(e =>
        `<li><span class="ev-time-sm">${fmtClock(e.t, true)}</span> <span class="ev-badge ev-k-event">${esc(e.kind)}</span> ${esc(e.message)}</li>`
      ).join('')}</ul>`);
    }
    parts.push(`<div class="ev-actions">
      ${item.model ? `<button type="button" class="btn btn-sm" data-filter-model="${esc(item.model)}">Only this model</button>` : ''}
      ${item.app ? `<button type="button" class="btn btn-sm" data-filter-app="${esc(item.app)}">Only this app</button>` : ''}
      <button type="button" class="btn btn-sm" data-zoom-around="${item.t}">Zoom ±5 min</button>
      <button type="button" class="btn btn-sm" data-copy="${esc(item.uid)}">Copy JSON</button>
    </div>`);
    return `<div class="ev-detail">${parts.join('')}</div>`;
  }

  // a gantt strip: each attempt from its start to its end
  function waterfall(list, focus) {
    const spans = list.map(a => ({ a, s: a.startT, e: a.endT }));
    const t0 = Math.min(...spans.map(x => x.s));
    const t1 = Math.max(...spans.map(x => x.e), t0 + 1);
    return `<div class="ev-wf">${spans.map(({ a, s, e }) => {
      const left = (100 * (s - t0)) / (t1 - t0);
      const width = Math.max(0.6, (100 * (e - s)) / (t1 - t0));
      const st = STATUS_ORDER.includes(a.status) ? a.status : 'error';
      return `<div class="ev-wf-row ${a.uid === focus ? 'focus' : ''}">
        <span class="ev-wf-label font-mono" title="${esc(a.model)}">#${a.attempt} ${esc(a.model)}</span>
        <span class="ev-wf-track"><i class="ev-s-${st}" style="left:${left.toFixed(2)}%;width:${width.toFixed(2)}%"></i></span>
        <span class="ev-wf-meta">${esc(a.status)} · ${fmtMs(a.latency_ms)}${a.out_tokens ? ` · ${fmtTokens(a.out_tokens)} out` : ''}</span>
      </div>`;
    }).join('')}</div>`;
  }

  function findItem(uid) {
    const all = ev.calls.concat(ev.events);
    const hit = all.find(i => i.uid === uid);
    if (hit) return hit;
    if (uid.startsWith('r')) {
      const chain = buildChains(baseFiltered().calls).find(c => c.uid === uid);
      if (chain) return chain;
    }
    return null;
  }

  // --- export ---
  function exportRows(format) {
    const rows = logRows(baseFiltered()).flatMap(r => (r.attempts ? r.attempts : [r]));
    let blob;
    let name;
    const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
    if (format === 'json') {
      blob = new Blob([JSON.stringify(rows.map(r => ({ type: r.type, ...r.raw })), null, 2)], { type: 'application/json' });
      name = `llmhub-events-${stamp}.json`;
    } else {
      const cols = ['type', 'ts', 'app', 'model', 'provider', 'account', 'status', 'kind', 'attempt', 'latency_ms',
        'in_tokens', 'out_tokens', 'cached_tokens', 'total_tokens', 'estimated', 'stream', 'error_code', 'message'];
      const cell = v => {
        const s = v === null || v === undefined ? '' : String(v);
        return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
      };
      const lines = [cols.join(',')].concat(rows.map(r => cols.map(c => cell(c === 'type' ? r.type : r.raw[c])).join(',')));
      blob = new Blob([lines.join('\n')], { type: 'text/csv' });
      name = `llmhub-events-${stamp}.csv`;
    }
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => {
      URL.revokeObjectURL(a.href);
      a.remove();
    }, 500);
  }

  // --- interaction ---
  function bind() {
    if (ev.bound) return;
    ev.bound = true;
    const panel = document.querySelector('[data-panel="events"]');
    if (!panel) return;

    panel.addEventListener('click', (e) => {
      const t = e.target;
      const range = t.closest('[data-range]');
      if (range) {
        ev.range = range.dataset.range;
        ev.zoom = null;
        ev.seen = null;
        ev.shown = PAGE;
        persist();
        load();
        return;
      }
      const kind = t.closest('[data-kind]');
      if (kind) {
        ev.kind = kind.dataset.kind;
        ev.shown = PAGE;
        persist();
        render();
        return;
      }
      const sort = t.closest('[data-sort]');
      if (sort) {
        const col = sort.dataset.sort;
        ev.sort = ev.sort.col === col ? { col, asc: !ev.sort.asc } : { col, asc: ['app', 'model', 'type'].includes(col) };
        render();
        return;
      }
      const bsort = t.closest('[data-board-sort]');
      if (bsort) {
        const col = bsort.dataset.boardSort;
        ev.boardSort = ev.boardSort.col === col ? { col, asc: !ev.boardSort.asc } : { col, asc: col === 'model' };
        render();
        return;
      }
      const fApp = t.closest('[data-filter-app]');
      if (fApp) {
        e.stopPropagation();
        ev.app = ev.app === fApp.dataset.filterApp ? '' : fApp.dataset.filterApp;
        ev.shown = PAGE;
        render();
        return;
      }
      const fModel = t.closest('[data-filter-model], [data-pick-model]');
      if (fModel) {
        e.stopPropagation();
        const m = fModel.dataset.filterModel || fModel.dataset.pickModel;
        ev.model = ev.model === m ? '' : m;
        ev.shown = PAGE;
        render();
        return;
      }
      const clear = t.closest('[data-clear]');
      if (clear) {
        const k = clear.dataset.clear;
        ev[k] = '';
        if (k === 'search') document.getElementById('ev-search').value = '';
        render();
        return;
      }
      const zoomAround = t.closest('[data-zoom-around]');
      if (zoomAround) {
        const at = Number(zoomAround.dataset.zoomAround);
        setZoom([at - 5 * 60e3, at + 5 * 60e3]);
        return;
      }
      const copy = t.closest('[data-copy]');
      if (copy) {
        const item = findItem(copy.dataset.copy);
        const payload = item && item.attempts ? item.attempts.map(a => a.raw) : item && item.raw;
        navigator.clipboard?.writeText(JSON.stringify(payload, null, 2)).then(
          () => window.LLMHub.notify('Copied to clipboard', 'success'),
          () => window.LLMHub.notify('Clipboard is not available here', 'error')
        );
        return;
      }
      if (t.closest('#ev-more')) {
        ev.shown += PAGE;
        render();
        return;
      }
      if (t.closest('#ev-zoom-reset')) {
        setZoom(null);
        return;
      }
      if (t.closest('#ev-live')) {
        ev.live = !ev.live;
        persist();
        schedule();
        render();
        if (ev.live) load(true);
        return;
      }
      if (t.closest('#ev-refresh')) {
        load();
        return;
      }
      const exp = t.closest('[data-export]');
      if (exp) {
        exportRows(exp.dataset.export);
        return;
      }
      const row = t.closest('tr.ev-row');
      if (row && !t.closest('button')) toggleRow(row.dataset.uid);
    });

    panel.addEventListener('keydown', (e) => {
      const row = e.target.closest && e.target.closest('tr.ev-row');
      if (!row) return;
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        toggleRow(row.dataset.uid, true);
      } else if (e.key === 'ArrowDown' || e.key === 'j') {
        e.preventDefault();
        focusSibling(row, 1);
      } else if (e.key === 'ArrowUp' || e.key === 'k') {
        e.preventDefault();
        focusSibling(row, -1);
      }
    });

    document.getElementById('ev-app')?.addEventListener('change', (e) => {
      ev.app = e.target.value;
      ev.shown = PAGE;
      render();
    });
    document.getElementById('ev-model')?.addEventListener('change', (e) => {
      ev.model = e.target.value;
      ev.shown = PAGE;
      render();
    });
    let typing = null;
    document.getElementById('ev-search')?.addEventListener('input', (e) => {
      clearTimeout(typing);
      typing = setTimeout(() => {
        ev.search = e.target.value;
        ev.shown = PAGE;
        render();
      }, 150);
    });
    document.getElementById('ev-chains')?.addEventListener('change', (e) => {
      ev.chains = e.target.checked;
      persist();
      render();
    });
    document.addEventListener('keydown', (e) => {
      if (window.LLMHub.activeTab() !== 'events') return;
      const typingInField = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || '');
      if (e.key === '/' && !typingInField) {
        e.preventDefault();
        document.getElementById('ev-search')?.focus();
      } else if (e.key === 'Escape') {
        if (ev.zoom) setZoom(null);
        else if (typingInField) document.activeElement.blur();
      }
    });
    document.addEventListener('visibilitychange', schedule);
    let resizeTimer = null;
    window.addEventListener('resize', () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        if (window.LLMHub.activeTab() === 'events') renderChart();
      }, 120);
    });
    bindChart();
  }

  function toggleRow(uid, keepFocus) {
    if (ev.open.has(uid)) ev.open.delete(uid);
    else ev.open.add(uid);
    renderLog(baseFiltered());
    if (keepFocus) document.querySelector(`tr.ev-row[data-uid="${CSS.escape(uid)}"]`)?.focus();
  }

  function focusSibling(row, dir) {
    let next = dir > 0 ? row.nextElementSibling : row.previousElementSibling;
    while (next && !next.classList.contains('ev-row')) next = dir > 0 ? next.nextElementSibling : next.previousElementSibling;
    next?.focus();
  }

  // --- entry points used by v2.js ---
  function show() {
    bind();
    load();
    schedule();
  }

  function hide() {
    clearInterval(ev.timer);
    ev.timer = null;
  }

  window.LLMHubEvents = { show, hide, load, _state: ev, _buildChains: buildChains, _shapeCall: shapeCall };
  window.loadEvents = () => load();
})();
