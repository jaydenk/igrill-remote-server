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
            unit         TEXT NOT NULL DEFAULT 'C',
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


# Services created during a test get appended here and torn down by the
# ``_close_services`` autouse fixture. Each PushService opens its own
# aiosqlite connection — and aiosqlite's worker threads are non-daemon,
# so leaking even one prevents interpreter shutdown and hangs pytest
# (and CI) at Py_FinalizeEx.
_open_services: list[PushService] = []


@pytest_asyncio.fixture(autouse=True)
async def _close_services():
    """Ensure every PushService created in a test is closed on teardown.

    Without this, the aiosqlite connection thread per service leaks as
    a live non-daemon thread, and Python's threading._shutdown blocks
    forever trying to join it.
    """
    yield
    while _open_services:
        svc = _open_services.pop()
        try:
            await svc.close()
        except Exception:
            pass


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
    _open_services.append(svc)
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

    def test_fahrenheit_target_shown_in_c_in_body(self):
        """When the stored target is F but the reading is C, the body must
        show the target converted to C so both numbers share a scale. Without
        conversion a body like 'is at 70° (target: 165°)' would be nonsense."""
        title, body = PushService.format_alert(
            "target_approaching",
            {
                "probeIndex": 1,
                "currentTemp": 72.0,
                "target": {"target_value": 165.0, "label": "Chicken", "unit": "F"},
            },
        )
        # 165F → 73.89C, rounded to 74 at .0f precision
        assert "72" in body
        assert "74" in body
        assert "165" not in body, "raw F value must not leak into a C-scale body"

    def test_fahrenheit_range_shown_in_c_in_body(self):
        title, body = PushService.format_alert(
            "target_reached",
            {
                "probeIndex": 1,
                "currentTemp": 110.0,
                "target": {
                    "range_low": 225.0, "range_high": 250.0,
                    "label": "BBQ", "unit": "F",
                },
            },
        )
        # 225F=107.22C, 250F=121.11C → rounded to 107 and 121.
        assert "107" in body
        assert "121" in body
        assert "225" not in body
        assert "250" not in body


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

    @pytest.mark.asyncio
    async def test_fahrenheit_target_converted_to_celsius(self, db, db_path):
        """Target stored in F must be emitted in the content state as C — iOS
        will convert back to the user's display unit. Top-level unit stays C."""
        svc = await _make_enabled_service(db_path)

        await db.execute(
            "INSERT INTO sessions (id, started_at, start_reason) VALUES (?, ?, ?)",
            ("sess-f", "2026-04-15T10:00:00Z", "manual"),
        )
        await db.execute(
            "INSERT INTO session_targets "
            "(session_id, address, probe_index, mode, target_value, label, unit) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("sess-f", "AA:BB:CC:DD:EE:FF", 1, "fixed", 165.0, "Chicken", "F"),
        )
        await db.commit()

        reading = {
            "payload": {
                "sensorId": "AA:BB:CC:DD:EE:FF",
                "sessionId": "sess-f",
                "data": {
                    "unit": "C",
                    "probes": [{"index": 1, "temperature": 70.0}],
                },
            }
        }

        state = await svc._build_content_state(reading)
        p1 = state["probes"][0]
        expected_c = (165.0 - 32.0) * 5.0 / 9.0
        assert p1["target"] == pytest.approx(expected_c, abs=0.01)
        assert state["unit"] == "C", "top-level unit stays canonical C"

    @pytest.mark.asyncio
    async def test_range_mode_emits_low_high_and_targetmode(self, db, db_path):
        """Range targets emit targetMode='range' + targetLow/targetHigh so iOS
        can render 'in range' rather than 'exceeded' for a mid-range reading."""
        svc = await _make_enabled_service(db_path)

        await db.execute(
            "INSERT INTO sessions (id, started_at, start_reason) VALUES (?, ?, ?)",
            ("sess-r", "2026-04-15T10:00:00Z", "manual"),
        )
        await db.execute(
            "INSERT INTO session_targets "
            "(session_id, address, probe_index, mode, range_low, range_high, label, unit) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("sess-r", "AA:BB:CC:DD:EE:FF", 1, "range", 55.0, 70.0, "Ribs", "C"),
        )
        await db.commit()

        reading = {
            "payload": {
                "sensorId": "AA:BB:CC:DD:EE:FF",
                "sessionId": "sess-r",
                "data": {
                    "unit": "C",
                    "probes": [{"index": 1, "temperature": 60.0}],
                },
            }
        }

        state = await svc._build_content_state(reading)
        p1 = state["probes"][0]
        assert p1["targetMode"] == "range"
        assert p1["targetLow"] == 55.0
        assert p1["targetHigh"] == 70.0
        # The single scalar `target` is the legacy fixed-mode field — it must
        # not appear on a range-mode probe.
        assert "target" not in p1

    @pytest.mark.asyncio
    async def test_range_mode_in_fahrenheit_converts_both_bounds(self, db, db_path):
        """Range target stored in F must have both bounds converted to C."""
        svc = await _make_enabled_service(db_path)

        await db.execute(
            "INSERT INTO sessions (id, started_at, start_reason) VALUES (?, ?, ?)",
            ("sess-rf", "2026-04-15T10:00:00Z", "manual"),
        )
        await db.execute(
            "INSERT INTO session_targets "
            "(session_id, address, probe_index, mode, range_low, range_high, label, unit) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("sess-rf", "AA:BB:CC:DD:EE:FF", 1, "range", 225.0, 250.0, "BBQ", "F"),
        )
        await db.commit()

        reading = {
            "payload": {
                "sensorId": "AA:BB:CC:DD:EE:FF",
                "sessionId": "sess-rf",
                "data": {
                    "unit": "C",
                    "probes": [{"index": 1, "temperature": 110.0}],
                },
            }
        }

        state = await svc._build_content_state(reading)
        p1 = state["probes"][0]
        assert p1["targetMode"] == "range"
        assert p1["targetLow"] == pytest.approx((225.0 - 32.0) * 5.0 / 9.0, abs=0.01)
        assert p1["targetHigh"] == pytest.approx((250.0 - 32.0) * 5.0 / 9.0, abs=0.01)

    @pytest.mark.asyncio
    async def test_fixed_target_emits_targetmode_fixed(self, db, db_path):
        """Fixed mode emits targetMode='fixed' alongside target so iOS can
        dispatch on the mode rather than inferring from field presence."""
        svc = await _make_enabled_service(db_path)

        await db.execute(
            "INSERT INTO sessions (id, started_at, start_reason) VALUES (?, ?, ?)",
            ("sess-fx", "2026-04-15T10:00:00Z", "manual"),
        )
        await db.execute(
            "INSERT INTO session_targets "
            "(session_id, address, probe_index, mode, target_value, label, unit) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("sess-fx", "AA:BB:CC:DD:EE:FF", 1, "fixed", 93.0, "Brisket", "C"),
        )
        await db.commit()

        reading = {
            "payload": {
                "sensorId": "AA:BB:CC:DD:EE:FF",
                "sessionId": "sess-fx",
                "data": {
                    "unit": "C",
                    "probes": [{"index": 1, "temperature": 85.0}],
                },
            }
        }

        state = await svc._build_content_state(reading)
        p1 = state["probes"][0]
        assert p1["target"] == 93.0
        assert p1["targetMode"] == "fixed"
        assert "targetLow" not in p1
        assert "targetHigh" not in p1

    @pytest.mark.asyncio
    async def test_celsius_target_passed_through_unchanged(self, db, db_path):
        """Target stored in C must be emitted unchanged."""
        svc = await _make_enabled_service(db_path)

        await db.execute(
            "INSERT INTO sessions (id, started_at, start_reason) VALUES (?, ?, ?)",
            ("sess-c", "2026-04-15T10:00:00Z", "manual"),
        )
        await db.execute(
            "INSERT INTO session_targets "
            "(session_id, address, probe_index, mode, target_value, label, unit) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("sess-c", "AA:BB:CC:DD:EE:FF", 1, "fixed", 90.0, "Pork", "C"),
        )
        await db.commit()

        reading = {
            "payload": {
                "sensorId": "AA:BB:CC:DD:EE:FF",
                "sessionId": "sess-c",
                "data": {
                    "unit": "C",
                    "probes": [{"index": 1, "temperature": 85.0}],
                },
            }
        }

        state = await svc._build_content_state(reading)
        assert state["probes"][0]["target"] == 90.0


