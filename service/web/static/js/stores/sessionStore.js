/**
 * SessionStore
 * ------------
 *
 * Single source of truth for session state in the browser, for the
 * session-first web UI redesign. Modelled as a vanilla-JS reducer +
 * subscribe API exposed on `window.SessionStore`, to match the
 * existing dashboard which is a plain IIFE (no React, no bundler).
 *
 * State shape:
 * ```
 * {
 *   status: 'none' | 'configuring' | 'active' | 'ending',
 *   activeSession: {
 *     id, name, startedAt, targetDurationSecs,
 *     targets: [...],           // array of target rows
 *     timers: [...],             // array of per-probe timer rows
 *   } | null,
 *   devices: [ { address, ... } ],
 *   notes: '',                   // primary note body
 *   lastStatusFetchedAt: null,   // ISO timestamp
 *   alerts: {                    // keyed by "address:probeIndex"
 *     "AA:BB..:0": { level: 'approaching'|'reached'|'exceeded'|'reminder', ts }
 *   },
 *   readings: {                  // keyed by address -> { probeIndex -> { tempC, ts } }
 *     "AA:BB...": { 0: { tempC: 42.1, ts: 1712...} }
 *   },
 * }
 * ```
 *
 * The store does NOT own the WebSocket — the existing app owns it.
 * Instead, `SessionStore.attachWebSocket(ws, { onOpen })` installs a
 * passive listener for incoming messages and hydrates on open via
 * `status_request`. This keeps the legacy dashboard working while
 * the new session-first views migrate progressively.
 */
