"""Tests for HTTP route handlers."""

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
