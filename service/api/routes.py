"""HTTP route handlers and route setup for the iGrill Remote server."""

from __future__ import annotations

import logging
import time

from aiohttp import web

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


async def history_handler(request: web.Request) -> web.Response:
    """Return session history, optionally filtered by MAC address."""
    history: HistoryStore = request.app["history"]
    address = request.query.get("mac")
    sessions = await history.get_history(address)
    return web.json_response(
        {
            "generated_at": now_iso(),
            "session_count": len(sessions),
            "sessions": sessions,
        }
    )


async def health_handler(request: web.Request) -> web.Response:
    """Lightweight health check endpoint."""
    store: DeviceStore = request.app["store"]
    history: HistoryStore = request.app["history"]
    devices = await store.snapshot()
    connected = sum(1 for d in devices.values() if d.get("connected"))
    session_state = await history.get_session_state()
    return web.json_response(
        {
            "status": "ok",
            "uptime_seconds": time.monotonic() - request.app["start_time"],
            "devices_total": len(devices),
            "devices_connected": connected,
            "active_session_id": session_state.get("current_session_id"),
            "ble_adapter": "bluez",
        }
    )


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def setup_routes(app: web.Application) -> None:
    """Register all HTTP and WebSocket routes on *app*."""
    from service.api.websocket import websocket_handler

    app.router.add_get("/metrics", metrics_handler)
    app.router.add_get("/history", history_handler)
    app.router.add_get("/health", health_handler)
    app.router.add_get("/ws", websocket_handler)
