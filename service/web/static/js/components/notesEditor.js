/**
 * NotesEditor
 * -----------
 *
 * Reusable modal for editing a session's primary note body. Used by:
 *   - ActiveSessionView (Task 10) -- edit the live session note while
 *     the cook is running.
 *   - History tab (Task 12) -- edit notes on saved sessions.
 *
 * Behaviour:
 *   - Opens as a modal with a single <textarea> pre-populated with
 *     `initialBody`.
 *   - Debounced (500ms) auto-save: every change kicks a timer; on
 *     expiry we call `onSave(body)`. This fires a
 *     `session_notes_update_request` upstream. No explicit Save
 *     button -- auto-save is the contract.
 *   - A Done button closes the modal. If a save is pending (timer
 *     armed) we flush it first so the ack/broadcast completes before
 *     the user leaves.
 *   - External `session_notes_update` broadcasts that mutate the
 *     primary note body are pushed in via `setBody()` so multi-
 *     client editing stays coherent. We skip updates for strings the
 *     user has locally unsaved -- if the textarea differs from the
 *     last body we broadcast, we keep the local value to avoid
 *     clobbering the user's in-flight edit.
 *
 * Usage:
 *   var editor = window.NotesEditor.open({
 *     sessionId,          // optional -- included in the save payload
 *     initialBody: '...',
 *     onSave: function (body) { ... send WS message ... },
 *   });
 *   // editor.setBody(newBody);  -- push an external update
 *   // editor.close();           -- programmatic close
 *
 * Styling mirrors ProbeTargetConfigSheet / StartCookFlow for visual
 * consistency. Scoped under `.ne-root`.
 */
