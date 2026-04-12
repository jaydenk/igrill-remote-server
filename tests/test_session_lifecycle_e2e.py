"""End-to-end integration tests exercising the full session lifecycle.

These tests drive the real WebSocket handlers, in-memory device store,
broadcast pipeline, HistoryStore, and REST export layer together. They
are intentionally coarse-grained: each scenario simulates a realistic
client flow (start session, configure probes/timers, write notes,
simulate readings, save/discard/end) and then asserts the server-side
state — both via broadcast messages and via the GET /api/sessions/{id}
REST endpoint.
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from service.api.websocket import broadcast_events
from service.config import Config
from service.main import create_app


_DEVICE_A = "AA:BB:CC:DD:EE:01"
_DEVICE_B = "AA:BB:CC:DD:EE:02"  # currently unused; room for later expansion


@pytest.fixture
def config(tmp_db):
    return Config(db_path=tmp_db)


@pytest.fixture(autouse=True)
def _reset_session_rate_limiter():
    """Isolate the module-level session-control rate limiter per test."""
    from service.api import websocket as ws_mod

    ws_mod._session_limiter = ws_mod._RateLimiter(max_requests=10, window_seconds=60)
    yield
    ws_mod._session_limiter = ws_mod._RateLimiter(max_requests=10, window_seconds=60)


@pytest_asyncio.fixture
async def client(aiohttp_client, config):
    app = create_app(config)
    await app["history"].connect()
    c = await aiohttp_client(app)
    yield c
    await app["history"].close()


async def _seed_device(client, address: str = _DEVICE_A, name: str = "Test iGrill") -> None:
    """Register a fake connected device so session_start_request succeeds."""
    await client.app["store"].upsert(address, connected=True, name=name)


async def _drain_until(ws, wanted_type: str, *, timeout: float = 2.0) -> dict:
    """Receive messages until one of ``wanted_type`` (or ``error``) is seen."""
    while True:
        msg = await asyncio.wait_for(ws.receive_json(), timeout=timeout)
        if msg.get("type") in (wanted_type, "error"):
            return msg


async def _wait_for_broadcast(ws, wanted_type: str, *, attempts: int = 20) -> dict | None:
    """Poll ``ws`` for a broadcast of ``wanted_type``; return None on timeout."""
    for _ in range(attempts):
        try:
            msg = await asyncio.wait_for(ws.receive_json(), timeout=1.0)
        except asyncio.TimeoutError:
            return None
        if msg.get("type") == wanted_type:
            return msg
    return None


async def _drain(ws, *, timeout: float = 0.3) -> None:
    """Drain any currently-queued WS messages."""
    try:
        while True:
            await asyncio.wait_for(ws.receive_json(), timeout=timeout)
    except asyncio.TimeoutError:
        return


# ---------------------------------------------------------------------------
# Scenario 1 — full save lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_session_save_lifecycle(client):
    """Drive a full session from start to save: targets (mixed fixed +
    range), timers (count_down + count_up) with start, simulated readings,
    notes, and session_end. Then verify GET /api/sessions/{id} returns the
    expected aggregated shape.
    """
    await _seed_device(client)
    broadcast_task = asyncio.create_task(broadcast_events(client.app))
    try:
        async with client.ws_connect("/ws") as ws:
            # --- 1. start session ---
            await ws.send_json({
                "v": 2, "type": "session_start_request", "requestId": "r1",
                "payload": {
                    "name": "Brisket & Ribs",
                    "deviceAddresses": [_DEVICE_A],
                    "targetDurationSecs": 3600,
                    "targets": [
                        {
                            "probe_index": 1, "mode": "fixed",
                            "target_value": 90, "label": "Brisket",
                        },
                        {
                            "probe_index": 2, "mode": "range",
                            "range_low": 110, "range_high": 130,
                            "label": "Ribs",
                        },
                    ],
                },
            })
            ack = await _drain_until(ws, "session_start_ack")
            assert ack.get("type") == "session_start_ack", ack
            session_id = ack["payload"]["sessionId"]
            assert ack["payload"]["name"] == "Brisket & Ribs"
            assert ack["payload"]["targetDurationSecs"] == 3600
            assert len(ack["payload"]["targets"]) == 2

            # --- 2. upsert two timers ---
            # probe 1: count_down 1800s
            await ws.send_json({
                "v": 2, "type": "probe_timer_request", "requestId": "t1u",
                "payload": {
                    "address": _DEVICE_A, "probe_index": 1,
                    "action": "upsert",
                    "mode": "count_down", "duration_secs": 1800,
                },
            })
            ack = await _drain_until(ws, "probe_timer_ack")
            assert ack["type"] == "probe_timer_ack", ack

            # probe 2: count_up
            await ws.send_json({
                "v": 2, "type": "probe_timer_request", "requestId": "t2u",
                "payload": {
                    "address": _DEVICE_A, "probe_index": 2,
                    "action": "upsert",
                    "mode": "count_up",
                },
            })
            ack = await _drain_until(ws, "probe_timer_ack")
            assert ack["type"] == "probe_timer_ack", ack

            # --- 3. start both timers ---
            for probe in (1, 2):
                await ws.send_json({
                    "v": 2, "type": "probe_timer_request",
                    "requestId": f"t{probe}s",
                    "payload": {
                        "address": _DEVICE_A, "probe_index": probe,
                        "action": "start",
                    },
                })
                ack = await _drain_until(ws, "probe_timer_ack")
                assert ack["type"] == "probe_timer_ack", ack
                assert ack["payload"]["started_at"] is not None

            # --- 4. simulate readings (direct path, like other tests) ---
            history = client.app["history"]
            await history.record_reading(
                session_id=session_id, address=_DEVICE_A, seq=1,
                probes=[
                    {"index": 1, "temperature": 45.0},
                    {"index": 2, "temperature": 60.0},
                ],
                battery=88, propane=None, heating=None,
            )
            await history.record_reading(
                session_id=session_id, address=_DEVICE_A, seq=2,
                probes=[
                    {"index": 1, "temperature": 55.0},
                    {"index": 2, "temperature": 72.0},
                ],
                battery=87, propane=None, heating=None,
            )

            # --- 5. update notes ---
            await ws.send_json({
                "v": 2, "type": "session_notes_update_request",
                "requestId": "n1",
                "payload": {"body": "Meat on at 8am"},
            })
            ack = await _drain_until(ws, "session_notes_update_ack")
            assert ack["type"] == "session_notes_update_ack", ack
            assert ack["payload"]["body"] == "Meat on at 8am"

            # --- 6. end session ---
            await ws.send_json({
                "v": 2, "type": "session_end_request", "requestId": "r_end",
                "payload": {},
            })
            ack = await _drain_until(ws, "session_end_ack")
            assert ack["type"] == "session_end_ack", ack
    finally:
        broadcast_task.cancel()
        try:
            await broadcast_task
        except asyncio.CancelledError:
            pass

    # --- 7. REST — verify the persisted aggregate state ---
    resp = await client.get(f"/api/sessions/{session_id}")
    assert resp.status == 200
    body = await resp.json()

    assert body["name"] == "Brisket & Ribs"
    assert body["notesBody"] == "Meat on at 8am"
    assert body["targetDurationSecs"] == 3600

    # Targets — two entries, ordering is not guaranteed, check by probe_index.
    targets_by_probe = {t["probe_index"]: t for t in body["targets"]}
    assert set(targets_by_probe) == {1, 2}
    assert targets_by_probe[1]["mode"] == "fixed"
    assert targets_by_probe[1]["target_value"] == 90
    assert targets_by_probe[1]["label"] == "Brisket"
    assert targets_by_probe[2]["mode"] == "range"
    assert targets_by_probe[2]["range_low"] == 110
    assert targets_by_probe[2]["range_high"] == 130
    assert targets_by_probe[2]["label"] == "Ribs"

    # Timers — two entries with the correct shape.
    assert len(body["timers"]) == 2
    timers_by_probe = {t["probeIndex"]: t for t in body["timers"]}
    assert set(timers_by_probe) == {1, 2}
    assert timers_by_probe[1]["mode"] == "count_down"
    assert timers_by_probe[1]["durationSecs"] == 1800
    assert timers_by_probe[1]["startedAt"] is not None
    assert timers_by_probe[2]["mode"] == "count_up"
    assert timers_by_probe[2]["startedAt"] is not None

    # Notes — one entry with the expected body.
    assert len(body["notes"]) == 1
    assert body["notes"][0]["body"] == "Meat on at 8am"

    # Readings — two cycles * two probes = 4 rows present.
    assert len(body["readings"]) == 4
    probe_indices = sorted({r["probe_index"] for r in body["readings"]})
    assert probe_indices == [1, 2]


# ---------------------------------------------------------------------------
# Scenario 2 — discard lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_session_discard_lifecycle(client):
    """Start a session, generate readings/timers/notes, then discard.

    After discard the session, its timers, and its notes must be gone and
    the broadcast visible to observers. There should be no active session.
    """
    await _seed_device(client)
    broadcast_task = asyncio.create_task(broadcast_events(client.app))
    try:
        async with client.ws_connect("/ws") as ws_a, client.ws_connect("/ws") as ws_b:
            # --- 1. start session ---
            await ws_a.send_json({
                "v": 2, "type": "session_start_request", "requestId": "r1",
                "payload": {
                    "name": "Discard Me",
                    "deviceAddresses": [_DEVICE_A],
                    "targets": [{
                        "probe_index": 1, "mode": "fixed", "target_value": 95,
                    }],
                },
            })
            ack = await _drain_until(ws_a, "session_start_ack")
            assert ack["type"] == "session_start_ack", ack
            session_id = ack["payload"]["sessionId"]

            # Drain the start broadcast on ws_b so we can isolate later ones.
            await _wait_for_broadcast(ws_b, "session_start")

            # --- 2. simulate readings ---
            history = client.app["history"]
            await history.record_reading(
                session_id=session_id, address=_DEVICE_A, seq=1,
                probes=[{"index": 1, "temperature": 40.0}],
                battery=90, propane=None, heating=None,
            )
            await history.record_reading(
                session_id=session_id, address=_DEVICE_A, seq=2,
                probes=[{"index": 1, "temperature": 50.0}],
                battery=89, propane=None, heating=None,
            )

            # --- 3. upsert + start a timer ---
            await ws_a.send_json({
                "v": 2, "type": "probe_timer_request", "requestId": "t1u",
                "payload": {
                    "address": _DEVICE_A, "probe_index": 1,
                    "action": "upsert",
                    "mode": "count_up",
                },
            })
            ack = await _drain_until(ws_a, "probe_timer_ack")
            assert ack["type"] == "probe_timer_ack", ack

            await ws_a.send_json({
                "v": 2, "type": "probe_timer_request", "requestId": "t1s",
                "payload": {
                    "address": _DEVICE_A, "probe_index": 1,
                    "action": "start",
                },
            })
            ack = await _drain_until(ws_a, "probe_timer_ack")
            assert ack["type"] == "probe_timer_ack", ack

            # --- 4. update notes ---
            await ws_a.send_json({
                "v": 2, "type": "session_notes_update_request",
                "requestId": "n1",
                "payload": {"body": "will be discarded"},
            })
            ack = await _drain_until(ws_a, "session_notes_update_ack")
            assert ack["type"] == "session_notes_update_ack", ack

            # Drain any queued broadcasts on ws_b so the discard one is clean.
            await _drain(ws_b, timeout=0.3)

            # --- 5. discard the session ---
            await ws_a.send_json({
                "v": 2, "type": "session_discard_request", "requestId": "d1",
                "payload": {},
            })
            ack = await _drain_until(ws_a, "session_discard_ack")
            assert ack["type"] == "session_discard_ack", ack
            assert ack["payload"]["sessionId"] == session_id
            assert ack["payload"]["ok"] is True

            # ws_b sees the session_discarded broadcast.
            discard_broadcast = await _wait_for_broadcast(ws_b, "session_discarded")
            assert discard_broadcast is not None, (
                "ws_b did not observe session_discarded broadcast"
            )
            assert discard_broadcast["payload"]["sessionId"] == session_id

            # --- 6. status shows no active session ---
            await ws_a.send_json({
                "v": 2, "type": "status_request", "requestId": "s1",
                "payload": {},
            })
            status = await _drain_until(ws_a, "status")
            assert status.get("type") == "status", status
            assert status["payload"]["currentSessionId"] is None
    finally:
        broadcast_task.cancel()
        try:
            await broadcast_task
        except asyncio.CancelledError:
            pass

    # --- REST lookup for the discarded session returns 404 ---
    resp = await client.get(f"/api/sessions/{session_id}")
    assert resp.status == 404

    # --- HistoryStore state is fully purged for this session ---
    history = client.app["history"]
    assert await history.get_timers(session_id) == []
    assert await history.get_notes(session_id) == []


# ---------------------------------------------------------------------------
# Scenario 3 — mid-session probe add
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mid_session_probe_add(client):
    """Start a session with only probe 1 targeted. After some readings,
    add probe 2 via target_update_request, then send readings for probe 2.
    The saved session must show targets for both probes and readings from
    both probes, with probe 2 readings temporally after probe 1's first
    readings."""
    await _seed_device(client)
    broadcast_task = asyncio.create_task(broadcast_events(client.app))
    try:
        async with client.ws_connect("/ws") as ws:
            # --- 1. start session with target for probe 1 only ---
            await ws.send_json({
                "v": 2, "type": "session_start_request", "requestId": "r1",
                "payload": {
                    "name": "Mid-cook add",
                    "deviceAddresses": [_DEVICE_A],
                    "targets": [{
                        "probe_index": 1, "mode": "fixed",
                        "target_value": 90, "label": "Brisket",
                    }],
                },
            })
            ack = await _drain_until(ws, "session_start_ack")
            assert ack["type"] == "session_start_ack", ack
            session_id = ack["payload"]["sessionId"]

            # --- 2. initial readings for probe 1 only ---
            history = client.app["history"]
            await history.record_reading(
                session_id=session_id, address=_DEVICE_A, seq=1,
                probes=[{"index": 1, "temperature": 40.0}],
                battery=95, propane=None, heating=None,
                recorded_at="2026-04-12T08:00:00+00:00",
            )
            await history.record_reading(
                session_id=session_id, address=_DEVICE_A, seq=2,
                probes=[{"index": 1, "temperature": 50.0}],
                battery=94, propane=None, heating=None,
                recorded_at="2026-04-12T08:05:00+00:00",
            )

            # --- 3. mid-session: add probe 2 via target_update_request ---
            # target_update_request replaces the target list for the given
            # address with the provided list, so we must send probe 1 too.
            await ws.send_json({
                "v": 2, "type": "target_update_request", "requestId": "tu1",
                "payload": {
                    "deviceAddress": _DEVICE_A,
                    "targets": [
                        {
                            "probe_index": 1, "mode": "fixed",
                            "target_value": 90, "label": "Brisket",
                        },
                        {
                            "probe_index": 2, "mode": "fixed",
                            "target_value": 75, "label": "Ribs",
                        },
                    ],
                },
            })
            ack = await _drain_until(ws, "target_update_ack")
            assert ack["type"] == "target_update_ack", ack
            assert ack["payload"]["ok"] is True
            assert len(ack["payload"]["targets"]) == 2

            # --- 4. readings for probe 2 (strictly later than probe 1) ---
            await history.record_reading(
                session_id=session_id, address=_DEVICE_A, seq=3,
                probes=[
                    {"index": 1, "temperature": 60.0},
                    {"index": 2, "temperature": 55.0},
                ],
                battery=93, propane=None, heating=None,
                recorded_at="2026-04-12T08:10:00+00:00",
            )
            await history.record_reading(
                session_id=session_id, address=_DEVICE_A, seq=4,
                probes=[
                    {"index": 1, "temperature": 70.0},
                    {"index": 2, "temperature": 68.0},
                ],
                battery=92, propane=None, heating=None,
                recorded_at="2026-04-12T08:15:00+00:00",
            )

            # --- 5. end session ---
            await ws.send_json({
                "v": 2, "type": "session_end_request", "requestId": "r_end",
                "payload": {},
            })
            ack = await _drain_until(ws, "session_end_ack")
            assert ack["type"] == "session_end_ack", ack
    finally:
        broadcast_task.cancel()
        try:
            await broadcast_task
        except asyncio.CancelledError:
            pass

    # --- 6. REST verification ---
    resp = await client.get(f"/api/sessions/{session_id}")
    assert resp.status == 200
    body = await resp.json()

    # Both probes now have targets.
    targets_by_probe = {t["probe_index"]: t for t in body["targets"]}
    assert set(targets_by_probe) == {1, 2}
    assert targets_by_probe[1]["target_value"] == 90
    assert targets_by_probe[2]["target_value"] == 75

    # Readings include both probes.
    readings = body["readings"]
    probe_indices = {r["probe_index"] for r in readings}
    assert probe_indices == {1, 2}

    probe1_times = sorted(
        r["recorded_at"] for r in readings if r["probe_index"] == 1
    )
    probe2_times = sorted(
        r["recorded_at"] for r in readings if r["probe_index"] == 2
    )
    # Probe 1 has four readings (seqs 1,2,3,4); probe 2 has two (seqs 3,4).
    assert len(probe1_times) == 4
    assert len(probe2_times) == 2

    # Probe 2's earliest reading is strictly later than probe 1's earliest.
    assert probe2_times[0] > probe1_times[0], (
        f"Expected probe 2 readings to start after probe 1; "
        f"probe1_earliest={probe1_times[0]!r} probe2_earliest={probe2_times[0]!r}"
    )
