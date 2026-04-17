"""HTTP route handlers and route setup for the iGrill Remote server."""

from __future__ import annotations

import csv
import io
import logging
import re
import time
from typing import Any, Optional

from aiohttp import web

from service.config import Config
from service.history.store import HistoryStore
from service.models.device import DeviceStore

LOG = logging.getLogger("igrill.http")


# APNS device tokens are 64 hexadecimal characters. Live Activity tokens are
# longer (typically 160 hex chars) — both are validated with this pattern.
_PUSH_TOKEN_RE = re.compile(r"^[0-9a-fA-F]{32,200}$")


# ---------------------------------------------------------------------------
# Camel-case serialisation helpers
# ---------------------------------------------------------------------------


def _timer_to_camel(row: dict) -> dict:
    """Convert a ``session_timers`` dict to the camelCased API shape."""
    return {
        "address": row.get("address"),
        "probeIndex": row.get("probe_index"),
        "mode": row.get("mode"),
        "durationSecs": row.get("duration_secs"),
        "startedAt": row.get("started_at"),
        "pausedAt": row.get("paused_at"),
        "accumulatedSecs": row.get("accumulated_secs"),
        "completedAt": row.get("completed_at"),
    }


def _note_to_camel(row: dict) -> dict:
    """Convert a ``session_notes`` dict to the camelCased API shape."""
    return {
        "id": row.get("id"),
        "createdAt": row.get("created_at"),
        "updatedAt": row.get("updated_at"),
        "body": row.get("body"),
    }


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
    from service.api.websocket import is_authorized
    if not is_authorized(request):
        return web.json_response({"error": "unauthorised"}, status=401)
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
    from service.api.websocket import is_authorized
    if not is_authorized(request):
        return web.json_response({"error": "unauthorised"}, status=401)
    history: HistoryStore = request.app["history"]
    session_id = request.match_info["id"]
    if not await history.session_exists(session_id):
        return web.json_response({"error": "session not found"}, status=404)
    readings = await history.get_session_readings(session_id)
    targets = await history.get_targets(session_id)
    devices = await history.get_session_devices(session_id)
    meta = await history.get_session_metadata(session_id)
    name = meta["name"] if meta else None
    notes_body = meta["notes"] if meta else None
    target_duration_secs = meta["target_duration_secs"] if meta else None
    timers = await history.get_timers(session_id)
    notes = await history.get_notes(session_id)
    return web.json_response({
        "sessionId": session_id,
        "name": name,
        # Legacy string form of the primary note, dual-written from
        # ``sessions.notes``.  Retained under a new key so the richer
        # ``notes: [...]`` array below can own the ``notes`` field.
        "notesBody": notes_body,
        "targetDurationSecs": target_duration_secs,
        "devices": devices,
        "targets": [t.to_dict() for t in targets],
        "readings": readings,
        "timers": [_timer_to_camel(t) for t in timers],
        "notes": [_note_to_camel(n) for n in notes],
    })


