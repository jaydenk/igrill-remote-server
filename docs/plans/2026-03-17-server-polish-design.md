# Server Polish — Design Document

**Date:** 2026-03-17
**Approach:** Incremental refactor of existing aiohttp server

## Priority Order

1. ESP32 cleanup & attribution
2. Data layer redesign
3. Session handling
4. BLE connection reliability
5. Structured logging & metrics
6. Documentation
7. Web interface

---

## 1. ESP32 Cleanup & Attribution

**Remove:**
- `components/` directory entirely (ESPHome C++ components and BLE listener)
- `full_example_*.yaml` (4 ESPHome config files)
- ESPHome references in `AGENTS.md`
- ESPHome sections from `README.md`

**Attribution:**
- Update `LICENSE` to dual copyright: original MIT from Bendik Wang Andreassen (2022) + new copyright for server/iOS work
- Add "Originally derived from [bendikwa/esphome-igrill](https://github.com/bendikwa/esphome-igrill)" prominently in README
- Add attribution comment in `service/ble/protocol.py` for the reverse-engineered BLE constants

---

## 2. Data Layer

### New Schema

```sql
CREATE TABLE schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE devices (
    address     TEXT PRIMARY KEY,
    name        TEXT,
    model       TEXT,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL
);

CREATE TABLE sessions (
    id           TEXT PRIMARY KEY,
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    start_reason TEXT NOT NULL,
    end_reason   TEXT
);

CREATE TABLE session_devices (
    session_id  TEXT NOT NULL REFERENCES sessions(id),
    address     TEXT NOT NULL REFERENCES devices(address),
    joined_at   TEXT NOT NULL,
    left_at     TEXT,
    PRIMARY KEY (session_id, address)
);

CREATE TABLE probe_readings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(id),
    address     TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    probe_index INTEGER NOT NULL,
    temperature REAL,
    UNIQUE(session_id, address, seq, probe_index)
);

CREATE TABLE device_readings (
    session_id  TEXT NOT NULL REFERENCES sessions(id),
    address     TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    battery     INTEGER,
    propane     REAL,
    heating_json TEXT,
    PRIMARY KEY (session_id, address, seq)
);

CREATE TABLE session_targets (
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
```

### Key Changes
- `probe_readings` replaces `probes_json` blob — one row per probe per read cycle, queryable
- `device_readings` separates device-level data (battery/propane/heating) from probe temps
- `session_devices` junction enables multi-device sessions
- `devices` table persists discovered devices across restarts
- `propane` and `heating_json` nullable — model-specific, NULL when not applicable
- No migration from v1 needed (no existing data)

### Post-Session Downsampling
When a session ends, background task:
1. Readings < 24 hours old: full resolution
2. Readings 1–7 days old: downsample to 1-minute averages
3. Readings > 7 days old: downsample to 5-minute averages

Downsampled rows replace originals in-place.

---

## 3. Session Handling

### Core Change
Sessions are user-initiated only. No auto-start on server restart, sensor reconnect, or idle timeout.

### Lifecycle

```
No Session (server default)
    │
    ├── User sends session_start_request (with device addresses + targets)
    │       → Create session
    │       → Attach specified devices via session_devices
    │       → Begin recording readings to DB
    │       → Broadcast session_start event
    │
Active Session
    │
    ├── Device disconnects
    │       → Mark device as "left" in session_devices (left_at)
    │       → Continue session (other devices may still be active)
    │       → Broadcast device_left event
    │       → If ALL devices disconnected for > reconnect_grace: auto-end session
    │
    ├── Device reconnects during session
    │       → Re-attach to session (clear left_at)
    │       → Resume recording
    │       → Broadcast device_rejoined event
    │
    ├── User sends session_end_request
    │       → End session, set end_reason="user"
    │       → Stop recording, trigger downsampling (background)
    │       → Broadcast session_end event
    │
    └── All devices disconnected beyond grace period
            → End session, set end_reason="all_devices_lost"
            → Trigger downsampling, broadcast session_end event
```

### Outside a Session
- BLE readings still polled and broadcast over WebSocket (live dashboard works)
- Not persisted to SQLite
- In-memory DeviceStore always has current state

### Multi-Device
- `session_start_request` accepts array of device addresses (empty = all connected)
- `session_add_device_request` to add devices mid-session
- Each device tracks join/leave independently

