/**
 * ProbeTargetConfigSheet
 * ----------------------
 *
 * Reusable modal sheet that configures a target (plus optional timer)
 * for a single probe. Opened at Start Cook time and when adding a
 * probe mid-session.
 *
 * Usage:
 *   ProbeTargetConfigSheet.open({
 *     probe: { address, probeIndex, label },
 *     existing: <TargetConfig | undefined>,
 *     temperatureUnit: 'C' | 'F',
 *   }).then(function (config) {
 *     if (config === null) { // cancelled }
 *     else { // confirmed TargetConfig }
 *   });
 *
 * Returned `TargetConfig` shape (temps always in celsius):
 *   {
 *     probe_index, mode: 'fixed'|'range',
 *     target_value?, range_low?, range_high?,
 *     pre_alert_offset, reminder_interval_secs,
 *     label?,
 *     timer?: { mode: 'count_up'|'count_down', duration_secs? }
 *   }
 *
 * Vanilla JS / IIFE style to match the rest of the dashboard (no
 * bundler, no React). Attaches to `window.ProbeTargetConfigSheet`.
 */
(function (global) {
  "use strict";

  /* Injected once on first open so multiple opens reuse the same
   * stylesheet. Styles scope every rule under `.ptcs-root`. */
  var STYLE_ID = "ptcs-styles";
  var STYLE_CSS = [
    ".ptcs-root{position:fixed;inset:0;z-index:100;display:flex;align-items:center;justify-content:center;font-family:inherit;}",
    ".ptcs-backdrop{position:absolute;inset:0;background:rgba(0,0,0,0.55);backdrop-filter:blur(2px);}",
    ".ptcs-card{position:relative;background:var(--bg-secondary,#16213e);color:var(--text-primary,#e0e0e0);border:1px solid var(--border,#2a2a4a);border-radius:var(--radius,10px);padding:1.25rem;width:min(92vw,420px);max-height:90vh;overflow-y:auto;box-shadow:0 20px 60px rgba(0,0,0,0.55);}",
    ".ptcs-title{font-size:1.05rem;font-weight:600;margin-bottom:0.25rem;}",
    ".ptcs-subtitle{font-size:0.85rem;color:var(--text-secondary,#a0a0b8);margin-bottom:1rem;}",
    ".ptcs-field{display:flex;flex-direction:column;gap:0.35rem;margin-bottom:0.9rem;}",
    ".ptcs-label{font-size:0.8rem;font-weight:500;color:var(--text-secondary,#a0a0b8);text-transform:uppercase;letter-spacing:0.04em;}",
    ".ptcs-input,.ptcs-select{background:var(--bg-card,#0f3460);color:var(--text-primary,#e0e0e0);border:1px solid var(--border,#2a2a4a);border-radius:8px;padding:0.55rem 0.7rem;font-size:0.95rem;font-family:inherit;width:100%;}",
    ".ptcs-input:focus,.ptcs-select:focus{outline:2px solid var(--brand,#935240);outline-offset:0;border-color:var(--brand,#935240);}",
    ".ptcs-row{display:flex;gap:0.5rem;}",
    ".ptcs-row>*{flex:1;}",
    ".ptcs-seg{display:flex;background:var(--bg-card,#0f3460);border:1px solid var(--border,#2a2a4a);border-radius:8px;padding:3px;gap:3px;}",
    ".ptcs-seg-btn{flex:1;background:transparent;color:var(--text-secondary,#a0a0b8);border:none;border-radius:6px;padding:0.45rem 0.5rem;font-size:0.9rem;font-family:inherit;cursor:pointer;transition:background 0.15s,color 0.15s;}",
    ".ptcs-seg-btn:hover{color:var(--text-primary,#e0e0e0);}",
    ".ptcs-seg-btn.active{background:var(--brand,#935240);color:#fff;}",
    ".ptcs-seg-btn:focus-visible{outline:2px solid var(--brand,#935240);outline-offset:1px;}",
    ".ptcs-actions{display:flex;gap:0.5rem;justify-content:flex-end;margin-top:1rem;}",
    ".ptcs-btn{background:var(--bg-card,#0f3460);color:var(--text-primary,#e0e0e0);border:1px solid var(--border,#2a2a4a);border-radius:8px;padding:0.55rem 1rem;font-size:0.95rem;font-family:inherit;cursor:pointer;}",
    ".ptcs-btn:hover{background:var(--bg-card-hover,#134074);}",
    ".ptcs-btn-primary{background:var(--brand,#935240);border-color:var(--brand,#935240);color:#fff;}",
    ".ptcs-btn-primary:hover{filter:brightness(1.08);}",
    ".ptcs-btn-primary:disabled{opacity:0.5;cursor:not-allowed;filter:none;}",
    ".ptcs-btn:focus-visible{outline:2px solid var(--brand,#935240);outline-offset:2px;}",
    ".ptcs-hint{font-size:0.8rem;color:var(--text-muted,#6c6c80);margin-top:0.25rem;}",
    ".ptcs-error{font-size:0.8rem;color:var(--red,#f87171);margin-top:0.25rem;min-height:1em;}",
    ".ptcs-timer-group{border:1px solid var(--border,#2a2a4a);border-radius:8px;padding:0.75rem;background:var(--bg-card,#0f3460);}",
    "@media (prefers-color-scheme: light){.ptcs-card{box-shadow:0 20px 60px rgba(0,0,0,0.25);}}",
  ].join("\n");

  function ensureStyles() {
    if (typeof document === "undefined") return;
    if (document.getElementById(STYLE_ID)) return;
    var s = document.createElement("style");
    s.id = STYLE_ID;
    s.textContent = STYLE_CSS;
    document.head.appendChild(s);
  }

  /* Temperature conversion helpers. Server stores/returns celsius. */
  function cToF(c) {
    return c * 9 / 5 + 32;
  }
  function fToC(f) {
    return (f - 32) * 5 / 9;
  }
  function toDisplay(c, unit) {
    if (c == null || isNaN(c)) return "";
    var v = unit === "F" ? cToF(c) : c;
    /* Round to 1 decimal for display but keep integer when exact. */
    return Math.round(v * 10) / 10;
  }
  function fromDisplay(v, unit) {
    var n = parseFloat(v);
    if (!isFinite(n)) return NaN;
    return unit === "F" ? fToC(n) : n;
  }

  var REMINDER_OPTIONS = [
    { label: "Off", value: 0 },
    { label: "1 min", value: 60 },
    { label: "5 min", value: 300 },
    { label: "15 min", value: 900 },
  ];

  /* Parse "mm:ss" or "hh:mm:ss" or plain seconds into seconds. */
  function parseDuration(str) {
    if (str == null) return NaN;
    var trimmed = String(str).trim();
    if (trimmed === "") return NaN;
    if (trimmed.indexOf(":") === -1) {
      var n = parseInt(trimmed, 10);
      return isFinite(n) ? n : NaN;
    }
    var parts = trimmed.split(":").map(function (p) {
      return parseInt(p, 10);
    });
    if (parts.some(function (p) { return !isFinite(p) || p < 0; })) return NaN;
    if (parts.length === 2) return parts[0] * 60 + parts[1];
    if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
    return NaN;
  }

  function formatDuration(secs) {
    if (secs == null || !isFinite(secs) || secs <= 0) return "";
    var h = Math.floor(secs / 3600);
    var m = Math.floor((secs % 3600) / 60);
    var s = secs % 60;
    function pad(n) { return n < 10 ? "0" + n : String(n); }
    if (h > 0) return h + ":" + pad(m) + ":" + pad(s);
    return m + ":" + pad(s);
  }

  /* --------------------------------------------------------------- */
  /* Focus trap                                                      */
  /* --------------------------------------------------------------- */

  var FOCUSABLE =
    'a[href],area[href],input:not([disabled]),select:not([disabled]),' +
    'textarea:not([disabled]),button:not([disabled]),iframe,' +
    '[tabindex]:not([tabindex="-1"])';

  function getFocusable(root) {
    return Array.prototype.slice.call(root.querySelectorAll(FOCUSABLE));
  }

  /* --------------------------------------------------------------- */
  /* State holder for the currently-open modal                        */
  /* --------------------------------------------------------------- */

  var currentInstance = null;

  function closeWith(result) {
    if (!currentInstance) return;
    var inst = currentInstance;
    currentInstance = null;
    try {
      document.removeEventListener("keydown", inst.onKeyDown, true);
    } catch (_) { /* no-op */ }
    if (inst.root && inst.root.parentNode) {
      inst.root.parentNode.removeChild(inst.root);
    }
    if (inst.previousFocus && typeof inst.previousFocus.focus === "function") {
      try { inst.previousFocus.focus(); } catch (_) { /* no-op */ }
    }
    inst.resolve(result);
  }

  /* --------------------------------------------------------------- */
  /* Public: close()                                                  */
  /* --------------------------------------------------------------- */

  function close() {
    closeWith(null);
  }

  /* --------------------------------------------------------------- */
  /* Public: open()                                                   */
  /* --------------------------------------------------------------- */

  function open(options) {
    options = options || {};
    var probe = options.probe || {};
    var existing = options.existing || null;
    var unit = options.temperatureUnit === "F" ? "F" : "C";

    /* If a modal is already open, cancel it first. */
    if (currentInstance) closeWith(null);
    ensureStyles();

    return new Promise(function (resolve) {
      var state = {
        mode: existing && existing.mode === "range" ? "range" : "fixed",
        fixedValue: existing && existing.target_value != null
          ? toDisplay(existing.target_value, unit)
          : "",
        rangeLow: existing && existing.range_low != null
          ? toDisplay(existing.range_low, unit)
          : "",
        rangeHigh: existing && existing.range_high != null
          ? toDisplay(existing.range_high, unit)
          : "",
        preAlert: existing && existing.pre_alert_offset != null
          ? existing.pre_alert_offset
          : 5,
        reminderSecs: existing && existing.reminder_interval_secs != null
          ? existing.reminder_interval_secs
          : 0,
        timerMode: existing && existing.timer && existing.timer.mode
          ? existing.timer.mode
          : "none",
        timerDuration: existing && existing.timer &&
          existing.timer.mode === "count_down" &&
          existing.timer.duration_secs != null
          ? formatDuration(existing.timer.duration_secs)
          : "",
      };

      /* ---------- DOM construction ---------- */
      var root = document.createElement("div");
      root.className = "ptcs-root";
      root.setAttribute("role", "dialog");
      root.setAttribute("aria-modal", "true");
      root.setAttribute("aria-labelledby", "ptcs-title");

      var backdrop = document.createElement("div");
      backdrop.className = "ptcs-backdrop";
      backdrop.addEventListener("click", function () { closeWith(null); });
      root.appendChild(backdrop);

      var card = document.createElement("div");
      card.className = "ptcs-card";
      root.appendChild(card);

      var title = document.createElement("div");
      title.className = "ptcs-title";
      title.id = "ptcs-title";
      title.textContent = probe.label
        ? "Configure " + probe.label
        : "Configure probe " + (probe.probeIndex + 1);
      card.appendChild(title);

      var subtitle = document.createElement("div");
      subtitle.className = "ptcs-subtitle";
      subtitle.textContent = "Temperatures in \u00B0" + unit + ".";
      card.appendChild(subtitle);

      /* --- Mode toggle --- */
      var modeField = document.createElement("div");
      modeField.className = "ptcs-field";
      var modeLabel = document.createElement("div");
      modeLabel.className = "ptcs-label";
      modeLabel.textContent = "Target mode";
      modeField.appendChild(modeLabel);

      var modeSeg = document.createElement("div");
      modeSeg.className = "ptcs-seg";
      modeSeg.setAttribute("role", "tablist");
      var fixedBtn = document.createElement("button");
      fixedBtn.type = "button";
      fixedBtn.className = "ptcs-seg-btn";
      fixedBtn.textContent = "Fixed";
      fixedBtn.setAttribute("role", "tab");
      var rangeBtn = document.createElement("button");
      rangeBtn.type = "button";
      rangeBtn.className = "ptcs-seg-btn";
      rangeBtn.textContent = "Range";
      rangeBtn.setAttribute("role", "tab");
      modeSeg.appendChild(fixedBtn);
      modeSeg.appendChild(rangeBtn);
      modeField.appendChild(modeSeg);
      card.appendChild(modeField);

      /* --- Fixed input --- */
      var fixedField = document.createElement("div");
      fixedField.className = "ptcs-field";
      var fixedLabel = document.createElement("label");
      fixedLabel.className = "ptcs-label";
      fixedLabel.textContent = "Target temperature (\u00B0" + unit + ")";
      var fixedInput = document.createElement("input");
      fixedInput.type = "number";
      fixedInput.step = "0.1";
      fixedInput.className = "ptcs-input";
      fixedInput.placeholder = unit === "F" ? "e.g. 165" : "e.g. 74";
      fixedInput.value = state.fixedValue;
      fixedLabel.appendChild(fixedInput);
      fixedField.appendChild(fixedLabel);
      card.appendChild(fixedField);

      /* --- Range inputs --- */
      var rangeField = document.createElement("div");
      rangeField.className = "ptcs-field";
      var rangeLabel = document.createElement("div");
      rangeLabel.className = "ptcs-label";
      rangeLabel.textContent = "Target range (\u00B0" + unit + ")";
      rangeField.appendChild(rangeLabel);
      var rangeRow = document.createElement("div");
      rangeRow.className = "ptcs-row";
      var rangeLowInput = document.createElement("input");
      rangeLowInput.type = "number";
      rangeLowInput.step = "0.1";
      rangeLowInput.className = "ptcs-input";
      rangeLowInput.placeholder = "Low";
      rangeLowInput.value = state.rangeLow;
      rangeLowInput.setAttribute("aria-label", "Range low");
      var rangeHighInput = document.createElement("input");
      rangeHighInput.type = "number";
      rangeHighInput.step = "0.1";
      rangeHighInput.className = "ptcs-input";
      rangeHighInput.placeholder = "High";
      rangeHighInput.value = state.rangeHigh;
      rangeHighInput.setAttribute("aria-label", "Range high");
      rangeRow.appendChild(rangeLowInput);
      rangeRow.appendChild(rangeHighInput);
      rangeField.appendChild(rangeRow);
      card.appendChild(rangeField);

      /* --- Pre-alert offset --- */
      var preField = document.createElement("div");
      preField.className = "ptcs-field";
      var preLabel = document.createElement("label");
      preLabel.className = "ptcs-label";
      preLabel.textContent = "Pre-alert offset (\u00B0" + unit + ")";
      var preInput = document.createElement("input");
      preInput.type = "number";
      preInput.step = "1";
      preInput.min = "0";
      preInput.className = "ptcs-input";
      preInput.value = String(state.preAlert);
      preLabel.appendChild(preInput);
      preField.appendChild(preLabel);
      var preHint = document.createElement("div");
      preHint.className = "ptcs-hint";
      preHint.textContent = "Alert when the probe is within this many degrees of target.";
      preField.appendChild(preHint);
      card.appendChild(preField);

      /* --- Reminder interval --- */
      var remField = document.createElement("div");
      remField.className = "ptcs-field";
      var remLabel = document.createElement("label");
      remLabel.className = "ptcs-label";
      remLabel.textContent = "Reminder interval";
      var remSelect = document.createElement("select");
      remSelect.className = "ptcs-select";
      REMINDER_OPTIONS.forEach(function (opt) {
        var o = document.createElement("option");
        o.value = String(opt.value);
        o.textContent = opt.label;
        if (opt.value === state.reminderSecs) o.selected = true;
        remSelect.appendChild(o);
      });
      remLabel.appendChild(remSelect);
      remField.appendChild(remLabel);
      card.appendChild(remField);

      /* --- Timer group --- */
      var timerField = document.createElement("div");
      timerField.className = "ptcs-field";
      var timerLabel = document.createElement("div");
      timerLabel.className = "ptcs-label";
      timerLabel.textContent = "Probe timer";
      timerField.appendChild(timerLabel);

      var timerGroup = document.createElement("div");
      timerGroup.className = "ptcs-timer-group";
      var timerSeg = document.createElement("div");
      timerSeg.className = "ptcs-seg";
      var timerBtns = {};
      ["none", "count_up", "count_down"].forEach(function (m) {
        var b = document.createElement("button");
        b.type = "button";
        b.className = "ptcs-seg-btn";
        b.textContent = m === "none" ? "None"
          : m === "count_up" ? "Count Up" : "Count Down";
        b.addEventListener("click", function () {
          state.timerMode = m;
          sync();
        });
        timerSeg.appendChild(b);
        timerBtns[m] = b;
      });
      timerGroup.appendChild(timerSeg);

      var durationWrap = document.createElement("div");
      durationWrap.style.marginTop = "0.6rem";
      var durationLabel = document.createElement("label");
      durationLabel.className = "ptcs-label";
      durationLabel.textContent = "Duration (mm:ss or hh:mm:ss)";
      var durationInput = document.createElement("input");
      durationInput.type = "text";
      durationInput.className = "ptcs-input";
      durationInput.placeholder = "e.g. 45:00";
      durationInput.value = state.timerDuration;
      durationLabel.appendChild(durationInput);
      durationWrap.appendChild(durationLabel);
      timerGroup.appendChild(durationWrap);

      timerField.appendChild(timerGroup);
      card.appendChild(timerField);

      /* --- Error line --- */
      var errorLine = document.createElement("div");
      errorLine.className = "ptcs-error";
      errorLine.setAttribute("role", "alert");
      errorLine.setAttribute("aria-live", "polite");
      card.appendChild(errorLine);

      /* --- Actions --- */
      var actions = document.createElement("div");
      actions.className = "ptcs-actions";
      var cancelBtn = document.createElement("button");
      cancelBtn.type = "button";
      cancelBtn.className = "ptcs-btn";
      cancelBtn.textContent = "Cancel";
      cancelBtn.addEventListener("click", function () { closeWith(null); });
      var confirmBtn = document.createElement("button");
      confirmBtn.type = "button";
      confirmBtn.className = "ptcs-btn ptcs-btn-primary";
      confirmBtn.textContent = "Confirm";
      confirmBtn.addEventListener("click", function () {
        var result = validateAndBuild();
        if (result.ok) closeWith(result.config);
      });
      actions.appendChild(cancelBtn);
      actions.appendChild(confirmBtn);
      card.appendChild(actions);

      /* ---------- Behaviour ---------- */

      fixedBtn.addEventListener("click", function () { state.mode = "fixed"; sync(); });
      rangeBtn.addEventListener("click", function () { state.mode = "range"; sync(); });
      fixedInput.addEventListener("input", function () { state.fixedValue = fixedInput.value; sync(); });
      rangeLowInput.addEventListener("input", function () { state.rangeLow = rangeLowInput.value; sync(); });
      rangeHighInput.addEventListener("input", function () { state.rangeHigh = rangeHighInput.value; sync(); });
      preInput.addEventListener("input", function () { state.preAlert = preInput.value; sync(); });
      remSelect.addEventListener("change", function () {
        state.reminderSecs = parseInt(remSelect.value, 10) || 0;
      });
      durationInput.addEventListener("input", function () {
        state.timerDuration = durationInput.value;
        sync();
      });

      function validateAndBuild() {
        var errs = [];
        var config = {
          probe_index: probe.probeIndex,
          mode: state.mode,
          pre_alert_offset: 5,
          reminder_interval_secs: parseInt(remSelect.value, 10) || 0,
        };
        if (probe.label) config.label = probe.label;

        /* Pre-alert offset */
        var pre = parseFloat(state.preAlert);
        if (!isFinite(pre) || pre < 0) {
          errs.push("Pre-alert offset must be 0 or greater.");
        } else {
          config.pre_alert_offset = pre;
        }

        if (state.mode === "fixed") {
          var tv = fromDisplay(state.fixedValue, unit);
          if (!isFinite(tv)) {
            errs.push("Enter a target temperature.");
          } else {
            config.target_value = tv;
          }
        } else {
          var lo = fromDisplay(state.rangeLow, unit);
          var hi = fromDisplay(state.rangeHigh, unit);
          if (!isFinite(lo) || !isFinite(hi)) {
            errs.push("Enter both low and high values.");
          } else if (lo >= hi) {
            errs.push("Low must be less than high.");
          } else {
            config.range_low = lo;
            config.range_high = hi;
          }
        }

        if (state.timerMode && state.timerMode !== "none") {
          var timer = { mode: state.timerMode };
          if (state.timerMode === "count_down") {
            var secs = parseDuration(state.timerDuration);
            if (!isFinite(secs) || secs <= 0) {
              errs.push("Enter a count-down duration (e.g. 45:00).");
            } else {
              timer.duration_secs = secs;
            }
          }
          config.timer = timer;
        }

        if (errs.length) return { ok: false, errors: errs };
        return { ok: true, config: config };
      }

      function sync() {
        /* Mode seg active state */
        fixedBtn.classList.toggle("active", state.mode === "fixed");
        rangeBtn.classList.toggle("active", state.mode === "range");
        fixedField.style.display = state.mode === "fixed" ? "" : "none";
        rangeField.style.display = state.mode === "range" ? "" : "none";

        /* Timer seg + duration visibility */
        Object.keys(timerBtns).forEach(function (m) {
          timerBtns[m].classList.toggle("active", state.timerMode === m);
        });
        durationWrap.style.display = state.timerMode === "count_down" ? "" : "none";

        /* Validation -> error line + confirm enabled */
        var v = validateAndBuild();
        if (v.ok) {
          errorLine.textContent = "";
          confirmBtn.disabled = false;
        } else {
          errorLine.textContent = v.errors[0] || "";
          confirmBtn.disabled = true;
        }
      }

      /* ---------- Mount + focus + keys ---------- */

      var instance = {
        root: root,
        resolve: resolve,
        previousFocus: document.activeElement,
        onKeyDown: function (e) {
          if (e.key === "Escape") {
            e.stopPropagation();
            e.preventDefault();
            closeWith(null);
            return;
          }
          if (e.key === "Tab") {
            /* Trap focus */
            var focusables = getFocusable(card);
            if (focusables.length === 0) {
              e.preventDefault();
              return;
            }
            var first = focusables[0];
            var last = focusables[focusables.length - 1];
            var active = document.activeElement;
            if (e.shiftKey && active === first) {
              e.preventDefault();
              last.focus();
            } else if (!e.shiftKey && active === last) {
              e.preventDefault();
              first.focus();
            }
          }
        },
      };
      currentInstance = instance;

      document.body.appendChild(root);
      document.addEventListener("keydown", instance.onKeyDown, true);

      sync();

      /* Initial focus: prefer the first visible input. */
      setTimeout(function () {
        if (!currentInstance || currentInstance !== instance) return;
        if (state.mode === "fixed") {
          try { fixedInput.focus(); fixedInput.select(); } catch (_) { /* no-op */ }
        } else {
          try { rangeLowInput.focus(); rangeLowInput.select(); } catch (_) { /* no-op */ }
        }
      }, 0);
    });
  }

  global.ProbeTargetConfigSheet = {
    open: open,
    close: close,
    /* Exposed for unit-level reasoning; not part of the public API. */
    _internals: {
      cToF: cToF,
      fToC: fToC,
      parseDuration: parseDuration,
      formatDuration: formatDuration,
    },
  };
})(typeof window !== "undefined" ? window : this);
