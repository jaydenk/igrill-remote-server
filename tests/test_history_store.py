"""Tests for the rewritten HistoryStore."""

import pytest

from service.history.store import HistoryStore


@pytest.mark.asyncio
async def test_no_session_on_startup(store):
    state = await store.get_session_state()
    assert state["current_session_id"] is None


@pytest.mark.asyncio
async def test_start_session(store):
    result = await store.start_session(
        addresses=["70:91:8F:00:00:01"],
        reason="user",
    )
    assert result["session_id"] is not None
    assert result["start_event"]["reason"] == "user"
    state = await store.get_session_state()
    assert state["current_session_id"] == result["session_id"]


@pytest.mark.asyncio
async def test_end_session(store):
    start = await store.start_session(addresses=["70:91:8F:00:00:01"], reason="user")
    result = await store.end_session(reason="user")
    assert result is not None
    assert result["sessionId"] == start["session_id"]
    state = await store.get_session_state()
    assert state["current_session_id"] is None


@pytest.mark.asyncio
async def test_end_session_when_none_active(store):
    result = await store.end_session(reason="user")
    assert result is None


@pytest.mark.asyncio
async def test_record_reading_during_session(store):
    start = await store.start_session(addresses=["70:91:8F:00:00:01"], reason="user")
    await store.record_reading(
        session_id=start["session_id"],
        address="70:91:8F:00:00:01",
        seq=1,
        probes=[{"index": 1, "temperature": 72.5}],
        battery=85,
        propane=None,
        heating=None,
    )
    items = await store.get_session_readings(start["session_id"])
    assert len(items) == 1


@pytest.mark.asyncio
async def test_multi_device_session(store):
    result = await store.start_session(
        addresses=["70:91:8F:00:00:01", "70:91:8F:00:00:02"],
        reason="user",
    )
    devices = await store.get_session_devices(result["session_id"])
    assert len(devices) == 2


@pytest.mark.asyncio
async def test_register_device(store):
    await store.register_device(address="70:91:8F:00:00:01", name="Kitchen iGrill", model="iGrill_V3")
    devices = await store.list_devices()
    assert len(devices) == 1
    assert devices[0]["address"] == "70:91:8F:00:00:01"


@pytest.mark.asyncio
async def test_device_leave_and_rejoin(store):
    start = await store.start_session(addresses=["70:91:8F:00:00:01"], reason="user")
    await store.device_left_session(session_id=start["session_id"], address="70:91:8F:00:00:01")
    devices = await store.get_session_devices(start["session_id"])
    assert devices[0]["left_at"] is not None
    await store.device_rejoined_session(session_id=start["session_id"], address="70:91:8F:00:00:01")
    devices = await store.get_session_devices(start["session_id"])
    assert devices[0]["left_at"] is None


@pytest.mark.asyncio
async def test_list_sessions(store):
    await store.start_session(addresses=["70:91:8F:00:00:01"], reason="user")
    await store.end_session(reason="user")
    sessions = await store.list_sessions(limit=10)
    assert len(sessions) == 1


@pytest.mark.asyncio
async def test_save_and_get_targets(store):
    from service.models.session import TargetConfig
    start = await store.start_session(addresses=["70:91:8F:00:00:01"], reason="user")
    targets = [TargetConfig(probe_index=1, mode="fixed", target_value=74.0)]
    await store.save_targets(start["session_id"], "70:91:8F:00:00:01", targets)
    loaded = await store.get_targets(start["session_id"])
    assert len(loaded) == 1
    assert loaded[0].target_value == 74.0


@pytest.mark.asyncio
async def test_start_session_ends_previous(store):
    r1 = await store.start_session(addresses=["70:91:8F:00:00:01"], reason="user")
    r2 = await store.start_session(addresses=["70:91:8F:00:00:01"], reason="user")
    assert r1["session_id"] != r2["session_id"]
    assert r2.get("end_event") is not None


@pytest.mark.asyncio
async def test_all_devices_left(store):
    start = await store.start_session(
        addresses=["70:91:8F:00:00:01", "70:91:8F:00:00:02"],
        reason="user",
    )
    sid = start["session_id"]
    assert await store.all_devices_left(sid) is False
    await store.device_left_session(sid, "70:91:8F:00:00:01")
    assert await store.all_devices_left(sid) is False
    await store.device_left_session(sid, "70:91:8F:00:00:02")
    assert await store.all_devices_left(sid) is True


@pytest.mark.asyncio
async def test_is_session_active(store):
    assert await store.is_session_active() is False
    await store.start_session(addresses=["70:91:8F:00:00:01"], reason="user")
    assert await store.is_session_active() is True
    await store.end_session(reason="user")
    assert await store.is_session_active() is False


