# Session-First Redesign — Web UI Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:executing-plans to implement this plan task-by-task.

**Goal:** Restructure the web dashboard to be session-first (Cook / History / Settings), matching the iOS app's model. Devices become a supporting concept within Settings.

**Architecture:** Single React SPA served by the server at `/service/web/static/index.html`. Introduce a `SessionStore` React context as the single source of truth for session state, subscribing to WebSocket events. Cook tab renders `IdleCookView` or `ActiveSessionView` based on session state. Rolling 3-minute preview buffer lives in browser memory.

**Tech Stack:** React (existing), uPlot (charts), WebSocket v2 envelope protocol.

**Prerequisite:** Server plan must be at least Task 11 complete (new WebSocket handlers exist) before end-to-end testing of Cook flows.

**Design Reference:** `../../../docs/plans/2026-04-12-session-first-redesign-design.md` (sections 3, 4, 9).

---

### Task 0: Prep

**Steps:**
1. Read `service/web/static/index.html` and any bundled JS to understand current component layout.
2. Confirm the existing build/serve workflow (is there a bundler, or is index.html inline React with CDN?).
3. Create a branch.

**Acceptance:** You can describe, in writing, how the current dashboard is structured and served.

---

### Task 1: `SessionStore` context

**Files:**
- Create: `service/web/static/js/stores/sessionStore.js` (or equivalent location given current structure)
- Modify: `service/web/static/index.html` (wire provider)

**Steps:**
1. Define the state shape: `{status: 'none' | 'configuring' | 'active' | 'ending', activeSession: {...} | null, devices: [...], targets: [...], timers: [...], notes: '', lastStatusFetchedAt}`.
2. Reducer actions: `status`, `session_start`, `session_end`, `session_discarded`, `target_update_ack`, `probe_timer_update`, `session_notes_update`, `reading`, `alert`.
3. Subscribe to WebSocket events (existing connection) and dispatch reducer actions.
4. On mount, send a `status_request` to hydrate.

**Acceptance:** Unit test the reducer covering each action (if a test harness exists). At minimum, demo in-browser: connect, send a `status` event manually, observe store state change.

---

### Task 2: `RollingBuffer` module

**Files:**
- Create: `service/web/static/js/stores/rollingBuffer.js`

**Steps:**
1. Pure JS module with `push(deviceAddr, probeIndex, timestamp, tempC)` and `getSeries(deviceAddr, probeIndex)`.
2. Prune entries older than 3 minutes on every push.
3. Reset all buffers when session starts (listen to `session_start` event).

**Acceptance:** Unit test prune + reset behaviour.

---

### Task 3: Three-tab layout

**Files:**
- Modify: `service/web/static/index.html` (or the top-level component)

**Steps:**
1. Replace current tab bar with three tabs: Cook (default), History, Settings.
2. Remove the "Live" tab; move the devices listing to a component that Settings will consume.
3. Route selection stored in URL hash for linkability (`#cook`, `#history`, `#settings`).

**Acceptance:** Each tab renders placeholder content; hash changes work; no console errors.

---

### Task 4: `IdleCookView`

**Files:**
- Create: `service/web/static/js/views/IdleCookView.jsx` (or equivalent)

**Steps:**
1. Render a prominent "Start Cook" button (disabled if no devices connected).
2. For each connected device, render a card: device name, connection status, current probe temps.
3. Inline sparkline per probe using the rolling buffer (simple SVG polyline is fine; uPlot overkill for 3-min preview).
4. Empty state: "No devices connected — check Settings" with a Settings-tab link.

**Acceptance:** With the server in idle state and a simulator running briefly to populate data, Idle view shows a sparkline climbing.

---

### Task 5: `ProbeTargetConfigSheet` modal

**Files:**
- Create: `service/web/static/js/components/ProbeTargetConfigSheet.jsx`

**Steps:**
1. Modal that takes `{device, probeIndex, existingTarget?}` and returns a `TargetConfig` via `onConfirm`.
2. Fields: target mode (fixed / range), target temp or low/high, pre-alert offset (default 5°C), reminder interval (default 0 = off), optional per-probe timer (mode count-up / count-down, duration).
3. Validation: count-down requires duration > 0; range requires low < high.
4. Confirm/Cancel buttons.

**Acceptance:** Open, fill, confirm, observe returned payload shape matches server `TargetConfig` schema.

---

### Task 6: Start Cook flow

**Files:**
- Modify: `IdleCookView.jsx`

**Steps:**
1. Clicking "Start Cook" opens a multi-step flow: pick probes + configure targets (reuse `ProbeTargetConfigSheet`) + optional session name + optional target cook duration.
2. On confirm, send `session_start_request` with the compiled targets and `target_duration_secs`.
3. On receipt of `session_start` broadcast, transition the Cook tab to Active view.

**Acceptance:** Start a session from the web UI; observe DB rows in the server; observe Active view render.

