"""Integration tests for the full server."""

import pytest
import pytest_asyncio
from service.main import create_app
from service.config import Config


@pytest.fixture
def config(tmp_db):
    return Config(db_path=tmp_db)


@pytest.fixture(autouse=True)
def _reset_session_rate_limiter():
    """Reset the module-level session-control rate limiter before and after
    each test so that tests cannot pollute each other's limiter state."""
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


@pytest.mark.asyncio
async def test_health_check(client):
    """Health endpoint returns ok."""
    resp = await client.get("/health")
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_no_sessions_on_start(client):
    """No sessions exist initially."""
    resp = await client.get("/api/sessions")
    assert resp.status == 200
    data = await resp.json()
    assert data["sessions"] == []


@pytest.mark.asyncio
async def test_dashboard_loads(client):
    """Dashboard HTML loads."""
    resp = await client.get("/")
    assert resp.status == 200
    text = await resp.text()
    assert "iGrill Remote" in text


@pytest.mark.asyncio
async def test_session_detail_not_found(client):
    """Session detail for nonexistent ID returns 404."""
    resp = await client.get("/api/sessions/nonexistent")
    assert resp.status == 404
    data = await resp.json()
    assert data["error"] == "session not found"


@pytest.mark.asyncio
async def test_log_levels_update(client):
    """Can update log levels at runtime."""
    resp = await client.put(
        "/api/config/log-levels",
        json={"igrill.ble": "DEBUG"},
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["results"]["igrill.ble"] is True


_TEST_DEVICE = "AA:BB:CC:DD:EE:FF"


async def _seed_device(client) -> None:
    """Register a fake connected device in the in-memory DeviceStore so that
    session_start_request can proceed without real BLE hardware."""
    store = client.app["store"]
    await store.upsert(_TEST_DEVICE, connected=True, name="Test iGrill")


@pytest.mark.asyncio
async def test_ws_session_update(client):
    """session_update_request sets name and notes on a session."""
    await _seed_device(client)
    async with client.ws_connect("/ws") as ws:
        # Start session with name
        await ws.send_json({
            "v": 2, "type": "session_start_request", "requestId": "r1",
            "payload": {
                "name": "Test Cook",
                "deviceAddresses": [_TEST_DEVICE],
                "targets": [{"probe_index": 1, "mode": "fixed", "target_value": 90}],
            },
        })
        # Consume messages until session_start_ack
        msg = await ws.receive_json()
        while msg.get("type") != "session_start_ack":
            msg = await ws.receive_json()
        session_id = msg["payload"]["sessionId"]
        assert msg["payload"].get("name") == "Test Cook"

        # Update notes
        await ws.send_json({
            "v": 2, "type": "session_update_request", "requestId": "r2",
            "payload": {"sessionId": session_id, "notes": "Oak wood."},
        })
        msg = await ws.receive_json()
        while msg.get("type") != "session_update_ack":
            msg = await ws.receive_json()
        assert msg["payload"]["ok"] is True
        assert msg["payload"]["notes"] == "Oak wood."
        assert msg["payload"]["name"] == "Test Cook"


@pytest.mark.asyncio
async def test_ws_session_discard_happy_path(client):
    """session_discard_request hard-deletes the active session, broadcasts
    session_discarded to connected clients, and clears server state."""
    import asyncio as _asyncio
    from service.api.websocket import broadcast_events

    await _seed_device(client)
    # Run the broadcast_events task so that publish_event enqueues reach
    # connected clients (the test fixture doesn't start this task itself).
    broadcast_task = _asyncio.create_task(broadcast_events(client.app))
    try:
        # Open two WS connections — one sends the discard, the other observes
        # the broadcast.
        async with client.ws_connect("/ws") as ws_a, client.ws_connect("/ws") as ws_b:
            await ws_a.send_json({
                "v": 2, "type": "session_start_request", "requestId": "r1",
                "payload": {
                    "name": "Discard Me",
                    "deviceAddresses": [_TEST_DEVICE],
                    "targets": [{"probe_index": 1, "mode": "fixed", "target_value": 90}],
                },
            })
            msg = await ws_a.receive_json()
            while msg.get("type") != "session_start_ack":
                msg = await ws_a.receive_json()
            session_id = msg["payload"]["sessionId"]

            # Drain the session_start broadcast on ws_b.
            saw_start_on_b = False
            for _ in range(5):
                try:
                    msg_b = await _asyncio.wait_for(ws_b.receive_json(), timeout=1.0)
                except _asyncio.TimeoutError:
                    break
                if msg_b.get("type") == "session_start":
                    saw_start_on_b = True
                    break
            assert saw_start_on_b, "ws_b did not observe session_start broadcast"

            await ws_a.send_json({
                "v": 2, "type": "session_discard_request", "requestId": "r2",
                "payload": {},
            })

            # ws_a should receive session_discard_ack.
            ack = await ws_a.receive_json()
            while ack.get("type") not in ("session_discard_ack", "error"):
                ack = await ws_a.receive_json()
            assert ack.get("type") == "session_discard_ack", ack
            assert ack["payload"]["sessionId"] == session_id
            assert ack["payload"]["ok"] is True

            # ws_b should observe the session_discarded broadcast.
            saw_discard = False
            for _ in range(10):
                try:
                    msg_b = await _asyncio.wait_for(ws_b.receive_json(), timeout=1.0)
                except _asyncio.TimeoutError:
                    break
                if msg_b.get("type") == "session_discarded":
                    assert msg_b["payload"]["sessionId"] == session_id
                    saw_discard = True
                    break
            assert saw_discard, "ws_b did not observe session_discarded broadcast"

            # status_request must now show no active session.
            await ws_a.send_json({
                "v": 2, "type": "status_request", "requestId": "r3", "payload": {},
            })
            status = await ws_a.receive_json()
            while status.get("type") != "status":
                status = await ws_a.receive_json()
            assert status["payload"]["currentSessionId"] is None
    finally:
        broadcast_task.cancel()
        try:
            await broadcast_task
        except _asyncio.CancelledError:
            pass

    # REST lookup for the deleted session returns 404 — confirms hard-delete.
    resp = await client.get(f"/api/sessions/{session_id}")
    assert resp.status == 404


@pytest.mark.asyncio
async def test_ws_session_discard_no_active_session(client):
    """session_discard_request with no active session replies with an error."""
    async with client.ws_connect("/ws") as ws:
        await ws.send_json({
            "v": 2, "type": "session_discard_request", "requestId": "r1",
            "payload": {},
        })
        msg = await ws.receive_json()
        while msg.get("type") not in ("error", "session_discard_ack"):
            msg = await ws.receive_json()
        assert msg.get("type") == "error"
        assert msg["payload"]["code"] == "no_active_session"
        assert msg.get("requestId") == "r1"


@pytest.mark.asyncio
async def test_ws_session_discard_unauthorized(aiohttp_client, tmp_db):
    """session_discard_request without Bearer auth returns unauthorized."""
    from service.main import create_app

    cfg = Config(db_path=tmp_db, session_token="secret")
    app = create_app(cfg)
    await app["history"].connect()
    try:
        c = await aiohttp_client(app)

        # Seed a device and start a session by calling the store/history
        # directly — we cannot use the session_start_request path without a
        # token either, and the purpose of this test is only to confirm
        # the discard handler rejects unauth.
        await c.app["store"].upsert(_TEST_DEVICE, connected=True, name="Test iGrill")
        await c.app["history"].start_session(addresses=[_TEST_DEVICE], reason="user")

        async with c.ws_connect("/ws") as ws:
            await ws.send_json({
                "v": 2, "type": "session_discard_request", "requestId": "r1",
                "payload": {},
            })
            msg = await ws.receive_json()
            while msg.get("type") not in ("error", "session_discard_ack"):
                msg = await ws.receive_json()
            assert msg.get("type") == "error"
            assert msg["payload"]["code"] == "unauthorized"
    finally:
        await app["history"].close()


@pytest.mark.asyncio
async def test_ws_session_discard_rate_limited(aiohttp_client, tmp_db):
    """The 11th session-control request within the window is rate-limited.

    Uses its own isolated app (not the shared ``client`` fixture) because
    this test deliberately exhausts the module-level rate limiter, and
    that state would otherwise leak into subsequent tests and hang them.
    The autouse ``_reset_session_rate_limiter`` fixture resets the limiter
    again on teardown.
    """
    from service.main import create_app

    cfg = Config(db_path=tmp_db)
    app = create_app(cfg)
    await app["history"].connect()
    try:
        c = await aiohttp_client(app)
        async with c.ws_connect("/ws") as ws:
            # Send 11 discard requests back-to-back.  All will fail with
            # "no_active_session" (there is no active session), except the
            # final one which should fail with "rate_limited" instead.
            for i in range(11):
                await ws.send_json({
                    "v": 2, "type": "session_discard_request",
                    "requestId": f"r{i}", "payload": {},
                })

            codes: list[str] = []
            import asyncio as _asyncio
            for _ in range(11):
                try:
                    msg = await _asyncio.wait_for(ws.receive_json(), timeout=1.0)
                except _asyncio.TimeoutError:
                    break
                if msg.get("type") == "error":
                    codes.append(msg["payload"]["code"])

            assert codes[:10] == ["no_active_session"] * 10
            assert codes[10] == "rate_limited"
    finally:
        await app["history"].close()


@pytest.mark.asyncio
async def test_session_detail_includes_name_notes(client):
    """GET /api/sessions/{id} includes name, notes, and target labels."""
    await _seed_device(client)
    async with client.ws_connect("/ws") as ws:
        await ws.send_json({
            "v": 2, "type": "session_start_request", "requestId": "r1",
            "payload": {
                "name": "REST Test",
                "deviceAddresses": [_TEST_DEVICE],
                "targets": [{"probe_index": 1, "mode": "fixed", "target_value": 80, "label": "Brisket"}],
            },
        })
        msg = await ws.receive_json()
        while msg.get("type") != "session_start_ack":
            msg = await ws.receive_json()
        session_id = msg["payload"]["sessionId"]

        await ws.send_json({"v": 2, "type": "session_end_request", "requestId": "r2", "payload": {}})
        msg = await ws.receive_json()
        while msg.get("type") != "session_end_ack":
            msg = await ws.receive_json()

    resp = await client.get(f"/api/sessions/{session_id}")
    assert resp.status == 200
    body = await resp.json()
    assert body.get("name") == "REST Test"
    assert body["targets"][0]["label"] == "Brisket"


# ---------------------------------------------------------------------------
# probe_timer_request handler tests (Task 10)
# ---------------------------------------------------------------------------


async def _start_session_for_timer(ws) -> str:
    """Helper — start a session over ws and return its session id."""
    await ws.send_json({
        "v": 2, "type": "session_start_request", "requestId": "rstart",
        "payload": {
            "name": "Timer Test",
            "deviceAddresses": [_TEST_DEVICE],
            "targets": [{"probe_index": 1, "mode": "fixed", "target_value": 90}],
        },
    })
    msg = await ws.receive_json()
    while msg.get("type") != "session_start_ack":
        msg = await ws.receive_json()
    return msg["payload"]["sessionId"]


@pytest.mark.asyncio
async def test_ws_probe_timer_full_roundtrip(client):
    """Full probe_timer lifecycle: upsert + start + pause + resume + reset,
    with ack on the requester and probe_timer_update broadcast on a second
    observer client."""
    import asyncio as _asyncio
    from service.api.websocket import broadcast_events

    await _seed_device(client)
    broadcast_task = _asyncio.create_task(broadcast_events(client.app))
    try:
        async with client.ws_connect("/ws") as ws_a, client.ws_connect("/ws") as ws_b:
            session_id = await _start_session_for_timer(ws_a)

            async def _drain_until(ws, ack_type):
                msg = await ws.receive_json()
                while msg.get("type") not in (ack_type, "error"):
                    msg = await ws.receive_json()
                return msg

            async def _wait_for_broadcast(ws, wanted_type):
                for _ in range(20):
                    try:
                        msg = await _asyncio.wait_for(ws.receive_json(), timeout=1.0)
                    except _asyncio.TimeoutError:
                        return None
                    if msg.get("type") == wanted_type:
                        return msg
                return None

            # --- upsert ---
            await ws_a.send_json({
                "v": 2, "type": "probe_timer_request", "requestId": "t1",
                "payload": {
                    "address": _TEST_DEVICE,
                    "probe_index": 1,
                    "action": "upsert",
                    "mode": "count_down",
                    "duration_secs": 60,
                },
            })
            ack = await _drain_until(ws_a, "probe_timer_ack")
            assert ack.get("type") == "probe_timer_ack", ack
            assert ack["payload"]["session_id"] == session_id
            assert ack["payload"]["address"] == _TEST_DEVICE
            assert ack["payload"]["probe_index"] == 1
            assert ack["payload"]["mode"] == "count_down"
            assert ack["payload"]["duration_secs"] == 60
            assert ack["payload"]["started_at"] is None
            assert ack["payload"]["paused_at"] is None
            assert ack["payload"]["accumulated_secs"] == 0

            broadcast = await _wait_for_broadcast(ws_b, "probe_timer_update")
            assert broadcast is not None, "ws_b did not observe upsert broadcast"
            assert broadcast["payload"]["mode"] == "count_down"
            assert broadcast["payload"]["duration_secs"] == 60

            # --- start ---
            await ws_a.send_json({
                "v": 2, "type": "probe_timer_request", "requestId": "t2",
                "payload": {
                    "address": _TEST_DEVICE, "probe_index": 1, "action": "start",
                },
            })
            ack = await _drain_until(ws_a, "probe_timer_ack")
            assert ack.get("type") == "probe_timer_ack", ack
            assert ack["payload"]["started_at"] is not None
            assert ack["payload"]["paused_at"] is None

            broadcast = await _wait_for_broadcast(ws_b, "probe_timer_update")
            assert broadcast is not None, "ws_b did not observe start broadcast"
            assert broadcast["payload"]["started_at"] is not None

            # --- pause ---
            await ws_a.send_json({
                "v": 2, "type": "probe_timer_request", "requestId": "t3",
                "payload": {
                    "address": _TEST_DEVICE, "probe_index": 1, "action": "pause",
                },
            })
            ack = await _drain_until(ws_a, "probe_timer_ack")
            assert ack.get("type") == "probe_timer_ack", ack
            assert ack["payload"]["paused_at"] is not None
            assert ack["payload"]["started_at"] is None
            assert ack["payload"]["accumulated_secs"] >= 0

            broadcast = await _wait_for_broadcast(ws_b, "probe_timer_update")
            assert broadcast is not None, "ws_b did not observe pause broadcast"
            assert broadcast["payload"]["paused_at"] is not None

            # --- resume ---
            await ws_a.send_json({
                "v": 2, "type": "probe_timer_request", "requestId": "t4",
                "payload": {
                    "address": _TEST_DEVICE, "probe_index": 1, "action": "resume",
                },
            })
            ack = await _drain_until(ws_a, "probe_timer_ack")
            assert ack.get("type") == "probe_timer_ack", ack
            assert ack["payload"]["paused_at"] is None
            assert ack["payload"]["started_at"] is not None

            broadcast = await _wait_for_broadcast(ws_b, "probe_timer_update")
            assert broadcast is not None, "ws_b did not observe resume broadcast"
            assert broadcast["payload"]["started_at"] is not None

            # --- reset ---
            await ws_a.send_json({
                "v": 2, "type": "probe_timer_request", "requestId": "t5",
                "payload": {
                    "address": _TEST_DEVICE, "probe_index": 1, "action": "reset",
                },
            })
            ack = await _drain_until(ws_a, "probe_timer_ack")
            assert ack.get("type") == "probe_timer_ack", ack
            assert ack["payload"]["accumulated_secs"] == 0
            assert ack["payload"]["started_at"] is None
            assert ack["payload"]["paused_at"] is None

            broadcast = await _wait_for_broadcast(ws_b, "probe_timer_update")
            assert broadcast is not None, "ws_b did not observe reset broadcast"
            assert broadcast["payload"]["accumulated_secs"] == 0
    finally:
        broadcast_task.cancel()
        try:
            await broadcast_task
        except _asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_ws_probe_timer_no_active_session(client):
    """probe_timer_request with no active session returns no_active_session."""
    async with client.ws_connect("/ws") as ws:
        await ws.send_json({
            "v": 2, "type": "probe_timer_request", "requestId": "r1",
            "payload": {
                "address": _TEST_DEVICE,
                "probe_index": 1,
                "action": "start",
            },
        })
        msg = await ws.receive_json()
        while msg.get("type") not in ("error", "probe_timer_ack"):
            msg = await ws.receive_json()
        assert msg.get("type") == "error"
        assert msg["payload"]["code"] == "no_active_session"
        assert msg.get("requestId") == "r1"


@pytest.mark.asyncio
async def test_ws_probe_timer_invalid_action(client):
    """Unknown action strings produce an invalid_action error."""
    await _seed_device(client)
    async with client.ws_connect("/ws") as ws:
        await _start_session_for_timer(ws)
        await ws.send_json({
            "v": 2, "type": "probe_timer_request", "requestId": "r1",
            "payload": {
                "address": _TEST_DEVICE,
                "probe_index": 1,
                "action": "explode",
            },
        })
        msg = await ws.receive_json()
        while msg.get("type") not in ("error", "probe_timer_ack"):
            msg = await ws.receive_json()
        assert msg.get("type") == "error"
        assert msg["payload"]["code"] == "invalid_action"


@pytest.mark.asyncio
async def test_ws_probe_timer_invalid_mode(client):
    """Upserting with a mode other than count_up/count_down returns invalid_mode."""
    await _seed_device(client)
    async with client.ws_connect("/ws") as ws:
        await _start_session_for_timer(ws)
        await ws.send_json({
            "v": 2, "type": "probe_timer_request", "requestId": "r1",
            "payload": {
                "address": _TEST_DEVICE,
                "probe_index": 1,
                "action": "upsert",
                "mode": "countdown",  # missing underscore
                "duration_secs": 60,
            },
        })
        msg = await ws.receive_json()
        while msg.get("type") not in ("error", "probe_timer_ack"):
            msg = await ws.receive_json()
        assert msg.get("type") == "error"
        assert msg["payload"]["code"] == "invalid_mode"


@pytest.mark.asyncio
async def test_ws_probe_timer_countdown_requires_duration(client):
    """count_down mode without duration_secs is rejected."""
    await _seed_device(client)
    async with client.ws_connect("/ws") as ws:
        await _start_session_for_timer(ws)
        await ws.send_json({
            "v": 2, "type": "probe_timer_request", "requestId": "r1",
            "payload": {
                "address": _TEST_DEVICE,
                "probe_index": 1,
                "action": "upsert",
                "mode": "count_down",
                # duration_secs deliberately omitted
            },
        })
        msg = await ws.receive_json()
        while msg.get("type") not in ("error", "probe_timer_ack"):
            msg = await ws.receive_json()
        assert msg.get("type") == "error"
        assert msg["payload"]["code"] == "invalid_mode"


# ---------------------------------------------------------------------------
# session_notes_update_request handler tests (Task 11)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ws_session_notes_update_active_session(client):
    """session_notes_update_request with no sessionId uses the active session,
    acks the requester with the note row, broadcasts session_notes_update to
    other clients, and the body is visible via GET /api/sessions/{id}."""
    import asyncio as _asyncio
    from service.api.websocket import broadcast_events

    await _seed_device(client)
    broadcast_task = _asyncio.create_task(broadcast_events(client.app))
    try:
        async with client.ws_connect("/ws") as ws_a, client.ws_connect("/ws") as ws_b:
            await ws_a.send_json({
                "v": 2, "type": "session_start_request", "requestId": "r1",
                "payload": {
                    "name": "Notes Test",
                    "deviceAddresses": [_TEST_DEVICE],
                    "targets": [{"probe_index": 1, "mode": "fixed", "target_value": 90}],
                },
            })
            msg = await ws_a.receive_json()
            while msg.get("type") != "session_start_ack":
                msg = await ws_a.receive_json()
            session_id = msg["payload"]["sessionId"]

            # Drain the session_start broadcast on ws_b so the notes broadcast
            # is easy to isolate.
            for _ in range(5):
                try:
                    msg_b = await _asyncio.wait_for(ws_b.receive_json(), timeout=1.0)
                except _asyncio.TimeoutError:
                    break
                if msg_b.get("type") == "session_start":
                    break

            await ws_a.send_json({
                "v": 2, "type": "session_notes_update_request", "requestId": "n1",
                "payload": {"body": "Low and slow — oak wood."},
            })
            ack = await ws_a.receive_json()
            while ack.get("type") not in ("session_notes_update_ack", "error"):
                ack = await ws_a.receive_json()
            assert ack.get("type") == "session_notes_update_ack", ack
            assert ack["payload"]["session_id"] == session_id
            assert ack["payload"]["body"] == "Low and slow — oak wood."
            assert ack["payload"]["created_at"] is not None
            assert ack["payload"]["updated_at"] is not None

            saw_broadcast = False
            for _ in range(10):
                try:
                    msg_b = await _asyncio.wait_for(ws_b.receive_json(), timeout=1.0)
                except _asyncio.TimeoutError:
                    break
                if msg_b.get("type") == "session_notes_update":
                    assert msg_b["payload"]["session_id"] == session_id
                    assert msg_b["payload"]["body"] == "Low and slow — oak wood."
                    saw_broadcast = True
                    break
            assert saw_broadcast, "ws_b did not observe session_notes_update broadcast"

        resp = await client.get(f"/api/sessions/{session_id}")
        assert resp.status == 200
        body = await resp.json()
        # Legacy string form of the primary note.
        assert body["notesBody"] == "Low and slow — oak wood."
        # New notes array form.
        assert isinstance(body["notes"], list)
        assert len(body["notes"]) == 1
        assert body["notes"][0]["body"] == "Low and slow — oak wood."
    finally:
        broadcast_task.cancel()
        try:
            await broadcast_task
        except _asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_ws_session_notes_update_after_session_ends(client):
    """Notes remain editable after the session ends: the explicit sessionId
    path works on an ended session."""
    import asyncio as _asyncio
    from service.api.websocket import broadcast_events

    await _seed_device(client)
    broadcast_task = _asyncio.create_task(broadcast_events(client.app))
    try:
        async with client.ws_connect("/ws") as ws_a, client.ws_connect("/ws") as ws_b:
            await ws_a.send_json({
                "v": 2, "type": "session_start_request", "requestId": "r1",
                "payload": {
                    "name": "Ends First",
                    "deviceAddresses": [_TEST_DEVICE],
                    "targets": [{"probe_index": 1, "mode": "fixed", "target_value": 90}],
                },
            })
            msg = await ws_a.receive_json()
            while msg.get("type") != "session_start_ack":
                msg = await ws_a.receive_json()
            session_id = msg["payload"]["sessionId"]

            # First write a note while the session is active to establish the row.
            await ws_a.send_json({
                "v": 2, "type": "session_notes_update_request", "requestId": "n1",
                "payload": {"sessionId": session_id, "body": "initial"},
            })
            ack = await ws_a.receive_json()
            while ack.get("type") not in ("session_notes_update_ack", "error"):
                ack = await ws_a.receive_json()
            assert ack.get("type") == "session_notes_update_ack"

            # End the session.
            await ws_a.send_json({
                "v": 2, "type": "session_end_request", "requestId": "r2", "payload": {},
            })
            msg = await ws_a.receive_json()
            while msg.get("type") != "session_end_ack":
                msg = await ws_a.receive_json()

            # Drain whatever ws_b has queued so we can isolate the next broadcast.
            for _ in range(20):
                try:
                    await _asyncio.wait_for(ws_b.receive_json(), timeout=0.3)
                except _asyncio.TimeoutError:
                    break

            # Edit the note with an explicit sessionId (no active session now).
            await ws_a.send_json({
                "v": 2, "type": "session_notes_update_request", "requestId": "n2",
                "payload": {"sessionId": session_id, "body": "updated after end"},
            })
            ack = await ws_a.receive_json()
            while ack.get("type") not in ("session_notes_update_ack", "error"):
                ack = await ws_a.receive_json()
            assert ack.get("type") == "session_notes_update_ack", ack
            assert ack["payload"]["body"] == "updated after end"

            saw_broadcast = False
            for _ in range(10):
                try:
                    msg_b = await _asyncio.wait_for(ws_b.receive_json(), timeout=1.0)
                except _asyncio.TimeoutError:
                    break
                if msg_b.get("type") == "session_notes_update":
                    assert msg_b["payload"]["body"] == "updated after end"
                    saw_broadcast = True
                    break
            assert saw_broadcast, "ws_b did not observe post-end session_notes_update"

        resp = await client.get(f"/api/sessions/{session_id}")
        assert resp.status == 200
        body = await resp.json()
        assert body["notesBody"] == "updated after end"
        assert isinstance(body["notes"], list)
        assert len(body["notes"]) == 1
        assert body["notes"][0]["body"] == "updated after end"
    finally:
        broadcast_task.cancel()
        try:
            await broadcast_task
        except _asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_ws_session_notes_update_session_not_found(client):
    """Explicit sessionId pointing at a non-existent session returns
    session_not_found."""
    async with client.ws_connect("/ws") as ws:
        await ws.send_json({
            "v": 2, "type": "session_notes_update_request", "requestId": "n1",
            "payload": {"sessionId": "does-not-exist", "body": "anything"},
        })
        msg = await ws.receive_json()
        while msg.get("type") not in ("error", "session_notes_update_ack"):
            msg = await ws.receive_json()
        assert msg.get("type") == "error"
        assert msg["payload"]["code"] == "session_not_found"
        assert msg.get("requestId") == "n1"


@pytest.mark.asyncio
async def test_ws_session_notes_update_missing_body(client):
    """Missing body is rejected with invalid_payload, even when an active
    session exists."""
    await _seed_device(client)
    async with client.ws_connect("/ws") as ws:
        await ws.send_json({
            "v": 2, "type": "session_start_request", "requestId": "r1",
            "payload": {
                "name": "Missing Body",
                "deviceAddresses": [_TEST_DEVICE],
                "targets": [{"probe_index": 1, "mode": "fixed", "target_value": 90}],
            },
        })
        msg = await ws.receive_json()
        while msg.get("type") != "session_start_ack":
            msg = await ws.receive_json()

        await ws.send_json({
            "v": 2, "type": "session_notes_update_request", "requestId": "n1",
            "payload": {},
        })
        msg = await ws.receive_json()
        while msg.get("type") not in ("error", "session_notes_update_ack"):
            msg = await ws.receive_json()
        assert msg.get("type") == "error"
        assert msg["payload"]["code"] == "invalid_payload"


@pytest.mark.asyncio
async def test_ws_session_notes_update_no_session_specified(client):
    """No active session and no sessionId returns no_session_specified."""
    async with client.ws_connect("/ws") as ws:
        await ws.send_json({
            "v": 2, "type": "session_notes_update_request", "requestId": "n1",
            "payload": {"body": "orphan note"},
        })
        msg = await ws.receive_json()
        while msg.get("type") not in ("error", "session_notes_update_ack"):
            msg = await ws.receive_json()
        assert msg.get("type") == "error"
        assert msg["payload"]["code"] == "no_session_specified"


@pytest.mark.asyncio
async def test_ws_probe_timer_start_without_upsert(client):
    """start on a probe that has no timer row returns timer_not_found."""
    await _seed_device(client)
    async with client.ws_connect("/ws") as ws:
        await _start_session_for_timer(ws)
        await ws.send_json({
            "v": 2, "type": "probe_timer_request", "requestId": "r1",
            "payload": {
                "address": _TEST_DEVICE,
                "probe_index": 7,  # never upserted
                "action": "start",
            },
        })
        msg = await ws.receive_json()
        while msg.get("type") not in ("error", "probe_timer_ack"):
            msg = await ws.receive_json()
        assert msg.get("type") == "error"
        assert msg["payload"]["code"] == "timer_not_found"


# ---------------------------------------------------------------------------
# Countdown auto-completion (Task 12)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_countdown_completer_auto_completes_and_broadcasts(client):
    """When a running count_down timer's effective elapsed exceeds
    duration_secs, the completer tick() must mark it complete AND broadcast
    probe_timer_update exactly once — observable by a second WebSocket
    client."""
    import asyncio as _asyncio
    from service.api.websocket import broadcast_events
    from service.timers import CountdownCompleter

    await _seed_device(client)
    broadcast_task = _asyncio.create_task(broadcast_events(client.app))
    try:
        async with client.ws_connect("/ws") as ws_a, client.ws_connect("/ws") as ws_b:
            await _start_session_for_timer(ws_a)

            # Upsert a 1-second count_down and start it.
            await ws_a.send_json({
                "v": 2, "type": "probe_timer_request", "requestId": "c1",
                "payload": {
                    "address": _TEST_DEVICE,
                    "probe_index": 1,
                    "action": "upsert",
                    "mode": "count_down",
                    "duration_secs": 1,
                },
            })
            msg = await ws_a.receive_json()
            while msg.get("type") != "probe_timer_ack":
                msg = await ws_a.receive_json()

            await ws_a.send_json({
                "v": 2, "type": "probe_timer_request", "requestId": "c2",
                "payload": {
                    "address": _TEST_DEVICE,
                    "probe_index": 1,
                    "action": "start",
                },
            })
            msg = await ws_a.receive_json()
            while msg.get("type") != "probe_timer_ack":
                msg = await ws_a.receive_json()

            # Drain any already-queued probe_timer_update broadcasts on ws_b
            # from the upsert + start (so we can cleanly detect the
            # auto-complete broadcast below).
            async def _drain(ws, timeout=0.3):
                try:
                    while True:
                        await _asyncio.wait_for(ws.receive_json(), timeout=timeout)
                except _asyncio.TimeoutError:
                    pass

            await _drain(ws_b)

            # Wait slightly longer than the 1-second duration.
            await _asyncio.sleep(1.2)

            # Run one tick of the completer directly (deterministic, no loop).
            completer = CountdownCompleter(
                client.app["history"], client.app["store"],
            )
            completed = await completer.tick()
            assert completed == 1, (
                f"Expected one timer auto-completed, got {completed}"
            )

            # ws_b should now see the probe_timer_update broadcast.
            async def _wait_for_broadcast(ws, wanted_type):
                for _ in range(20):
                    try:
                        m = await _asyncio.wait_for(ws.receive_json(), timeout=1.0)
                    except _asyncio.TimeoutError:
                        return None
                    if m.get("type") == wanted_type:
                        return m
                return None

            broadcast = await _wait_for_broadcast(ws_b, "probe_timer_update")
            assert broadcast is not None, (
                "ws_b did not observe auto-complete probe_timer_update broadcast"
            )
            payload = broadcast["payload"]
            assert payload["probe_index"] == 1
            assert payload["completed_at"] is not None
            assert payload["started_at"] is None
            assert payload["paused_at"] is not None

            # Second tick should be a no-op (timer is already completed).
            assert await completer.tick() == 0
    finally:
        broadcast_task.cancel()
        try:
            await broadcast_task
        except _asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# session_start_request — target_duration_secs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ws_session_start_with_target_duration_secs(client):
    """session_start_request accepts targetDurationSecs; the ack,
    status_request response, and GET /api/sessions/{id} all expose it."""
    await _seed_device(client)
    async with client.ws_connect("/ws") as ws:
        await ws.send_json({
            "v": 2, "type": "session_start_request", "requestId": "r1",
            "payload": {
                "name": "Long cook",
                "deviceAddresses": [_TEST_DEVICE],
                "targetDurationSecs": 3600,
            },
        })
        msg = await ws.receive_json()
        while msg.get("type") != "session_start_ack":
            msg = await ws.receive_json()
        assert msg["payload"]["targetDurationSecs"] == 3600
        session_id = msg["payload"]["sessionId"]

        # status_request should echo it on the active session.
        await ws.send_json({
            "v": 2, "type": "status_request", "requestId": "r2", "payload": {},
        })
        status = await ws.receive_json()
        while status.get("type") != "status":
            status = await ws.receive_json()
        assert status["payload"]["currentSessionId"] == session_id
        assert status["payload"]["currentTargetDurationSecs"] == 3600

    # REST detail endpoint exposes targetDurationSecs.
    resp = await client.get(f"/api/sessions/{session_id}")
    assert resp.status == 200
    body = await resp.json()
    assert body["targetDurationSecs"] == 3600


@pytest.mark.asyncio
async def test_ws_session_start_without_target_duration_secs_is_null(client):
    """Omitting targetDurationSecs must persist NULL; the ack, status,
    and REST detail all expose the field as null (not missing)."""
    await _seed_device(client)
    async with client.ws_connect("/ws") as ws:
        await ws.send_json({
            "v": 2, "type": "session_start_request", "requestId": "r1",
            "payload": {
                "deviceAddresses": [_TEST_DEVICE],
            },
        })
        msg = await ws.receive_json()
        while msg.get("type") != "session_start_ack":
            msg = await ws.receive_json()
        assert "targetDurationSecs" in msg["payload"]
        assert msg["payload"]["targetDurationSecs"] is None
        session_id = msg["payload"]["sessionId"]

        await ws.send_json({
            "v": 2, "type": "status_request", "requestId": "r2", "payload": {},
        })
        status = await ws.receive_json()
        while status.get("type") != "status":
            status = await ws.receive_json()
        assert "currentTargetDurationSecs" in status["payload"]
        assert status["payload"]["currentTargetDurationSecs"] is None

    resp = await client.get(f"/api/sessions/{session_id}")
    assert resp.status == 200
    body = await resp.json()
    assert "targetDurationSecs" in body
    assert body["targetDurationSecs"] is None


@pytest.mark.asyncio
async def test_ws_session_start_rejects_non_integer_target_duration_secs(client):
    """targetDurationSecs must be an integer; strings/floats/<=0 are rejected
    with an invalid_payload error and no session is created."""
    await _seed_device(client)
    async with client.ws_connect("/ws") as ws:
        await ws.send_json({
            "v": 2, "type": "session_start_request", "requestId": "r1",
            "payload": {
                "deviceAddresses": [_TEST_DEVICE],
                "targetDurationSecs": "not-a-number",
            },
        })
        msg = await ws.receive_json()
        while msg.get("type") not in ("error", "session_start_ack"):
            msg = await ws.receive_json()
        assert msg.get("type") == "error"
        assert msg["payload"]["code"] == "invalid_payload"

        await ws.send_json({
            "v": 2, "type": "session_start_request", "requestId": "r2",
            "payload": {
                "deviceAddresses": [_TEST_DEVICE],
                "targetDurationSecs": 0,
            },
        })
        msg = await ws.receive_json()
        while msg.get("type") not in ("error", "session_start_ack"):
            msg = await ws.receive_json()
        assert msg.get("type") == "error"
        assert msg["payload"]["code"] == "invalid_payload"
