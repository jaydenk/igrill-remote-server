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
async def test_get_targets_by_device_groups_per_address(store):
    """la-followups Task 7: multi-device peers need the per-device target
    map so editing device A doesn't clobber device B's targets in their
    local state. get_targets_by_device powers that broadcast.
    """
    from service.models.session import TargetConfig

    await store.start_session(addresses=["A", "B"], reason="user")
    state = await store.get_session_state()
    sid = state["current_session_id"]

    await store.save_targets(sid, "A", [TargetConfig(
        probe_index=1, mode="fixed", target_value=60.0, label="A.1",
    )])
    await store.save_targets(sid, "B", [TargetConfig(
        probe_index=2, mode="fixed", target_value=80.0, label="B.2",
    )])

    grouped = await store.get_targets_by_device(sid)
    assert set(grouped.keys()) == {"A", "B"}
    assert len(grouped["A"]) == 1
    assert grouped["A"][0].label == "A.1"
    assert grouped["B"][0].label == "B.2"


@pytest.mark.asyncio
async def test_get_targets_by_device_empty_for_unknown_session(store):
    """Unknown session id returns an empty dict, not an error."""
    grouped = await store.get_targets_by_device("nope")
    assert grouped == {}


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
async def test_save_and_get_targets_round_trips_unit(store):
    """unit defaults to 'C' and round-trips 'F' when set explicitly."""
    from service.models.session import TargetConfig

    start = await store.start_session(addresses=["70:91:8F:00:00:01"], reason="user")
    sid = start["session_id"]
    addr = "70:91:8F:00:00:01"

    await store.save_targets(sid, addr, [
        TargetConfig(probe_index=1, mode="fixed", target_value=74.0),
        TargetConfig(probe_index=2, mode="fixed", target_value=165.0, unit="F"),
    ])
    loaded = sorted(await store.get_targets(sid), key=lambda t: t.probe_index)
    assert loaded[0].unit == "C", "default unit should be 'C'"
    assert loaded[1].unit == "F", "unit 'F' should round-trip through the store"
    assert loaded[1].target_value == 165.0


@pytest.mark.asyncio
async def test_update_targets_round_trips_unit(store):
    """update_targets replaces rows and preserves per-target unit."""
    from service.models.session import TargetConfig

    start = await store.start_session(addresses=["70:91:8F:00:00:01"], reason="user")
    sid = start["session_id"]
    addr = "70:91:8F:00:00:01"

    await store.save_targets(sid, addr, [
        TargetConfig(probe_index=1, mode="fixed", target_value=74.0, unit="C"),
    ])
    await store.update_targets(sid, addr, [
        TargetConfig(probe_index=1, mode="fixed", target_value=165.0, unit="F"),
    ])
    loaded = await store.get_targets(sid)
    assert len(loaded) == 1
    assert loaded[0].unit == "F"
    assert loaded[0].target_value == 165.0


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
async def test_list_sessions_includes_devices_with_names(store, sample_address):
    """list_sessions must emit per-session devices with address+name+model.

    iOS history list relies on ``devices[0].name`` to render a grill name
    under each session row (task E4). If the field ever stops being
    emitted the row shows the date twice with no device hint.
    """
    await store.register_device(
        address=sample_address, name="Kitchen iGrill", model="iGrill_V3"
    )
    await store.start_session([sample_address], "user", name="Cook 1")
    sessions = await store.list_sessions(limit=5)
    assert len(sessions) == 1
    devices = sessions[0].get("devices")
    assert isinstance(devices, list), "devices must be a list on every row"
    assert len(devices) == 1
    assert devices[0]["address"] == sample_address
    assert devices[0]["name"] == "Kitchen iGrill"
    assert devices[0]["model"] == "iGrill_V3"


@pytest.mark.asyncio
async def test_list_sessions_emits_devices_for_unregistered_address(
    store, sample_address
):
    """Address with no matching devices row — name/model come back null."""
    await store.start_session([sample_address], "user")
    sessions = await store.list_sessions(limit=5)
    devices = sessions[0].get("devices") or []
    assert len(devices) == 1
    assert devices[0]["address"] == sample_address
    assert devices[0]["name"] is None
    assert devices[0]["model"] is None


