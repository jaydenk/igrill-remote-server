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
    assert result["start_event"]["devices"] == ["70:91:8F:00:00:01"]
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


# ---------------------------------------------------------------------------
# Session timers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_timer_creates_paused_initial(store, sample_address):
    """upsert_timer should create a paused-initial row (all runtime fields null/0)."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]

    row = await store.upsert_timer(sid, sample_address, 1, mode="countdown", duration_secs=900)
    assert row["session_id"] == sid
    assert row["address"] == sample_address
    assert row["probe_index"] == 1
    assert row["mode"] == "countdown"
    assert row["duration_secs"] == 900
    assert row["started_at"] is None
    assert row["paused_at"] is None
    assert row["accumulated_secs"] == 0
    assert row["completed_at"] is None


@pytest.mark.asyncio
async def test_upsert_timer_replaces_existing_row(store, sample_address):
    """A second upsert resets runtime state and updates mode/duration."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]

    await store.upsert_timer(sid, sample_address, 1, mode="countdown", duration_secs=60)
    await store.start_timer(sid, sample_address, 1)
    # Now re-upsert
    row = await store.upsert_timer(sid, sample_address, 1, mode="stopwatch", duration_secs=None)
    assert row["mode"] == "stopwatch"
    assert row["duration_secs"] is None
    assert row["started_at"] is None
    assert row["paused_at"] is None
    assert row["accumulated_secs"] == 0
    assert row["completed_at"] is None


@pytest.mark.asyncio
async def test_upsert_timer_requires_active_session(store, sample_address):
    """upsert_timer on a historical (ended) session should raise."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]
    await store.end_session(reason="user")

    with pytest.raises(ValueError, match="active session"):
        await store.upsert_timer(sid, sample_address, 1, mode="countdown", duration_secs=60)


@pytest.mark.asyncio
async def test_upsert_timer_unknown_session(store, sample_address):
    """Upsert against a current-session-id that isn't in sessions should raise."""
    # No active session at all
    with pytest.raises(ValueError, match="active session"):
        await store.upsert_timer("nonexistent", sample_address, 1, mode="countdown")


@pytest.mark.asyncio
async def test_start_timer_sets_started_at(store, sample_address):
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]
    await store.upsert_timer(sid, sample_address, 1, mode="countdown", duration_secs=60)
    row = await store.start_timer(sid, sample_address, 1)
    assert row["started_at"] is not None
    assert row["paused_at"] is None