async def export_handler(request: web.Request) -> web.Response:
    """GET /api/sessions/{id}/export — download session data as CSV or JSON.

    CSV exports default to ``resource=readings`` (backwards-compatible single
    CSV of probe readings).  Pass ``?resource=timers`` or ``?resource=notes``
    to download a CSV of the session's timers or notes respectively.
    JSON exports always include the full bundle (readings, timers, notes).
    """
    from service.api.websocket import is_authorized
    if not is_authorized(request):
        return web.json_response({"error": "unauthorised"}, status=401)
    history: HistoryStore = request.app["history"]
    session_id = request.match_info["id"]
    if not await history.session_exists(session_id):
        return web.json_response({"error": "session not found"}, status=404)

    readings = await history.get_session_readings(session_id)
    targets = await history.get_targets(session_id)
    meta = await history.get_session_metadata(session_id)
    name = meta["name"] if meta else None
    notes_body = meta["notes"] if meta else None
    target_duration_secs = meta["target_duration_secs"] if meta else None
    timers = await history.get_timers(session_id)
    notes = await history.get_notes(session_id)

    # Build label lookup from targets
    label_by_probe: dict[int, str] = {}
    for t in targets:
        if t.label:
            label_by_probe[t.probe_index] = t.label

    fmt = request.query.get("format", "json").lower()
    if fmt == "csv":
        resource = request.query.get("resource", "readings").lower()
        safe_name = (name or session_id).replace('"', "'")
        buf = io.StringIO()
        writer = csv.writer(buf)

        if resource == "timers":
            writer.writerow([
                "address", "probe_index", "mode", "duration_secs",
                "started_at", "paused_at", "accumulated_secs", "completed_at",
            ])
            for t in timers:
                writer.writerow([
                    t.get("address", ""),
                    t.get("probe_index", ""),
                    t.get("mode", ""),
                    t.get("duration_secs") if t.get("duration_secs") is not None else "",
                    t.get("started_at") or "",
                    t.get("paused_at") or "",
                    t.get("accumulated_secs") if t.get("accumulated_secs") is not None else "",
                    t.get("completed_at") or "",
                ])
            filename = f"{safe_name}-timers.csv"
        elif resource == "notes":
            writer.writerow(["id", "created_at", "updated_at", "body"])
            for n in notes:
                writer.writerow([
                    n.get("id", ""),
                    n.get("created_at", ""),
                    n.get("updated_at", ""),
                    n.get("body", ""),
                ])
            filename = f"{safe_name}-notes.csv"
        elif resource == "readings":
            writer.writerow([
                "timestamp", "probe_index", "label", "temperature_c",
                "battery_pct", "propane_pct",
            ])
            for r in readings:
                ts = r.get("recorded_at", "")
                battery = r.get("battery")
                propane = r.get("propane")
                idx = r.get("probe_index", 0)
                temp = r.get("temperature")
                label = label_by_probe.get(idx, "")
                writer.writerow([ts, idx, label, temp, battery, propane])
            filename = f"{safe_name}.csv"
        else:
            return web.json_response(
                {"error": f"unknown resource: {resource!r}"}, status=400,
            )

        return web.Response(
            text=buf.getvalue(),
            content_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )

    # Default: enriched JSON — full bundle (readings + timers + notes).
    for r in readings:
        idx = r.get("probe_index", 0)
        r["label"] = label_by_probe.get(idx)
    return web.json_response({
        "sessionId": session_id,
        "name": name,
        "notesBody": notes_body,
        "targetDurationSecs": target_duration_secs,
        "readings": readings,
        "timers": [_timer_to_camel(t) for t in timers],
        "notes": [_note_to_camel(n) for n in notes],
    })


async def push_token_handler(request: web.Request) -> web.Response:
    """POST /api/v1/devices/push-token — register or update a push token.

    Auth, validation, and rate limiting were missing historically: any LAN
    client could stuff the push token table with junk strings, and every
    subsequent alert would then spend serial APNS round trips before
    those tokens got evicted as bad. Gate on the shared session token,
    validate the token string shape, and cap at 10 requests/minute/peer.
    """
    from service.api.websocket import is_authorized, _RateLimiter

    if not is_authorized(request):
        return web.json_response({"error": "unauthorised"}, status=401)

    limiter: "_RateLimiter" = request.app["push_token_limiter"]
    peer = request.remote or "unknown"
    if not limiter.allow(peer):
        return web.json_response({"error": "rate limited"}, status=429)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)

    token = body.get("token")
    if not token or not isinstance(token, str):
        return web.json_response({"error": "token is required"}, status=400)
    if not _PUSH_TOKEN_RE.match(token):
        return web.json_response(
            {"error": "token must be a hex string"}, status=400,
        )

    la_token = body.get("liveActivityToken")
    if la_token is not None:
        if not isinstance(la_token, str) or not _PUSH_TOKEN_RE.match(la_token):
            return web.json_response(
                {"error": "liveActivityToken must be a hex string"}, status=400,
            )

    push_service = request.app.get("push_service")
    if push_service:
        await push_service.upsert_token(token, live_activity_token=la_token)

    return web.json_response({"ok": True})


async def push_test_handler(request: web.Request) -> web.Response:
    """POST /api/v1/push/test — send a test push to all registered tokens."""
    from service.api.websocket import is_authorized
    if not is_authorized(request):
        return web.json_response({"error": "unauthorised"}, status=401)

    push_service = request.app.get("push_service")
    if not push_service:
        return web.json_response({"error": "push service not available"}, status=503)

    result = await push_service.send_test()
    if "error" in result:
        return web.json_response(result, status=400)
    return web.json_response(result)


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


async def simulate_start_handler(request: web.Request) -> web.Response:
    """POST /api/v1/simulate/start — start a simulated cook session."""
    from service.api.websocket import is_authorized
    if not is_authorized(request):
        return web.json_response({"error": "unauthorised"}, status=401)

    simulator = request.app.get("simulator")
    if not simulator:
        return web.json_response({"error": "simulator not available"}, status=503)

    try:
        body = await request.json()
    except Exception:
        body = {}

    speed = body.get("speed", 10)
    probes = body.get("probes", 4)
    probe_timers_raw = body.get("probe_timers")

    try:
        speed = float(speed)
        probes = int(probes)
    except (TypeError, ValueError):
        return web.json_response({"error": "speed must be a number, probes must be an integer"}, status=400)

    probe_timers: Optional[dict[int, dict[str, Any]]] = None
    if probe_timers_raw is not None:
        if not isinstance(probe_timers_raw, dict):
            return web.json_response(
                {"error": "probe_timers must be an object keyed by probe index"},
                status=400,
            )
        probe_timers = {}
        for key, spec in probe_timers_raw.items():
            try:
                probe_timers[int(key)] = spec
            except (TypeError, ValueError):
                return web.json_response(
                    {"error": f"probe_timers key {key!r} is not an integer"},
                    status=400,
                )

    result = await simulator.start(speed=speed, probes=probes, probe_timers=probe_timers)
    if "error" in result:
        return web.json_response(result, status=409)
    return web.json_response(result)


