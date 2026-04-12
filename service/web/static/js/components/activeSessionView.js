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
 *   4. Action row        -- Add Probe / Add Note / End Session buttons
 *                           (Tasks 9, 10, 11). Clicking delegates to
 *                           AddProbeFlow, NotesEditor, EndSessionFlow
 *                           respectively; wiring is passed in via
 *                           mount(target, {send, onMessage,
 *                           generateRequestId, getDevices}).
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
    ".asv-chip.done{background:rgba(74,222,128,0.18);color:var(--green,#4ade80);}",
    ".asv-probe-timer{display:flex;flex-direction:column;gap:0.35rem;margin-top:0.2rem;padding-top:0.5rem;border-top:1px solid var(--border,#2a2a4a);}",
    ".asv-timer-row{display:flex;align-items:center;gap:0.45rem;flex-wrap:wrap;}",
    ".asv-timer-label{font-size:0.7rem;color:var(--text-secondary,#a0a0b8);text-transform:uppercase;letter-spacing:0.06em;}",
    ".asv-timer-value{font-size:1rem;font-weight:600;color:var(--text-primary,#e0e0e0);font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;font-variant-numeric:tabular-nums;font-feature-settings:\"tnum\" 1;letter-spacing:0.02em;}",
    ".asv-timer-btn{background:var(--bg-secondary,#16213e);color:var(--text-primary,#e0e0e0);border:1px solid var(--border,#2a2a4a);border-radius:6px;padding:0.25rem 0.55rem;font-size:0.72rem;font-family:inherit;cursor:pointer;line-height:1.2;}",
    ".asv-timer-btn:hover{background:var(--bg-card-hover,#134074);}",
    ".asv-timer-btn:focus-visible{outline:2px solid var(--brand,#935240);outline-offset:2px;}",
    ".asv-timer-btn.primary{background:var(--brand,#935240);border-color:var(--brand,#935240);color:#fff;}",
    ".asv-timer-btn.primary:hover{background:var(--brand-hover,#a46352);}",
    ".asv-timer-add{background:transparent;border:none;color:var(--brand,#935240);font-size:0.8rem;font-family:inherit;cursor:pointer;padding:0.1rem 0;align-self:flex-start;text-decoration:underline dotted;text-underline-offset:3px;}",
    ".asv-timer-add:hover{color:var(--brand-hover,#a46352);}",
    ".asv-timer-add:focus-visible{outline:2px solid var(--brand,#935240);outline-offset:2px;border-radius:4px;}",
    ".asv-timer-picker{display:flex;flex-direction:column;gap:0.4rem;padding:0.5rem;background:var(--bg-secondary,#16213e);border:1px solid var(--border,#2a2a4a);border-radius:6px;}",
    ".asv-timer-picker-row{display:flex;gap:0.4rem;align-items:center;flex-wrap:wrap;}",
    ".asv-timer-picker label{font-size:0.72rem;color:var(--text-secondary,#a0a0b8);}",
    ".asv-timer-picker select,.asv-timer-picker input{background:var(--bg-card,#0f3460);color:var(--text-primary,#e0e0e0);border:1px solid var(--border,#2a2a4a);border-radius:4px;padding:0.2rem 0.35rem;font-size:0.8rem;font-family:inherit;}",
    ".asv-timer-picker input.duration{width:7rem;font-variant-numeric:tabular-nums;}",
    ".asv-timer-picker-actions{display:flex;gap:0.35rem;justify-content:flex-end;}",
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

  /* ------------------------------------------------------------------ */
  /* Timer helpers                                                       */
  /* ------------------------------------------------------------------ */

  /* Parse an ISO-ish timestamp to epoch seconds, tolerating the Python
   * `now_iso_utc()` format (which may or may not include a timezone). */
  function parseIsoSeconds(value) {
    if (value == null) return null;
    if (typeof value === "number") {
      return value > 1e12 ? value / 1000 : value;
    }
    if (typeof value !== "string") return null;
    var s = value;
    /* If no timezone suffix present, assume UTC (server writes UTC). */
    if (!/[zZ]$|[+\-]\d{2}:?\d{2}$/.test(s)) s += "Z";
    var ms = Date.parse(s);
    if (isNaN(ms)) return null;
    return ms / 1000;
  }

  /* Format seconds as mm:ss when under an hour, hh:mm:ss otherwise. */
  function formatTimerDisplay(secs) {
    if (secs == null || !isFinite(secs) || secs < 0) secs = 0;
    secs = Math.floor(secs);
    var h = Math.floor(secs / 3600);
    var m = Math.floor((secs % 3600) / 60);
    var s = secs % 60;
    function pad(n) { return (n < 10 ? "0" : "") + n; }
    if (h > 0) return h + ":" + pad(m) + ":" + pad(s);
    return pad(m) + ":" + pad(s);
  }

  /* Compute the effective elapsed accumulated seconds for a timer row.
   * This is `accumulatedSecs + (now - startedAt)` if running, else just
   * `accumulatedSecs`. */
  function effectiveAccumulated(timer) {
    var acc = timer.accumulatedSecs || 0;
    if (timer.startedAt && !timer.pausedAt && !timer.completedAt) {
      var startSecs = parseIsoSeconds(timer.startedAt);
      if (startSecs != null) {
        var delta = nowSeconds() - startSecs;
        if (delta > 0) acc += delta;
      }
    }
    return acc;
  }

  /* Compute the current display seconds for a timer row (mode-aware). */
  function timerDisplaySecs(timer) {
    var acc = effectiveAccumulated(timer);
    if (timer.mode === "count_down") {
      var d = timer.durationSecs || 0;
      return Math.max(0, d - acc);
    }
    return acc;
  }

  function timerIsRunning(timer) {
    return !!(timer && timer.startedAt && !timer.pausedAt && !timer.completedAt);
  }

  function timerHasProgress(timer) {
    if (!timer) return false;
    if ((timer.accumulatedSecs || 0) > 0) return true;
    return timerIsRunning(timer);
  }

  /* Parse `mm:ss` or `hh:mm:ss` into total seconds. Returns null on bad
   * input. Also accepts plain integers (treated as seconds). */
  function parseDurationInput(value) {
    if (value == null) return null;
    var trimmed = String(value).trim();
    if (!trimmed) return null;
    if (/^\d+$/.test(trimmed)) {
      var n = parseInt(trimmed, 10);
      return n >= 0 ? n : null;
    }
    var parts = trimmed.split(":").map(function (p) { return p.trim(); });
    if (parts.length < 2 || parts.length > 3) return null;
    for (var i = 0; i < parts.length; i++) {
      if (!/^\d+$/.test(parts[i])) return null;
    }
    var total = 0;
    if (parts.length === 2) {
      total = parseInt(parts[0], 10) * 60 + parseInt(parts[1], 10);
    } else {
      total = parseInt(parts[0], 10) * 3600 +
              parseInt(parts[1], 10) * 60 +
              parseInt(parts[2], 10);
    }
    return total > 0 ? total : null;
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
    var unsubscribeMessages = null;
    var tickInterval = null;
    var mounted = false;

    /* Wiring injected by mount(target, options). Lets the legacy
     * dashboard hand us its `send`, `onMessage`, `generateRequestId`,
     * and a `getDevices()` snapshot function without the component
     * needing to know how the WebSocket is owned. */
    var wiring = {
      send: null,
      onMessage: null,
      generateRequestId: null,
      getDevices: null,
      temperatureUnit: "C",
    };

    /* Tracks the currently-open NotesEditor instance so we can push
     * live `session_notes_update` broadcasts into it. */
    var openNotesEditor = null;

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
    var btnAddProbe = null;
    var btnAddNote = null;
    var btnEndSession = null;

    /* Chart state */
    var uplot = null;
    var chartSeriesIndexByProbe = {}; /* probeIndex -> seriesDataIndex */
    var chartData = null;             /* [timestamps, ...seriesArrays] */
    var chartStartSecs = null;
    var chartProbeSignature = "";

    /* Name edit state */
    var editingName = false;

    /* Timer UI state.
     *
     * `timerPickerOpenFor` — set of probe indices whose inline upsert
     *   picker is currently visible. Survives re-renders so that the
     *   picker stays open across tick redraws (the tick loop skips a full
     *   probe-cards rebuild; it only updates value elements in-place). */
    var timerPickerOpenFor = {};
    /* Per-probe-index refs to the live value DOM node so onTick can
     * update them without rebuilding the card tree. Populated on each
     * full probe-card render and consulted each 1Hz tick. */
    var timerValueRefs = {};

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

    function getWs() {
      return global.__activeSessionWs || global.ws || null;
    }

    /* Pick the "default" device address for probe timers.
     *
     * Targets today only carry `probe_index` (no device binding), so we
     * mirror `currentTempForTarget`'s first-match semantics: prefer the
     * session's devices array, fall back to any address we have readings
     * for, then fall back to an existing timer row (if the user already
     * configured one for this probe index). */
    function resolveTimerAddress(state, probeIndex) {
      var timers = (state.activeSession && state.activeSession.timers) || [];
      for (var i = 0; i < timers.length; i++) {
        if (timers[i] && timers[i].probeIndex === probeIndex && timers[i].address) {
          return timers[i].address;
        }
      }
      var devices = state.devices || [];
      for (var j = 0; j < devices.length; j++) {
        var d = devices[j];
        if (d && d.address) return d.address;
        if (typeof d === "string" && d) return d;
      }
      var readings = state.readings || {};
      for (var addr in readings) {
        if (Object.prototype.hasOwnProperty.call(readings, addr)) return addr;
      }
      return null;
    }

    function findTimerForProbe(state, probeIndex) {
      var timers = (state.activeSession && state.activeSession.timers) || [];
      for (var i = 0; i < timers.length; i++) {
        /* Use the first timer whose probeIndex matches, mirroring the
         * first-match semantics used elsewhere for address binding. */
        if (timers[i] && timers[i].probeIndex === probeIndex) return timers[i];
      }
      return null;
    }

    function sendProbeTimerRequest(payload) {
      var ws = getWs();
      if (!ws || ws.readyState !== 1 /* OPEN */) return;
      try {
        ws.send(JSON.stringify({
          v: 2,
          type: "probe_timer_request",
          requestId: "asv-timer-" + Date.now() + "-" + Math.random().toString(36).slice(2, 6),
          payload: payload,
        }));
      } catch (e) {
        if (typeof console !== "undefined") {
          console.warn("[ActiveSessionView] probe_timer_request failed:", e);
        }
      }
    }

    /* --------------------------------------------------------------- */
    /* Action-row handlers (Tasks 9, 10, 11)                            */
    /* --------------------------------------------------------------- */

    function ensureWiring() {
      if (wiring.send && wiring.onMessage && wiring.generateRequestId) return true;
      if (typeof console !== "undefined") {
        console.warn(
          "[ActiveSessionView] action requires wiring: " +
          "mount the view with {send, onMessage, generateRequestId} options."
        );
      }
      return false;
    }

    function onAddProbeClick() {
      if (!ensureWiring()) return;
      if (!global.AddProbeFlow) {
        if (typeof console !== "undefined") {
          console.warn("[ActiveSessionView] AddProbeFlow not loaded");
        }
        return;
      }
      var state = getState();
      var session = state.activeSession;
      if (!session) return;
      var devicesMap = (typeof wiring.getDevices === "function" && wiring.getDevices()) || {};
      global.AddProbeFlow.open({
        devices: devicesMap,
        currentTargets: session.targets || [],
        sessionDevices: state.devices || [],
        send: wiring.send,
        generateRequestId: wiring.generateRequestId,
        onMessage: wiring.onMessage,
        temperatureUnit: wiring.temperatureUnit || "C",
      });
    }

    function onAddNoteClick() {
      if (!ensureWiring()) return;
      if (!global.NotesEditor) {
        if (typeof console !== "undefined") {
          console.warn("[ActiveSessionView] NotesEditor not loaded");
        }
        return;
      }
      var state = getState();
      var session = state.activeSession;
      if (!session) return;
      var sessionId = session.id;

      var handle = global.NotesEditor.open({
        sessionId: sessionId,
        initialBody: state.notes || "",
        onSave: function (body) {
          wiring.send({
            v: 2,
            type: "session_notes_update_request",
            requestId: wiring.generateRequestId(),
            payload: {
              body: body,
              sessionId: sessionId,
            },
          });
        },
      });

      openNotesEditor = handle;

      /* The NotesEditor doesn't notify us on close, so wrap its close
       * function to clear our tracking ref. */
      var originalClose = handle.close;
      handle.close = function () {
        openNotesEditor = null;
        originalClose();
      };
    }

    function onEndSessionClick() {
      if (!ensureWiring()) return;
      if (!global.EndSessionFlow) {
        if (typeof console !== "undefined") {
          console.warn("[ActiveSessionView] EndSessionFlow not loaded");
        }
        return;
      }
      global.EndSessionFlow.open({
        send: wiring.send,
        generateRequestId: wiring.generateRequestId,
        onMessage: wiring.onMessage,
      });
    }

    /* Listen for notes broadcasts so the open editor (if any) can be
     * updated live when another client edits the same note. The
     * reducer already updates `state.notes`; we mirror that into the
     * NotesEditor textarea (it protects against clobbering unsaved
     * local edits internally). */
    function onWsMessage(msg) {
      if (!msg || typeof msg !== "object") return;
      if (!openNotesEditor) return;
      if (msg.type !== "session_notes_update") return;
      var payload = msg.payload || {};
      var note = payload.note || payload;
      if (note && typeof note.body === "string") {
        try { openNotesEditor.setBody(note.body); } catch (_) { /* no-op */ }
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
      btnAddProbe = el("button", "asv-btn", "Add Probe");
      btnAddProbe.type = "button";
      btnAddProbe.addEventListener("click", onAddProbeClick);
      btnAddNote = el("button", "asv-btn", "Add Note");
      btnAddNote.type = "button";
      btnAddNote.addEventListener("click", onAddNoteClick);
      btnEndSession = el("button", "asv-btn asv-btn-danger", "End Session");
      btnEndSession.type = "button";
      btnEndSession.addEventListener("click", onEndSessionClick);
      /* Buttons remain enabled by default; the handlers surface a
       * console warning if wiring is missing, which only happens when
       * the view is embedded somewhere outside the main dashboard. */
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
      /* Drop stale refs; we're rebuilding the whole grid. The live
       * tick-updated nodes will be re-registered below. */
      timerValueRefs = {};

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

        card.appendChild(renderTimerSection(state, t, label));

        probesGridEl.appendChild(card);
      });
    }

    /* Build the timer subsection for a probe card. Returns a DOM node
     * to append to the card. */
    function renderTimerSection(state, target, probeLabel) {
      var probeIndex = target.probe_index;
      var timer = findTimerForProbe(state, probeIndex);
      var wrap = el("div", "asv-probe-timer");

      /* Picker open? Show picker (plus any existing timer summary above
       * it). The user can dismiss by clicking Cancel. */
      var pickerOpen = !!timerPickerOpenFor[probeIndex];

      if (!timer) {
        if (pickerOpen) {
          wrap.appendChild(renderTimerPicker(probeIndex, probeLabel, null));
        } else {
          var addBtn = el("button", "asv-timer-add", "Add timer");
          addBtn.type = "button";
          addBtn.setAttribute(
            "aria-label", "Add timer for " + probeLabel
          );
          addBtn.addEventListener("click", function () {
            timerPickerOpenFor[probeIndex] = true;
            renderProbeCards();
          });
          wrap.appendChild(addBtn);
        }
        return wrap;
      }

      /* We have a timer row. Decide which buttons to show. */
      var completed = !!timer.completedAt;
      var running = timerIsRunning(timer);
      var hasProgress = timerHasProgress(timer);
      var mode = timer.mode;

      /* Label line: "Count up" or "1:30:00 countdown" */
      var labelText;
      if (mode === "count_down") {
        labelText = formatTimerDisplay(timer.durationSecs || 0) + " countdown";
      } else {
        labelText = "Count up";
      }
      wrap.appendChild(el("div", "asv-timer-label", labelText));

      var displayRow = el("div", "asv-timer-row");
      var valueEl;
      if (completed) {
        valueEl = el("span", "asv-timer-value", "Done");
        displayRow.appendChild(valueEl);
        displayRow.appendChild(el("span", "asv-chip done", "Completed"));
      } else {
        valueEl = el("span", "asv-timer-value", formatTimerDisplay(timerDisplaySecs(timer)));
        displayRow.appendChild(valueEl);
      }
      wrap.appendChild(displayRow);

      /* Track the live value node for the tick loop; only required for
       * running (non-completed) timers, but we track paused ones too so
       * a resume from the server refreshes them correctly via the full
       * re-render path. */
      timerValueRefs[probeIndex] = {
        node: valueEl,
        timer: timer,
      };

      var controls = el("div", "asv-timer-row");
      var probeName = probeLabel || ("probe " + probeIndex);

      function mkBtn(text, primary, ariaSuffix, action) {
        var b = el("button", "asv-timer-btn" + (primary ? " primary" : ""), text);
        b.type = "button";
        b.setAttribute("aria-label", ariaSuffix + " " + probeName + " timer");
        b.addEventListener("click", function () {
          var st = getState();
          var address = timer.address || resolveTimerAddress(st, probeIndex);
          if (!address) return;
          sendProbeTimerRequest({
            address: address,
            probe_index: probeIndex,
            action: action,
          });
        });
        return b;
      }

      if (completed) {
        controls.appendChild(mkBtn("Reset", false, "Reset", "reset"));
      } else if (running) {
        controls.appendChild(mkBtn("Pause", false, "Pause", "pause"));
        controls.appendChild(mkBtn("Reset", false, "Reset", "reset"));
      } else if (!hasProgress) {
        /* Paused and never started. Offer Start (primary) + Reset only
         * for count_down so users can re-open the picker to change the
         * duration via a separate "Edit" affordance (not in this task). */
        controls.appendChild(mkBtn("Start", true, "Start", "start"));
        if (mode === "count_down") {
          controls.appendChild(mkBtn("Reset", false, "Reset", "reset"));
        }
      } else {
        /* Paused mid-run. */
        controls.appendChild(mkBtn("Resume", true, "Resume", "resume"));
        controls.appendChild(mkBtn("Reset", false, "Reset", "reset"));
      }
      wrap.appendChild(controls);

      if (pickerOpen) {
        wrap.appendChild(renderTimerPicker(probeIndex, probeLabel, timer));
      }

      return wrap;
    }

    /* Build the inline upsert picker for a probe. `existing` is the
     * current timer row (if any) so we can pre-fill mode/duration. */
    function renderTimerPicker(probeIndex, probeLabel, existing) {
      var picker = el("div", "asv-timer-picker");

      var modeRow = el("div", "asv-timer-picker-row");
      var modeLabel = el("label", null, "Mode");
      var modeSelect = document.createElement("select");
      modeSelect.setAttribute("aria-label", "Timer mode for " + probeLabel);
      var optUp = document.createElement("option");
      optUp.value = "count_up";
      optUp.textContent = "Count up";
      var optDown = document.createElement("option");
      optDown.value = "count_down";
      optDown.textContent = "Count down";
      modeSelect.appendChild(optUp);
      modeSelect.appendChild(optDown);
      modeSelect.value = (existing && existing.mode) || "count_down";
      modeLabel.appendChild(modeSelect);
      modeRow.appendChild(modeLabel);
      picker.appendChild(modeRow);

      var durRow = el("div", "asv-timer-picker-row");
      var durLabel = el("label", null, "Duration");
      var durInput = document.createElement("input");
      durInput.type = "text";
      durInput.className = "duration";
      durInput.placeholder = "mm:ss or hh:mm:ss";
      durInput.setAttribute("aria-label", "Timer duration for " + probeLabel);
      if (existing && existing.durationSecs) {
        durInput.value = formatTimerDisplay(existing.durationSecs);
      } else {
        durInput.value = "30:00";
      }
      durLabel.appendChild(durInput);
      durRow.appendChild(durLabel);
      picker.appendChild(durRow);

      function syncDurationVisibility() {
        durRow.style.display = modeSelect.value === "count_down" ? "" : "none";
      }
      modeSelect.addEventListener("change", syncDurationVisibility);
      syncDurationVisibility();

      var actions = el("div", "asv-timer-picker-actions");
      var cancel = el("button", "asv-timer-btn", "Cancel");
      cancel.type = "button";
      cancel.setAttribute("aria-label", "Cancel timer setup for " + probeLabel);
      cancel.addEventListener("click", function () {
        delete timerPickerOpenFor[probeIndex];
        renderProbeCards();
      });
      var confirm = el("button", "asv-timer-btn primary", "Save");
      confirm.type = "button";
      confirm.setAttribute("aria-label", "Save timer for " + probeLabel);
      confirm.addEventListener("click", function () {
        var mode = modeSelect.value;
        var payload = {
          probe_index: probeIndex,
          action: "upsert",
          mode: mode,
        };
        if (mode === "count_down") {
          var secs = parseDurationInput(durInput.value);
          if (!secs) {
            durInput.focus();
            durInput.select();
            return;
          }
          payload.duration_secs = secs;
        }
        var st = getState();
        var address = resolveTimerAddress(st, probeIndex);
        if (!address) return;
        payload.address = address;
        sendProbeTimerRequest(payload);
        delete timerPickerOpenFor[probeIndex];
        renderProbeCards();
      });
      actions.appendChild(cancel);
      actions.appendChild(confirm);
      picker.appendChild(actions);

      return picker;
    }

    /* Lightweight per-tick update for running timer displays. Avoids
     * rebuilding probe cards every second; just updates the textContent
     * of each live value node. */
    function tickTimers() {
      for (var k in timerValueRefs) {
        if (!Object.prototype.hasOwnProperty.call(timerValueRefs, k)) continue;
        var ref = timerValueRefs[k];
        if (!ref || !ref.node || !ref.timer) continue;
        var t = ref.timer;
        if (t.completedAt) continue;
        if (!timerIsRunning(t)) continue;
        ref.node.textContent = formatTimerDisplay(timerDisplaySecs(t));
      }
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
       * the chart so the X axis keeps pace even in quiet periods, and
       * update any running probe timers in-place. */
      renderHeader();
      refreshChartData();
      tickTimers();
    }

    function mount(target, options) {
      if (mounted) return;
      ensureStyles();
      options = options || {};
      wiring.send = typeof options.send === "function" ? options.send : null;
      wiring.onMessage = typeof options.onMessage === "function" ? options.onMessage : null;
      wiring.generateRequestId = typeof options.generateRequestId === "function"
        ? options.generateRequestId
        : null;
      wiring.getDevices = typeof options.getDevices === "function" ? options.getDevices : null;
      wiring.temperatureUnit = options.temperatureUnit === "F" ? "F" : "C";

      container = target;
      while (container.firstChild) container.removeChild(container.firstChild);
      buildSkeleton();
      mounted = true;

      if (global.SessionStore && global.SessionStore.instance) {
        unsubscribe = global.SessionStore.instance.subscribe(onStoreChange);
      }
      if (wiring.onMessage) {
        unsubscribeMessages = wiring.onMessage(onWsMessage);
      }
      tickInterval = setInterval(onTick, 1000);
      global.addEventListener("resize", handleResize);
      fullRender();
    }

    function unmount() {
      if (!mounted) return;
      mounted = false;
      if (unsubscribe) { try { unsubscribe(); } catch (e) { /* no-op */ } unsubscribe = null; }
      if (unsubscribeMessages) {
        try { unsubscribeMessages(); } catch (e) { /* no-op */ }
        unsubscribeMessages = null;
      }
      if (tickInterval) { clearInterval(tickInterval); tickInterval = null; }
      global.removeEventListener("resize", handleResize);
      tearDownChart();
      if (openNotesEditor) {
        try { openNotesEditor.close(); } catch (_) { /* no-op */ }
        openNotesEditor = null;
      }
      if (container) {
        while (container.firstChild) container.removeChild(container.firstChild);
      }
      container = null;
      root = null;
      editingName = false;
      timerPickerOpenFor = {};
      timerValueRefs = {};
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
        formatTimerDisplay: formatTimerDisplay,
        parseDurationInput: parseDurationInput,
        parseIsoSeconds: parseIsoSeconds,
        timerDisplaySecs: timerDisplaySecs,
        effectiveAccumulated: effectiveAccumulated,
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
