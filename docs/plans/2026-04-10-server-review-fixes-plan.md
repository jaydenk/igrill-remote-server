# Server Code Review Fixes — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:executing-plans to implement this plan task-by-task.

**Goal:** Fix all critical data-loss risks, bugs, inconsistencies, and simplification opportunities identified in the server code review.

**Architecture:** Bottom-up by layer — data layer first (schema, migrations, store), then API/WebSocket, then BLE, then tests. Each layer is corrected before the layer above depends on it.

**Tech Stack:** Python 3.11, aiosqlite, aiohttp, bleak, pytest / pytest-asyncio

---

### Task 1: Remove legacy schema code and dead imports from `schema.py`

**Files:**
- Modify: `iGrillRemoteServer/service/db/schema.py`
- Modify: `iGrillRemoteServer/tests/test_schema.py`

**Step 1: Update `schema.py`**

Remove the `import sqlite3` on line 4 (unused — all DB access goes through `aiosqlite`).

Remove the `SCHEMA_VERSION = 1` constant on line 10 (misleading — migrations advance the version independently, the canonical version is `max(MIGRATIONS.keys())` in `migrations.py`).

Delete the entire `_drop_legacy_schema` function (lines 85–123). No deployments are on the old INTEGER schema.

Update `init_db` to remove the call to `_drop_legacy_schema` on line 128. The function should become:

```python
async def init_db(conn: aiosqlite.Connection) -> None:
    """Create all tables if they don't exist and record schema version."""
    await conn.executescript(_SCHEMA_SQL)

    cursor = await conn.execute(
        "SELECT version FROM schema_version WHERE version = 1",
    )
    existing = await cursor.fetchone()
    if existing is None:
        await conn.execute(
            "INSERT INTO schema_version (version) VALUES (1)",
        )
        await conn.commit()
```

**Step 2: Update tests that reference removed code**

In `test_schema.py`:
- Remove the `SCHEMA_VERSION` import from line 8
- Update `test_schema_version_recorded` (line 30) to assert `row[0] == 1` instead of `row[0] == SCHEMA_VERSION`
- Remove `test_legacy_schema_upgrade` (lines 114–151) entirely — the function it tests no longer exists

**Step 3: Run tests**

Run: `cd iGrillRemoteServer && python -m pytest tests/test_schema.py -v`
Expected: All remaining schema tests pass.

**Step 4: Commit**

```bash
git add iGrillRemoteServer/service/db/schema.py iGrillRemoteServer/tests/test_schema.py
git commit -m "fix: remove legacy schema drop and dead imports from schema.py"
```

---

### Task 2: Make migrations atomic

**Files:**
- Modify: `iGrillRemoteServer/service/db/migrations.py`

**Step 1: Write a failing test**

Add to `tests/test_schema.py`:

```python
@pytest.mark.asyncio
async def test_partial_migration_rolls_back(tmp_db):
    """A migration that fails partway through should not leave the DB in a
    half-applied state."""
    from service.db.migrations import MIGRATIONS, run_migrations

    # Inject a migration v99 that will fail on the second statement
    original = dict(MIGRATIONS)
    MIGRATIONS[99] = [
        "ALTER TABLE sessions ADD COLUMN _test_col_1 TEXT",
        "ALTER TABLE nonexistent_table ADD COLUMN oops TEXT",  # will fail
    ]
    try:
        async with aiosqlite.connect(tmp_db) as conn:
            conn.row_factory = aiosqlite.Row
            await init_db(conn)
            from service.db.migrations import run_migrations as rm
            await rm(conn)  # apply v2 (real)

            with pytest.raises(Exception):
                await rm(conn)  # attempt v99, should fail + rollback

            # _test_col_1 should NOT exist — the migration was rolled back
            cursor = await conn.execute("PRAGMA table_info(sessions)")
            cols = {row[1] for row in await cursor.fetchall()}
            assert "_test_col_1" not in cols

            # schema_version should still be at 2
            cursor = await conn.execute("SELECT MAX(version) FROM schema_version")
            row = await cursor.fetchone()
            assert row[0] == 2
    finally:
        MIGRATIONS.clear()
        MIGRATIONS.update(original)
```

**Step 2: Run test to verify it fails**