@pytest.mark.asyncio
async def test_recover_orphaned_sessions_clears_left_at(tmp_db, sample_address):
    """A graceful shutdown sets session_devices.left_at; recovery must
    clear it so readings resume being persisted after restart.

    Without this fix is_device_in_session() would return False after
    restart, silently dropping every subsequent BLE reading until a
    client reconnects and manually triggers a rejoin. See la-followups
    Task 1.
    """
    store = HistoryStore(tmp_db, reconnect_grace=10)
    await store.connect()
    try:
        info = await store.start_session(
            addresses=[sample_address], reason="user"
        )
        sid = info["session_id"]

        # Simulate graceful shutdown — device marked as left.
        await store.device_left_session(sid, sample_address)
        assert await store.is_device_in_session(sample_address) is False

        # Drop and reopen the in-memory session pointer to mimic a restart.
        store._current_session_id = None
        store._current_session_start_ts = None
        await store.recover_orphaned_sessions()

        assert store._current_session_id == sid
        assert await store.is_device_in_session(sample_address) is True
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_recover_orphaned_sessions(tmp_db, sample_address):
    """Orphaned sessions (no ended_at) should be RESUMED on recovery.

    A cooking session routinely outlives a server reboot — kernel
    upgrade, container restart, power blip. The previous behaviour
    (auto-ending on restart) killed legitimate in-progress cooks and
    forced the user to start over. Resume the session instead; readings
    pick up naturally once BLE reconnects.
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

    # Session is resumed: in-memory current_session_id matches the
    # orphan and the row is still open.
    state = await store2.get_session_state()
    assert state["current_session_id"] == sid

    # The session row is still on disk with ended_at NULL (a cook in
    # progress), so list_sessions surfaces it with no endReason set.
    sessions = await store2.list_sessions(limit=10)
    assert len(sessions) == 1
    assert sessions[0]["sessionId"] == sid
    assert sessions[0]["endReason"] is None
    assert sessions[0]["endTs"] is None
    await store2.close()


@pytest.mark.asyncio
async def test_recover_orphaned_sessions_picks_newest_when_multiple(tmp_db):
    """If multiple orphans exist (should never happen via the API, which
    always ends the previous session before creating a new one, but could
    under unusual crash scenarios), the newest is resumed and the rest
    are ended — keeping the at-most-one-active-session invariant intact.
    """
    store1 = HistoryStore(tmp_db, reconnect_grace=60)
    await store1.connect()

    # Direct SQL to simulate the impossible-via-API state.
    await store1._conn.execute(
        "INSERT INTO sessions (id, started_at, start_reason) VALUES (?, ?, ?)",
        ("older", "2026-04-12T00:00:00Z", "user"),
    )
    await store1._conn.execute(
        "INSERT INTO sessions (id, started_at, start_reason) VALUES (?, ?, ?)",
        ("newer", "2026-04-13T00:00:00Z", "user"),
    )
    await store1._conn.commit()
    await store1.close()

    store2 = HistoryStore(tmp_db, reconnect_grace=60)
    await store2.connect()
    await store2.recover_orphaned_sessions()

    state = await store2.get_session_state()
    assert state["current_session_id"] == "newer"  # newest wins

    # list_sessions returns both: the resumed "newer" with no end and
    # the ended "older" with server_restart_duplicate as the reason.
    sessions = await store2.list_sessions(limit=10)
    by_id = {s["sessionId"]: s for s in sessions}
    assert by_id["newer"]["endReason"] is None
    assert by_id["older"]["endReason"] == "server_restart_duplicate"
    await store2.close()


@pytest.mark.asyncio
async def test_recover_orphaned_sessions_handles_partial_discard(
    store, sample_address, monkeypatch
):
    """A crash mid-discard must leave the database in a consistent state.

    ``discard_session`` performs its deletes inside a single BEGIN/COMMIT
    transaction wrapped in a try/except that issues ROLLBACK on failure.
    If a crash prevents the commit, every child-table DELETE is rolled
    back atomically by SQLite — so the session row and its children
    remain intact. On startup, ``recover_orphaned_sessions`` resumes
    the still-active session (rather than ending it), so the cook can
    continue with its child data intact.

    This test encodes that guarantee end-to-end in a single connection
    (equivalent to crash + restart, but simpler to express):

      1. Start a session and write some child rows.
      2. Patch ``_conn.commit`` so ``discard_session`` raises on commit —
         the except branch issues ROLLBACK, unwinding every DELETE.
      3. Verify all session data is still on disk (rollback was atomic).
      4. Call ``recover_orphaned_sessions`` to simulate the post-restart
         startup hook and verify it resumes the session, leaving child
         data intact.
    """
    from service.models.session import TargetConfig

    # 1. Populate a session with data to make rollback observable.
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]
    await store.record_reading(
        session_id=sid,
        address=sample_address,
        seq=1,
        probes=[{"index": 1, "temperature": 72.5}],
        battery=80,
        propane=None,
        heating=None,
    )
    await store.save_targets(
        sid,
        sample_address,
        [TargetConfig(probe_index=1, mode="fixed", target_value=74.0)],
    )
    await store.upsert_primary_note(sid, "before crash")

    # Sanity — data exists before the simulated crash.
    assert await _count(store, "session_devices", sid) == 1
    assert await _count(store, "probe_readings", sid) == 1
    assert await _count(store, "device_readings", sid) == 1
    assert await _count(store, "session_targets", sid) == 1
    assert await _count(store, "session_notes", sid) == 1

    # 2. Patch commit to raise — simulates a crash between the final
    #    DELETE and the COMMIT. The except branch inside discard_session
    #    issues ROLLBACK, which undoes every DELETE atomically.
    original_commit = store._conn.commit

    async def failing_commit():
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(store._conn, "commit", failing_commit)
    with pytest.raises(RuntimeError, match="simulated crash"):
        await store.discard_session(sid)

    # Restore commit so subsequent operations work normally. This
    # represents the fresh process that comes up after a real crash —
    # SQLite would roll back any uncommitted txn on reconnect; here the
    # in-process ROLLBACK inside discard_session's except block already
    # cleaned it up.
    monkeypatch.setattr(store._conn, "commit", original_commit)

    # 3. Every child row must still be present (rollback was atomic).
    assert await _count(store, "session_devices", sid) == 1
    assert await _count(store, "probe_readings", sid) == 1
    assert await _count(store, "device_readings", sid) == 1
    assert await _count(store, "session_targets", sid) == 1
    assert await _count(store, "session_notes", sid) == 1

    cursor = await store._conn.execute(
        "SELECT id, ended_at FROM sessions WHERE id = ?", (sid,)
    )
    row = await cursor.fetchone()
    assert row is not None
    # Session is still active (ended_at IS NULL) — it looks like an
    # orphan to recovery.
    assert row["ended_at"] is None

    # The discard raised, so in-memory state is (correctly) untouched;
    # reset it to simulate a fresh process after restart where no
    # session is loaded yet.
    store._current_session_id = None
    store._current_session_start_ts = None

    # 4. Run recovery — the still-active session is RESUMED (not ended)
    #    so the cook can continue after the restart. Child data is
    #    retained intact by the earlier discard rollback.
    await store.recover_orphaned_sessions()

    # Session remains active (ended_at still NULL) and in-memory state
    # points at it again.
    state = await store.get_session_state()
    assert state["current_session_id"] == sid
    # The row is still in the sessions table (active), so list_sessions
    # returns it with endReason=None.
    sessions = await store.list_sessions(limit=10)
    assert len(sessions) == 1
    assert sessions[0]["sessionId"] == sid
    assert sessions[0]["endReason"] is None

    # Child rows from the rolled-back discard are still on disk.
    assert await _count(store, "session_devices", sid) == 1
    assert await _count(store, "probe_readings", sid) == 1
    assert await _count(store, "session_notes", sid) == 1


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

    row = await store.upsert_timer(sid, sample_address, 1, mode="count_down", duration_secs=900)
    assert row["session_id"] == sid
    assert row["address"] == sample_address
    assert row["probe_index"] == 1
    assert row["mode"] == "count_down"
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

    await store.upsert_timer(sid, sample_address, 1, mode="count_down", duration_secs=60)
    await store.start_timer(sid, sample_address, 1)
    # Now re-upsert
    row = await store.upsert_timer(sid, sample_address, 1, mode="count_up", duration_secs=None)
    assert row["mode"] == "count_up"
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
        await store.upsert_timer(sid, sample_address, 1, mode="count_down", duration_secs=60)


@pytest.mark.asyncio
async def test_upsert_timer_unknown_session(store, sample_address):
    """Upsert against a current-session-id that isn't in sessions should raise."""
    # No active session at all
    with pytest.raises(ValueError, match="active session"):
        await store.upsert_timer("nonexistent", sample_address, 1, mode="count_down")


