"""Tests for the HistoryStore (sessions, readings, session targets)."""

import asyncio
import os
import tempfile

import pytest

from service.history.store import HistoryStore
from service.models.session import TargetConfig


def _run(coro):
    """Run an async coroutine synchronously (avoids pytest-asyncio dependency)."""
    return asyncio.run(coro)


def _make_store(tmp_path: str, reconnect_grace: int = 300) -> HistoryStore:
    """Create a HistoryStore backed by a temporary SQLite file."""
    db_path = os.path.join(tmp_path, "test_history.db")
    return HistoryStore(db_path, reconnect_grace)


# -----------------------------------------------------------------------
# Session lifecycle
# -----------------------------------------------------------------------


class TestSessionCreatedOnInit:
    def test_session_created_on_init(self, tmp_path):
        """Constructing a HistoryStore should create an initial session."""
        async def _test():
            store = _make_store(str(tmp_path))
            state = await store.get_session_state()
            assert state["current_session_id"] is not None
            assert isinstance(state["current_session_id"], int)
            assert state["current_session_start_ts"] is not None

        _run(_test())


class TestForceNewSession:
    def test_force_new_session(self, tmp_path):
        """Forcing a new session should produce a different session ID."""
        async def _test():
            store = _make_store(str(tmp_path))
            state_before = await store.get_session_state()
            old_id = state_before["current_session_id"]

            result = await store.force_new_session(
                "2026-01-01T00:00:00+00:00", "AA:BB:CC:DD:EE:FF", "manual"
            )
            new_id = result["session_id"]

            assert new_id != old_id
            assert result["start_event"] is not None
            assert result["start_event"]["reason"] == "manual"

            state_after = await store.get_session_state()
            assert state_after["current_session_id"] == new_id

        _run(_test())


# -----------------------------------------------------------------------
# Session targets
# -----------------------------------------------------------------------


class TestSaveAndGetTargets:
    def test_save_and_get_targets(self, tmp_path):
        """Saved targets should be retrievable with matching field values."""
        async def _test():
            store = _make_store(str(tmp_path))
            state = await store.get_session_state()
            session_id = state["current_session_id"]

            targets = [
                TargetConfig(probe_index=0, mode="fixed", target_value=74.0),
                TargetConfig(
                    probe_index=1,
                    mode="range",
                    range_low=60.0,
                    range_high=80.0,
                    pre_alert_offset=5.0,
                    reminder_interval_secs=120,
                ),
            ]
            await store.save_targets(session_id, targets)

            loaded = await store.get_targets(session_id)
            assert len(loaded) == 2

            t0 = loaded[0]
            assert t0.probe_index == 0
            assert t0.mode == "fixed"
            assert t0.target_value == 74.0

            t1 = loaded[1]
            assert t1.probe_index == 1
            assert t1.mode == "range"
            assert t1.range_low == 60.0
            assert t1.range_high == 80.0
            assert t1.pre_alert_offset == 5.0
            assert t1.reminder_interval_secs == 120

        _run(_test())


class TestUpdateTargets:
    def test_update_targets(self, tmp_path):
        """Updating targets should replace all existing targets for the session."""
        async def _test():
            store = _make_store(str(tmp_path))
            state = await store.get_session_state()
            session_id = state["current_session_id"]

            # Save initial targets
            initial_targets = [
                TargetConfig(probe_index=0, mode="fixed", target_value=74.0),
                TargetConfig(probe_index=1, mode="fixed", target_value=80.0),
            ]
            await store.save_targets(session_id, initial_targets)

            # Update with different targets
            updated_targets = [
                TargetConfig(
                    probe_index=0,
                    mode="range",
                    range_low=55.0,
                    range_high=65.0,
                    pre_alert_offset=8.0,
                    reminder_interval_secs=60,
                ),
            ]
            await store.update_targets(session_id, updated_targets)

            loaded = await store.get_targets(session_id)
            assert len(loaded) == 1

            t = loaded[0]
            assert t.probe_index == 0
            assert t.mode == "range"
            assert t.range_low == 55.0
            assert t.range_high == 65.0
            assert t.pre_alert_offset == 8.0
            assert t.reminder_interval_secs == 60
            # Original target_value should be None for a range target
            assert t.target_value is None

        _run(_test())
