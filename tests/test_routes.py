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


# ---------------------------------------------------------------------------
# Session detail + export: timers + notes bundle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_detail_includes_timers_and_notes(
    client, store, sample_address,
):
    """GET /api/sessions/{id} returns timers + notes arrays alongside
    the legacy notesBody string and the targetDurationSecs field."""
    start = await store.start_session(
        addresses=[sample_address], reason="user",
        target_duration_secs=3600,
    )
    sid = start["session_id"]

    # A timer on probe 1.
    await store.upsert_timer(
        session_id=sid, address=sample_address, probe_index=1,
        mode="count_down", duration_secs=1800,
    )
    # Primary note (dual-written to sessions.notes).
    await store.upsert_primary_note(sid, "Oak and cherry.")

    resp = await client.get(f"/api/sessions/{sid}")
    assert resp.status == 200
    data = await resp.json()

    assert data["sessionId"] == sid
    assert data["targetDurationSecs"] == 3600
    # Backwards-compat string form of the primary note.
    assert data["notesBody"] == "Oak and cherry."

    # New timers array.
    assert isinstance(data["timers"], list)
    assert len(data["timers"]) == 1
    timer = data["timers"][0]
    assert timer["address"] == sample_address
    assert timer["probeIndex"] == 1
    assert timer["mode"] == "count_down"
    assert timer["durationSecs"] == 1800
    assert timer["startedAt"] is None
    assert timer["pausedAt"] is None
    assert timer["accumulatedSecs"] == 0
    assert timer["completedAt"] is None

    # New notes array.
    assert isinstance(data["notes"], list)
    assert len(data["notes"]) == 1
    note = data["notes"][0]
    assert note["body"] == "Oak and cherry."
    assert isinstance(note["id"], int)
    assert note["createdAt"] is not None
    assert note["updatedAt"] is not None


@pytest.mark.asyncio
async def test_session_detail_empty_timers_and_notes(
    client, store, sample_address,
):
    """Sessions with no timers or notes return empty arrays, not null."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]
    resp = await client.get(f"/api/sessions/{sid}")
    assert resp.status == 200
    data = await resp.json()
    assert data["timers"] == []
    assert data["notes"] == []
    assert data["notesBody"] is None


@pytest.mark.asyncio
async def test_export_json_includes_timers_and_notes(
    client, store, sample_address,
):
    """/export?format=json returns the full bundle including timers + notes."""
    start = await store.start_session(
        addresses=[sample_address], reason="user",
        target_duration_secs=7200,
    )
    sid = start["session_id"]
    await store.upsert_timer(
        session_id=sid, address=sample_address, probe_index=2,
        mode="count_up",
    )
    await store.upsert_primary_note(sid, "Slow smoke.")

    resp = await client.get(f"/api/sessions/{sid}/export?format=json")
    assert resp.status == 200
    data = await resp.json()

    assert data["sessionId"] == sid
    assert data["targetDurationSecs"] == 7200
    assert data["notesBody"] == "Slow smoke."
    assert len(data["timers"]) == 1
    assert data["timers"][0]["probeIndex"] == 2
    assert data["timers"][0]["mode"] == "count_up"
    assert data["timers"][0]["durationSecs"] is None
    assert len(data["notes"]) == 1
    assert data["notes"][0]["body"] == "Slow smoke."


@pytest.mark.asyncio
async def test_export_csv_resource_timers(client, store, sample_address):
    """?format=csv&resource=timers returns a timers CSV."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]
    await store.upsert_timer(
        session_id=sid, address=sample_address, probe_index=1,
        mode="count_down", duration_secs=600,
    )
    await store.upsert_timer(
        session_id=sid, address=sample_address, probe_index=2,
        mode="count_up",
    )

    resp = await client.get(
        f"/api/sessions/{sid}/export?format=csv&resource=timers",
    )
    assert resp.status == 200
    assert resp.content_type == "text/csv"
    text = await resp.text()
    lines = text.strip().splitlines()
    assert lines[0] == (
        "address,probe_index,mode,duration_secs,started_at,paused_at,"
        "accumulated_secs,completed_at"
    )
    assert len(lines) == 3  # header + 2 timers
    assert "count_down" in lines[1]
    assert "600" in lines[1]
    assert "count_up" in lines[2]


@pytest.mark.asyncio
async def test_export_csv_resource_notes(client, store, sample_address):
    """?format=csv&resource=notes returns a notes CSV."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]
    await store.upsert_primary_note(sid, "First note.")

    resp = await client.get(
        f"/api/sessions/{sid}/export?format=csv&resource=notes",
    )
    assert resp.status == 200
    assert resp.content_type == "text/csv"
    text = await resp.text()
    lines = text.strip().splitlines()
    assert lines[0] == "id,created_at,updated_at,body"
    assert len(lines) == 2  # header + 1 note
    assert "First note." in lines[1]


@pytest.mark.asyncio
async def test_export_csv_default_resource_still_readings(
    client, store, sample_address,
):
    """Backwards-compat: ?format=csv (no resource param) still returns the
    readings CSV shape."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]
    await store.record_reading(
        session_id=sid, address=sample_address, seq=1,
        probes=[{"index": 1, "temperature": 72.5}],
        battery=85, propane=None, heating=None,
    )
    resp = await client.get(f"/api/sessions/{sid}/export?format=csv")
    assert resp.status == 200
    text = await resp.text()
    assert text.splitlines()[0].startswith(
        "timestamp,probe_index,label,temperature_c,battery_pct,propane_pct",
    )


@pytest.mark.asyncio
async def test_export_csv_unknown_resource_returns_400(
    client, store, sample_address,
):
    """An unknown ?resource= value returns 400."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]
    resp = await client.get(
        f"/api/sessions/{sid}/export?format=csv&resource=bogus",
    )
    assert resp.status == 400
    data = await resp.json()
    assert "bogus" in data["error"]