@pytest.mark.asyncio
async def test_start_timer_sets_started_at(store, sample_address):
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]
    await store.upsert_timer(sid, sample_address, 1, mode="count_down", duration_secs=60)
    row = await store.start_timer(sid, sample_address, 1)
    assert row["started_at"] is not None
    assert row["paused_at"] is None


@pytest.mark.asyncio
async def test_start_timer_idempotent(store, sample_address):
    """Calling start_timer twice should not reset started_at."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]
    await store.upsert_timer(sid, sample_address, 1, mode="count_down", duration_secs=60)

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
    await store.upsert_timer(sid, sample_address, 1, mode="count_down", duration_secs=60)
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
    await store.upsert_timer(sid, sample_address, 1, mode="count_down", duration_secs=600)

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
    await store.upsert_timer(sid, sample_address, 1, mode="count_down", duration_secs=600)

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
    await store.upsert_timer(sid, sample_address, 1, mode="count_down", duration_secs=60)

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
    await store.upsert_timer(sid, sample_address, 1, mode="count_down", duration_secs=60)
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
    await store.upsert_timer(sid, sample_address, 1, mode="count_down", duration_secs=60)

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
    await store.upsert_timer(sid, sample_address, 1, mode="count_down", duration_secs=60)
    await store.start_timer(sid, sample_address, 1)
    await store.complete_timer(sid, sample_address, 1)

    row = await store.reset_timer(sid, sample_address, 1)
    assert row["started_at"] is None
    assert row["paused_at"] is None
    assert row["completed_at"] is None
    assert row["accumulated_secs"] == 0
    # mode + duration preserved
    assert row["mode"] == "count_down"
    assert row["duration_secs"] == 60


@pytest.mark.asyncio
async def test_complete_timer_sets_completed_and_paused(store, sample_address, monkeypatch):
    """complete_timer sets both completed_at and paused_at, accumulates elapsed."""
    from datetime import datetime, timezone, timedelta
    from service.history import store as store_mod

    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]
    await store.upsert_timer(sid, sample_address, 1, mode="count_down", duration_secs=600)

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
    await store.upsert_timer(sid, sample_address, 1, mode="count_down", duration_secs=60)
    await store.start_timer(sid, sample_address, 1)

    first = await store.complete_timer(sid, sample_address, 1)
    second = await store.complete_timer(sid, sample_address, 1)
    assert first["completed_at"] == second["completed_at"]


@pytest.mark.asyncio
async def test_reset_after_complete_clears_completed_at(store, sample_address):
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]
    await store.upsert_timer(sid, sample_address, 1, mode="count_down", duration_secs=60)
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

    await store.upsert_timer(sid, sample_address, 1, mode="count_down", duration_secs=60)
    await store.upsert_timer(sid, sample_address, 2, mode="count_up", duration_secs=None)

    timers = await store.get_timers(sid)
    assert len(timers) == 2
    probe_indices = sorted(t["probe_index"] for t in timers)
    assert probe_indices == [1, 2]


@pytest.mark.asyncio
async def test_get_timers_works_on_historical_session(store, sample_address):
    """get_timers must work for a session that is no longer active."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]
    await store.upsert_timer(sid, sample_address, 1, mode="count_down", duration_secs=60)
    await store.end_session(reason="user")

    timers = await store.get_timers(sid)
    assert len(timers) == 1
    assert timers[0]["mode"] == "count_down"


