# Session-First Redesign — Server Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:executing-plans to implement this plan task-by-task.

**Goal:** Extend the iGrill Remote server to support the session-first redesign (discard, per-probe timers, dedicated notes, target duration) while remaining backwards-compatible with existing clients.

**Architecture:** Additive schema migrations + new WebSocket message types + new broadcast events. Existing session start/end flow is preserved; `session_end` now represents "save." Discard is a new explicit action. Per-probe timers and notes are persisted to dedicated tables and broadcast authoritatively. Rolling-preview data remains client-side only.

**Tech Stack:** Python 3.11+, aiohttp, aiosqlite, bleak, APNS.

**Design Reference:** `../../../docs/plans/2026-04-12-session-first-redesign-design.md` (sections 3, 5, 6, 10).

---

### Task 0: Prep

**Steps:**
1. Confirm existing test suite passes: `pytest`
2. Create working branch: `git checkout -b session-first-redesign`
3. Skim current schema in `service/db/schema.py` and migrations in `service/db/migrations.py` to understand the migration pattern in use.

**Acceptance:** Clean green test run on main; branch created.

---

### Task 1: Migration — `session_timers` table

**Files:**
- Modify: `service/db/schema.py` (add `CREATE TABLE session_timers` DDL)
- Modify: `service/db/migrations.py` (add new migration version)
- Test: `tests/db/test_migrations.py` (extend)

**Step 1: Write the failing test**

Add a test asserting the new migration creates the `session_timers` table with the columns from the design (session_id, address, probe_index, mode, duration_secs, started_at, paused_at, accumulated_secs, completed_at) and the composite primary key.

**Step 2: Run test to verify it fails**

`pytest tests/db/test_migrations.py -v` — expect failure because table does not exist.

**Step 3: Implement the migration**

Add DDL to `schema.py`. Add a migration step that runs `CREATE TABLE IF NOT EXISTS session_timers (...)` with `ON DELETE CASCADE` from `sessions(id)`. Bump schema version.

**Step 4: Verify tests pass**

**Step 5: Commit**

```
git add service/db/schema.py service/db/migrations.py tests/db/test_migrations.py
git commit -m "feat(db): add session_timers table"
```

---

### Task 2: Migration — `session_notes` table + backfill

**Files:**
- Modify: `service/db/schema.py`, `service/db/migrations.py`
- Test: `tests/db/test_migrations.py`

**Steps:**
1. Write failing test: new migration creates `session_notes` with the columns from the design; after running the migration against a DB containing a session with a non-empty `sessions.notes` value, there is exactly one row in `session_notes` with that body, `created_at = sessions.started_at`.
2. Implement: `CREATE TABLE session_notes`, `CREATE INDEX idx_session_notes_session`, and a backfill step: `INSERT INTO session_notes(session_id, created_at, updated_at, body) SELECT id, started_at, started_at, notes FROM sessions WHERE notes IS NOT NULL AND notes != ''`.
3. Keep the `sessions.notes` column (readable, dual-write in later tasks) until a follow-up migration.
4. Verify tests pass; commit.

---

### Task 3: Migration — `sessions.target_duration_secs`

**Files:**
- Modify: `service/db/schema.py`, `service/db/migrations.py`
- Test: `tests/db/test_migrations.py`

**Steps:**
1. Test: column exists and is nullable INTEGER after migration.
2. Implement with `ALTER TABLE sessions ADD COLUMN target_duration_secs INTEGER`.
3. Commit.

---

### Task 4: HistoryStore — timer CRUD

**Files:**
- Modify: `service/history/store.py`
- Test: `tests/history/test_store.py` (new or extend)

**Steps:**
1. Write failing tests for:
   - `upsert_timer(session_id, address, probe_index, mode, duration_secs)` creates a paused-initial row.
   - `start_timer(...)` sets `started_at`, clears `paused_at`.
   - `pause_timer(...)` sets `paused_at`, adds elapsed to `accumulated_secs`, clears `started_at`.
   - `resume_timer(...)` sets fresh `started_at`, clears `paused_at`.
   - `reset_timer(...)` clears `started_at`, `paused_at`, `accumulated_secs`, `completed_at`.
   - `complete_timer(...)` sets `completed_at`.
   - `get_timers(session_id)` returns all rows for a session.