# ---------------------------------------------------------------------------
# la-followups Task 8 — multi-device content-state
# ---------------------------------------------------------------------------


class TestMultiDeviceContentState:
    """_build_content_state must surface probes from every device in the
    session, not just the firing one — otherwise a multi-device Live
    Activity flickers between disjoint probe sets as pushes arrive."""

    @pytest.mark.asyncio
    async def test_includes_other_device_probes_as_stubs(self, db, db_path):
        svc = await _make_enabled_service(db_path)

        await db.execute(
            "INSERT INTO sessions (id, started_at, start_reason) VALUES (?, ?, ?)",
            ("sess-m", "2026-04-17T10:00:00Z", "manual"),
        )
        await db.execute(
            "INSERT INTO session_targets "
            "(session_id, address, probe_index, mode, target_value, label, unit) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("sess-m", "AA:BB:CC:DD:EE:01", 1, "fixed", 60.0, "Brisket", "C"),
        )
        await db.execute(
            "INSERT INTO session_targets "
            "(session_id, address, probe_index, mode, target_value, label, unit) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("sess-m", "AA:BB:CC:DD:EE:02", 1, "fixed", 110.0, "Pit", "C"),
        )
        await db.commit()

        # Firing reading is from device A.
        reading = {
            "payload": {
                "sensorId": "AA:BB:CC:DD:EE:01",
                "sessionId": "sess-m",
                "data": {
                    "unit": "C",
                    "probes": [{"index": 1, "temperature": 55.0}],
                },
            }
        }
        state = await svc._build_content_state(reading)

        # Expect two probes: one live from A (index 1, with temperature)
        # and one stub from B (index 1, unplugged, no temperature).
        assert len(state["probes"]) == 2
        by_addr = {
            (p["deviceAddress"], p["index"]): p for p in state["probes"]
        }
        live = by_addr[("AA:BB:CC:DD:EE:01", 1)]
        stub = by_addr[("AA:BB:CC:DD:EE:02", 1)]

        assert live["temperature"] == 55.0
        assert live["unplugged"] is False
        assert live["target"] == 60.0
        assert live["label"] == "Brisket"

        assert "temperature" not in stub
        assert stub["unplugged"] is True
        assert stub["target"] == 110.0
        assert stub["label"] == "Pit"

    @pytest.mark.asyncio
    async def test_single_device_session_still_emits_deviceAddress(self, db, db_path):
        """deviceAddress must appear on every ProbeState, not just
        multi-device cooks — the iOS decoder (Task 10) requires it to
        distinguish probes across devices."""
        svc = await _make_enabled_service(db_path)
        await db.execute(
            "INSERT INTO sessions (id, started_at, start_reason) VALUES (?, ?, ?)",
            ("sess-s", "2026-04-17T10:00:00Z", "manual"),
        )
        await db.execute(
            "INSERT INTO session_targets "
            "(session_id, address, probe_index, mode, target_value, label, unit) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("sess-s", "AA:BB:CC:DD:EE:01", 1, "fixed", 74.0, "Pork", "C"),
        )
        await db.commit()

        reading = {
            "payload": {
                "sensorId": "AA:BB:CC:DD:EE:01",
                "sessionId": "sess-s",
                "data": {
                    "unit": "C",
                    "probes": [{"index": 1, "temperature": 70.0}],
                },
            }
        }
        state = await svc._build_content_state(reading)
        assert state["probes"][0]["deviceAddress"] == "AA:BB:CC:DD:EE:01"


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


