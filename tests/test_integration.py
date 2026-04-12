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