@pytest.mark.asyncio
async def test_timer_mutations_blocked_on_historical_session(store, sample_address):
    """All mutating timer ops should reject non-active sessions."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]
    await store.upsert_timer(sid, sample_address, 1, mode="count_down", duration_secs=60)
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
async def test_upsert_timer_rejects_invalid_mode(store, sample_address):
    """upsert_timer must reject any mode other than count_up / count_down."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]
    with pytest.raises(ValueError, match="count_up.*count_down"):
        await store.upsert_timer(sid, sample_address, 1, mode="stopwatch", duration_secs=60)


@pytest.mark.asyncio
async def test_complete_timer_on_paused_preserves_paused_at(
    store, sample_address, monkeypatch
):
    """Completing an already-paused timer must leave paused_at untouched."""
    from datetime import datetime, timezone, timedelta
    from service.history import store as store_mod

    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]
    await store.upsert_timer(sid, sample_address, 1, mode="count_down", duration_secs=600)

    t0 = datetime(2026, 4, 12, 10, 0, 0, tzinfo=timezone.utc)
    # start, pause (20s later), complete (50s later)
    times = [
        t0,
        t0 + timedelta(seconds=20),
        t0 + timedelta(seconds=50),
    ]

    def fake_now_iso():
        return times.pop(0).isoformat()

    monkeypatch.setattr(store_mod, "now_iso_utc", fake_now_iso)

    await store.start_timer(sid, sample_address, 1)
    paused = await store.pause_timer(sid, sample_address, 1)
    paused_at_before = paused["paused_at"]
    accum_before = paused["accumulated_secs"]

    completed = await store.complete_timer(sid, sample_address, 1)
    assert completed["completed_at"] is not None
    # paused_at and accumulated_secs must be preserved (timer wasn't running)
    assert completed["paused_at"] == paused_at_before
    assert completed["accumulated_secs"] == accum_before
    assert completed["started_at"] is None


@pytest.mark.asyncio
async def test_start_timer_on_paused_acts_as_resume(
    store, sample_address, monkeypatch
):
    """start_timer on a paused timer resumes it, preserving accumulated_secs."""
    from datetime import datetime, timezone, timedelta
    from service.history import store as store_mod

    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]
    await store.upsert_timer(sid, sample_address, 1, mode="count_down", duration_secs=600)

    t0 = datetime(2026, 4, 12, 10, 0, 0, tzinfo=timezone.utc)
    # start, pause (25s later), resume-via-start (40s later)
    times = [
        t0,
        t0 + timedelta(seconds=25),
        t0 + timedelta(seconds=40),
    ]

    def fake_now_iso():
        return times.pop(0).isoformat()

    monkeypatch.setattr(store_mod, "now_iso_utc", fake_now_iso)

    await store.start_timer(sid, sample_address, 1)
    paused = await store.pause_timer(sid, sample_address, 1)
    assert paused["accumulated_secs"] == 25

    row = await store.start_timer(sid, sample_address, 1)
    assert row["started_at"] is not None
    assert row["paused_at"] is None
    # accumulated_secs preserved from the pause
    assert row["accumulated_secs"] == 25


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


