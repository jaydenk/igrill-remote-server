# Server Polish Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:executing-plans to implement this plan task-by-task.

**Goal:** Remove ESP32 legacy code, redesign the data layer and session model, improve BLE reliability, add structured logging with metrics, enhance the web interface, and update documentation.

**Architecture:** Incremental refactor of the existing aiohttp server. Each task is self-contained and results in a working server. Changes build on each other in priority order: cleanup → data → sessions → BLE → logging → docs → web UI.

**Tech Stack:** Python 3.12, aiohttp 3.9.5, bleak 0.22.2, SQLite (via stdlib sqlite3), pytest + pytest-aiohttp for testing, vanilla JS for web UI, uPlot for charts.

---

### Task 1: ESP32 Cleanup & Attribution

**Files:**
- Delete: `components/igrill/` (entire directory)
- Delete: `components/igrill_ble_listener/` (entire directory)
- Delete: `full_example_mini.yaml`
- Delete: `full_example_V2.yaml`
- Delete: `full_example_V3.yaml`
- Delete: `full_example_pulse2000.yaml`
- Modify: `LICENSE`
- Modify: `AGENTS.md`
- Modify: `service/ble/protocol.py:1-10` (add attribution comment)

**Step 1: Remove ESP32 files**

```bash
rm -rf components/
rm full_example_mini.yaml full_example_V2.yaml full_example_V3.yaml full_example_pulse2000.yaml
```

**Step 2: Update LICENSE**

Add dual copyright. Keep the original MIT licence intact, add a second copyright line:

```
MIT License

Copyright (c) 2022 Bendik Wang Andreassen
Copyright (c) 2026 Jayden Kerr

Permission is hereby granted ...
```

**Step 3: Add attribution to protocol.py**

Add to the top of `service/ble/protocol.py`:

```python
"""BLE protocol constants for iGrill devices.

BLE GATT UUIDs and model definitions originally reverse-engineered by
Bendik Wang Andreassen for the esphome-igrill project:
https://github.com/bendikwa/esphome-igrill
"""
```

**Step 4: Update AGENTS.md**

Remove all ESPHome references (build commands, component structure). Update to reflect the Python-only server structure.

**Step 5: Commit**

```bash
git add -A
git commit -m "chore: remove ESP32/ESPHome code, add fork attribution"
```

---

### Task 2: Test Infrastructure

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`
- Create: `tests/test_config.py`
- Modify: `requirements.txt` (add test dependencies)

**Step 1: Add test dependencies to requirements.txt**

```
aiohttp==3.9.5
bleak==0.22.2
pytest==8.3.4
pytest-asyncio==0.24.0
```

**Step 2: Create test infrastructure**

`tests/__init__.py` — empty file.

`tests/conftest.py`:

```python
"""Shared fixtures for iGrill server tests."""

import asyncio
import pytest


@pytest.fixture
def tmp_db(tmp_path):
    """Return a path for a temporary SQLite database."""
    return str(tmp_path / "test.db")
```

**Step 3: Write a smoke test for Config**

`tests/test_config.py`:

```python
"""Tests for service.config."""

import os
from service.config import Config


def test_config_defaults():
    """Config.from_env() returns sensible defaults with no env vars."""
    cfg = Config.from_env()
    assert cfg.port == 39120
    assert cfg.poll_interval == 15
    assert cfg.timeout == 30
    assert cfg.log_level == "INFO"


def test_config_from_env(monkeypatch):
    """Config picks up environment overrides."""
    monkeypatch.setenv("IGRILL_PORT", "8080")
    monkeypatch.setenv("IGRILL_LOG_LEVEL", "DEBUG")
    cfg = Config.from_env()
    assert cfg.port == 8080
    assert cfg.log_level == "DEBUG"
```

**Step 4: Run tests**

```bash
cd iGrillRemoteServer && python -m pytest tests/ -v
```

Expected: 2 tests PASS.

**Step 5: Commit**

```bash
git add tests/ requirements.txt
git commit -m "test: add test infrastructure and config smoke tests"
```

---

### Task 3: Config Updates

**Files:**
- Modify: `service/config.py`
- Modify: `env.example`
- Create: `tests/test_config_new.py`

**Step 1: Write tests for new config fields**

`tests/test_config_new.py`:

```python
"""Tests for new config fields."""

from service.config import Config


def test_connect_timeout_default():
    cfg = Config.from_env()
    assert cfg.connect_timeout == 10


def test_max_backoff_default():
    cfg = Config.from_env()
    assert cfg.max_backoff == 60


def test_per_subsystem_log_levels(monkeypatch):
    monkeypatch.setenv("IGRILL_LOG_LEVEL_BLE", "DEBUG")
    cfg = Config.from_env()
    assert cfg.log_level_ble == "DEBUG"
    assert cfg.log_level_ws == ""  # not set, falls back to global
```

**Step 2: Run tests, verify they fail**

```bash
python -m pytest tests/test_config_new.py -v
```

Expected: FAIL — `Config` doesn't have these fields yet.

**Step 3: Add new fields to Config**

In `service/config.py`, add constants:

```python
DEFAULT_CONNECT_TIMEOUT = 10
DEFAULT_MAX_BACKOFF = 60
```

Add fields to `Config` dataclass:

```python
connect_timeout: int = DEFAULT_CONNECT_TIMEOUT
max_backoff: int = DEFAULT_MAX_BACKOFF
log_level_ble: str = ""
log_level_ws: str = ""
log_level_session: str = ""
log_level_alert: str = ""
log_level_http: str = ""
```

Add to `from_env()`:

```python
connect_timeout=_read_int_env("IGRILL_CONNECT_TIMEOUT", DEFAULT_CONNECT_TIMEOUT, min_value=1),
max_backoff=_read_int_env("IGRILL_MAX_BACKOFF", DEFAULT_MAX_BACKOFF, min_value=1),
log_level_ble=os.getenv("IGRILL_LOG_LEVEL_BLE", ""),
log_level_ws=os.getenv("IGRILL_LOG_LEVEL_WS", ""),
log_level_session=os.getenv("IGRILL_LOG_LEVEL_SESSION", ""),
log_level_alert=os.getenv("IGRILL_LOG_LEVEL_ALERT", ""),
log_level_http=os.getenv("IGRILL_LOG_LEVEL_HTTP", ""),
```

**Step 4: Update env.example**

Add the new variables with defaults and comments.

**Step 5: Run tests, verify they pass**

```bash
python -m pytest tests/ -v
```

Expected: All PASS.

**Step 6: Commit**

```bash
git add service/config.py env.example tests/test_config_new.py
git commit -m "feat: add connect_timeout, max_backoff, and per-subsystem log level config"
```

---

### Task 4: Data Layer — New Schema

**Files:**
- Create: `service/db/__init__.py`
- Create: `service/db/schema.py`
- Create: `service/db/migrations.py`
- Create: `tests/test_schema.py`

**Step 1: Write schema tests**

`tests/test_schema.py`:

```python
"""Tests for the database schema."""

