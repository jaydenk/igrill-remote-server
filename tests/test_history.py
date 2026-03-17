"""Tests for the HistoryStore (sessions, readings, session targets).

Updated to match the rewritten HistoryStore API which uses UUID session
IDs, user-initiated sessions, and normalised schema with per-device
address on targets.
"""

import asyncio
import os

import pytest

from service.history.store import HistoryStore
from service.models.session import TargetConfig

_TEST_ADDRESS = "AA:BB:CC:DD:EE:FF"


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


class TestNoSessionOnInit:
    def test_no_session_on_init(self, tmp_path):
        """Constructing a HistoryStore should NOT create an initial session."""
        async def _test():
            store = _make_store(str(tmp_path))
            state = await store.get_session_state()
            assert state["current_session_id"] is None
            assert state["current_session_start_ts"] is None

        _run(_test())


class TestStartSession:
    def test_start_session(self, tmp_path):
        """Starting a session should produce a valid UUID session ID."""
        async def _test():
            store = _make_store(str(tmp_path))
            result = await store.start_session(
                addresses=[_TEST_ADDRESS], reason="manual"
            )
            new_id = result["session_id"]

            assert new_id is not None
            assert isinstance(new_id, str)
            assert len(new_id) == 32  # UUID hex
            assert result["start_event"] is not None
            assert result["start_event"]["reason"] == "manual"

            state_after = await store.get_session_state()
            assert state_after["current_session_id"] == new_id

        _run(_test())


class TestStartSessionEndsPrevious:
    def test_start_session_ends_previous(self, tmp_path):
        """Starting a new session should auto-end the previous one."""
        async def _test():
            store = _make_store(str(tmp_path))
            r1 = await store.start_session(
                addresses=[_TEST_ADDRESS], reason="manual"
            )
            old_id = r1["session_id"]

            r2 = await store.start_session(
                addresses=[_TEST_ADDRESS], reason="manual"
            )
            new_id = r2["session_id"]

            assert new_id != old_id
            assert r2["end_event"] is not None
            assert r2["end_event"]["sessionId"] == old_id

        _run(_test())


# -----------------------------------------------------------------------
# Session targets
# -----------------------------------------------------------------------


class TestSaveAndGetTargets:
    def test_save_and_get_targets(self, tmp_path):
        """Saved targets should be retrievable with matching field values."""
        async def _test():
            store = _make_store(str(tmp_path))
            result = await store.start_session(
                addresses=[_TEST_ADDRESS], reason="user"
            )
            session_id = result["session_id"]

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
            await store.save_targets(session_id, _TEST_ADDRESS, targets)

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
            result = await store.start_session(
                addresses=[_TEST_ADDRESS], reason="user"
            )
            session_id = result["session_id"]

            # Save initial targets
            initial_targets = [
                TargetConfig(probe_index=0, mode="fixed", target_value=74.0),
                TargetConfig(probe_index=1, mode="fixed", target_value=80.0),
            ]
            await store.save_targets(session_id, _TEST_ADDRESS, initial_targets)

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
            await store.update_targets(session_id, _TEST_ADDRESS, updated_targets)

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