# ---------------------------------------------------------------------------
# Session notes CRUD (Task 5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_primary_note_returns_none_when_no_notes(store, sample_address):
    """get_primary_note should return None for a session with no notes rows."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]

    assert await store.get_primary_note(sid) is None


@pytest.mark.asyncio
async def test_upsert_primary_note_creates_new_row(store, sample_address):
    """First upsert should INSERT a session_notes row with matching timestamps."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]

    row = await store.upsert_primary_note(sid, "first draft")
    assert row["session_id"] == sid
    assert row["body"] == "first draft"
    assert row["id"] is not None
    assert row["created_at"] is not None
    assert row["updated_at"] is not None
    # On INSERT, created_at and updated_at are set to the same value.
    assert row["created_at"] == row["updated_at"]


@pytest.mark.asyncio
async def test_upsert_primary_note_updates_existing_row(store, sample_address, monkeypatch):
    """A second upsert should UPDATE the same row — preserving id and created_at,
    bumping updated_at."""
    from datetime import datetime, timezone, timedelta
    from service.history import store as store_mod

    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]

    t0 = datetime(2026, 4, 12, 10, 0, 0, tzinfo=timezone.utc)
    times = [t0, t0 + timedelta(seconds=30)]

    def fake_now_iso():
        return times.pop(0).isoformat()

    monkeypatch.setattr(store_mod, "now_iso_utc", fake_now_iso)

    first = await store.upsert_primary_note(sid, "first")
    second = await store.upsert_primary_note(sid, "second")

    assert second["id"] == first["id"]
    assert second["created_at"] == first["created_at"]
    assert second["updated_at"] != first["updated_at"]
    assert second["body"] == "second"


@pytest.mark.asyncio
async def test_get_primary_note_returns_row_after_upsert(store, sample_address):
    """get_primary_note should return the row created by upsert_primary_note."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]

    upserted = await store.upsert_primary_note(sid, "hello world")
    fetched = await store.get_primary_note(sid)

    assert fetched is not None
    assert fetched == upserted


@pytest.mark.asyncio
async def test_upsert_primary_note_editable_after_session_ended(store, sample_address):
    """Notes are editable on ENDED sessions — unlike timers, no active-session guard."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]

    await store.upsert_primary_note(sid, "during session")
    await store.end_session(reason="user")

    # Still editable after end
    row = await store.upsert_primary_note(sid, "after session")
    assert row["body"] == "after session"

    fetched = await store.get_primary_note(sid)
    assert fetched["body"] == "after session"


@pytest.mark.asyncio
async def test_upsert_primary_note_dual_writes_sessions_notes(store, sample_address):
    """After upsert, the legacy sessions.notes column should match body."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]

    await store.upsert_primary_note(sid, "legacy body 1")
    cursor = await store._conn.execute(
        "SELECT notes FROM sessions WHERE id = ?", (sid,)
    )
    row = await cursor.fetchone()
    assert row["notes"] == "legacy body 1"

    # Update path also dual-writes.
    await store.upsert_primary_note(sid, "legacy body 2")
    cursor = await store._conn.execute(
        "SELECT notes FROM sessions WHERE id = ?", (sid,)
    )
    row = await cursor.fetchone()
    assert row["notes"] == "legacy body 2"


@pytest.mark.asyncio
async def test_get_notes_empty_for_session_without_notes(store, sample_address):
    """get_notes should return an empty list when no notes exist."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]

    assert await store.get_notes(sid) == []


