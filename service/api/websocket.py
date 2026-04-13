"""WebSocket hub, client, and handler for the iGrill Remote v2 protocol."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

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
from service.simulate.runner import SimulationRunner

LOG = logging.getLogger("igrill.ws")

_SESSION_CONTROL_TYPES = frozenset({
    "session_start_request",
    "session_end_request",
    "session_add_device_request",
    "session_update_request",
    "target_update_request",
    # Session-first redesign request types (handlers wired in Tasks 9-11).
    # Listed here so they share the session-control rate limit even before
    # their handlers exist; unknown types still fall through to "unknown_type".
    "session_discard_request",
    "probe_timer_request",
    "session_notes_update_request",
})


# ---------------------------------------------------------------------------
# Simple per-peer rate limiter
# ---------------------------------------------------------------------------


class _RateLimiter:
    """Sliding-window rate limiter keyed by an arbitrary string (e.g. peer IP)."""

    _MAX_TRACKED_KEYS = 256
    _SWEEP_INTERVAL = 300.0  # seconds between full eviction sweeps

    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self._max = max_requests
        self._window = window_seconds
        self._requests: dict[str, list[float]] = {}
        self._last_sweep: float = 0.0

    def allow(self, key: str) -> bool:
        now = time.monotonic()

        # Periodic sweep: remove all keys with only expired entries
        if now - self._last_sweep > self._SWEEP_INTERVAL:
            self._sweep(now)
            self._last_sweep = now

        entries = self._requests.get(key, [])
        entries = [t for t in entries if now - t < self._window]
        if not entries:
            self._requests.pop(key, None)
        if len(entries) >= self._max:
            self._requests[key] = entries
            return False
        entries.append(now)
        self._requests[key] = entries
        return True

    def _sweep(self, now: float) -> None:
        """Remove keys whose entries have all expired."""
        expired_keys = [
            k for k, v in self._requests.items()
            if not any(now - t < self._window for t in v)
        ]
        for k in expired_keys:
            del self._requests[k]


# Rate limit session-control messages: 10 per 60 seconds per peer
_session_limiter = _RateLimiter(max_requests=10, window_seconds=60)


# ---------------------------------------------------------------------------
# WebSocketClient — per-connection queues and sender task
# ---------------------------------------------------------------------------


class WebSocketClient:
    """Wraps a WebSocket connection with separate queues for readings and
    critical events, ensuring events are never dropped by reading back-pressure."""

    def __init__(self, ws: web.WebSocketResponse) -> None:
        self.ws = ws
        self._reading_queue: asyncio.Queue[Dict[str, object]] = asyncio.Queue(maxsize=1)
        self._event_queue: asyncio.Queue[Dict[str, object]] = asyncio.Queue(maxsize=64)
        self.task = asyncio.create_task(self._sender())

    async def _sender(self) -> None:
        while True:
            # Always drain events first (priority); fall back to readings.
            try:
                message = self._event_queue.get_nowait()
            except asyncio.QueueEmpty:
                try:
                    message = self._reading_queue.get_nowait()
                except asyncio.QueueEmpty:
                    # Both empty — wait on event queue with a short timeout,
                    # then loop to check reading queue.
                    try:
                        message = await asyncio.wait_for(
                            self._event_queue.get(), timeout=0.1,
                        )
                    except asyncio.TimeoutError:
                        continue

            if self.ws.closed:
                break
            try:
                await self.ws.send_json(message)
            except (ConnectionError, OSError):
                break

    def enqueue(self, message: Dict[str, object], critical: bool = False) -> None:
        """Enqueue a message.  Non-critical messages replace the previous
        reading when the reading queue is full.  Critical messages are
        buffered in a larger queue."""
        if critical:
            if self._event_queue.full():
                try:
                    evicted = self._event_queue.get_nowait()
                    LOG.warning(
                        "Event queue full — evicted message type=%s to enqueue type=%s",
                        evicted.get("type", "unknown"),
                        message.get("type", "unknown"),
                    )
                except asyncio.QueueEmpty:
                    pass
            self._event_queue.put_nowait(message)
        else:
            if self._reading_queue.full():
                try:
                    self._reading_queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            self._reading_queue.put_nowait(message)

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
                asyncio.create_task(client.close())
                continue
            client.enqueue(message, critical=critical)


# ---------------------------------------------------------------------------
# Authorisation helper
# ---------------------------------------------------------------------------


def is_authorized(request: web.Request) -> bool:
    """Check whether the request carries a valid Bearer token."""
    config: Config = request.app["config"]
    token = config.session_token
    if not token:
        return True
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return False
    return header.split(" ", 1)[1].strip() == token


# ---------------------------------------------------------------------------
# Message handler context and dispatch
# ---------------------------------------------------------------------------


@dataclass
class _MessageContext:
    """Bundles all state needed by individual message handlers."""

    ws: web.WebSocketResponse
    store: DeviceStore
    history: HistoryStore
    evaluator: AlertEvaluator
    config: Config
    simulator: Optional[SimulationRunner]
    authorized: bool
    peer: str
    request_id: Optional[str]
    payload: dict


async def _handle_status(ctx: _MessageContext) -> None:
    snapshot = await ctx.store.snapshot()
    has_data = any(device.get("last_update") for device in snapshot.values())
    latest_ts = None
    if has_data:
        latest_ts = max(
            device.get("last_update")
            for device in snapshot.values()
            if device.get("last_update")
        )
    history_available = await ctx.history.has_history()
    if latest_ts is None and history_available:
        latest_ts = await ctx.history.latest_ts()

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

    session_state = await ctx.history.get_session_state()

    active_targets: list[dict[str, Any]] = []
    session_devices: list[dict[str, Any]] = []
    current_sid = session_state.get("current_session_id")
    if current_sid is not None:
        targets = await ctx.history.get_targets(current_sid)
        active_targets = [t.to_dict() for t in targets]
        session_devices = await ctx.history.get_session_devices(current_sid)

    status_payload: dict[str, Any] = {
        "hasData": has_data,
        "latestTs": latest_ts,
        "sampleRateHz": round(1.0 / ctx.config.poll_interval, 4),
        "historyAvailable": history_available,
        "deviceState": device_state,
        "currentSessionId": session_state.get("current_session_id"),
        "currentSessionStartTs": session_state.get("current_session_start_ts"),
        "lastSessionId": session_state.get("last_session_id"),
        "sessionTimeoutSeconds": session_state.get("session_timeout_seconds"),
        "activeTargets": active_targets,
        "sessionDevices": session_devices,
    }
    if current_sid is not None:
        meta = await ctx.history.get_session_metadata(current_sid)
        status_payload["currentSessionName"] = meta["name"] if meta else None
        status_payload["currentTargetDurationSecs"] = (
            meta["target_duration_secs"] if meta else None
        )
    LOG.info("WS send status to %s: deviceState=%s hasData=%s sessionId=%s",
             ctx.peer, device_state, has_data, session_state.get("current_session_id"))
    await send_envelope(ctx.ws, "status", status_payload, request_id=ctx.request_id)


async def _handle_sessions(ctx: _MessageContext) -> None:
    if not isinstance(ctx.payload, dict):
        await send_error(ctx.ws, "invalid_payload", "sessions_request payload must be an object.", ctx.request_id)
        return

    limit = ctx.payload.get("limit", 20)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        await send_error(ctx.ws, "invalid_payload", "limit must be an integer.", ctx.request_id)
        return
    if limit <= 0:
        limit = 20
    if limit > 100:
        limit = 100

    offset = ctx.payload.get("offset", 0)
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        await send_error(ctx.ws, "invalid_payload", "offset must be an integer.", ctx.request_id)
        return
    if offset < 0:
        offset = 0

    sessions = await ctx.history.list_sessions(limit, offset=offset)
    await send_envelope(ctx.ws, "sessions", {"sessions": sessions}, request_id=ctx.request_id)


async def _handle_history(ctx: _MessageContext) -> None:
    if not isinstance(ctx.payload, dict):
        await send_error(ctx.ws, "invalid_payload", "history_request payload must be an object.", ctx.request_id)
        return

    since_ts = ctx.payload.get("sinceTs")
    until_ts = ctx.payload.get("untilTs")
    limit = ctx.payload.get("limit")
    session_id = ctx.payload.get("sessionId")
    chunk_size = ctx.payload.get("chunkSize", 200)

    try:
        if limit is not None:
            limit = int(limit)
        if session_id is not None:
            session_id = str(session_id)
        chunk_size = int(chunk_size)
    except (TypeError, ValueError):
        await send_error(
            ctx.ws, "invalid_payload",
            "limit and chunkSize must be integers.",
            ctx.request_id,
        )
        return

    if limit is not None and limit <= 0:
        limit = None
    if limit is None:
        limit = 10000
    if limit > 10000:
        limit = 10000
    if chunk_size <= 0:
        chunk_size = 200

    items = await ctx.history.get_history_items(since_ts, until_ts, limit, session_id)
    count = 0
    latest_ts = None
    chunk: List[Dict[str, object]] = []
    for item in items:
        chunk.append(item)
        count += 1
        latest_ts = item.get("recorded_at") or latest_ts
        if len(chunk) >= chunk_size:
            await send_envelope(
                ctx.ws, "history_chunk", {"items": chunk}, request_id=ctx.request_id,
            )
            chunk = []
    if chunk:
        await send_envelope(
            ctx.ws, "history_chunk", {"items": chunk}, request_id=ctx.request_id,
        )
    await send_envelope(
        ctx.ws, "history_end", {"count": count, "latestTs": latest_ts}, request_id=ctx.request_id,
    )


async def _handle_session_start(ctx: _MessageContext) -> None:
    if not ctx.authorized:
        await send_error(
            ctx.ws, "unauthorized", "Not allowed to start a new session",
            request_id=ctx.request_id,
        )
        return

    raw_targets = ctx.payload.get("targets", [])
    targets: list[TargetConfig] = []
    try:
        for raw in raw_targets:
            targets.append(TargetConfig.from_dict(raw))
    except (KeyError, TypeError, ValueError) as exc:
        await send_error(
            ctx.ws, "invalid_payload",
            f"Invalid target configuration: {exc}",
            request_id=ctx.request_id,
        )
        return

    # Accept deviceAddresses (array) or fall back to deviceAddress (string)
    device_addresses: list[str] = ctx.payload.get("deviceAddresses", [])
    if not device_addresses:
        single = ctx.payload.get("deviceAddress")
        if single:
            device_addresses = [single]

    snapshot = await ctx.store.snapshot()

    # If still empty, use all currently connected devices
    if not device_addresses:
        device_addresses = [
            addr for addr, dev in snapshot.items()
            if dev.get("connected")
        ]

    if not device_addresses:
        await send_error(
            ctx.ws, "no_devices",
            "No devices specified and none are currently connected.",
            request_id=ctx.request_id,
        )
        return

    # Validate every explicitly requested address exists in the device
    # store before we write anything. Previously this handler upserted
    # unknown addresses as a side-effect, which let a misbehaving
    # client pollute the in-memory store with ghost entries that
    # persisted until server restart and later satisfied
    # session_add_device_request "device_not_found" checks incorrectly.
    unknown = [addr for addr in device_addresses if addr not in snapshot]
    if unknown:
        await send_error(
            ctx.ws, "device_not_found",
            f"Unknown device address(es): {', '.join(unknown)}. "
            "Device must be discovered by BLE scan first.",
            request_id=ctx.request_id,
        )
        return

    name = ctx.payload.get("name")
    if name is not None:
        name = str(name)[:200]  # Truncate to prevent abuse

    raw_target_duration = ctx.payload.get("targetDurationSecs")
    target_duration_secs: Optional[int] = None
    if raw_target_duration is not None:
        try:
            target_duration_secs = int(raw_target_duration)
        except (TypeError, ValueError):
            await send_error(
                ctx.ws, "invalid_payload",
                "targetDurationSecs must be an integer.",
                request_id=ctx.request_id,
            )
            return
        if target_duration_secs <= 0:
            await send_error(
                ctx.ws, "invalid_payload",
                "targetDurationSecs must be a positive integer.",
                request_id=ctx.request_id,
            )
            return

    session_info = await ctx.history.start_session(
        addresses=device_addresses,
        reason="user",
        name=name,
        target_duration_secs=target_duration_secs,
    )

    if session_info.get("end_event"):
        await ctx.store.publish_event(make_envelope("session_end", session_info["end_event"]))
    await ctx.store.publish_event(make_envelope("session_start", session_info["start_event"]))

    new_session_id = session_info["session_id"]

    if targets:
        for addr in device_addresses:
            await ctx.history.save_targets(new_session_id, addr, targets)
        ctx.evaluator.set_targets(new_session_id, targets)

    for address in device_addresses:
        await ctx.store.upsert(
            address,
            session_id=new_session_id,
            session_start_ts=session_info["session_start_ts"],
        )

    response_payload: dict[str, Any] = {
        "ok": True,
        "sessionId": new_session_id,
        "sessionStartTs": session_info["session_start_ts"],
        "name": name,
        "targetDurationSecs": target_duration_secs,
        "devices": device_addresses,
        "targets": [t.to_dict() for t in targets],
    }
    await send_envelope(ctx.ws, "session_start_ack", response_payload, request_id=ctx.request_id)


async def _handle_session_end(ctx: _MessageContext) -> None:
    if not ctx.authorized:
        await send_error(
            ctx.ws, "unauthorized", "Not allowed to end a session",
            request_id=ctx.request_id,
        )
        return

    result = await ctx.history.end_session(reason="user")

    if result is None:
        await send_error(
            ctx.ws, "no_active_session", "No session is currently active.",
            request_id=ctx.request_id,
        )
        return

    ended_session_id = result["sessionId"]
    ctx.evaluator.clear_session(ended_session_id)

    # Stop simulation if the ended session was a simulated one
    if ctx.simulator and ctx.simulator.is_running:
        await ctx.simulator.stop()

    await ctx.store.publish_event(make_envelope("session_end", result))

    await send_envelope(
        ctx.ws, "session_end_ack",
        {
            "ok": True,
            "sessionId": ended_session_id,
            "endedAt": result["sessionEndTs"],
        },
        request_id=ctx.request_id,
    )


async def _handle_session_discard(ctx: _MessageContext) -> None:
    if not ctx.authorized:
        await send_error(
            ctx.ws, "unauthorized", "Not allowed to discard sessions",
            request_id=ctx.request_id,
        )
        return

    session_state = await ctx.history.get_session_state()
    current_sid = session_state.get("current_session_id")
    if current_sid is None:
        await send_error(
            ctx.ws, "no_active_session", "No session is currently active.",
            request_id=ctx.request_id,
        )
        return

    deleted = await ctx.history.discard_session(current_sid)
    if not deleted:
        await send_error(
            ctx.ws, "session_not_found",
            f"Session {current_sid} does not exist.",
            request_id=ctx.request_id,
        )
        return

    ctx.evaluator.clear_session(current_sid)

    # Stop simulation if a simulated session was in progress.
    if ctx.simulator and ctx.simulator.is_running:
        await ctx.simulator.stop()

    await ctx.store.publish_event(
        make_envelope("session_discarded", {"sessionId": current_sid})
    )

    await send_envelope(
        ctx.ws, "session_discard_ack",
        {"ok": True, "sessionId": current_sid},
        request_id=ctx.request_id,
    )


async def _handle_probe_timer(ctx: _MessageContext) -> None:
    if not ctx.authorized:
        await send_error(
            ctx.ws, "unauthorized", "Not allowed to modify probe timers",
            request_id=ctx.request_id,
        )
        return

    address = ctx.payload.get("address")
    probe_index = ctx.payload.get("probe_index")
    action = ctx.payload.get("action")

    if not isinstance(address, str) or not address:
        await send_error(
            ctx.ws, "invalid_payload", "address is required.",
            request_id=ctx.request_id,
        )
        return
    if not isinstance(probe_index, int):
        await send_error(
            ctx.ws, "invalid_payload", "probe_index must be an integer.",
            request_id=ctx.request_id,
        )
        return
    if not isinstance(action, str):
        await send_error(
            ctx.ws, "invalid_payload", "action is required.",
            request_id=ctx.request_id,
        )
        return

    session_state = await ctx.history.get_session_state()
    current_sid = session_state.get("current_session_id")
    if current_sid is None:
        await send_error(
            ctx.ws, "no_active_session", "No session is currently active.",
            request_id=ctx.request_id,
        )
        return

    try:
        if action == "upsert":
            mode = ctx.payload.get("mode")
            duration_secs = ctx.payload.get("duration_secs")
            if mode == "count_down" and duration_secs is None:
                await send_error(
                    ctx.ws, "invalid_mode",
                    "duration_secs is required when mode is count_down.",
                    request_id=ctx.request_id,
                )
                return
            row = await ctx.history.upsert_timer(
                current_sid, address, probe_index, mode, duration_secs,
            )
        elif action == "start":
            row = await ctx.history.start_timer(current_sid, address, probe_index)
        elif action == "pause":
            row = await ctx.history.pause_timer(current_sid, address, probe_index)
        elif action == "resume":
            row = await ctx.history.resume_timer(current_sid, address, probe_index)
        elif action == "reset":
            row = await ctx.history.reset_timer(current_sid, address, probe_index)
        else:
            await send_error(
                ctx.ws, "invalid_action",
                f"Unsupported probe_timer action: {action}",
                request_id=ctx.request_id,
            )
            return
    except ValueError as exc:
        message = str(exc)
        if action == "upsert" and "mode must be" in message:
            code = "invalid_mode"
        else:
            code = "timer_not_found"
        await send_error(ctx.ws, code, message, request_id=ctx.request_id)
        return

    await ctx.store.publish_event(make_envelope("probe_timer_update", row))

    await send_envelope(
        ctx.ws, "probe_timer_ack", row, request_id=ctx.request_id,
    )


async def _handle_session_notes_update(ctx: _MessageContext) -> None:
    if not ctx.authorized:
        await send_error(
            ctx.ws, "unauthorized", "Not allowed to update session notes",
            request_id=ctx.request_id,
        )
        return

    body = ctx.payload.get("body")
    if not isinstance(body, str):
        await send_error(
            ctx.ws, "invalid_payload", "body is required and must be a string.",
            request_id=ctx.request_id,
        )
        return

    session_id = ctx.payload.get("sessionId")
    if session_id is None:
        session_state = await ctx.history.get_session_state()
        session_id = session_state.get("current_session_id")
        if session_id is None:
            await send_error(
                ctx.ws, "no_session_specified",
                "No sessionId provided and no active session.",
                request_id=ctx.request_id,
            )
            return
    else:
        session_id = str(session_id)

    if not await ctx.history.session_exists(session_id):
        await send_error(
            ctx.ws, "session_not_found",
            f"Session {session_id} does not exist.",
            request_id=ctx.request_id,
        )
        return

    row = await ctx.history.upsert_primary_note(session_id, body)

    await ctx.store.publish_event(make_envelope("session_notes_update", row))

    await send_envelope(
        ctx.ws, "session_notes_update_ack", row, request_id=ctx.request_id,
    )


async def _handle_target_update(ctx: _MessageContext) -> None:
    if not ctx.authorized:
        await send_error(
            ctx.ws, "unauthorized", "Not allowed to update targets",
            request_id=ctx.request_id,
        )
        return

    raw_targets = ctx.payload.get("targets", [])
    target_address = ctx.payload.get("deviceAddress")
    targets: list[TargetConfig] = []
    try:
        for raw in raw_targets:
            targets.append(TargetConfig.from_dict(raw))
    except (KeyError, TypeError, ValueError) as exc:
        await send_error(
            ctx.ws, "invalid_payload",
            f"Invalid target configuration: {exc}",
            request_id=ctx.request_id,
        )
        return

    session_state = await ctx.history.get_session_state()
    current_sid = session_state.get("current_session_id")
    if current_sid is None:
        await send_error(
            ctx.ws, "no_active_session", "No session is currently active.",
            request_id=ctx.request_id,
        )
        return

    if not target_address:
        session_devices = await ctx.history.get_session_devices(current_sid)
        if session_devices:
            target_address = session_devices[0]["address"]
        else:
            target_address = "all"

    await ctx.history.update_targets(current_sid, target_address, targets)
    ctx.evaluator.set_targets(current_sid, targets)

    payload = {
        "ok": True,
        "sessionId": current_sid,
        "targets": [t.to_dict() for t in targets],
    }

    # Broadcast to all peers so other clients (iOS, other web tabs) pick
    # up the new targets without waiting for a status snapshot. The
    # requesting client also gets the ack via send_envelope below; both
    # carry the same payload shape and iOS treats them equivalently.
    await ctx.store.publish_event(make_envelope("target_update", payload))

    await send_envelope(
        ctx.ws, "target_update_ack", payload, request_id=ctx.request_id,
    )


async def _handle_session_add_device(ctx: _MessageContext) -> None:
    if not ctx.authorized:
        await send_error(
            ctx.ws, "unauthorized", "Not allowed to modify sessions",
            request_id=ctx.request_id,
        )
        return

    add_address = ctx.payload.get("deviceAddress")
    if not add_address:
        await send_error(
            ctx.ws, "invalid_payload",
            "deviceAddress is required.",
            request_id=ctx.request_id,
        )
        return

    known_devices = await ctx.store.snapshot()
    if add_address not in known_devices:
        await send_error(
            ctx.ws, "device_not_found",
            "Device not found. It must be discovered by BLE scan first.",
            request_id=ctx.request_id,
        )
        return

    session_state = await ctx.history.get_session_state()
    current_sid = session_state.get("current_session_id")
    if current_sid is None:
        await send_error(
            ctx.ws, "no_active_session", "No session is currently active.",
            request_id=ctx.request_id,
        )
        return

    await ctx.history.add_device_to_session(current_sid, add_address)

    device_joined_payload: dict[str, Any] = {
        "sessionId": current_sid,
        "deviceAddress": add_address,
        "joinedAt": now_iso_utc(),
    }
    await ctx.store.publish_event(make_envelope("device_joined", device_joined_payload))

    await send_envelope(
        ctx.ws, "session_add_device_ack",
        {
            "ok": True,
            "sessionId": current_sid,
            "deviceAddress": add_address,
        },
        request_id=ctx.request_id,
    )


async def _handle_session_update(ctx: _MessageContext) -> None:
    if not ctx.authorized:
        await send_error(ctx.ws, "unauthorized", "Not allowed to update sessions", request_id=ctx.request_id)
        return

    session_id = ctx.payload.get("sessionId")
    if session_id is None:
        session_state = await ctx.history.get_session_state()
        session_id = session_state.get("current_session_id")
    if session_id is None:
        await send_error(ctx.ws, "no_session", "No sessionId provided and no active session.", request_id=ctx.request_id)
        return

    name = ctx.payload.get("name")
    if name is not None:
        name = str(name)[:200]  # Truncate to prevent abuse
    notes = ctx.payload.get("notes")

    result = await ctx.history.update_session(session_id, name=name, notes=notes)
    if result is None:
        await send_error(ctx.ws, "session_not_found", f"Session {session_id} does not exist.", request_id=ctx.request_id)
        return

    payload = {
        "ok": True, "sessionId": session_id,
        "name": result["name"], "notes": result["notes"],
    }

    # Broadcast so peers pick up the rename / notes edit without waiting
    # for a status snapshot. Mirrors the probe_timer_update pattern:
    # broadcast event + targeted ack share the same payload shape and
    # the receiving client applies them through the same handler.
    await ctx.store.publish_event(make_envelope("session_update", payload))

    await send_envelope(ctx.ws, "session_update_ack", payload, request_id=ctx.request_id)


# Map message types to their handler functions.  Each handler receives a
# _MessageContext and uses ``return`` instead of ``continue`` to abort.
_MESSAGE_HANDLERS: dict[str, Any] = {
    "status_request": _handle_status,
    "sessions_request": _handle_sessions,
    "history_request": _handle_history,
    "session_start_request": _handle_session_start,
    "session_end_request": _handle_session_end,
    "session_discard_request": _handle_session_discard,
    "session_update_request": _handle_session_update,
    "target_update_request": _handle_target_update,
    "session_add_device_request": _handle_session_add_device,
    "probe_timer_request": _handle_probe_timer,
    "session_notes_update_request": _handle_session_notes_update,
}


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
    simulator: Optional[SimulationRunner] = request.app.get("simulator")
    authorized = is_authorized(request)

    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    client = WebSocketClient(ws)
    hub.add(client)

    peer = request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote or "unknown"
    LOG.info("WebSocket client connected from %s (total: %d)", peer, len(hub.clients))

    try:
        async for msg in ws:
            # Accept both TEXT and BINARY frames. JSON over WebSocket is
            # conventionally TEXT, but some clients (including earlier
            # versions of the iOS app) send BINARY — previously those
            # frames fell through this loop silently, producing the
            # confusing symptom of "session_start never reaches the
            # server". Decoding both types avoids that trap.
            if msg.type in (web.WSMsgType.TEXT, web.WSMsgType.BINARY):
                raw = msg.data
                if isinstance(raw, (bytes, bytearray)):
                    try:
                        raw = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        LOG.warning("Non-UTF8 binary frame from %s", peer)
                        await send_error(ws, "invalid_encoding", "Binary frame must be UTF-8 JSON.")
                        continue
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    LOG.warning("Invalid JSON from %s", peer)
                    await send_error(ws, "invalid_json", "Message must be valid JSON.")
                    continue

                if not isinstance(data, dict):
                    await send_error(ws, "invalid_message", "Message must be a JSON object.")
                    continue

                msg_version = data.get("v")
                if msg_version != PROTOCOL_VERSION:
                    await send_error(ws, "unsupported_version", "Unsupported message version.")
                    continue

                msg_type = data.get("type")
                request_id = data.get("requestId")
                payload = data.get("payload") or {}

                LOG.info("WS recv from %s: type=%s requestId=%s", peer, msg_type, request_id)

                # Rate-limit session-control messages
                if msg_type in _SESSION_CONTROL_TYPES:
                    if not _session_limiter.allow(peer):
                        await send_error(
                            ws, "rate_limited",
                            "Too many session control requests. Try again later.",
                            request_id=request_id,
                        )
                        continue

                handler = _MESSAGE_HANDLERS.get(msg_type)
                if handler is not None:
                    if not request_id:
                        await send_error(ws, "missing_request_id", f"{msg_type} requires requestId.")
                        continue
                    ctx = _MessageContext(
                        ws=ws,
                        store=store,
                        history=history,
                        evaluator=evaluator,
                        config=config,
                        simulator=simulator,
                        authorized=authorized,
                        peer=peer,
                        request_id=request_id,
                        payload=payload,
                    )
                    await handler(ctx)
                else:
                    await send_error(
                        ws, "unknown_type",
                        f"Unsupported message type: {msg_type}",
                        request_id=request_id,
                    )

            elif msg.type == web.WSMsgType.ERROR:
                LOG.warning("WebSocket error from %s: %s", peer, ws.exception())
    finally:
        LOG.info("WebSocket client disconnected: %s (remaining: %d)", peer, len(hub.clients) - 1)
        await hub.remove(client)

    return ws


# ---------------------------------------------------------------------------
# Broadcast coroutines — run as background tasks
# ---------------------------------------------------------------------------


async def broadcast_readings(app: web.Application) -> None:
    """Forward queued readings to all WebSocket clients."""
    store: DeviceStore = app["store"]
    hub: WebSocketHub = app["hub"]
    push_service = app.get("push_service")

    while True:
        reading = await store.next_reading()
        message = make_envelope("reading", reading["payload"], seq=reading.get("seq"))
        client_count = len(hub.clients)
        if client_count > 0:
            LOG.debug("Broadcasting reading to %d client(s) seq=%s", client_count, reading.get("seq"))
        hub.broadcast(message, critical=False)

        # Throttled Live Activity push update
        if push_service and push_service.should_send_la_update():
            try:
                await push_service.send_live_activity_update(reading)
            except Exception:
                LOG.exception("Failed to send Live Activity update")


async def broadcast_events(app: web.Application) -> None:
    """Forward queued events to all WebSocket clients and push service."""
    store: DeviceStore = app["store"]
    hub: WebSocketHub = app["hub"]
    push_service = app.get("push_service")
    alert_types = {"target_approaching", "target_reached", "target_exceeded", "target_reminder"}

    while True:
        event = await store.next_event()
        hub.broadcast(event, critical=True)

        # Send push notification for alert events
        if push_service and event.get("type") in alert_types:
            try:
                await push_service.send_alert(event)
            except Exception:
                LOG.exception("Failed to send push for event %s", event.get("type"))
