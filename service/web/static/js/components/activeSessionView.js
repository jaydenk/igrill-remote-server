/**
 * ActiveSessionView
 * -----------------
 *
 * Live view for the Cook tab while a session is running. Mounts into
 * the #activeCookPlaceholder section of index.html when
 * SessionStore.status === "active", and unmounts when it transitions
 * back to "none".
 *
 * Layout (top-down):
 *   1. Session header    -- inline editable name, elapsed timer, progress
 *                           bar when targetDurationSecs is set.
 *   2. Chart area        -- uPlot line chart, one series per targeted
 *                           probe, X axis = seconds since session start.
 *   3. Probe cards grid  -- one card per targeted probe with current
 *                           temp, target display, status chip, timer
 *                           stub (Task 8 wires the timer controls).
 *   4. Action row        -- Add Probe / Add Note / End Session buttons.
 *                           Task 7 leaves these as visual stubs;
 *                           Tasks 9/10/11 wire real behaviour.
 *
 * This module is intentionally self-contained. It reads state through
 * window.SessionStore.instance.getState() and sends session_update_request
 * messages via the global `ws` (attached by the main IIFE). It does NOT
 * import from the IIFE; the main bundle just calls mount()/unmount().
 *
 * Exposed on window.ActiveSessionView with a singleton-style mount API
 * so the legacy IIFE can wire it without bundler machinery.
 */