@pytest.mark.asyncio
async def test_get_notes_returns_all_in_created_at_order(store, sample_address):
    """get_notes orders by created_at ASC, id ASC; primary note (earliest) first."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]

    # Insert two rows directly via SQL so we control created_at precisely,
    # simulating the eventual multi-note future.
    await store._conn.execute(
        "INSERT INTO session_notes (session_id, created_at, updated_at, body) "
        "VALUES (?, ?, ?, ?)",
        (sid, "2026-04-12T10:00:02+00:00", "2026-04-12T10:00:02+00:00", "second"),
    )
    await store._conn.execute(
        "INSERT INTO session_notes (session_id, created_at, updated_at, body) "
        "VALUES (?, ?, ?, ?)",
        (sid, "2026-04-12T10:00:01+00:00", "2026-04-12T10:00:01+00:00", "first"),
    )
    await store._conn.commit()

    notes = await store.get_notes(sid)
    assert len(notes) == 2
    assert notes[0]["body"] == "first"
    assert notes[1]["body"] == "second"

    # get_primary_note should return the earliest-created row.
    primary = await store.get_primary_note(sid)
    assert primary["body"] == "first"


# ---------------------------------------------------------------------------
# discard_session
# ---------------------------------------------------------------------------


async def _count(store, table: str, session_id: str) -> int:
    cursor = await store._conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE session_id = ?", (session_id,)
    )
    row = await cursor.fetchone()
    return row[0]


@pytest.mark.asyncio
async def test_discard_session_deletes_row_and_cascade(store, sample_address):
    """discard_session removes the sessions row and every child-table row."""
    from service.models.session import TargetConfig

    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]

    # Record a reading -> probe_readings + device_readings
    await store.record_reading(
        session_id=sid,
        address=sample_address,
        seq=1,
        probes=[{"index": 1, "temperature": 72.5}],
        battery=80,
        propane=None,
        heating=None,
    )

    # Targets
    await store.save_targets(
        sid,
        sample_address,
        [TargetConfig(probe_index=1, mode="fixed", target_value=74.0)],
    )

    # Timer
    await store.upsert_timer(
        sid, sample_address, 1, mode="count_down", duration_secs=60
    )

    # Note
    await store.upsert_primary_note(sid, "before discard")

    # Sanity — data exists
    assert await _count(store, "session_devices", sid) == 1
    assert await _count(store, "probe_readings", sid) == 1
    assert await _count(store, "device_readings", sid) == 1
    assert await _count(store, "session_targets", sid) == 1
    assert await _count(store, "session_timers", sid) == 1
    assert await _count(store, "session_notes", sid) == 1

    result = await store.discard_session(sid)
    assert result is True

    assert await _count(store, "session_devices", sid) == 0
    assert await _count(store, "probe_readings", sid) == 0
    assert await _count(store, "device_readings", sid) == 0
    assert await _count(store, "session_targets", sid) == 0
    assert await _count(store, "session_timers", sid) == 0
    assert await _count(store, "session_notes", sid) == 0

    cursor = await store._conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE id = ?", (sid,)
    )
    assert (await cursor.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_discard_session_clears_active_state(store, sample_address):
    """Discarding the current session clears _current_session_id so further
    timer/other active-only operations on the old id fail (no-op writes)."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]

    assert await store.is_session_active() is True
    assert await store.get_current_session_id() == sid

    result = await store.discard_session(sid)
    assert result is True

    # Active state cleared
    assert await store.is_session_active() is False
    assert await store.get_current_session_id() is None

    state = await store.get_session_state()
    assert state["current_session_id"] is None
    assert state["current_session_start_ts"] is None

    # Subsequent active-session operation on the old id must not write.
    with pytest.raises(ValueError, match="active session"):
        await store.upsert_timer(
            sid, sample_address, 1, mode="count_down", duration_secs=60
        )
    # Confirm no row was created.
    cursor = await store._conn.execute(
        "SELECT COUNT(*) FROM session_timers WHERE session_id = ?", (sid,)
    )
    assert (await cursor.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_discard_session_preserves_in_memory_state_on_commit_failure(
    store, sample_address, monkeypatch
):
    """If commit() raises, in-memory session state must NOT be cleared.

    This guards the correctness of discard_session when the database
    commit fails mid-operation: the transaction is rolled back (so the
    sessions row still exists on disk), and the in-memory active-session
    tracking must also remain untouched so the two stay consistent.
    """
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]

    assert await store.get_current_session_id() == sid

    # Make commit fail to simulate a disk/transaction error.
    original_commit = store._conn.commit

    async def failing_commit():
        raise RuntimeError("simulated commit failure")

    monkeypatch.setattr(store._conn, "commit", failing_commit)

    with pytest.raises(RuntimeError, match="simulated commit failure"):
        await store.discard_session(sid)

    # Restore commit so the rest of the test can interact with the DB.
    monkeypatch.setattr(store._conn, "commit", original_commit)

    # In-memory state must still reflect the active session.
    assert await store.get_current_session_id() == sid
    assert await store.is_session_active() is True

    # On-disk row must still exist (ROLLBACK on exception).
    cursor = await store._conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE id = ?", (sid,)
    )
    assert (await cursor.fetchone())[0] == 1


@pytest.mark.asyncio
async def test_discard_session_returns_false_for_unknown_id(store):
    """Unknown session_id returns False without raising."""
    result = await store.discard_session("deadbeef" * 4)
    assert result is False