### Metadata
- Sessions table stays lean (lifecycle only)
- Cook metadata (notes, meat type) is iOS app concern, stored locally keyed by session_id

---

## 4. BLE Connection Reliability

### Device Worker Changes

1. **Exponential backoff with jitter** — start 2s, cap at 60s (configurable via `IGRILL_MAX_BACKOFF`), 0-25% random jitter
2. **Connection state machine** — `DISCOVERED → CONNECTING → AUTHENTICATING → POLLING → DISCONNECTED → BACKOFF`. Each transition logged and broadcast.
3. **Authentication retry** — up to 3 attempts before falling back to backoff
4. **Stale state cleanup** — on disconnect, zero out probe readings in DeviceStore, broadcast `device_disconnected` event
5. **Separate connection timeout** — `IGRILL_CONNECT_TIMEOUT` (default 10s) vs read timeout `IGRILL_TIMEOUT` (30s)
6. **Disconnect callback** — register Bleak's `set_disconnected_callback` for near-instant disconnect detection

### Device Manager Changes

7. **Worker health monitoring** — periodic check that workers are alive; respawn dead workers

### New Config
- `IGRILL_CONNECT_TIMEOUT` (default 10)
- `IGRILL_MAX_BACKOFF` (default 60)

---

## 5. Structured Logging & Metrics

### Categorised Loggers
- `igrill.ble` — connection state, auth, scans, read failures
- `igrill.session` — session start/end, targets
- `igrill.ws` — client connect/disconnect, messages, auth failures
- `igrill.alert` — target events
- `igrill.http` — REST requests

### Log Format (structured stdout)
```
2026-03-17T10:30:15+00:00 [igrill.ble] INFO device_connected address=70:91:8F:XX:XX:XX model=iGrill_V3 rssi=-62
```

### Per-Subsystem Log Levels
- `IGRILL_LOG_LEVEL` — global default (INFO)
- `IGRILL_LOG_LEVEL_BLE` — BLE override
- `IGRILL_LOG_LEVEL_WS` — WebSocket override

### Prometheus Metrics (`/metrics`)
- `igrill_devices_connected` (gauge)
- `igrill_ble_reads_total` (counter)
- `igrill_ble_read_errors_total` (counter)
- `igrill_ble_connection_attempts_total` (counter, label: result)
- `igrill_ws_clients_connected` (gauge)
- `igrill_ws_messages_sent_total` (counter, label: type)
- `igrill_sessions_total` (counter, label: reason)
- `igrill_probe_temperature_celsius` (gauge, labels: address, probe_index)

No external dependency — Prometheus text exposition format.

---

## 6. Documentation

- **README:** Remove ESP32 sections, add fork attribution, document setup/config/API/models
- **LICENSE:** Dual copyright (Bendik Wang Andreassen 2022 + new)
- **AGENTS.md:** Remove ESPHome commands, update structure
- **env.example:** Add new config vars

---

## 7. Web Interface (Lowest Priority)

### Single-page vanilla JS app with tab navigation

**Live Dashboard (default tab):**
- Device cards with connection state badge, RSSI, probe temps with colour coding (approaching=amber, reached=green, exceeded=red), inline targets
- Session banner with duration, device count, end button
- Start session button with device picker and target config
- **Live charts (when session active):**
  - Combined chart — all probes + per-probe target lines (dashed, colour-matched)
  - Per-probe charts — individual larger views with target lines
  - Auto-scroll, backfill from REST on mid-session page load

**History (second tab):**
- Session list cards (date, duration, devices, reading count)
- Session detail with temperature charts (same component as live), summary stats (min/max/avg, time to target)

**Settings (third tab):**
- Connected devices with BLE state, last seen, model
- Server info (uptime, version, config)
- Log level controls (runtime adjustment via API)

### New API Surface
- `device_state_change` WebSocket event
- `log_level_update_request` / `log_level_update_ack` WebSocket messages
- `GET /api/sessions` — paginated session list
- `GET /api/sessions/{id}` — session detail with readings
- `PUT /api/config/log-levels` — runtime log level update

### Charting
- Single reusable chart component for both live and history views
- Lightweight library (uPlot ~35KB) or Canvas-based