# ---------------------------------------------------------------------------
# LA token preservation on upsert (E22)
# ---------------------------------------------------------------------------


class TestUpsertTokenPreservesLATokenOnNull:
    @pytest.mark.asyncio
    async def test_null_la_token_does_not_clobber_existing(self, db, db_path):
        """If a caller re-registers only the APNS token (e.g. APNS token
        rotation) WITHOUT a liveActivityToken, the existing stored LA
        token must be preserved — iOS otherwise silently loses Live
        Activity updates until the user starts another session."""
        svc = await _make_enabled_service(db_path)
        await svc.upsert_token("device_token_abc", live_activity_token="la_xyz")
        await svc.upsert_token("device_token_abc")  # no LA token this time

        cursor = await db.execute(
            "SELECT live_activity_token FROM push_tokens WHERE token = ?",
            ("device_token_abc",),
        )
        row = await cursor.fetchone()
        assert row["live_activity_token"] == "la_xyz", \
            "APNS-only re-register nulled the previously-stored LA token"

    @pytest.mark.asyncio
    async def test_explicit_la_token_replaces_existing(self, db, db_path):
        """An explicit new liveActivityToken still replaces the old one —
        this is how iOS rotates LA tokens session-to-session."""
        svc = await _make_enabled_service(db_path)
        await svc.upsert_token("device_token_abc", live_activity_token="la_old")
        await svc.upsert_token("device_token_abc", live_activity_token="la_new")

        cursor = await db.execute(
            "SELECT live_activity_token FROM push_tokens WHERE token = ?",
            ("device_token_abc",),
        )
        row = await cursor.fetchone()
        assert row["live_activity_token"] == "la_new"


