"""HTTP route handlers and route setup for the iGrill Remote server."""

from __future__ import annotations

import csv
import io
import logging
import time

from aiohttp import web

from service.config import Config
from service.history.store import HistoryStore
from service.models.device import DeviceStore

LOG = logging.getLogger("igrill.http")


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


async def health_handler(request: web.Request) -> web.Response:
    """Lightweight health check endpoint."""
    store: DeviceStore = request.app["store"]
    history: HistoryStore = request.app["history"]
    devices = await store.snapshot()
    connected = sum(1 for d in devices.values() if d.get("connected"))
    session_state = await history.get_session_state()
    config: Config = request.app["config"]
    return web.json_response(
        {
            "status": "ok",
            "uptime_seconds": time.monotonic() - request.app["start_time"],
            "devices_total": len(devices),
            "devices_connected": connected,
            "active_session_id": session_state.get("current_session_id"),
            "ble_adapter": "bluez",
            "poll_interval": config.poll_interval,
            "scan_interval": config.scan_interval,
        }
    )


async def sessions_handler(request: web.Request) -> web.Response:
    """GET /api/sessions — paginated session list."""
    history: HistoryStore = request.app["history"]
    try:
        limit = int(request.query.get("limit", "20"))
        offset = int(request.query.get("offset", "0"))
    except (TypeError, ValueError):
        return web.json_response(
            {"error": "limit and offset must be integers"}, status=400,
        )
    if limit <= 0:
        limit = 20
    if limit > 100:
        limit = 100
    if offset < 0:
        offset = 0
    sessions = await history.list_sessions(limit=limit, offset=offset)
    return web.json_response({"sessions": sessions})


async def session_detail_handler(request: web.Request) -> web.Response:
    """GET /api/sessions/{id} — session detail with readings."""
    history: HistoryStore = request.app["history"]
    session_id = request.match_info["id"]
    if not await history.session_exists(session_id):
        return web.json_response({"error": "session not found"}, status=404)
    readings = await history.get_session_readings(session_id)
    targets = await history.get_targets(session_id)
    devices = await history.get_session_devices(session_id)
    name = await history.get_session_name(session_id)
    notes = await history.get_session_notes(session_id)
    return web.json_response({
        "sessionId": session_id,
        "name": name,
        "notes": notes,
        "devices": devices,
        "targets": [t.to_dict() for t in targets],
        "readings": readings,
    })


async def export_handler(request: web.Request) -> web.Response:
    """GET /api/sessions/{id}/export — download session data as CSV or JSON."""
    history: HistoryStore = request.app["history"]
    session_id = request.match_info["id"]
    if not await history.session_exists(session_id):
        return web.json_response({"error": "session not found"}, status=404)

    readings = await history.get_session_readings(session_id)
    targets = await history.get_targets(session_id)
    name = await history.get_session_name(session_id)

    # Build label lookup from targets
    label_by_probe: dict[int, str] = {}
    for t in targets:
        if t.label:
            label_by_probe[t.probe_index] = t.label

    fmt = request.query.get("format", "json").lower()
    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["timestamp", "probe_index", "label", "temperature_c", "battery_pct", "propane_pct"])
        for r in readings:
            ts = r.get("recorded_at", "")
            battery = r.get("battery")
            propane = r.get("propane")
            probes = r.get("probes", [])
            for p in probes:
                idx = p.get("index", 0)
                temp = p.get("temperature")
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

    # Default: enriched JSON
    for r in readings:
        for p in r.get("probes", []):
            idx = p.get("index", 0)
            p["label"] = label_by_probe.get(idx)
    return web.json_response({
        "sessionId": session_id,
        "name": name,
        "readings": readings,
    })


async def log_levels_handler(request: web.Request) -> web.Response:
    """PUT /api/config/log-levels — runtime log level update."""
    from service.api.websocket import is_authorized
    if not is_authorized(request):
        return web.json_response({"error": "unauthorised"}, status=401)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be a JSON object"}, status=400)
    from service.logging_setup import update_log_level
    results = {}
    for logger_name, level in body.items():
        if not isinstance(level, str):
            results[logger_name] = False
            continue
        results[logger_name] = update_log_level(logger_name, level)
    return web.json_response({"results": results})


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def setup_routes(app: web.Application) -> None:
    """Register all HTTP and WebSocket routes on *app*."""
    from service.api.websocket import websocket_handler

    app.router.add_get("/health", health_handler)
    app.router.add_get("/ws", websocket_handler)
    app.router.add_get("/api/sessions", sessions_handler)
    app.router.add_get("/api/sessions/{id}", session_detail_handler)
    app.router.add_get("/api/sessions/{id}/export", export_handler)
    app.router.add_put("/api/config/log-levels", log_levels_handler)
