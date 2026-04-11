"""Tests for the PushService class."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from service.push.service import PushService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db(tmp_path):
    """Create a temporary SQLite database with the push_tokens table."""
    import aiosqlite

    db_path = str(tmp_path / "test.db")
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
        """
    )
    yield conn
    await conn.close()


def _make_service(db, **kwargs):
    """Create a PushService with sensible defaults."""
    defaults = {
        "key_path": "",
        "key_id": "",
        "team_id": "",
        "bundle_id": "",
        "use_sandbox": True,
    }
    defaults.update(kwargs)
    return PushService(db=db, **defaults)


def _make_enabled_service(db):
    """Create a PushService with all credentials set (but no real APNS)."""
    return _make_service(
        db,
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
    async def test_disabled_when_no_credentials(self, db):
        svc = _make_service(db)
        assert svc.enabled is False

    @pytest.mark.asyncio
    async def test_disabled_when_partial_credentials(self, db):
        svc = _make_service(db, key_path="/some/path", key_id="KEYID")
        assert svc.enabled is False

    @pytest.mark.asyncio
    async def test_send_alert_is_noop_when_disabled(self, db):
        svc = _make_service(db)
        # Should not raise
        await svc.send_alert({"type": "target_reached", "payload": {}})

    @pytest.mark.asyncio
    async def test_send_la_update_is_noop_when_disabled(self, db):
        svc = _make_service(db)
        # Should not raise
        await svc.send_live_activity_update({"probes": []})

    @pytest.mark.asyncio
    async def test_enabled_when_all_credentials(self, db):
        svc = _make_enabled_service(db)
        assert svc.enabled is True


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------


class TestTokenManagement:
    @pytest.mark.asyncio
    async def test_upsert_token(self, db):
        svc = _make_enabled_service(db)
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
    async def test_upsert_token_with_la_token(self, db):
        svc = _make_enabled_service(db)
        await svc.upsert_token("device_token_abc", live_activity_token="la_token_xyz")

        cursor = await db.execute(
            "SELECT token, live_activity_token FROM push_tokens WHERE token = ?",
            ("device_token_abc",),
        )
        row = await cursor.fetchone()
        assert row["live_activity_token"] == "la_token_xyz"

    @pytest.mark.asyncio
    async def test_upsert_token_replaces_existing(self, db):
        svc = _make_enabled_service(db)
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
    async def test_remove_token(self, db):
        svc = _make_enabled_service(db)
        await svc.upsert_token("device_token_abc")
        await svc.remove_token("device_token_abc")

        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM push_tokens WHERE token = ?",
            ("device_token_abc",),
        )
        row = await cursor.fetchone()
        assert row["cnt"] == 0

    @pytest.mark.asyncio
    async def test_remove_nonexistent_token(self, db):
        svc = _make_enabled_service(db)
        # Should not raise
        await svc.remove_token("nonexistent")

    @pytest.mark.asyncio
    async def test_get_all_tokens(self, db):
        svc = _make_enabled_service(db)
        await svc.upsert_token("token_a")
        await svc.upsert_token("token_b")
        await svc.upsert_token("token_c")

        tokens = await svc._get_all_tokens()
        assert set(tokens) == {"token_a", "token_b", "token_c"}

    @pytest.mark.asyncio
    async def test_get_la_tokens(self, db):
        svc = _make_enabled_service(db)
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
    def test_should_send_initially(self, db):
        svc = _make_enabled_service(db)
        assert svc.should_send_la_update() is True

    def test_should_skip_if_too_soon(self, db):
        svc = _make_enabled_service(db)
        # Simulate having just sent
        svc._last_la_update_ts = time.monotonic()
        assert svc.should_send_la_update() is False

    def test_should_allow_after_interval(self, db):
        svc = _make_enabled_service(db)
        # Simulate having sent 16 seconds ago
        svc._last_la_update_ts = time.monotonic() - 16
        assert svc.should_send_la_update() is True

    def test_should_skip_at_exactly_interval(self, db):
        svc = _make_enabled_service(db)
        # Simulate having sent exactly 15 seconds ago (boundary)
        svc._last_la_update_ts = time.monotonic() - 15
        # At exactly 15 seconds, should allow (>= check)
        assert svc.should_send_la_update() is True

    def test_should_skip_just_under_interval(self, db):
        svc = _make_enabled_service(db)
        svc._last_la_update_ts = time.monotonic() - 14.9
        assert svc.should_send_la_update() is False