2. Implement methods using `aiosqlite`, inside the existing async-lock pattern in `HistoryStore`.
3. All methods are no-ops (return False) when session is not active or doesn't exist.
4. Commit.

---

### Task 5: HistoryStore — notes CRUD

**Files:**
- Modify: `service/history/store.py`
- Test: `tests/history/test_store.py`

**Steps:**
1. Tests for:
   - `get_primary_note(session_id)` returns the earliest-created row.
   - `upsert_primary_note(session_id, body)` creates if none, else updates `body` + `updated_at`.
   - `get_notes(session_id)` returns all rows ordered by `created_at`.
2. Implement. Writes should also dual-write body into `sessions.notes` column for one release cycle for old-client compatibility.
3. Commit.

---

### Task 6: HistoryStore — `discard_session`

**Files:**
- Modify: `service/history/store.py`
- Test: `tests/history/test_store.py`

**Steps:**
1. Test: after `discard_session(session_id)`, the row is gone from `sessions` and all child rows (probe_readings, device_readings, session_targets, session_devices, session_timers, session_notes) are also gone due to cascade.
2. Test: discarding the *current* active session also clears `_active_session_id`/equivalent in-memory state.
3. Implement: set `discarded = 1`, commit, then `DELETE FROM sessions WHERE id = ?`. Foreign keys must be `ON` for cascade to fire — verify via `PRAGMA foreign_keys`.
4. Commit.

---

### Task 7: Alert evaluator — clear state on discard

**Files:**
- Modify: `service/alerts/evaluator.py` (if needed; likely already scoped correctly)
- Test: `tests/alerts/test_evaluator.py`

**Steps:**
1. Test: discarding a session clears its per-probe state from `AlertEvaluator` so a subsequent session reusing the same probe indices starts with a clean state machine.
2. Implement: expose `clear_session(session_id)` if not already present; call it from the discard path wired in Task 11.
3. Commit.

---

### Task 8: Envelope — new message types registered

**Files:**
- Modify: `service/api/envelope.py` (type constants), any type/enum used by router
- Test: `tests/api/test_envelope.py`

**Steps:**
1. Add type constants: `SESSION_DISCARD_REQUEST`, `PROBE_TIMER_REQUEST`, `SESSION_NOTES_UPDATE_REQUEST`, `SESSION_DISCARDED`, `PROBE_TIMER_UPDATE`, `SESSION_NOTES_UPDATE`.
2. Test envelope encoding/decoding round-trips for each new type.
3. Commit.

---

### Task 9: WebSocket handler — `session_discard_request`

**Files:**
- Modify: `service/api/websocket.py` (add handler + router entry)
- Test: `tests/api/test_websocket.py`

**Steps:**
1. Write failing integration test: authenticated client sends `session_discard_request` during an active session → server responds with `session_discard_ack` carrying the deleted session_id, broadcasts `session_discarded` to all clients, and a follow-up `status_request` shows no active session.
2. Test the auth guard: unauthenticated client is rejected with `error`.
3. Test the "no active session" path: request with no session returns error.
4. Implement handler: validate auth → `history_store.discard_session(...)` → `alert_evaluator.clear_session(...)` → `simulator.stop()` if running → broadcast `session_discarded` → ack requester.
5. Commit.

---

### Task 10: WebSocket handler — `probe_timer_request`

**Files:**
- Modify: `service/api/websocket.py`
- Test: `tests/api/test_websocket.py`

**Steps:**
1. Failing tests, one per action (`start`, `pause`, `resume`, `reset`):
   - Only accepted during active session.
   - Requires auth.
   - After handling, server broadcasts `probe_timer_update` with the authoritative row to all clients including the requester.
   - `start` action with `mode="count_down"` requires `duration_secs` > 0 (error otherwise).
2. Implement: dispatch to the matching `HistoryStore` method, then load the fresh row via `get_timers` and broadcast.
3. Commit.

---

### Task 11: WebSocket handler — `session_notes_update_request`

**Files:**
- Modify: `service/api/websocket.py`
- Test: `tests/api/test_websocket.py`

