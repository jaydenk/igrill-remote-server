"""WebSocket message envelope construction."""

from __future__ import annotations

from typing import Any, Optional

from aiohttp import web

from service.history.store import now_iso_utc

PROTOCOL_VERSION = 2


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
