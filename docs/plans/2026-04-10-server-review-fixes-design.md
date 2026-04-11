# Server Code Review Fixes — Design

**Date:** 2026-04-10
**Scope:** Address all findings from the server code review — 5 critical data-loss risks, 5 warnings (bugs/inconsistencies), and 7 suggestions (simplification/cleanup).

## Approach

Bottom-up by layer: data layer first (schema, migrations, store), then API/WebSocket, then BLE/infrastructure, then tests. Each layer is corrected before the layer above depends on it.

## Phase 1: Data Layer

### 1.1 Remove `_drop_legacy_schema` and dead code in `schema.py`
- Delete `_drop_legacy_schema()` and its call in `init_db()`
- Remove `import sqlite3` (unused)
- Remove `SCHEMA_VERSION = 1` (misleading; canonical version is `max(MIGRATIONS.keys())`)

### 1.2 Make migrations atomic in `migrations.py`
- Wrap each version's statements + version-record in explicit `BEGIN` / `COMMIT`
- `ROLLBACK` and re-raise on failure

### 1.3 `INSERT OR REPLACE` to `INSERT OR IGNORE` in `store.py`
- Both statements in `record_reading()` — preserves existing data on seq collision

### 1.4 Transaction safety for downsampler
- `downsampler.py`: wrap the bucket loop in explicit `BEGIN` / `ROLLBACK` on failure
- `store.py`: move `downsample_session` import to top-level

### 1.5 Align `TargetConfig` defaults
- `session.py`: change defaults to `pre_alert_offset=5.0`, `reminder_interval_secs=0`
- `from_dict()` fallback values updated to match
- Three sources of truth (model, schema, read-time fallback) now agree

### 1.6 Merge `get_session_name` + `get_session_notes`
- Replace with `get_session_metadata()` returning `{"name": ..., "notes": ...}` in one query
- Update callers in `routes.py`

### 1.7 Increment `_seq` after successful write
- `device_worker.py`: move `self._seq += 1` to after `record_reading()` succeeds

## Phase 2: API and WebSocket

### 2.1 Fix broken export handler in `routes.py`
- Rewrite CSV export to use flat row shape: `r["probe_index"]`, `r["temperature"]` directly
- Rewrite JSON label enrichment to iterate flat rows
- Sanitise `Content-Disposition` filename with `urllib.parse.quote`

### 2.2 Simplify `_sender` in `websocket.py`
- Replace ~55-line `asyncio.wait` + cancel + re-enqueue with priority-drain pattern
- Check event queue first, fall back to reading queue with short timeout
- Eliminates both silent message-drop paths

### 2.3 Add `offset` to WebSocket `_handle_sessions`
- Read `offset` from payload, pass through to `list_sessions()`

### 2.4 Log evicted critical events
- Add WARNING-level log when the event queue overflow evicts a message, naming the evicted type

### 2.5 Scope `get_targets` by address
- Add `address` parameter to `get_targets()` in `store.py`
- Update callers to pass device address
- Include `address` in `TargetConfig.to_dict()` where needed

### 2.6 Scope evaluator target update by address
- `websocket.py` `_handle_target_update`: pass `target_address` to `evaluator.set_targets()`

## Phase 3: BLE and Infrastructure

### 3.1 Remove dead `services is None` guard
- `device_worker.py`: remove `get_services()` fallback and `FutureWarning` suppression
- Remove `import warnings`

### 3.2 Make `ws_seq` monotonic across respawns
- Use a class-level or manager-level monotonic counter rather than reseeding from `_seq`

### 3.3 Log `device_left_session` failures at WARNING
- `device_worker.py`: change `LOG.debug` to `LOG.warning` for `device_left_session` failure in the except block

## Phase 4: Cleanup

### 4.1 Replace `asyncio.ensure_future` with `asyncio.create_task`
- `websocket.py` `broadcast()` method

### 4.2 Remove duplicate session fields from reading payload
- `reading.py`: remove `session_id` and `session_start_ts` from inside `data` dict (keep only top-level camelCase keys)

## Phase 5: Tests

### 5.1 Add export handler tests
- Test CSV export produces correct rows from flat reading data
- Test JSON export includes labels
- Test Content-Disposition header sanitisation

### 5.2 Add downsampler tests
- Bucketing correctness
- Averaging with `None` temperatures
- Empty range early-return
- Orphaned `device_readings` cleanup
- Transaction rollback on failure

### 5.3 Add `recover_orphaned_sessions` test
- Crashed session recovery produces correct end state

### 5.4 Add `get_history_items` time-range filter tests
- `since_ts`, `until_ts`, `limit` parameters

### 5.5 Add migration atomicity test
- Verify partial migration failure rolls back cleanly