async def simulate_stop_handler(request: web.Request) -> web.Response:
    """POST /api/v1/simulate/stop — stop the running simulation."""
    from service.api.websocket import is_authorized
    if not is_authorized(request):
        return web.json_response({"error": "unauthorised"}, status=401)

    simulator = request.app.get("simulator")
    if not simulator:
        return web.json_response({"error": "simulator not available"}, status=503)

    result = await simulator.stop()
    if "error" in result:
        return web.json_response(result, status=400)
    return web.json_response(result)


async def simulate_probe_timer_handler(request: web.Request) -> web.Response:
    """POST /api/v1/simulate/probe-timer — drive a simulated session's
    per-probe timer through the same dispatch path the real device uses.

    Body: { "probe_index": int, "action": str,
            "mode": str?, "duration_secs": int? }
    """
    from service.api.websocket import is_authorized
    from service.api.envelope import make_envelope
    from service.simulate.runner import SIM_ADDRESS

    if not is_authorized(request):
        return web.json_response({"error": "unauthorised"}, status=401)

    history = request.app.get("history")
    store = request.app.get("store")
    if history is None or store is None:
        return web.json_response(
            {"error": "history store not available"}, status=503,
        )

    try:
        body = await request.json()
    except Exception:
        body = {}

    probe_index = body.get("probe_index")
    action = body.get("action")
    # isinstance(x, int) returns True for bool too — reject explicitly so
    # "probe_index": true doesn't persist as probe 1.
    if not isinstance(probe_index, int) or isinstance(probe_index, bool):
        return web.json_response(
            {"error": "probe_index must be an integer"}, status=400,
        )
    if not isinstance(action, str):
        return web.json_response(
            {"error": "action is required"}, status=400,
        )

    session_state = await history.get_session_state()
    current_sid = session_state.get("current_session_id")
    if current_sid is None:
        return web.json_response({"error": "no active session"}, status=409)

    try:
        if action == "upsert":
            mode = body.get("mode")
            duration_secs = body.get("duration_secs")
            if mode == "count_down" and duration_secs is None:
                return web.json_response(
                    {"error": "duration_secs required for count_down"},
                    status=400,
                )
            row = await history.upsert_timer(
                current_sid, SIM_ADDRESS, probe_index, mode, duration_secs,
            )
        elif action == "start":
            row = await history.start_timer(current_sid, SIM_ADDRESS, probe_index)
        elif action == "pause":
            row = await history.pause_timer(current_sid, SIM_ADDRESS, probe_index)
        elif action == "resume":
            row = await history.resume_timer(current_sid, SIM_ADDRESS, probe_index)
        elif action == "reset":
            row = await history.reset_timer(current_sid, SIM_ADDRESS, probe_index)
        else:
            return web.json_response(
                {"error": f"unsupported action: {action}"}, status=400,
            )
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)

    await store.publish_event(make_envelope("probe_timer_update", row))
    return web.json_response({"ok": True, "row": row})


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def setup_routes(app: web.Application) -> None:
    """Register all HTTP and WebSocket routes on *app*."""
    from service.api.websocket import websocket_handler, _RateLimiter

    # Per-app rate limiter for push-token registration. Lives on the app
    # instance so test clients get isolated state.
    app["push_token_limiter"] = _RateLimiter(max_requests=10, window_seconds=60)

    app.router.add_get("/health", health_handler)
    app.router.add_get("/ws", websocket_handler)
    app.router.add_get("/api/sessions", sessions_handler)
    app.router.add_get("/api/sessions/{id}", session_detail_handler)
    app.router.add_get("/api/sessions/{id}/export", export_handler)
    app.router.add_post("/api/v1/devices/push-token", push_token_handler)
    app.router.add_post("/api/v1/push/test", push_test_handler)
    app.router.add_put("/api/config/log-levels", log_levels_handler)
    app.router.add_post("/api/v1/simulate/start", simulate_start_handler)
    app.router.add_post("/api/v1/simulate/stop", simulate_stop_handler)
    app.router.add_post(
        "/api/v1/simulate/probe-timer", simulate_probe_timer_handler
    )