---

### Task 7: `ActiveSessionView` skeleton

**Files:**
- Create: `service/web/static/js/views/ActiveSessionView.jsx`

**Steps:**
1. Header: session name (editable inline), elapsed timer (ticking every second), target duration progress bar if set.
2. Chart area: reuse existing uPlot chart, now filtered to probes with targets.
3. Probe cards grid.
4. Action row: Add Probe, Add Note, End Session.

**Acceptance:** View renders with simulator running; chart accumulates; elapsed timer ticks.

---

### Task 8: Probe cards with timer UI

**Files:**
- Create: `service/web/static/js/components/ProbeCard.jsx`

**Steps:**
1. Current temp, target display, status chip (climbing / in range / over).
2. Per-probe timer controls: if no timer configured, show "Add timer" link opening the timer section of the config sheet. If configured, show running/paused state + Start/Pause/Resume/Reset buttons.
3. Countdown shows remaining; count-up shows elapsed.
4. Clicking a control sends `probe_timer_request`; UI updates on `probe_timer_update` broadcast (optimistic updates optional).

**Acceptance:** Start/pause/reset a timer from web UI; observe DB state; open the app (simulator) and confirm sync.

---

### Task 9: Add Probe mid-session

**Files:**
- Modify: `ActiveSessionView.jsx`

**Steps:**
1. "Add Probe" opens a device/probe picker filtered to probes that don't have a target in the current session.
2. Selecting opens `ProbeTargetConfigSheet` → confirm sends `target_update_request`.
3. On `target_update_ack` broadcast, chart + probe cards reflect the new probe.

**Acceptance:** Start a 1-probe session; mid-flight add a second probe; observe it appear.

---

### Task 10: Notes

**Files:**
- Create: `service/web/static/js/components/NotesEditor.jsx`
- Modify: `ActiveSessionView.jsx`

**Steps:**
1. Single textarea for free-text notes; debounced auto-save (500ms) dispatches `session_notes_update_request`.
2. UI reflects `session_notes_update` broadcast from other clients.
3. Same editor used on History detail view (Task 14).

**Acceptance:** Edit from web; open simulator iOS mock (or just raw WebSocket); confirm both sync.

---

### Task 11: End Session modal

**Files:**
- Create: `service/web/static/js/components/EndSessionModal.jsx`

**Steps:**
1. Primary CTA: Save. Secondary: Discard. Tertiary: Cancel.
2. Clicking Save sends `session_end_request`; on `session_end` broadcast, Cook tab transitions back to Idle.
3. Clicking Discard opens a confirm-secondary ("This will permanently delete all data from this cook"); on confirm sends `session_discard_request`; on `session_discarded` broadcast, Cook tab transitions back to Idle.
4. Cancel closes the modal.

**Acceptance:** Full save flow; full discard flow with secondary confirm; both leave the UI in a clean Idle state.

---

### Task 12: History tab refinement

**Files:**
- Modify: existing history list + detail components

**Steps:**
1. Ensure discarded sessions never appear in the list.
2. History detail: enable `NotesEditor` for editing saved-session notes.
3. Show timer end-states alongside probe summary.
4. Show `target_duration_secs` and total elapsed.

**Acceptance:** Discard a session; confirm it never shows in history. Edit notes on a saved session; refresh; edits persist.

---

### Task 13: Settings tab

**Files:**
- Modify: existing settings component

**Steps:**
1. Bring the Connected Devices list into Settings (name, address, connection state).
2. Keep existing controls: log levels, simulation start/stop, speed/probes.
3. Clear separation between sections.

**Acceptance:** Devices list + simulation controls + log-level controls are all accessible from Settings.

---

### Task 14: Visual pass

**Files:**
- Modify: stylesheet

**Steps:**
1. Apply a warm, subtle palette consistent with the iOS brand (`#935240`). This is the web — less theming needed, but the Cook button, session header, and active-state highlights should pick up the brand colour.
2. Ensure readable contrast (WCAG AA minimum).
3. Mobile-Safari friendly (it's listed as a primary target per CLAUDE.md).

**Acceptance:** Visual check in desktop + mobile Safari.

---

### Task 15: Final verification

**Steps:**
1. Start from a clean server. Verify:
   - Fresh page shows Cook tab with no devices → empty state
   - Connect a simulator device → Idle view populates with sparklines
   - Start a session with 2 probes + target duration → Active view renders, chart + timer + progress bar work
   - Add a 3rd probe mid-session → appears on chart
   - Start a per-probe countdown → observe `probe_timer_update` broadcast, UI reflects
   - Edit notes → auto-saves
   - End with Save → History shows the session with notes and timers
   - Start another, End with Discard → History does NOT show it
2. Commit any final polish.

---

## Out of Scope (web UI)

- Timestamped-notes UI (single primary note only).
- Live Activity (iOS-only concept).
- Offline mode.
