"""Tests for the PushService class."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from service.push.service import PushService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path):
    """Path to a temporary SQLite database file."""
    return str(tmp_path / "test.db")


@pytest_asyncio.fixture
async def db(db_path):
    """Connection against ``db_path`` with the schema pre-populated.

    Tests use this connection to set up fixtures and verify state.
    PushService opens its own connection against the same path.
    """
    import aiosqlite

    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    await conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS push_tokens (
            token       TEXT PRIMARY KEY,
            live_activity_token TEXT,
            created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id          TEXT PRIMARY KEY,
            started_at  TEXT NOT NULL,
            ended_at    TEXT,
            start_reason TEXT NOT NULL,
            end_reason  TEXT
        );

        CREATE TABLE IF NOT EXISTS session_targets (
            session_id   TEXT NOT NULL REFERENCES sessions(id),
            address      TEXT NOT NULL,
            probe_index  INTEGER NOT NULL,
            mode         TEXT NOT NULL DEFAULT 'fixed',
            target_value REAL,
            range_low    REAL,
            range_high   REAL,
            label        TEXT,
            PRIMARY KEY (session_id, address, probe_index)
        );

        CREATE TABLE IF NOT EXISTS session_timers (
            session_id       TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            address          TEXT NOT NULL,
            probe_index      INTEGER NOT NULL,
            mode             TEXT NOT NULL,
            duration_secs    INTEGER,
            started_at       TEXT,
            paused_at        TEXT,
            accumulated_secs INTEGER NOT NULL DEFAULT 0,
            completed_at     TEXT,
            PRIMARY KEY (session_id, address, probe_index)
        );
        """
    )
    yield conn
    await conn.close()


async def _make_service(db_path, **kwargs):
    """Create a PushService with sensible defaults and open its DB.

    PushService now owns its own aiosqlite connection. We call ``_open_db``
    directly rather than the full ``connect()`` so the unit tests can
    pretend APNS credentials exist without needing a real key file on
    disk — ``connect()`` reads the key and would flip ``_enabled`` off
    when the fake path doesn't exist.
    """
    defaults = {
        "key_path": "",
        "key_id": "",
        "team_id": "",
        "bundle_id": "",
        "use_sandbox": True,
    }
    defaults.update(kwargs)
    svc = PushService(db_path=db_path, **defaults)
    await svc._open_db()
    return svc


async def _make_enabled_service(db_path):
    """Create a PushService with all credentials set (but no real APNS)."""
    return await _make_service(
        db_path,
        key_path="/fake/key.p8",
        key_id="KEYID12345",
        team_id="TEAMID1234",
        bundle_id="com.example.app",
        use_sandbox=True,
    )


# ---------------------------------------------------------------------------
# Disabled / no-op behaviour
# ---------------------------------------------------------------------------


class TestPushServiceDisabled:
    """PushService should be a no-op when credentials are not configured."""

    @pytest.mark.asyncio
    async def test_disabled_when_no_credentials(self, db, db_path):
        svc = await _make_service(db_path)
        assert svc.enabled is False

    @pytest.mark.asyncio
    async def test_disabled_when_partial_credentials(self, db, db_path):
        svc = await _make_service(db_path, key_path="/some/path", key_id="KEYID")
        assert svc.enabled is False

    @pytest.mark.asyncio
    async def test_send_alert_is_noop_when_disabled(self, db, db_path):
        svc = await _make_service(db_path)
        # Should not raise
        await svc.send_alert({"type": "target_reached", "payload": {}})

    @pytest.mark.asyncio
    async def test_send_la_update_is_noop_when_disabled(self, db, db_path):
        svc = await _make_service(db_path)
        # Should not raise
        await svc.send_live_activity_update({"probes": []})

    @pytest.mark.asyncio
    async def test_enabled_when_all_credentials(self, db, db_path):
        svc = await _make_enabled_service(db_path)
        assert svc.enabled is True


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------