(function (global) {
  "use strict";

  var STYLE_ID = "ne-styles";
  var STYLE_CSS = [
    ".ne-root{position:fixed;inset:0;z-index:97;display:flex;align-items:center;justify-content:center;font-family:inherit;}",
    ".ne-backdrop{position:absolute;inset:0;background:rgba(0,0,0,0.55);-webkit-backdrop-filter:blur(2px);backdrop-filter:blur(2px);}",
    ".ne-card{position:relative;background:var(--bg-secondary,#16213e);color:var(--text-primary,#e0e0e0);border:1px solid var(--border,#2a2a4a);border-radius:var(--radius,10px);padding:1.25rem;width:min(92vw,560px);max-height:90vh;display:flex;flex-direction:column;gap:0.75rem;box-shadow:0 20px 60px rgba(0,0,0,0.55);}",
    ".ne-title{font-size:1.05rem;font-weight:600;}",
    ".ne-subtitle{font-size:0.85rem;color:var(--text-secondary,#a0a0b8);}",
    ".ne-textarea{background:var(--bg-card,#0f3460);color:var(--text-primary,#e0e0e0);border:1px solid var(--border,#2a2a4a);border-radius:8px;padding:0.7rem 0.8rem;font-size:0.95rem;font-family:inherit;width:100%;min-height:220px;resize:vertical;box-sizing:border-box;}",
    ".ne-textarea:focus{outline:2px solid var(--brand,#935240);outline-offset:0;border-color:var(--brand,#935240);}",
    ".ne-status{font-size:0.8rem;color:var(--text-secondary,#a0a0b8);min-height:1.1em;display:flex;align-items:center;gap:0.35rem;}",
    ".ne-status.saving{color:var(--amber,#f5a623);}",
    ".ne-status.saved{color:var(--green,#4ade80);}",
    ".ne-status.error{color:var(--red,#f87171);}",
    ".ne-actions{display:flex;gap:0.5rem;justify-content:flex-end;margin-top:0.25rem;}",
    ".ne-btn{background:var(--bg-card,#0f3460);color:var(--text-primary,#e0e0e0);border:1px solid var(--border,#2a2a4a);border-radius:8px;padding:0.55rem 1rem;font-size:0.95rem;font-family:inherit;cursor:pointer;}",
    ".ne-btn:hover{background:var(--bg-card-hover,#134074);}",
    ".ne-btn:focus-visible{outline:2px solid var(--brand,#935240);outline-offset:2px;}",
    ".ne-btn-primary{background:var(--brand,#935240);border-color:var(--brand,#935240);color:#fff;}",
    ".ne-btn-primary:hover{filter:brightness(1.08);}",
    "@media (prefers-color-scheme: light){.ne-card{box-shadow:0 20px 60px rgba(0,0,0,0.25);}}",
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

  var DEBOUNCE_MS = 500;

  var currentInstance = null;

  function closeInstance(inst) {
    if (!inst) return;
    /* Flush a pending debounce before closing so in-flight text is
     * sent. No-op if there's nothing pending or content matches last
     * saved value. */
    if (inst.debounceTimer) {
      clearTimeout(inst.debounceTimer);
      inst.debounceTimer = null;
      inst.flushIfDirty();
    }
    try { document.removeEventListener("keydown", inst.onKeyDown, true); }
    catch (_) { /* no-op */ }
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
    closeInstance(inst);
  }

  function open(options) {
    options = options || {};
    var sessionId = options.sessionId || null;
    var initialBody = typeof options.initialBody === "string" ? options.initialBody : "";
    var onSave = typeof options.onSave === "function" ? options.onSave : function () {};
    var titleText = options.title || "Session note";
    var subtitleText = options.subtitle || "Changes are saved automatically.";

    if (currentInstance) close();
    ensureStyles();

    /* DOM ------------------------------------------------------------ */
    var root = document.createElement("div");
    root.className = "ne-root";
    root.setAttribute("role", "dialog");
    root.setAttribute("aria-modal", "true");
    root.setAttribute("aria-labelledby", "ne-title");

    var backdrop = document.createElement("div");
    backdrop.className = "ne-backdrop";
    backdrop.addEventListener("click", function () { close(); });
    root.appendChild(backdrop);

    var card = document.createElement("div");
    card.className = "ne-card";
    root.appendChild(card);

    var title = document.createElement("div");
    title.className = "ne-title";
    title.id = "ne-title";
    title.textContent = titleText;
    card.appendChild(title);

    var subtitle = document.createElement("div");
    subtitle.className = "ne-subtitle";
    subtitle.textContent = subtitleText;
    card.appendChild(subtitle);

    var textarea = document.createElement("textarea");
    textarea.className = "ne-textarea";
    textarea.value = initialBody;
    textarea.setAttribute("aria-label", "Session note body");
    textarea.placeholder = "Jot down cook notes -- pellet temp, spritzes, wrap time, anything.";
    card.appendChild(textarea);

    var status = document.createElement("div");
    status.className = "ne-status";
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    card.appendChild(status);

    var actions = document.createElement("div");
    actions.className = "ne-actions";
    var doneBtn = document.createElement("button");
    doneBtn.type = "button";
    doneBtn.className = "ne-btn ne-btn-primary";
    doneBtn.textContent = "Done";
    doneBtn.addEventListener("click", function () { close(); });
    actions.appendChild(doneBtn);
    card.appendChild(actions);

    /* Instance state ------------------------------------------------- */
    var inst = {
      root: root,
      previousFocus: document.activeElement,
      debounceTimer: null,
      lastSavedBody: initialBody,
      pendingBody: initialBody,
    };

    function setStatus(kind, text) {
      status.className = "ne-status" + (kind ? " " + kind : "");
      status.textContent = text || "";
    }

    function doSave(body) {
      inst.pendingBody = body;
      setStatus("saving", "Saving\u2026");
      try {
        onSave(body);
        inst.lastSavedBody = body;
        setStatus("saved", "Saved");
        /* Clear the "Saved" badge after a short beat so it doesn't
         * linger as stale feedback. */
        setTimeout(function () {
          if (!currentInstance || currentInstance !== inst) return;
          if (status.textContent === "Saved") setStatus("", "");
        }, 1500);
      } catch (e) {
        setStatus("error", "Save failed");
        if (typeof console !== "undefined") {
          console.warn("[NotesEditor] onSave threw:", e);
        }
      }
    }

    inst.flushIfDirty = function () {
      var body = textarea.value;
      if (body === inst.lastSavedBody) return;
      doSave(body);
    };

    function scheduleSave() {
      if (inst.debounceTimer) clearTimeout(inst.debounceTimer);
      var body = textarea.value;
      if (body === inst.lastSavedBody) {
        setStatus("", "");
        return;
      }
      setStatus("", "Unsaved changes\u2026");
      inst.debounceTimer = setTimeout(function () {
        inst.debounceTimer = null;
        if (currentInstance !== inst) return;
        doSave(textarea.value);
      }, DEBOUNCE_MS);
    }

    textarea.addEventListener("input", scheduleSave);

    /* External updates: called by the caller when a
     * `session_notes_update` broadcast arrives. Don't clobber local
     * unsaved edits -- if the user's current text differs from what we
     * last saved, they have in-flight edits we shouldn't replace. */
    inst.setBody = function (nextBody) {
      if (typeof nextBody !== "string") return;
      if (textarea.value !== inst.lastSavedBody) return; /* local edits pending */
      if (textarea.value === nextBody) return;
      textarea.value = nextBody;
      inst.lastSavedBody = nextBody;
      inst.pendingBody = nextBody;
    };

    inst.onKeyDown = function (e) {
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
    };

    currentInstance = inst;
    document.body.appendChild(root);
    document.addEventListener("keydown", inst.onKeyDown, true);

    setTimeout(function () {
      if (currentInstance !== inst) return;
      try {
        textarea.focus();
        /* Put the caret at the end rather than selecting all. */
        var len = textarea.value.length;
        textarea.setSelectionRange(len, len);
      } catch (_) { /* no-op */ }
    }, 0);

    return {
      close: close,
      setBody: function (body) { inst.setBody(body); },
      /* Expose sessionId so callers can correlate updates. */
      sessionId: sessionId,
    };
  }

  global.NotesEditor = {
    open: open,
    close: close,
    _internals: {
      DEBOUNCE_MS: DEBOUNCE_MS,
    },
  };
})(typeof window !== "undefined" ? window : this);
