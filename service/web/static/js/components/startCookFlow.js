/**
 * StartCookFlow
 * -------------
 *
 * Multi-step modal that walks the user through starting a cook session:
 *   1. Session setup (optional name + target duration).
 *   2. Probe targets (list of plugged-in probes; each can be configured
 *      via ProbeTargetConfigSheet).
 *   3. Review + Start (disabled until at least one probe has a target).
 *
 * On confirm the component:
 *   - Sends `session_start_request` via the supplied `send(envelope)`
 *     callback.
 *   - Waits for either `session_start_ack { ok: true }` or `error`.
 *   - On success, fires any configured `probe_timer_request { action:
 *     'upsert' }` messages for probes that had a timer defined in their
 *     TargetConfig. Timers are created in the 'idle' state; users start
 *     them manually from probe controls (Task 8).
 *   - On failure, keeps the modal open and shows the server error.
 *
 * Usage:
 *   StartCookFlow.open({
 *     devices,            // map: address -> { data: { name, probes: [...] } }
 *     send,               // function(envelope)  sends via active WebSocket
 *     generateRequestId,  // function() returns a unique requestId string
 *     onMessage,          // function(handler) subscribes; returns
 *                          //   unsubscribe. handler receives the full
 *                          //   decoded message.
 *     temperatureUnit,    // 'C' | 'F' (default 'C')
 *   });
 *
 * Styling mirrors ProbeTargetConfigSheet to stay visually consistent:
 * same CSS variables, same radius/shadow/border treatment. Scoped
 * under `.scf-root` to avoid collisions with the legacy dashboard.
 *
 * Accessibility:
 *   - Dialog role with focus trap.
 *   - Escape cancels.
 *   - Enter on the final step triggers Start (when enabled).
 */