# ---------------------------------------------------------------------------
# Live Activity teardown on session end (E23)
# ---------------------------------------------------------------------------


class TestEndLiveActivities:
    @pytest.mark.asyncio
    async def test_end_live_activities_sends_end_event_to_all_la_tokens(
        self, db, db_path,
    ):
        """end_live_activities must send aps.event='end' to every stored
        LA token so the Live Activity actually dismisses from the lock
        screen on session end."""
        import sys
        import types

        # aioapns isn't installed in the test environment — stub it out
        # just enough that end_live_activities' local import succeeds.
        fake_aioapns = types.ModuleType("aioapns")

        class _FakeNotificationRequest:
            def __init__(self, *, device_token, message, notification_id,
                         push_type, apns_topic=None):
                self.device_token = device_token
                self.message = message

        class _FakePushType:
            LIVEACTIVITY = "liveactivity"

        fake_aioapns.NotificationRequest = _FakeNotificationRequest
        fake_aioapns.PushType = _FakePushType

        with patch.dict(sys.modules, {"aioapns": fake_aioapns}):
            svc = await _make_enabled_service(db_path)
            await svc.upsert_token("t1", live_activity_token="la1")
            await svc.upsert_token("t2", live_activity_token="la2")
            await svc.upsert_token("t3", live_activity_token=None)  # no LA

            mock_client = AsyncMock()
            mock_client.send_notification = AsyncMock(
                return_value=MagicMock(is_successful=True, description=""),
            )
            svc._client = mock_client

            await svc.end_live_activities()

        assert mock_client.send_notification.await_count == 2
        sent_tokens = {
            call.args[0].device_token
            for call in mock_client.send_notification.await_args_list
        }
        assert sent_tokens == {"la1", "la2"}
        for call in mock_client.send_notification.await_args_list:
            request = call.args[0]
            assert request.message["aps"]["event"] == "end"

    @pytest.mark.asyncio
    async def test_end_live_activities_noop_when_disabled(self, db, db_path):
        """Disabled push service end_live_activities is a silent no-op."""
        svc = await _make_service(db_path)  # no credentials → disabled
        assert svc.enabled is False
        await svc.end_live_activities()