@pytest.mark.asyncio
async def test_discard_session_on_ended_session(store, sample_address):
    """Discarding an already-ended session still deletes rows and returns True."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]
    await store.record_reading(
        session_id=sid,
        address=sample_address,
        seq=1,
        probes=[{"index": 1, "temperature": 70.0}],
        battery=80,
        propane=None,
        heating=None,
    )
    await store.upsert_primary_note(sid, "done")
    await store.end_session(reason="user")

    # End clears active state already.
    assert await store.get_current_session_id() is None

    result = await store.discard_session(sid)
    assert result is True

    assert await _count(store, "session_devices", sid) == 0
    assert await _count(store, "probe_readings", sid) == 0
    assert await _count(store, "device_readings", sid) == 0
    assert await _count(store, "session_notes", sid) == 0

    cursor = await store._conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE id = ?", (sid,)
    )
    assert (await cursor.fetchone())[0] == 0


# ---------------------------------------------------------------------------
# find_expired_running_countdowns (Task 12)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_expired_running_countdowns_respects_filters(
    store, sample_address, monkeypatch,
):
    """find_expired_running_countdowns must only return timers that are:

      * mode='count_down'
      * running (started_at set, paused_at null)
      * not completed
      * have a duration_secs set
      * whose effective elapsed time >= duration_secs

    Each probe exercises a distinct exclusion; probe 6 is the positive case.
    """
    from datetime import datetime, timezone, timedelta
    from service.history import store as store_mod

    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]

    # Anchor on real now so find_expired_running_countdowns (which uses real
    # now) computes positive elapsed seconds against t_old.
    t_now = datetime.now(timezone.utc)
    t_old = t_now - timedelta(seconds=3600)

    # Drive now_iso_utc() so started_at values are predictable. Order matches
    # the mutation sequence below — start/pause/complete each call now once.
    times = iter([
        t_now, t_now + timedelta(seconds=5),           # probe 1 start, pause
        t_now, t_now + timedelta(seconds=1),           # probe 2 start, complete
        t_now,                                         # probe 3 start
        t_now,                                         # probe 4 start
        t_now,                                         # probe 5 start
        t_old,                                         # probe 6 start (long ago)
    ])

    def fake_now_iso():
        return next(times).isoformat()

    monkeypatch.setattr(store_mod, "now_iso_utc", fake_now_iso)

    # Probe 1: paused running count_down — excluded (paused_at set)
    await store.upsert_timer(sid, sample_address, 1, mode="count_down", duration_secs=60)
    await store.start_timer(sid, sample_address, 1)
    await store.pause_timer(sid, sample_address, 1)

    # Probe 2: completed count_down — excluded (completed_at set)
    await store.upsert_timer(sid, sample_address, 2, mode="count_down", duration_secs=60)
    await store.start_timer(sid, sample_address, 2)
    await store.complete_timer(sid, sample_address, 2)

    # Probe 3: count_up timer — excluded (mode != count_down)
    await store.upsert_timer(sid, sample_address, 3, mode="count_up", duration_secs=None)
    await store.start_timer(sid, sample_address, 3)

    # Probe 4: running count_down, duration is 10 hours — not yet expired
    await store.upsert_timer(sid, sample_address, 4, mode="count_down", duration_secs=36000)
    await store.start_timer(sid, sample_address, 4)

    # Probe 5: count_down with null duration — excluded
    await store.upsert_timer(sid, sample_address, 5, mode="count_down", duration_secs=None)
    await store.start_timer(sid, sample_address, 5)

    # Probe 6: running count_down started an hour ago, duration 60s — SHOULD expire
    await store.upsert_timer(sid, sample_address, 6, mode="count_down", duration_secs=60)
    await store.start_timer(sid, sample_address, 6)

    # Restore real now so find_expired_running_countdowns sees real elapsed.
    monkeypatch.setattr(
        store_mod, "now_iso_utc",
        lambda: datetime.now(timezone.utc).isoformat(),
    )

    expired = await store.find_expired_running_countdowns()
    probe_indices = sorted(r["probe_index"] for r in expired)
    assert probe_indices == [6], (
        f"Expected only probe 6 expired, got {probe_indices}"
    )

    row = expired[0]
    assert row["mode"] == "count_down"
    assert row["duration_secs"] == 60
    assert row["started_at"] is not None
    assert row["paused_at"] is None
    assert row["completed_at"] is None


@pytest.mark.asyncio
async def test_find_expired_running_countdowns_empty_when_nothing_matches(
    store, sample_address,
):
    """Base cases: no timers, and a count_down that has been upserted but
    never started, both yield an empty list."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]

    assert await store.find_expired_running_countdowns() == []

    await store.upsert_timer(sid, sample_address, 1, mode="count_down", duration_secs=60)
    # Never started — not expired.
    assert await store.find_expired_running_countdowns() == []


