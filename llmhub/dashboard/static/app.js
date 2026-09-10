(function () {
  "use strict";

  var TOKEN_KEY = "llmhub.token";
  var EVENTS_FILTER_KEY = "llmhub.eventsFilters";
  var EVENTS_RANGES = ["1h", "24h", "7d", "all"];
  var DEFAULT_EVENTS_FILTERS = { kinds: [], app: "", model: "", q: "", range: "24h" };
  var MODELS_SORT_KEY = "llmhub.modelsSort";
  // pre-v0.15 shape: a single mode string picked from a <select>, kept here only so
  // loadModelsSort can recognize and drop a value written by that old build
  var MODELS_SORT_VALUES = ["registry", "requests", "tokens", "last_used", "status"];
  // header click order is asc -> desc -> registry; count-like columns (a bigger number is the
  // more interesting one) start at desc instead, everything else starts at asc
  var MODELS_SORT_DEFAULT_DIR = {
    provider: "asc", model: "asc", account: "asc", caps: "asc", status: "asc",
    today: "desc", out_tokens: "desc", last_used: "asc", apps: "desc",
    hourly: "asc", daily: "asc", monthly: "asc", latency: "desc", last_error: "asc"
  };
  var MODELS_FILTERS_KEY = "llmhub.modelsFilters";
  var DEFAULT_MODELS_FILTERS = {
    q: "", statuses: [], provider: "", caps: [], liveOnly: false, usedToday: false, onlyProblems: false
  };
  var STATUS_SORT_RANK = { down: 0, cooldown: 1, exhausted: 2, unknown: 3, disabled: 4, ok: 5 };
  var PROMO_FILTERS_KEY = "llmhub.promosFilters";
  var PROMO_SORT_KEY = "llmhub.promosSort";
  var DEFAULT_PROMO_FILTERS = { statuses: [], source: "", q: "", hideDone: true };
  var PROMO_STATUS_RANK = { new: 0, known: 1, rejected: 2, used: 3, expired: 4 };
  var REJECT_REASON_MIN = 8;
  var PROMO_SORT_DEFAULT_DIR = { provider: "asc", status: "asc", expires: "asc", found: "desc", source: "asc", key: "asc" };
  var POLL_MS = 10000;
  // the live strip is the only thing on the page that has to keep up with a running call;
  // it drops back to the status cadence once nothing has been in flight for a minute
  var LIVE_WINDOW_MIN = 15;
  var LIVE_FAST_MS = 2000;
  var LIVE_SLOW_MS = 10000;
  var LIVE_IDLE_MS = 60000;
  var SVG_NS = "http://www.w3.org/2000/svg";

  var state = {
    tab: "models",
    status: null,
    usageWindow: "24h",
    usageMetric: "total_tokens",
    queueState: "",
    eventsFilters: loadEventsFilters(),
    eventsRaw: [],
    modelsSort: loadModelsSort(),
    modelsFilters: loadModelsFilters(),
    modelRowNodes: {},
    promosRaw: [],
    promosFilters: loadPromosFilters(),
    promosSort: loadPromosSort(),
    known: null,
    add: null,
    scoutActive: false,
    live: null,
    liveModelMap: null,
    liveBusyAt: 0
  };
  var scoutPollTimer = null;
  var livePollTimer = null;

  function loadModelsSort() {
    try {
      var raw = localStorage.getItem(MODELS_SORT_KEY);
      if (!raw) return null;
      if (MODELS_SORT_VALUES.indexOf(raw) >= 0) return null; // pre-v0.15 mode string
      var p = JSON.parse(raw);
      if (p && typeof p.key === "string" && (p.dir === "asc" || p.dir === "desc")) {
        return { key: p.key, dir: p.dir };
      }
    } catch (e) {}
    return null;
  }

  function saveModelsSort() {
    try {
      if (state.modelsSort) localStorage.setItem(MODELS_SORT_KEY, JSON.stringify(state.modelsSort));
      else localStorage.removeItem(MODELS_SORT_KEY);
    } catch (e) {}
  }

  function loadModelsFilters() {
    try {
      var raw = localStorage.getItem(MODELS_FILTERS_KEY);
      if (!raw) return clone(DEFAULT_MODELS_FILTERS);
      var p = JSON.parse(raw);
      return {
        q: typeof p.q === "string" ? p.q : "",
        statuses: Array.isArray(p.statuses) ? p.statuses.map(String) : [],
        provider: typeof p.provider === "string" ? p.provider : "",
        caps: Array.isArray(p.caps) ? p.caps.map(String) : [],
        liveOnly: typeof p.liveOnly === "boolean" ? p.liveOnly : false,
        usedToday: typeof p.usedToday === "boolean" ? p.usedToday : false,
        onlyProblems: typeof p.onlyProblems === "boolean" ? p.onlyProblems : false
      };
    } catch (e) {
      return clone(DEFAULT_MODELS_FILTERS);
    }
  }

  function saveModelsFilters() {
    try { localStorage.setItem(MODELS_FILTERS_KEY, JSON.stringify(state.modelsFilters)); } catch (e) {}
  }

  function loadPromosFilters() {
    try {
      var raw = localStorage.getItem(PROMO_FILTERS_KEY);
      if (!raw) return clone(DEFAULT_PROMO_FILTERS);
      var p = JSON.parse(raw);
      return {
        statuses: Array.isArray(p.statuses) ? p.statuses.map(String) : [],
        source: typeof p.source === "string" ? p.source : "",
        q: typeof p.q === "string" ? p.q : "",
        hideDone: typeof p.hideDone === "boolean" ? p.hideDone : true
      };
    } catch (e) {
      return clone(DEFAULT_PROMO_FILTERS);
    }
  }

  function savePromosFilters() {
    try { localStorage.setItem(PROMO_FILTERS_KEY, JSON.stringify(state.promosFilters)); } catch (e) {}
  }

  function loadPromosSort() {
    try {
      var raw = localStorage.getItem(PROMO_SORT_KEY);
      var p = raw ? JSON.parse(raw) : null;
      if (p && PROMO_SORT_DEFAULT_DIR[p.key] && (p.dir === "asc" || p.dir === "desc")) {
        return { key: p.key, dir: p.dir };
      }
    } catch (e) {}
    return { key: "found", dir: "desc" };
  }

  function savePromosSort() {
    try { localStorage.setItem(PROMO_SORT_KEY, JSON.stringify(state.promosSort)); } catch (e) {}
  }

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }

  function svgEl(tag, attrs) {
    var n = document.createElementNS(SVG_NS, tag);
    Object.keys(attrs || {}).forEach(function (k) { n.setAttribute(k, String(attrs[k])); });
    return n;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
    return node;
  }

  function getToken() {
    try { return localStorage.getItem(TOKEN_KEY) || ""; } catch (e) { return ""; }
  }

  function setToken(v) {
    try { if (v) localStorage.setItem(TOKEN_KEY, v); else localStorage.removeItem(TOKEN_KEY); } catch (e) {}
  }

  function clone(o) { return JSON.parse(JSON.stringify(o)); }

  function loadEventsFilters() {
    try {
      var raw = localStorage.getItem(EVENTS_FILTER_KEY);
      if (!raw) return clone(DEFAULT_EVENTS_FILTERS);
      var p = JSON.parse(raw);
      return {
        kinds: Array.isArray(p.kinds) ? p.kinds.map(String) : [],
        app: typeof p.app === "string" ? p.app : "",
        model: typeof p.model === "string" ? p.model : "",
        q: typeof p.q === "string" ? p.q : "",
        range: EVENTS_RANGES.indexOf(p.range) >= 0 ? p.range : "24h"
      };
    } catch (e) {
      return clone(DEFAULT_EVENTS_FILTERS);
    }
  }

  function saveEventsFilters() {
    try { localStorage.setItem(EVENTS_FILTER_KEY, JSON.stringify(state.eventsFilters)); } catch (e) {}
  }

  function isLoopbackHost() {
    var h = location.hostname;
    return h === "" || h === "localhost" || h === "127.0.0.1" || h === "::1" || h === "[::1]" ||
      /\.localhost$/.test(h) || /^127\./.test(h);
  }

  function toast(msg, kind) {
    if (!msg) return;
    var host = $("#toasts");
    if (!host) return;
    var t = el("div", "toast" + (kind ? " toast-" + kind : ""));
    t.appendChild(el("span", null, String(msg)));
    var x = el("button", "toast-x", "x");
    x.type = "button";
    x.setAttribute("aria-label", "dismiss");
    x.addEventListener("click", function () { if (t.parentNode) t.parentNode.removeChild(t); });
    t.appendChild(x);
    host.appendChild(t);
    setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, 9000);
  }

  /* in-page modal replacing window.confirm/prompt.
     resolves null on cancel, {value, checked} on confirm. */
  function ask(opts) {
    opts = opts || {};
    return new Promise(function (resolve) {
      var back = $("#modal");
      var box = clear($("#modal-box"));
      box.classList.remove("modal-wide");
      var input = null;
      var check = null;
      var settled = false;

      var h = el("h3", null, opts.title || "Confirm");
      h.id = "modal-title";
      box.appendChild(h);
      if (opts.body) box.appendChild(el("p", "modal-body", opts.body));

      if (opts.input) {
        var f = el("label", "field");
        if (opts.input.label) f.appendChild(el("span", "field-label", opts.input.label));
        input = el("input");
        input.type = opts.input.type || "text";
        input.id = "modal-input";
        input.autocomplete = "off";
        input.placeholder = opts.input.placeholder || "";
        input.value = opts.input.value || "";
        f.appendChild(input);
        box.appendChild(f);
      }

      if (opts.checkbox) {
        var cl = el("label", "modal-check");
        check = el("input");
        check.type = "checkbox";
        check.id = "modal-check";
        cl.appendChild(check);
        cl.appendChild(el("span", null, opts.checkbox));
        box.appendChild(cl);
      }

      var foot = el("div", "modal-foot");
      var cancel = el("button", "btn", opts.cancel || "Cancel");
      cancel.type = "button";
      cancel.id = "modal-cancel";
      var ok = el("button", "btn btn-primary", opts.confirm || "OK");
      ok.type = "button";
      ok.id = "modal-ok";
      foot.appendChild(cancel);
      foot.appendChild(ok);
      box.appendChild(foot);

      function finish(v) {
        if (settled) return;
        settled = true;
        if (input) input.value = "";
        document.removeEventListener("keydown", onKey, true);
        back.hidden = true;
        clear($("#modal-box"));
        resolve(v);
      }
      function onKey(e) {
        if (e.key === "Escape") { e.preventDefault(); finish(null); }
        else if (e.key === "Enter" && input) { e.preventDefault(); confirmNow(); }
      }
      function confirmNow() {
        finish({ value: input ? input.value : "", checked: check ? check.checked : false });
      }

      cancel.addEventListener("click", function () { finish(null); });
      ok.addEventListener("click", confirmNow);
      back.addEventListener("click", function onBack(e) {
        if (e.target === back) { back.removeEventListener("click", onBack); finish(null); }
      });
      document.addEventListener("keydown", onKey, true);

      back.hidden = false;
      (input || ok).focus();
    });
  }

  function askToken(msg) {
    return ask({
      title: "Hub token",
      body: msg || "Mutating actions need the hub token off loopback.",
      input: { label: "LLMHUB_TOKEN", type: "password", value: getToken(), placeholder: "token" },
      confirm: "Save"
    }).then(function (r) {
      if (!r) return null;
      var v = String(r.value || "").trim();
      setToken(v);
      updateTokenButton();
      return v;
    });
  }

  function updateTokenButton() {
    $("#token-btn").textContent = getToken() ? "Token set" : "Token";
  }

  function api(path, opts) {
    opts = opts || {};
    var headers = opts.headers || {};
    var tok = getToken();
    if (tok) headers["Authorization"] = "Bearer " + tok;
    if (opts.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";

    function done(res) {
      if (!res.ok) {
        var err = new Error(res.status + " " + res.statusText);
        err.status = res.status;
        return res.text().then(function (body) {
          err.body = body;
          throw err;
        }, function () { throw err; });
      }
      var ct = res.headers.get("content-type") || "";
      return ct.indexOf("json") >= 0 ? res.json() : res.text();
    }

    return fetch(path, {
      method: opts.method || "GET",
      headers: headers,
      body: opts.body,
      cache: "no-store"
    }).then(function (res) {
      if (res.status === 401 && !opts._retried) {
        return askToken("Token required for this action.").then(function (t) {
          if (!t) return done(res);
          opts._retried = true;
          return api(path, opts);
        });
      }
      return done(res);
    });
  }

  function errJson(e) {
    if (!e || !e.body) return null;
    try { return JSON.parse(e.body); } catch (x) { return null; }
  }

  function errText(e) {
    var j = errJson(e);
    if (j) {
      var m = j.error;
      if (m && typeof m === "object" && m.message) return String(m.message);
      if (typeof m === "string") return m;
      if (j.detail) return typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
      if (j.message) return String(j.message);
      return JSON.stringify(j).slice(0, 400);
    }
    if (e && e.body) return String(e.body).slice(0, 400);
    return e && e.message ? e.message : "request failed";
  }

  function notice(msg) {
    var n = $("#notice");
    if (!msg) { n.hidden = true; n.textContent = ""; return; }
    n.hidden = false;
    n.textContent = msg;
  }

  function rowsOf(payload) {
    if (!payload) return [];
    if (Array.isArray(payload)) return payload;
    var keys = ["rows", "items", "data", "jobs", "events", "promos", "apps", "bans", "accounts", "providers", "points", "buckets", "series"];
    for (var i = 0; i < keys.length; i++) {
      if (Array.isArray(payload[keys[i]])) return payload[keys[i]];
    }
    return [];
  }

  function rowKey(r) {
    var v = r.key || r.app || r.model || r.account || r.day || r.date || r.bucket || r.ts || r.name;
    return v === undefined || v === null ? "-" : String(v);
  }

  function num(v) { return typeof v === "number" && isFinite(v) ? v : 0; }
  function tokensIn(r) { return num(r.in_tokens !== undefined ? r.in_tokens : r.tokens_in); }
  function tokensOut(r) { return num(r.out_tokens !== undefined ? r.out_tokens : r.tokens_out); }

  function tokensTotal(r) {
    if (r.total_tokens !== undefined) return num(r.total_tokens);
    if (r.tokens !== undefined) return num(r.tokens);
    return tokensIn(r) + tokensOut(r);
  }

  function requests(r) { return num(r.requests !== undefined ? r.requests : r.count); }
  function errors(r) { return num(r.errors !== undefined ? r.errors : r.error_count); }
  function metricOf(r) { return state.usageMetric === "requests" ? requests(r) : tokensTotal(r); }

  function fmtNum(v) {
    v = num(v);
    var a = Math.abs(v);
    if (a >= 1e9) return (v / 1e9).toFixed(a >= 1e10 ? 0 : 1) + "G";
    if (a >= 1e6) return (v / 1e6).toFixed(a >= 1e7 ? 0 : 1) + "M";
    if (a >= 1e4) return Math.round(v / 1e3) + "k";
    if (a >= 1e3) return (v / 1e3).toFixed(1) + "k";
    return String(Math.round(v));
  }

  function fmtInt(v) { return num(v).toLocaleString("en-US"); }

  function parseTs(s) {
    if (!s) return null;
    var d = new Date(s);
    return isNaN(d.getTime()) ? null : d;
  }

  function pad2(n) { return String(n).length < 2 ? "0" + n : String(n); }

  function fmtTime(s) {
    var d = parseTs(s);
    if (!d) return "-";
    var hh = pad2(d.getHours()) + ":" + pad2(d.getMinutes());
    if (d.toDateString() === new Date().toDateString()) return hh;
    return pad2(d.getMonth() + 1) + "-" + pad2(d.getDate()) + " " + hh;
  }

  function fmtDelta(iso) {
    var d = parseTs(iso);
    if (!d) return "";
    var ms = d.getTime() - Date.now();
    var neg = ms < 0;
    var s = Math.floor(Math.abs(ms) / 1000);
    var h = Math.floor(s / 3600);
    var m = Math.floor((s % 3600) / 60);
    var sec = s % 60;
    var out;
    if (h >= 24) out = Math.floor(h / 24) + "d" + (h % 24) + "h";
    else if (h > 0) out = h + "h" + pad2(m) + "m";
    else if (m > 0) out = m + "m" + pad2(sec) + "s";
    else out = sec + "s";
    return neg ? "-" + out : out;
  }

  function fmtAgo(iso) {
    var d = parseTs(iso);
    if (!d) return "-";
    var ms = Math.max(0, Date.now() - d.getTime());
    var mins = Math.floor(ms / 60000);
    if (mins < 1) return "just now";
    if (mins < 60) return mins + " min ago";
    var hours = Math.floor(mins / 60);
    if (hours < 24) return hours + " h ago";
    var days = Math.floor(hours / 24);
    return days + " d ago";
  }

  function sinceISO(w) {
    var ms = w === "7d" ? 7 * 864e5 : w === "30d" ? 30 * 864e5 : 864e5;
    return new Date(Date.now() - ms).toISOString();
  }

  function statusClass(s) {
    return ["ok", "exhausted", "cooldown", "down", "disabled"].indexOf(s) >= 0
      ? "pill pill-" + s
      : "pill pill-unknown";
  }

  function emptyRow(span, text) {
    var tr = el("tr", "empty");
    var td = el("td", null, text);
    td.colSpan = span;
    tr.appendChild(td);
    return tr;
  }

  function bars(container, rows, opts) {
    clear(container);
    if (!rows.length) {
      container.appendChild(el("p", "empty", "no data"));
      return;
    }
    var max = 0;
    rows.forEach(function (r) { max = Math.max(max, opts.value(r)); });
    if (max <= 0) max = 1;
    rows.forEach(function (r) {
      var row = el("div", "bar-row");
      var label = el("div", "bar-label", opts.label(r));
      label.title = opts.label(r);
      var track = el("div", "bar-track");
      var fill = el("div", "bar-fill");
      fill.style.width = Math.max(1, (opts.value(r) / max) * 100).toFixed(2) + "%";
      track.appendChild(fill);
      row.appendChild(label);
      row.appendChild(track);
      row.appendChild(el("div", "bar-value", opts.text(r)));
      container.appendChild(row);
    });
  }

  var LIVE_JOB_STATES = ["queued", "waiting_quota", "running"];

  // the counter says what is still moving; done/failed/expired/cancelled rows are history
  function liveDepth(queue) {
    if (!queue) return 0;
    if (queue.live !== undefined && queue.live !== null) return num(queue.live);
    var depth = queue.depth_by_state || {};
    var total = 0;
    LIVE_JOB_STATES.forEach(function (k) { total += num(depth[k]); });
    return total;
  }

  function renderSummary() {
    var st = state.status;
    if (!st) return;
    var models = st.models || [];
    var ok = 0;
    models.forEach(function (m) { if (m.status === "ok") ok++; });
    $("#sum-models").textContent = "models " + ok + " ok / " + models.length;
    $("#sum-queue").textContent = "queue " + liveDepth(st.queue);
    $("#sum-age").textContent = "updated " + (st.generated_at ? fmtTime(st.generated_at) : "-");
  }

  // the metric a window is metered in decides what the cell means: "12/20 req" is calls left,
  // a bare "12/20" is tokens. Same number, different thing to do about it.
  function observedMetric(observedEntry) {
    if (!observedEntry) return null;
    var names = ["out_tokens", "total_tokens", "in_tokens", "requests"];
    for (var i = 0; i < names.length; i++) {
      var value = observedEntry[names[i]];
      if (value !== undefined && value !== null) return { metric: names[i], value: value };
    }
    return null;
  }

  function windowCell(w, observedEntry) {
    var td = el("td", "win");
    var hasDeclared = w && w.limit !== undefined && w.limit !== null;
    if (!hasDeclared) {
      var obs = observedMetric(observedEntry);
      if (obs) {
        var obsUsed = num(obs.value);
        var obsText = el("div", "win-text win-observed-only", fmtNum(obsUsed) + "/observed");
        obsText.title = fmtInt(obsUsed) + " " + obs.metric + " observed" +
          (observedEntry.observed_at ? " - learned from a 429 at " + observedEntry.observed_at : "");
        td.appendChild(obsText);
        return td;
      }
      td.appendChild(el("span", "dim", "-"));
      return td;
    }
    var used = num(w.used);
    var limit = num(w.limit);
    var pct = limit > 0 ? Math.min(100, (used / limit) * 100) : 0;
    var unit = w.unit || w.metric;
    var isRequests = unit === "requests";
    var isObservedLimit = w.limit_source === "observed";
    var text = el("div", "win-text" + (isObservedLimit ? " win-limit-observed" : ""),
      fmtNum(used) + "/" + fmtNum(limit) + (isRequests ? " req" : ""));
    var obsAt = observedEntry && observedEntry.observed_at;
    text.title = fmtInt(used) + " / " + fmtInt(limit) + (unit ? " " + unit : "") +
      (isObservedLimit ? " - learned from a 429 at " + (obsAt || "unknown time") : "");
    var meter = el("div", "meter" + (pct >= 90 ? " hot" : ""));
    var span = el("span");
    span.style.width = pct.toFixed(1) + "%";
    meter.appendChild(span);
    td.appendChild(text);
    td.appendChild(meter);
    if (w.resets_at) {
      var reset = el("div", "win-reset");
      reset.setAttribute("data-reset", w.resets_at);
      reset.title = w.resets_at;
      reset.textContent = "resets " + fmtDelta(w.resets_at);
      td.appendChild(reset);
    }
    return td;
  }

  function actionButton(label, path, opts) {
    var b = el("button", "btn btn-mini", label);
    b.type = "button";
    b.addEventListener("click", function () {
      b.disabled = true;
      api(path, { method: (opts && opts.method) || "POST", body: (opts && opts.body) || "{}" })
        .then(function () {
          notice(null);
          return refreshStatus();
        })
        .then(function () { if (opts && opts.after) opts.after(); })
        .catch(function (e) {
          notice(label + " failed: " + e.message + (e.body ? " " + String(e.body).slice(0, 200) : ""));
        })
        .then(function () { b.disabled = false; });
    });
    return b;
  }

  function usageToday(m) { return (m && m.usage_today) || null; }
  function usageRequests(m) { var u = usageToday(m); return u ? num(u.requests) : 0; }
  function usageOutTokens(m) { var u = usageToday(m); return u ? num(u.out_tokens) : 0; }
  function usageErrors(m) { var u = usageToday(m); return u ? num(u.errors) : 0; }
  function usageLastUsedAt(m) { var u = usageToday(m); return u ? u.last_used_at : null; }
  function usageLastUsedTs(m) {
    var d = parseTs(usageLastUsedAt(m));
    return d ? d.getTime() : -1;
  }

  function statusRank(m) {
    var s = m.disabled === true ? "disabled" : (m.status || "unknown");
    return STATUS_SORT_RANK[s] !== undefined ? STATUS_SORT_RANK[s] : STATUS_SORT_RANK.unknown;
  }

  function modelStatusOf(m) { return m.disabled === true ? "disabled" : (m.status || "unknown"); }

  function capsSortText(m) { return (m.caps || []).slice().sort().join(","); }

  // used for the Hourly/Daily/Monthly sort keys: how much room is left, as a fraction of the
  // window's own limit, so a 90%-full hourly window and a 90%-full monthly one compare equal
  function remainingFraction(w) {
    if (!w || !(num(w.limit) > 0)) return null;
    return Math.max(0, (num(w.limit) - num(w.used)) / num(w.limit));
  }

  // a null value (no window declared, no latency measured, never errored) always sorts to the
  // bottom regardless of direction - it is not "smaller" or "bigger", it is "not applicable"
  function compareNullableNumeric(a, b, dirMul) {
    if (a === null && b === null) return 0;
    if (a === null) return 1;
    if (b === null) return -1;
    return dirMul * (a - b);
  }

  // the (account, model) key the live map and the models table both join on
  function liveJoinKey(account, model) { return String(account || "") + "|" + String(model || ""); }

  function modelsLiveEntry(m) {
    // api/live's calls carry the full "provider/model" key (same text the live strip's chips
    // show), not the bare model name status rows use in m.model - join on m.key instead
    var key = liveJoinKey(m.account, m.key || ((m.provider || "") + "/" + (m.model || "")));
    if (state.liveModelMap) return state.liveModelMap[key] || { count: 0, calls: [] };
    // api/live has never answered - all we have is the count api/status carried along
    return { count: num(m.in_flight), calls: null };
  }

  function compareModels(a, b, key, dirMul) {
    if (key === "provider") return dirMul * String(a.provider || "").localeCompare(String(b.provider || ""));
    if (key === "account") return dirMul * String(a.account || "").localeCompare(String(b.account || ""));
    if (key === "model") {
      return dirMul * String(a.model || a.key || "").localeCompare(String(b.model || b.key || ""));
    }
    if (key === "caps") {
      var diff = dirMul * ((a.caps || []).length - (b.caps || []).length);
      return diff !== 0 ? diff : dirMul * capsSortText(a).localeCompare(capsSortText(b));
    }
    if (key === "status") return dirMul * (statusRank(a) - statusRank(b));
    if (key === "today") return dirMul * (usageRequests(a) - usageRequests(b));
    if (key === "out_tokens") return dirMul * (usageOutTokens(a) - usageOutTokens(b));
    if (key === "last_used") return dirMul * (usageLastUsedTs(a) - usageLastUsedTs(b));
    if (key === "apps") {
      var la = modelsLiveEntry(a), lb = modelsLiveEntry(b);
      var d1 = dirMul * (la.count - lb.count);
      return d1 !== 0 ? d1 : dirMul * ((a.apps_15m || []).length - (b.apps_15m || []).length);
    }
    if (key === "hourly" || key === "daily" || key === "monthly") {
      var fa = remainingFraction((a.windows || {})[key]);
      var fb = remainingFraction((b.windows || {})[key]);
      return compareNullableNumeric(fa, fb, dirMul);
    }
    if (key === "latency") {
      var va = a.avg_latency_ms === undefined || a.avg_latency_ms === null ? null : num(a.avg_latency_ms);
      var vb = b.avg_latency_ms === undefined || b.avg_latency_ms === null ? null : num(b.avg_latency_ms);
      return compareNullableNumeric(va, vb, dirMul);
    }
    if (key === "last_error") {
      var ea = parseTs(a.last_error_at), eb = parseTs(b.last_error_at);
      return compareNullableNumeric(ea ? ea.getTime() : null, eb ? eb.getTime() : null, dirMul);
    }
    return 0;
  }

  function sortModels(models) {
    var s = state.modelsSort;
    if (!s) return models;
    var dirMul = s.dir === "desc" ? -1 : 1;
    var arr = models.slice();
    arr.sort(function (a, b) { return compareModels(a, b, s.key, dirMul); });
    return arr;
  }

  function modelsSearchHay(m) {
    return [m.provider, m.model, m.account, m.key, m.last_error]
      .map(function (v) { return v ? String(v) : ""; })
      .join(" ").toLowerCase();
  }

  function filteredModels() {
    var all = (state.status && state.status.models) || [];
    var f = state.modelsFilters;
    var q = f.q.trim().toLowerCase();
    return all.filter(function (m) {
      if (f.onlyProblems && m.status === "ok") return false;
      if (f.statuses.length && f.statuses.indexOf(modelStatusOf(m)) < 0) return false;
      if (f.provider && String(m.provider || "") !== f.provider) return false;
      if (f.caps.length) {
        var caps = m.caps || [];
        if (!f.caps.some(function (c) { return caps.indexOf(c) >= 0; })) return false;
      }
      if (f.liveOnly && modelsLiveEntry(m).count <= 0) return false;
      if (f.usedToday && usageRequests(m) <= 0) return false;
      if (q && modelsSearchHay(m).indexOf(q) < 0) return false;
      return true;
    });
  }

  function modelsFiltersActive() {
    var f = state.modelsFilters;
    return !!(f.q || f.statuses.length || f.provider || f.caps.length ||
      f.liveOnly || f.usedToday || f.onlyProblems);
  }

  function orderModelStatusValues(values) {
    return values.slice().sort(function (a, b) {
      var ra = STATUS_SORT_RANK[a] !== undefined ? STATUS_SORT_RANK[a] : 99;
      var rb = STATUS_SORT_RANK[b] !== undefined ? STATUS_SORT_RANK[b] : 99;
      return ra !== rb ? ra - rb : a.localeCompare(b);
    });
  }

  function renderModelsStatusChips(statuses) {
    var host = clear($("#models-status-chips"));
    state.modelsFilters.statuses = state.modelsFilters.statuses.filter(function (s) {
      return statuses.indexOf(s) >= 0;
    });
    if (!statuses.length) {
      host.appendChild(el("span", "dim", "no statuses"));
      return;
    }
    statuses.forEach(function (s) {
      var b = el("button", "chip-toggle", s);
      b.type = "button";
      b.setAttribute("aria-pressed", state.modelsFilters.statuses.indexOf(s) >= 0 ? "true" : "false");
      b.addEventListener("click", function () {
        var idx = state.modelsFilters.statuses.indexOf(s);
        if (idx >= 0) state.modelsFilters.statuses.splice(idx, 1);
        else state.modelsFilters.statuses.push(s);
        saveModelsFilters();
        renderModels();
      });
      host.appendChild(b);
    });
  }

  function renderModelsCapsChips(caps) {
    var host = clear($("#models-caps-chips"));
    state.modelsFilters.caps = state.modelsFilters.caps.filter(function (c) { return caps.indexOf(c) >= 0; });
    if (!caps.length) {
      host.appendChild(el("span", "dim", "no caps"));
      return;
    }
    caps.forEach(function (c) {
      var b = el("button", "chip-toggle", c);
      b.type = "button";
      b.setAttribute("aria-pressed", state.modelsFilters.caps.indexOf(c) >= 0 ? "true" : "false");
      b.addEventListener("click", function () {
        var idx = state.modelsFilters.caps.indexOf(c);
        if (idx >= 0) state.modelsFilters.caps.splice(idx, 1);
        else state.modelsFilters.caps.push(c);
        saveModelsFilters();
        renderModels();
      });
      host.appendChild(b);
    });
  }

  function fillModelsProviderSelect(values) {
    var sel = $("#models-provider-filter");
    var want = state.modelsFilters.provider;
    clear(sel);
    var optAll = el("option", null, "all providers");
    optAll.value = "";
    sel.appendChild(optAll);
    values.forEach(function (v) {
      var o = el("option", null, v);
      o.value = v;
      sel.appendChild(o);
    });
    sel.value = values.indexOf(want) >= 0 ? want : "";
    if (want && sel.value !== want) {
      state.modelsFilters.provider = "";
      saveModelsFilters();
    }
  }

  function populateModelsFilterOptions(models) {
    renderModelsStatusChips(orderModelStatusValues(uniqueSorted(models.map(modelStatusOf))));
    renderModelsCapsChips(uniqueSorted([].concat.apply([], models.map(function (m) { return m.caps || []; }))));
    fillModelsProviderSelect(uniqueSorted(models.map(function (m) { return m.provider; })));
  }

  function syncModelsFilterBar() {
    $("#models-search").value = state.modelsFilters.q;
    $("#models-live-only").checked = state.modelsFilters.liveOnly;
    $("#models-used-today").checked = state.modelsFilters.usedToday;
    $("#models-only-problems").checked = state.modelsFilters.onlyProblems;
    $("#models-clear").hidden = !modelsFiltersActive();
  }

  function syncModelsSortHeaders() {
    $$("#models-table thead th[data-sort-key]").forEach(function (th) {
      var key = th.getAttribute("data-sort-key");
      var active = state.modelsSort && state.modelsSort.key === key;
      th.setAttribute("data-sort-active", active ? "true" : "false");
      th.setAttribute("data-sort-dir", active ? state.modelsSort.dir : "");
      th.setAttribute("aria-sort", active ? (state.modelsSort.dir === "asc" ? "ascending" : "descending") : "none");
    });
  }

  // one chip per in-flight call on this (account, model) pair, named for the app that is
  // waiting on it (the model itself is already the row we are in) - shares the live strip's
  // chip class and its client-side elapsed ticker (tickLive walks [data-elapsed-base] anywhere
  // in the document, this table included)
  function appLiveChip(call) {
    var waiting = call.state === "waiting";
    var chip = el("span", "live-chip " + (waiting ? "live-chip-waiting" : "live-chip-on"));
    chip.appendChild(document.createTextNode(call.app));
    if (waiting) chip.appendChild(el("span", "live-wait", "waiting"));
    chip.appendChild(elapsedNode(call));
    if (num(call.attempt) > 1) chip.appendChild(el("span", "live-attempt", "#" + call.attempt));
    chip.title = callTitle(call, call.app + " calling " + call.model);
    if (call.call_id) chip.appendChild(killButton(call, call.app + " on " + call.model));
    return chip;
  }

  // renders the Apps cell for one row and reports whether it is live, so the caller can flag
  // the row - called both from the full render and, per row, from the 2 s live tick
  function renderAppsCell(td, m, tr) {
    clear(td);
    var live = modelsLiveEntry(m);
    var wrap = el("div", "apps-cell");
    var hasLive = false;
    if (live.calls) {
      live.calls.forEach(function (c) { wrap.appendChild(appLiveChip(c)); hasLive = true; });
    } else if (live.count > 0) {
      var cnt = el("span", "live-chip live-chip-on", live.count + " live");
      cnt.title = live.count + " call(s) in flight on this account and model (app names unavailable - " +
        "the live feed is not answering)";
      wrap.appendChild(cnt);
      hasLive = true;
    }
    (m.apps_15m || []).forEach(function (a) { wrap.appendChild(el("span", "app-tag", a)); });
    if (hasLive || wrap.childNodes.length) td.appendChild(wrap);
    else td.appendChild(el("span", "dim", "-"));
    if (tr) tr.classList.toggle("row-live", hasLive);
  }

  // called on every api/live poll (every 2 s): touches only the Apps cell and the row-live
  // class of rows already on screen - the rest of the row (usage, windows, status) only
  // changes on the api/status poll, which does a full renderModels() instead
  function updateModelsLiveCells() {
    Object.keys(state.modelRowNodes).forEach(function (key) {
      var node = state.modelRowNodes[key];
      renderAppsCell(node.td, node.m, node.tr);
    });
  }

  function renderModels() {
    syncModelsSortHeaders();
    var all = (state.status && state.status.models) || [];
    populateModelsFilterOptions(all);
    syncModelsFilterBar();
    var models = sortModels(filteredModels());
    var body = clear($("#models-body"));
    state.modelRowNodes = {};
    if (!models.length) {
      body.appendChild(emptyRow(15, all.length ? "no models match the filters" : "no models"));
    } else {
      models.forEach(function (m) {
        var key = m.key || ((m.provider || "?") + "/" + (m.model || "?"));
        var uReq = usageRequests(m);
        var tr = el("tr", uReq > 0 ? "row-used" : null);
        tr.appendChild(el("td", "mono", m.provider || "-"));
        var tdModel = el("td", "mono", m.model || key);
        tdModel.title = key;
        tr.appendChild(tdModel);
        tr.appendChild(el("td", "mono dim", m.account || "-"));

        var tdCaps = el("td");
        var caps = el("div", "caps");
        (m.caps || []).forEach(function (c) { caps.appendChild(el("span", "cap", c)); });
        tdCaps.appendChild(caps);
        tr.appendChild(tdCaps);

        var shown = modelStatusOf(m);
        var tdStatus = el("td", "td-fit");
        var pill = el("span", statusClass(shown), shown);
        pill.title = (m.reason ? "reason " + m.reason + ", " : "") +
          (m.last_ok_at ? "last ok " + fmtTime(m.last_ok_at) : "never ok");
        tdStatus.appendChild(pill);
        tr.appendChild(tdStatus);

        var tdToday = el("td", "num");
        if (usageToday(m) && uReq > 0) {
          var uErr = usageErrors(m);
          tdToday.appendChild(document.createTextNode(fmtInt(uReq)));
          if (uErr > 0) tdToday.appendChild(el("span", "err", " / " + fmtInt(uErr) + " err"));
        } else {
          tdToday.appendChild(el("span", "dim", "-"));
        }
        tr.appendChild(tdToday);

        var tdOut = el("td", "num");
        var uOut = usageOutTokens(m);
        if (usageToday(m) && uOut > 0) {
          var outSpan = el("span", null, fmtNum(uOut));
          outSpan.title = fmtInt(uOut) + " out tokens today";
          tdOut.appendChild(outSpan);
        } else {
          tdOut.appendChild(el("span", "dim", "-"));
        }
        tr.appendChild(tdOut);

        var lastIso = usageLastUsedAt(m);
        var tdLast = el("td", "mono dim");
        if (lastIso) {
          tdLast.textContent = fmtAgo(lastIso);
          tdLast.title = lastIso;
        } else {
          tdLast.textContent = "-";
        }
        tr.appendChild(tdLast);

        var tdApps = el("td");
        tr.appendChild(tdApps);

        var w = m.windows || {};
        var obs = m.observed || {};
        tr.appendChild(windowCell(w.hourly, obs.hourly));
        tr.appendChild(windowCell(w.daily, obs.daily));
        tr.appendChild(windowCell(w.monthly, obs.monthly));

        var lat = m.avg_latency_ms;
        tr.appendChild(el("td", "num", lat === undefined || lat === null ? "-" : Math.round(lat) + " ms"));

        var tdErr = el("td");
        if (m.last_error) {
          var e = el("span", "err", m.last_error);
          e.title = m.last_error + (m.last_error_at ? " (" + fmtAgo(m.last_error_at) + ")" : "");
          tdErr.appendChild(e);
        } else {
          tdErr.appendChild(el("span", "dim", "-"));
        }
        tr.appendChild(tdErr);

        var tdAct = el("td");
        var acts = el("div", "actions");
        var disabled = m.status === "disabled" || m.disabled === true;
        acts.appendChild(actionButton("forgive", "api/models/" + key + "/forgive"));
        acts.appendChild(actionButton(disabled ? "enable" : "disable",
          "api/models/" + key + "/" + (disabled ? "enable" : "disable")));
        tdAct.appendChild(acts);
        tr.appendChild(tdAct);

        renderAppsCell(tdApps, m, tr);
        state.modelRowNodes[key] = { tr: tr, td: tdApps, m: m };
        body.appendChild(tr);
      });
    }
    $("#models-count").textContent = models.length + " of " + all.length + " models";
    tickCountdowns();
  }

  function tickCountdowns() {
    $$("[data-reset]").forEach(function (n) {
      n.textContent = "resets " + fmtDelta(n.getAttribute("data-reset"));
    });
    $$("[data-eta]").forEach(function (n) {
      n.textContent = fmtTime(n.getAttribute("data-eta")) + " (" + fmtDelta(n.getAttribute("data-eta")) + ")";
    });
  }

  function shortModel(key) {
    var s = String(key || "");
    var i = s.indexOf("/");
    return i >= 0 ? s.slice(i + 1) : s;
  }

  function fmtElapsed(s) {
    var v = num(s);
    if (v < 60) return Math.round(v) + "s";
    return Math.floor(v / 60) + "m" + pad2(Math.round(v % 60)) + "s";
  }

  // the server sends the elapsed seconds it measured; the page keeps counting from there so a
  // chip ticks between polls instead of jumping every two seconds
  function elapsedNode(call) {
    var n = el("b", "live-elapsed", fmtElapsed(call.elapsed_s));
    n.setAttribute("data-elapsed-base", String(num(call.elapsed_s)));
    n.setAttribute("data-elapsed-at", String(Date.now()));
    return n;
  }

  // POST a cancel and refresh the strip from the answer rather than waiting for the next poll
  function cancelCalls(path, body, label) {
    return api(path, { method: "POST", body: body || "{}" })
      .then(function (r) {
        toast("cancelled " + num(r.count) + " call(s) on " + label, "ok");
        return loadLive();
      })
      .catch(function (e) {
        // 404 means the call is past cancelling - it ended between the poll and the click, or
        // it is a stream whose body is already on its way out - not a failure
        if (e.status === 404) {
          toast("nothing to cancel on " + label + ": the call is already finished", "warn");
          return loadLive();
        }
        toast("cancel failed: " + errText(e), "bad");
      });
  }

  // the x on a live chip. Cancelling one call needs no confirmation - the chip names it, and
  // the call is the owner's to end; killing a whole app's worth does (see killAppButton)
  function killButton(call, label) {
    var b = el("button", "live-kill", "x");
    b.type = "button";
    b.title = "cancel this call";
    b.setAttribute("aria-label", "cancel " + label);
    b.addEventListener("click", function (ev) {
      ev.stopPropagation();
      b.disabled = true;
      cancelCalls("api/live/" + call.call_id + "/cancel", "{}", label).then(function () {
        b.disabled = false;
      });
    });
    return b;
  }

  function callTitle(call, subject) {
    return subject + " on " + call.account + ", " + call.kind +
      (call.state === "waiting" ? ", waiting for a free slot" : "") +
      (num(call.attempt) > 1 ? ", attempt " + call.attempt : "") +
      (call.job_id ? ", job " + call.job_id : "");
  }

  // a waiting call holds no slot and no vendor: muted, and labelled, so a queue behind a stuck
  // backend does not read as a model that is working
  function liveChip(call) {
    var waiting = call.state === "waiting";
    var chip = el("span", "live-chip " + (waiting ? "live-chip-waiting" : "live-chip-on"));
    chip.appendChild(document.createTextNode(shortModel(call.model)));
    if (waiting) chip.appendChild(el("span", "live-wait", "waiting"));
    chip.appendChild(elapsedNode(call));
    if (num(call.attempt) > 1) chip.appendChild(el("span", "live-attempt", "#" + call.attempt));
    chip.title = callTitle(call, call.model);
    if (call.call_id) chip.appendChild(killButton(call, call.model));
    return chip;
  }

  function killAppButton(app, count) {
    var b = el("button", "live-kill live-kill-all", "kill " + count);
    b.type = "button";
    b.title = "cancel every call " + app + " has in flight";
    b.addEventListener("click", function () {
      ask({
        title: "Cancel " + count + " calls?",
        body: "Every call " + app + " has in flight stops: the vendor call is cancelled, the " +
          "slot frees up, and a client still waiting gets a 503.",
        confirm: "Cancel calls"
      }).then(function (r) {
        if (!r) return null;
        b.disabled = true;
        return cancelCalls("api/live/cancel", JSON.stringify({ app: app }), app).then(function () {
          b.disabled = false;
        });
      });
    });
    return b;
  }

  function recentChip(row) {
    var chip = el("span", "live-chip live-chip-recent", shortModel(row.model));
    chip.appendChild(el("span", "live-calls", "x" + fmtInt(row.calls)));
    chip.title = row.model + ": " + fmtInt(row.calls) + " calls, " +
      fmtInt(row.out_tokens) + " out tokens in the last " + LIVE_WINDOW_MIN + " min";
    return chip;
  }

  function renderLive() {
    var box = $("#live");
    var host = clear($("#live-rows"));
    var data = state.live;
    var rows = (data && data.apps) || [];
    if (!rows.length) {
      box.hidden = true;
      return;
    }
    box.hidden = false;
    rows.forEach(function (r) {
      var row = el("div", "live-row");
      row.appendChild(el("div", "live-app", r.app));
      var chips = el("div", "live-chips");
      var busy = {};
      var live = r.in_flight || [];
      live.forEach(function (c) {
        busy[c.model] = true;
        chips.appendChild(liveChip(c));
      });
      (r.recent || []).forEach(function (m) {
        if (busy[m.model]) return;
        chips.appendChild(recentChip(m));
      });
      // one x per chip covers a single call; the row button is for an app that has run away
      if (live.length > 1) chips.appendChild(killAppButton(r.app, live.length));
      row.appendChild(chips);
      host.appendChild(row);
    });
    var total = num(data.in_flight_total);
    $("#live-count").textContent = total ? total + " in flight" : "idle";
  }

  function tickLive() {
    var now = Date.now();
    $$("[data-elapsed-base]").forEach(function (n) {
      var base = parseFloat(n.getAttribute("data-elapsed-base")) || 0;
      var at = parseFloat(n.getAttribute("data-elapsed-at")) || now;
      n.textContent = fmtElapsed(base + (now - at) / 1000);
    });
  }

  // (account, model) -> { count, calls[] }, rebuilt on every live poll. The Models table's Apps
  // cells and its "in use now" filter/sort all read this instead of hitting api/live again.
  function buildLiveModelMap(data) {
    var map = {};
    ((data && data.apps) || []).forEach(function (r) {
      (r.in_flight || []).forEach(function (c) {
        var key = liveJoinKey(c.account, c.model);
        var entry = map[key];
        if (!entry) { entry = { count: 0, calls: [] }; map[key] = entry; }
        entry.count++;
        entry.calls.push({
          app: r.app, model: c.model, account: c.account, call_id: c.call_id, state: c.state,
          elapsed_s: c.elapsed_s, attempt: c.attempt, kind: c.kind, job_id: c.job_id
        });
      });
    });
    return map;
  }

  function loadLive() {
    return api("api/live?window_min=" + LIVE_WINDOW_MIN).then(function (data) {
      state.live = data;
      state.liveModelMap = buildLiveModelMap(data);
      if (num(data.in_flight_total) > 0) state.liveBusyAt = Date.now();
      renderLive();
      updateModelsLiveCells();
      return data;
    }).catch(function () {
      // the status poll already reports a hub that is not answering; a dead strip is enough
      return null;
    });
  }

  function livePollMs() {
    if (state.live && num(state.live.in_flight_total) > 0) return LIVE_FAST_MS;
    return Date.now() - state.liveBusyAt < LIVE_IDLE_MS ? LIVE_FAST_MS : LIVE_SLOW_MS;
  }

  function scheduleLive() {
    if (livePollTimer) clearTimeout(livePollTimer);
    livePollTimer = setTimeout(pollLive, livePollMs());
  }

  function pollLive() {
    if (document.visibilityState !== "visible") {
      scheduleLive();
      return;
    }
    loadLive().then(scheduleLive, scheduleLive);
  }

  function refreshStatus() {
    return api("api/status").then(function (data) {
      state.status = data;
      notice(null);
      renderSummary();
      if (state.tab === "models") renderModels();
      if (state.tab === "queue") renderQueueDepth();
      return data;
    }).catch(function (e) {
      notice("api/status failed: " + e.message);
    });
  }

  function loadUsage() {
    var since = sinceISO(state.usageWindow);
    var bucket = state.usageWindow === "24h" ? "hour" : "day";
    var q = "since=" + encodeURIComponent(since);
    return Promise.all([
      api("api/usage?" + q + "&group_by=app").catch(function () { return []; }),
      api("api/usage?" + q + "&group_by=model").catch(function () { return []; }),
      api("api/usage?" + q + "&group_by=day").catch(function () { return []; }),
      api("api/usage/timeseries?bucket=" + bucket + "&" + q).catch(function () { return []; })
    ]).then(function (res) {
      var byApp = rowsOf(res[0]);
      var byModel = rowsOf(res[1]);
      var byDay = rowsOf(res[2]);
      var series = rowsOf(res[3]);

      var t = { req: 0, tin: 0, tout: 0, err: 0 };
      byApp.forEach(function (r) {
        t.req += requests(r);
        t.tin += tokensIn(r);
        t.tout += tokensOut(r);
        t.err += errors(r);
      });
      $("#usage-total-req").textContent = fmtInt(t.req);
      $("#usage-total-in").textContent = fmtNum(t.tin);
      $("#usage-total-out").textContent = fmtNum(t.tout);
      $("#usage-total-err").textContent = fmtInt(t.err);

      var sortDesc = function (a, b) { return metricOf(b) - metricOf(a); };
      var valueText = function (r) {
        return state.usageMetric === "requests"
          ? fmtInt(requests(r)) + " req"
          : fmtNum(tokensTotal(r)) + " tok";
      };
      bars($("#usage-by-app"), byApp.slice().sort(sortDesc), { label: rowKey, value: metricOf, text: valueText });
      bars($("#usage-by-model"), byModel.slice().sort(sortDesc), { label: rowKey, value: metricOf, text: valueText });
      bars($("#usage-by-day"), byDay.slice().sort(function (a, b) {
        return String(rowKey(a)).localeCompare(String(rowKey(b)));
      }), {
        label: function (r) { return String(rowKey(r)).slice(0, 10); },
        value: metricOf,
        text: valueText
      });
      renderStrip(series);
    });
  }

  function renderStrip(points) {
    var host = clear($("#usage-strip"));
    if (!points.length) {
      host.appendChild(el("p", "empty", "no data"));
      return;
    }
    var W = 600, H = 130, pad = 4;
    var colW = (W - pad * 2) / points.length;
    var barW = Math.max(1, colW * 0.72);
    var max = 0, maxErr = 0;
    points.forEach(function (p) {
      max = Math.max(max, tokensIn(p) + tokensOut(p));
      maxErr = Math.max(maxErr, errors(p));
    });
    if (max <= 0) max = 1;

    var svg = svgEl("svg", {
      viewBox: "0 0 " + W + " " + H,
      preserveAspectRatio: "none",
      role: "img",
      "aria-label": "usage timeseries"
    });
    points.forEach(function (p, i) {
      var x = pad + i * colW + (colW - barW) / 2;
      var tin = tokensIn(p), tout = tokensOut(p);
      var hIn = (tin / max) * (H - 12);
      var hOut = (tout / max) * (H - 12);
      var yOut = H - hOut;
      var yIn = yOut - hIn;
      var g = svgEl("g", {});
      var title = svgEl("title", {});
      title.textContent = (p.ts || p.bucket || p.day || "") +
        " in " + fmtNum(tin) + " out " + fmtNum(tout) + " err " + errors(p);
      g.appendChild(title);
      g.appendChild(svgEl("rect", {
        x: x.toFixed(2), y: yIn.toFixed(2), width: barW.toFixed(2),
        height: Math.max(0, hIn).toFixed(2), fill: "var(--bar)"
      }));
      g.appendChild(svgEl("rect", {
        x: x.toFixed(2), y: yOut.toFixed(2), width: barW.toFixed(2),
        height: Math.max(0, hOut).toFixed(2), fill: "var(--bar-2)"
      }));
      if (errors(p) > 0) {
        var eh = Math.max(2, (errors(p) / Math.max(1, maxErr)) * 8);
        g.appendChild(svgEl("rect", {
          x: x.toFixed(2), y: (yIn - eh - 2).toFixed(2), width: barW.toFixed(2),
          height: eh.toFixed(2), fill: "var(--bar-3)"
        }));
      }
      svg.appendChild(g);
    });
    host.appendChild(svg);

    var scale = el("div", "strip-legend");
    scale.style.display = "flex";
    scale.appendChild(el("span", "dim", String(points[0].ts || points[0].bucket || points[0].day || "").slice(0, 16)));
    var right = el("span", "dim", "peak " + fmtNum(max) + " tok");
    right.style.marginLeft = "auto";
    scale.appendChild(right);
    host.appendChild(scale);
  }

  function card(host, label, value, title) {
    var box = el("div", "card");
    box.appendChild(el("div", "card-label", label));
    box.appendChild(el("div", "card-value num", value));
    if (title) box.title = title;
    host.appendChild(box);
  }

  function renderQueueDepth() {
    var host = clear($("#queue-depth"));
    var queue = (state.status && state.status.queue) || {};
    var depth = queue.depth_by_state || {};
    var keys = Object.keys(depth);
    if (!keys.length) {
      host.appendChild(el("p", "empty", "queue empty"));
      return;
    }
    card(host, "live", fmtInt(liveDepth(queue)), "queued + waiting_quota + running");
    keys.forEach(function (k) { card(host, k, fmtInt(depth[k])); });
    if (queue.oldest_queued_at) card(host, "oldest queued", fmtTime(queue.oldest_queued_at), queue.oldest_queued_at);
    if (queue.expired_last_24h !== undefined && queue.expired_last_24h !== null) {
      card(host, "expired 24h", fmtInt(queue.expired_last_24h));
    }
  }

  function loadQueue() {
    renderQueueDepth();
    var jobsUrl = "api/jobs" + (state.queueState ? "?state=" + encodeURIComponent(state.queueState) : "");
    return Promise.all([
      api("api/apps").catch(function () { return []; }),
      api(jobsUrl).catch(function (e) { notice("api/jobs failed: " + e.message); return []; })
    ]).then(function (res) {
      renderApps(rowsOf(res[0]));
      renderJobs(rowsOf(res[1]));
    });
  }

  /* models this app told the hub never to route it to again. The count is on the row, the
     reasons come from api/apps/{app}/bans only when the owner opens them */
  function bansCell(name, count) {
    var td = el("td", "num");
    if (!count) {
      td.appendChild(el("span", "dim", "0"));
      return td;
    }
    var btn = el("button", "chip-toggle", String(count));
    btn.type = "button";
    btn.title = "models " + name + " banned for itself";
    btn.setAttribute("aria-expanded", "false");
    btn.addEventListener("click", function () { toggleAppBans(td.parentNode, name, btn); });
    td.appendChild(btn);
    return td;
  }

  function toggleAppBans(tr, name, btn) {
    var next = tr.nextSibling;
    if (next && next.className === "app-bans") {
      next.parentNode.removeChild(next);
      btn.setAttribute("aria-expanded", "false");
      return;
    }
    btn.setAttribute("aria-expanded", "true");
    var row = el("tr", "app-bans");
    var td = el("td");
    td.colSpan = 9;
    td.appendChild(el("div", "dim", "loading bans"));
    row.appendChild(td);
    tr.parentNode.insertBefore(row, tr.nextSibling);
    api("api/apps/" + encodeURIComponent(name) + "/bans")
      .then(function (payload) { renderAppBans(td, name, rowsOf(payload)); })
      .catch(function (e) {
        clear(td).appendChild(el("div", "dim", "bans unavailable: " + e.message));
      });
  }

  function renderAppBans(td, name, bans) {
    clear(td);
    if (!bans.length) {
      td.appendChild(el("div", "dim", "no bans"));
      return;
    }
    var list = el("div", "ban-list");
    bans.forEach(function (b) {
      var line = el("div", "ban-line");
      line.appendChild(el("span", "mono", b.model));
      line.appendChild(el("span", "ban-reason", b.reason || "-"));
      line.appendChild(el("span", "mono dim", fmtTime(b.created_at)));
      line.appendChild(actionButton("unban",
        "api/apps/" + encodeURIComponent(name) + "/bans/" + encodeURI(b.model),
        { method: "DELETE", after: loadQueue }));
      list.appendChild(line);
    });
    td.appendChild(list);
  }

  function renderApps(apps) {
    var body = clear($("#apps-body"));
    var byApp = (state.status && state.status.queue && state.status.queue.by_app) || {};
    if (!apps.length) {
      apps = Object.keys(byApp).map(function (k) { return { app: k, queued: byApp[k] }; });
    }
    if (!apps.length) {
      body.appendChild(emptyRow(9, "no apps"));
      return;
    }
    apps.forEach(function (a) {
      var name = a.app || a.name || a.key || "-";
      var tr = el("tr");
      tr.appendChild(el("td", "mono", name));
      var queued = a.queued !== undefined ? a.queued : byApp[name];
      tr.appendChild(el("td", "num", queued === undefined ? "-" : fmtInt(queued)));
      tr.appendChild(el("td", "num", a.running === undefined ? "-" : fmtInt(a.running)));
      var cap = el("td", "num", a.cap === undefined || a.cap === null ? "-" : fmtInt(a.cap));
      cap.title = "running jobs this app may hold right now (fair share of the worker pool)";
      tr.appendChild(cap);
      tr.appendChild(el("td", "num", fmtInt(requests(a))));
      tr.appendChild(el("td", "num", fmtNum(tokensTotal(a))));
      tr.appendChild(bansCell(name, num(a.bans)));
      var paused = a.paused === true;
      var tdP = el("td");
      tdP.appendChild(el("span", paused ? "pill pill-disabled" : "pill pill-ok", paused ? "paused" : "active"));
      tr.appendChild(tdP);
      var tdAct = el("td");
      var acts = el("div", "actions");
      acts.appendChild(actionButton(paused ? "resume" : "pause",
        "api/apps/" + encodeURIComponent(name) + "/" + (paused ? "resume" : "pause"),
        { after: loadQueue }));
      tdAct.appendChild(acts);
      tr.appendChild(tdAct);
      body.appendChild(tr);
    });
  }

  function renderJobs(jobs) {
    var body = clear($("#jobs-body"));
    if (!jobs.length) {
      body.appendChild(emptyRow(8, "no jobs"));
      return;
    }
    jobs.forEach(function (j) {
      var id = j.id !== undefined ? j.id : j.job_id;
      var s = j.state || j.status || "unknown";
      var tr = el("tr");
      tr.appendChild(el("td", "mono", id));
      tr.appendChild(el("td", "mono", j.app || "-"));
      tr.appendChild(el("td", "mono", j.model || j.alias || "-"));
      var tdS = el("td");
      tdS.appendChild(el("span",
        s === "waiting_quota" ? "pill pill-exhausted"
          : s === "failed" ? "pill pill-down"
            : s === "expired" ? "pill pill-disabled"
              : s === "done" ? "pill pill-ok" : "pill pill-unknown", s));
      tr.appendChild(tdS);
      tr.appendChild(el("td", "num", j.priority === undefined ? "-" : j.priority));
      tr.appendChild(el("td", "mono dim", fmtTime(j.created || j.created_at)));
      var tdN = el("td", "mono dim");
      if (j.next_window_at) {
        tdN.setAttribute("data-eta", j.next_window_at);
        tdN.title = j.next_window_at;
        tdN.textContent = fmtTime(j.next_window_at);
      } else {
        tdN.textContent = "-";
      }
      tr.appendChild(tdN);
      var tdAct = el("td");
      if (["done", "cancelled", "canceled", "failed", "expired"].indexOf(String(s)) >= 0) {
        tdAct.appendChild(el("span", "dim", "-"));
      } else {
        var acts = el("div", "actions");
        acts.appendChild(cancelButton(id));
        tdAct.appendChild(acts);
      }
      tr.appendChild(tdAct);
      body.appendChild(tr);
    });
    tickCountdowns();
  }

  function cancelButton(id) {
    var b = el("button", "btn btn-mini", "cancel");
    b.type = "button";
    b.addEventListener("click", function () {
      b.disabled = true;
      api("jobs/" + encodeURIComponent(id), { method: "DELETE" })
        .catch(function (e) {
          if (e.status !== 404 && e.status !== 405) throw e;
          return api("api/jobs/" + encodeURIComponent(id), { method: "DELETE" });
        })
        .then(function () { notice(null); return loadQueue(); })
        .catch(function (e) { notice("cancel failed: " + e.message); })
        .then(function () { b.disabled = false; });
    });
    return b;
  }

  function enc(v) { return encodeURIComponent(String(v)); }

  function modelId(m) {
    if (m === null || m === undefined) return "";
    return typeof m === "string" ? m : String(m.id || m.model || m.key || "");
  }

  function hasAllowance(m) { return !!(m && typeof m === "object" && m.free && m.free.allowance); }

  function modelWindowText(m) {
    if (!m || typeof m === "string") return "";
    var f = m.free;
    if (f === null || f === undefined) return "paid";
    var keys = Object.keys(f);
    if (!keys.length) return "free, limits unknown";
    return keys.map(function (k) {
      var w = f[k] || {};
      var parts = Object.keys(w).filter(function (x) {
        return x.indexOf("tokens") >= 0 || x === "requests";
      }).map(function (x) { return fmtNum(w[x]) + " " + x; });
      return parts.length ? k + " " + parts.join(", ") : k;
    }).join(" | ");
  }

  function loadAccounts() {
    return Promise.all([
      api("api/registry").catch(function () { return null; }),
      knownProviders()
    ]).then(function (res) {
      var providers = registryToAccounts(res[0]);
      if (providers.length) {
        renderAccounts(providers);
        return null;
      }
      var inStatus = state.status && state.status.accounts;
      if (Array.isArray(inStatus) && inStatus.length) {
        renderAccounts(normalizeAccounts(inStatus));
        return null;
      }
      return api("api/accounts")
        .then(function (data) { renderAccounts(normalizeAccounts(data)); })
        .catch(function () { renderAccounts(accountsFromStatus()); });
    });
  }

  function registryToAccounts(data) {
    var providers = (data && data.providers) || null;
    if (!providers) return [];
    if (Array.isArray(providers)) return normalizeAccounts(providers);
    return Object.keys(providers).map(function (name) {
      var p = providers[name] || {};
      return {
        provider: name,
        kind: p.kind,
        base_url: p.base_url,
        docs_url: p.docs_url || p.docs,
        accounts: (p.accounts || []).map(function (a) {
          return {
            id: a.id || a.account,
            api_key_env: a.api_key_env,
            key_present: a.key_present,
            env_file: a.env_file,
            models: p.models || []
          };
        })
      };
    });
  }

  function normalizeAccounts(data) {
    var rows = rowsOf(data);
    if (!rows.length) return accountsFromStatus();
    if (rows[0] && rows[0].accounts) return rows;
    var byProvider = {};
    rows.forEach(function (a) {
      var p = a.provider || "-";
      byProvider[p] = byProvider[p] || { provider: p, kind: a.kind, base_url: a.base_url, accounts: [] };
      byProvider[p].accounts.push(a);
    });
    return Object.keys(byProvider).map(function (k) { return byProvider[k]; });
  }

  function accountsFromStatus() {
    var models = (state.status && state.status.models) || [];
    var providers = {};
    models.forEach(function (m) {
      var p = m.provider || "-";
      var a = m.account || "-";
      providers[p] = providers[p] || { provider: p, accounts: {} };
      providers[p].accounts[a] = providers[p].accounts[a] || { id: a, key_present: m.key_present, models: [] };
      providers[p].accounts[a].models.push({ key: m.key, model: m.model, status: m.status, caps: m.caps });
    });
    return Object.keys(providers).map(function (p) {
      return {
        provider: p,
        accounts: Object.keys(providers[p].accounts).map(function (a) { return providers[p].accounts[a]; })
      };
    });
  }

  function renderAccounts(providers) {
    var host = clear($("#accounts-body"));
    if (!providers.length) {
      host.appendChild(el("p", "empty", "no accounts"));
      return;
    }
    var statusByKey = {};
    ((state.status && state.status.models) || []).forEach(function (m) { statusByKey[m.key] = m; });

    providers.forEach(function (p) {
      var name = p.provider || "-";
      var tpl = templateFor(name);
      var block = el("div", "provider-block");
      block.setAttribute("data-provider", name);
      var head = el("div", "provider-head");
      head.appendChild(el("span", "provider-name", name));
      if (p.kind) head.appendChild(el("span", "cap", p.kind));
      if (p.base_url) head.appendChild(el("span", "dim mono", p.base_url));
      var docs = p.docs_url || (tpl && tpl.docs_url);
      if (docs) {
        var a = el("a", "docs-link", "docs");
        a.href = docs;
        a.title = docs;
        a.target = "_blank";
        a.rel = "noreferrer noopener";
        head.appendChild(a);
      }
      block.appendChild(head);

      (p.accounts || []).forEach(function (acc) {
        block.appendChild(accountBlock(p, acc, statusByKey));
      });
      host.appendChild(block);
    });
  }

  function accountBlock(p, a, statusByKey) {
    var provider = p.provider || "-";
    var accId = a.id || a.account || "-";
    var ab = el("div", "account-block");
    ab.setAttribute("data-account", provider + "/" + accId);

    var ah = el("div", "account-head");
    ah.appendChild(el("span", "account-id", accId));
    var present = a.key_present;
    ah.appendChild(el("span",
      present === false ? "pill pill-down" : present === true ? "pill pill-ok" : "pill pill-unknown",
      present === false ? "no key" : present === true ? "key present" : "key unknown"));
    if (a.api_key_env) ah.appendChild(el("span", "cap", a.api_key_env));
    ab.appendChild(ah);

    var meta = el("div", "account-meta");
    var envFile = el("span", "mono", "env file " + (a.env_file || "-"));
    envFile.title = a.env_file || "";
    meta.appendChild(envFile);
    ab.appendChild(meta);

    var list = el("div", "model-list");
    var models = a.models || [];
    if (!models.length) list.appendChild(el("span", "dim", "no models"));
    models.forEach(function (m) {
      var isStr = typeof m === "string";
      var mname = isStr ? m : (m.model || m.id || m.key);
      var key = isStr ? provider + "/" + m : (m.key || provider + "/" + mname);
      var known = statusByKey[key] || (isStr ? {} : m);
      var shown = known.disabled === true ? "disabled" : (known.status || "unknown");
      var line = el("div", "model-line");
      line.appendChild(el("span", null, mname));
      line.appendChild(el("span", statusClass(shown), shown));
      (known.caps || m.caps || []).forEach(function (c) { line.appendChild(el("span", "cap", c)); });
      list.appendChild(line);
    });
    ab.appendChild(list);

    var detail = el("div", "account-detail");
    var acts = el("div", "account-actions");

    var modelSel = null;
    var ids = models.map(modelId).filter(Boolean);
    if (ids.length > 1) {
      modelSel = el("select");
      modelSel.setAttribute("aria-label", "model to test");
      ids.forEach(function (id) {
        var o = el("option", null, id);
        o.value = id;
        modelSel.appendChild(o);
      });
    }

    var testBtn = el("button", "btn btn-mini", "Test");
    testBtn.type = "button";
    testBtn.addEventListener("click", function () {
      runTest(provider, accId, modelSel ? modelSel.value : (ids[0] || ""), detail, false, testBtn);
    });
    acts.appendChild(testBtn);
    if (modelSel) acts.appendChild(modelSel);

    var rotateBtn = el("button", "btn btn-mini", "Rotate key");
    rotateBtn.type = "button";
    rotateBtn.addEventListener("click", function () { rotateForm(provider, accId, detail); });
    acts.appendChild(rotateBtn);

    var discBtn = el("button", "btn btn-mini", "Discover models");
    discBtn.type = "button";
    discBtn.addEventListener("click", function () { discoverInto(provider, accId, detail, discBtn); });
    acts.appendChild(discBtn);

    var delBtn = el("button", "btn btn-mini", "Remove");
    delBtn.type = "button";
    delBtn.addEventListener("click", function () { removeAccount(provider, accId, a.env_file); });
    acts.appendChild(delBtn);

    ab.appendChild(acts);
    ab.appendChild(detail);
    return ab;
  }

  function handleWarning(res) {
    if (res && typeof res === "object" && res.warning) toast(String(res.warning), "warn");
    return res;
  }

  function afterAccountMutation(msg) {
    if (msg) toast(msg, "ok");
    return refreshStatus().then(function () {
      renderModels();
      return loadAccounts();
    });
  }

  function runTest(provider, accId, model, detail, allowPaid, btn) {
    var body = {};
    if (model) body.model = model;
    if (allowPaid) body.allow_paid = true;
    if (btn) btn.disabled = true;
    clear(detail).appendChild(el("span", "dim", "testing " + (model || "first model") + " ..."));
    return api("api/accounts/" + enc(provider) + "/" + enc(accId) + "/test", {
      method: "POST",
      body: JSON.stringify(body)
    }).then(function (res) {
      handleWarning(res);
      renderTestResult(detail, res);
    }).catch(function (e) {
      if (e.status === 409) {
        var box = clear(detail);
        var line = el("div", "test-line");
        line.appendChild(el("span", "pill pill-exhausted", "refused"));
        line.appendChild(el("span", null, "paid model - test refused"));
        var again = el("button", "btn btn-mini", "test anyway (may cost)");
        again.type = "button";
        again.addEventListener("click", function () {
          runTest(provider, accId, model, detail, true, again);
        });
        line.appendChild(again);
        box.appendChild(line);
        var why = errText(e);
        if (why) box.appendChild(el("div", "test-err", why));
      } else {
        renderTestResult(detail, { status: "error", error: errText(e) });
      }
    }).then(function () {
      if (btn) btn.disabled = false;
    });
  }

  function testPill(status) {
    if (status === "ok") return "pill pill-ok";
    if (status === "quota") return "pill pill-exhausted";
    if (status === "retry" || status === "cooldown") return "pill pill-cooldown";
    return "pill pill-down";
  }

  function renderTestResult(detail, res) {
    var box = clear(detail);
    var line = el("div", "test-line");
    var st = String((res && res.status) || (res && res.ok === false ? "error" : "unknown"));
    line.appendChild(el("span", testPill(st), st));
    var lat = res && (res.latency_ms !== undefined ? res.latency_ms : res.latency);
    if (lat !== undefined && lat !== null) line.appendChild(el("span", "mono", Math.round(lat) + " ms"));
    var served = res && (res.model || res.served_model || res.model_id);
    if (served) line.appendChild(el("span", "mono dim", "served " + served));
    var count = res && (res.attempt_count !== undefined ? res.attempt_count
      : (typeof res.attempts === "number" ? res.attempts : null));
    if (count !== null && count !== undefined) line.appendChild(el("span", "dim", "attempts " + count));
    if (res && res.http_status) line.appendChild(el("span", "dim", "http " + res.http_status));
    box.appendChild(line);
    if (res && res.status === "no_key") {
      box.appendChild(el("div", "test-err",
        "no key in env " + (res.api_key_env || "-") + " - rotate the key first"));
    }
    if (res && res.sample) box.appendChild(el("div", "dim mono", "sample " + res.sample));
    var err = res && (res.error || res.vendor_error || res.message);
    if (err) {
      var text;
      if (typeof err === "string") {
        text = err;
      } else if (err.error && (err.error.message || typeof err.error === "string")) {
        text = String(err.error.message || err.error);
      } else if (err.message) {
        text = String(err.message);
      } else {
        text = JSON.stringify(err);
      }
      if (res.error_code) text = res.error_code + ": " + text;
      box.appendChild(el("div", "test-err", text));
    }
  }

  function rotateForm(provider, accId, detail) {
    var box = clear(detail);
    var form = el("form", "inline-form");
    form.autocomplete = "off";
    var inp = el("input");
    inp.type = "password";
    inp.autocomplete = "off";
    inp.spellcheck = false;
    inp.required = true;
    inp.placeholder = "new api key for " + provider + "/" + accId;
    inp.setAttribute("aria-label", "new api key");
    inp.setAttribute("data-role", "rotate-key");
    var save = el("button", "btn btn-mini btn-primary", "Save key");
    save.type = "submit";
    var cancel = el("button", "btn btn-mini", "Cancel");
    cancel.type = "button";
    cancel.addEventListener("click", function () { inp.value = ""; clear(detail); });
    form.appendChild(inp);
    form.appendChild(save);
    form.appendChild(cancel);
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var value = inp.value;
      inp.value = "";
      if (!value) return;
      save.disabled = true;
      api("api/accounts/" + enc(provider) + "/" + enc(accId) + "/key", {
        method: "PUT",
        body: JSON.stringify({ api_key: value })
      }).then(function (res) {
        handleWarning(res);
        clear(detail);
        return afterAccountMutation("Key rotated for " + provider + "/" + accId);
      }).catch(function (e2) {
        save.disabled = false;
        clear(detail).appendChild(el("div", "test-err", "rotate failed: " + errText(e2)));
      });
    });
    box.appendChild(form);
    inp.focus();
  }

  function removeAccount(provider, accId, envFile) {
    return ask({
      title: "Remove account",
      body: provider + "/" + accId + " will be removed from the registry.",
      checkbox: "also delete the key line from the env file" + (envFile ? " (" + envFile + ")" : ""),
      confirm: "Remove",
      cancel: "Keep"
    }).then(function (r) {
      if (!r) return null;
      var url = "api/accounts/" + enc(provider) + "/" + enc(accId) + (r.checked ? "?purge_key=1" : "");
      return api(url, { method: "DELETE" }).then(function (res) {
        handleWarning(res);
        return afterAccountMutation("Removed " + provider + "/" + accId +
          (r.checked ? " and its env line" : ""));
      }).catch(function (e) {
        toast("remove failed: " + errText(e), "bad");
      });
    });
  }

  function fetchDiscover(provider, accId) {
    return api("api/providers/" + enc(provider) + "/discover", {
      method: "POST",
      body: JSON.stringify({ account_id: accId })
    }).then(function (res) {
      handleWarning(res);
      var rows = Array.isArray(res) ? res : ((res && (res.models || res.ids || res.data)) || rowsOf(res));
      return rows.map(function (m) { return typeof m === "string" ? { id: m } : m; })
        .filter(function (m) { return modelId(m); });
    });
  }

  /* checklist of discovered ids; returns a function yielding the ticked model payloads */
  function renderDiscovered(host, items) {
    clear(host);
    if (!items.length) {
      host.appendChild(el("span", "dim", "vendor returned no models"));
      return function () { return []; };
    }
    var boxes = [];
    items.forEach(function (m) {
      var id = modelId(m);
      var line = el("label", "check-line");
      var cb = el("input");
      cb.type = "checkbox";
      cb.value = id;
      cb.setAttribute("data-discovered", id);
      line.appendChild(cb);
      line.appendChild(el("span", null, id));
      (m.caps || []).forEach(function (c) { line.appendChild(el("span", "cap", c)); });
      line.appendChild(el("span", "win-text", m.free === undefined ? "limits unknown" : modelWindowText(m)));
      host.appendChild(line);
      boxes.push({ cb: cb, m: m });
    });
    return function () {
      return boxes.filter(function (b) { return b.cb.checked; }).map(function (b) {
        var out = { id: modelId(b.m), free: b.m.free === undefined ? {} : b.m.free,
          notes: b.m.notes || "limits unknown" };
        if (b.m.caps) out.caps = b.m.caps;
        if (b.m.extra_body) out.extra_body = b.m.extra_body;
        return out;
      });
    };
  }

  /* register models on an account that already exists: POST api/accounts with the same
     provider/account_id and no key (append). A backend that treats a repeated account id as
     a conflict gets the second shape instead, on the account's own models path. */
  function registerModels(provider, accId, models) {
    var body = JSON.stringify({ provider: provider, account_id: accId, models: models });
    return api("api/accounts", { method: "POST", body: body }).catch(function (e) {
      if (e.status !== 409 && e.status !== 405) throw e;
      return api("api/accounts/" + enc(provider) + "/" + enc(accId) + "/models", {
        method: "POST",
        body: JSON.stringify({ models: models })
      });
    });
  }

  function discoverInto(provider, accId, detail, btn) {
    if (btn) btn.disabled = true;
    clear(detail).appendChild(el("span", "dim", "discovering models from the vendor ..."));
    return fetchDiscover(provider, accId).then(function (items) {
      var box = clear(detail);
      box.appendChild(el("div", "field-label", "discovered models - tick the ones to register"));
      var listHost = el("div", "check-list");
      box.appendChild(listHost);
      var selected = renderDiscovered(listHost, items);
      var foot = el("div", "inline-form");
      var reg = el("button", "btn btn-mini btn-primary", "Register selected");
      reg.type = "button";
      reg.addEventListener("click", function () {
        var models = selected();
        if (!models.length) {
          toast("tick at least one model", "warn");
          return;
        }
        reg.disabled = true;
        registerModels(provider, accId, models).then(function (res) {
          handleWarning(res);
          clear(detail);
          return afterAccountMutation("Registered " + models.length + " model(s) on " +
            provider + "/" + accId);
        }).catch(function (e) {
          reg.disabled = false;
          toast("register failed: " + errText(e), "bad");
        });
      });
      var close = el("button", "btn btn-mini", "Close");
      close.type = "button";
      close.addEventListener("click", function () { clear(detail); });
      foot.appendChild(reg);
      foot.appendChild(close);
      box.appendChild(foot);
    }).catch(function (e) {
      clear(detail).appendChild(el("div", "test-err", "discover failed: " + errText(e)));
    }).then(function () {
      if (btn) btn.disabled = false;
    });
  }

  /* ---------- add account panel ---------- */

  function knownProviders() {
    if (state.known) return Promise.resolve(state.known);
    return api("api/providers/known").then(function (data) {
      var rows = Array.isArray(data)
        ? data
        : ((data && (data.providers || data.templates || data.known)) || rowsOf(data));
      state.known = rows.map(normalizeTemplate).filter(function (t) { return t.id; });
      return state.known;
    }).catch(function () {
      state.known = [];
      return state.known;
    });
  }

  function normalizeTemplate(t) {
    if (typeof t === "string") t = { id: t };
    return {
      id: String(t.id || t.provider || t.name || ""),
      kind: t.kind || "openai",
      aliases: (t.aliases || []).map(String).filter(Boolean),
      base_url: t.base_url || t.baseUrl || "",
      api_key_env: t.api_key_env || t.env || t.env_var || t.api_key || "",
      docs_url: t.docs_url || t.docs || "",
      fields: (t.fields || t.extra_fields || []).map(function (f) {
        if (typeof f === "string") return { name: f, label: f, placeholder: "", value: "" };
        return {
          name: String(f.name || f.id || ""),
          label: String(f.label || f.name || f.id || ""),
          placeholder: f.placeholder || "",
          value: f.default !== undefined ? f.default : (f.value || "")
        };
      }).filter(function (f) { return f.name; }),
      models: (t.models || []).map(function (m) {
        return typeof m === "string" ? { id: m, free: {} } : m;
      })
    };
  }

  function templateFor(id) {
    var list = state.known || [];
    for (var i = 0; i < list.length; i++) {
      if (list[i].id === id) return list[i];
    }
    return null;
  }

  function substituteBase(base, values) {
    return String(base || "").replace(/\{([A-Za-z0-9_]+)\}/g, function (all, name) {
      return values[name] !== undefined && values[name] !== "" ? values[name] : all;
    });
  }

  function fieldValues() {
    var out = {};
    $$("#add-extra input").forEach(function (i) {
      var v = i.value.trim();
      if (v) out[i.getAttribute("data-field")] = v;
    });
    return out;
  }

  function setAddResult(msg, kind) {
    var n = $("#add-result");
    if (!msg) { n.hidden = true; n.textContent = ""; n.className = "add-result"; return; }
    n.hidden = false;
    n.className = "add-result" + (kind ? " " + kind : "");
    n.textContent = msg;
  }

  function setKeyVisible(on) {
    var k = $("#add-key");
    var b = $("#add-key-toggle");
    k.type = on ? "text" : "password";
    b.textContent = on ? "Hide" : "Show";
    b.setAttribute("aria-pressed", on ? "true" : "false");
  }

  function openAdd() {
    state.add = { stage: "form", tpl: null, provider: "", account: "", baseDirty: false, selected: null };
    $("#add-account-form").hidden = false;
    setAdvancedToggle(true);
    $("#add-lan-warn").hidden = isLoopbackHost();
    setAddResult(null);
    setKeyVisible(false);
    $("#add-key").value = "";
    $("#add-submit").disabled = false;
    $("#add-submit").textContent = "Add account";
    ["add-template", "add-provider", "add-account-id", "add-kind", "add-base-url", "add-env"]
      .forEach(function (id) { $("#" + id).disabled = false; });
    knownProviders().then(function (list) {
      var sel = clear($("#add-template"));
      list.forEach(function (t) {
        var o = el("option", null, t.id);
        o.value = t.id;
        sel.appendChild(o);
      });
      var c = el("option", null, "custom");
      c.value = "";
      sel.appendChild(c);
      sel.value = list.length ? list[0].id : "";
      applyTemplate();
    });
  }

  function setAdvancedToggle(open) {
    var b = $("#add-account-btn");
    b.setAttribute("aria-expanded", open ? "true" : "false");
    b.textContent = open ? "Hide advanced" : "Advanced";
  }

  function closeAdd() {
    $("#add-key").value = "";
    setKeyVisible(false);
    $("#add-account-form").hidden = true;
    setAdvancedToggle(false);
    clear($("#add-extra"));
    clear($("#add-models"));
    setAddResult(null);
    state.add = null;
  }

  function applyTemplate() {
    var id = $("#add-template").value;
    var tpl = id ? templateFor(id) : null;
    state.add.tpl = tpl;
    state.add.baseDirty = false;
    state.add.selected = null;

    var provider = tpl ? tpl.id : "";
    var pin = $("#add-provider");
    pin.value = provider;
    pin.readOnly = !!tpl;
    $("#add-account-id").value = provider ? provider + "-main" : "";
    $("#add-kind").value = (tpl && tpl.kind) || "openai";
    $("#add-env").value = (tpl && tpl.api_key_env) || "";
    $("#add-base-url").value = tpl ? tpl.base_url : "";

    var docs = $("#add-docs");
    if (tpl && tpl.docs_url) {
      docs.href = tpl.docs_url;
      docs.textContent = "Provider docs";
      docs.title = tpl.docs_url;
      docs.hidden = false;
    } else {
      docs.hidden = true;
    }

    renderExtraFields(tpl);
    renderTemplateModels(tpl);
    updateBaseFromExtras();

    var disc = $("#add-discover");
    disc.hidden = false;
    disc.disabled = true;
    disc.title = "available once the account exists";
    $("#add-models-hint").textContent = (tpl && tpl.models.length)
      ? "known free models, all ticked"
      : "no template models - create the account first, then discover";
  }

  function renderExtraFields(tpl) {
    var host = clear($("#add-extra"));
    if (!tpl || !tpl.fields.length) return;
    tpl.fields.forEach(function (f) {
      var lab = el("label", "field");
      lab.appendChild(el("span", "field-label", f.label));
      var inp = el("input");
      inp.type = "text";
      inp.value = f.value || "";
      inp.placeholder = f.placeholder || "";
      inp.autocomplete = "off";
      inp.setAttribute("data-field", f.name);
      inp.id = "add-x-" + f.name;
      inp.addEventListener("input", updateBaseFromExtras);
      lab.appendChild(inp);
      host.appendChild(lab);
    });
  }

  function updateBaseFromExtras() {
    var tpl = state.add && state.add.tpl;
    if (!tpl || state.add.baseDirty) return;
    $("#add-base-url").value = substituteBase(tpl.base_url, fieldValues());
  }

  function renderTemplateModels(tpl) {
    var host = clear($("#add-models"));
    state.add.selected = null;
    state.add.allowanceTicked = function () { return false; };
    var models = (tpl && tpl.models) || [];
    if (!models.length) {
      host.appendChild(el("span", "dim", "none"));
      syncActivatedField();
      return;
    }
    var boxes = [];
    models.forEach(function (m) {
      var id = modelId(m);
      var line = el("label", "check-line");
      var cb = el("input");
      cb.type = "checkbox";
      cb.checked = true;
      cb.value = id;
      cb.setAttribute("data-model", id);
      cb.addEventListener("change", syncActivatedField);
      line.appendChild(cb);
      line.appendChild(el("span", null, id));
      (m.caps || []).forEach(function (c) { line.appendChild(el("span", "cap", c)); });
      line.appendChild(el("span", "win-text", modelWindowText(m)));
      host.appendChild(line);
      boxes.push({ cb: cb, m: m });
    });
    state.add.selected = function () {
      return boxes.filter(function (b) { return b.cb.checked; }).map(function (b) {
        var out = { id: modelId(b.m) };
        if (b.m.caps) out.caps = b.m.caps;
        if (b.m.free !== undefined) out.free = b.m.free;
        if (b.m.extra_body) out.extra_body = b.m.extra_body;
        if (b.m.context) out.context = b.m.context;
        if (b.m.reset_tz) out.reset_tz = b.m.reset_tz;
        if (b.m.concurrency) out.concurrency = b.m.concurrency;
        if (b.m.notes) out.notes = b.m.notes;
        return out;
      });
    };
    state.add.allowanceTicked = function () {
      return boxes.some(function (b) { return b.cb.checked && hasAllowance(b.m); });
    };
    syncActivatedField();
  }

  function syncActivatedField() {
    var on = !!(state.add && state.add.allowanceTicked && state.add.allowanceTicked());
    $("#add-activated-field").hidden = !on;
    if (!on) $("#add-activated").value = "";
  }

  function collectAddModels() {
    return (state.add && state.add.selected) ? state.add.selected() : [];
  }

  function submitAdd(e) {
    e.preventDefault();
    if (state.add && state.add.stage === "discover") {
      return submitDiscovered();
    }
    var keyEl = $("#add-key");
    var apiKey = keyEl.value;
    keyEl.value = "";
    setKeyVisible(false);

    var provider = $("#add-provider").value.trim();
    var accountId = $("#add-account-id").value.trim();
    if (!provider || !accountId) {
      setAddResult("provider id and account id are required", "bad");
      return;
    }

    var payload = { provider: provider, account_id: accountId, kind: $("#add-kind").value };
    if (apiKey) payload.api_key = apiKey;
    var env = $("#add-env").value.trim();
    if (env) payload.api_key_env = env;
    var base = $("#add-base-url").value.trim();
    if (base) payload.base_url = base;
    var models = collectAddModels();
    if (models.length) payload.models = models;
    var act = $("#add-activated").value;
    if (act && !$("#add-activated-field").hidden) payload.activated_at = act;
    var tpl = state.add.tpl;
    if (tpl) {
      payload.template = tpl.id;
      if (tpl.docs_url) payload.docs_url = tpl.docs_url;
    }
    var fields = fieldValues();
    if (Object.keys(fields).length) payload.fields = fields;

    var submit = $("#add-submit");
    submit.disabled = true;
    setAddResult("creating " + provider + "/" + accountId + " ...");

    api("api/accounts", { method: "POST", body: JSON.stringify(payload) })
      .then(function (res) {
        handleWarning(res);
        state.add.provider = provider;
        state.add.account = accountId;
        if (models.length) {
          closeAdd();
          return afterAccountMutation("Added " + provider + "/" + accountId +
            " with " + models.length + " model(s)");
        }
        enterDiscoverStage(provider, accountId);
        return afterAccountMutation("Added " + provider + "/" + accountId + " without models");
      })
      .catch(function (e2) {
        submit.disabled = false;
        setAddResult("add failed: " + errText(e2), "bad");
      });
  }

  function enterDiscoverStage(provider, accountId) {
    state.add.stage = "discover";
    setAddResult("Account created. Discover models, tick the ones to register, then submit.", "good");
    ["add-template", "add-provider", "add-account-id", "add-kind", "add-base-url", "add-env"].forEach(function (id) {
      $("#" + id).disabled = true;
    });
    $("#add-key").value = "";
    var disc = $("#add-discover");
    disc.hidden = false;
    disc.disabled = false;
    disc.title = "";
    var submit = $("#add-submit");
    submit.textContent = "Register selected models";
    submit.disabled = false;
    clear($("#add-models")).appendChild(el("span", "dim", "run discover to list the vendor's models"));
    $("#add-models-hint").textContent = "";
  }

  function runAddDiscover() {
    var provider = state.add.provider || $("#add-provider").value.trim();
    var accountId = state.add.account || $("#add-account-id").value.trim();
    var btn = $("#add-discover");
    btn.disabled = true;
    var host = clear($("#add-models"));
    host.appendChild(el("span", "dim", "discovering models from the vendor ..."));
    fetchDiscover(provider, accountId).then(function (items) {
      state.add.selected = renderDiscovered($("#add-models"), items);
      state.add.allowanceTicked = function () { return false; };
      setAddResult("Tick the models to register on " + provider + "/" + accountId + ".", "good");
    }).catch(function (e) {
      clear($("#add-models"));
      setAddResult("discover failed: " + errText(e), "bad");
    }).then(function () { btn.disabled = false; });
  }

  function submitDiscovered() {
    var models = collectAddModels();
    if (!models.length) {
      setAddResult("tick at least one model", "bad");
      return;
    }
    var provider = state.add.provider;
    var accountId = state.add.account;
    var submit = $("#add-submit");
    submit.disabled = true;
    registerModels(provider, accountId, models)
      .then(function (res) {
        handleWarning(res);
        closeAdd();
        return afterAccountMutation("Registered " + models.length + " model(s) on " +
          provider + "/" + accountId);
      })
      .catch(function (e) {
        submit.disabled = false;
        setAddResult("register failed: " + errText(e), "bad");
      });
  }

  /* ---------- quick add: a key plus a hint of who it is from ---------- */

  /* the key never lands in state or in the DOM: the caller reads the input into a local,
     blanks the input, and hands the value to runQuickAdd, which drops it as soon as the
     request settles. A 422 asking for more detail re-submits from the same closure. */

  function fmtSecs(ms) {
    var v = Number(ms);
    if (ms === undefined || ms === null || !isFinite(v)) return null;
    return (v / 1000).toFixed(1) + " s";
  }

  function testText(t) {
    if (!t) return "no test run";
    if (t.ok === true || t.status === "ok") {
      var s = "test ok";
      var secs = fmtSecs(t.latency_ms !== undefined ? t.latency_ms : t.latency);
      if (secs) s += " " + secs;
      if (t.model) s += " (served " + t.model + ")";
      return s;
    }
    var err = t.error;
    var text;
    if (typeof err === "string") text = err;
    else if (err && err.error && (err.error.message || typeof err.error === "string")) {
      text = String(err.error.message || err.error);
    } else if (err && err.message) text = String(err.message);
    else if (err) text = JSON.stringify(err);
    else return "test " + String(t.status || "failed");
    if (t.error_code) text = t.error_code + ": " + text;
    return "test failed - " + text;
  }

  function quickText(res) {
    var models = res.models || [];
    var head = String(res.provider || "provider") +
      (res.rotated ? " key rotated: " : " added: ") +
      models.length + " model" + (models.length === 1 ? "" : "s");
    if (res.discovered) head += " (discovered)";
    return head + ", " + testText(res.test);
  }

  function quickOk(res) {
    var t = res && res.test;
    return !t || t.ok === true || t.status === "ok";
  }

  /* 422 body: {needs: [...], guess: ...}, plain or wrapped in detail/error */
  function needsOf(e) {
    var j = errJson(e);
    if (!j) return null;
    var cands = [j, j.detail, j.error];
    for (var i = 0; i < cands.length; i++) {
      var c = cands[i];
      if (c && typeof c === "object" && Array.isArray(c.needs)) {
        return {
          needs: c.needs.map(String),
          guess: c.guess,
          guesses: Array.isArray(c.guesses) ? c.guesses : null,
          provider: c.provider || "",
          message: c.message || c.hint || ""
        };
      }
    }
    return null;
  }

  function guessOptions(guess) {
    var out = [];
    function push(v) {
      if (v === null || v === undefined) return;
      if (typeof v === "string") {
        if (v) out.push({ value: v, label: v });
        return;
      }
      var value = v.value || v.id || v.provider || v.source || v.base_url || "";
      if (!value) return;
      var label = v.label || value;
      if (v.why || v.reason) label += " - " + (v.why || v.reason);
      out.push({ value: String(value), label: String(label) });
    }
    if (Array.isArray(guess)) guess.forEach(push);
    else push(guess);
    return out;
  }

  function guessStatusLabel(status) {
    if (status === "exists_key_rejected") return "key rejected";
    if (status === "no_response") return "no response";
    if (status === "not_found") return "not found";
    if (status === "error") return "error";
    return String(status || "unknown");
  }

  function quickBody(ctx) {
    var body = { api_key: ctx.key };
    if (ctx.source) body.source = ctx.source;
    if (ctx.promoId !== null && ctx.promoId !== undefined) body.promo_id = ctx.promoId;
    if (ctx.baseUrl) body.base_url = ctx.baseUrl;
    if (ctx.fields && Object.keys(ctx.fields).length) body.fields = ctx.fields;
    return body;
  }

  function runQuickAdd(ctx) {
    ctx.busy(true);
    ctx.status("adding the key ...", null);
    return api("api/accounts/quick", { method: "POST", body: JSON.stringify(quickBody(ctx)) })
      .then(function (res) {
        ctx.key = "";
        clear(ctx.host);
        ctx.busy(false);
        ctx.status(null);
        handleWarning(res);
        toast(quickText(res), quickOk(res) ? "ok" : "warn");
        ctx.done(res);
      })
      .catch(function (e) {
        ctx.busy(false);
        var need = e.status === 422 ? needsOf(e) : null;
        if (need) {
          var fallback = need.guesses && need.guesses.length
            ? "no known endpoint for this provider - check or edit the guessed URL"
            : "the hub needs one more detail";
          ctx.status(need.message || fallback, null);
          renderQuickNeeds(ctx, need);
          return;
        }
        ctx.key = "";
        clear(ctx.host);
        ctx.status("add failed: " + errText(e), "bad");
        toast("add failed: " + errText(e), "bad");
      });
  }

  function renderQuickNeeds(ctx, need) {
    var host = clear(ctx.host);
    var controls = [];

    need.needs.forEach(function (what) {
      var lab = el("label", "field");
      var control;
      var triedNode = null;
      if (what === "source") {
        lab.appendChild(el("span", "field-label", "Who is it from"));
        var opts = guessOptions(need.guess);
        if (opts.length) {
          control = el("select");
          control.setAttribute("data-need", "source");
          opts.forEach(function (o) {
            var op = el("option", null, o.label);
            op.value = o.value;
            control.appendChild(op);
          });
        } else {
          control = el("input");
          control.type = "text";
          control.setAttribute("data-need", "source");
          control.placeholder = "provider id, alias or url";
        }
      } else if (what === "base_url") {
        /* guess names the provider the hub landed on (a list of template ids), so it
           labels the field; a guess sent as a url is used as the value instead.
           guesses (plural) are base urls the hub actually tried - prefer the one
           that at least found a key check (exists_key_rejected) as the prefill */
        var g = need.guess;
        var guesses = need.guesses || [];
        var named = need.provider ||
          (Array.isArray(g) && typeof g[0] === "string" && g[0].indexOf("://") < 0 ? g[0] : "");
        lab.appendChild(el("span", "field-label", "Base url" + (named ? " for " + named : "")));
        control = el("input");
        control.type = "text";
        control.setAttribute("data-need", "base_url");
        control.placeholder = "https://api.example.com/v1";
        control.spellcheck = false;
        var best = guesses.find(function (x) { return x && x.status === "exists_key_rejected"; }) || guesses[0];
        control.value = best && best.url
          ? best.url
          : (typeof g === "string" && g.indexOf("://") >= 0
            ? g
            : ((g && !Array.isArray(g) && (g.base_url || g.value)) || ""));
        if (guesses.length) {
          triedNode = el("div", "dim quick-tried", "tried: " + guesses.map(function (x) {
            return (x && x.url ? x.url : "?") + " (" + guessStatusLabel(x && x.status) + ")";
          }).join(", "));
        }
      } else {
        var g2 = need.guess;
        var named2 = Array.isArray(g2) && typeof g2[0] === "string" ? g2[0] : "";
        var label = what.replace(/_/g, " ");
        label = label.charAt(0).toUpperCase() + label.slice(1);
        lab.appendChild(el("span", "field-label", label + (named2 ? " for " + named2 : "")));
        control = el("input");
        control.type = "text";
        control.setAttribute("data-need", what);
        lab.appendChild(control);
        lab.appendChild(el("span", "dim", "or paste the dashboard URL into From whom"));
        host.appendChild(lab);
        controls.push({ what: what, control: control });
        return;
      }
      lab.appendChild(control);
      if (triedNode) lab.appendChild(triedNode);
      host.appendChild(lab);
      controls.push({ what: what, control: control });
    });

    var foot = el("div", "inline-form");
    var go = el("button", "btn btn-mini btn-primary", "Continue");
    go.type = "button";
    go.setAttribute("data-role", "quick-continue");
    go.addEventListener("click", function () {
      var missing = false;
      controls.forEach(function (c) {
        var v = String(c.control.value || "").trim();
        if (!v) { missing = true; return; }
        if (c.what === "base_url") ctx.baseUrl = v;
        else if (c.what === "source") ctx.source = v;
        else {
          ctx.fields = ctx.fields || {};
          ctx.fields[c.what] = v;
        }
      });
      if (missing) {
        ctx.status("fill the field above to continue", "bad");
        return;
      }
      runQuickAdd(ctx);
    });
    var cancel = el("button", "btn btn-mini", "Cancel");
    cancel.type = "button";
    cancel.addEventListener("click", function () {
      ctx.key = "";
      clear(ctx.host);
      ctx.status("cancelled - the key was dropped, paste it again to retry", null);
    });
    foot.appendChild(go);
    foot.appendChild(cancel);
    host.appendChild(foot);
    if (controls.length) controls[0].control.focus();
  }

  function setQuickResult(msg, kind) {
    var n = $("#quick-result");
    if (!msg) { n.hidden = true; n.textContent = ""; n.className = "add-result"; return; }
    n.hidden = false;
    n.className = "add-result" + (kind ? " " + kind : "");
    n.textContent = msg;
  }

  function setQuickKeyVisible(on) {
    var k = $("#quick-key");
    var b = $("#quick-key-toggle");
    k.type = on ? "text" : "password";
    b.textContent = on ? "Hide" : "Show";
    b.setAttribute("aria-pressed", on ? "true" : "false");
  }

  function fillKnownDatalist() {
    return knownProviders().then(function (list) {
      var host = clear($("#known-providers"));
      var seen = {};
      list.forEach(function (t) {
        [t.id].concat(t.aliases || []).forEach(function (name) {
          if (!name || seen[name]) return;
          seen[name] = true;
          var o = el("option");
          o.value = name;
          if (name !== t.id) o.label = t.id;
          host.appendChild(o);
        });
      });
      return list;
    });
  }

  function submitQuickAdd(e) {
    e.preventDefault();
    var keyEl = $("#quick-key");
    var key = keyEl.value;
    keyEl.value = "";
    setQuickKeyVisible(false);
    if (!key) {
      setQuickResult("paste the api key first", "bad");
      return;
    }
    var sourceEl = $("#quick-source");
    var ctx = {
      key: key,
      source: sourceEl.value.trim(),
      promoId: null,
      baseUrl: "",
      host: $("#quick-extra"),
      status: setQuickResult,
      busy: function (on) { $("#quick-submit").disabled = on; },
      done: function (res) {
        sourceEl.value = "";
        setQuickResult(quickText(res), quickOk(res) ? "good" : null);
        afterAccountMutation(null);
      }
    };
    runQuickAdd(ctx);
  }

  function shortenUrl(raw) {
    if (!raw) return null;
    try {
      var u = new URL(raw);
      var segs = u.pathname.split("/").filter(Boolean);
      var short = u.hostname + (segs.length ? "/" + segs[0] : "");
      var full = u.hostname + u.pathname + u.search + u.hash;
      return full.length > short.length ? short + "..." : short;
    } catch (e) {
      return raw.length > 42 ? raw.slice(0, 42) + "..." : raw;
    }
  }

  function promoStatusClass(s) {
    if (s === "used") return "pill pill-ok";
    if (s === "expired") return "pill pill-down";
    if (s === "rejected") return "pill pill-rejected";
    if (s === "known") return "pill pill-cooldown";
    return "pill pill-unknown";
  }

  function promoSource(r) {
    return [r.provider || "", r.url || ""].join(" ").trim();
  }

  function promoStatusOf(r) { return r.status || "new"; }

  function promoStatusRank(r) {
    var v = PROMO_STATUS_RANK[promoStatusOf(r)];
    return v === undefined ? 0 : v;
  }

  function promoSourceGroup(s) {
    s = String(s || "");
    var idx = s.indexOf(",");
    return (idx >= 0 ? s.slice(0, idx) : s).trim();
  }

  function promoExpiresTs(r) {
    var d = parseTs(r.expires_at);
    return d ? d.getTime() : null;
  }

  function promoFoundTs(r) {
    var d = parseTs(r.found_at);
    return d ? d.getTime() : -Infinity;
  }

  function promoKeyRank(r) { return r.account_key ? 0 : 1; }

  function orderStatusValues(values) {
    return values.slice().sort(function (a, b) {
      var ra = PROMO_STATUS_RANK[a] !== undefined ? PROMO_STATUS_RANK[a] : 99;
      var rb = PROMO_STATUS_RANK[b] !== undefined ? PROMO_STATUS_RANK[b] : 99;
      if (ra !== rb) return ra - rb;
      return a.localeCompare(b);
    });
  }

  function sortPromos(rows) {
    var s = state.promosSort;
    var dirMul = s.dir === "desc" ? -1 : 1;
    var arr = rows.slice();
    arr.sort(function (a, b) {
      if (s.key === "expires") {
        var ea = promoExpiresTs(a), eb = promoExpiresTs(b);
        if (ea === null && eb === null) return 0;
        if (ea === null) return 1;
        if (eb === null) return -1;
        return dirMul * (ea - eb);
      }
      var diff = 0;
      if (s.key === "provider") diff = String(a.provider || "").localeCompare(String(b.provider || ""));
      else if (s.key === "status") diff = promoStatusRank(a) - promoStatusRank(b);
      else if (s.key === "source") diff = String(a.source || "").localeCompare(String(b.source || ""));
      else if (s.key === "key") diff = promoKeyRank(a) - promoKeyRank(b);
      else if (s.key === "found") diff = promoFoundTs(a) - promoFoundTs(b);
      return dirMul * diff;
    });
    return arr;
  }

  function filteredPromos() {
    var f = state.promosFilters;
    var q = f.q.trim().toLowerCase();
    return state.promosRaw.filter(function (r) {
      var status = promoStatusOf(r);
      if (f.hideDone && (status === "used" || status === "expired" || status === "rejected")) return false;
      if (f.statuses.length && f.statuses.indexOf(status) < 0) return false;
      if (f.source && promoSourceGroup(r.source) !== f.source) return false;
      if (q) {
        var hay = (String(r.provider || "") + " " + String(r.url || "") + " " +
          String(r.note || "") + " " + String(r.rejected_reason || "")).toLowerCase();
        if (hay.indexOf(q) < 0) return false;
      }
      return true;
    });
  }

  function renderPromoStatusChips(statuses) {
    var host = clear($("#promos-status-chips"));
    state.promosFilters.statuses = state.promosFilters.statuses.filter(function (s) {
      return statuses.indexOf(s) >= 0;
    });
    if (!statuses.length) {
      host.appendChild(el("span", "dim", "no statuses"));
      return;
    }
    statuses.forEach(function (s) {
      var b = el("button", "chip-toggle", s);
      b.type = "button";
      b.setAttribute("aria-pressed", state.promosFilters.statuses.indexOf(s) >= 0 ? "true" : "false");
      b.addEventListener("click", function () {
        var idx = state.promosFilters.statuses.indexOf(s);
        if (idx >= 0) state.promosFilters.statuses.splice(idx, 1);
        else state.promosFilters.statuses.push(s);
        savePromosFilters();
        b.setAttribute("aria-pressed", idx >= 0 ? "false" : "true");
        renderPromosRows();
      });
      host.appendChild(b);
    });
  }

  function fillPromoSourceSelect(values) {
    var sel = $("#promos-source-filter");
    var want = state.promosFilters.source;
    clear(sel);
    var optAll = el("option", null, "all");
    optAll.value = "";
    sel.appendChild(optAll);
    values.forEach(function (v) {
      var o = el("option", null, v);
      o.value = v;
      sel.appendChild(o);
    });
    sel.value = values.indexOf(want) >= 0 ? want : "";
    if (want && sel.value !== want) {
      state.promosFilters.source = "";
      savePromosFilters();
    }
  }

  function populatePromosFilterOptions() {
    var rows = state.promosRaw;
    renderPromoStatusChips(orderStatusValues(uniqueSorted(rows.map(function (r) { return promoStatusOf(r); }))));
    fillPromoSourceSelect(uniqueSorted(rows.map(function (r) { return promoSourceGroup(r.source); })));
  }

  function syncPromosFilterBar() {
    $("#promos-search").value = state.promosFilters.q;
    $("#promos-hide-done").checked = state.promosFilters.hideDone;
  }

  function syncPromosSortHeaders() {
    $$("#promos-table thead th[data-sort-key]").forEach(function (th) {
      var key = th.getAttribute("data-sort-key");
      var active = state.promosSort.key === key;
      th.setAttribute("data-sort-active", active ? "true" : "false");
      th.setAttribute("data-sort-dir", active ? state.promosSort.dir : "");
      th.setAttribute("aria-sort", active ? (state.promosSort.dir === "asc" ? "ascending" : "descending") : "none");
    });
  }

  /* inline "Add key" row under a promo: one password field, one button, and the
     provider it will be filed under - the promo row is the "who is it from" hint */
  function openPromoAdd(promo, tr, btn) {
    var next = tr.nextSibling;
    if (next && next.className === "promo-add") {
      var inp = $("input[data-role='promo-key']", next);
      if (inp) inp.value = "";
      next.parentNode.removeChild(next);
      btn.setAttribute("aria-expanded", "false");
      return;
    }
    btn.setAttribute("aria-expanded", "true");

    var row = el("tr", "promo-add");
    var td = el("td");
    td.colSpan = 9;
    var form = el("form", "promo-add-form");
    form.autocomplete = "off";

    var line = el("div", "inline-form");
    var key = el("input");
    key.type = "password";
    key.autocomplete = "off";
    key.spellcheck = false;
    key.required = true;
    key.placeholder = "api key from " + (promo.provider || "this promo");
    key.setAttribute("aria-label", "api key");
    key.setAttribute("data-role", "promo-key");
    var toggle = el("button", "btn btn-mini", "Show");
    toggle.type = "button";
    toggle.setAttribute("aria-pressed", "false");
    toggle.addEventListener("click", function () {
      var on = key.type === "password";
      key.type = on ? "text" : "password";
      toggle.textContent = on ? "Hide" : "Show";
      toggle.setAttribute("aria-pressed", on ? "true" : "false");
    });
    var add = el("button", "btn btn-mini btn-primary", "Add");
    add.type = "submit";
    add.setAttribute("data-role", "promo-add-submit");
    line.appendChild(key);
    line.appendChild(toggle);
    line.appendChild(add);
    form.appendChild(line);

    form.appendChild(el("div", "dim promo-add-meta",
      "provider: " + (promo.provider || "unknown") + " (from this promo)"));

    var extra = el("div", "quick-extra");
    form.appendChild(extra);
    var result = el("div", "promo-add-result");
    result.hidden = true;
    form.appendChild(result);

    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var value = key.value;
      key.value = "";
      key.type = "password";
      toggle.textContent = "Show";
      toggle.setAttribute("aria-pressed", "false");
      if (!value) {
        result.hidden = false;
        result.className = "promo-add-result bad";
        result.textContent = "paste the api key first";
        return;
      }
      var ctx = {
        key: value,
        source: promoSource(promo),
        promoId: promo.id !== undefined ? promo.id : promo.promo_id,
        baseUrl: "",
        host: extra,
        status: function (msg, kind) {
          if (!msg) { result.hidden = true; result.textContent = ""; return; }
          result.hidden = false;
          result.className = "promo-add-result" + (kind ? " " + kind : "");
          result.textContent = msg;
        },
        busy: function (on) { add.disabled = on; },
        done: function () {
          afterAccountMutation(null);
          loadPromos();
        }
      };
      runQuickAdd(ctx);
    });

    td.appendChild(form);
    row.appendChild(td);
    tr.parentNode.insertBefore(row, tr.nextSibling);
    key.focus();
  }

  /* inline "Reject" row: the reason is the point of the action, so it is typed here and
     not guessed - the scout and the promo-hunt skill read it back as a standing rule */
  function openPromoReject(promo, tr, btn) {
    var next = tr.nextSibling;
    if (next && next.className === "promo-reject") {
      next.parentNode.removeChild(next);
      btn.setAttribute("aria-expanded", "false");
      return;
    }
    btn.setAttribute("aria-expanded", "true");

    var row = el("tr", "promo-reject");
    var td = el("td");
    td.colSpan = 9;
    var form = el("form", "promo-reject-form");

    var why = el("textarea");
    why.rows = 2;
    why.placeholder = "why this is useless for a free-only gateway - the scout learns from it";
    why.setAttribute("aria-label", "rejection reason");
    why.setAttribute("data-role", "promo-reject-reason");
    form.appendChild(why);

    var line = el("div", "inline-form");
    var reject = el("button", "btn btn-mini btn-danger", "Reject");
    reject.type = "submit";
    reject.disabled = true;
    reject.setAttribute("data-role", "promo-reject-submit");
    var cancel = el("button", "btn btn-mini", "Cancel");
    cancel.type = "button";
    line.appendChild(reject);
    line.appendChild(cancel);
    line.appendChild(el("span", "dim promo-reject-hint",
      "at least " + REJECT_REASON_MIN + " characters"));
    form.appendChild(line);

    var result = el("div", "promo-add-result");
    result.hidden = true;
    form.appendChild(result);

    why.addEventListener("input", function () {
      reject.disabled = why.value.trim().length < REJECT_REASON_MIN;
    });
    function close() {
      if (row.parentNode) row.parentNode.removeChild(row);
      btn.setAttribute("aria-expanded", "false");
    }
    cancel.addEventListener("click", close);

    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var reason = why.value.trim();
      if (reason.length < REJECT_REASON_MIN) return;
      reject.disabled = true;
      api("api/promos/" + promo.id + "/reject", {
        method: "POST",
        body: JSON.stringify({ reason: reason })
      }).then(function () {
        close();
        toast("Rejected " + (promo.provider || "promo"), "ok");
        loadPromos();
      }).catch(function (err) {
        reject.disabled = false;
        result.hidden = false;
        result.className = "promo-add-result bad";
        result.textContent = errText(err);
      });
    });

    td.appendChild(form);
    row.appendChild(td);
    tr.parentNode.insertBefore(row, tr.nextSibling);
    why.focus();
  }

  function reopenPromo(promo, btn) {
    btn.disabled = true;
    api("api/promos/" + promo.id + "/reopen", { method: "POST", body: "{}" })
      .then(function () {
        toast("Reopened " + (promo.provider || "promo"), "ok");
        loadPromos();
      })
      .catch(function (err) {
        btn.disabled = false;
        toast("Reopen failed: " + errText(err), "bad");
      });
  }

  function scoutRunStatusPill(s) {
    if (s === "done") return "pill pill-ok";
    if (s === "failed") return "pill pill-down";
    if (s === "running") return "pill pill-cooldown";
    return "pill pill-unknown";
  }

  function runDurationText(run) {
    if (!run || !run.started_at) return "-";
    var start = parseTs(run.started_at);
    if (!start) return "-";
    var end = run.finished_at ? parseTs(run.finished_at) : null;
    var ms = (end ? end.getTime() : Date.now()) - start.getTime();
    var s = Math.max(0, Math.round(ms / 1000));
    var m = Math.floor(s / 60);
    var sec = s % 60;
    var text = m > 0 ? (m + "m" + pad2(sec) + "s") : (sec + "s");
    return end ? text : text + " (running)";
  }

  function scoutModelsLine(run) {
    var mu = (run && run.models_used) || {};
    var extract = (mu.extract && mu.extract.length) ? mu.extract.join(", ") : "-";
    var curate = (mu.curate && mu.curate.length) ? mu.curate.join(", ") : "-";
    return "extract: " + extract + " - curate: " + curate;
  }

  function scoutTokensLine(run) {
    var t = (run && run.tokens) || {};
    var ex = t.extract || {};
    var cu = t.curate || {};
    return "extract " + fmtInt(ex.in) + " in / " + fmtInt(ex.out) + " out - " +
      "curate " + fmtInt(cu.in) + " in / " + fmtInt(cu.out) + " out";
  }

  function stopScoutPoll() {
    if (scoutPollTimer) { clearTimeout(scoutPollTimer); scoutPollTimer = null; }
  }

  function scheduleScoutPoll() {
    stopScoutPoll();
    scoutPollTimer = setTimeout(function () { loadScoutStatus(); }, 5000);
  }

  function renderScoutSummary(status) {
    var host = clear($("#scout-summary"));
    var run = status && status.last_run;

    $("#scout-schedule-chip").textContent = status && status.schedule
      ? "schedule " + status.schedule + " daily" : "schedule off";

    var nextEl = $("#scout-next");
    if (status && status.active) {
      nextEl.removeAttribute("data-eta");
      nextEl.textContent = "run in progress";
    } else if (status && status.next_run_at) {
      nextEl.setAttribute("data-eta", status.next_run_at);
      nextEl.textContent = fmtTime(status.next_run_at) + " (" + fmtDelta(status.next_run_at) + ")";
    } else {
      nextEl.removeAttribute("data-eta");
      nextEl.textContent = "schedule off";
    }

    if (!run) {
      host.appendChild(el("p", "empty", "no runs yet"));
      return;
    }

    var meta = el("div", "scout-meta");
    meta.appendChild(el("span", scoutRunStatusPill(run.status), run.status || "-"));
    meta.appendChild(el("span", "dim",
      fmtTime(run.started_at) + " (" + fmtAgo(run.started_at) + ") - " + runDurationText(run)));
    host.appendChild(meta);

    var cards = el("div", "cards scout-cards");
    function card(label, value) {
      var c = el("div", "card");
      c.appendChild(el("div", "card-label", label));
      c.appendChild(el("div", "card-value num", value));
      cards.appendChild(c);
    }
    card("Sources", fmtInt(run.sources));
    card("Pages", fmtInt(run.pages_fetched) + "/" + fmtInt(run.pages_changed) + " chg");
    card("Offers", fmtInt(run.offers));
    card("New", fmtInt(run.new));
    card("Updated", fmtInt(run.updated));
    card("Skipped", fmtInt(run.skipped));
    card("Errors", fmtInt(run.errors));
    host.appendChild(cards);

    var stages = el("div", "scout-stages dim");
    stages.appendChild(el("div", null, "models - " + scoutModelsLine(run)));
    stages.appendChild(el("div", null, "tokens - " + scoutTokensLine(run)));
    host.appendChild(stages);
  }

  function loadScoutStatus() {
    return api("api/scout/status").then(function (data) {
      var wasActive = state.scoutActive;
      state.scoutActive = !!data.active;
      renderScoutSummary(data);
      $("#scout-run-btn").disabled = state.scoutActive;
      if (state.scoutActive) {
        scheduleScoutPoll();
      } else {
        stopScoutPoll();
        if (wasActive) {
          loadPromos();
          if (!$("#scout-runs-panel").hidden) loadScoutRuns();
        }
      }
    }).catch(function (e) {
      stopScoutPoll();
      clear($("#scout-summary")).appendChild(el("p", "empty", "api/scout/status failed: " + e.message));
    });
  }

  function scoutRunNow() {
    $("#scout-run-btn").disabled = true;
    api("api/scout/run", { method: "POST", body: "{}" })
      .then(function () {
        toast("Scout run started", "ok");
        return loadScoutStatus();
      })
      .catch(function (e) {
        if (e.status === 409) toast("A scout run is already active", "warn");
        else toast("Run now failed: " + errText(e), "bad");
        return loadScoutStatus();
      });
  }

  function loadScoutRuns() {
    return api("api/scout/runs?limit=10").then(function (data) {
      var rows = (data && Array.isArray(data.runs)) ? data.runs : [];
      var body = clear($("#scout-runs-body"));
      if (!rows.length) { body.appendChild(emptyRow(6, "no runs yet")); return; }
      rows.forEach(function (r) {
        var tr = el("tr", "scout-run-row");
        tr.tabIndex = 0;
        tr.appendChild(el("td", "mono dim", fmtTime(r.started_at)));
        var tdSt = el("td", "td-fit");
        tdSt.appendChild(el("span", scoutRunStatusPill(r.status), r.status || "-"));
        tr.appendChild(tdSt);
        tr.appendChild(el("td", "num mono", fmtInt(r.pages_fetched)));
        tr.appendChild(el("td", "num mono", fmtInt(r.offers)));
        tr.appendChild(el("td", "mono dim", r.new + " / " + r.updated + " / " + r.skipped));
        tr.appendChild(el("td", "num mono", fmtInt(r.errors)));
        tr.addEventListener("click", function () { openScoutReport(r.id); });
        tr.addEventListener("keydown", function (e) {
          if (e.key === "Enter") openScoutReport(r.id);
        });
        body.appendChild(tr);
      });
    }).catch(function (e) {
      clear($("#scout-runs-body")).appendChild(emptyRow(6, "api/scout/runs failed: " + e.message));
    });
  }

  function openScoutReport(runId) {
    api("api/scout/runs/" + encodeURIComponent(runId)).then(function (run) {
      var back = $("#modal");
      var box = clear($("#modal-box"));
      box.classList.add("modal-wide");

      box.appendChild(el("h3", null, "Scout run " + runId));

      var metaText = fmtTime(run.started_at) + " - " + runDurationText(run) + " - " + (run.status || "-") +
        " - " + fmtInt(run.pages_fetched) + " pages, " + fmtInt(run.offers) + " offers, " +
        run.new + " new / " + run.updated + " updated / " + run.skipped + " skipped" +
        (run.errors ? ", " + run.errors + " errors" : "");
      box.appendChild(el("p", "modal-body", metaText));

      box.appendChild(el("pre", "scout-report-pre", run.report_md || "(no report)"));

      if (run.decisions && run.decisions.length) {
        box.appendChild(el("h3", null, "Decisions"));
        var scroller = el("div", "scroller");
        var table = el("table", "scout-decisions-table");
        var thead = el("thead");
        var htr = el("tr");
        ["Action", "Provider", "Reason"].forEach(function (t) { htr.appendChild(el("th", null, t)); });
        thead.appendChild(htr);
        table.appendChild(thead);
        var tbody = el("tbody");
        run.decisions.forEach(function (d) {
          var dtr = el("tr");
          dtr.appendChild(el("td", "mono", d.action || "-"));
          var provider = (d.row && d.row.provider) || (d.promo_id !== undefined ? "#" + d.promo_id : "-");
          dtr.appendChild(el("td", "mono", String(provider)));
          dtr.appendChild(el("td", "wrap", d.reason || "-"));
          tbody.appendChild(dtr);
        });
        table.appendChild(tbody);
        scroller.appendChild(table);
        box.appendChild(scroller);
      }

      var foot = el("div", "modal-foot");
      var close = el("button", "btn btn-primary", "Close");
      close.type = "button";
      foot.appendChild(close);
      box.appendChild(foot);

      function finish() {
        back.hidden = true;
        box.classList.remove("modal-wide");
        clear(box);
        back.removeEventListener("click", onBack);
        document.removeEventListener("keydown", onKey, true);
      }
      function onBack(e) { if (e.target === back) finish(); }
      function onKey(e) { if (e.key === "Escape") { e.preventDefault(); finish(); } }

      close.addEventListener("click", finish);
      back.addEventListener("click", onBack);
      document.addEventListener("keydown", onKey, true);

      back.hidden = false;
      close.focus();
    }).catch(function (e) {
      toast("failed to load report: " + errText(e), "bad");
    });
  }

  function renderPromosRows() {
    syncPromosSortHeaders();
    var rows = sortPromos(filteredPromos());
    var body = clear($("#promos-body"));
    if (!rows.length) {
      body.appendChild(emptyRow(9, state.promosRaw.length ? "no promos match the filters" : "watchlist empty"));
    } else {
      rows.forEach(function (r) {
        var tr = el("tr");
        var tdP = el("td");
        tdP.appendChild(el("span", "mono", r.provider || "-"));
        if (r.provider) tdP.title = r.provider;
        if (r.base_url) {
          var buLine = el("div", "dim mono promo-base-url", shortenUrl(r.base_url));
          buLine.title = r.base_url;
          tdP.appendChild(buLine);
        }
        tr.appendChild(tdP);

        var tdU = el("td");
        if (r.url) {
          var a = el("a", "mono promo-url", shortenUrl(r.url));
          a.href = r.url;
          a.title = r.url;
          a.target = "_blank";
          a.rel = "noreferrer noopener";
          tdU.appendChild(a);
        } else {
          tdU.appendChild(el("span", "dim", "-"));
        }
        tr.appendChild(tdU);

        var tdN = el("td", "wrap");
        tdN.appendChild(el("div", null, r.note || "-"));
        if (r.rejected_reason) {
          var why = el("div", "dim promo-reject-reason", "rejected: " + r.rejected_reason);
          why.title = r.rejected_reason;
          tdN.appendChild(why);
        }
        tr.appendChild(tdN);

        var tdS = el("td", "td-fit");
        var statusPill = el("span", promoStatusClass(r.status), r.status || "new");
        if (r.rejected_reason) statusPill.title = r.rejected_reason;
        tdS.appendChild(statusPill);
        if (r.account_key) {
          var added = el("span", "pill pill-ok promo-key-pill", "key added");
          added.title = String(r.account_key);
          tdS.appendChild(added);
        }
        if (r.updates_count) {
          tdS.appendChild(el("span", "chip promo-updates-chip", "+" + r.updates_count + " updates"));
        }
        tr.appendChild(tdS);

        tr.appendChild(el("td", "mono dim", r.expires_at ? fmtTime(r.expires_at) : "-"));

        var tdFound = el("td", "mono dim", fmtTime(r.found_at));
        if (r.updated_at) tdFound.title = "last update: " + fmtTime(r.updated_at);
        tr.appendChild(tdFound);

        var tdSrc = el("td", "td-fit");
        if (r.source) {
          var srcChip = el("span", "chip chip-source", r.source);
          srcChip.title = r.source;
          tdSrc.appendChild(srcChip);
        } else {
          tdSrc.appendChild(el("span", "dim", "-"));
        }
        tr.appendChild(tdSrc);

        var tdKey = el("td", "td-fit");
        tdKey.appendChild(r.account_key ? el("span", "pill pill-ok", "key") : el("span", "dim", "-"));
        tr.appendChild(tdKey);

        var tdAct = el("td", "td-fit");
        var actions = el("div", "promo-actions");
        tdAct.appendChild(actions);
        var addBtn = el("button", "btn btn-mini", r.account_key ? "Add key again" : "Add key");
        addBtn.type = "button";
        addBtn.setAttribute("data-promo-add", String(r.id !== undefined ? r.id : ""));
        addBtn.setAttribute("aria-expanded", "false");
        addBtn.addEventListener("click", function () { openPromoAdd(r, tr, addBtn); });
        actions.appendChild(addBtn);
        var status = promoStatusOf(r);
        if (status === "rejected") {
          var reopenBtn = el("button", "btn btn-mini", "Reopen");
          reopenBtn.type = "button";
          reopenBtn.setAttribute("data-promo-reopen", String(r.id !== undefined ? r.id : ""));
          reopenBtn.addEventListener("click", function () { reopenPromo(r, reopenBtn); });
          actions.appendChild(reopenBtn);
        } else if (status !== "used") {
          var rejBtn = el("button", "btn btn-mini", "Reject");
          rejBtn.type = "button";
          rejBtn.setAttribute("data-promo-reject", String(r.id !== undefined ? r.id : ""));
          rejBtn.setAttribute("aria-expanded", "false");
          rejBtn.addEventListener("click", function () { openPromoReject(r, tr, rejBtn); });
          actions.appendChild(rejBtn);
        }
        tr.appendChild(tdAct);

        body.appendChild(tr);
      });
    }
    $("#promos-count").textContent = rows.length + " of " + state.promosRaw.length + " promos";
  }

  function loadPromos() {
    syncPromosFilterBar();
    return api("api/promos").then(function (data) {
      state.promosRaw = rowsOf(data);
      populatePromosFilterOptions();
      renderPromosRows();
    }).catch(function (e) {
      state.promosRaw = [];
      clear($("#promos-body")).appendChild(emptyRow(9, "api/promos failed: " + e.message));
      $("#promos-count").textContent = "0 of 0 promos";
    });
  }

  function eventsLimit() {
    var r = state.eventsFilters.range;
    return (r === "7d" || r === "all") ? 500 : 200;
  }

  function eventsRangeMs(r) {
    if (r === "1h") return 3600e3;
    if (r === "24h") return 864e5;
    if (r === "7d") return 7 * 864e5;
    return null;
  }

  function kindPillClass(kind) {
    if (kind === "error" || kind === "unavailable") return "pill pill-down";
    if (kind === "quota" || kind === "waiting_quota" || kind === "no_candidates") return "pill pill-exhausted";
    if (kind === "fallback" || kind === "retry" || kind === "cooldown") return "pill pill-cooldown";
    return "pill pill-unknown";
  }

  function uniqueSorted(values) {
    var seen = {};
    var out = [];
    values.forEach(function (v) {
      if (v === undefined || v === null || v === "") return;
      var s = String(v);
      if (!seen[s]) { seen[s] = true; out.push(s); }
    });
    out.sort();
    return out;
  }

  function fillSelect(sel, values, want, stateField) {
    clear(sel);
    var optAll = el("option", null, "all");
    optAll.value = "";
    sel.appendChild(optAll);
    values.forEach(function (v) {
      var o = el("option", null, v);
      o.value = v;
      sel.appendChild(o);
    });
    sel.value = values.indexOf(want) >= 0 ? want : "";
    if (want && sel.value !== want) {
      state.eventsFilters[stateField] = "";
      saveEventsFilters();
    }
  }

  function renderKindChips(kinds) {
    var host = clear($("#events-kind-chips"));
    state.eventsFilters.kinds = state.eventsFilters.kinds.filter(function (k) {
      return kinds.indexOf(k) >= 0;
    });
    if (!kinds.length) {
      host.appendChild(el("span", "dim", "no kinds"));
      return;
    }
    kinds.forEach(function (k) {
      var b = el("button", "chip-toggle", k);
      b.type = "button";
      b.setAttribute("aria-pressed", state.eventsFilters.kinds.indexOf(k) >= 0 ? "true" : "false");
      b.addEventListener("click", function () {
        var idx = state.eventsFilters.kinds.indexOf(k);
        if (idx >= 0) state.eventsFilters.kinds.splice(idx, 1);
        else state.eventsFilters.kinds.push(k);
        saveEventsFilters();
        b.setAttribute("aria-pressed", idx >= 0 ? "false" : "true");
        renderEventsRows();
      });
      host.appendChild(b);
    });
  }

  function populateEventsFilterOptions() {
    var rows = state.eventsRaw;
    renderKindChips(uniqueSorted(rows.map(function (r) { return r.kind; })));
    fillSelect($("#events-app-filter"), uniqueSorted(rows.map(function (r) { return r.app; })),
      state.eventsFilters.app, "app");
    fillSelect($("#events-model-filter"), uniqueSorted(rows.map(function (r) { return r.model; })),
      state.eventsFilters.model, "model");
  }

  function syncEventsFilterBar() {
    $("#events-search").value = state.eventsFilters.q;
    $$(".seg-btn", $("#events-range")).forEach(function (b) {
      b.setAttribute("aria-pressed", b.getAttribute("data-range") === state.eventsFilters.range ? "true" : "false");
    });
  }

  function filteredEvents() {
    var f = state.eventsFilters;
    var rangeMs = eventsRangeMs(f.range);
    var cutoff = rangeMs === null ? null : Date.now() - rangeMs;
    var q = f.q.trim().toLowerCase();
    return state.eventsRaw.filter(function (r) {
      if (f.kinds.length && f.kinds.indexOf(String(r.kind || "")) < 0) return false;
      if (f.app && String(r.app || "") !== f.app) return false;
      if (f.model && String(r.model || "") !== f.model) return false;
      if (cutoff !== null) {
        var d = parseTs(r.ts);
        if (!d || d.getTime() < cutoff) return false;
      }
      if (q) {
        var hay = (String(r.message || "") + " " + String(r.model || "") + " " + String(r.app || "")).toLowerCase();
        if (hay.indexOf(q) < 0) return false;
      }
      return true;
    });
  }

  function renderEventsRows() {
    var rows = filteredEvents();
    var body = clear($("#events-body"));
    if (!rows.length) {
      body.appendChild(emptyRow(5, "no events match the filters"));
    } else {
      rows.forEach(function (r) {
        var kind = r.kind || "-";
        var tr = el("tr");
        var tdT = el("td", "mono dim", fmtTime(r.ts));
        tdT.title = r.ts || "";
        tr.appendChild(tdT);
        var tdK = el("td", "td-fit");
        tdK.appendChild(el("span", kindPillClass(kind), kind));
        tr.appendChild(tdK);
        tr.appendChild(el("td", "mono", r.app || "-"));
        tr.appendChild(el("td", "mono", r.model || "-"));
        var tdM = el("td", "wrap mono", r.message || "-");
        tdM.title = r.message || "";
        tr.appendChild(tdM);
        body.appendChild(tr);
      });
    }
    $("#events-count").textContent = rows.length + " of " + state.eventsRaw.length + " events";
  }

  function loadEvents() {
    syncEventsFilterBar();
    return api("api/events?limit=" + eventsLimit()).then(function (data) {
      state.eventsRaw = rowsOf(data);
      populateEventsFilterOptions();
      renderEventsRows();
    }).catch(function (e) {
      state.eventsRaw = [];
      clear($("#events-body")).appendChild(emptyRow(5, "api/events failed: " + e.message));
      $("#events-count").textContent = "0 of 0 events";
    });
  }

  function showTab(name) {
    state.tab = name;
    $$(".tab").forEach(function (t) {
      t.setAttribute("aria-selected", t.getAttribute("data-tab") === name ? "true" : "false");
    });
    $$(".panel").forEach(function (p) {
      p.hidden = p.getAttribute("data-panel") !== name;
    });
    if (location.hash.slice(1) !== name) history.replaceState(null, "", "#" + name);
    loadTab(name);
  }

  function loadTab(name) {
    if (name === "models") return renderModels();
    if (name === "usage") return loadUsage();
    if (name === "queue") return loadQueue();
    if (name === "accounts") return loadAccounts();
    if (name === "promos") { loadScoutStatus(); return loadPromos(); }
    if (name === "events") return loadEvents();
  }

  function initSeg(sel, attr, stateKey, onChange) {
    var buttons = $$(".seg-btn", $(sel));
    function sync() {
      buttons.forEach(function (b) {
        b.setAttribute("aria-pressed", b.getAttribute(attr) === String(state[stateKey]) ? "true" : "false");
      });
    }
    buttons.forEach(function (b) {
      b.addEventListener("click", function () {
        state[stateKey] = b.getAttribute(attr);
        sync();
        onChange();
      });
    });
    sync();
  }

  function init() {
    updateTokenButton();

    $$(".tab").forEach(function (t) {
      t.addEventListener("click", function () { showTab(t.getAttribute("data-tab")); });
    });
    $("#refresh-btn").addEventListener("click", function () {
      refreshStatus().then(function () { loadTab(state.tab); });
    });
    $("#token-btn").addEventListener("click", function () { askToken(); });

    $("#models-search").addEventListener("input", function (e) {
      state.modelsFilters.q = e.target.value;
      saveModelsFilters();
      renderModels();
    });
    $("#models-provider-filter").addEventListener("change", function (e) {
      state.modelsFilters.provider = e.target.value;
      saveModelsFilters();
      renderModels();
    });
    $("#models-live-only").addEventListener("change", function (e) {
      state.modelsFilters.liveOnly = e.target.checked;
      saveModelsFilters();
      renderModels();
    });
    $("#models-used-today").addEventListener("change", function (e) {
      state.modelsFilters.usedToday = e.target.checked;
      saveModelsFilters();
      renderModels();
    });
    $("#models-only-problems").addEventListener("change", function (e) {
      state.modelsFilters.onlyProblems = e.target.checked;
      saveModelsFilters();
      renderModels();
    });
    $("#models-clear").addEventListener("click", function () {
      state.modelsFilters = clone(DEFAULT_MODELS_FILTERS);
      saveModelsFilters();
      renderModels();
    });
    $$("#models-table thead th[data-sort-key]").forEach(function (th) {
      th.addEventListener("click", function () {
        var key = th.getAttribute("data-sort-key");
        var def = MODELS_SORT_DEFAULT_DIR[key] || "asc";
        var cur = state.modelsSort;
        if (!cur || cur.key !== key) state.modelsSort = { key: key, dir: def };
        else if (cur.dir === def) state.modelsSort = { key: key, dir: def === "asc" ? "desc" : "asc" };
        else state.modelsSort = null;
        saveModelsSort();
        renderModels();
      });
    });

    initSeg("#usage-window", "data-window", "usageWindow", loadUsage);
    initSeg("#usage-metric", "data-metric", "usageMetric", loadUsage);
    initSeg("#queue-state", "data-state", "queueState", loadQueue);

    $$(".seg-btn", $("#events-range")).forEach(function (b) {
      b.addEventListener("click", function () {
        state.eventsFilters.range = b.getAttribute("data-range");
        saveEventsFilters();
        loadEvents();
      });
    });
    $("#events-app-filter").addEventListener("change", function (e) {
      state.eventsFilters.app = e.target.value;
      saveEventsFilters();
      renderEventsRows();
    });
    $("#events-model-filter").addEventListener("change", function (e) {
      state.eventsFilters.model = e.target.value;
      saveEventsFilters();
      renderEventsRows();
    });
    $("#events-search").addEventListener("input", function (e) {
      state.eventsFilters.q = e.target.value;
      saveEventsFilters();
      renderEventsRows();
    });
    $("#events-clear").addEventListener("click", function () {
      state.eventsFilters = clone(DEFAULT_EVENTS_FILTERS);
      saveEventsFilters();
      loadEvents();
    });

    $("#promos-source-filter").addEventListener("change", function (e) {
      state.promosFilters.source = e.target.value;
      savePromosFilters();
      renderPromosRows();
    });
    $("#promos-search").addEventListener("input", function (e) {
      state.promosFilters.q = e.target.value;
      savePromosFilters();
      renderPromosRows();
    });
    $("#promos-hide-done").addEventListener("change", function (e) {
      state.promosFilters.hideDone = e.target.checked;
      savePromosFilters();
      renderPromosRows();
    });
    $("#promos-clear").addEventListener("click", function () {
      state.promosFilters = clone(DEFAULT_PROMO_FILTERS);
      savePromosFilters();
      syncPromosFilterBar();
      populatePromosFilterOptions();
      renderPromosRows();
    });
    $$("#promos-table thead th[data-sort-key]").forEach(function (th) {
      th.addEventListener("click", function () {
        var key = th.getAttribute("data-sort-key");
        if (state.promosSort.key === key) {
          state.promosSort.dir = state.promosSort.dir === "asc" ? "desc" : "asc";
        } else {
          state.promosSort.key = key;
          state.promosSort.dir = PROMO_SORT_DEFAULT_DIR[key] || "asc";
        }
        savePromosSort();
        renderPromosRows();
      });
    });

    $("#add-account-btn").addEventListener("click", function () {
      if ($("#add-account-form").hidden) openAdd();
      else closeAdd();
    });
    $("#add-cancel").addEventListener("click", closeAdd);

    $("#quick-lan-warn").hidden = isLoopbackHost();
    $("#quick-form").addEventListener("submit", submitQuickAdd);
    $("#quick-key-toggle").addEventListener("click", function () {
      setQuickKeyVisible($("#quick-key").type === "password");
    });
    fillKnownDatalist();
    $("#add-template").addEventListener("change", applyTemplate);
    $("#add-account-form").addEventListener("submit", submitAdd);
    $("#add-discover").addEventListener("click", function () {
      if (state.add && state.add.stage === "discover") runAddDiscover();
    });
    $("#add-key-toggle").addEventListener("click", function () {
      setKeyVisible($("#add-key").type === "password");
    });
    $("#add-base-url").addEventListener("input", function () {
      if (state.add) state.add.baseDirty = true;
    });

    $("#scout-run-btn").addEventListener("click", scoutRunNow);
    $("#scout-runs-toggle").addEventListener("click", function () {
      var panel = $("#scout-runs-panel");
      var opening = panel.hidden;
      panel.hidden = !opening;
      $("#scout-runs-toggle").setAttribute("aria-expanded", opening ? "true" : "false");
      if (opening) loadScoutRuns();
    });

    $("#promo-form").addEventListener("submit", function (e) {
      e.preventDefault();
      var f = e.target;
      var payload = {
        provider: f.provider.value.trim(),
        url: f.url.value.trim(),
        note: f.note.value.trim()
      };
      api("api/promos", { method: "POST", body: JSON.stringify(payload) })
        .then(function () {
          f.reset();
          notice(null);
          return loadPromos();
        })
        .catch(function (e2) { notice("add promo failed: " + e2.message); });
    });

    window.addEventListener("hashchange", function () {
      var h = location.hash.slice(1);
      if (h && h !== state.tab) showTab(h);
    });

    var initialTab = location.hash.slice(1) || "models";
    if (!$('.tab[data-tab="' + initialTab + '"]')) initialTab = "models";

    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "visible") pollLive();
    });

    refreshStatus().then(function () { showTab(initialTab); });
    loadLive().then(scheduleLive, scheduleLive);
    setInterval(function () {
      refreshStatus().then(function () { if (state.tab === "queue") loadQueue(); });
    }, POLL_MS);
    setInterval(function () {
      tickCountdowns();
      tickLive();
    }, 1000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