import sqlite3
from service.db.schema import init_db, SCHEMA_VERSION


def test_init_db_creates_tables(tmp_db):
    conn = sqlite3.connect(tmp_db)
    init_db(conn)
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = sorted(row[0] for row in cursor.fetchall())
    assert "devices" in tables
    assert "sessions" in tables
    assert "session_devices" in tables
    assert "probe_readings" in tables
    assert "device_readings" in tables
    assert "session_targets" in tables
    assert "schema_version" in tables


def test_schema_version_recorded(tmp_db):
    conn = sqlite3.connect(tmp_db)
    init_db(conn)
    row = conn.execute(
        "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
    ).fetchone()
    assert row[0] == SCHEMA_VERSION


def test_init_db_idempotent(tmp_db):
    conn = sqlite3.connect(tmp_db)
    init_db(conn)
    init_db(conn)  # should not raise
    row = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()
    assert row[0] == 1
```

**Step 2: Run tests, verify they fail**

```bash
python -m pytest tests/test_schema.py -v
```

**Step 3: Implement schema module**

`service/db/__init__.py` — empty.

`service/db/schema.py`:

```python
"""Database schema definitions and initialisation."""

import sqlite3

SCHEMA_VERSION = 1

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS devices (
    address     TEXT PRIMARY KEY,
    name        TEXT,
    model       TEXT,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id           TEXT PRIMARY KEY,
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    start_reason TEXT NOT NULL,
    end_reason   TEXT
);

CREATE TABLE IF NOT EXISTS session_devices (
    session_id  TEXT NOT NULL REFERENCES sessions(id),
    address     TEXT NOT NULL REFERENCES devices(address),
    joined_at   TEXT NOT NULL,
    left_at     TEXT,
    PRIMARY KEY (session_id, address)
);

CREATE TABLE IF NOT EXISTS probe_readings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(id),
    address     TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    probe_index INTEGER NOT NULL,
    temperature REAL,
    UNIQUE(session_id, address, seq, probe_index)
);

CREATE TABLE IF NOT EXISTS device_readings (
    session_id  TEXT NOT NULL REFERENCES sessions(id),
    address     TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    battery     INTEGER,
    propane     REAL,
    heating_json TEXT,
    PRIMARY KEY (session_id, address, seq)
);

CREATE TABLE IF NOT EXISTS session_targets (
    session_id           TEXT NOT NULL REFERENCES sessions(id),
    address              TEXT NOT NULL,
    probe_index          INTEGER NOT NULL,
    mode                 TEXT NOT NULL DEFAULT 'fixed',
    target_value         REAL,
    range_low            REAL,
    range_high           REAL,
    pre_alert_offset     REAL DEFAULT 5.0,
    reminder_interval_secs INTEGER DEFAULT 0,
    PRIMARY KEY (session_id, address, probe_index)
);

CREATE INDEX IF NOT EXISTS idx_probe_readings_session ON probe_readings(session_id);
CREATE INDEX IF NOT EXISTS idx_probe_readings_lookup ON probe_readings(session_id, address, recorded_at);
CREATE INDEX IF NOT EXISTS idx_device_readings_session ON device_readings(session_id);
CREATE INDEX IF NOT EXISTS idx_session_devices_session ON session_devices(session_id);
CREATE INDEX IF NOT EXISTS idx_session_targets_session ON session_targets(session_id);
"""


def init_db(conn: sqlite3.Connection) -> None:
    """Create all tables if they don't exist and record schema version."""
    conn.executescript(_SCHEMA_SQL)

    existing = conn.execute(
        "SELECT version FROM schema_version WHERE version = ?",
        (SCHEMA_VERSION,),
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO schema_version (version) VALUES (?)",
            (SCHEMA_VERSION,),
        )
        conn.commit()
```

`service/db/migrations.py`:

```python
"""Schema migration runner. Currently a stub for future use."""

import sqlite3


def get_current_version(conn: sqlite3.Connection) -> int:
    """Return the current schema version, or 0 if not initialised."""
    try:
        row = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()
        return row[0] or 0
    except sqlite3.OperationalError:
        return 0
```

**Step 4: Run tests, verify they pass**

```bash
python -m pytest tests/test_schema.py -v
```

**Step 5: Commit**

```bash
git add service/db/ tests/test_schema.py
git commit -m "feat: add new normalised database schema"
```

---

### Task 5: Data Layer — Rewrite HistoryStore

**Files:**
- Rewrite: `service/history/store.py`
- Create: `tests/test_history_store.py`

This is the largest single task. The store needs to be completely rewritten to use the new schema, support multi-device sessions, and only record readings during active sessions.

**Step 1: Write tests for the new HistoryStore**

`tests/test_history_store.py`:

```python
"""Tests for the rewritten HistoryStore."""

import pytest
from service.history.store import HistoryStore


@pytest.fixture
def store(tmp_db):
    return HistoryStore(tmp_db, reconnect_grace=60)


@pytest.mark.asyncio
async def test_no_session_on_startup(store):
    """Server starts with no active session."""
    state = await store.get_session_state()
    assert state["current_session_id"] is None


@pytest.mark.asyncio
async def test_start_session(store):
    result = await store.start_session(
        addresses=["70:91:8F:00:00:01"],
        reason="user",
    )
    assert result["session_id"] is not None
    assert result["start_event"]["reason"] == "user"

    state = await store.get_session_state()
    assert state["current_session_id"] == result["session_id"]


@pytest.mark.asyncio
async def test_end_session(store):
    start = await store.start_session(
        addresses=["70:91:8F:00:00:01"],
        reason="user",
    )
    result = await store.end_session(reason="user")
    assert result is not None
    assert result["sessionId"] == start["session_id"]

    state = await store.get_session_state()
    assert state["current_session_id"] is None


@pytest.mark.asyncio
async def test_end_session_when_none_active(store):
    result = await store.end_session(reason="user")
    assert result is None