class TestTokenManagement:
    @pytest.mark.asyncio
    async def test_upsert_token(self, db, db_path):
        svc = await _make_enabled_service(db_path)
        await svc.upsert_token("device_token_abc")

        cursor = await db.execute(
            "SELECT token, live_activity_token FROM push_tokens WHERE token = ?",
            ("device_token_abc",),
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row["token"] == "device_token_abc"
        assert row["live_activity_token"] is None

    @pytest.mark.asyncio
    async def test_upsert_token_with_la_token(self, db, db_path):
        svc = await _make_enabled_service(db_path)
        await svc.upsert_token("device_token_abc", live_activity_token="la_token_xyz")

        cursor = await db.execute(
            "SELECT token, live_activity_token FROM push_tokens WHERE token = ?",
            ("device_token_abc",),
        )
        row = await cursor.fetchone()
        assert row["live_activity_token"] == "la_token_xyz"

    @pytest.mark.asyncio
    async def test_upsert_token_replaces_existing(self, db, db_path):
        svc = await _make_enabled_service(db_path)
        await svc.upsert_token("device_token_abc", live_activity_token="old_la")
        await svc.upsert_token("device_token_abc", live_activity_token="new_la")

        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM push_tokens WHERE token = ?",
            ("device_token_abc",),
        )
        row = await cursor.fetchone()
        assert row["cnt"] == 1

        cursor = await db.execute(
            "SELECT live_activity_token FROM push_tokens WHERE token = ?",
            ("device_token_abc",),
        )
        row = await cursor.fetchone()
        assert row["live_activity_token"] == "new_la"

    @pytest.mark.asyncio
    async def test_remove_token(self, db, db_path):
        svc = await _make_enabled_service(db_path)
        await svc.upsert_token("device_token_abc")
        await svc.remove_token("device_token_abc")

        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM push_tokens WHERE token = ?",
            ("device_token_abc",),
        )
        row = await cursor.fetchone()
        assert row["cnt"] == 0

    @pytest.mark.asyncio
    async def test_remove_nonexistent_token(self, db, db_path):
        svc = await _make_enabled_service(db_path)
        # Should not raise
        await svc.remove_token("nonexistent")

    @pytest.mark.asyncio
    async def test_get_all_tokens(self, db, db_path):
        svc = await _make_enabled_service(db_path)
        await svc.upsert_token("token_a")
        await svc.upsert_token("token_b")
        await svc.upsert_token("token_c")

        tokens = await svc._get_all_tokens()
        assert set(tokens) == {"token_a", "token_b", "token_c"}

    @pytest.mark.asyncio
    async def test_get_la_tokens(self, db, db_path):
        svc = await _make_enabled_service(db_path)
        await svc.upsert_token("token_a", live_activity_token="la_1")
        await svc.upsert_token("token_b")  # no LA token
        await svc.upsert_token("token_c", live_activity_token="la_2")

        la_tokens = await svc._get_la_tokens()
        assert set(la_tokens) == {"la_1", "la_2"}


# ---------------------------------------------------------------------------
# Alert formatting
# ---------------------------------------------------------------------------


class TestFormatAlert:
    def test_target_approaching(self):
        title, body = PushService.format_alert(
            "target_approaching",
            {
                "probeIndex": 1,
                "currentTemp": 68.0,
                "target": {"target_value": 73.0, "label": None},
            },
        )
        assert title == "Approaching Target"
        assert "Probe 1" in body
        assert "68" in body

    def test_target_reached(self):
        title, body = PushService.format_alert(
            "target_reached",
            {
                "probeIndex": 2,
                "currentTemp": 73.0,
                "target": {"target_value": 73.0, "label": None},
            },
        )
        assert title == "Target Reached"
        assert "Probe 2" in body

    def test_target_exceeded(self):
        title, body = PushService.format_alert(
            "target_exceeded",
            {
                "probeIndex": 3,
                "currentTemp": 80.0,
                "target": {"target_value": 73.0, "label": None},
            },
        )
        assert title == "Target Exceeded"
        assert "Probe 3" in body

    def test_target_reminder(self):
        title, body = PushService.format_alert(
            "target_reminder",
            {
                "probeIndex": 1,
                "currentTemp": 85.0,
                "target": {"target_value": 73.0, "label": None},
            },
        )
        assert title == "Still Exceeded \u2014 Reminder"
        assert "Probe 1" in body

    def test_uses_label_when_available(self):
        title, body = PushService.format_alert(
            "target_reached",
            {
                "probeIndex": 1,
                "currentTemp": 93.0,
                "target": {"target_value": 93.0, "label": "Brisket Point"},
            },
        )
        assert "Brisket Point" in body
        assert "Probe 1" not in body

    def test_falls_back_to_probe_n(self):
        title, body = PushService.format_alert(
            "target_reached",
            {
                "probeIndex": 4,
                "currentTemp": 73.0,
                "target": {"target_value": 73.0, "label": None},
            },
        )
        assert "Probe 4" in body

    def test_falls_back_when_label_empty_string(self):
        title, body = PushService.format_alert(
            "target_reached",
            {
                "probeIndex": 2,
                "currentTemp": 73.0,
                "target": {"target_value": 73.0, "label": ""},
            },
        )
        assert "Probe 2" in body

    def test_unknown_alert_type(self):
        title, body = PushService.format_alert(
            "unknown_type",
            {"probeIndex": 1, "currentTemp": 50.0, "target": {"label": None}},
        )
        assert title == "Alert"
        assert "Probe 1" in body


