"""HTTP route handlers and route setup for the iGrill Remote server."""

from __future__ import annotations

import logging
import time

from aiohttp import web

from service.config import Config
from service.history.store import HistoryStore, now_iso
from service.models.device import DeviceStore

LOG = logging.getLogger("igrill.http")


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


async def metrics_handler(request: web.Request) -> web.Response:
    """Serve Prometheus text format metrics, or fall back to JSON device snapshot."""
    metrics = request.app.get("metrics")
    if metrics:
        return web.Response(text=metrics.render(), content_type="text/plain")
    # Fallback: return device snapshot as JSON (backwards compat)
    store: DeviceStore = request.app["store"]
    snapshot = await store.snapshot()
    return web.json_response(
        {
            "generated_at": now_iso(),
            "device_count": len(snapshot),
            "devices": list(snapshot.values()),
        }
    )


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
    limit = int(request.query.get("limit", "20"))
    offset = int(request.query.get("offset", "0"))
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
    readings = await history.get_session_readings(session_id)
    targets = await history.get_targets(session_id)
    devices = await history.get_session_devices(session_id)
    return web.json_response({
        "sessionId": session_id,
        "devices": devices,
        "targets": [t.to_dict() for t in targets],
        "readings": readings,
    })


async def log_levels_handler(request: web.Request) -> web.Response:
    """PUT /api/config/log-levels — runtime log level update."""
    from service.api.websocket import is_authorized
    if not is_authorized(request):
        return web.json_response({"error": "unauthorised"}, status=401)
    body = await request.json()
    from service.logging_setup import update_log_level
    results = {}
    for logger_name, level in body.items():
        results[logger_name] = update_log_level(logger_name, level)
    return web.json_response({"results": results})


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def setup_routes(app: web.Application) -> None:
    """Register all HTTP and WebSocket routes on *app*."""
    from service.api.websocket import websocket_handler

    app.router.add_get("/metrics", metrics_handler)
    app.router.add_get("/health", health_handler)
    app.router.add_get("/ws", websocket_handler)
    app.router.add_get("/api/sessions", sessions_handler)
    app.router.add_get("/api/sessions/{id}", session_detail_handler)
    app.router.add_put("/api/config/log-levels", log_levels_handler)