# ---------------------------------------------------------------------------
# LA throttle only advances after success (E25)
# ---------------------------------------------------------------------------


class TestLiveActivityThrottleAfterSuccess:
    @pytest.mark.asyncio
    async def test_throttle_does_not_advance_on_total_failure(
        self, db, db_path,
    ):
        """If every LA push fails (e.g. APNS connection dead), the
        throttle must NOT advance — otherwise the next ~15s of live
        readings also carry no push, compounding the outage."""
        import sys
        import types

        fake_aioapns = types.ModuleType("aioapns")

        class _FakeNotificationRequest:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        class _FakePushType:
            LIVEACTIVITY = "liveactivity"

        fake_aioapns.NotificationRequest = _FakeNotificationRequest
        fake_aioapns.PushType = _FakePushType

        with patch.dict(sys.modules, {"aioapns": fake_aioapns}):
            svc = await _make_enabled_service(db_path)
            await svc.upsert_token("t1", live_activity_token="la1")

            mock_client = AsyncMock()
            mock_client.send_notification = AsyncMock(
                return_value=MagicMock(
                    is_successful=False, description="TransientFailure",
                ),
            )
            svc._client = mock_client

            before = svc._last_la_update_ts
            reading = {
                "payload": {"sensorId": "A", "sessionId": None,
                            "data": {"unit": "C", "probes": []}},
            }
            await svc.send_live_activity_update(reading)

        assert svc._last_la_update_ts == before, \
            "throttle advanced despite zero successful pushes"

    @pytest.mark.asyncio
    async def test_throttle_advances_on_any_success(self, db, db_path):
        import sys
        import types

        fake_aioapns = types.ModuleType("aioapns")

        class _FakeNotificationRequest:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        class _FakePushType:
            LIVEACTIVITY = "liveactivity"

        fake_aioapns.NotificationRequest = _FakeNotificationRequest
        fake_aioapns.PushType = _FakePushType

        with patch.dict(sys.modules, {"aioapns": fake_aioapns}):
            svc = await _make_enabled_service(db_path)
            await svc.upsert_token("t1", live_activity_token="la1")

            mock_client = AsyncMock()
            mock_client.send_notification = AsyncMock(
                return_value=MagicMock(is_successful=True, description=""),
            )
            svc._client = mock_client

            before = svc._last_la_update_ts
            reading = {
                "payload": {"sensorId": "A", "sessionId": None,
                            "data": {"unit": "C", "probes": []}},
            }
            await svc.send_live_activity_update(reading)

        assert svc._last_la_update_ts > before


# ---------------------------------------------------------------------------
# send_alert fans out with gather + retry (E24)
# ---------------------------------------------------------------------------


def _install_fake_aioapns():
    """Install a minimal aioapns stub into sys.modules for the duration of
    the caller's `with` block. Mirrors the helper pattern used elsewhere in
    this file."""
    import sys
    import types

    fake_aioapns = types.ModuleType("aioapns")

    class _FakeNotificationRequest:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class _FakePushType:
        ALERT = "alert"
        LIVEACTIVITY = "liveactivity"

    fake_aioapns.NotificationRequest = _FakeNotificationRequest
    fake_aioapns.PushType = _FakePushType
    return patch.dict(sys.modules, {"aioapns": fake_aioapns})