**Steps:**
1. Failing tests:
   - Authenticated client sends body → server broadcasts `session_notes_update` with the new body and `note_id` of the primary note; DB row is updated.
   - Notes can be edited after session has ended (saved sessions are still notes-editable).
2. Implement: accept `note_id?` (unused for MVP, present for forward-compat) + `body`; call `upsert_primary_note`; broadcast.
3. Commit.

---

### Task 12: Countdown auto-completion broadcast

**Files:**
- Modify: `service/history/store.py` and/or `service/api/websocket.py` (wherever the per-second tick loop lives)
- Test: `tests/history/test_store.py` or `tests/api/test_websocket.py`

**Steps:**
1. Test: when a count-down timer's `started_at + accumulated_secs + (now - started_at) >= duration_secs`, the server marks it completed and broadcasts `probe_timer_update` with `completed_at` set exactly once.
2. Implementation options:
   - Simple: run a coarse (5–10s) background task in the server that scans active timers and marks completions.
   - Keep it coarse; this is not real-time-critical. Completion fires within ±10s of true zero.
3. Commit.

---

### Task 13: Extend `session_start_request` with `target_duration_secs`

**Files:**
- Modify: `service/api/websocket.py` (accept new optional field)
- Modify: `service/history/store.py` (`start_session` accepts the field)
- Test: `tests/api/test_websocket.py`

**Steps:**
1. Test: starting a session with `target_duration_secs` persists it; absent field persists NULL.
2. Test: the value is returned in `status_request` payload and in `sessions_request` listing.
3. Implement. Commit.

---

### Task 14: Session export — include timers + notes + target duration

**Files:**
- Modify: `service/api/routes.py` (`GET /api/sessions/{id}` and `/export`)
- Test: `tests/api/test_routes.py`

**Steps:**
1. Tests: JSON export includes `target_duration_secs`, full `timers` array, and `notes` array. CSV export gains a `timer_states.csv` and `notes.csv` supplementary file, or appends sections to the existing CSV — pick the simpler option (separate files) and test it.
2. Implement. Commit.

---

### Task 15: Rate limiter — include new session-control messages

**Files:**
- Modify: `service/api/websocket.py` (rate limiter message list)
- Test: `tests/api/test_websocket.py`

**Steps:**
1. Ensure `session_discard_request`, `probe_timer_request`, `session_notes_update_request` are counted toward the per-IP 10/60s rate limit.
2. Test: 11 rapid discards from one IP — the 11th is rejected.
3. Commit.

---

### Task 16: Orphaned session recovery — discarded state

**Files:**
- Modify: `service/history/store.py::recover_orphaned_sessions()`
- Test: `tests/history/test_store.py`

**Steps:**
1. Test: a session left in `discarded = 1` at startup (server crashed mid-discard) is fully hard-deleted on boot.
2. Implement. Commit.

---

### Task 17: End-to-end integration test — full session lifecycle

**Files:**
- Create: `tests/integration/test_session_lifecycle.py`

**Steps:**
1. Start session with targets + target_duration → send readings → update timer → update notes → end session → confirm data is in history + notes editable.
2. Start session → configure probes → **discard** → confirm DB is empty for that session_id, broadcast was received.
3. Start session → mid-cook add probe via `target_update_request` → confirm new probe begins recording.
4. Commit.

---

### Task 18: Documentation updates

**Files:**
- Modify: `README.md` (feature list + docs/API_CONTRACT.md pointer)
- Modify: `docs/API_CONTRACT.md` (or equivalent) — add new message types, link to design doc

**Steps:**
1. Document new WebSocket message types with examples.
2. Document new DB schema.
3. Add a note that the server supports both legacy `sessions.notes` and new `session_notes` table during transition.
4. Commit.

---

### Task 19: Final verification

**Steps:**
1. `pytest` — all green.
2. Run server manually with simulator: start → discard → start → add probe mid-cook → end with save → verify saved session in history with edited notes and timer states.
3. Commit any last doc tweaks.

---

## Out of Scope (server)

- Dropping the legacy `sessions.notes` column (follow-up migration after one release cycle).
- Timestamped multi-row notes UI (scaffolding only; single primary note suffices).
- Live Activity visual/content changes.