@pytest.mark.asyncio
async def test_record_reading_during_session(store):
    start = await store.start_session(
        addresses=["70:91:8F:00:00:01"],
        reason="user",
    )
    await store.record_reading(
        session_id=start["session_id"],
        address="70:91:8F:00:00:01",
        seq=1,
        probes=[{"index": 1, "temperature": 72.5}],
        battery=85,
        propane=None,
        heating=None,
    )
    items = await store.get_session_readings(start["session_id"])
    assert len(items) > 0


@pytest.mark.asyncio
async def test_multi_device_session(store):
    result = await store.start_session(
        addresses=["70:91:8F:00:00:01", "70:91:8F:00:00:02"],
        reason="user",
    )
    devices = await store.get_session_devices(result["session_id"])
    assert len(devices) == 2


@pytest.mark.asyncio
async def test_register_device(store):
    await store.register_device(
        address="70:91:8F:00:00:01",
        name="Kitchen iGrill",
        model="iGrill_V3",
    )
    devices = await store.list_devices()
    assert len(devices) == 1
    assert devices[0]["address"] == "70:91:8F:00:00:01"


@pytest.mark.asyncio
async def test_device_leave_and_rejoin(store):
    start = await store.start_session(
        addresses=["70:91:8F:00:00:01"],
        reason="user",
    )
    await store.device_left_session(
        session_id=start["session_id"],
        address="70:91:8F:00:00:01",
    )
    devices = await store.get_session_devices(start["session_id"])
    assert devices[0]["left_at"] is not None

    await store.device_rejoined_session(
        session_id=start["session_id"],
        address="70:91:8F:00:00:01",
    )
    devices = await store.get_session_devices(start["session_id"])
    assert devices[0]["left_at"] is None


@pytest.mark.asyncio
async def test_list_sessions(store):
    await store.start_session(addresses=["70:91:8F:00:00:01"], reason="user")
    await store.end_session(reason="user")
    sessions = await store.list_sessions(limit=10)
    assert len(sessions) == 1


@pytest.mark.asyncio
async def test_save_and_get_targets(store):
    from service.models.session import TargetConfig
    start = await store.start_session(
        addresses=["70:91:8F:00:00:01"],
        reason="user",
    )
    targets = [
        TargetConfig(probe_index=1, mode="fixed", target_value=74.0),
    ]
    await store.save_targets(start["session_id"], "70:91:8F:00:00:01", targets)
    loaded = await store.get_targets(start["session_id"])
    assert len(loaded) == 1
    assert loaded[0].target_value == 74.0
```

**Step 2: Run tests, verify they fail**

```bash
python -m pytest tests/test_history_store.py -v
```

**Step 3: Rewrite HistoryStore**

Complete rewrite of `service/history/store.py` to:
- Use the new schema from `service/db/schema.py`
- Generate UUID-based session IDs (TEXT primary key) instead of autoincrement
- No auto-session on startup — `_current_session_id` starts as `None`
- New methods: `start_session(addresses, reason)`, `end_session(reason)`, `device_left_session()`, `device_rejoined_session()`, `add_device_to_session()`, `register_device()`, `list_devices()`, `get_session_devices()`, `get_session_readings()`
- `record_reading()` writes to `probe_readings` (one row per probe) and `device_readings` (one row per cycle)
- Remove `ensure_session_for_reading()`, `force_new_session()` (replaced by explicit start/end)
- Keep `save_targets()`, `get_targets()`, `update_targets()` but updated for new schema (includes `address` column)
- Keep `list_sessions()`, `get_history_items()` updated for new schema

Key implementation details:
- Session IDs are `uuid.uuid4().hex` (32-char hex string)
- `start_session()` ends any existing session first, then creates new one
- `start_session()` registers devices if not already in `devices` table
- `record_reading()` inserts into both `probe_readings` and `device_readings`
- All public methods are `async` and use `self._lock`
- Keep `now_iso()` and `now_iso_utc()` module-level helpers

**Step 4: Run tests, verify they pass**

```bash
python -m pytest tests/test_history_store.py -v
```

**Step 5: Commit**

```bash
git add service/history/store.py tests/test_history_store.py
git commit -m "feat: rewrite HistoryStore for new normalised schema"
```

---

### Task 6: Session Handling — Wire Into Server

**Files:**
- Modify: `service/ble/device_worker.py` — remove auto-session logic, only record readings when session active
- Modify: `service/api/websocket.py` — update session_start_request for multi-device, update status_request
- Modify: `service/main.py` — remove auto-session creation on startup
- Create: `tests/test_session_lifecycle.py`

**Step 1: Write session lifecycle tests**

`tests/test_session_lifecycle.py`:

```python
"""Tests for session lifecycle integration."""

import pytest
from service.history.store import HistoryStore


@pytest.fixture
def store(tmp_db):
    return HistoryStore(tmp_db, reconnect_grace=60)


@pytest.mark.asyncio
async def test_readings_not_persisted_without_session(store):
    """Readings outside a session should not be stored."""
    state = await store.get_session_state()
    assert state["current_session_id"] is None
    # Attempting to record without a session should be a no-op or raise
    # The caller (device_worker) checks for active session before calling


@pytest.mark.asyncio
async def test_session_start_ends_previous(store):
    """Starting a new session auto-ends any active session."""
    r1 = await store.start_session(addresses=["70:91:8F:00:00:01"], reason="user")
    r2 = await store.start_session(addresses=["70:91:8F:00:00:01"], reason="user")
    assert r1["session_id"] != r2["session_id"]
    assert r2.get("end_event") is not None  # previous session ended


@pytest.mark.asyncio
async def test_all_devices_lost_scenario(store):
    """When all devices leave, session should track it."""
    start = await store.start_session(
        addresses=["70:91:8F:00:00:01", "70:91:8F:00:00:02"],
        reason="user",
    )
    sid = start["session_id"]
    await store.device_left_session(sid, "70:91:8F:00:00:01")
    await store.device_left_session(sid, "70:91:8F:00:00:02")

    all_left = await store.all_devices_left(sid)
    assert all_left is True
