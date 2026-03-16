"""WebSocket hub, client, and handler for the iGrill Remote v2 protocol."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List

from aiohttp import web

from service.api.envelope import (
    PROTOCOL_VERSION,
    make_envelope,
    send_envelope,
    send_error,
)
from service.alerts.evaluator import AlertEvaluator
from service.config import Config
from service.history.store import HistoryStore, now_iso_utc
from service.models.device import DeviceStore
from service.models.session import TargetConfig

LOG = logging.getLogger("igrill")


# ---------------------------------------------------------------------------
# WebSocketClient — per-connection queue and sender task
# ---------------------------------------------------------------------------


class WebSocketClient:
    """Wraps a WebSocket connection with a bounded outgoing queue."""

    def __init__(self, ws: web.WebSocketResponse, queue_size: int = 1) -> None:
        self.ws = ws
        self.queue: asyncio.Queue[Dict[str, object]] = asyncio.Queue(maxsize=queue_size)
        self.task = asyncio.create_task(self._sender())

    async def _sender(self) -> None:
        while True:
            message = await self.queue.get()
            if self.ws.closed:
                break
            await self.ws.send_json(message)

    def enqueue(self, message: Dict[str, object], critical: bool = False) -> None:
        """Enqueue a message. Drops oldest non-critical messages when full."""
        if self.queue.full():
            while not self.queue.empty():
                try:
                    self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
        if not self.queue.full() or critical:
            self.queue.put_nowait(message)

    async def close(self) -> None:
        if not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


# ---------------------------------------------------------------------------
# WebSocketHub — manages connected clients and broadcasting
# ---------------------------------------------------------------------------


class WebSocketHub:
    """Registry of active WebSocket clients with broadcast support."""

    def __init__(self) -> None:
        self.clients: set[WebSocketClient] = set()

    def add(self, client: WebSocketClient) -> None:
        self.clients.add(client)

    async def remove(self, client: WebSocketClient) -> None:
        if client in self.clients:
            self.clients.remove(client)
        await client.close()

    def broadcast(self, message: Dict[str, object], critical: bool = False) -> None:
        for client in list(self.clients):
            if client.ws.closed:
                self.clients.discard(client)
                continue
            client.enqueue(message, critical=critical)


# ---------------------------------------------------------------------------
# Authorisation helper
# ---------------------------------------------------------------------------


def is_authorized(request: web.Request) -> bool:
    """Check whether the request carries a valid session token."""
    config: Config = request.app["config"]
    token = config.session_token
    if not token:
        return True
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        header_token = header.split(" ", 1)[1].strip()
    else:
        header_token = header.strip()
    return header_token == token


# ---------------------------------------------------------------------------
# WebSocket request handler
# ---------------------------------------------------------------------------


async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
    """Handle an incoming WebSocket connection (v2 protocol)."""
    store: DeviceStore = request.app["store"]
    history: HistoryStore = request.app["history"]
    hub: WebSocketHub = request.app["hub"]
    evaluator: AlertEvaluator = request.app["evaluator"]
    config: Config = request.app["config"]
    authorized = is_authorized(request)

    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    client = WebSocketClient(ws)
    hub.add(client)

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    await send_error(ws, "invalid_json", "Message must be valid JSON.")
                    continue

                if not isinstance(data, dict):
                    await send_error(ws, "invalid_message", "Message must be a JSON object.")
                    continue

                msg_version = data.get("v")
                if msg_version not in (1, 2, PROTOCOL_VERSION):
                    await send_error(ws, "unsupported_version", "Unsupported message version.")
                    continue

                msg_type = data.get("type")
                request_id = data.get("requestId")
                payload = data.get("payload") or {}

                # -- status_request ----------------------------------------
                if msg_type == "status_request":
                    if not request_id:
                        await send_error(ws, "missing_request_id", "status_request requires requestId.")
                        continue

                    snapshot = await store.snapshot()
                    has_data = any(device.get("last_update") for device in snapshot.values())
                    latest_ts = None
                    if has_data:
                        latest_ts = max(
                            device.get("last_update")
                            for device in snapshot.values()
                            if device.get("last_update")
                        )
                    history_available = await history.has_history()
                    if latest_ts is None and history_available:
                        latest_ts = await history.latest_ts()

                    any_connected = any(device.get("connected") for device in snapshot.values())
                    any_error = any(device.get("error") for device in snapshot.values())

                    if any_error:
                        device_state = "error"
                    elif any_connected and has_data:
                        device_state = "ok"
                    elif any_connected and not has_data:
                        device_state = "warming_up"
                    else:
                        device_state = "offline"

                    session_state = await history.get_session_state()

                    # Fetch active targets for the current session
                    active_targets: list[dict[str, Any]] = []
                    current_sid = session_state.get("current_session_id")
                    if current_sid is not None:
                        targets = await history.get_targets(int(current_sid))
                        active_targets = [t.to_dict() for t in targets]

                    status_payload: dict[str, Any] = {
                        "hasData": has_data,
                        "latestTs": latest_ts,
                        "sampleRateHz": round(1.0 / config.poll_interval, 4),
                        "historyAvailable": history_available,
                        "deviceState": device_state,
                        "currentSessionId": session_state.get("current_session_id"),
                        "currentSessionStartTs": session_state.get("current_session_start_ts"),
                        "lastSessionId": session_state.get("last_session_id"),
                        "sessionTimeoutSeconds": session_state.get("session_timeout_seconds"),
                        "activeTargets": active_targets,
                    }
                    await send_envelope(ws, "status", status_payload, request_id=request_id)

                # -- sessions_request --------------------------------------
                elif msg_type == "sessions_request":
                    if not request_id:
                        await send_error(ws, "missing_request_id", "sessions_request requires requestId.")
                        continue
                    if not isinstance(payload, dict):
                        await send_error(ws, "invalid_payload", "sessions_request payload must be an object.", request_id)
                        continue

                    limit = payload.get("limit", 20)
                    try:
                        limit = int(limit)
                    except (TypeError, ValueError):
                        await send_error(ws, "invalid_payload", "limit must be an integer.", request_id)
                        continue
                    if limit <= 0:
                        limit = 20
                    if limit > 100:
                        limit = 100

                    sessions = await history.list_sessions(limit)
                    await send_envelope(ws, "sessions", {"sessions": sessions}, request_id=request_id)

                # -- history_request ---------------------------------------
                elif msg_type == "history_request":
                    if not request_id:
                        await send_error(ws, "missing_request_id", "history_request requires requestId.")
                        continue
                    if not isinstance(payload, dict):
                        await send_error(ws, "invalid_payload", "history_request payload must be an object.", request_id)
                        continue

                    since_ts = payload.get("sinceTs")
                    until_ts = payload.get("untilTs")
                    limit = payload.get("limit")
                    session_id = payload.get("sessionId")
                    chunk_size = payload.get("chunkSize", 200)

                    try:
                        if limit is not None:
                            limit = int(limit)
                        if session_id is not None:
                            session_id = int(session_id)
                        chunk_size = int(chunk_size)
                    except (TypeError, ValueError):
                        await send_error(
                            ws, "invalid_payload",
                            "limit, sessionId, and chunkSize must be integers.",
                            request_id,
                        )
                        continue

                    if limit is not None and limit <= 0:
                        limit = None
                    if chunk_size <= 0:
                        chunk_size = 200

                    items = await history.get_history_items(since_ts, until_ts, limit, session_id)
                    count = 0
                    latest_ts = None
                    chunk: List[Dict[str, object]] = []
                    for item in items:
                        chunk.append(item)
                        count += 1
                        latest_ts = item.get("ts") or latest_ts
                        if len(chunk) >= chunk_size:
                            await send_envelope(
                                ws, "history_chunk", {"items": chunk}, request_id=request_id,
                            )
                            chunk = []
                    if chunk:
                        await send_envelope(
                            ws, "history_chunk", {"items": chunk}, request_id=request_id,
                        )
                    await send_envelope(
                        ws, "history_end", {"count": count, "latestTs": latest_ts}, request_id=request_id,
                    )

                # -- session_start_request ---------------------------------
                elif msg_type == "session_start_request":
                    if not request_id:
                        await send_error(ws, "missing_request_id", "session_start_request requires requestId.")
                        continue
                    if not authorized:
                        await send_error(
                            ws, "unauthorized", "Not allowed to start a new session",
                            request_id=request_id,
                        )
                        continue

                    # Parse optional targets and device address from payload
                    raw_targets = payload.get("targets", [])
                    device_address = payload.get("deviceAddress")
                    targets: list[TargetConfig] = []
                    try:
                        for raw in raw_targets:
                            targets.append(TargetConfig.from_dict(raw))
                    except (KeyError, TypeError, ValueError) as exc:
                        await send_error(
                            ws, "invalid_payload",
                            f"Invalid target configuration: {exc}",
                            request_id=request_id,
                        )
                        continue

                    now_ts = now_iso_utc()
                    sensor_id = device_address or "all"
                    session_info = await history.force_new_session(now_ts, sensor_id, "user")

                    if session_info.get("end_event"):
                        await store.publish_event(make_envelope("session_end", session_info["end_event"]))
                    await store.publish_event(make_envelope("session_start", session_info["start_event"]))

                    new_session_id = session_info["session_id"]

                    # Save and register targets
                    if targets:
                        await history.save_targets(new_session_id, targets)
                        evaluator.set_targets(new_session_id, targets)

                    # Update device store with new session info
                    snapshot = await store.snapshot()
                    for address in snapshot.keys():
                        await store.upsert(
                            address,
                            session_id=new_session_id,
                            session_start_ts=session_info["session_start_ts"],
                        )

                    response_payload: dict[str, Any] = {
                        "ok": True,
                        "sessionId": new_session_id,
                        "sessionStartTs": session_info["session_start_ts"],
                        "targets": [t.to_dict() for t in targets],
                    }
                    await send_envelope(ws, "session_start_ack", response_payload, request_id=request_id)

                # -- session_end_request -----------------------------------
                elif msg_type == "session_end_request":
                    if not request_id:
                        await send_error(ws, "missing_request_id", "session_end_request requires requestId.")
                        continue
                    if not authorized:
                        await send_error(
                            ws, "unauthorized", "Not allowed to end a session",
                            request_id=request_id,
                        )
                        continue

                    now_ts = now_iso_utc()
                    result = await history.end_current_session(now_ts, "user")

                    if result is None:
                        await send_error(
                            ws, "no_active_session", "No session is currently active.",
                            request_id=request_id,
                        )
                        continue

                    ended_session_id = result["sessionId"]
                    evaluator.clear_session(int(ended_session_id))

                    await store.publish_event(make_envelope("session_end", result))

                    await send_envelope(
                        ws, "session_end_ack",
                        {
                            "ok": True,
                            "sessionId": ended_session_id,
                            "endedAt": result["sessionEndTs"],
                        },
                        request_id=request_id,
                    )

                # -- target_update_request ---------------------------------
                elif msg_type == "target_update_request":
                    if not request_id:
                        await send_error(ws, "missing_request_id", "target_update_request requires requestId.")
                        continue
                    if not authorized:
                        await send_error(
                            ws, "unauthorized", "Not allowed to update targets",
                            request_id=request_id,
                        )
                        continue

                    raw_targets = payload.get("targets", [])
                    targets = []
                    try:
                        for raw in raw_targets:
                            targets.append(TargetConfig.from_dict(raw))
                    except (KeyError, TypeError, ValueError) as exc:
                        await send_error(
                            ws, "invalid_payload",
                            f"Invalid target configuration: {exc}",
                            request_id=request_id,
                        )
                        continue

                    session_state = await history.get_session_state()
                    current_sid = session_state.get("current_session_id")
                    if current_sid is None:
                        await send_error(
                            ws, "no_active_session", "No session is currently active.",
                            request_id=request_id,
                        )
                        continue

                    sid = int(current_sid)
                    await history.update_targets(sid, targets)
                    evaluator.set_targets(sid, targets)

                    await send_envelope(
                        ws, "target_update_ack",
                        {
                            "ok": True,
                            "sessionId": sid,
                            "targets": [t.to_dict() for t in targets],
                        },
                        request_id=request_id,
                    )

                # -- unknown -----------------------------------------------
                else:
                    await send_error(
                        ws, "unknown_type",
                        f"Unsupported message type: {msg_type}",
                        request_id=request_id,
                    )

            elif msg.type == web.WSMsgType.ERROR:
                LOG.debug("WebSocket error: %s", ws.exception())
    finally:
        await hub.remove(client)

    return ws


# ---------------------------------------------------------------------------
# Broadcast coroutines — run as background tasks
# ---------------------------------------------------------------------------


async def broadcast_readings(app: web.Application) -> None:
    """Forward device readings from the store to all connected WebSocket clients."""
    store: DeviceStore = app["store"]
    hub: WebSocketHub = app["hub"]
    while True:
        reading = await store.next_reading()
        message = make_envelope("reading", reading["payload"], seq=reading.get("seq"))
        hub.broadcast(message, critical=False)


async def broadcast_events(app: web.Application) -> None:
    """Forward device events from the store to all connected WebSocket clients."""
    store: DeviceStore = app["store"]
    hub: WebSocketHub = app["hub"]
    while True:
        event = await store.next_event()
        hub.broadcast(event, critical=True)