Run: `cd iGrillRemoteServer && python -m pytest tests/test_schema.py::test_partial_migration_rolls_back -v`
Expected: FAIL — the first ALTER TABLE persists despite the second failing.

**Step 3: Make migrations atomic**

In `migrations.py`, update `run_migrations` to wrap each version in an explicit transaction:

```python
async def run_migrations(conn: aiosqlite.Connection) -> None:
    """Apply any pending migrations in order.

    Each migration is a list of SQL statements executed inside a single
    transaction.  If any statement fails the entire migration is rolled
    back and the error is re-raised.
    """
    current = await get_current_version(conn)

    for version in sorted(MIGRATIONS):
        if version <= current:
            continue
        LOG.info("Applying schema migration v%d", version)
        try:
            await conn.execute("BEGIN")
            for statement in MIGRATIONS[version]:
                await conn.execute(statement)
            await conn.execute(
                "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
                (version,),
            )
            await conn.commit()
        except Exception:
            await conn.execute("ROLLBACK")
            LOG.error("Schema migration v%d FAILED — rolled back", version)
            raise
        LOG.info("Schema migration v%d applied successfully", version)
```

**Step 4: Run tests**

Run: `cd iGrillRemoteServer && python -m pytest tests/test_schema.py -v`
Expected: All pass, including the new rollback test.

**Step 5: Commit**

```bash
git add iGrillRemoteServer/service/db/migrations.py iGrillRemoteServer/tests/test_schema.py
git commit -m "fix: wrap schema migrations in explicit transactions for atomicity"
```

---

### Task 3: Change `INSERT OR REPLACE` to `INSERT OR IGNORE` in `record_reading`

**Files:**
- Modify: `iGrillRemoteServer/service/history/store.py:471-491`

**Step 1: Write a failing test**

Add to `tests/test_history_store.py`:

```python
@pytest.mark.asyncio
async def test_duplicate_seq_preserves_original(store, sample_address):
    """INSERT OR IGNORE should keep the first reading, not overwrite."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]

    await store.record_reading(
        session_id=sid, address=sample_address, seq=1,
        probes=[{"index": 1, "temperature": 72.5}],
        battery=85, propane=None, heating=None,
    )
    # Attempt duplicate with different temperature
    await store.record_reading(
        session_id=sid, address=sample_address, seq=1,
        probes=[{"index": 1, "temperature": 99.9}],
        battery=50, propane=None, heating=None,
    )

    items = await store.get_session_readings(sid)
    assert len(items) == 1
    assert items[0]["temperature"] == 72.5  # original preserved
    assert items[0]["battery"] == 85        # original preserved
```

**Step 2: Run test to verify it fails**

Run: `cd iGrillRemoteServer && python -m pytest tests/test_history_store.py::test_duplicate_seq_preserves_original -v`
Expected: FAIL — temperature is 99.9 (overwritten by `INSERT OR REPLACE`).

**Step 3: Change to `INSERT OR IGNORE`**

In `store.py`, `record_reading` method (~line 471 and ~line 479):

Replace both occurrences of `INSERT OR REPLACE` with `INSERT OR IGNORE`.

**Step 4: Run tests**

Run: `cd iGrillRemoteServer && python -m pytest tests/test_history_store.py -v`
Expected: All pass.

**Step 5: Commit**

```bash
git add iGrillRemoteServer/service/history/store.py iGrillRemoteServer/tests/test_history_store.py
git commit -m "fix: use INSERT OR IGNORE to prevent overwriting readings on seq collision"
```

---

### Task 4: Add transaction safety to downsampler

**Files:**
- Modify: `iGrillRemoteServer/service/history/downsampler.py`
- Modify: `iGrillRemoteServer/service/history/store.py:197` (move import to top-level)

**Step 1: Write a failing test**

Add a new file `tests/test_downsampler.py`:

```python
"""Tests for the session reading downsampler."""

import pytest
from datetime import datetime, timezone, timedelta

from service.history.store import HistoryStore


@pytest.mark.asyncio
async def test_downsample_averages_temperatures(store, sample_address):
    """Readings in the same bucket should be averaged."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]

    # Insert 3 readings 10 seconds apart (all within one 60s bucket)
    old_time = datetime.now(timezone.utc) - timedelta(days=2)
    for i in range(3):
        ts = (old_time + timedelta(seconds=i * 10)).isoformat()
        await store.record_reading(
            session_id=sid, address=sample_address, seq=i + 1,
            probes=[{"index": 1, "temperature": 70.0 + i * 10}],  # 70, 80, 90
            battery=85, propane=None, heating=None, recorded_at=ts,
        )

    # Verify 3 readings exist before downsampling
    readings_before = await store.get_session_readings(sid)
    assert len(readings_before) == 3

    from service.history.downsampler import downsample_session
    await downsample_session(store, sid)

    readings_after = await store.get_session_readings(sid)
    assert len(readings_after) == 1
    assert readings_after[0]["temperature"] == pytest.approx(80.0)  # avg of 70,80,90


@pytest.mark.asyncio
async def test_downsample_singleton_buckets_untouched(store, sample_address):
    """A bucket with only one reading should not be modified."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]

    old_time = datetime.now(timezone.utc) - timedelta(days=2)
    ts = old_time.isoformat()
    await store.record_reading(
        session_id=sid, address=sample_address, seq=1,
        probes=[{"index": 1, "temperature": 72.5}],
        battery=85, propane=None, heating=None, recorded_at=ts,
    )

    from service.history.downsampler import downsample_session
    await downsample_session(store, sid)

    readings = await store.get_session_readings(sid)
    assert len(readings) == 1
    assert readings[0]["temperature"] == 72.5


@pytest.mark.asyncio
async def test_downsample_empty_range(store, sample_address):
    """Downsampling with no readings in range should be a no-op."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]

    # Insert a recent reading (within 24h, so not eligible for downsampling)
    await store.record_reading(
        session_id=sid, address=sample_address, seq=1,
        probes=[{"index": 1, "temperature": 72.5}],
        battery=85, propane=None, heating=None,
    )

    from service.history.downsampler import downsample_session
    await downsample_session(store, sid)

    readings = await store.get_session_readings(sid)
    assert len(readings) == 1  # untouched


@pytest.mark.asyncio
async def test_downsample_none_temperatures(store, sample_address):
    """Buckets where all temperatures are None should produce None average."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]

    old_time = datetime.now(timezone.utc) - timedelta(days=2)
    for i in range(3):
        ts = (old_time + timedelta(seconds=i * 10)).isoformat()
        await store.record_reading(
            session_id=sid, address=sample_address, seq=i + 1,
            probes=[{"index": 1, "temperature": None}],
            battery=85, propane=None, heating=None, recorded_at=ts,
        )

    from service.history.downsampler import downsample_session
    await downsample_session(store, sid)

    readings = await store.get_session_readings(sid)
    assert len(readings) == 1
    assert readings[0]["temperature"] is None
```

**Step 2: Run tests to verify they pass with current code**