# ---------------------------------------------------------------------------
# Live Activity throttle
# ---------------------------------------------------------------------------


class TestLiveActivityThrottle:
    @pytest.mark.asyncio
    async def test_should_send_initially(self, db, db_path):
        svc = await _make_enabled_service(db_path)
        assert svc.should_send_la_update() is True

    @pytest.mark.asyncio
    async def test_should_skip_if_too_soon(self, db, db_path):
        svc = await _make_enabled_service(db_path)
        # Simulate having just sent
        svc._last_la_update_ts = time.monotonic()
        assert svc.should_send_la_update() is False

    @pytest.mark.asyncio
    async def test_should_allow_after_interval(self, db, db_path):
        svc = await _make_enabled_service(db_path)
        # Simulate having sent 16 seconds ago
        svc._last_la_update_ts = time.monotonic() - 16
        assert svc.should_send_la_update() is True

    @pytest.mark.asyncio
    async def test_should_skip_at_exactly_interval(self, db, db_path):
        svc = await _make_enabled_service(db_path)
        # Simulate having sent exactly 15 seconds ago (boundary)
        svc._last_la_update_ts = time.monotonic() - 15
        # At exactly 15 seconds, should allow (>= check)
        assert svc.should_send_la_update() is True

    @pytest.mark.asyncio
    async def test_should_skip_just_under_interval(self, db, db_path):
        svc = await _make_enabled_service(db_path)
        svc._last_la_update_ts = time.monotonic() - 14.9
        assert svc.should_send_la_update() is False


# ---------------------------------------------------------------------------
# _build_content_state
# ---------------------------------------------------------------------------


