/**
 * RollingBuffer
 * -------------
 *
 * A pure, in-memory sliding-window store of probe temperature samples
 * used by `IdleCookView` for the 3-minute live sparkline preview.
 *
 * Keyed by `(address, probeIndex)`; each entry is an array of
 * `{ts, tempC}` points. Samples older than `WINDOW_SECONDS` are pruned
 * on every push. All buffers are cleared when `session_start` arrives
 * (the new cook starts with a blank chart; the 30-minute session chart
 * takes over from there).
 *
 * Exposed on `window.RollingBuffer` as both a factory (`createBuffer`)
 * and a singleton (`instance`) for the legacy dashboard IIFE to use
 * directly.
 */
(function (global) {
  "use strict";

  var WINDOW_SECONDS = 3 * 60; /* 3 minutes */

  function keyOf(address, probeIndex) {
    return address + ":" + probeIndex;
  }

  function createBuffer(opts) {
    opts = opts || {};
    var windowSecs = opts.windowSecs || WINDOW_SECONDS;

    /* Map<string, Array<{ts:number, tempC:number}>> */
    var series = Object.create(null);

    /**
     * Return the current epoch-seconds time, either from a test-only
     * override or `Date.now() / 1000`. Kept internal so callers can't
     * accidentally drift.
     */
    function now() {
      if (typeof opts.now === "function") return opts.now();
      return Date.now() / 1000;
    }

    function pruneKey(key, reference) {
      var arr = series[key];
      if (!arr || arr.length === 0) return;
      var cutoff = reference - windowSecs;
      /* Most samples are newer than cutoff; find the first index we keep. */
      var i = 0;
      while (i < arr.length && arr[i].ts < cutoff) i++;
      if (i > 0) {
        arr.splice(0, i);
      }
    }

    /**
     * Add a sample. Prunes anything older than the window.
     *
     * @param {string} address    - device MAC / address
     * @param {number} probeIndex - probe index (0-based or 1-based; caller's convention)
     * @param {number} timestamp  - epoch seconds
     * @param {number} tempC      - temperature in Celsius; null/undefined are ignored
     */
    function push(address, probeIndex, timestamp, tempC) {
      if (address == null || probeIndex == null) return;
      if (tempC == null || isNaN(tempC)) return;
      if (timestamp == null || isNaN(timestamp)) timestamp = now();
      var key = keyOf(address, probeIndex);
      var arr = series[key];
      if (!arr) {
        arr = [];
        series[key] = arr;
      }
      /* Append; if an older sample somehow arrives, keep order sorted
       * by ts so `getSeries()` returns monotonic data. */
      if (arr.length === 0 || arr[arr.length - 1].ts <= timestamp) {
        arr.push({ ts: timestamp, tempC: tempC });
      } else {
        var lo = 0,
          hi = arr.length;
        while (lo < hi) {
          var mid = (lo + hi) >>> 1;
          if (arr[mid].ts <= timestamp) lo = mid + 1;
          else hi = mid;
        }
        arr.splice(lo, 0, { ts: timestamp, tempC: tempC });
      }
      pruneKey(key, timestamp);
    }

    /**
     * Return a shallow copy of the series for `(address, probeIndex)`,
     * pruned to the current window. Empty array if nothing is buffered.
     */
    function getSeries(address, probeIndex) {
      var key = keyOf(address, probeIndex);
      pruneKey(key, now());
      var arr = series[key];
      if (!arr) return [];
      /* Copy so callers can't accidentally mutate internal state. */
      return arr.slice();
    }

    /**
     * Drop every sample for every series. Called on `session_start`.
     */
    function reset() {
      series = Object.create(null);
    }

    /**
     * Install a listener on the given SessionStore (or any object
     * exposing `subscribe(fn)` whose callbacks receive
     * `(state, action)`). Returns an unsubscribe function.
     */
    function attachToStore(store) {
      if (!store || typeof store.subscribe !== "function") {
        return function () {};
      }
      return store.subscribe(function (_state, action) {
        if (!action || !action.type) return;
        if (action.type === "session_start" || action.type === "session_start_ack") {
          reset();
        } else if (action.type === "reading") {
          var payload = action.payload || {};
          var address = payload.sensorId;
          if (!address || !payload.data) return;
          var probes = payload.data.probes;
          if (!Array.isArray(probes)) return;
          /* Reading payload probes use `index` and `temperature`
           * (see service/models/reading.py). */
          var ts = payload.data.last_update
            ? new Date(payload.data.last_update).getTime() / 1000
            : now();
          for (var i = 0; i < probes.length; i++) {
            var p = probes[i];
            if (p && !p.unplugged && p.temperature != null && p.index != null) {
              push(address, p.index, ts, p.temperature);
            }
          }
        }
      });
    }

    /**
     * Diagnostic helper — not part of the public contract, but
     * useful for tests and browser console inspection.
     */
    function _debugSeriesKeys() {
      var keys = [];
      for (var k in series) {
        if (Object.prototype.hasOwnProperty.call(series, k)) keys.push(k);
      }
      return keys;
    }

    return {
      push: push,
      getSeries: getSeries,
      reset: reset,
      attachToStore: attachToStore,
      _debugSeriesKeys: _debugSeriesKeys,
      WINDOW_SECONDS: windowSecs,
    };
  }

  global.RollingBuffer = {
    createBuffer: createBuffer,
    WINDOW_SECONDS: WINDOW_SECONDS,
    /* Singleton for the dashboard. */
    instance: createBuffer(),
  };
})(typeof window !== "undefined" ? window : this);