# ---------------------------------------------------------------------------
# target_duration_secs on start_session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_session_persists_target_duration_secs(store, sample_address):
    """start_session(..., target_duration_secs=3600) must persist the value
    to sessions.target_duration_secs and expose it via get_session_metadata."""
    result = await store.start_session(
        addresses=[sample_address],
        reason="user",
        target_duration_secs=3600,
    )
    session_id = result["session_id"]

    assert result["target_duration_secs"] == 3600
    assert result["start_event"]["targetDurationSecs"] == 3600

    async with store._conn.execute(
        "SELECT target_duration_secs FROM sessions WHERE id = ?", (session_id,)
    ) as cursor:
        row = await cursor.fetchone()
    assert row["target_duration_secs"] == 3600

    meta = await store.get_session_metadata(session_id)
    assert meta["target_duration_secs"] == 3600


@pytest.mark.asyncio
async def test_start_session_without_target_duration_secs_stores_null(
    store, sample_address,
):
    """start_session without target_duration_secs must persist NULL."""
    result = await store.start_session(
        addresses=[sample_address], reason="user",
    )
    session_id = result["session_id"]

    assert result["target_duration_secs"] is None
    assert result["start_event"]["targetDurationSecs"] is None

    async with store._conn.execute(
        "SELECT target_duration_secs FROM sessions WHERE id = ?", (session_id,)
    ) as cursor:
        row = await cursor.fetchone()
    assert row["target_duration_secs"] is None

    meta = await store.get_session_metadata(session_id)
    assert meta["target_duration_secs"] is None


@pytest.mark.asyncio
async def test_list_sessions_includes_target_duration_secs(store, sample_address):
    """list_sessions must surface targetDurationSecs (camelCase) per session."""
    await store.start_session(
        addresses=[sample_address], reason="user", target_duration_secs=7200,
    )
    sessions = await store.list_sessions(limit=10)
    assert len(sessions) == 1
    assert sessions[0]["targetDurationSecs"] == 7200


@pytest.mark.asyncio
async def test_connect_sets_wal_and_busy_timeout_pragmas(store):
    """HistoryStore.connect must set journal_mode=WAL and busy_timeout so
    that concurrent writers don't immediately fail with SQLITE_BUSY."""
    async with store._conn.execute("PRAGMA journal_mode") as cur:
        row = await cur.fetchone()
    assert row[0].lower() == "wal"

    async with store._conn.execute("PRAGMA busy_timeout") as cur:
        row = await cur.fetchone()
    assert row[0] >= 5000


def test_parse_iso_coerces_naive_to_utc_aware():
    """Naive ISO strings must be read as UTC so datetime arithmetic
    against now_iso_utc (aware) does not raise TypeError."""
    from service.history.store import parse_iso, now_iso_utc
    from datetime import timezone

    naive = parse_iso("2026-04-17T10:00:00")
    assert naive is not None
    assert naive.tzinfo is not None
    assert naive.utcoffset() == timezone.utc.utcoffset(None)

    aware = parse_iso(now_iso_utc())
    assert aware is not None
    assert aware.tzinfo is not None

    # Subtracting the two must not raise.
    _ = aware - naive


@pytest.mark.asyncio
async def test_find_expired_running_countdowns_skips_corrupt_rows(
    store, sample_address,
):
    """One row with a garbage numeric column must not abort the iteration
    — other expired timers in the same tick must still be returned."""
    from service.history.store import now_iso_utc
    from datetime import datetime, timedelta, timezone

    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]

    # Healthy row: 60-second count_down started 120s ago → expired.
    await store.upsert_timer(
        session_id=sid, address=sample_address, probe_index=1,
        mode="count_down", duration_secs=60,
    )
    started = (
        datetime.now(timezone.utc) - timedelta(seconds=120)
    ).isoformat()
    await store._conn.execute(
        "UPDATE session_timers SET started_at = ? "
        "WHERE session_id = ? AND address = ? AND probe_index = ?",
        (started, sid, sample_address, 1),
    )
    # Corrupt row: accumulated_secs is a non-numeric string (SQLite's loose
    # typing allows this). This row should be skipped without aborting.
    await store.upsert_timer(
        session_id=sid, address=sample_address, probe_index=2,
        mode="count_down", duration_secs=60,
    )
    await store._conn.execute(
        "UPDATE session_timers SET started_at = ?, accumulated_secs = 'not-an-int' "
        "WHERE session_id = ? AND address = ? AND probe_index = ?",
        (started, sid, sample_address, 2),
    )
    await store._conn.commit()

    expired = await store.find_expired_running_countdowns()
    probes = sorted(e["probe_index"] for e in expired)
    assert probes == [1], \
        f"healthy countdown lost because a sibling row had corrupt state: {expired}"
