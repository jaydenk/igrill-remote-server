"""Tests for HTTP route handlers."""

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from aiohttp import web

from service.api.routes import setup_routes
from service.models.device import DeviceStore


@pytest_asyncio.fixture
async def client(store, aiohttp_client):
    """Create an aiohttp test client with routes and a real HistoryStore."""
    application = web.Application()
    application["history"] = store
    application["store"] = DeviceStore()
    application["config"] = type("Config", (), {"poll_interval": 15, "scan_interval": 60})()
    application["start_time"] = 0
    setup_routes(application)
    return await aiohttp_client(application)


@pytest_asyncio.fixture
async def client_with_push(store, aiohttp_client):
    """Create an aiohttp test client with a mock push_service."""
    application = web.Application()
    application["history"] = store
    application["store"] = DeviceStore()
    application["config"] = type("Config", (), {"poll_interval": 15, "scan_interval": 60})()
    application["start_time"] = 0
    mock_push = AsyncMock()
    application["push_service"] = mock_push
    setup_routes(application)
    return await aiohttp_client(application), mock_push


@pytest.mark.asyncio
async def test_export_csv_produces_data(client, store, sample_address):
    """CSV export should produce rows from flat probe readings."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]
    await store.record_reading(
        session_id=sid, address=sample_address, seq=1,
        probes=[
            {"index": 1, "temperature": 72.5},
            {"index": 2, "temperature": 80.0},
        ],
        battery=85, propane=None, heating=None,
    )

    resp = await client.get(f"/api/sessions/{sid}/export?format=csv")
    assert resp.status == 200
    text = await resp.text()
    lines = text.strip().split("\n")
    assert len(lines) == 3  # header + 2 probe rows
    assert "72.5" in lines[1]
    assert "80.0" in lines[2]


@pytest.mark.asyncio
async def test_export_json_includes_labels(client, store, sample_address):
    """JSON export should enrich readings with target labels."""
    from service.models.session import TargetConfig
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]
    await store.record_reading(
        session_id=sid, address=sample_address, seq=1,
        probes=[{"index": 1, "temperature": 72.5}],
        battery=85, propane=None, heating=None,
    )
    await store.save_targets(sid, sample_address, [
        TargetConfig(probe_index=1, mode="fixed", target_value=74.0, label="Brisket"),
    ])

    resp = await client.get(f"/api/sessions/{sid}/export?format=json")
    assert resp.status == 200
    data = await resp.json()
    assert len(data["readings"]) == 1
    assert data["readings"][0].get("label") == "Brisket"


# ---------------------------------------------------------------------------
# Push token endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_push_token(client):
    """POST with valid token returns 200 and ok=True."""
    resp = await client.post(
        "/api/v1/devices/push-token",
        json={"token": "abc123hextoken"},
    )
    assert resp.status == 200
    data = await resp.json()
    assert data == {"ok": True}


@pytest.mark.asyncio
async def test_register_with_la_token(client_with_push):
    """POST with token + liveActivityToken returns 200 and calls upsert_token."""
    test_client, mock_push = client_with_push
    resp = await test_client.post(
        "/api/v1/devices/push-token",
        json={"token": "abc123hextoken", "liveActivityToken": "la_hex_token"},
    )
    assert resp.status == 200
    data = await resp.json()
    assert data == {"ok": True}
    mock_push.upsert_token.assert_awaited_once_with(
        "abc123hextoken", live_activity_token="la_hex_token",
    )


@pytest.mark.asyncio
async def test_register_missing_token(client):
    """POST with empty body returns 400."""
    resp = await client.post(
        "/api/v1/devices/push-token",
        json={},
    )
    assert resp.status == 400
    data = await resp.json()
    assert "token" in data["error"].lower()


@pytest.mark.asyncio
async def test_register_invalid_json(client):
    """POST with non-JSON body returns 400."""
    resp = await client.post(
        "/api/v1/devices/push-token",
        data=b"not json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 400
    data = await resp.json()
    assert "json" in data["error"].lower()