Run: `cd iGrillRemoteServer && python -m pytest tests/test_downsampler.py -v`
Expected: All pass (the downsampler works; we're adding coverage before modifying it).

**Step 3: Add transaction safety to `downsample_range`**

In `downsampler.py`, wrap the bucket processing loop (lines 121–171) in an explicit transaction:

Replace lines 114 onward with:

```python
    deleted_count = 0
    inserted_count = 0

    touched_addresses: set[tuple[str, str]] = set()

    try:
        await conn.execute("BEGIN")

        for key, readings in buckets.items():
            if len(readings) <= 1:
                continue

            address, probe_index, bucket_key = key

            temps = [r["temperature"] for r in readings if r["temperature"] is not None]
            avg_temp = sum(temps) / len(temps) if temps else None

            timestamps = [datetime.fromisoformat(r["recorded_at"]) for r in readings]
            mid_ts = min(timestamps) + (max(timestamps) - min(timestamps)) / 2
            mid_ts_iso = mid_ts.isoformat()

            min_seq = min(r["seq"] for r in readings)

            sid = readings[0]["session_id"]
            touched_addresses.add((sid, address))

            ids = [r["id"] for r in readings]
            placeholders = ",".join("?" for _ in ids)
            await conn.execute(
                f"DELETE FROM probe_readings WHERE id IN ({placeholders})",
                ids,
            )
            deleted_count += len(ids)

            await conn.execute(
                "INSERT INTO probe_readings "
                "(session_id, address, recorded_at, seq, probe_index, temperature) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (sid, address, mid_ts_iso, min_seq, probe_index, avg_temp),
            )
            inserted_count += 1

        for sid_val, addr_val in touched_addresses:
            await conn.execute(
                "DELETE FROM device_readings "
                "WHERE session_id = ? AND address = ? "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM probe_readings "
                "  WHERE probe_readings.session_id = device_readings.session_id "
                "  AND probe_readings.address = device_readings.address "
                "  AND probe_readings.seq = device_readings.seq"
                ")",
                (sid_val, addr_val),
            )

        await conn.commit()
    except Exception:
        await conn.execute("ROLLBACK")
        LOG.error(
            "Downsampling FAILED for session %s [%s] — rolled back",
            session_id, label,
        )
        raise
```

**Step 4: Move `downsample_session` import to top-level in `store.py`**

In `store.py`, add at the top of the file (with other imports):

```python
from service.history.downsampler import downsample_session
```

And change line 197 from:
```python
from service.history.downsampler import downsample_session
await downsample_session(self, session_id)
```
to:
```python
await downsample_session(self, session_id)
```

Similarly, move the dynamic import in `execute_downsampling` (~line 818) to use the top-level import:

```python
from service.history.downsampler import downsample_range
```

And change the body of `execute_downsampling` accordingly.

**Step 5: Run tests**

Run: `cd iGrillRemoteServer && python -m pytest tests/test_downsampler.py tests/test_history_store.py -v`
Expected: All pass.

**Step 6: Commit**

```bash
git add iGrillRemoteServer/service/history/downsampler.py iGrillRemoteServer/service/history/store.py iGrillRemoteServer/tests/test_downsampler.py
git commit -m "fix: wrap downsampler in explicit transaction for rollback safety"
```

---

### Task 5: Align `TargetConfig` defaults across model, schema, and read-time fallback

**Files:**
- Modify: `iGrillRemoteServer/service/models/session.py:28-29`

**Step 1: Write a failing test**

Add to `tests/test_models.py`:

```python
def test_target_config_defaults_match_schema():
    """TargetConfig defaults must match the DB schema defaults."""
    from service.models.session import TargetConfig
    t = TargetConfig(probe_index=1, mode="fixed")
    # DB schema defaults: pre_alert_offset=5.0, reminder_interval_secs=0
    assert t.pre_alert_offset == 5.0
    assert t.reminder_interval_secs == 0
```

**Step 2: Run test to verify it fails**

Run: `cd iGrillRemoteServer && python -m pytest tests/test_models.py::test_target_config_defaults_match_schema -v`
Expected: FAIL — `pre_alert_offset` is 10.0 and `reminder_interval_secs` is 300.

**Step 3: Align defaults**

In `session.py`, change lines 28-29:

```python
    pre_alert_offset: float = 5.0
    reminder_interval_secs: int = 0
```

Also update `from_dict()` fallback values on lines 64 and 68:

```python
        pre_alert_offset = float(data.get("pre_alert_offset", 5.0))
        ...
        reminder_interval_secs = int(data.get("reminder_interval_secs", 0))
```

**Step 4: Run tests**

Run: `cd iGrillRemoteServer && python -m pytest tests/test_models.py -v`
Expected: All pass.

**Step 5: Commit**

```bash
git add iGrillRemoteServer/service/models/session.py iGrillRemoteServer/tests/test_models.py
git commit -m "fix: align TargetConfig defaults with DB schema (5.0 offset, 0 reminder)"
```

---

### Task 6: Merge `get_session_name` and `get_session_notes` into `get_session_metadata`

**Files:**
- Modify: `iGrillRemoteServer/service/history/store.py` (lines 294–310)
- Modify: `iGrillRemoteServer/service/api/routes.py` (lines 75–76, 96)
- Modify: `iGrillRemoteServer/service/api/websocket.py` (line 310)

**Step 1: Replace the two methods in `store.py`**

Delete `get_session_name` (lines 294–301) and `get_session_notes` (lines 303–310).

Add a single replacement:

```python
    async def get_session_metadata(self, session_id: str) -> Optional[dict]:
        """Return name and notes for a session, or None if not found."""
        async with self._lock:
            cursor = await self._conn.execute(
                "SELECT name, notes FROM sessions WHERE id = ?", (session_id,)
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return {"name": row["name"], "notes": row["notes"]}
```

**Step 2: Update callers**

In `routes.py`, `session_detail_handler` (lines 75–76), replace:
```python
    name = await history.get_session_name(session_id)
    notes = await history.get_session_notes(session_id)
```
with:
```python
    meta = await history.get_session_metadata(session_id)
    name = meta["name"] if meta else None
    notes = meta["notes"] if meta else None
```

In `routes.py`, `export_handler` (line 96), replace:
```python
    name = await history.get_session_name(session_id)
```
with:
```python
    meta = await history.get_session_metadata(session_id)
    name = meta["name"] if meta else None
```

In `websocket.py`, `_handle_status` (line 310), replace:
```python
    status_payload["currentSessionName"] = await ctx.history.get_session_name(current_sid)
```
with:
```python
    meta = await ctx.history.get_session_metadata(current_sid)
    status_payload["currentSessionName"] = meta["name"] if meta else None
```

**Step 3: Run tests**

Run: `cd iGrillRemoteServer && python -m pytest -v`
Expected: All pass.

**Step 4: Commit**

```bash
git add iGrillRemoteServer/service/history/store.py iGrillRemoteServer/service/api/routes.py iGrillRemoteServer/service/api/websocket.py
git commit -m "refactor: merge get_session_name/notes into single get_session_metadata query"
```

---

### Task 7: Increment `_seq` only after successful DB write

**Files:**
- Modify: `iGrillRemoteServer/service/ble/device_worker.py:352-363`

**Step 1: Reorder the seq increment**

In `device_worker.py` `_poll_loop`, change the session-recording block (around lines 352–363) from:

```python
            if session_id is not None and await self.history.is_device_in_session(self.address):
                self._seq += 1
                probes: List[Dict[str, Any]] = payload.get("probes", [])
                await self.history.record_reading(
                    session_id=session_id,
                    address=self.address,
                    seq=self._seq,
                    ...
                )
```

to:

```python
            if session_id is not None and await self.history.is_device_in_session(self.address):
                next_seq = self._seq + 1
                probes: List[Dict[str, Any]] = payload.get("probes", [])
                await self.history.record_reading(
                    session_id=session_id,
                    address=self.address,
                    seq=next_seq,
                    ...
                )
                self._seq = next_seq
```

**Step 2: Commit**

```bash
git add iGrillRemoteServer/service/ble/device_worker.py
git commit -m "fix: increment _seq only after successful record_reading to avoid gaps"
```

---

### Task 8: Fix broken export handler

**Files:**
- Modify: `iGrillRemoteServer/service/api/routes.py:105-138`

**Step 1: Write a failing test**

Add a new file `tests/test_routes.py`:

```python
"""Tests for HTTP route handlers."""

import pytest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, TestClient

from service.api.routes import setup_routes
from service.history.store import HistoryStore
from service.models.device import DeviceStore


@pytest.fixture
async def app(store):
    """Create an aiohttp app with routes and a real HistoryStore."""
    application = web.Application()
    application["history"] = store
    application["store"] = DeviceStore()
    application["config"] = type("Config", (), {"poll_interval": 15, "scan_interval": 60})()
    application["start_time"] = 0
    setup_routes(application)
    return application


@pytest.fixture
async def client(app, aiohttp_client):
    return await aiohttp_client(app)


@pytest.mark.asyncio
async def test_export_csv_produces_data(client, store, sample_address):
    """CSV export should produce rows from flat probe readings."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]
    await store.record_reading(
        session_id=sid, address=sample_address, seq=1,
        probes=[
            {"index": 1, "temperature": 72.5},
            {"index": 2, "temperature": 80.0},
        ],
        battery=85, propane=None, heating=None,
    )

    resp = await client.get(f"/api/sessions/{sid}/export?format=csv")
    assert resp.status == 200
    text = await resp.text()
    lines = text.strip().split("\n")
    assert len(lines) == 3  # header + 2 probe rows
    assert "72.5" in lines[1]
    assert "80.0" in lines[2]


@pytest.mark.asyncio
async def test_export_json_includes_labels(client, store, sample_address):
    """JSON export should enrich readings with target labels."""
    from service.models.session import TargetConfig
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]
    await store.record_reading(
        session_id=sid, address=sample_address, seq=1,
        probes=[{"index": 1, "temperature": 72.5}],
        battery=85, propane=None, heating=None,
    )
    await store.save_targets(sid, sample_address, [
        TargetConfig(probe_index=1, mode="fixed", target_value=74.0, label="Brisket"),
    ])

    resp = await client.get(f"/api/sessions/{sid}/export?format=json")
    assert resp.status == 200
    data = await resp.json()
    assert len(data["readings"]) == 1
    assert data["readings"][0].get("label") == "Brisket"
```

**Step 2: Run test to verify it fails**

Run: `cd iGrillRemoteServer && python -m pytest tests/test_routes.py -v`
Expected: FAIL — CSV produces only header row, JSON label is missing.

**Step 3: Fix the export handler**

In `routes.py`, replace the CSV export section (lines 105–127) with:

```python
    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["timestamp", "probe_index", "label", "temperature_c", "battery_pct", "propane_pct"])
        for r in readings:
            ts = r.get("recorded_at", "")
            battery = r.get("battery")
            propane = r.get("propane")
            idx = r.get("probe_index", 0)
            temp = r.get("temperature")
            label = label_by_probe.get(idx, "")
            writer.writerow([ts, idx, label, temp, battery, propane])

        safe_name = (name or session_id).replace('"', "'")
        return web.Response(
            text=buf.getvalue(),
            content_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{safe_name}.csv"',
            },
        )
```

Replace the JSON export section (lines 129–138) with:

```python
    # Default: enriched JSON
    for r in readings:
        idx = r.get("probe_index", 0)
        r["label"] = label_by_probe.get(idx)
    return web.json_response({
        "sessionId": session_id,
        "name": name,
        "readings": readings,
    })
```

**Step 4: Run tests**

Run: `cd iGrillRemoteServer && python -m pytest tests/test_routes.py -v`
Expected: All pass.

**Step 5: Commit**

```bash
git add iGrillRemoteServer/service/api/routes.py iGrillRemoteServer/tests/test_routes.py
git commit -m "fix: rewrite export handler to use flat row shape from get_session_readings"
```

---

### Task 9: Simplify WebSocket `_sender` with priority-drain pattern

**Files:**
- Modify: `iGrillRemoteServer/service/api/websocket.py:102-168`

**Step 1: Replace `_sender`**

Replace the entire `_sender` method (lines 102–168) with:

```python
    async def _sender(self) -> None:
        while True:
            # Always drain events first (priority); fall back to readings.
            try:
                message = self._event_queue.get_nowait()
            except asyncio.QueueEmpty:
                try:
                    message = self._reading_queue.get_nowait()
                except asyncio.QueueEmpty:
                    # Both empty — wait on event queue with a short timeout,
                    # then loop to check reading queue.
                    try:
                        message = await asyncio.wait_for(
                            self._event_queue.get(), timeout=0.1,
                        )
                    except asyncio.TimeoutError:
                        continue

            if self.ws.closed:
                break
            try:
                await self.ws.send_json(message)
            except (ConnectionError, OSError):
                break
```

**Step 2: Run tests**

Run: `cd iGrillRemoteServer && python -m pytest tests/ -v`
Expected: All pass — the integration tests use WebSocket and will verify the new sender works.

**Step 3: Commit**

```bash
git add iGrillRemoteServer/service/api/websocket.py
git commit -m "refactor: simplify WebSocket _sender with priority-drain pattern"
```

---

### Task 10: Add `offset` to WebSocket `_handle_sessions`

**Files:**
- Modify: `iGrillRemoteServer/service/api/websocket.py` (lines 316–333)

**Step 1: Add offset parsing**

In `_handle_sessions`, after the `limit` parsing block (line 330), add:

```python
    offset = ctx.payload.get("offset", 0)
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        await send_error(ctx.ws, "invalid_payload", "offset must be an integer.", ctx.request_id)
        return
    if offset < 0:
        offset = 0
```

Update the `list_sessions` call (line 332) to pass offset:

```python
    sessions = await ctx.history.list_sessions(limit, offset=offset)
```

**Step 2: Commit**

```bash
git add iGrillRemoteServer/service/api/websocket.py
git commit -m "fix: pass offset parameter through WebSocket sessions handler for pagination"
```

---

### Task 11: Log evicted critical events at WARNING level

**Files:**
- Modify: `iGrillRemoteServer/service/api/websocket.py:170-180`

**Step 1: Add logging to `enqueue`**

In the `enqueue` method, when a critical event is evicted (lines 174–179), add a log statement:

```python
    def enqueue(self, message: Dict[str, object], critical: bool = False) -> None:
        if critical:
            if self._event_queue.full():
                try:
                    evicted = self._event_queue.get_nowait()
                    LOG.warning(
                        "Event queue full — evicted message type=%s to enqueue type=%s",
                        evicted.get("type", "unknown"),
                        message.get("type", "unknown"),
                    )
                except asyncio.QueueEmpty:
                    pass
            self._event_queue.put_nowait(message)
        else:
            if self._reading_queue.full():
                try:
                    self._reading_queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            self._reading_queue.put_nowait(message)
```

Ensure `LOG = logging.getLogger("igrill.ws")` exists near the top of `websocket.py` (it likely already does).

**Step 2: Commit**

```bash
git add iGrillRemoteServer/service/api/websocket.py
git commit -m "fix: log evicted critical WebSocket events at WARNING level"
```

---

### Task 12: Remove duplicate session fields from reading payload

**Files:**
- Modify: `iGrillRemoteServer/service/models/reading.py:82-97`

**Step 1: Remove duplicates from `data` dict**

In `build_reading_payload`, remove `session_id` and `session_start_ts` from the `data` dict (lines 86-87). The top-level `sessionId` and `sessionStartTs` keys (lines 100-101) are the canonical source.

The `data` dict should become:

```python
    data = {
        "name": device_entry.get("name"),
        "model": device_entry.get("model"),
        "model_name": device_entry.get("model_name"),
        "last_update": device_entry.get("last_update"),
        "unit": device_entry.get("unit"),
        "battery_percent": device_entry.get("battery_percent"),
        "propane_percent": device_entry.get("propane_percent"),
        "probes": device_entry.get("probes", []),
        "connected_probes": device_entry.get("connected_probes", []),
        "probe_status": device_entry.get("probe_status"),
        "pulse": device_entry.get("pulse", {}),
        "error": device_entry.get("error"),
    }
```

> **Note:** Verify the iOS app does not read `data.session_id` or `data.session_start_ts`. Search the Swift codebase for these keys. If the app uses them, update the iOS model to read from the top-level `sessionId`/`sessionStartTs` instead.

**Step 2: Commit**

```bash
git add iGrillRemoteServer/service/models/reading.py
git commit -m "fix: remove duplicate session fields from reading payload data dict"
```

---

### Task 13: Remove dead BLE code and unused imports

**Files:**
- Modify: `iGrillRemoteServer/service/ble/device_worker.py`

**Step 1: Remove `import warnings` (line 12)**

Delete:
```python
import warnings
```

**Step 2: Remove `services is None` guard (lines 123–135)**

Replace lines 123–135:

```python
                    services = client.services
                    if services is None:
                        with warnings.catch_warnings():
                            warnings.simplefilter("ignore", category=FutureWarning)
                            services = await client.get_services()
                    if services is None:
                        LOG.warning("No services discovered for %s", self.address)
                        await self.store.upsert(self.address, connected=False, error="services_unavailable")
                        self._state_machine.transition(ConnectionState.DISCONNECTED)
                        self._state_machine.transition(ConnectionState.BACKOFF)
                        await self._publish_state_change_event()
                        await asyncio.sleep(self._state_machine.backoff_seconds)
                        continue
```

with:

```python
                    services = client.services
```

**Step 3: Commit**

```bash
git add iGrillRemoteServer/service/ble/device_worker.py
git commit -m "refactor: remove dead get_services() fallback and unused warnings import"
```

---

### Task 14: Make `ws_seq` monotonic across respawns

**Files:**
- Modify: `iGrillRemoteServer/service/ble/device_worker.py`

**Step 1: Replace `ws_seq` seeding**

In `_poll_loop` (around line 308), change:

```python
        ws_seq = self._seq
```

to use monotonic time to ensure uniqueness across respawns:

```python
        ws_seq = int(asyncio.get_event_loop().time() * 1000)
```

This produces a millisecond-resolution monotonic counter that never resets to 0 on respawn.

**Step 2: Commit**

```bash
git add iGrillRemoteServer/service/ble/device_worker.py
git commit -m "fix: seed ws_seq from monotonic clock to prevent duplicates across respawns"
```

---

### Task 15: Log `device_left_session` failure at WARNING level

**Files:**
- Modify: `iGrillRemoteServer/service/ble/device_worker.py:175-176`

**Step 1: Change log level**

In the error disconnect handler (around line 176), change:

```python
                        LOG.debug("Failed to mark device %s as left on error", self.address)
```

to:

```python
                        LOG.warning("Failed to mark device %s as left — session_devices.left_at may be stale", self.address)
```

**Step 2: Commit**

```bash
git add iGrillRemoteServer/service/ble/device_worker.py
git commit -m "fix: log device_left_session failure at WARNING for observability"
```

---

### Task 16: Replace `asyncio.ensure_future` with `asyncio.create_task`

**Files:**
- Modify: `iGrillRemoteServer/service/api/websocket.py` (line 218)

**Step 1: Replace deprecated call**

Change line 218 from:

```python
                asyncio.ensure_future(client.close())
```

to:

```python
                asyncio.create_task(client.close())
```

**Step 2: Commit**

```bash
git add iGrillRemoteServer/service/api/websocket.py
git commit -m "refactor: replace deprecated asyncio.ensure_future with create_task"
```

---

### Task 17: Add remaining test coverage

**Files:**
- Modify: `iGrillRemoteServer/tests/test_history_store.py`

**Step 1: Add `recover_orphaned_sessions` test**

```python
@pytest.mark.asyncio
async def test_recover_orphaned_sessions(store, sample_address):
    """Orphaned sessions (no ended_at) should be closed on recovery."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]
    # Simulate crash — session is active with no end

    await store.recover_orphaned_sessions()

    state = await store.get_session_state()
    assert state["current_session_id"] is None

    sessions = await store.list_sessions(limit=10)
    assert len(sessions) == 1
    assert sessions[0]["end_reason"] == "server_restart"
```

**Step 2: Add `get_history_items` time-range filter tests**

```python
@pytest.mark.asyncio
async def test_get_history_items_with_time_filter(store, sample_address):
    """get_history_items should filter by since_ts and until_ts."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]

    from datetime import datetime, timezone, timedelta
    base = datetime.now(timezone.utc) - timedelta(hours=2)

    for i in range(3):
        ts = (base + timedelta(minutes=i * 30)).isoformat()
        await store.record_reading(
            session_id=sid, address=sample_address, seq=i + 1,
            probes=[{"index": 1, "temperature": 70.0 + i}],
            battery=85, propane=None, heating=None, recorded_at=ts,
        )

    # Filter: only readings after the first
    since = (base + timedelta(minutes=15)).isoformat()
    items = await store.get_history_items(since_ts=since, session_id=sid)
    assert len(items) == 2  # second and third


@pytest.mark.asyncio
async def test_get_history_items_with_limit(store, sample_address):
    """get_history_items should respect the limit parameter."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]

    for i in range(5):
        await store.record_reading(
            session_id=sid, address=sample_address, seq=i + 1,
            probes=[{"index": 1, "temperature": 70.0 + i}],
            battery=85, propane=None, heating=None,
        )

    items = await store.get_history_items(session_id=sid, limit=3)
    assert len(items) == 3
```

**Step 3: Run all tests**

Run: `cd iGrillRemoteServer && python -m pytest -v`
Expected: All pass.

**Step 4: Commit**

```bash
git add iGrillRemoteServer/tests/test_history_store.py
git commit -m "test: add coverage for orphan recovery, time-range filters, and limit"
```

---

### Task 18: Update README

**Files:**
- Modify: `iGrillRemoteServer/README.md`

**Step 1: Update any documentation that references removed features or changed behaviour**

- Remove any mention of `SCHEMA_VERSION` if present
- Remove any mention of legacy schema migration if present
- Note that exports produce flat rows (one per probe per timestamp) rather than grouped rows

**Step 2: Commit**

```bash
git add iGrillRemoteServer/README.md
git commit -m "docs: update README to reflect code review fixes"
```
