/**
 * AddProbeFlow
 * ------------
 *
 * Task 9: Add a probe to an already-running session.
 *
 * Two-step flow:
 *   1. Chooser -- list plugged-in probes across all connected devices
 *      that don't already have a target in the current session. Probes
 *      already targeted are excluded. If no probes are eligible we
 *      show an explanatory empty state and the user can cancel.
 *   2. Target config -- we delegate to `ProbeTargetConfigSheet.open(...)`
 *      with the selected probe. On confirm we build the new target
 *      list and send a `target_update_request` containing both the
 *      existing targets and the new one.
 *
 * Prior to sending `target_update_request`, if the selected probe's
 * device isn't yet part of the session (mid-cook second iGrill) we
 * first fire `session_add_device_request { deviceAddress }` and wait
 * for `session_add_device_ack`.
 *
 * If the new target carries a timer, we fire a `probe_timer_request
 * { action: 'upsert' }` after the `target_update_ack` lands. This
 * mirrors the Start Cook flow so the server never has to understand a
 * `timer` field on the target row itself.
 *
 * Usage:
 *   window.AddProbeFlow.open({
 *     devices,            // map: address -> { data: { name, probes: [...] } }
 *     currentTargets,     // state.activeSession.targets
 *     sessionDevices,     // list of addresses already in the session
 *     send,               // fn(envelope) -> void
 *     generateRequestId,  // fn() -> string
 *     onMessage,          // fn(handler) -> unsubscribe
 *     temperatureUnit,    // 'C' | 'F' (default 'C')
 *   });
 *
 * Styling scopes under `.apf-root` but reuses the StartCookFlow
 * visual language (same tokens, same radius, same button treatments).
 */