(function (global) {
  "use strict";

  /* ------------------------------------------------------------------ */
  /* Styles (injected once)                                              */
  /* ------------------------------------------------------------------ */

  var STYLE_ID = "asv-styles";
  var STYLE_CSS = [
    ".asv-root{display:flex;flex-direction:column;gap:1.25rem;}",
    /* Header */
    ".asv-header{background:var(--bg-card,#0f3460);border:1px solid var(--border,#2a2a4a);border-radius:var(--radius,10px);padding:1rem 1.25rem;display:flex;flex-direction:column;gap:0.75rem;}",
    ".asv-header-row{display:flex;align-items:baseline;gap:1rem;flex-wrap:wrap;justify-content:space-between;}",
    ".asv-name-wrap{display:flex;align-items:center;gap:0.5rem;min-width:0;flex:1;}",
    ".asv-name{font-size:1.2rem;font-weight:600;color:var(--text-primary,#e0e0e0);background:transparent;border:1px solid transparent;border-radius:6px;padding:0.2rem 0.4rem;cursor:text;max-width:100%;}",
    ".asv-name:hover{background:var(--bg-card-hover,#134074);}",
    ".asv-name:focus-visible{outline:2px solid var(--brand,#935240);outline-offset:2px;}",
    ".asv-name-input{font-size:1.2rem;font-weight:600;color:var(--text-primary,#e0e0e0);background:var(--bg-secondary,#16213e);border:1px solid var(--brand,#935240);border-radius:6px;padding:0.2rem 0.4rem;font-family:inherit;width:100%;max-width:360px;}",
    ".asv-elapsed{font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;font-size:1.1rem;color:var(--text-primary,#e0e0e0);letter-spacing:0.02em;white-space:nowrap;}",
    ".asv-elapsed-label{font-size:0.75rem;color:var(--text-secondary,#a0a0b8);text-transform:uppercase;letter-spacing:0.06em;margin-right:0.5rem;}",
    ".asv-progress{display:flex;flex-direction:column;gap:0.35rem;}",
    ".asv-progress-meta{display:flex;justify-content:space-between;font-size:0.75rem;color:var(--text-secondary,#a0a0b8);}",
    ".asv-progress-track{position:relative;height:6px;background:var(--bg-secondary,#16213e);border-radius:3px;overflow:hidden;}",
    ".asv-progress-fill{position:absolute;top:0;left:0;bottom:0;background:var(--brand,#935240);border-radius:3px;transition:width 0.4s ease, background 0.2s ease;}",
    ".asv-progress-fill.over{background:var(--amber,#f5a623);}",
    /* Chart */
    ".asv-chart-card{background:var(--bg-card,#0f3460);border:1px solid var(--border,#2a2a4a);border-radius:var(--radius,10px);padding:1rem;}",
    ".asv-chart-title{font-size:0.8rem;color:var(--text-secondary,#a0a0b8);text-transform:uppercase;letter-spacing:0.06em;margin-bottom:0.5rem;}",
    ".asv-chart-host{width:100%;min-height:220px;}",
    ".asv-chart-empty{padding:2rem 0;text-align:center;color:var(--text-secondary,#a0a0b8);font-size:0.9rem;}",
    /* Probe cards */
    ".asv-probes{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:0.85rem;}",
    ".asv-probe-card{background:var(--bg-card,#0f3460);border:1px solid var(--border,#2a2a4a);border-radius:var(--radius,10px);padding:1rem;display:flex;flex-direction:column;gap:0.45rem;}",
    ".asv-probe-label{font-size:0.75rem;color:var(--text-secondary,#a0a0b8);text-transform:uppercase;letter-spacing:0.06em;}",
    ".asv-probe-temp{font-size:1.75rem;font-weight:600;line-height:1;color:var(--text-primary,#e0e0e0);font-variant-numeric:tabular-nums;}",
    ".asv-probe-temp.unplugged{color:var(--text-muted,#6c6c80);font-size:1.25rem;}",
    ".asv-probe-target{font-size:0.85rem;color:var(--text-secondary,#a0a0b8);}",
    ".asv-chip{display:inline-flex;align-items:center;gap:0.3rem;font-size:0.72rem;font-weight:600;text-transform:uppercase;letter-spacing:0.05em;padding:0.25rem 0.55rem;border-radius:999px;align-self:flex-start;}",
    ".asv-chip.climbing{background:rgba(147,82,64,0.22);color:#f0b8a8;}",
    ".asv-chip.in-range{background:rgba(74,222,128,0.18);color:var(--green,#4ade80);}",
    ".asv-chip.over{background:rgba(245,166,35,0.2);color:var(--amber,#f5a623);}",
    ".asv-chip.unknown{background:var(--bg-secondary,#16213e);color:var(--text-muted,#6c6c80);}",
    ".asv-probe-timer{font-size:0.78rem;color:var(--text-muted,#6c6c80);margin-top:0.15rem;}",
    /* Actions */
    ".asv-actions{display:flex;gap:0.5rem;flex-wrap:wrap;}",
    ".asv-btn{background:var(--bg-card,#0f3460);color:var(--text-primary,#e0e0e0);border:1px solid var(--border,#2a2a4a);border-radius:8px;padding:0.6rem 1rem;font-size:0.95rem;font-family:inherit;cursor:pointer;transition:background 0.15s;}",
    ".asv-btn:hover{background:var(--bg-card-hover,#134074);}",
    ".asv-btn:focus-visible{outline:2px solid var(--brand,#935240);outline-offset:2px;}",
    ".asv-btn-danger{color:var(--red,#f87171);border-color:rgba(248,113,113,0.4);}",
    ".asv-btn-danger:hover{background:rgba(248,113,113,0.12);}",
  ].join("\n");

  function ensureStyles() {
    if (typeof document === "undefined") return;
    if (document.getElementById(STYLE_ID)) return;
    var s = document.createElement("style");
    s.id = STYLE_ID;
    s.textContent = STYLE_CSS;
    document.head.appendChild(s);
  }

  /* ------------------------------------------------------------------ */
  /* Helpers                                                             */
  /* ------------------------------------------------------------------ */

  function el(tag, className, text) {
    var e = document.createElement(tag);
    if (className) e.className = className;
    if (text != null) e.textContent = text;
    return e;
  }

  function nowSeconds() {
    return Date.now() / 1000;
  }

  function sessionStartSeconds(session) {
    if (!session || session.startedAt == null) return null;
    /* startedAt may be an ISO string or a numeric epoch (seconds or
     * millis). Normalise to epoch seconds. */
    var s = session.startedAt;
    if (typeof s === "string") {
      var parsed = Date.parse(s);
      if (isNaN(parsed)) return null;
      return parsed / 1000;
    }
    if (typeof s === "number") {
      /* Heuristic: values beyond year ~33658 are almost certainly ms. */
      return s > 1e12 ? s / 1000 : s;
    }
    return null;
  }

  function formatElapsed(secs) {
    if (secs == null || secs < 0 || !isFinite(secs)) return "00:00";
    secs = Math.floor(secs);
    var h = Math.floor(secs / 3600);
    var m = Math.floor((secs % 3600) / 60);
    var s = secs % 60;
    function pad(n) { return (n < 10 ? "0" : "") + n; }
    if (h > 0) return h + ":" + pad(m) + ":" + pad(s);
    return pad(m) + ":" + pad(s);
  }

  function formatTarget(target) {
    if (!target) return "";
    if (target.mode === "range") {
      var lo = target.range_low != null ? Math.round(target.range_low) : "?";
      var hi = target.range_high != null ? Math.round(target.range_high) : "?";
      return lo + "\u2013" + hi + "\u00B0C";
    }
    if (target.target_value != null) {
      return "\u2192 " + Math.round(target.target_value) + "\u00B0C";
    }
    return "";
  }

  /* Determine probe status vs target.
   *   climbing  : current is still below the effective target
   *   in-range  : range mode, current within [low, high]; fixed mode,
   *               current is within 1 degree of the target
   *   over      : above the effective target (or above the range high)
   *   unknown   : no current reading available
   */
  function classifyProbe(target, currentTempC) {
    if (currentTempC == null || !isFinite(currentTempC)) {
      return { kind: "unknown", label: "Waiting" };
    }
    if (target.mode === "range") {
      var lo = target.range_low;
      var hi = target.range_high;
      if (lo != null && currentTempC < lo) {
        return { kind: "climbing", label: "Climbing" };
      }
      if (hi != null && currentTempC > hi) {
        return { kind: "over", label: "Over" };
      }
      return { kind: "in-range", label: "In range" };
    }
    var tgt = target.target_value;
    if (tgt == null) return { kind: "unknown", label: "No target" };
    if (currentTempC > tgt) return { kind: "over", label: "Over" };
    if (tgt - currentTempC <= 1.0) return { kind: "in-range", label: "In range" };
    return { kind: "climbing", label: "Climbing" };
  }

  function currentTempForTarget(state, target) {
    /* Targets are probe-indexed (no address). Walk all devices and
     * return the first reading that matches the probe index. This is
     * fine for the single-device case that dominates today; multi-
     * device sessions get first-match semantics until a follow-up task
     * extends targets with a device binding. */
    var readings = state.readings || {};
    for (var addr in readings) {
      if (!Object.prototype.hasOwnProperty.call(readings, addr)) continue;
      var perAddr = readings[addr];
      var r = perAddr && perAddr[target.probe_index];
      if (r && typeof r.tempC === "number") {
        return { tempC: r.tempC, ts: r.ts };
      }
    }
    return null;
  }

  /* Probe palette matching the legacy chart. */
  var PROBE_COLORS = ["#e94560", "#4ade80", "#fbbf24", "#60a5fa"];
  function probeColor(probeIndex) {
    return PROBE_COLORS[(probeIndex - 1) % PROBE_COLORS.length] || "#e94560";
  }

  /* ------------------------------------------------------------------ */
  /* View                                                                */
  /* ------------------------------------------------------------------ */

  function createView() {
    var container = null;
    var unsubscribe = null;
    var tickInterval = null;
    var mounted = false;

    /* DOM refs */
    var root = null;
    var nameNode = null;        /* current name element (span or input) */
    var elapsedValueEl = null;
    var progressEl = null;
    var progressFillEl = null;
    var progressPctEl = null;
    var progressTargetEl = null;
    var chartHostEl = null;
    var chartEmptyEl = null;
    var probesGridEl = null;
    var btnEndSession = null;

    /* Chart state */
    var uplot = null;
    var chartSeriesIndexByProbe = {}; /* probeIndex -> seriesDataIndex */
    var chartData = null;             /* [timestamps, ...seriesArrays] */
    var chartStartSecs = null;
    var chartProbeSignature = "";

    /* Name edit state */
    var editingName = false;

    function getState() {
      if (!global.SessionStore || !global.SessionStore.instance) {
        return { status: "none", activeSession: null, readings: {}, devices: [] };
      }
      return global.SessionStore.instance.getState();
    }

    function sendSessionNameUpdate(newName) {
      var ws = global.__activeSessionWs || global.ws || null;
      /* Fall back to the global `ws` defined in the main IIFE. It is
       * assigned to window via `var` inside the IIFE in some bundles;
       * when not present we still update the local name optimistically
       * so Task 6's start flow + Task 11's end flow stay resilient. */
      if (!ws || ws.readyState !== 1 /* OPEN */) return;
      var state = getState();
      var sessionId = state.activeSession ? state.activeSession.id : null;
      try {
        ws.send(JSON.stringify({
          v: 2,
          type: "session_update_request",
          requestId: "asv-rename-" + Date.now(),
          payload: {
            sessionId: sessionId,
            name: newName,
          },
        }));
      } catch (e) {
        if (typeof console !== "undefined") {
          console.warn("[ActiveSessionView] session_update_request failed:", e);
        }
      }
    }

    function buildSkeleton() {
      root = el("div", "asv-root");

      /* --- Header ---------------------------------------------------- */
      var header = el("div", "asv-header");

      var headerRow = el("div", "asv-header-row");
      var nameWrap = el("div", "asv-name-wrap");
      nameNode = el("span", "asv-name");
      nameNode.setAttribute("tabindex", "0");
      nameNode.setAttribute("role", "button");
      nameNode.setAttribute("aria-label", "Edit session name");
      nameNode.addEventListener("click", beginEditName);
      nameNode.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          beginEditName();
        }
      });
      nameWrap.appendChild(nameNode);

      var elapsedBlock = el("div");
      var elapsedLabel = el("span", "asv-elapsed-label", "Elapsed");
      elapsedValueEl = el("span", "asv-elapsed", "00:00");
      elapsedBlock.appendChild(elapsedLabel);
      elapsedBlock.appendChild(elapsedValueEl);

      headerRow.appendChild(nameWrap);
      headerRow.appendChild(elapsedBlock);
      header.appendChild(headerRow);

      progressEl = el("div", "asv-progress");
      progressEl.style.display = "none";
      var progMeta = el("div", "asv-progress-meta");
      progressPctEl = el("span", null, "0%");
      progressTargetEl = el("span", null, "");
      progMeta.appendChild(progressPctEl);
      progMeta.appendChild(progressTargetEl);
      var progTrack = el("div", "asv-progress-track");
      progressFillEl = el("div", "asv-progress-fill");
      progressFillEl.style.width = "0%";
      progTrack.appendChild(progressFillEl);
      progressEl.appendChild(progMeta);
      progressEl.appendChild(progTrack);
      header.appendChild(progressEl);

      root.appendChild(header);

      /* --- Chart ----------------------------------------------------- */
      var chartCard = el("div", "asv-chart-card");
      chartCard.appendChild(el("div", "asv-chart-title", "Temperature"));
      chartHostEl = el("div", "asv-chart-host");
      chartEmptyEl = el("div", "asv-chart-empty", "Waiting for readings\u2026");
      chartCard.appendChild(chartEmptyEl);
      chartCard.appendChild(chartHostEl);
      root.appendChild(chartCard);

      /* --- Probe cards ---------------------------------------------- */
      probesGridEl = el("div", "asv-probes");
      root.appendChild(probesGridEl);

      /* --- Actions --------------------------------------------------- */
      var actions = el("div", "asv-actions");
      var btnAddProbe = el("button", "asv-btn", "Add Probe");
      btnAddProbe.type = "button";
      btnAddProbe.disabled = true;
      btnAddProbe.title = "Coming in Task 9";
      var btnAddNote = el("button", "asv-btn", "Add Note");
      btnAddNote.type = "button";
      btnAddNote.disabled = true;
      btnAddNote.title = "Coming in Task 10";
      btnEndSession = el("button", "asv-btn asv-btn-danger", "End Session");
      btnEndSession.type = "button";
      btnEndSession.disabled = true;
      btnEndSession.title = "Coming in Task 11";
      actions.appendChild(btnAddProbe);
      actions.appendChild(btnAddNote);
      actions.appendChild(btnEndSession);
      root.appendChild(actions);

      container.appendChild(root);
    }

    function beginEditName() {
      if (editingName) return;
      var state = getState();
      var current = (state.activeSession && state.activeSession.name) || "";
      editingName = true;
      var input = document.createElement("input");
      input.type = "text";
      input.className = "asv-name-input";
      input.value = current;
      input.maxLength = 200;
      input.setAttribute("aria-label", "Session name");
      nameNode.parentNode.replaceChild(input, nameNode);
      nameNode = input;
      input.focus();
      input.select();

      function commit() {
        if (!editingName) return;
        var next = (input.value || "").trim();
        editingName = false;
        if (next && next !== current) {
          sendSessionNameUpdate(next);
          /* Optimistic local update: mutate the store directly so the
           * header reflects immediately even if the server ack is slow. */
          if (global.SessionStore && global.SessionStore.instance) {
            var st = global.SessionStore.instance.getState();
            if (st.activeSession) {
              st.activeSession.name = next;
            }
          }
        }
        restoreNameSpan();
      }

      function revert() {
        editingName = false;
        restoreNameSpan();
      }

      input.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter") {
          ev.preventDefault();
          commit();
        } else if (ev.key === "Escape") {
          ev.preventDefault();
          revert();
        }
      });
      input.addEventListener("blur", commit);
    }

    function restoreNameSpan() {
      var span = el("span", "asv-name");
      span.setAttribute("tabindex", "0");
      span.setAttribute("role", "button");
      span.setAttribute("aria-label", "Edit session name");
      span.addEventListener("click", beginEditName);
      span.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          beginEditName();
        }
      });
      if (nameNode && nameNode.parentNode) {
        nameNode.parentNode.replaceChild(span, nameNode);
      }
      nameNode = span;
      renderHeader();
    }

    /* --------------------------------------------------------------- */
    /* Render passes                                                    */
    /* --------------------------------------------------------------- */

    function renderHeader() {
      var state = getState();
      var session = state.activeSession;
      if (!session) return;

      if (!editingName && nameNode) {
        nameNode.textContent = session.name || "Untitled cook";
      }

      var startSecs = sessionStartSeconds(session);
      var elapsed = startSecs != null ? nowSeconds() - startSecs : 0;
      elapsedValueEl.textContent = formatElapsed(elapsed);

      var tgtSecs = session.targetDurationSecs;
      if (tgtSecs && tgtSecs > 0) {
        progressEl.style.display = "";
        var pct = Math.max(0, (elapsed / tgtSecs) * 100);
        var fillPct = Math.min(pct, 100);
        progressFillEl.style.width = fillPct.toFixed(1) + "%";
        if (pct > 100) {
          progressFillEl.classList.add("over");
        } else {
          progressFillEl.classList.remove("over");
        }
        progressPctEl.textContent = Math.round(pct) + "%";
        progressTargetEl.textContent = "Target: " + formatElapsed(tgtSecs);
      } else {
        progressEl.style.display = "none";
      }
    }

    function renderProbeCards() {
      var state = getState();
      var session = state.activeSession;
      var targets = (session && session.targets) || [];

      while (probesGridEl.firstChild) {
        probesGridEl.removeChild(probesGridEl.firstChild);
      }

      if (targets.length === 0) {
        var empty = el("div", "asv-chart-empty", "No targets configured for this cook.");
        probesGridEl.appendChild(empty);
        return;
      }

      targets.forEach(function (t) {
        var card = el("div", "asv-probe-card");
        var label = t.label || ("Probe " + t.probe_index);
        card.appendChild(el("div", "asv-probe-label", label));

        var reading = currentTempForTarget(state, t);
        var tempEl;
        if (reading) {
          tempEl = el("div", "asv-probe-temp", reading.tempC.toFixed(1) + "\u00B0");
        } else {
          tempEl = el("div", "asv-probe-temp unplugged", "\u2014");
        }
        card.appendChild(tempEl);

        card.appendChild(el("div", "asv-probe-target", formatTarget(t)));

        var status = classifyProbe(t, reading ? reading.tempC : null);
        card.appendChild(el("span", "asv-chip " + status.kind, status.label));

        /* Task 8 replaces this stub with real timer controls. */
        card.appendChild(el("div", "asv-probe-timer", "Timer: \u2014"));

        probesGridEl.appendChild(card);
      });
    }

    /* --------------------------------------------------------------- */
    /* Chart                                                            */
    /* --------------------------------------------------------------- */

    function probeSignature(targets) {
      return targets.map(function (t) { return t.probe_index; }).sort().join(",");
    }

    function ensureChart() {
      var state = getState();
      var session = state.activeSession;
      var targets = (session && session.targets) || [];
      var sig = probeSignature(targets);

      if (targets.length === 0) {
        tearDownChart();
        chartEmptyEl.style.display = "";
        chartEmptyEl.textContent = "No targets configured for this cook.";
        chartHostEl.style.display = "none";
        return;
      }

      /* Rebuild the plot whenever the set of targeted probes changes
       * (add/remove probe). Simpler than mutating uPlot series in place
       * and correct for the skeleton pass — this happens at most once
       * per target edit. */
      if (sig !== chartProbeSignature) {
        tearDownChart();
        chartProbeSignature = sig;
        createUplot(targets);
      }

      chartStartSecs = sessionStartSeconds(session) || nowSeconds();
      refreshChartData();
    }

    function createUplot(targets) {
      if (typeof global.uPlot !== "function") {
        chartEmptyEl.style.display = "";
        chartEmptyEl.textContent = "Chart library not loaded.";
        chartHostEl.style.display = "none";
        return;
      }

      var computedStyle = getComputedStyle(document.documentElement);
      var textSecondary = computedStyle.getPropertyValue("--text-secondary").trim() || "#a0a0b8";
      var borderColor = computedStyle.getPropertyValue("--border").trim() || "#2a2a4a";

      var seriesConfig = [{}];
      chartSeriesIndexByProbe = {};
      targets.forEach(function (t, i) {
        chartSeriesIndexByProbe[t.probe_index] = i + 1;
        seriesConfig.push({
          label: t.label || ("Probe " + t.probe_index),
          stroke: probeColor(t.probe_index),
          width: 2,
          points: { show: false },
        });
      });

      chartData = [[]];
      for (var i = 0; i < targets.length; i++) chartData.push([]);

      var width = chartHostEl.clientWidth || 600;
      var opts = {
        width: width,
        height: 240,
        scales: {
          x: { time: false },
          y: { auto: true },
        },
        axes: [
          {
            stroke: textSecondary,
            grid: { stroke: borderColor, width: 1 },
            ticks: { stroke: borderColor, width: 1 },
            values: function (u, vals) {
              return vals.map(function (v) { return formatElapsed(v); });
            },
          },
          {
            stroke: textSecondary,
            grid: { stroke: borderColor, width: 1 },
            ticks: { stroke: borderColor, width: 1 },
            label: "\u00B0C",
            size: 50,
          },
        ],
        series: seriesConfig,
        cursor: { drag: { x: false, y: false } },
        legend: { show: false },
      };

      chartHostEl.style.display = "";
      chartEmptyEl.style.display = "none";
      uplot = new global.uPlot(opts, chartData, chartHostEl);
    }

    function tearDownChart() {
      if (uplot) {
        try { uplot.destroy(); } catch (e) { /* no-op */ }
        uplot = null;
      }
      if (chartHostEl) {
        while (chartHostEl.firstChild) chartHostEl.removeChild(chartHostEl.firstChild);
      }
      chartData = null;
      chartSeriesIndexByProbe = {};
      chartProbeSignature = "";
    }

    /* Rebuild chartData from the current readings map each tick. For a
     * skeleton view this is cheap — it's a single pass per probe per
     * 1Hz redraw — and avoids the bookkeeping that an append-only
     * buffer would need. Later we can swap to incremental updates if
     * profiling shows it's worth it. */
    function refreshChartData() {
      if (!uplot || !chartData || !chartStartSecs) {
        var hasAnyData = false;
        if (chartEmptyEl) {
          chartEmptyEl.textContent = "Waiting for readings\u2026";
          chartEmptyEl.style.display = hasAnyData ? "none" : "";
        }
        return;
      }

      var state = getState();
      var session = state.activeSession;
      var targets = (session && session.targets) || [];
      var readings = state.readings || {};

      /* Collect per-probe time series from the rolling buffer if
       * available (it retains the last 3 minutes), otherwise fall back
       * to just the most recent point. */
      var buffer = global.RollingBuffer && global.RollingBuffer.instance;

      /* Merge timestamps across probes into one sorted axis. uPlot
       * accepts a single X array shared by all Y series. */
      var tsSet = Object.create(null);
      var perProbeSeries = {};
      targets.forEach(function (t) {
        /* Targets are not bound to a specific device today (they only
         * carry probe_index). Walk devices in insertion order and take
         * the first one that has readings for this probe index. Multi-
         * device sessions with colliding probe indices get first-match
         * semantics until the server model grows a device binding. */
        var series = [];
        var addrs = Object.keys(readings);
        for (var a = 0; a < addrs.length; a++) {
          var addr = addrs[a];
          if (buffer) {
            var s = buffer.getSeries(addr, t.probe_index);
            if (s && s.length) {
              series = s.slice();
              break;
            }
          }
          var r = readings[addr] && readings[addr][t.probe_index];
          if (r) {
            series.push({ ts: r.ts, tempC: r.tempC });
            break;
          }
        }
        perProbeSeries[t.probe_index] = series;
        for (var i = 0; i < series.length; i++) {
          tsSet[series[i].ts] = true;
        }
      });

      var timestamps = Object.keys(tsSet)
        .map(function (v) { return +v; })
        .sort(function (a, b) { return a - b; });

      if (timestamps.length === 0) {
        if (chartEmptyEl) {
          chartEmptyEl.textContent = "Waiting for readings\u2026";
          chartEmptyEl.style.display = "";
        }
        chartHostEl.style.display = "none";
        return;
      }

      chartEmptyEl.style.display = "none";
      chartHostEl.style.display = "";

      /* Convert absolute timestamps to "seconds since session start". */
      var xs = timestamps.map(function (t) { return t - chartStartSecs; });

      var newData = [xs];
      targets.forEach(function (t) {
        var seriesByTs = {};
        var arr = perProbeSeries[t.probe_index] || [];
        for (var i = 0; i < arr.length; i++) {
          seriesByTs[arr[i].ts] = arr[i].tempC;
        }
        var col = timestamps.map(function (ts) {
          return seriesByTs[ts] != null ? seriesByTs[ts] : null;
        });
        newData.push(col);
      });

      chartData = newData;
      try {
        uplot.setData(chartData);
      } catch (e) {
        /* Defensive: a resize during tab switching can throw. */
        if (typeof console !== "undefined") {
          console.warn("[ActiveSessionView] uplot.setData failed:", e);
        }
      }
    }

    function handleResize() {
      if (!uplot || !chartHostEl) return;
      var w = chartHostEl.clientWidth;
      if (w > 0) {
        try { uplot.setSize({ width: w, height: 240 }); } catch (e) { /* no-op */ }
      }
    }

    /* --------------------------------------------------------------- */
    /* Mount / unmount                                                  */
    /* --------------------------------------------------------------- */

    function fullRender() {
      if (!mounted) return;
      renderHeader();
      ensureChart();
      renderProbeCards();
    }

    function onStoreChange() {
      if (!mounted) return;
      var state = getState();
      if (state.status !== "active") {
        /* IIFE owns show/hide of the placeholder; when it hides we
         * just stop doing work. The next mount call will rebuild. */
        return;
      }
      fullRender();
    }

    function onTick() {
      if (!mounted) return;
      /* Recompute elapsed/progress from startedAt each tick so that
       * background-tab throttling doesn't drift the clock. Also nudge
       * the chart so the X axis keeps pace even in quiet periods. */
      renderHeader();
      refreshChartData();
    }

    function mount(target) {
      if (mounted) return;
      ensureStyles();
      container = target;
      while (container.firstChild) container.removeChild(container.firstChild);
      buildSkeleton();
      mounted = true;

      if (global.SessionStore && global.SessionStore.instance) {
        unsubscribe = global.SessionStore.instance.subscribe(onStoreChange);
      }
      tickInterval = setInterval(onTick, 1000);
      global.addEventListener("resize", handleResize);
      fullRender();
    }

    function unmount() {
      if (!mounted) return;
      mounted = false;
      if (unsubscribe) { try { unsubscribe(); } catch (e) { /* no-op */ } unsubscribe = null; }
      if (tickInterval) { clearInterval(tickInterval); tickInterval = null; }
      global.removeEventListener("resize", handleResize);
      tearDownChart();
      if (container) {
        while (container.firstChild) container.removeChild(container.firstChild);
      }
      container = null;
      root = null;
      editingName = false;
    }

    return {
      mount: mount,
      unmount: unmount,
      /* Exposed for tests/debugging. */
      _internal: {
        formatElapsed: formatElapsed,
        formatTarget: formatTarget,
        classifyProbe: classifyProbe,
        sessionStartSeconds: sessionStartSeconds,
      },
    };
  }

  var singleton = createView();

  global.ActiveSessionView = {
    mount: singleton.mount,
    unmount: singleton.unmount,
    create: createView,
    _internal: singleton._internal,
  };
})(typeof window !== "undefined" ? window : this);
