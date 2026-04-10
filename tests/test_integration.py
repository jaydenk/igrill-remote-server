"""Integration tests for the full server."""

import pytest
import pytest_asyncio
from service.main import create_app
from service.config import Config


@pytest.fixture
def config(tmp_db):
    return Config(db_path=tmp_db)


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
