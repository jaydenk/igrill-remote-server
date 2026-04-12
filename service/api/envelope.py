"""WebSocket message envelope construction."""

from __future__ import annotations

from typing import Any, Optional

from aiohttp import web

from service.history.store import now_iso_utc

PROTOCOL_VERSION = 2

# ---------------------------------------------------------------------------
# Message type constants
#
# These mirror the string literals used throughout the WebSocket router and
# broadcast code.  New session-first redesign types should be added here so
# there is one canonical place to look up every valid envelope ``type``.
# Handlers for the new types are wired up in Tasks 9-11.
# ---------------------------------------------------------------------------

# --- Existing request types (client -> server) ---
TYPE_STATUS_REQUEST = "status_request"
TYPE_SESSIONS_REQUEST = "sessions_request"
TYPE_HISTORY_REQUEST = "history_request"
TYPE_SESSION_START_REQUEST = "session_start_request"
TYPE_SESSION_END_REQUEST = "session_end_request"
TYPE_SESSION_UPDATE_REQUEST = "session_update_request"
TYPE_SESSION_ADD_DEVICE_REQUEST = "session_add_device_request"
TYPE_TARGET_UPDATE_REQUEST = "target_update_request"

# --- New request types for the session-first redesign ---
TYPE_SESSION_DISCARD_REQUEST = "session_discard_request"
TYPE_PROBE_TIMER_REQUEST = "probe_timer_request"
TYPE_SESSION_NOTES_UPDATE_REQUEST = "session_notes_update_request"

# --- Existing response / broadcast types (server -> client) ---
TYPE_STATUS = "status"
TYPE_SESSIONS = "sessions"
TYPE_HISTORY_CHUNK = "history_chunk"
TYPE_HISTORY_END = "history_end"
TYPE_READING = "reading"
TYPE_SESSION_START = "session_start"
TYPE_SESSION_END = "session_end"
TYPE_DEVICE_JOINED = "device_joined"
TYPE_SESSION_START_ACK = "session_start_ack"
TYPE_SESSION_END_ACK = "session_end_ack"
TYPE_SESSION_UPDATE_ACK = "session_update_ack"
TYPE_SESSION_ADD_DEVICE_ACK = "session_add_device_ack"
TYPE_TARGET_UPDATE_ACK = "target_update_ack"
TYPE_ERROR = "error"

# --- New response / broadcast types for the session-first redesign ---
TYPE_SESSION_DISCARD_ACK = "session_discard_ack"
TYPE_SESSION_DISCARDED = "session_discarded"  # broadcast
TYPE_PROBE_TIMER_ACK = "probe_timer_ack"
TYPE_PROBE_TIMER_UPDATE = "probe_timer_update"  # broadcast
TYPE_SESSION_NOTES_UPDATE_ACK = "session_notes_update_ack"
TYPE_SESSION_NOTES_UPDATE = "session_notes_update"  # broadcast


def make_envelope(
    msg_type: str,
    payload: dict[str, Any],
    request_id: str | None = None,
    seq: int | None = None,
) -> dict[str, Any]:
    """Build a versioned message envelope."""
    env: dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "type": msg_type,
        "ts": now_iso_utc(),
        "payload": payload,
    }
    if request_id is not None:
        env["requestId"] = request_id
    if seq is not None:
        env["seq"] = seq
    return env


async def send_envelope(
    ws: web.WebSocketResponse,
    msg_type: str,
    payload: dict[str, Any],
    request_id: str | None = None,
    seq: int | None = None,
) -> None:
    """Send a JSON envelope over *ws*."""
    await ws.send_json(make_envelope(msg_type, payload, request_id=request_id, seq=seq))


async def send_error(
    ws: web.WebSocketResponse,
    code: str,
    message: str,
    request_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Send an error envelope over *ws*."""
    payload: dict[str, Any] = {"code": code, "message": message}
    if details:
        payload["details"] = details
    await send_envelope(ws, "error", payload, request_id=request_id)