@pytest.mark.asyncio
async def test_record_multiple_probes(store):
    start = await store.start_session(addresses=["70:91:8F:00:00:01"], reason="user")
    await store.record_reading(
        session_id=start["session_id"],
        address="70:91:8F:00:00:01",
        seq=1,
        probes=[
            {"index": 1, "temperature": 72.5},
            {"index": 2, "temperature": 80.0},
            {"index": 3, "temperature": None},  # unplugged
        ],
        battery=90,
        propane=None,
        heating=None,
    )
    items = await store.get_session_readings(start["session_id"])
    # Should have 3 probe reading rows
    assert len(items) == 3


@pytest.mark.asyncio
async def test_register_device_idempotent(store):
    """Registering the same device twice should update, not duplicate."""
    await store.register_device(address="70:91:8F:00:00:01", name="Old Name", model="V2")
    await store.register_device(address="70:91:8F:00:00:01", name="New Name", model="V3")
    devices = await store.list_devices()
    assert len(devices) == 1
    assert devices[0]["name"] == "New Name"
    assert devices[0]["model"] == "V3"


@pytest.mark.asyncio
async def test_register_device_preserves_fields_on_none(store):
    """Registering with None name/model should not overwrite existing values."""
    await store.register_device(address="70:91:8F:00:00:01", name="My Grill", model="V3")
    await store.register_device(address="70:91:8F:00:00:01", name=None, model=None)
    devices = await store.list_devices()
    assert devices[0]["name"] == "My Grill"
    assert devices[0]["model"] == "V3"


@pytest.mark.asyncio
async def test_add_device_to_session(store):
    """Adding a device mid-session should create a session_devices entry."""
    start = await store.start_session(addresses=["70:91:8F:00:00:01"], reason="user")
    await store.add_device_to_session(start["session_id"], "70:91:8F:00:00:02")
    devices = await store.get_session_devices(start["session_id"])
    assert len(devices) == 2
    addresses = {d["address"] for d in devices}
    assert "70:91:8F:00:00:02" in addresses


@pytest.mark.asyncio
async def test_get_history_items_by_session(store):
    """get_history_items should filter by session_id."""
    s1 = await store.start_session(addresses=["70:91:8F:00:00:01"], reason="user")
    await store.record_reading(
        session_id=s1["session_id"],
        address="70:91:8F:00:00:01",
        seq=1,
        probes=[{"index": 1, "temperature": 72.5}],
        battery=85,
        propane=None,
        heating=None,
    )
    s2 = await store.start_session(addresses=["70:91:8F:00:00:01"], reason="user")
    await store.record_reading(
        session_id=s2["session_id"],
        address="70:91:8F:00:00:01",
        seq=1,
        probes=[{"index": 1, "temperature": 80.0}],
        battery=90,
        propane=None,
        heating=None,
    )
    items = await store.get_history_items(session_id=s1["session_id"])
    assert len(items) == 1
    assert items[0]["temperature"] == 72.5


@pytest.mark.asyncio
async def test_update_targets(store):
    """update_targets should replace existing targets for the device."""
    from service.models.session import TargetConfig
    start = await store.start_session(addresses=["70:91:8F:00:00:01"], reason="user")
    sid = start["session_id"]
    addr = "70:91:8F:00:00:01"

    await store.save_targets(sid, addr, [
        TargetConfig(probe_index=1, mode="fixed", target_value=74.0),
        TargetConfig(probe_index=2, mode="fixed", target_value=80.0),
    ])

    await store.update_targets(sid, addr, [
        TargetConfig(probe_index=1, mode="range", range_low=60.0, range_high=70.0),
    ])

    loaded = await store.get_targets(sid)
    assert len(loaded) == 1
    assert loaded[0].mode == "range"
    assert loaded[0].range_low == 60.0


@pytest.mark.asyncio
async def test_session_start_registers_devices(store):
    """start_session should register unknown devices automatically."""
    await store.start_session(addresses=["70:91:8F:00:00:01"], reason="user")
    devices = await store.list_devices()
    assert len(devices) == 1
    assert devices[0]["address"] == "70:91:8F:00:00:01"


@pytest.mark.asyncio
async def test_record_reading_with_heating(store):
    """record_reading should store heating data as JSON."""
    start = await store.start_session(addresses=["70:91:8F:00:00:01"], reason="user")
    heating = {"heating_actual1": 200, "heating_setpoint1": 225}
    await store.record_reading(
        session_id=start["session_id"],
        address="70:91:8F:00:00:01",
        seq=1,
        probes=[{"index": 1, "temperature": 72.5}],
        battery=85,
        propane=None,
        heating=heating,
    )
    items = await store.get_session_readings(start["session_id"])
    assert items[0]["heating"] == heating


@pytest.mark.asyncio
async def test_list_sessions_with_offset(store):
    """list_sessions should support pagination via offset."""
    for i in range(5):
        await store.start_session(addresses=["70:91:8F:00:00:01"], reason="user")
        await store.end_session(reason="user")
    sessions = await store.list_sessions(limit=2, offset=2)
    assert len(sessions) == 2