@pytest.mark.asyncio
async def test_start_timer_idempotent(store, sample_address):
    """Calling start_timer twice should not reset started_at."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]
    await store.upsert_timer(sid, sample_address, 1, mode="countdown", duration_secs=60)

    row1 = await store.start_timer(sid, sample_address, 1)
    row2 = await store.start_timer(sid, sample_address, 1)
    assert row1["started_at"] == row2["started_at"]


@pytest.mark.asyncio
async def test_start_timer_missing_row(store, sample_address):
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]
    with pytest.raises(ValueError, match="Timer not found"):
        await store.start_timer(sid, sample_address, 1)


@pytest.mark.asyncio
async def test_start_timer_requires_active_session(store, sample_address):
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]
    await store.upsert_timer(sid, sample_address, 1, mode="countdown", duration_secs=60)
    await store.end_session(reason="user")
    with pytest.raises(ValueError, match="active session"):
        await store.start_timer(sid, sample_address, 1)


@pytest.mark.asyncio
async def test_pause_timer_accumulates_elapsed(store, sample_address, monkeypatch):
    """Pausing a running timer should add integer-second elapsed to accumulated_secs."""
    from datetime import datetime, timezone, timedelta
    from service.history import store as store_mod

    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]
    await store.upsert_timer(sid, sample_address, 1, mode="countdown", duration_secs=600)

    t0 = datetime(2026, 4, 12, 10, 0, 0, tzinfo=timezone.utc)
    times = [t0, t0 + timedelta(seconds=30)]

    def fake_now_iso():
        return times.pop(0).isoformat()

    monkeypatch.setattr(store_mod, "now_iso_utc", fake_now_iso)

    await store.start_timer(sid, sample_address, 1)
    row = await store.pause_timer(sid, sample_address, 1)

    assert row["started_at"] is None
    assert row["paused_at"] is not None
    assert row["accumulated_secs"] == 30


@pytest.mark.asyncio
async def test_pause_resume_pause_accumulates(store, sample_address, monkeypatch):
    """A pause->resume->pause cycle should sum both running intervals."""
    from datetime import datetime, timezone, timedelta
    from service.history import store as store_mod

    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]
    await store.upsert_timer(sid, sample_address, 1, mode="countdown", duration_secs=600)

    t0 = datetime(2026, 4, 12, 10, 0, 0, tzinfo=timezone.utc)
    # start, pause (30s later), resume (60s later), pause (75s later)
    times = [
        t0,
        t0 + timedelta(seconds=30),
        t0 + timedelta(seconds=60),
        t0 + timedelta(seconds=75),
    ]

    def fake_now_iso():
        return times.pop(0).isoformat()

    monkeypatch.setattr(store_mod, "now_iso_utc", fake_now_iso)

    await store.start_timer(sid, sample_address, 1)
    await store.pause_timer(sid, sample_address, 1)
    await store.resume_timer(sid, sample_address, 1)
    row = await store.pause_timer(sid, sample_address, 1)

    # First interval: 30s; second interval: 75-60 = 15s; total = 45s
    assert row["accumulated_secs"] == 45


@pytest.mark.asyncio
async def test_pause_timer_noop_when_not_running(store, sample_address):
    """Pausing a never-started or already-paused timer returns the row unchanged."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]
    await store.upsert_timer(sid, sample_address, 1, mode="countdown", duration_secs=60)

    # Never started
    row = await store.pause_timer(sid, sample_address, 1)
    assert row["paused_at"] is None
    assert row["accumulated_secs"] == 0

    # Already paused
    await store.start_timer(sid, sample_address, 1)
    paused = await store.pause_timer(sid, sample_address, 1)
    paused_at_first = paused["paused_at"]
    again = await store.pause_timer(sid, sample_address, 1)
    assert again["paused_at"] == paused_at_first
    assert again["accumulated_secs"] == paused["accumulated_secs"]


@pytest.mark.asyncio
async def test_resume_timer_clears_paused(store, sample_address):
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]
    await store.upsert_timer(sid, sample_address, 1, mode="countdown", duration_secs=60)
    await store.start_timer(sid, sample_address, 1)
    await store.pause_timer(sid, sample_address, 1)
    row = await store.resume_timer(sid, sample_address, 1)
    assert row["paused_at"] is None
    assert row["started_at"] is not None


@pytest.mark.asyncio
async def test_resume_timer_noop_when_not_paused(store, sample_address):
    """Resuming an un-paused timer returns the current row unchanged."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]
    await store.upsert_timer(sid, sample_address, 1, mode="countdown", duration_secs=60)

    # Never started
    row = await store.resume_timer(sid, sample_address, 1)
    assert row["started_at"] is None
    assert row["paused_at"] is None

    # Running
    started = await store.start_timer(sid, sample_address, 1)
    again = await store.resume_timer(sid, sample_address, 1)
    assert again["started_at"] == started["started_at"]


@pytest.mark.asyncio
async def test_reset_timer_clears_all_runtime_fields(store, sample_address):
    """reset_timer clears started/paused/completed and zeros accumulated_secs."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]
    await store.upsert_timer(sid, sample_address, 1, mode="countdown", duration_secs=60)
    await store.start_timer(sid, sample_address, 1)
    await store.complete_timer(sid, sample_address, 1)

    row = await store.reset_timer(sid, sample_address, 1)
    assert row["started_at"] is None
    assert row["paused_at"] is None
    assert row["completed_at"] is None
    assert row["accumulated_secs"] == 0
    # mode + duration preserved
    assert row["mode"] == "countdown"
    assert row["duration_secs"] == 60