class TestSendAlertFanOut:
    """E24: send_alert must fan out concurrently so one slow peer can't
    stall others, retry once on transient 5xx, and drop permanently on 4xx.
    """

    @pytest.mark.asyncio
    async def test_send_alert_fires_all_tokens_concurrently(self, db, db_path):
        """With N tokens, all N send_notification calls must run — even if
        one raises mid-flight. Serial `for` + `await` would short-circuit
        on the first exception."""
        with _install_fake_aioapns():
            svc = await _make_enabled_service(db_path)
            await svc.upsert_token("t1")
            await svc.upsert_token("t2")
            await svc.upsert_token("t3")

            call_count = {"n": 0}

            async def send(request):
                call_count["n"] += 1
                if request.device_token == "t2":
                    raise RuntimeError("simulated timeout for t2")
                return MagicMock(is_successful=True, description="", status=200)

            mock_client = AsyncMock()
            mock_client.send_notification = AsyncMock(side_effect=send)
            svc._client = mock_client

            # Must not raise even though t2's send throws.
            await svc.send_alert({
                "type": "target_reached",
                "payload": {
                    "probeIndex": 1, "currentTemp": 90,
                    "target": {"target_value": 90, "label": None, "unit": "C"},
                },
            })

        assert call_count["n"] == 3, (
            f"expected fan-out to all 3 tokens, got {call_count['n']}"
        )

    @pytest.mark.asyncio
    async def test_send_alert_retries_once_on_transient_5xx(self, db, db_path):
        """A transient 5xx (InternalServerError / status >= 500) must be
        retried exactly once after a short backoff."""
        with _install_fake_aioapns():
            svc = await _make_enabled_service(db_path)
            await svc.upsert_token("t1")

            call_log: list[int] = []

            async def send(request):
                call_log.append(len(call_log))
                # First call fails with 5xx; second succeeds.
                if len(call_log) == 1:
                    return MagicMock(
                        is_successful=False,
                        description="InternalServerError",
                        status=500,
                    )
                return MagicMock(is_successful=True, description="", status=200)

            mock_client = AsyncMock()
            mock_client.send_notification = AsyncMock(side_effect=send)
            svc._client = mock_client

            await svc.send_alert({
                "type": "target_reached",
                "payload": {
                    "probeIndex": 1, "currentTemp": 90,
                    "target": {"target_value": 90, "label": None, "unit": "C"},
                },
            })

        assert len(call_log) == 2, (
            f"expected 1 initial + 1 retry = 2 calls, got {len(call_log)}"
        )

    @pytest.mark.asyncio
    async def test_send_alert_does_not_retry_on_4xx(self, db, db_path):
        """4xx responses (BadDeviceToken, Unregistered, any client error)
        must NOT retry — the problem is permanent and a retry is wasted
        APNS traffic."""
        with _install_fake_aioapns():
            svc = await _make_enabled_service(db_path)
            await svc.upsert_token("t1")

            call_log: list[int] = []

            async def send(request):
                call_log.append(len(call_log))
                return MagicMock(
                    is_successful=False,
                    description="BadDeviceToken",
                    status=400,
                )

            mock_client = AsyncMock()
            mock_client.send_notification = AsyncMock(side_effect=send)
            svc._client = mock_client

            await svc.send_alert({
                "type": "target_reached",
                "payload": {
                    "probeIndex": 1, "currentTemp": 90,
                    "target": {"target_value": 90, "label": None, "unit": "C"},
                },
            })

        assert len(call_log) == 1, (
            f"expected no retry on 4xx, got {len(call_log)} calls"
        )

    @pytest.mark.asyncio
    async def test_send_alert_removes_invalid_token_on_4xx(self, db, db_path):
        """When a 4xx indicates the token is dead (BadDeviceToken,
        Unregistered), the push_tokens row must be removed so we don't
        keep sending to it."""
        with _install_fake_aioapns():
            svc = await _make_enabled_service(db_path)
            await svc.upsert_token("t1")

            mock_client = AsyncMock()
            mock_client.send_notification = AsyncMock(
                return_value=MagicMock(
                    is_successful=False,
                    description="BadDeviceToken",
                    status=400,
                ),
            )
            svc._client = mock_client

            await svc.send_alert({
                "type": "target_reached",
                "payload": {
                    "probeIndex": 1, "currentTemp": 90,
                    "target": {"target_value": 90, "label": None, "unit": "C"},
                },
            })

        cursor = await db.execute(
            "SELECT COUNT(*) FROM push_tokens WHERE token = ?", ("t1",),
        )
        row = await cursor.fetchone()
        assert row[0] == 0, "BadDeviceToken should have removed the token"