```

**Step 2: Run tests, verify they fail (new methods not yet wired)**

**Step 3: Update device_worker.py**

Key changes to `DeviceWorker`:
- Remove calls to `self.history.ensure_session_for_reading()` — no auto-session
- In `_poll_loop()`: check `await self.history.get_session_state()` for active session
- If session active and this device is in the session: record reading
- If no session: still publish reading to WebSocket (live dashboard works), but skip DB write
- Remove `_session_id` field — get session ID from history store
- Remove session event publishing from poll loop (sessions are managed externally)

**Step 4: Update websocket.py**

Key changes to `websocket_handler`:
- `session_start_request`: accept `deviceAddresses` (array) instead of `deviceAddress` (string)
  - If empty array or not provided: use all currently connected devices
  - Call `history.start_session(addresses=..., reason="user")`
  - Register targets with address per probe
- `session_end_request`: call `history.end_session(reason="user")`
  - Trigger post-session downsampling (background task)
- Add new `session_add_device_request` handler
- `status_request`: update to use new session state format

**Step 5: Update main.py**

- Remove auto-session creation — `HistoryStore.__init__` no longer creates sessions
- Pass `config` to `DeviceManager` constructor (needs `connect_timeout`)

**Step 6: Run all tests**

```bash
python -m pytest tests/ -v
```

**Step 7: Commit**

```bash
git add service/ble/device_worker.py service/api/websocket.py service/main.py tests/test_session_lifecycle.py
git commit -m "feat: user-initiated sessions with multi-device support"
```

---

### Task 7: Post-Session Downsampling

**Files:**
- Create: `service/history/downsampler.py`
- Create: `tests/test_downsampler.py`

**Step 1: Write downsampling tests**

`tests/test_downsampler.py`:

```python
"""Tests for post-session reading downsampling."""

import pytest
from datetime import datetime, timezone, timedelta
from service.history.store import HistoryStore
from service.history.downsampler import downsample_session


@pytest.fixture
def store(tmp_db):
    return HistoryStore(tmp_db, reconnect_grace=60)


@pytest.mark.asyncio
async def test_downsample_old_readings(store):
    """Readings older than 24h should be downsampled to 1-minute averages."""
    start = await store.start_session(
        addresses=["70:91:8F:00:00:01"],
        reason="user",
    )
    sid = start["session_id"]

    # Insert 60 readings at 15-second intervals (15 minutes of data)
    # dated 2 days ago
    base_time = datetime.now(timezone.utc) - timedelta(days=2)
    for i in range(60):
        ts = (base_time + timedelta(seconds=i * 15)).isoformat()
        await store.record_reading(
            session_id=sid,
            address="70:91:8F:00:00:01",
            seq=i,
            probes=[{"index": 1, "temperature": 70.0 + i * 0.1}],
            battery=85,
            propane=None,
            heating=None,
            recorded_at=ts,
        )

    await store.end_session(reason="user")

    before = await store.get_session_readings(sid)
    before_count = len(before)

    await downsample_session(store, sid)

    after = await store.get_session_readings(sid)
    # 60 readings over 15 min = 15 one-minute buckets
    assert len(after) < before_count
```

**Step 2: Implement downsampler**

`service/history/downsampler.py`:

```python
"""Post-session reading downsampler.

After a session ends, reduces storage by averaging readings:
- < 24 hours old: keep full resolution
- 1-7 days old: downsample to 1-minute averages
- > 7 days old: downsample to 5-minute averages
"""

import logging
from datetime import datetime, timezone, timedelta

from service.history.store import HistoryStore

LOG = logging.getLogger("igrill.session")

WINDOW_1_MIN = 60
WINDOW_5_MIN = 300
THRESHOLD_24H = timedelta(hours=24)
THRESHOLD_7D = timedelta(days=7)


async def downsample_session(store: HistoryStore, session_id: str) -> None:
    """Downsample readings for a completed session based on age thresholds."""
    now = datetime.now(timezone.utc)
    threshold_1min = (now - THRESHOLD_24H).isoformat()
    threshold_5min = (now - THRESHOLD_7D).isoformat()

    async with store._lock:
        # 1-minute downsampling for readings 1-7 days old
        _downsample_range(
            store._conn, session_id,
            older_than=threshold_1min,
            newer_than=threshold_5min,
            window_seconds=WINDOW_1_MIN,
        )

        # 5-minute downsampling for readings > 7 days old
        _downsample_range(
            store._conn, session_id,
            older_than=threshold_5min,
            newer_than=None,
            window_seconds=WINDOW_5_MIN,
        )

    LOG.info("downsample_complete session_id=%s", session_id)


