# iGrill Remote Server

Originally derived from [bendikwa/esphome-igrill](https://github.com/bendikwa/esphome-igrill) by Bendik Wang Andreassen.

## Overview

A standalone BLE polling service for Weber iGrill thermometer devices. It continuously scans for and connects to iGrill devices over Bluetooth Low Energy, exposes real-time temperature data via an HTTP + WebSocket API, and serves a single-page web dashboard for live monitoring.

## Supported Devices

- iGrill mini
- iGrill mini V2
- iGrill V2
- iGrill V202
- iGrill V3
- iDevices Kitchen Thermometer
- Weber Pulse 1000
- Weber Pulse 2000

## Quick Start

### Docker Compose (recommended)

The image is built automatically by CI and published to the GitHub Container Registry.

```sh
cp env.example .env            # edit values as needed
docker compose up -d
curl http://localhost:39120/health
```

To update to the latest image:

```sh
docker compose pull && docker compose up -d
```

By default the container exposes the port directly. To place the service behind a [Traefik](https://traefik.io/) reverse proxy, create a `docker-compose.override.yml` (gitignored) alongside the base compose file:

```yaml
services:
  igrill:
    ports: !reset []
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.igrill.rule=Host(`igrill.${HOSTNAME}`)"
      - "traefik.http.routers.igrill.entrypoints=web-secure"
      - "traefik.http.routers.igrill.tls.certresolver=myresolver"
      - "traefik.http.services.igrill.loadbalancer.server.port=39120"
    networks:
      - proxy

networks:
  proxy:
    external: true
```

Ensure the external network exists: `docker network create proxy`

### Local Development

```sh
pip install -r requirements-dev.txt
python -m service.main
```

The server binds to `0.0.0.0:39120` by default. All configuration is via environment variables (see below).

## Configuration

Copy `env.example` to `.env` and edit values as needed. All variables are optional and have sensible defaults.

| Variable | Default | Description |
| --- | --- | --- |
| `IGRILL_PORT` | `39120` | HTTP server port. |
| `IGRILL_POLL_INTERVAL` | `15` | BLE polling interval in seconds (clamped to 5-60). |
| `IGRILL_TIMEOUT` | `30` | GATT characteristic read timeout in seconds. |
| `IGRILL_CONNECT_TIMEOUT` | `10` | BLE connection timeout in seconds (separate from read timeout). |
| `IGRILL_MAX_BACKOFF` | `60` | Maximum exponential backoff delay in seconds between reconnection attempts. |
| `IGRILL_SCAN_INTERVAL` | `60` | Time between BLE discovery scans in seconds. |
| `IGRILL_SCAN_TIMEOUT` | `5` | Duration of each BLE discovery scan in seconds. |
| `IGRILL_RECONNECT_GRACE` | `60` | Seconds within which a reconnecting device reuses its existing session membership. |
| `IGRILL_DB_PATH` | `/data/igrill.db` | SQLite database path for persisted sessions and readings. |
| `IGRILL_MAC_PREFIX` | `70:91:8F` | MAC address prefix used to filter devices during scans. |
| `IGRILL_BIND_ADDRESS` | `0.0.0.0` | HTTP server bind address. |
| `IGRILL_LOG_LEVEL` | `INFO` | Global log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |
| `IGRILL_LOG_LEVEL_BLE` | *(global)* | Override log level for the BLE subsystem (`igrill.ble`). |
| `IGRILL_LOG_LEVEL_WS` | *(global)* | Override log level for the WebSocket subsystem (`igrill.ws`). |
| `IGRILL_LOG_LEVEL_SESSION` | *(global)* | Override log level for the session/history subsystem (`igrill.session`). |
| `IGRILL_LOG_LEVEL_ALERT` | *(global)* | Override log level for the alert subsystem (`igrill.alert`). |
| `IGRILL_LOG_LEVEL_HTTP` | *(global)* | Override log level for the HTTP subsystem (`igrill.http`). |
| `IGRILL_SESSION_TOKEN` | *(empty)* | If set, requires `Authorization: Bearer <token>` on WebSocket session-control messages. |
| `IGRILL_CORS_ORIGIN` | *(empty)* | If set, adds CORS `Access-Control-Allow-Origin` headers (e.g. `*` for development). A warning is logged if set to `*`. |
| `IGRILL_APNS_KEY_PATH` | *(empty)* | Path to the APNS `.p8` private key file. Push notifications are disabled when any APNS credential is missing. |
| `IGRILL_APNS_KEY_ID` | *(empty)* | Apple APNS key identifier. |
| `IGRILL_APNS_TEAM_ID` | *(empty)* | Apple Developer Team ID. |
| `IGRILL_APNS_BUNDLE_ID` | *(empty)* | iOS app bundle identifier (e.g. `com.example.iGrillRemote`). |
| `IGRILL_APNS_USE_SANDBOX` | `true` | Use the APNS sandbox environment (`true`, `1`, `yes`) or production (`false`, `0`, `no`). |

## Push Notifications

Push notifications are **optional**. When configured, the server sends APNS alerts to registered iOS devices for target-reached events and other session alerts. Without APNS credentials the server runs normally — all alerts are still delivered to connected WebSocket clients.

### Obtaining an APNS Key

1. Sign in to the [Apple Developer Portal](https://developer.apple.com/account/).
2. Navigate to **Certificates, Identifiers & Profiles** → **Keys**.
3. Create a new key with the **Apple Push Notifications service (APNs)** capability enabled.
4. Download the resulting `.p8` file (e.g. `AuthKey_XXXXXXXXXX.p8`). This file can only be downloaded once — store it securely.
5. Note the **Key ID** shown on the key details page and your **Team ID** from the top-right of the portal (or **Membership** → **Team ID**).

### Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `IGRILL_APNS_KEY_PATH` | *(empty)* | Path to the `.p8` private key file inside the container. Push notifications are disabled when any APNS credential is missing. |
| `IGRILL_APNS_KEY_ID` | *(empty)* | The Key ID shown in the Apple Developer Portal when the key was created. |
| `IGRILL_APNS_TEAM_ID` | *(empty)* | Your Apple Developer Team ID. |
| `IGRILL_APNS_BUNDLE_ID` | *(empty)* | The iOS app's bundle identifier (e.g. `com.example.iGrillRemote`). |
| `IGRILL_APNS_USE_SANDBOX` | `true` | Set to `true` for development/TestFlight builds or `false` for production App Store builds. |

### Docker Setup

Mount the `.p8` key file into the container by uncommenting the volume line in `docker-compose.yml`:

```yaml
volumes:
  - ./AuthKey.p8:/app/AuthKey.p8:ro
```

Then set the environment variables in your `.env` file:

```env
IGRILL_APNS_KEY_PATH=/app/AuthKey.p8
IGRILL_APNS_KEY_ID=XXXXXXXXXX
IGRILL_APNS_TEAM_ID=YYYYYYYYYY
IGRILL_APNS_BUNDLE_ID=com.example.iGrillRemote
IGRILL_APNS_USE_SANDBOX=true
```

## API Reference

### REST Endpoints

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/` | Web dashboard — tab-based single-page UI (Live, History, Settings) with real-time WebSocket updates, session controls (including optional session name input), BLE state indicators, live temperature charts (uPlot) with probe labels, past session browsing with names, notes, and full-timeline charts and summary statistics, and runtime log level management. |
| `GET` | `/health` | Health check with uptime, device counts, active session ID, poll interval, and scan interval. |
| `GET` | `/api/sessions` | Paginated session list (`?limit=20&offset=0`). |
| `GET` | `/api/sessions/{id}` | Session detail with `name`, `notes`, devices, targets, and readings. Returns 404 if the session does not exist. |
| `GET` | `/api/sessions/{id}/export` | Export session data as CSV (`?format=csv`) or enriched JSON (`?format=json`, default). CSV columns: `timestamp`, `probe_index`, `label`, `temperature_c`, `battery_pct`, `propane_pct`. |
| `POST` | `/api/v1/devices/push-token` | Register or update an APNS push token. Body: `{"token": "hex_device_token", "liveActivityToken": "hex_la_token"}`. The `liveActivityToken` field is optional. |
| `POST` | `/api/v1/simulate/start` | Start a simulated cook session. Body (optional): `{"speed": 10, "probes": 4}`. `speed` is the time multiplier (default 10), `probes` is the number of active probes 1-4 (default 4). Returns `sessionId`, `speed`, and `probes`. Returns 409 if a simulation is already running. Requires authorisation. |
| `POST` | `/api/v1/simulate/stop` | Stop the running simulation. Returns the `sessionId` and total `readings` count. Returns 400 if no simulation is running. Requires authorisation. |
| `PUT` | `/api/config/log-levels` | Runtime log level update (requires authorisation). |

### WebSocket Protocol (v2)

Connect to `/ws` for real-time streaming. All messages use the v2 envelope format:

```json
{"v": 2, "type": "<msg_type>", "ts": "...", "requestId": "...", "payload": {}}
```

**Client request types:**

| Type | Description |
| --- | --- |
| `status_request` | Returns device state, session info, sample rate, active targets, and session devices. |
| `sessions_request` | Lists recent sessions (`payload.limit` defaults to 20, max 100; `payload.offset` defaults to 0). |
| `history_request` | Streams history chunks (`sinceTs`, `untilTs`, `limit` (max 10,000), `sessionId`, `chunkSize`). |
| `session_start_request` | Starts a new user-initiated session. Accepts optional `name` (string), `targets` array, and `deviceAddresses` (array) or `deviceAddress` (string). If no devices are specified, all currently connected devices are included. Requires authorisation. |
| `session_end_request` | Ends the current session. Requires authorisation. |
| `session_update_request` | Updates `name` and/or `notes` on a session. Optional `sessionId` field; defaults to the active session. Requires authorisation. |
| `session_add_device_request` | Adds a device to the active session mid-cook. Requires `deviceAddress` in payload. Requires authorisation. |
| `target_update_request` | Updates targets for the current session. Accepts optional `deviceAddress` to scope targets to a specific device. Requires authorisation. |

**Server response types:**

| Type | Description |
| --- | --- |
| `status` | Response to `status_request` with device state, session info (including `currentSessionName` when a session is active), sample rate, active targets, and session devices. |
| `sessions_list` | Response to `sessions_request` with recent session summaries. |
| `history_chunk` / `history_end` | Streamed response to `history_request`. |
| `session_start_ack` | Acknowledgement for `session_start_request`. Includes `sessionId`, `sessionStartTs`, `name`, `devices`, and `targets`. |
| `session_end_ack` | Acknowledgement for `session_end_request`. |
| `session_update_ack` | Acknowledgement for `session_update_request`. Includes updated `name` and `notes`. |
| `target_update_ack` | Acknowledgement for `target_update_request`. |
| `session_add_device_ack` | Acknowledgement for `session_add_device_request`. |

**Server broadcast types:**

| Type | Description |
| --- | --- |
| `reading` | Pushed on each poll cycle with latest probe data (always broadcast, regardless of session state). |
| `session_start` / `session_end` | Broadcast when sessions change. |
| `device_joined` | Broadcast when a device is added to an active session. |
| `target_approaching` | Probe temperature crossed the pre-alert threshold. In range mode, may include `"subtype": "high"` when approaching the upper bound. |
| `target_reached` | Probe temperature hit the target. |
| `target_exceeded` | Probe temperature went above the target. |
| `target_reminder` | Periodic nudge while temperature remains above target. |
| `device_state_change` | Broadcast when a device's connection state changes (e.g. connecting, polling, disconnected, backoff). |

> **Note:** `curl` does not support WebSockets. Use a client such as `websocat`, `wscat`, or an iOS `URLSessionWebSocketTask`.

## Architecture

### BLE Connection State Machine

Each device worker manages a six-state connection lifecycle: `discovered` -> `connecting` -> `authenticating` -> `polling` -> `disconnected` -> `backoff` -> `connecting` (retry). On disconnect or error, the worker uses exponential backoff (starting at 2 seconds, capped at `IGRILL_MAX_BACKOFF`) before attempting reconnection. A successful connection resets the backoff counter. Authentication is retried up to three times before failing. Probe readings are zeroed on disconnect to avoid displaying stale data.

BLE drops are detected reactively via `BleakClient`'s `disconnected_callback`, which sets a per-connection asyncio event that the poll loop awaits in place of a plain `asyncio.sleep(poll_interval)`. This keeps detection latency close to the BlueZ d-bus round trip (typically under a second) instead of up to a full poll interval. Probe characteristics that return ATT "Unlikely Error" (iGrill V2/V202 empty-socket behaviour) are cached per connection and emitted as synthetic unplugged entries, so empty sockets do not spam the log or waste GATT reads on each poll.

### Scan Loop Observability

Each scan cycle emits a single `scan_complete total=N matches=M workers=W new=X` INFO log: `total` is the number of BLE devices seen by the adapter, `matches` is how many had the configured `IGRILL_MAC_PREFIX`, `workers` is the total active device workers, and `new` is how many workers were spawned this cycle. An iGrill that never appears (matches always 0) despite being powered on usually means it is already connected to another central — iGrill peripherals stop advertising while connected.

### User-Initiated Sessions

Sessions are user-initiated only — no session is auto-created on startup or when a device connects. The device worker always polls BLE and broadcasts live readings to WebSocket clients, but only records to the database and evaluates alert targets when a session is active and the device is part of it.

### Multi-Device Session Support

A single session can include multiple iGrill devices. Devices can be added to an active session at any time via `session_add_device_request`. When a device disconnects during a session, it is marked as having left; on reconnect within the grace period, it is automatically rejoined.

### Normalised Data Layer

Session data is stored in a normalised SQLite schema: sessions, session-device membership, per-probe readings, and per-device targets are all separate tables linked by foreign keys with UUID session identifiers. Schema changes are applied automatically via a sequential migration runner on startup — each migration runs inside an explicit transaction and is fully rolled back if any statement fails. Duplicate readings (same session, address, and sequence number) are silently ignored to prevent data loss on worker respawn.

### Post-Session Downsampling

When a session ends, the raw readings are downsampled to reduce storage. Both probe readings and device readings (battery, propane, heating) are cleaned up together so that historical queries remain consistent. The entire downsampling pass runs inside a transaction — if anything fails, the original readings are preserved via rollback. This preserves the overall shape of the temperature curve while significantly reducing database size for long cooks.

### Device Manager Health Monitoring

The device manager monitors worker health on each scan cycle. If a worker task crashes due to an unhandled exception, it is automatically respawned and the device store is updated with an error status.

## Project Structure

```
service/
  __init__.py
  config.py              # Centralised configuration from environment variables
  logging_setup.py       # Structured logging with per-subsystem level control
  main.py                # App factory and entry point
  alerts/
    evaluator.py         # Checks probes against targets, emits alert events
  api/
    envelope.py          # WebSocket v2 message envelope construction
    routes.py            # HTTP route handlers and route registration
    websocket.py         # WebSocketHub, WebSocketClient, and v2 protocol handler
  ble/
    protocol.py          # BLE protocol constants, model definitions, and detection
    connection_state.py  # ConnectionStateMachine with exponential backoff
    device_worker.py     # Connects, authenticates, and polls a single iGrill device
    device_manager.py    # Scans for iGrill devices and spawns/monitors workers
  db/
    schema.py            # Normalised database schema definitions and init_db()
    migrations.py        # Sequential schema migration runner
  history/
    downsampler.py       # Post-session reading downsampling
    store.py             # SQLite-backed sessions, readings, and targets
  models/
    device.py            # DeviceStore — async-safe in-memory device state
    reading.py           # Temperature probe parsing and reading payload builder
    session.py           # TargetConfig dataclass for probe target temperatures
  push/
    service.py           # APNS push notification service (alerts and Live Activity updates)
  simulate/
    curves.py            # Temperature curve generators (fixed target, range oscillation)
    runner.py            # Background task that generates simulated iGrill readings
  web/
    dashboard.py         # Dashboard route handler and static file serving
    static/
      index.html         # Tab-based monitoring dashboard with live view (session name, probe labels), session history with names, notes, and full-timeline charts and summary statistics, settings with device info and runtime log level controls (vanilla HTML/CSS/JS, uPlot)
tests/
  conftest.py            # Shared pytest fixtures
  test_alert_evaluator.py # AlertEvaluator unit tests (range approaching-high, reached, exceeded)
  test_alerts.py         # AlertEvaluator tests
  test_config.py         # Configuration module tests
  test_config_new.py     # Extended configuration tests
  test_connection_state.py  # ConnectionStateMachine tests
  test_downsampler.py    # Post-session downsampling tests
  test_history_store.py  # HistoryStore tests
  test_logging.py        # Structured logging tests
  test_models.py         # Data models tests
  test_protocol.py       # BLE protocol module tests
  test_integration.py    # Full-server integration tests
  test_routes.py         # HTTP route handler tests
  test_push_service.py   # Push notification service tests
  test_schema.py         # Database schema tests
  test_simulate_api.py   # Simulation API endpoint tests
  test_simulate_curves.py # Temperature curve generator tests
```

## Development

### Running Tests

```sh
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

### Docker Build

```sh
docker compose build
docker compose up -d
```

## CI/CD

A GitHub Actions workflow (`.github/workflows/ci.yml`) runs on every push and pull request to `main`:

1. **Test** — installs dependencies and runs `pytest` on Ubuntu.
2. **Docker** — on merge to `main`, builds and pushes the Docker image to `ghcr.io/jaydenk/igrill-remote-server:latest` (and a SHA-tagged variant).

To pull the pre-built image instead of building locally:

```sh
docker pull ghcr.io/jaydenk/igrill-remote-server:latest
```

## BLE Host Requirements

- The host must run BlueZ. Mount `/run/dbus` into the container and set `DBUS_SYSTEM_BUS_ADDRESS=unix:path=/run/dbus/system_bus_socket`.
- The container must be able to access the host Bluetooth adapter (runs as root in Docker by default).
- BLE devices accept only one connection at a time — disconnect the mobile app before connecting the server.
