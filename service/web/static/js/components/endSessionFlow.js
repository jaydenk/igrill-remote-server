/**
 * EndSessionFlow
 * --------------
 *
 * Task 11: three-button modal for ending the active cook.
 *
 *   Save     -> sends `session_end_request`. Modal closes on
 *               `session_end_ack` (or `session_end` broadcast) or an
 *               `error` with a matching `requestId`.
 *   Discard  -> opens a secondary confirmation. Confirm sends
 *               `session_discard_request`. Modal closes on
 *               `session_discarded` broadcast, `session_discard_ack`,
 *               or a matching `error`.
 *   Cancel   -> dismisses without sending anything; the session keeps
 *               running.
 *
 * The discard flow intentionally requires a second confirmation --
 * the action is destructive (hard-deletes the active session and all
 * child data on the server) and we want to make that the explicit
 * default-off option.
 *
 * Usage:
 *   window.EndSessionFlow.open({
 *     send,               // fn(envelope) -> void
 *     generateRequestId,  // fn() -> string
 *     onMessage,          // fn(handler) -> unsubscribe
 *   });
 */
(function (global) {
  "use strict";

  var STYLE_ID = "esf-styles";
  var STYLE_CSS = [
    ".esf-root{position:fixed;inset:0;z-index:96;display:flex;align-items:center;justify-content:center;font-family:inherit;}",
    ".esf-backdrop{position:absolute;inset:0;background:rgba(0,0,0,0.55);-webkit-backdrop-filter:blur(2px);backdrop-filter:blur(2px);}",
    ".esf-card{position:relative;background:var(--bg-secondary,#16213e);color:var(--text-primary,#e0e0e0);border:1px solid var(--border,#2a2a4a);border-radius:var(--radius,10px);padding:1.25rem;width:min(92vw,440px);box-shadow:0 20px 60px rgba(0,0,0,0.55);}",
    ".esf-title{font-size:1.05rem;font-weight:600;margin-bottom:0.35rem;}",
    ".esf-subtitle{font-size:0.9rem;color:var(--text-secondary,#a0a0b8);margin-bottom:1rem;line-height:1.35;}",
    ".esf-actions{display:flex;gap:0.5rem;justify-content:space-between;align-items:center;flex-wrap:wrap;}",
    ".esf-actions-right{display:flex;gap:0.5rem;}",
    ".esf-btn{background:var(--bg-card,#0f3460);color:var(--text-primary,#e0e0e0);border:1px solid var(--border,#2a2a4a);border-radius:8px;padding:0.55rem 1rem;font-size:0.95rem;font-family:inherit;cursor:pointer;}",
    ".esf-btn:hover{background:var(--bg-card-hover,#134074);}",
    ".esf-btn-primary{background:var(--brand,#935240);border-color:var(--brand,#935240);color:#fff;}",
    ".esf-btn-primary:hover{filter:brightness(1.08);}",
    ".esf-btn-primary:disabled{opacity:0.5;cursor:not-allowed;filter:none;}",
    ".esf-btn-danger{background:transparent;color:var(--red,#f87171);border-color:rgba(248,113,113,0.45);}",
    ".esf-btn-danger:hover{background:rgba(248,113,113,0.12);}",
    ".esf-btn-ghost{background:transparent;border-color:transparent;color:var(--text-secondary,#a0a0b8);}",
    ".esf-btn:focus-visible{outline:2px solid var(--brand,#935240);outline-offset:2px;}",
    ".esf-error{font-size:0.85rem;color:var(--red,#f87171);margin-bottom:0.6rem;min-height:1em;}",
    ".esf-status{font-size:0.85rem;color:var(--text-secondary,#a0a0b8);margin-bottom:0.6rem;min-height:1em;}",
    "@media (prefers-color-scheme: light){.esf-card{box-shadow:0 20px 60px rgba(0,0,0,0.25);}}",
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
    var send = typeof options.send === "function" ? options.send : null;
    var generateRequestId = typeof options.generateRequestId === "function"
      ? options.generateRequestId
      : function () { return "esf-" + Date.now() + "-" + Math.random().toString(36).slice(2, 8); };
    var onMessage = typeof options.onMessage === "function" ? options.onMessage : null;

    if (!send || !onMessage) {
      console.warn("[EndSessionFlow] send and onMessage options are required");
      return;
    }

    if (currentInstance) close();
    ensureStyles();

    /* View state. `mode` is "primary" (Save/Discard/Cancel) or
     * "confirm-discard" (Confirm/Cancel secondary). */
    var state = {
      mode: "primary",
      submitting: false,
      pendingRequestId: null,
      pendingType: null, /* 'end' | 'discard' */
      error: "",
      statusText: "",
    };

    /* DOM ------------------------------------------------------------ */
    var root = document.createElement("div");
    root.className = "esf-root";
    root.setAttribute("role", "dialog");
    root.setAttribute("aria-modal", "true");
    root.setAttribute("aria-labelledby", "esf-title");

    var backdrop = document.createElement("div");
    backdrop.className = "esf-backdrop";
    backdrop.addEventListener("click", function () {
      if (state.submitting) return;
      close();
    });
    root.appendChild(backdrop);

    var card = document.createElement("div");
    card.className = "esf-card";
    root.appendChild(card);

    var title = document.createElement("div");
    title.className = "esf-title";
    title.id = "esf-title";
    card.appendChild(title);

    var subtitle = document.createElement("div");
    subtitle.className = "esf-subtitle";
    card.appendChild(subtitle);

    var statusLine = document.createElement("div");
    statusLine.className = "esf-status";
    statusLine.setAttribute("aria-live", "polite");
    card.appendChild(statusLine);

    var errorLine = document.createElement("div");
    errorLine.className = "esf-error";
    errorLine.setAttribute("role", "alert");
    card.appendChild(errorLine);

    var actions = document.createElement("div");
    actions.className = "esf-actions";
    card.appendChild(actions);

    function render() {
      title.textContent = state.mode === "primary"
        ? "End session"
        : "Permanently discard this cook?";
      subtitle.textContent = state.mode === "primary"
        ? "Save the cook to history, or discard it without saving."
        : "This permanently deletes all data from this cook: readings, targets, timers, and notes. Continue?";

      errorLine.textContent = state.error || "";
      statusLine.textContent = state.statusText || "";

      while (actions.firstChild) actions.removeChild(actions.firstChild);

      if (state.mode === "primary") {
        var cancelBtn = document.createElement("button");
        cancelBtn.type = "button";
        cancelBtn.className = "esf-btn esf-btn-ghost";
        cancelBtn.textContent = "Cancel";
        cancelBtn.disabled = state.submitting;
        cancelBtn.addEventListener("click", function () { if (!state.submitting) close(); });
        actions.appendChild(cancelBtn);

        var right = document.createElement("div");
        right.className = "esf-actions-right";

        var discardBtn = document.createElement("button");
        discardBtn.type = "button";
        discardBtn.className = "esf-btn esf-btn-danger";
        discardBtn.textContent = "Discard";
        discardBtn.disabled = state.submitting;
        discardBtn.addEventListener("click", function () {
          if (state.submitting) return;
          state.mode = "confirm-discard";
          state.error = "";
          render();
        });
        right.appendChild(discardBtn);

        var saveBtn = document.createElement("button");
        saveBtn.type = "button";
        saveBtn.className = "esf-btn esf-btn-primary";
        saveBtn.textContent = state.submitting && state.pendingType === "end" ? "Saving\u2026" : "Save";
        saveBtn.disabled = state.submitting;
        saveBtn.addEventListener("click", function () { onSave(); });
        right.appendChild(saveBtn);

        actions.appendChild(right);

        setTimeout(function () {
          if (currentInstance !== instance) return;
          try { saveBtn.focus(); } catch (_) { /* no-op */ }
        }, 0);
      } else {
        var backBtn = document.createElement("button");
        backBtn.type = "button";
        backBtn.className = "esf-btn esf-btn-ghost";
        backBtn.textContent = "Cancel";
        backBtn.disabled = state.submitting;
        backBtn.addEventListener("click", function () {
          if (state.submitting) return;
          state.mode = "primary";
          state.error = "";
          render();
        });
        actions.appendChild(backBtn);

        var confirmBtn = document.createElement("button");
        confirmBtn.type = "button";
        confirmBtn.className = "esf-btn esf-btn-danger";
        confirmBtn.textContent = state.submitting && state.pendingType === "discard"
          ? "Discarding\u2026"
          : "Confirm discard";
        confirmBtn.disabled = state.submitting;
        confirmBtn.addEventListener("click", function () { onConfirmDiscard(); });
        actions.appendChild(confirmBtn);

        setTimeout(function () {
          if (currentInstance !== instance) return;
          /* Put initial focus on Cancel to make destructive intent
           * explicit -- the user has to deliberately tab to Confirm. */
          try { backBtn.focus(); } catch (_) { /* no-op */ }
        }, 0);
      }
    }

    function onSave() {
      if (state.submitting) return;
      state.submitting = true;
      state.pendingType = "end";
      state.error = "";
      state.statusText = "Saving cook\u2026";
      state.pendingRequestId = generateRequestId();
      try {
        send({
          v: 2,
          type: "session_end_request",
          requestId: state.pendingRequestId,
          payload: {},
        });
      } catch (e) {
        state.submitting = false;
        state.pendingType = null;
        state.pendingRequestId = null;
        state.statusText = "";
        state.error = "Failed to send request: " + (e && e.message ? e.message : e);
      }
      render();
    }

    function onConfirmDiscard() {
      if (state.submitting) return;
      state.submitting = true;
      state.pendingType = "discard";
      state.error = "";
      state.statusText = "Discarding cook\u2026";
      state.pendingRequestId = generateRequestId();
      try {
        send({
          v: 2,
          type: "session_discard_request",
          requestId: state.pendingRequestId,
          payload: {},
        });
      } catch (e) {
        state.submitting = false;
        state.pendingType = null;
        state.pendingRequestId = null;
        state.statusText = "";
        state.error = "Failed to send request: " + (e && e.message ? e.message : e);
      }
      render();
    }

    /* ---------------- Incoming messages ---------------- */

    function handleIncoming(msg) {
      if (!msg || typeof msg !== "object") return;
      var type = msg.type;
      var payload = msg.payload || {};

      /* Broadcasts without matching requestId also count: a session
       * may have been ended/discarded from another client. */
      if (state.pendingType === "end") {
        if (type === "session_end_ack" && msg.requestId === state.pendingRequestId) {
          if (payload.ok === false) {
            state.submitting = false;
            state.pendingType = null;
            state.pendingRequestId = null;
            state.statusText = "";
            state.error = payload.error || "Server rejected ending the session.";
            render();
            return;
          }
          close();
          return;
        }
        if (type === "session_end") {
          close();
          return;
        }
        if (type === "error" && msg.requestId === state.pendingRequestId) {
          state.submitting = false;
          state.pendingType = null;
          state.pendingRequestId = null;
          state.statusText = "";
          state.error = payload.message || payload.code || "Server error ending the session.";
          render();
          return;
        }
      } else if (state.pendingType === "discard") {
        if (type === "session_discard_ack" && msg.requestId === state.pendingRequestId) {
          if (payload.ok === false) {
            state.submitting = false;
            state.pendingType = null;
            state.pendingRequestId = null;
            state.statusText = "";
            state.error = payload.error || "Server rejected discarding the session.";
            render();
            return;
          }
          close();
          return;
        }
        if (type === "session_discarded") {
          close();
          return;
        }
        if (type === "error" && msg.requestId === state.pendingRequestId) {
          state.submitting = false;
          state.pendingType = null;
          state.pendingRequestId = null;
          state.statusText = "";
          state.error = payload.message || payload.code || "Server error discarding the session.";
          render();
          return;
        }
      } else {
        /* No request in flight -- still honour remote session_end /
         * session_discarded broadcasts so the modal doesn't linger
         * after another client ends the cook. */
        if (type === "session_end" || type === "session_discarded") {
          close();
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
          if (state.submitting) return;
          if (state.mode === "confirm-discard") {
            state.mode = "primary";
            state.error = "";
            render();
            return;
          }
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
    render();
  }

  global.EndSessionFlow = {
    open: open,
    close: close,
  };
})(typeof window !== "undefined" ? window : this);