@pytest.mark.asyncio
async def test_complete_timer_sets_completed_and_paused(store, sample_address, monkeypatch):
    """complete_timer sets both completed_at and paused_at, accumulates elapsed."""
    from datetime import datetime, timezone, timedelta
    from service.history import store as store_mod

    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]
    await store.upsert_timer(sid, sample_address, 1, mode="countdown", duration_secs=600)

    t0 = datetime(2026, 4, 12, 10, 0, 0, tzinfo=timezone.utc)
    times = [t0, t0 + timedelta(seconds=42)]

    def fake_now_iso():
        return times.pop(0).isoformat()

    monkeypatch.setattr(store_mod, "now_iso_utc", fake_now_iso)

    await store.start_timer(sid, sample_address, 1)
    row = await store.complete_timer(sid, sample_address, 1)

    assert row["completed_at"] is not None
    assert row["paused_at"] is not None
    assert row["started_at"] is None
    assert row["accumulated_secs"] == 42


@pytest.mark.asyncio
async def test_complete_timer_preserves_existing_completed_at(store, sample_address):
    """A second complete_timer call should not overwrite completed_at."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]
    await store.upsert_timer(sid, sample_address, 1, mode="countdown", duration_secs=60)
    await store.start_timer(sid, sample_address, 1)

    first = await store.complete_timer(sid, sample_address, 1)
    second = await store.complete_timer(sid, sample_address, 1)
    assert first["completed_at"] == second["completed_at"]


@pytest.mark.asyncio
async def test_reset_after_complete_clears_completed_at(store, sample_address):
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]
    await store.upsert_timer(sid, sample_address, 1, mode="countdown", duration_secs=60)
    await store.start_timer(sid, sample_address, 1)
    await store.complete_timer(sid, sample_address, 1)

    row = await store.reset_timer(sid, sample_address, 1)
    assert row["completed_at"] is None


@pytest.mark.asyncio
async def test_get_timers_empty_and_populated(store, sample_address):
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]

    timers = await store.get_timers(sid)
    assert timers == []

    await store.upsert_timer(sid, sample_address, 1, mode="countdown", duration_secs=60)
    await store.upsert_timer(sid, sample_address, 2, mode="stopwatch", duration_secs=None)

    timers = await store.get_timers(sid)
    assert len(timers) == 2
    probe_indices = sorted(t["probe_index"] for t in timers)
    assert probe_indices == [1, 2]


@pytest.mark.asyncio
async def test_get_timers_works_on_historical_session(store, sample_address):
    """get_timers must work for a session that is no longer active."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]
    await store.upsert_timer(sid, sample_address, 1, mode="countdown", duration_secs=60)
    await store.end_session(reason="user")

    timers = await store.get_timers(sid)
    assert len(timers) == 1
    assert timers[0]["mode"] == "countdown"


@pytest.mark.asyncio
async def test_timer_mutations_blocked_on_historical_session(store, sample_address):
    """All mutating timer ops should reject non-active sessions."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]
    await store.upsert_timer(sid, sample_address, 1, mode="countdown", duration_secs=60)
    await store.end_session(reason="user")

    for fn in (
        store.start_timer,
        store.pause_timer,
        store.resume_timer,
        store.reset_timer,
        store.complete_timer,
    ):
        with pytest.raises(ValueError, match="active session"):
            await fn(sid, sample_address, 1)


@pytest.mark.asyncio
async def test_reset_timer_missing_row(store, sample_address):
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]
    with pytest.raises(ValueError, match="Timer not found"):
        await store.reset_timer(sid, sample_address, 1)


@pytest.mark.asyncio
async def test_complete_timer_missing_row(store, sample_address):
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]
    with pytest.raises(ValueError, match="Timer not found"):
        await store.complete_timer(sid, sample_address, 1)


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
