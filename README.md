# docker-igrill

Standalone BLE polling service for iGrill devices that exposes JSON metrics over HTTP and real-time data via WebSocket.

Originally derived from [bendikwa/esphome-igrill](https://github.com/bendikwa/esphome-igrill) by Bendik Wang Andreassen.

## Project Structure

```
service/
  config.py        # Centralised configuration from environment variables
  __init__.py      # Package marker
  main.py          # App factory and entry point (thin ~90-line module)
  ble/
    protocol.py       # BLE protocol constants, model definitions, and detection
    device_worker.py  # DeviceWorker — connects, authenticates, and polls a single iGrill device
    device_manager.py # DeviceManager — scans for iGrill devices and spawns workers
  api/
    envelope.py    # WebSocket v2 message envelope construction
    websocket.py   # WebSocketHub, WebSocketClient, and v2 protocol handler
    routes.py      # HTTP route handlers (/metrics, /history, /health) and route setup
  models/
    device.py      # DeviceStore — async-safe in-memory device state
    reading.py     # Temperature probe parsing and reading payload builder
    session.py     # TargetConfig dataclass for probe target temperatures
  history/
    store.py       # HistoryStore — SQLite-backed sessions, readings, and session targets
  alerts/
    evaluator.py   # AlertEvaluator — checks probes against targets, emits alert events
  web/
    dashboard.py   # Dashboard route handler and static file serving
    static/
      index.html   # Single-page monitoring dashboard (vanilla HTML/CSS/JS)
tests/
  conftest.py        # Shared pytest fixtures (tmp_db, etc.)
  test_config.py     # Configuration module tests
  test_protocol.py   # BLE protocol module tests
  test_models.py     # Data models tests (device store, readings, session config)
  test_history.py    # HistoryStore tests (sessions, targets)
  test_alerts.py     # AlertEvaluator tests (approaching, reached, exceeded, range, clear)
```

## Quick Start (Docker Compose)

The service is designed to sit behind a [Traefik](https://traefik.io/) reverse proxy. The compose file declares an external `proxy` network and Traefik labels, so ensure your Traefik instance is running and the network exists:

```sh
docker network create proxy   # one-time setup, if not already created
docker compose up -d --build
curl https://igrill.<your-hostname>/metrics
```

If you need direct port access for local development, add a `ports` section to the compose override:

```yaml
# docker-compose.override.yml
services:
  igrill:
    ports:
      - "${IGRILL_PORT:-39120}:${IGRILL_PORT:-39120}"
```

## Configuration
To override defaults with a file, copy `env.example` to `.env` and edit values.

| Variable | Default | Possible values | Notes |
| --- | --- | --- | --- |
| `IGRILL_PORT` | `39120` | integer (1-65535) | HTTP port for `/metrics`. |
| `IGRILL_POLL_INTERVAL` | `15` | integer (5-60) | Polling interval in seconds. |
| `IGRILL_TIMEOUT` | `30` | integer (>=1) | GATT read/connect timeout in seconds. |
| `IGRILL_MAC_PREFIX` | `70:91:8F` | MAC prefix string | Prefix used to filter devices during scans. |
| `IGRILL_BIND_ADDRESS` | `0.0.0.0` | IP address | Bind address for the HTTP server. |
| `IGRILL_SCAN_INTERVAL` | `60` | integer (>=1) | Time between BLE scans in seconds. |
| `IGRILL_SCAN_TIMEOUT` | `5` | integer (>=1) | Duration of each BLE scan in seconds. |
| `IGRILL_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` | Controls log verbosity for the service and BLE layer. |
| `IGRILL_RECONNECT_GRACE` | `60` | integer (>=0) | Reuse the same session if a reconnect happens within this window. |
| `IGRILL_DB_PATH` | `/data/igrill.db` | file path | SQLite DB location for persisted history. |
| `IGRILL_SESSION_TOKEN` | empty | string | If set, require `Authorization: Bearer <token>` on WebSocket to start sessions. |

## Supported Devices

- IGrill mini
- IGrill mini V2
- IGrill V2
- IGrill V202
- IGrill V3
- iDevices Kitchen Thermometer
- Weber Pulse 1000
- Weber Pulse 2000

## API
- `GET /` serves the web dashboard — a single-page monitoring UI showing real-time device status, probe temperatures, and session information via WebSocket.
- `GET /metrics` returns the latest readings for all discovered devices.
- `GET /history` returns all persisted sessions and readings (optional `?mac=70:91:8F:...`).
- `GET /health` returns a lightweight health check with uptime, device counts, and active session ID.

History is stored in SQLite at `IGRILL_DB_PATH` (default `/data/igrill.db`). Ensure the container has a persistent `/data` volume if you want history across restarts.

Example response:
```json
{
  "generated_at": "2024-01-01T12:00:00+00:00",
  "device_count": 1,
  "devices": [
    {
      "address": "70:91:8F:AA:BB:CC",
      "name": "iGrill_v3",
      "model": "igrill_v3",
      "model_name": "IGrill V3",
      "connected": true,
      "last_seen": "2024-01-01T12:00:00+00:00",
      "last_update": "2024-01-01T12:00:15+00:00",
      "unit": "F",
      "battery_percent": 92,
      "propane_percent": null,
      "probes": [
        { "index": 1, "temperature": 145.0, "raw": 145, "unplugged": false }
      ],
      "connected_probes": [1],
      "probe_status": "probes_connected",
      "pulse": {},
      "error": null,
      "rssi": -62
    }
  ]
}
```

### WebSocket Streaming (v2 Protocol)
Connect to `/ws` for real-time streaming. All messages use the v2 envelope format:

```json
{"v": 2, "type": "<msg_type>", "ts": "...", "requestId": "...", "payload": {}}
```

**Client request types:**
- `status_request` -- returns device state, session info, sample rate, and active targets.
- `sessions_request` -- lists recent sessions (`payload.limit` defaults to 20, max 100).
- `history_request` -- streams history chunks (`sinceTs`, `untilTs`, `limit`, `sessionId`, `chunkSize`).
- `session_start_request` -- starts a new session (requires authorisation). Accepts optional `targets` array and `deviceAddress`.
- `session_end_request` -- ends the current session (requires authorisation).
- `target_update_request` -- updates targets for the current session (requires authorisation).

**Server response types:**
- `status` -- response to `status_request` with device state, session info, sample rate, and active targets.
- `sessions_list` -- response to `sessions_request` with recent session summaries.
- `history_chunk` / `history_end` -- streamed response to `history_request`.
- `session_start_ack` / `session_end_ack` / `target_update_ack` -- acknowledgements for session control requests.

**Server broadcast types:**
- `reading` -- pushed on each poll cycle with latest probe data.
- `session_start` / `session_end` -- broadcast when sessions change.
- `target_approaching` -- probe temperature crossed the pre-alert threshold.
- `target_reached` -- probe temperature hit the target.
- `target_exceeded` -- probe temperature went above the target.
- `target_reminder` -- periodic nudge while temperature remains above target.

Note: `curl` does not support WebSockets. Use a client like `websocat` or `wscat`, or an iOS `URLSessionWebSocketTask`.

## BLE Host Requirements
- Host must run BlueZ; mount `/run/dbus` into the container and set `DBUS_SYSTEM_BUS_ADDRESS=unix:path=/run/dbus/system_bus_socket`.
- The container must be able to access the host Bluetooth adapter (run as root in Docker by default).
- BLE devices accept only one connection at a time; disconnect the mobile app before connecting the server.