(function (global) {
  "use strict";

  var STYLE_ID = "scf-styles";
  var STYLE_CSS = [
    ".scf-root{position:fixed;inset:0;z-index:95;display:flex;align-items:center;justify-content:center;font-family:inherit;}",
    ".scf-backdrop{position:absolute;inset:0;background:rgba(0,0,0,0.55);-webkit-backdrop-filter:blur(2px);backdrop-filter:blur(2px);}",
    ".scf-card{position:relative;background:var(--bg-secondary,#16213e);color:var(--text-primary,#e0e0e0);border:1px solid var(--border,#2a2a4a);border-radius:var(--radius,10px);padding:1.25rem;width:min(92vw,480px);max-height:90vh;overflow-y:auto;box-shadow:0 20px 60px rgba(0,0,0,0.55);}",
    ".scf-steps{display:flex;gap:0.4rem;margin-bottom:0.9rem;}",
    ".scf-step-pip{flex:1;height:4px;border-radius:2px;background:var(--border,#2a2a4a);}",
    ".scf-step-pip.active{background:var(--brand,#935240);}",
    ".scf-step-pip.done{background:var(--brand-tint,rgba(147,82,64,0.55));}",
    ".scf-title{font-size:1.05rem;font-weight:600;margin-bottom:0.25rem;}",
    ".scf-subtitle{font-size:0.85rem;color:var(--text-secondary,#a0a0b8);margin-bottom:1rem;}",
    ".scf-field{display:flex;flex-direction:column;gap:0.35rem;margin-bottom:0.9rem;}",
    ".scf-label{font-size:0.8rem;font-weight:500;color:var(--text-secondary,#a0a0b8);text-transform:uppercase;letter-spacing:0.04em;}",
    ".scf-input{background:var(--bg-card,#0f3460);color:var(--text-primary,#e0e0e0);border:1px solid var(--border,#2a2a4a);border-radius:8px;padding:0.55rem 0.7rem;font-size:0.95rem;font-family:inherit;width:100%;}",
    ".scf-input:focus{outline:2px solid var(--brand,#935240);outline-offset:0;border-color:var(--brand,#935240);}",
    ".scf-hint{font-size:0.8rem;color:var(--text-muted,#6c6c80);margin-top:0.25rem;}",
    ".scf-error{font-size:0.85rem;color:var(--red,#f87171);margin-top:0.25rem;min-height:1em;}",
    ".scf-probe-list{display:flex;flex-direction:column;gap:0.5rem;margin-bottom:0.5rem;}",
    ".scf-probe-row{display:flex;align-items:center;gap:0.6rem;background:var(--bg-card,#0f3460);border:1px solid var(--border,#2a2a4a);border-radius:8px;padding:0.55rem 0.7rem;}",
    ".scf-probe-info{flex:1;min-width:0;}",
    ".scf-probe-title{font-size:0.95rem;font-weight:500;color:var(--text-primary,#e0e0e0);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}",
    ".scf-probe-meta{font-size:0.8rem;color:var(--text-secondary,#a0a0b8);margin-top:0.15rem;}",
    ".scf-probe-meta.configured{color:var(--brand,#935240);}",
    ".scf-probe-actions{display:flex;align-items:center;gap:0.35rem;}",
    ".scf-empty{padding:0.9rem;border:1px dashed var(--border,#2a2a4a);border-radius:8px;font-size:0.9rem;color:var(--text-secondary,#a0a0b8);text-align:center;margin-bottom:0.5rem;}",
    ".scf-actions{display:flex;gap:0.5rem;justify-content:space-between;margin-top:1rem;align-items:center;}",
    ".scf-actions-right{display:flex;gap:0.5rem;}",
    ".scf-btn{background:var(--bg-card,#0f3460);color:var(--text-primary,#e0e0e0);border:1px solid var(--border,#2a2a4a);border-radius:8px;padding:0.55rem 1rem;font-size:0.95rem;font-family:inherit;cursor:pointer;}",
    ".scf-btn:hover{background:var(--bg-card-hover,#134074);}",
    ".scf-btn-primary{background:var(--brand,#935240);border-color:var(--brand,#935240);color:#fff;}",
    ".scf-btn-primary:hover{filter:brightness(1.08);}",
    ".scf-btn-primary:disabled{opacity:0.5;cursor:not-allowed;filter:none;}",
    ".scf-btn-ghost{background:transparent;border-color:transparent;color:var(--text-secondary,#a0a0b8);padding:0.35rem 0.5rem;}",
    ".scf-btn-ghost:hover{color:var(--text-primary,#e0e0e0);background:transparent;}",
    ".scf-btn:focus-visible{outline:2px solid var(--brand,#935240);outline-offset:2px;}",
    ".scf-duration-row{display:flex;gap:0.5rem;align-items:center;}",
    ".scf-duration-row>.scf-input{flex:1;}",
    ".scf-duration-row>span{font-size:0.9rem;color:var(--text-secondary,#a0a0b8);}",
    "@media (prefers-color-scheme: light){.scf-card{box-shadow:0 20px 60px rgba(0,0,0,0.25);}}",
  ].join("\n");

  function ensureStyles() {
    if (typeof document === "undefined") return;
    if (document.getElementById(STYLE_ID)) return;
    var s = document.createElement("style");
    s.id = STYLE_ID;
    s.textContent = STYLE_CSS;
    document.head.appendChild(s);
  }

  var FOCUSABLE =
    'a[href],area[href],input:not([disabled]),select:not([disabled]),' +
    'textarea:not([disabled]),button:not([disabled]),iframe,' +
    '[tabindex]:not([tabindex="-1"])';

  function getFocusable(root) {
    return Array.prototype.slice.call(root.querySelectorAll(FOCUSABLE));
  }

  /* Format seconds as hh:mm for the display input. */
  function formatHHMM(secs) {
    if (!isFinite(secs) || secs <= 0) return "";
    var h = Math.floor(secs / 3600);
    var m = Math.floor((secs % 3600) / 60);
    function pad(n) { return n < 10 ? "0" + n : String(n); }
    return pad(h) + ":" + pad(m);
  }

  /* Parse "h:mm" / "hh:mm" / "mm" / integer seconds into seconds.
   * Returns NaN on unparseable input, 0 on an empty string (meaning
   * "no target duration"). */
  function parseHHMM(str) {
    if (str == null) return 0;
    var trimmed = String(str).trim();
    if (trimmed === "") return 0;
    if (trimmed.indexOf(":") === -1) {
      var n = parseFloat(trimmed);
      if (!isFinite(n) || n < 0) return NaN;
      /* Treat bare integers as hours (simplest for "4"). */
      return Math.round(n * 3600);
    }
    var parts = trimmed.split(":");
    if (parts.length !== 2) return NaN;
    var h = parseInt(parts[0], 10);
    var m = parseInt(parts[1], 10);
    if (!isFinite(h) || !isFinite(m) || h < 0 || m < 0 || m >= 60) return NaN;
    return h * 3600 + m * 60;
  }

  /* Format a TargetConfig summary for display ("Target: 74°C" etc.). */
  function summariseTarget(config, unit) {
    if (!config) return "";
    function disp(c) {
      if (c == null || !isFinite(c)) return "";
      var v = unit === "F" ? c * 9 / 5 + 32 : c;
      return (Math.round(v * 10) / 10) + "\u00B0" + unit;
    }
    var parts = [];
    if (config.mode === "range") {
      parts.push("Range " + disp(config.range_low) + "\u2013" + disp(config.range_high));
    } else {
      parts.push("Target " + disp(config.target_value));
    }
    if (config.timer && config.timer.mode && config.timer.mode !== "none") {
      parts.push(config.timer.mode === "count_down" ? "count down" : "count up");
    }
    return parts.join(" \u00B7 ");
  }

  /* --------------------------------------------------------------- */
  /* Instance                                                         */
  /* --------------------------------------------------------------- */

  var currentInstance = null;

  function cleanup(inst) {
    try { document.removeEventListener("keydown", inst.onKeyDown, true); }
    catch (_) { /* no-op */ }
    if (inst.unsubscribeMessages) {
      try { inst.unsubscribeMessages(); } catch (_) { /* no-op */ }
      inst.unsubscribeMessages = null;
    }
    if (inst.root && inst.root.parentNode) {
      inst.root.parentNode.removeChild(inst.root);
    }
    if (inst.previousFocus && typeof inst.previousFocus.focus === "function") {
      try { inst.previousFocus.focus(); } catch (_) { /* no-op */ }
    }
  }

  function close() {
    if (!currentInstance) return;
    var inst = currentInstance;
    currentInstance = null;
    cleanup(inst);
  }

  function open(options) {
    options = options || {};
    var devices = options.devices || {};
    var send = typeof options.send === "function" ? options.send : null;
    var generateRequestId = typeof options.generateRequestId === "function"
      ? options.generateRequestId
      : function () { return "scf-" + Date.now() + "-" + Math.random().toString(36).slice(2, 8); };
    var onMessage = typeof options.onMessage === "function" ? options.onMessage : null;
    var unit = options.temperatureUnit === "F" ? "F" : "C";

    if (!send || !onMessage) {
      console.warn("[StartCookFlow] send and onMessage options are required");
      return;
    }

    if (currentInstance) close();
    ensureStyles();

    /* Build the ordered list of eligible probes (plugged in) once. We
     * don't re-read `devices` during the flow: if probes unplug mid-
     * flow the user's configured targets still submit successfully,
     * the server just reports no readings until they're plugged in
     * again. Keeping the list stable avoids a moving target for the
     * user while they configure. */
    var probeRows = [];
    Object.keys(devices).forEach(function (address) {
      var dev = devices[address];
      var data = (dev && dev.data) || {};
      var deviceName = data.name || address;
      var probes = Array.isArray(data.probes) ? data.probes : [];
      probes.forEach(function (p) {
        if (!p || p.index == null) return;
        /* "Plugged in" == currently reporting a temperature. An
         * unplugged probe has `unplugged: true` and/or a null temp. */
        if (p.unplugged) return;
        if (p.temperature == null) return;
        probeRows.push({
          address: address,
          probeIndex: p.index,
          deviceName: deviceName,
          label: deviceName + " \u2014 Probe " + p.index,
        });
      });
    });

    /* Flow state. */
    var state = {
      step: 1,
      name: "",
      durationStr: "",
      targets: {},        /* key: address + "|" + probeIndex -> TargetConfig */
      submitting: false,
      submitRequestId: null,
      serverError: "",
    };
    function keyFor(address, probeIndex) { return address + "|" + probeIndex; }

    /* ---------------- DOM construction ---------------- */
    var root = document.createElement("div");
    root.className = "scf-root";
    root.setAttribute("role", "dialog");
    root.setAttribute("aria-modal", "true");
    root.setAttribute("aria-labelledby", "scf-title");

    var backdrop = document.createElement("div");
    backdrop.className = "scf-backdrop";
    backdrop.addEventListener("click", function () { close(); });
    root.appendChild(backdrop);

    var card = document.createElement("div");
    card.className = "scf-card";
    root.appendChild(card);

    /* Step pips */
    var steps = document.createElement("div");
    steps.className = "scf-steps";
    var pips = [
      document.createElement("div"),
      document.createElement("div"),
      document.createElement("div"),
    ];
    pips.forEach(function (p) { p.className = "scf-step-pip"; steps.appendChild(p); });
    card.appendChild(steps);

    var title = document.createElement("div");
    title.className = "scf-title";
    title.id = "scf-title";
    card.appendChild(title);

    var subtitle = document.createElement("div");
    subtitle.className = "scf-subtitle";
    card.appendChild(subtitle);

    /* Body wrapper swapped per step */
    var body = document.createElement("div");
    card.appendChild(body);

    /* Error line (shared across steps) */
    var errorLine = document.createElement("div");
    errorLine.className = "scf-error";
    errorLine.setAttribute("role", "alert");
    errorLine.setAttribute("aria-live", "polite");
    card.appendChild(errorLine);

    /* Actions */
    var actions = document.createElement("div");
    actions.className = "scf-actions";
    var cancelBtn = document.createElement("button");
    cancelBtn.type = "button";
    cancelBtn.className = "scf-btn scf-btn-ghost";
    cancelBtn.textContent = "Cancel";
    cancelBtn.addEventListener("click", function () { close(); });

    var rightWrap = document.createElement("div");
    rightWrap.className = "scf-actions-right";
    var backBtn = document.createElement("button");
    backBtn.type = "button";
    backBtn.className = "scf-btn";
    backBtn.textContent = "Back";
    backBtn.addEventListener("click", function () {
      if (state.step > 1) { state.step -= 1; state.serverError = ""; render(); }
    });
    var primaryBtn = document.createElement("button");
    primaryBtn.type = "button";
    primaryBtn.className = "scf-btn scf-btn-primary";
    primaryBtn.addEventListener("click", onPrimary);
    rightWrap.appendChild(backBtn);
    rightWrap.appendChild(primaryBtn);

    actions.appendChild(cancelBtn);
    actions.appendChild(rightWrap);
    card.appendChild(actions);

    /* ---------------- Step renderers ---------------- */

    function renderStep1() {
      title.textContent = "New cook";
      subtitle.textContent = "Step 1 of 3 \u00B7 Session setup";

      var frag = document.createDocumentFragment();

      var nameField = document.createElement("div");
      nameField.className = "scf-field";
      var nameLabel = document.createElement("label");
      nameLabel.className = "scf-label";
      nameLabel.textContent = "Session name (optional)";
      var nameInput = document.createElement("input");
      nameInput.type = "text";
      nameInput.className = "scf-input";
      nameInput.placeholder = "e.g. Brisket";
      nameInput.maxLength = 120;
      nameInput.value = state.name;
      nameInput.addEventListener("input", function () { state.name = nameInput.value; });
      nameLabel.appendChild(nameInput);
      nameField.appendChild(nameLabel);
      frag.appendChild(nameField);

      var durField = document.createElement("div");
      durField.className = "scf-field";
      var durLabel = document.createElement("label");
      durLabel.className = "scf-label";
      durLabel.textContent = "Target cook duration (optional)";
      var durRow = document.createElement("div");
      durRow.className = "scf-duration-row";
      var durInput = document.createElement("input");
      durInput.type = "text";
      durInput.className = "scf-input";
      durInput.placeholder = "hh:mm";
      durInput.value = state.durationStr;
      durInput.setAttribute("aria-describedby", "scf-dur-hint");
      durInput.addEventListener("input", function () {
        state.durationStr = durInput.value;
        validateStep1();
      });
      durRow.appendChild(durInput);
      var durUnits = document.createElement("span");
      durUnits.textContent = "hh:mm";
      durRow.appendChild(durUnits);
      durLabel.appendChild(durRow);
      durField.appendChild(durLabel);
      var durHint = document.createElement("div");
      durHint.className = "scf-hint";
      durHint.id = "scf-dur-hint";
      durHint.textContent = "Leave blank for no specific target duration.";
      durField.appendChild(durHint);
      frag.appendChild(durField);

      body.appendChild(frag);

      /* Focus the name input on first open of step 1. */
      setTimeout(function () {
        if (!currentInstance) return;
        try { nameInput.focus(); } catch (_) { /* no-op */ }
      }, 0);

      validateStep1();
    }

    function validateStep1() {
      var secs = parseHHMM(state.durationStr);
      if (isNaN(secs)) {
        errorLine.textContent = "Duration must be hh:mm (or blank).";
        primaryBtn.disabled = true;
      } else {
        errorLine.textContent = "";
        primaryBtn.disabled = false;
      }
    }

    function renderStep2() {
      title.textContent = "Configure probes";
      subtitle.textContent = "Step 2 of 3 \u00B7 At least one probe needs a target.";

      if (probeRows.length === 0) {
        var empty = document.createElement("div");
        empty.className = "scf-empty";
        empty.textContent = "No plugged-in probes detected. Plug a probe in and try again, or cancel.";
        body.appendChild(empty);
      } else {
        var list = document.createElement("div");
        list.className = "scf-probe-list";

        probeRows.forEach(function (row) {
          var k = keyFor(row.address, row.probeIndex);
          var existing = state.targets[k];

          var li = document.createElement("div");
          li.className = "scf-probe-row";

          var info = document.createElement("div");
          info.className = "scf-probe-info";
          var t = document.createElement("div");
          t.className = "scf-probe-title";
          t.textContent = row.label;
          info.appendChild(t);
          var meta = document.createElement("div");
          meta.className = "scf-probe-meta" + (existing ? " configured" : "");
          meta.textContent = existing ? summariseTarget(existing, unit) : "Not configured";
          info.appendChild(meta);
          li.appendChild(info);

          var rowActions = document.createElement("div");
          rowActions.className = "scf-probe-actions";

          if (existing) {
            var clearBtn = document.createElement("button");
            clearBtn.type = "button";
            clearBtn.className = "scf-btn scf-btn-ghost";
            clearBtn.setAttribute("aria-label", "Remove target for " + row.label);
            clearBtn.textContent = "\u2715";
            clearBtn.addEventListener("click", function () {
              delete state.targets[k];
              render();
            });
            rowActions.appendChild(clearBtn);
          }

          var configBtn = document.createElement("button");
          configBtn.type = "button";
          configBtn.className = "scf-btn";
          configBtn.textContent = existing ? "Edit" : "Configure";
          configBtn.addEventListener("click", function () {
            if (!global.ProbeTargetConfigSheet) {
              errorLine.textContent = "Target configuration unavailable.";
              return;
            }
            global.ProbeTargetConfigSheet.open({
              probe: {
                address: row.address,
                probeIndex: row.probeIndex,
                label: row.label,
              },
              existing: existing || undefined,
              temperatureUnit: unit,
            }).then(function (config) {
              if (config) {
                state.targets[k] = config;
                render();
              }
            });
          });
          rowActions.appendChild(configBtn);

          li.appendChild(rowActions);
          list.appendChild(li);
        });

        body.appendChild(list);
      }

      validateStep2();
    }

    function configuredTargetCount() {
      return Object.keys(state.targets).length;
    }

    function validateStep2() {
      if (configuredTargetCount() === 0) {
        errorLine.textContent = "";
        primaryBtn.disabled = true;
      } else {
        errorLine.textContent = "";
        primaryBtn.disabled = false;
      }
    }

    function renderStep3() {
      title.textContent = state.submitting ? "Starting\u2026" : "Start cook";
      subtitle.textContent = "Step 3 of 3 \u00B7 Review and start.";

      var frag = document.createDocumentFragment();

      var summary = document.createElement("div");
      summary.className = "scf-field";
      var sumLabel = document.createElement("div");
      sumLabel.className = "scf-label";
      sumLabel.textContent = "Summary";
      summary.appendChild(sumLabel);

      function row(labelText, valueText) {
        var r = document.createElement("div");
        r.className = "scf-probe-row";
        var info = document.createElement("div");
        info.className = "scf-probe-info";
        var l = document.createElement("div");
        l.className = "scf-probe-meta";
        l.textContent = labelText;
        var v = document.createElement("div");
        v.className = "scf-probe-title";
        v.textContent = valueText;
        info.appendChild(v);
        info.appendChild(l);
        r.appendChild(info);
        return r;
      }

      summary.appendChild(row("Name", state.name.trim() || "(unnamed)"));
      var secs = parseHHMM(state.durationStr);
      var durDisplay = secs > 0 ? formatHHMM(secs) : "(none)";
      summary.appendChild(row("Target duration", durDisplay));
      summary.appendChild(row(
        "Probes configured",
        String(configuredTargetCount()) + " of " + String(probeRows.length)
      ));
      frag.appendChild(summary);

      /* Per-probe target list */
      if (configuredTargetCount() > 0) {
        var list = document.createElement("div");
        list.className = "scf-probe-list";
        probeRows.forEach(function (pr) {
          var cfg = state.targets[keyFor(pr.address, pr.probeIndex)];
          if (!cfg) return;
          list.appendChild(row(pr.label, summariseTarget(cfg, unit)));
        });
        frag.appendChild(list);
      }

      body.appendChild(frag);

      if (state.serverError) {
        errorLine.textContent = state.serverError;
      } else {
        errorLine.textContent = "";
      }
      primaryBtn.disabled = state.submitting;
    }

    /* ---------------- Render + navigation ---------------- */

    function render() {
      while (body.firstChild) body.removeChild(body.firstChild);

      pips.forEach(function (pip, i) {
        pip.classList.remove("active", "done");
        var idx = i + 1;
        if (idx === state.step) pip.classList.add("active");
        else if (idx < state.step) pip.classList.add("done");
      });

      backBtn.style.visibility = state.step === 1 ? "hidden" : "visible";

      if (state.step === 3) {
        primaryBtn.textContent = state.submitting ? "Starting\u2026" : "Start Session";
      } else {
        primaryBtn.textContent = "Next";
      }

      if (state.step === 1) renderStep1();
      else if (state.step === 2) renderStep2();
      else renderStep3();
    }

    function onPrimary() {
      if (primaryBtn.disabled) return;
      if (state.step === 1) {
        validateStep1();
        if (primaryBtn.disabled) return;
        state.step = 2;
        render();
      } else if (state.step === 2) {
        if (configuredTargetCount() === 0) return;
        state.step = 3;
        render();
      } else {
        submit();
      }
    }

    /* ---------------- Submission ---------------- */

    function submit() {
      if (state.submitting) return;
      state.submitting = true;
      state.serverError = "";
      render();

      var deviceAddresses = [];
      var targets = [];
      /* Preserve stable ordering using probeRows. */
      probeRows.forEach(function (pr) {
        var cfg = state.targets[keyFor(pr.address, pr.probeIndex)];
        if (!cfg) return;
        if (deviceAddresses.indexOf(pr.address) === -1) {
          deviceAddresses.push(pr.address);
        }
        /* Strip `timer` before sending — server doesn't accept it on
         * session_start. We replay it via probe_timer_request after
         * session_start_ack. */
        var serverTarget = {};
        Object.keys(cfg).forEach(function (k) {
          if (k === "timer") return;
          serverTarget[k] = cfg[k];
        });
        targets.push(serverTarget);
      });

      var payload = { deviceAddresses: deviceAddresses, targets: targets };
      if (state.name.trim()) payload.name = state.name.trim();
      var durSecs = parseHHMM(state.durationStr);
      if (durSecs > 0) payload.targetDurationSecs = durSecs;

      var requestId = generateRequestId();
      state.submitRequestId = requestId;

      try {
        send({
          v: 2,
          type: "session_start_request",
          requestId: requestId,
          payload: payload,
        });
      } catch (err) {
        state.submitting = false;
        state.serverError = "Failed to send request: " + (err && err.message ? err.message : err);
        render();
      }
    }

    /* Subscribe to WS messages so we can react to ack / error. The
     * SessionStore handles the state transition; we only need to
     * close on success or surface the error on failure. */
    function handleIncoming(msg) {
      if (!msg || typeof msg !== "object") return;
      /* Only care about responses to our pending request. */
      if (state.submitRequestId && msg.requestId && msg.requestId !== state.submitRequestId) {
        return;
      }
      if (msg.type === "session_start_ack") {
        var p = msg.payload || {};
        if (p.ok === false) {
          state.submitting = false;
          state.serverError = p.error || "Server rejected the request.";
          state.submitRequestId = null;
          render();
          return;
        }
        /* Success: fire deferred timer upserts, then close. Timer
         * upserts need the session to be active; by the time the ack
         * has been broadcast it is. Fire and forget — failures here
         * don't block the cook starting. */
        probeRows.forEach(function (pr) {
          var cfg = state.targets[keyFor(pr.address, pr.probeIndex)];
          if (!cfg || !cfg.timer || !cfg.timer.mode || cfg.timer.mode === "none") return;
          var timerPayload = {
            action: "upsert",
            address: pr.address,
            probe_index: pr.probeIndex,
            mode: cfg.timer.mode,
          };
          if (cfg.timer.mode === "count_down" && cfg.timer.duration_secs != null) {
            timerPayload.duration_secs = cfg.timer.duration_secs;
          }
          try {
            send({
              v: 2,
              type: "probe_timer_request",
              requestId: generateRequestId(),
              payload: timerPayload,
            });
          } catch (_) { /* ignore */ }
        });
        close();
      } else if (msg.type === "error") {
        var errPayload = msg.payload || {};
        state.submitting = false;
        state.serverError = errPayload.message || errPayload.code || "Server error.";
        state.submitRequestId = null;
        render();
      }
    }

    var instance = {
      root: root,
      previousFocus: document.activeElement,
      unsubscribeMessages: null,
      onKeyDown: function (e) {
        if (e.key === "Escape") {
          e.stopPropagation();
          e.preventDefault();
          close();
          return;
        }
        if (e.key === "Enter") {
          /* Only trigger primary if focus is not in a textarea or a
           * secondary button. Plain inputs/select/body all should fire. */
          var tgt = e.target;
          var tagName = tgt && tgt.tagName ? tgt.tagName.toLowerCase() : "";
          if (tagName === "textarea") return;
          if (tagName === "button" && tgt !== primaryBtn) return;
          if (!primaryBtn.disabled) {
            e.preventDefault();
            onPrimary();
          }
          return;
        }
        if (e.key === "Tab") {
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

    instance.unsubscribeMessages = onMessage(handleIncoming);

    document.body.appendChild(root);
    document.addEventListener("keydown", instance.onKeyDown, true);

    render();
  }

  global.StartCookFlow = {
    open: open,
    close: close,
    _internals: {
      parseHHMM: parseHHMM,
      formatHHMM: formatHHMM,
      summariseTarget: summariseTarget,
    },
  };
})(typeof window !== "undefined" ? window : this);