(function (global) {
  "use strict";

  var STYLE_ID = "apf-styles";
  var STYLE_CSS = [
    ".apf-root{position:fixed;inset:0;z-index:96;display:flex;align-items:center;justify-content:center;font-family:inherit;}",
    ".apf-backdrop{position:absolute;inset:0;background:rgba(0,0,0,0.55);-webkit-backdrop-filter:blur(2px);backdrop-filter:blur(2px);}",
    ".apf-card{position:relative;background:var(--bg-secondary,#16213e);color:var(--text-primary,#e0e0e0);border:1px solid var(--border,#2a2a4a);border-radius:var(--radius,10px);padding:1.25rem;width:min(92vw,460px);max-height:90vh;overflow-y:auto;box-shadow:0 20px 60px rgba(0,0,0,0.55);}",
    ".apf-title{font-size:1.05rem;font-weight:600;margin-bottom:0.25rem;}",
    ".apf-subtitle{font-size:0.85rem;color:var(--text-secondary,#a0a0b8);margin-bottom:1rem;}",
    ".apf-list{display:flex;flex-direction:column;gap:0.5rem;margin-bottom:0.5rem;}",
    ".apf-row{display:flex;align-items:center;gap:0.6rem;background:var(--bg-card,#0f3460);border:1px solid var(--border,#2a2a4a);border-radius:8px;padding:0.55rem 0.7rem;}",
    ".apf-info{flex:1;min-width:0;}",
    ".apf-heading{font-size:0.95rem;font-weight:500;color:var(--text-primary,#e0e0e0);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}",
    ".apf-meta{font-size:0.8rem;color:var(--text-secondary,#a0a0b8);margin-top:0.15rem;}",
    ".apf-empty{padding:0.9rem;border:1px dashed var(--border,#2a2a4a);border-radius:8px;font-size:0.9rem;color:var(--text-secondary,#a0a0b8);text-align:center;margin-bottom:0.5rem;}",
    ".apf-error{font-size:0.85rem;color:var(--red,#f87171);margin-top:0.25rem;min-height:1em;}",
    ".apf-actions{display:flex;gap:0.5rem;justify-content:flex-end;margin-top:1rem;align-items:center;}",
    ".apf-btn{background:var(--bg-card,#0f3460);color:var(--text-primary,#e0e0e0);border:1px solid var(--border,#2a2a4a);border-radius:8px;padding:0.55rem 1rem;font-size:0.95rem;font-family:inherit;cursor:pointer;}",
    ".apf-btn:hover{background:var(--bg-card-hover,#134074);}",
    ".apf-btn-primary{background:var(--brand,#935240);border-color:var(--brand,#935240);color:#fff;}",
    ".apf-btn-primary:hover{filter:brightness(1.08);}",
    ".apf-btn-primary:disabled{opacity:0.5;cursor:not-allowed;filter:none;}",
    ".apf-btn-ghost{background:transparent;border-color:transparent;color:var(--text-secondary,#a0a0b8);}",
    ".apf-btn:focus-visible{outline:2px solid var(--brand,#935240);outline-offset:2px;}",
    ".apf-status{font-size:0.85rem;color:var(--text-secondary,#a0a0b8);margin-top:0.25rem;min-height:1em;}",
    "@media (prefers-color-scheme: light){.apf-card{box-shadow:0 20px 60px rgba(0,0,0,0.25);}}",
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
    var currentTargets = Array.isArray(options.currentTargets) ? options.currentTargets : [];
    var sessionDevices = Array.isArray(options.sessionDevices) ? options.sessionDevices : [];
    var send = typeof options.send === "function" ? options.send : null;
    var generateRequestId = typeof options.generateRequestId === "function"
      ? options.generateRequestId
      : function () { return "apf-" + Date.now() + "-" + Math.random().toString(36).slice(2, 8); };
    var onMessage = typeof options.onMessage === "function" ? options.onMessage : null;
    var unit = options.temperatureUnit === "F" ? "F" : "C";

    if (!send || !onMessage) {
      console.warn("[AddProbeFlow] send and onMessage options are required");
      return;
    }

    if (currentInstance) close();
    ensureStyles();

    /* Normalise session-device list to plain string addresses. The
     * caller passes either addresses or {address} records. */
    var sessionDeviceAddrs = sessionDevices.map(function (d) {
      return typeof d === "string" ? d : (d && d.address) || "";
    }).filter(function (s) { return s; });

    /* Build eligible probe list: plugged-in probes on connected
     * devices that do NOT already have a target in the session. We
     * match on probe_index only, since the current target row
     * schema doesn't carry an address binding today. If the index is
     * already targeted anywhere in the session we treat it as
     * claimed -- the server rejects duplicate probe_index entries. */
    var claimedIndices = {};
    currentTargets.forEach(function (t) {
      if (t && t.probe_index != null) claimedIndices[t.probe_index] = true;
    });

    var probeRows = [];
    Object.keys(devices).forEach(function (address) {
      var dev = devices[address];
      var data = (dev && dev.data) || {};
      var deviceName = data.name || address;
      var probes = Array.isArray(data.probes) ? data.probes : [];
      probes.forEach(function (p) {
        if (!p || p.index == null) return;
        if (p.unplugged) return;
        if (p.temperature == null) return;
        if (claimedIndices[p.index]) return;
        probeRows.push({
          address: address,
          probeIndex: p.index,
          deviceName: deviceName,
          temperature: p.temperature,
          label: deviceName + " \u2014 Probe " + p.index,
        });
      });
    });

    /* ---------------- DOM ---------------- */
    var root = document.createElement("div");
    root.className = "apf-root";
    root.setAttribute("role", "dialog");
    root.setAttribute("aria-modal", "true");
    root.setAttribute("aria-labelledby", "apf-title");

    var backdrop = document.createElement("div");
    backdrop.className = "apf-backdrop";
    backdrop.addEventListener("click", function () { close(); });
    root.appendChild(backdrop);

    var card = document.createElement("div");
    card.className = "apf-card";
    root.appendChild(card);

    var title = document.createElement("div");
    title.className = "apf-title";
    title.id = "apf-title";
    title.textContent = "Add a probe";
    card.appendChild(title);

    var subtitle = document.createElement("div");
    subtitle.className = "apf-subtitle";
    subtitle.textContent = probeRows.length === 0
      ? "No eligible probes available right now."
      : "Select a plugged-in probe to add to this cook.";
    card.appendChild(subtitle);

    var body = document.createElement("div");
    card.appendChild(body);

    var errorLine = document.createElement("div");
    errorLine.className = "apf-error";
    errorLine.setAttribute("role", "alert");
    errorLine.setAttribute("aria-live", "polite");
    card.appendChild(errorLine);

    var statusLine = document.createElement("div");
    statusLine.className = "apf-status";
    statusLine.setAttribute("aria-live", "polite");
    card.appendChild(statusLine);

    var actions = document.createElement("div");
    actions.className = "apf-actions";
    var cancelBtn = document.createElement("button");
    cancelBtn.type = "button";
    cancelBtn.className = "apf-btn apf-btn-ghost";
    cancelBtn.textContent = "Cancel";
    cancelBtn.addEventListener("click", function () { close(); });
    actions.appendChild(cancelBtn);
    card.appendChild(actions);

    if (probeRows.length === 0) {
      var empty = document.createElement("div");
      empty.className = "apf-empty";
      empty.textContent = "All plugged-in probes are already in this cook, or no probes are currently reporting. Plug a probe in and try again.";
      body.appendChild(empty);
    } else {
      var list = document.createElement("div");
      list.className = "apf-list";
      probeRows.forEach(function (row) {
        var li = document.createElement("div");
        li.className = "apf-row";
        var info = document.createElement("div");
        info.className = "apf-info";
        var h = document.createElement("div");
        h.className = "apf-heading";
        h.textContent = row.label;
        info.appendChild(h);
        var meta = document.createElement("div");
        meta.className = "apf-meta";
        var tempText = row.temperature != null
          ? Math.round(row.temperature * 10) / 10 + "\u00B0C"
          : "No reading";
        meta.textContent = "Currently " + tempText;
        info.appendChild(meta);
        li.appendChild(info);

        var addBtn = document.createElement("button");
        addBtn.type = "button";
        addBtn.className = "apf-btn apf-btn-primary";
        addBtn.textContent = "Configure";
        addBtn.addEventListener("click", function () { chooseProbe(row); });
        li.appendChild(addBtn);

        list.appendChild(li);
      });
      body.appendChild(list);
    }

    /* ---------------- Flow ---------------- */

    var pendingAddDeviceReqId = null;
    var pendingTargetUpdateReqId = null;
    var pendingRow = null;
    var pendingConfig = null;

    function setStatus(text) {
      statusLine.textContent = text || "";
    }

    function setError(text) {
      errorLine.textContent = text || "";
    }

    function chooseProbe(row) {
      if (!global.ProbeTargetConfigSheet) {
        setError("Target configuration unavailable.");
        return;
      }
      setError("");
      global.ProbeTargetConfigSheet.open({
        probe: {
          address: row.address,
          probeIndex: row.probeIndex,
          label: row.label,
        },
        temperatureUnit: unit,
      }).then(function (config) {
        if (!config) return;
        pendingRow = row;
        pendingConfig = config;
        proceedToSend();
      });
    }

    function proceedToSend() {
      if (!pendingRow || !pendingConfig) return;
      /* If the probe's device isn't in the session yet, add it first.
       * Otherwise go straight to target_update_request. */
      if (sessionDeviceAddrs.indexOf(pendingRow.address) === -1) {
        sendAddDevice();
      } else {
        sendTargetUpdate();
      }
    }

    function sendAddDevice() {
      setStatus("Adding device to session\u2026");
      pendingAddDeviceReqId = generateRequestId();
      try {
        send({
          v: 2,
          type: "session_add_device_request",
          requestId: pendingAddDeviceReqId,
          payload: { deviceAddress: pendingRow.address },
        });
      } catch (e) {
        pendingAddDeviceReqId = null;
        setStatus("");
        setError("Failed to send request: " + (e && e.message ? e.message : e));
      }
    }

    function sendTargetUpdate() {
      setStatus("Adding probe to cook\u2026");
      pendingTargetUpdateReqId = generateRequestId();

      /* Strip `timer` from the new target before sending -- server
       * doesn't accept it on target rows; replay via probe_timer_request
       * after ack. Existing targets already arrive in server shape. */
      var cleanNew = {};
      Object.keys(pendingConfig).forEach(function (k) {
        if (k === "timer") return;
        cleanNew[k] = pendingConfig[k];
      });

      var merged = [cleanNew].concat(currentTargets.slice());

      try {
        send({
          v: 2,
          type: "target_update_request",
          requestId: pendingTargetUpdateReqId,
          payload: { targets: merged },
        });
      } catch (e) {
        pendingTargetUpdateReqId = null;
        setStatus("");
        setError("Failed to send request: " + (e && e.message ? e.message : e));
      }
    }

    function sendTimerUpsert() {
      if (!pendingConfig || !pendingConfig.timer) return;
      var t = pendingConfig.timer;
      if (!t.mode || t.mode === "none") return;
      var payload = {
        action: "upsert",
        address: pendingRow.address,
        probe_index: pendingRow.probeIndex,
        mode: t.mode,
      };
      if (t.mode === "count_down" && t.duration_secs != null) {
        payload.duration_secs = t.duration_secs;
      }
      try {
        send({
          v: 2,
          type: "probe_timer_request",
          requestId: generateRequestId(),
          payload: payload,
        });
      } catch (_) { /* fire and forget */ }
    }

    /* ---------------- Incoming messages ---------------- */

    function handleIncoming(msg) {
      if (!msg || typeof msg !== "object") return;
      var type = msg.type;
      var payload = msg.payload || {};

      if (pendingAddDeviceReqId && msg.requestId === pendingAddDeviceReqId) {
        if (type === "session_add_device_ack") {
          pendingAddDeviceReqId = null;
          if (payload.ok === false) {
            setStatus("");
            setError(payload.error || "Server rejected adding the device.");
            pendingRow = null;
            pendingConfig = null;
            return;
          }
          sessionDeviceAddrs.push(pendingRow.address);
          sendTargetUpdate();
          return;
        }
        if (type === "error") {
          pendingAddDeviceReqId = null;
          setStatus("");
          setError(payload.message || payload.code || "Server error adding device.");
          pendingRow = null;
          pendingConfig = null;
          return;
        }
      }

      if (pendingTargetUpdateReqId && msg.requestId === pendingTargetUpdateReqId) {
        if (type === "target_update_ack") {
          pendingTargetUpdateReqId = null;
          if (payload.ok === false) {
            setStatus("");
            setError(payload.error || "Server rejected the target update.");
            return;
          }
          /* Fire timer upsert (fire-and-forget) and close. */
          sendTimerUpsert();
          close();
          return;
        }
        if (type === "error") {
          pendingTargetUpdateReqId = null;
          setStatus("");
          setError(payload.message || payload.code || "Server error updating targets.");
          return;
        }
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

    setTimeout(function () {
      if (currentInstance !== instance) return;
      var focusables = getFocusable(card);
      if (focusables.length > 0) {
        try { focusables[0].focus(); } catch (_) { /* no-op */ }
      }
    }, 0);
  }

  global.AddProbeFlow = {
    open: open,
    close: close,
  };
})(typeof window !== "undefined" ? window : this);