def _downsample_range(conn, session_id, older_than, newer_than, window_seconds):
    """Replace readings in a time range with averaged values per window."""
    query = """
        SELECT id, address, recorded_at, probe_index, temperature, seq
        FROM probe_readings
        WHERE session_id = ? AND recorded_at < ?
    """
    params = [session_id, older_than]
    if newer_than:
        query += " AND recorded_at >= ?"
        params.append(newer_than)
    query += " ORDER BY address, probe_index, recorded_at"

    rows = conn.execute(query, params).fetchall()
    if not rows:
        return

    # Group by (address, probe_index) then bucket by time window
    buckets = {}
    for row in rows:
        key = (row[1], row[3])  # address, probe_index
        ts = datetime.fromisoformat(row[2])
        bucket_ts = ts.replace(
            second=(ts.second // window_seconds) * window_seconds if window_seconds <= 60
            else 0,
            microsecond=0,
        )
        if window_seconds > 60:
            minute_bucket = (ts.minute // (window_seconds // 60)) * (window_seconds // 60)
            bucket_ts = bucket_ts.replace(minute=minute_bucket)

        bucket_key = (key, bucket_ts.isoformat())
        if bucket_key not in buckets:
            buckets[bucket_key] = {"ids": [], "temps": [], "address": row[1],
                                    "probe_index": row[3], "ts": bucket_ts.isoformat(),
                                    "seq": row[5]}
        buckets[bucket_key]["ids"].append(row[0])
        if row[4] is not None:
            buckets[bucket_key]["temps"].append(row[4])

    # For each bucket with >1 reading: delete originals, insert average
    ids_to_delete = []
    inserts = []
    for bucket in buckets.values():
        if len(bucket["ids"]) <= 1:
            continue
        ids_to_delete.extend(bucket["ids"])
        avg_temp = sum(bucket["temps"]) / len(bucket["temps"]) if bucket["temps"] else None
        inserts.append((session_id, bucket["address"], bucket["ts"],
                       min(bucket["ids"]), bucket["probe_index"], avg_temp))

    if ids_to_delete:
        placeholders = ",".join("?" for _ in ids_to_delete)
        conn.execute(
            f"DELETE FROM probe_readings WHERE id IN ({placeholders})",
            ids_to_delete,
        )
    for ins in inserts:
        conn.execute(
            "INSERT INTO probe_readings (session_id, address, recorded_at, seq, probe_index, temperature) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ins,
        )
    conn.commit()
```

**Step 3: Run tests, verify they pass**

```bash
python -m pytest tests/test_downsampler.py -v
```

**Step 4: Commit**

```bash
git add service/history/downsampler.py tests/test_downsampler.py
git commit -m "feat: add post-session reading downsampler"
```

---

### Task 8: BLE Connection State Machine

**Files:**
- Create: `service/ble/connection_state.py`
- Modify: `service/ble/device_worker.py`
- Create: `tests/test_connection_state.py`

**Step 1: Write state machine tests**

`tests/test_connection_state.py`:

```python
"""Tests for BLE connection state machine."""

from service.ble.connection_state import ConnectionState, ConnectionStateMachine


def test_initial_state():
    sm = ConnectionStateMachine()
    assert sm.state == ConnectionState.DISCOVERED


def test_transition_discovered_to_connecting():
    sm = ConnectionStateMachine()
    sm.transition(ConnectionState.CONNECTING)
    assert sm.state == ConnectionState.CONNECTING


def test_full_happy_path():
    sm = ConnectionStateMachine()
    sm.transition(ConnectionState.CONNECTING)
    sm.transition(ConnectionState.AUTHENTICATING)
    sm.transition(ConnectionState.POLLING)
    assert sm.state == ConnectionState.POLLING


def test_disconnect_resets():
    sm = ConnectionStateMachine()
    sm.transition(ConnectionState.CONNECTING)
    sm.transition(ConnectionState.DISCONNECTED)
    assert sm.state == ConnectionState.DISCONNECTED


def test_backoff_after_disconnect():
    sm = ConnectionStateMachine()
    sm.transition(ConnectionState.DISCONNECTED)
    sm.transition(ConnectionState.BACKOFF)
    assert sm.state == ConnectionState.BACKOFF
    assert sm.backoff_seconds >= 2  # initial backoff


def test_exponential_backoff():
    sm = ConnectionStateMachine(max_backoff=60)
    for _ in range(5):
        sm.transition(ConnectionState.DISCONNECTED)
        sm.transition(ConnectionState.BACKOFF)
    assert sm.backoff_seconds > 2  # should have increased
    assert sm.backoff_seconds <= 60  # capped


def test_successful_connection_resets_backoff():
    sm = ConnectionStateMachine()
    sm.transition(ConnectionState.DISCONNECTED)
    sm.transition(ConnectionState.BACKOFF)
    sm.transition(ConnectionState.CONNECTING)
    sm.transition(ConnectionState.AUTHENTICATING)
    sm.transition(ConnectionState.POLLING)
    sm.transition(ConnectionState.DISCONNECTED)
    sm.transition(ConnectionState.BACKOFF)
    assert sm.backoff_seconds == 2  # reset after successful connection


def test_state_change_callback():
    changes = []
    sm = ConnectionStateMachine(on_change=lambda old, new: changes.append((old, new)))
    sm.transition(ConnectionState.CONNECTING)
    assert len(changes) == 1
    assert changes[0] == (ConnectionState.DISCOVERED, ConnectionState.CONNECTING)
```

**Step 2: Implement ConnectionStateMachine**

`service/ble/connection_state.py`:

```python
"""BLE connection state machine with exponential backoff."""

import enum
import random
from typing import Callable, Optional


class ConnectionState(enum.Enum):
    DISCOVERED = "discovered"
    CONNECTING = "connecting"
    AUTHENTICATING = "authenticating"
    POLLING = "polling"
    DISCONNECTED = "disconnected"
    BACKOFF = "backoff"


class ConnectionStateMachine:
    """Tracks connection state and manages backoff timing."""

    def __init__(
        self,
        initial_backoff: float = 2.0,
        max_backoff: float = 60.0,
        jitter_factor: float = 0.25,
        on_change: Optional[Callable[[ConnectionState, ConnectionState], None]] = None,
    ) -> None:
        self._state = ConnectionState.DISCOVERED
        self._initial_backoff = initial_backoff
        self._max_backoff = max_backoff
        self._jitter_factor = jitter_factor
        self._on_change = on_change
        self._consecutive_failures = 0
        self._backoff_seconds = initial_backoff
        self._had_successful_connection = False

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def backoff_seconds(self) -> float:
        return self._backoff_seconds

    def transition(self, new_state: ConnectionState) -> None:
        old_state = self._state
        if new_state == old_state:
            return

        if new_state == ConnectionState.POLLING:
            self._had_successful_connection = True

        if new_state == ConnectionState.BACKOFF:
            if self._had_successful_connection:
                self._consecutive_failures = 0
                self._had_successful_connection = False
            self._consecutive_failures += 1
            base = self._initial_backoff * (2 ** (self._consecutive_failures - 1))
            capped = min(base, self._max_backoff)
            jitter = random.uniform(0, capped * self._jitter_factor)
            self._backoff_seconds = capped + jitter

        self._state = new_state
        if self._on_change:
            self._on_change(old_state, new_state)
```

**Step 3: Run tests**

```bash
python -m pytest tests/test_connection_state.py -v
```

**Step 4: Integrate into DeviceWorker**

Update `service/ble/device_worker.py`:
- Add `ConnectionStateMachine` to `__init__`
- Use state transitions at each stage (connecting, authenticating, polling, disconnected, backoff)
- Use `state_machine.backoff_seconds` instead of fixed `asyncio.sleep(3)`
- Register Bleak `set_disconnected_callback` for immediate disconnect detection
- Add auth retry (up to 3 attempts)
- On disconnect: zero out probe readings in DeviceStore
- Accept `connect_timeout` separately from `timeout` (read timeout)
- Accept `max_backoff` from config
- Broadcast `device_state_change` events on state transitions

**Step 5: Run all tests**

```bash
python -m pytest tests/ -v
```

**Step 6: Commit**

```bash
git add service/ble/connection_state.py service/ble/device_worker.py tests/test_connection_state.py
git commit -m "feat: add BLE connection state machine with exponential backoff"
```

---

### Task 9: Device Manager Health Monitoring

**Files:**
- Modify: `service/ble/device_manager.py`
- Modify: `service/ble/device_worker.py` (expose state)

**Step 1: Add health check to DeviceManager**

In `scan_loop()`, after the scan block, add worker health checking:

```python
# Check worker health
dead_workers = []
for address, task in self._tasks.items():
    if task.done():
        exc = task.exception() if not task.cancelled() else None
        if exc:
            LOG.warning("Worker for %s died: %s", address, exc)
        dead_workers.append(address)

for address in dead_workers:
    LOG.info("Respawning worker for %s", address)
    worker = self._workers[address]
    await self.store.upsert(address, connected=False, error="worker_crashed")
    self._tasks[address] = asyncio.create_task(worker.run())
```

**Step 2: Pass new config to DeviceManager**

Update constructor to accept `connect_timeout` and `max_backoff`, pass them through to `DeviceWorker`.

**Step 3: Update main.py**

Pass `config.connect_timeout` and `config.max_backoff` to `DeviceManager`.

**Step 4: Run all tests**

```bash
python -m pytest tests/ -v
```

**Step 5: Commit**

```bash
git add service/ble/device_manager.py service/main.py
git commit -m "feat: add worker health monitoring and respawning"
```

---

### Task 10: Structured Logging

**Files:**
- Create: `service/logging_setup.py`
- Modify: `service/main.py`
- Modify: `service/ble/device_worker.py` (use `igrill.ble` logger)
- Modify: `service/ble/device_manager.py` (use `igrill.ble` logger)
- Modify: `service/api/websocket.py` (use `igrill.ws` logger)
- Modify: `service/history/store.py` (use `igrill.session` logger)
- Modify: `service/alerts/evaluator.py` (use `igrill.alert` logger)
- Modify: `service/api/routes.py` (use `igrill.http` logger)
- Create: `tests/test_logging.py`

**Step 1: Create logging setup module**

`service/logging_setup.py`:

```python
"""Structured logging setup with per-subsystem level control."""

import logging
from service.config import Config

SUBSYSTEM_LOGGERS = {
    "igrill.ble": "log_level_ble",
    "igrill.ws": "log_level_ws",
    "igrill.session": "log_level_session",
    "igrill.alert": "log_level_alert",
    "igrill.http": "log_level_http",
}

LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s %(message)s"


def setup_logging(config: Config) -> None:
    """Configure root and per-subsystem loggers."""
    global_level = getattr(logging, config.log_level.upper(), logging.INFO)

    logging.basicConfig(level=global_level, format=LOG_FORMAT)

    for logger_name, config_attr in SUBSYSTEM_LOGGERS.items():
        level_str = getattr(config, config_attr, "")
        if level_str:
            level = getattr(logging, level_str.upper(), None)
            if level is not None:
                logging.getLogger(logger_name).setLevel(level)


def update_log_level(logger_name: str, level_str: str) -> bool:
    """Update a logger's level at runtime. Returns True on success."""
    level = getattr(logging, level_str.upper(), None)
    if level is None:
        return False
    logging.getLogger(logger_name).setLevel(level)
    return True
```

**Step 2: Update all modules to use subsystem loggers**

Replace `LOG = logging.getLogger("igrill")` with the appropriate subsystem logger in each module:

- `device_worker.py`: `LOG = logging.getLogger("igrill.ble")`
- `device_manager.py`: `LOG = logging.getLogger("igrill.ble")`
- `websocket.py`: `LOG = logging.getLogger("igrill.ws")`
- `store.py`: `LOG = logging.getLogger("igrill.session")`
- `evaluator.py`: `LOG = logging.getLogger("igrill.alert")`
- `routes.py`: `LOG = logging.getLogger("igrill.http")`
- `main.py`: `LOG = logging.getLogger("igrill")`

**Step 3: Update main.py to use setup_logging**

Replace the `logging.basicConfig()` call in `run()` with:

```python
from service.logging_setup import setup_logging
setup_logging(config)
```

**Step 4: Improve log messages to structured key=value format**

Update log messages in device_worker.py to include structured data:

```python
# Before:
LOG.info("%s mac_address: %s connected_probes: %s", device_label, self.address, json.dumps(connected_probes))

# After:
LOG.info("device_connected address=%s model=%s rssi=%s", self.address, self._model.label if self._model else "unknown", rssi)
LOG.debug("read_probes address=%s probe_0=%s probe_1=%s battery=%s", self.address, ...)
```

**Step 5: Write logging tests**

`tests/test_logging.py`:

```python
"""Tests for logging setup."""

import logging
from service.config import Config
from service.logging_setup import setup_logging, update_log_level


def test_setup_logging_global_level(monkeypatch):
    monkeypatch.setenv("IGRILL_LOG_LEVEL", "WARNING")
    config = Config.from_env()
    setup_logging(config)
    assert logging.getLogger("igrill").getEffectiveLevel() <= logging.WARNING


def test_subsystem_override(monkeypatch):
    monkeypatch.setenv("IGRILL_LOG_LEVEL", "INFO")
    monkeypatch.setenv("IGRILL_LOG_LEVEL_BLE", "DEBUG")
    config = Config.from_env()
    setup_logging(config)
    assert logging.getLogger("igrill.ble").level == logging.DEBUG


def test_runtime_level_update():
    assert update_log_level("igrill.ble", "WARNING") is True
    assert logging.getLogger("igrill.ble").level == logging.WARNING


def test_invalid_level():
    assert update_log_level("igrill.ble", "INVALID") is False
```

**Step 6: Run all tests**

```bash
python -m pytest tests/ -v
```

**Step 7: Commit**

```bash
git add service/logging_setup.py service/main.py service/ble/ service/api/ service/history/ service/alerts/ tests/test_logging.py
git commit -m "feat: add structured logging with per-subsystem levels"
```

---

### Task 11: Prometheus Metrics

**Files:**
- Create: `service/metrics.py`
- Modify: `service/api/routes.py`
- Create: `tests/test_metrics.py`

**Step 1: Write metrics tests**

`tests/test_metrics.py`:

```python
"""Tests for Prometheus metrics."""

from service.metrics import MetricsRegistry


def test_counter_increment():
    m = MetricsRegistry()
    m.inc("igrill_ble_reads_total")
    assert m.get("igrill_ble_reads_total") == 1
    m.inc("igrill_ble_reads_total")
    assert m.get("igrill_ble_reads_total") == 2


def test_gauge_set():
    m = MetricsRegistry()
    m.set("igrill_devices_connected", 3)
    assert m.get("igrill_devices_connected") == 3


def test_labelled_counter():
    m = MetricsRegistry()
    m.inc("igrill_ws_messages_sent_total", labels={"type": "reading"})
    m.inc("igrill_ws_messages_sent_total", labels={"type": "reading"})
    m.inc("igrill_ws_messages_sent_total", labels={"type": "status"})
    output = m.render()
    assert 'igrill_ws_messages_sent_total{type="reading"} 2' in output
    assert 'igrill_ws_messages_sent_total{type="status"} 1' in output


def test_render_prometheus_format():
    m = MetricsRegistry()
    m.set("igrill_devices_connected", 2)
    m.inc("igrill_ble_reads_total")
    output = m.render()
    assert "igrill_devices_connected 2" in output
    assert "igrill_ble_reads_total 1" in output
```

**Step 2: Implement MetricsRegistry**

`service/metrics.py`:

```python
"""Lightweight Prometheus-compatible metrics registry.

No external dependencies — renders text exposition format for /metrics.
"""


class MetricsRegistry:
    """In-memory counters and gauges with Prometheus text output."""

    def __init__(self) -> None:
        self._counters: dict[str, float] = {}
        self._gauges: dict[str, float] = {}
        self._labelled: dict[str, dict[str, float]] = {}

    def inc(self, name: str, value: float = 1, labels: dict[str, str] | None = None) -> None:
        if labels:
            key = self._label_key(labels)
            self._labelled.setdefault(name, {})[key] = (
                self._labelled.get(name, {}).get(key, 0) + value
            )
        else:
            self._counters[name] = self._counters.get(name, 0) + value

    def set(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        if labels:
            key = self._label_key(labels)
            self._labelled.setdefault(name, {})[key] = value
        else:
            self._gauges[name] = value

    def get(self, name: str, labels: dict[str, str] | None = None) -> float:
        if labels:
            key = self._label_key(labels)
            return self._labelled.get(name, {}).get(key, 0)
        return self._counters.get(name, self._gauges.get(name, 0))

    def render(self) -> str:
        lines = []
        for name, value in sorted(self._gauges.items()):
            lines.append(f"{name} {value}")
        for name, value in sorted(self._counters.items()):
            lines.append(f"{name} {value}")
        for name, labelled in sorted(self._labelled.items()):
            for label_str, value in sorted(labelled.items()):
                lines.append(f"{name}{{{label_str}}} {value}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _label_key(labels: dict[str, str]) -> str:
        return ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
```

**Step 3: Wire into the app**

- Create `MetricsRegistry` in `main.py` `create_app()`, store as `app["metrics"]`
- Update `routes.py`: `/metrics` endpoint renders `app["metrics"].render()` as `text/plain`
- Add `metrics.inc()`/`metrics.set()` calls in:
  - `device_worker.py`: `igrill_ble_reads_total`, `igrill_ble_read_errors_total`, `igrill_ble_connection_attempts_total`, `igrill_probe_temperature_celsius`
  - `websocket.py`: `igrill_ws_clients_connected`, `igrill_ws_messages_sent_total`
  - `device_manager.py`: `igrill_devices_connected`

**Step 4: Run tests**

```bash
python -m pytest tests/ -v
```

**Step 5: Commit**

```bash
git add service/metrics.py service/api/routes.py service/main.py tests/test_metrics.py
git commit -m "feat: add Prometheus-compatible metrics endpoint"
```

---

### Task 12: REST API Updates

**Files:**
- Modify: `service/api/routes.py`
- Create: `tests/test_routes.py`

**Step 1: Add new REST endpoints**

```python
# GET /api/sessions — paginated session list
async def sessions_handler(request):
    history = request.app["history"]
    limit = int(request.query.get("limit", "20"))
    offset = int(request.query.get("offset", "0"))
    sessions = await history.list_sessions(limit=limit, offset=offset)
    return web.json_response({"sessions": sessions})

# GET /api/sessions/{id} — session detail with readings
async def session_detail_handler(request):
    history = request.app["history"]
    session_id = request.match_info["id"]
    readings = await history.get_session_readings(session_id)
    targets = await history.get_targets(session_id)
    devices = await history.get_session_devices(session_id)
    return web.json_response({
        "sessionId": session_id,
        "devices": devices,
        "targets": [t.to_dict() for t in targets],
        "readings": readings,
    })

# PUT /api/config/log-levels — runtime log level update
async def log_levels_handler(request):
    if not is_authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    body = await request.json()
    from service.logging_setup import update_log_level
    results = {}
    for logger_name, level in body.items():
        results[logger_name] = update_log_level(logger_name, level)
    return web.json_response({"results": results})
```

Register routes:
```python
app.router.add_get("/api/sessions", sessions_handler)
app.router.add_get("/api/sessions/{id}", session_detail_handler)
app.router.add_put("/api/config/log-levels", log_levels_handler)
```

**Step 2: Write route tests**

`tests/test_routes.py`:

```python
"""Tests for REST API routes."""

import pytest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop
from service.main import create_app
from service.config import Config


@pytest.fixture
def config(tmp_db):
    return Config(db_path=tmp_db)


@pytest.fixture
async def client(aiohttp_client, config):
    app = create_app(config)
    return await aiohttp_client(app)


@pytest.mark.asyncio
async def test_health_endpoint(client):
    resp = await client.get("/health")
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_sessions_endpoint_empty(client):
    resp = await client.get("/api/sessions")
    assert resp.status == 200
    data = await resp.json()
    assert data["sessions"] == []


@pytest.mark.asyncio
async def test_metrics_endpoint(client):
    resp = await client.get("/metrics")
    assert resp.status == 200
    assert resp.content_type == "text/plain"
```

**Step 3: Run tests**

```bash
python -m pytest tests/test_routes.py -v
```

**Step 4: Commit**

```bash
git add service/api/routes.py tests/test_routes.py
git commit -m "feat: add REST endpoints for sessions, session detail, and log levels"
```

---

### Task 13: Documentation

**Files:**
- Rewrite: `README.md`
- Modify: `AGENTS.md`
- Modify: `env.example`
- Modify: `docker-compose.yml` (add new env vars)

**Step 1: Rewrite README.md**

Structure:
1. Title + attribution ("Originally derived from [bendikwa/esphome-igrill](https://github.com/bendikwa/esphome-igrill)")
2. Supported devices (all iGrill models)
3. Quick start (Docker Compose + local dev)
4. Configuration (table of all env vars including new ones)
5. API reference:
   - REST endpoints (`/health`, `/metrics`, `/api/sessions`, `/api/sessions/{id}`, `/api/config/log-levels`)
   - WebSocket protocol v2 (all message types)
6. Architecture overview (BLE state machine, session model, data layer)
7. Development (running tests, project structure)

**Step 2: Update AGENTS.md**

Remove ESPHome references. Update:
- Project structure (new `service/db/`, `service/metrics.py`, `service/logging_setup.py`)
- Build/run commands (pytest, docker compose)
- Coding conventions (subsystem loggers, structured log format)

**Step 3: Update env.example**

Add all new environment variables:

```
IGRILL_CONNECT_TIMEOUT=10
IGRILL_MAX_BACKOFF=60
IGRILL_LOG_LEVEL_BLE=
IGRILL_LOG_LEVEL_WS=
IGRILL_LOG_LEVEL_SESSION=
IGRILL_LOG_LEVEL_ALERT=
IGRILL_LOG_LEVEL_HTTP=
```

**Step 4: Update docker-compose.yml**

Add new environment variables to the service definition.

**Step 5: Commit**

```bash
git add README.md AGENTS.md env.example docker-compose.yml
git commit -m "docs: rewrite documentation for new architecture"
```

---

### Task 14: Web UI — Layout & Live Dashboard

**Files:**
- Rewrite: `service/web/static/index.html`

**Step 1: Create tab-based layout**

Replace the current single-page layout with a three-tab structure:
- Live Dashboard (default)
- History
- Settings

Use CSS-only tab switching (radio inputs + `:checked` selectors) for zero-JS navigation.

**Step 2: Enhanced device cards**

- Connection state badge showing BLE state machine state (Connecting, Authenticating, Polling, Disconnected, Backoff) with colour coding
- RSSI signal strength indicator (bars icon)
- Probe temps with colour coding: normal=white, approaching=amber (#f5a623), reached=green (#27ae60), exceeded=red (#e94560)
- Target value shown inline with each probe reading
- Battery indicator with percentage and colour

**Step 3: Session banner**

- When no session: "Start Session" button
- When session active: session duration (auto-updating), device count, "End Session" button
- Session start flow: modal/panel with device picker (checkboxes), target configuration per probe

**Step 4: WebSocket integration**

- Subscribe to `device_state_change` events
- Update connection state badges in real-time
- Handle `session_start` / `session_end` events to toggle session banner

**Step 5: Commit**

```bash
git add service/web/static/index.html
git commit -m "feat: redesign web dashboard with tabs and session controls"
```

---

### Task 15: Web UI — Live Charts

**Files:**
- Modify: `service/web/static/index.html` (add chart section)
- Modify: `service/web/dashboard.py` (serve uPlot from CDN or bundle)

**Step 1: Add uPlot dependency**

Load uPlot from CDN in the HTML head:

```html
<link rel="stylesheet" href="https://unpkg.com/uplot@1.6.31/dist/uPlot.min.css">
<script src="https://unpkg.com/uplot@1.6.31/dist/uPlot.iife.min.js"></script>
```

**Step 2: Combined probe chart**

- Shows all active probes on one chart
- Distinct colours per probe (probe 1=blue, 2=green, 3=orange, 4=purple)
- Target threshold lines per probe (dashed horizontal lines, colour-matched)
- X-axis: time, Y-axis: temperature
- Auto-scrolls as new readings arrive
- Data buffer: in-memory array of readings, appended from WebSocket stream

**Step 3: Per-probe charts**

- Individual chart per active probe below the combined chart
- Larger view of single probe's temperature curve
- Target line(s) overlaid (fixed: single line, range: two lines for low/high)

**Step 4: Backfill on page load**

When loading mid-session:
- Fetch existing session readings via `GET /api/sessions/{id}`
- Populate chart data arrays before starting WebSocket stream

**Step 5: Chart component**

Create a reusable `createChart(container, probes, targets)` JS function that:
- Initialises a uPlot instance
- Accepts new data points via `addReading(ts, probes)`
- Handles target threshold line rendering
- Auto-sizes to container

This function is reused in both the live dashboard and history tab.

**Step 6: Commit**

```bash
git add service/web/static/index.html service/web/dashboard.py
git commit -m "feat: add live temperature charts with uPlot"
```

---

### Task 16: Web UI — History & Settings

**Files:**
- Modify: `service/web/static/index.html`

**Step 1: History tab — session list**

- Fetch sessions via `GET /api/sessions`
- Render cards: date, duration, devices involved, reading count
- Click to expand/navigate to session detail

**Step 2: History tab — session detail**

- Fetch session data via `GET /api/sessions/{id}`
- Render combined + per-probe charts (reuse chart component from Task 15)
- Summary stats below: min/max/average per probe, time to reach target
- Target lines overlaid on charts

**Step 3: Settings tab — device list**

- Show all discovered devices with: address, name, model, BLE connection state, last seen, RSSI
- No edit controls needed (informational)

**Step 4: Settings tab — server info**

- Display: server uptime, server version (hardcode or read from package), config values (poll interval, scan interval, etc.)

**Step 5: Settings tab — log level controls**

- Dropdown per subsystem logger (igrill.ble, igrill.ws, etc.)
- Current level shown
- On change: `PUT /api/config/log-levels` with the new level
- Toast/notification on success

**Step 6: Commit**

```bash
git add service/web/static/index.html
git commit -m "feat: add history browsing and settings to web UI"
```

---

### Task 17: Final Integration & Polish

**Files:**
- All modified files
- Create: `tests/test_integration.py`

**Step 1: Integration test**

`tests/test_integration.py`:

```python
"""Integration tests for the full server."""

import pytest
from aiohttp.test_utils import AioHTTPTestCase
from service.main import create_app
from service.config import Config


@pytest.fixture
def config(tmp_db):
    return Config(db_path=tmp_db)


@pytest.fixture
async def client(aiohttp_client, config):
    app = create_app(config)
    return await aiohttp_client(app)


@pytest.mark.asyncio
async def test_full_lifecycle(client):
    """Health check works, no session on start."""
    resp = await client.get("/health")
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"

    # No sessions initially
    resp = await client.get("/api/sessions")
    data = await resp.json()
    assert data["sessions"] == []

    # Metrics endpoint works
    resp = await client.get("/metrics")
    assert resp.status == 200

    # Dashboard serves
    resp = await client.get("/")
    assert resp.status == 200
```

**Step 2: Run full test suite**

```bash
python -m pytest tests/ -v --tb=short
```

**Step 3: Verify Docker build**

```bash
docker compose build
```

**Step 4: Final commit**

```bash
git add -A
git commit -m "test: add integration tests and final polish"
```