class TestBuildContentState:
    """_build_content_state emits the full iOS ContentState schema."""

    @pytest.mark.asyncio
    async def test_no_session_id_returns_sensible_defaults(self, db, db_path):
        """A reading without a sessionId should not crash and should include
        all non-optional ProbeState fields with fallback values."""
        svc = await _make_enabled_service(db_path)
        reading = {
            "payload": {
                "sensorId": "AA:BB:CC:DD:EE:FF",
                "sessionId": None,
                "data": {
                    "unit": "C",
                    "probes": [
                        {"index": 1, "temperature": 82.5},
                        {"index": 2, "temperature": None},
                    ],
                },
            }
        }

        state = await svc._build_content_state(reading)

        assert state["unit"] == "C"
        assert state["featuredProbeIndex"] is None
        assert len(state["probes"]) == 2

        p1 = state["probes"][0]
        assert p1["index"] == 1
        assert p1["label"] == "Probe 1"
        assert p1["unplugged"] is False
        assert p1["recentTemps"] == []
        assert p1["temperature"] == 82.5

        p2 = state["probes"][1]
        assert p2["index"] == 2
        assert p2["label"] == "Probe 2"
        assert p2["unplugged"] is True
        assert p2["recentTemps"] == []
        assert "temperature" not in p2

    @pytest.mark.asyncio
    async def test_with_session_uses_db_label_and_target(self, db, db_path):
        """When a session exists in the DB the probe label and target_value are
        pulled from session_targets."""
        svc = await _make_enabled_service(db_path)

        # Seed session and target rows.
        await db.execute(
            "INSERT INTO sessions (id, started_at, start_reason) VALUES (?, ?, ?)",
            ("sess-1", "2026-04-15T10:00:00Z", "manual"),
        )
        await db.execute(
            "INSERT INTO session_targets "
            "(session_id, address, probe_index, mode, target_value, label) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("sess-1", "AA:BB:CC:DD:EE:FF", 1, "fixed", 90.0, "Brisket"),
        )
        await db.commit()

        reading = {
            "payload": {
                "sensorId": "AA:BB:CC:DD:EE:FF",
                "sessionId": "sess-1",
                "data": {
                    "unit": "C",
                    "probes": [{"index": 1, "temperature": 75.0}],
                },
            }
        }

        state = await svc._build_content_state(reading)

        p1 = state["probes"][0]
        assert p1["label"] == "Brisket"
        assert p1["target"] == 90.0
        assert p1["unplugged"] is False
        assert p1["temperature"] == 75.0

    @pytest.mark.asyncio
    async def test_range_mode_target_omitted(self, db, db_path):
        """For range-mode targets, target should not appear in the probe state."""
        svc = await _make_enabled_service(db_path)

        await db.execute(
            "INSERT INTO sessions (id, started_at, start_reason) VALUES (?, ?, ?)",
            ("sess-2", "2026-04-15T10:00:00Z", "manual"),
        )
        await db.execute(
            "INSERT INTO session_targets "
            "(session_id, address, probe_index, mode, target_value, range_low, range_high, label) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("sess-2", "AA:BB:CC:DD:EE:FF", 1, "range", None, 70.0, 80.0, "Ribs"),
        )
        await db.commit()

        reading = {
            "payload": {
                "sensorId": "AA:BB:CC:DD:EE:FF",
                "sessionId": "sess-2",
                "data": {
                    "unit": "C",
                    "probes": [{"index": 1, "temperature": 75.0}],
                },
            }
        }

        state = await svc._build_content_state(reading)

        p1 = state["probes"][0]
        assert p1["label"] == "Ribs"
        assert "target" not in p1

    @pytest.mark.asyncio
    async def test_timer_included_when_present(self, db, db_path):
        """When a session_timers row exists the timer dict is included with
        camelCase field names matching the iOS ProbeTimerAnchors struct."""
        svc = await _make_enabled_service(db_path)

        await db.execute(
            "INSERT INTO sessions (id, started_at, start_reason) VALUES (?, ?, ?)",
            ("sess-3", "2026-04-15T10:00:00Z", "manual"),
        )
        await db.execute(
            "INSERT INTO session_timers "
            "(session_id, address, probe_index, mode, duration_secs, "
            "started_at, paused_at, accumulated_secs, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "sess-3", "AA:BB:CC:DD:EE:FF", 1,
                "count_up", None, "2026-04-15T10:00:00Z", None, 0, None,
            ),
        )
        await db.commit()

        reading = {
            "payload": {
                "sensorId": "AA:BB:CC:DD:EE:FF",
                "sessionId": "sess-3",
                "data": {
                    "unit": "C",
                    "probes": [{"index": 1, "temperature": 60.0}],
                },
            }
        }

        state = await svc._build_content_state(reading)

        p1 = state["probes"][0]
        assert "timer" in p1
        timer = p1["timer"]
        assert timer["mode"] == "count_up"
        assert timer["startedAt"] == "2026-04-15T10:00:00Z"
        assert timer["pausedAt"] is None
        assert timer["completedAt"] is None
        assert timer["accumulatedSecs"] == 0
        assert timer["durationSecs"] is None

    @pytest.mark.asyncio
    async def test_no_timer_when_no_db_row(self, db, db_path):
        """Probe with no timer row in the DB should not have a 'timer' key."""
        svc = await _make_enabled_service(db_path)

        reading = {
            "payload": {
                "sensorId": "AA:BB:CC:DD:EE:FF",
                "sessionId": None,
                "data": {
                    "unit": "F",
                    "probes": [{"index": 1, "temperature": 165.0}],
                },
            }
        }

        state = await svc._build_content_state(reading)

        p1 = state["probes"][0]
        assert "timer" not in p1
        assert state["unit"] == "F"

    @pytest.mark.asyncio
    async def test_battery_percent_not_in_output(self, db, db_path):
        """batteryPercent must not appear in the content state — the iOS
        ContentState struct has no such field."""
        svc = await _make_enabled_service(db_path)

        reading = {
            "payload": {
                "sensorId": "AA:BB:CC:DD:EE:FF",
                "sessionId": None,
                "data": {
                    "unit": "C",
                    "battery_percent": 75,
                    "probes": [{"index": 1, "temperature": 80.0}],
                },
            }
        }

        state = await svc._build_content_state(reading)
        assert "batteryPercent" not in state


# ---------------------------------------------------------------------------
# Connection ownership (B1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_service_owns_its_own_connection(db_path, db):
    """PushService must open its own aiosqlite connection against db_path
    rather than sharing one passed in from a caller. Closing the service
    must not affect any separate connection the test still holds."""
    svc = PushService(
        db_path=db_path,
        key_path="", key_id="", team_id="", bundle_id="", use_sandbox=True,
    )
    await svc.connect()

    # The service's connection is distinct from the fixture's.
    assert svc._db is not None
    assert svc._db is not db

    # Round-trip a write through the service's connection, read back via
    # the independent fixture connection — proves they share the SQLite
    # file under WAL without interference.
    await svc.upsert_token("a" * 64)
    cursor = await db.execute(
        "SELECT token FROM push_tokens WHERE token = ?", ("a" * 64,),
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["token"] == "a" * 64

    await svc.close()
    assert svc._db is None

    # Fixture connection is still usable after svc.close().
    cursor = await db.execute("SELECT COUNT(*) AS n FROM push_tokens")
    row = await cursor.fetchone()
    assert row["n"] == 1