@pytest.mark.asyncio
async def test_start_session_with_name(store, sample_address):
    result = await store.start_session([sample_address], "user", name="Sunday Brisket")
    sid = result["session_id"]
    async with store._lock:
        cursor = await store._conn.execute("SELECT name FROM sessions WHERE id = ?", (sid,))
        row = await cursor.fetchone()
    assert row["name"] == "Sunday Brisket"


@pytest.mark.asyncio
async def test_update_session_name_and_notes(store, sample_address):
    result = await store.start_session([sample_address], "user")
    sid = result["session_id"]
    await store.update_session(sid, name="Sunday Brisket", notes="Oak and cherry.")
    async with store._lock:
        cursor = await store._conn.execute("SELECT name, notes FROM sessions WHERE id = ?", (sid,))
        row = await cursor.fetchone()
    assert row["name"] == "Sunday Brisket"
    assert row["notes"] == "Oak and cherry."


@pytest.mark.asyncio
async def test_update_session_partial(store, sample_address):
    result = await store.start_session([sample_address], "user", name="Original")
    sid = result["session_id"]
    await store.update_session(sid, notes="Added notes only")
    async with store._lock:
        cursor = await store._conn.execute("SELECT name, notes FROM sessions WHERE id = ?", (sid,))
        row = await cursor.fetchone()
    assert row["name"] == "Original"
    assert row["notes"] == "Added notes only"


@pytest.mark.asyncio
async def test_list_sessions_includes_name_notes(store, sample_address):
    await store.start_session([sample_address], "user", name="Cook 1")
    state = await store.get_session_state()
    await store.update_session(state["current_session_id"], notes="Tasty")
    sessions = await store.list_sessions(limit=5)
    assert sessions[0]["name"] == "Cook 1"
    assert sessions[0]["notes"] == "Tasty"


@pytest.mark.asyncio
async def test_recover_orphaned_sessions(tmp_db, sample_address):
    """Orphaned sessions (no ended_at) should be closed on recovery.

    Simulates a server restart by creating a session with one store
    instance, closing it without ending the session, then opening a
    fresh store (as startup would) and running recovery.
    """
    # Start a session then "crash" (close store without ending session)
    store1 = HistoryStore(tmp_db, reconnect_grace=60)
    await store1.connect()
    start = await store1.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]
    await store1.close()

    # "Restart" — new store instance, run recovery
    store2 = HistoryStore(tmp_db, reconnect_grace=60)
    await store2.connect()
    await store2.recover_orphaned_sessions()

    state = await store2.get_session_state()
    assert state["current_session_id"] is None

    sessions = await store2.list_sessions(limit=10)
    assert len(sessions) == 1
    assert sessions[0]["endReason"] == "server_restart"
    await store2.close()


@pytest.mark.asyncio
async def test_get_history_items_with_time_filter(store, sample_address):
    """get_history_items should filter by since_ts and until_ts."""
    from datetime import datetime, timezone, timedelta
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]

    base = datetime.now(timezone.utc) - timedelta(hours=2)
    for i in range(3):
        ts = (base + timedelta(minutes=i * 30)).isoformat()
        await store.record_reading(
            session_id=sid, address=sample_address, seq=i + 1,
            probes=[{"index": 1, "temperature": 70.0 + i}],
            battery=85, propane=None, heating=None, recorded_at=ts,
        )

    # Filter: only readings after the first
    since = (base + timedelta(minutes=15)).isoformat()
    items = await store.get_history_items(since_ts=since, session_id=sid)
    assert len(items) == 2


@pytest.mark.asyncio
async def test_get_history_items_with_limit(store, sample_address):
    """get_history_items should respect the limit parameter."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]

    for i in range(5):
        await store.record_reading(
            session_id=sid, address=sample_address, seq=i + 1,
            probes=[{"index": 1, "temperature": 70.0 + i}],
            battery=85, propane=None, heating=None,
        )

    items = await store.get_history_items(session_id=sid, limit=3)
    assert len(items) == 3


@pytest.mark.asyncio
async def test_duplicate_seq_preserves_original(store, sample_address):
    """INSERT OR IGNORE should keep the first reading, not overwrite."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]

    await store.record_reading(
        session_id=sid, address=sample_address, seq=1,
        probes=[{"index": 1, "temperature": 72.5}],
        battery=85, propane=None, heating=None,
    )
    await store.record_reading(
        session_id=sid, address=sample_address, seq=1,
        probes=[{"index": 1, "temperature": 99.9}],
        battery=50, propane=None, heating=None,
    )

    items = await store.get_session_readings(sid)
    assert len(items) == 1
    assert items[0]["temperature"] == 72.5
    assert items[0]["battery"] == 85