(function (global) {
  "use strict";

  var TERMINAL_ALERT_RANK = {
    approaching: 1,
    reminder: 2,
    reached: 3,
    exceeded: 4,
  };

  function shallowCopy(obj) {
    var out = {};
    for (var k in obj) {
      if (Object.prototype.hasOwnProperty.call(obj, k)) {
        out[k] = obj[k];
      }
    }
    return out;
  }

  function initialState() {
    return {
      status: "none",
      activeSession: null,
      devices: [],
      notes: "",
      lastStatusFetchedAt: null,
      alerts: {},
      readings: {},
    };
  }

  function buildActiveSession(payload) {
    if (!payload || !payload.sessionId) return null;
    return {
      id: payload.sessionId,
      name: payload.name || payload.currentSessionName || null,
      startedAt: payload.sessionStartTs || payload.currentSessionStartTs || null,
      targetDurationSecs:
        payload.targetDurationSecs != null
          ? payload.targetDurationSecs
          : payload.currentTargetDurationSecs != null
            ? payload.currentTargetDurationSecs
            : null,
      targets: payload.targets || payload.activeTargets || [],
      timers: payload.timers || [],
    };
  }

  /**
   * Pure reducer. Given a state and an action `{type, payload}`,
   * returns the next state.
   *
   * Event types mirror the WebSocket `type` field the server sends.
   */
  function reducer(state, action) {
    if (!action || !action.type) return state;
    var type = action.type;
    var payload = action.payload || {};

    switch (type) {
      case "status": {
        var next = shallowCopy(state);
        var hasActive = payload.currentSessionId != null;
        if (hasActive) {
          next.status = "active";
          next.activeSession = {
            id: payload.currentSessionId,
            name: payload.currentSessionName || null,
            startedAt: payload.currentSessionStartTs || null,
            targetDurationSecs:
              payload.currentTargetDurationSecs != null
                ? payload.currentTargetDurationSecs
                : null,
            targets: payload.activeTargets || [],
            /* timers are not part of the status payload today; preserve
             * whatever we already had if this is the same session. */
            timers:
              state.activeSession && state.activeSession.id === payload.currentSessionId
                ? state.activeSession.timers
                : [],
          };
        } else {
          next.status = "none";
          next.activeSession = null;
        }
        next.devices = payload.sessionDevices
          ? payload.sessionDevices.map(function (addr) {
              return typeof addr === "string" ? { address: addr } : addr;
            })
          : state.devices;
        next.lastStatusFetchedAt = new Date().toISOString();
        return next;
      }

      case "session_start":
      case "session_start_ack": {
        /* `session_start_ack` is the requester-scoped confirmation;
         * `session_start` is the broadcast. Both carry the same
         * authoritative payload fields. */
        if (type === "session_start_ack" && payload.ok === false) {
          return state;
        }
        var next2 = shallowCopy(state);
        next2.status = "active";
        next2.activeSession = buildActiveSession(payload);
        next2.notes = "";
        next2.alerts = {};
        next2.readings = {};
        next2.devices = (payload.devices || []).map(function (d) {
          return typeof d === "string" ? { address: d } : d;
        });
        return next2;
      }

      case "session_end":
      case "session_end_ack":
      case "session_discarded":
      case "session_discard_ack": {
        if (
          (type === "session_end_ack" || type === "session_discard_ack") &&
          payload.ok === false
        ) {
          return state;
        }
        var next3 = shallowCopy(state);
        next3.status = "none";
        next3.activeSession = null;
        next3.alerts = {};
        /* Preserve readings/devices/notes — the caller may want the
         * post-cook summary. Individual views can clear as needed. */
        return next3;
      }

      case "target_update_ack": {
        if (payload.ok === false) return state;
        if (!state.activeSession) return state;
        var next4 = shallowCopy(state);
        next4.activeSession = shallowCopy(state.activeSession);
        if (payload.targets) {
          next4.activeSession.targets = payload.targets;
        }
        return next4;
      }

      case "probe_timer_update":
      case "probe_timer_ack": {
        if (type === "probe_timer_ack" && payload.ok === false) return state;
        if (!state.activeSession) return state;
        var timer = payload.timer || payload;
        if (!timer || timer.address == null || timer.probeIndex == null) {
          return state;
        }
        var next5 = shallowCopy(state);
        next5.activeSession = shallowCopy(state.activeSession);
        var existing = state.activeSession.timers || [];
        var replaced = false;
        var updatedTimers = existing.map(function (t) {
          if (t.address === timer.address && t.probeIndex === timer.probeIndex) {
            replaced = true;
            return timer;
          }
          return t;
        });
        if (!replaced) updatedTimers.push(timer);
        next5.activeSession.timers = updatedTimers;
        return next5;
      }

      case "session_notes_update":
      case "session_notes_update_ack": {
        if (type === "session_notes_update_ack" && payload.ok === false) return state;
        var note = payload.note || payload;
        var next6 = shallowCopy(state);
        next6.notes = note && typeof note.body === "string" ? note.body : state.notes;
        return next6;
      }

      case "reading": {
        var address = payload.sensorId;
        if (!address || !payload.data) return state;
        var probes = payload.data.probes;
        if (!Array.isArray(probes)) return state;
        /* The reading payload probes use `index` and `temperature`
         * (see service/models/reading.py build_reading_envelope). */
        var ts = payload.data.last_update
          ? new Date(payload.data.last_update).getTime() / 1000
          : Date.now() / 1000;
        var next7 = shallowCopy(state);
        next7.readings = shallowCopy(state.readings);
        next7.readings[address] = shallowCopy(state.readings[address] || {});
        for (var i = 0; i < probes.length; i++) {
          var p = probes[i];
          if (p && !p.unplugged && p.temperature != null && p.index != null) {
            next7.readings[address][p.index] = {
              tempC: p.temperature,
              ts: ts,
            };
          }
        }
        return next7;
      }

      case "target_approaching":
      case "target_reached":
      case "target_exceeded":
      case "target_reminder": {
        var level =
          type === "target_approaching"
            ? "approaching"
            : type === "target_reached"
              ? "reached"
              : type === "target_exceeded"
                ? "exceeded"
                : "reminder";
        var key = payload.sensorId + ":" + payload.probeIndex;
        var existingAlert = state.alerts[key];
        var currentRank = existingAlert ? TERMINAL_ALERT_RANK[existingAlert.level] || 0 : 0;
        var incomingRank = TERMINAL_ALERT_RANK[level] || 0;
        if (incomingRank < currentRank) return state;
        var next8 = shallowCopy(state);
        next8.alerts = shallowCopy(state.alerts);
        next8.alerts[key] = { level: level, ts: Date.now() };
        return next8;
      }

      case "__reset__":
        return initialState();

      default:
        return state;
    }
  }

  /**
   * Create a new store instance. Most apps will use the singleton
   * exposed on `window.SessionStore` but factory access is handy for
   * testing.
   */
  function createStore() {
    var state = initialState();
    var listeners = [];
    var attachedWs = null;
    var wsMessageHandler = null;

    function getState() {
      return state;
    }

    function dispatch(action) {
      var next = reducer(state, action);
      if (next !== state) {
        state = next;
        for (var i = 0; i < listeners.length; i++) {
          try {
            listeners[i](state, action);
          } catch (e) {
            /* A bad listener shouldn't kill the store. */
            if (typeof console !== "undefined") {
              console.warn("[SessionStore] listener threw:", e);
            }
          }
        }
      }
      return action;
    }

    function subscribe(fn) {
      listeners.push(fn);
      return function unsubscribe() {
        var idx = listeners.indexOf(fn);
        if (idx !== -1) listeners.splice(idx, 1);
      };
    }

    /**
     * Attach the store to an already-constructed WebSocket. The store
     * does NOT own the socket; it just listens to `message` and, on
     * open, sends a `status_request` to hydrate.
     *
     * Returns a detach() function.
     */
    function attachWebSocket(ws, opts) {
      opts = opts || {};
      detach();
      attachedWs = ws;

      wsMessageHandler = function (event) {
        var msg;
        try {
          msg = JSON.parse(event.data);
        } catch (e) {
          return;
        }
        if (msg && msg.type) {
          dispatch({ type: msg.type, payload: msg.payload || {} });
        }
      };
      ws.addEventListener("message", wsMessageHandler);

      function requestStatus() {
        if (ws.readyState !== 1 /* OPEN */) return;
        try {
          ws.send(
            JSON.stringify({
              v: 2,
              type: "status_request",
              requestId: "sessionstore-hydrate-" + Date.now(),
              payload: {},
            })
          );
        } catch (e) {
          if (typeof console !== "undefined") {
            console.warn("[SessionStore] status_request failed:", e);
          }
        }
      }

      if (ws.readyState === 1 /* OPEN */) {
        requestStatus();
      } else {
        ws.addEventListener("open", requestStatus, { once: true });
      }

      if (opts.onOpen) {
        ws.addEventListener("open", opts.onOpen);
      }

      return detach;
    }

    function detach() {
      if (attachedWs && wsMessageHandler) {
        attachedWs.removeEventListener("message", wsMessageHandler);
      }
      attachedWs = null;
      wsMessageHandler = null;
    }

    function reset() {
      dispatch({ type: "__reset__" });
    }

    return {
      getState: getState,
      dispatch: dispatch,
      subscribe: subscribe,
      attachWebSocket: attachWebSocket,
      detach: detach,
      reset: reset,
    };
  }

  global.SessionStore = {
    createStore: createStore,
    reducer: reducer,
    initialState: initialState,
    /* Singleton for the dashboard to use directly. */
    instance: createStore(),
  };
})(typeof window !== "undefined" ? window : this);
