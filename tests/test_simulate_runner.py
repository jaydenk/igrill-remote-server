import asyncio
import pytest
import pytest_asyncio
from service.models.device import DeviceStore
from service.history.store import HistoryStore
from service.alerts.evaluator import AlertEvaluator
from service.simulate.runner import SimulationRunner

SIM_ADDRESS = "SIM:UL:AT:ED:00:01"


@pytest_asyncio.fixture
async def history(tmp_path):
    s = HistoryStore(str(tmp_path / "test.db"), reconnect_grace=60)
    await s.connect()
    yield s
    await s.close()


@pytest.fixture
def runner(history):
    store = DeviceStore()
    evaluator = AlertEvaluator()
    return SimulationRunner(
        store=store,
        history=history,
        evaluator=evaluator,
        poll_interval=15,
    )


class TestSimulationRunner:
    @pytest.mark.asyncio
    async def test_not_running_initially(self, runner):
        assert not runner.is_running

    @pytest.mark.asyncio
    async def test_start_registers_device(self, runner):
        result = await runner.start(speed=100, probes=2)
        assert result["ok"] is True
        assert result["deviceAddress"] == SIM_ADDRESS
        assert result["probes"] == 2
        device = await runner.store.get_device(SIM_ADDRESS)
        assert device is not None
        assert device["connected"] is True
        assert device["name"] == "Simulated iGrill V2"
        await runner.stop()

    @pytest.mark.asyncio
    async def test_start_does_not_create_session(self, runner, history):
        """The simulator must present like a real BLE device: register and
        broadcast, never start a cook session. Users own session lifecycle."""
        result = await runner.start(speed=100, probes=4)
        assert "sessionId" not in result
        state = await history.get_session_state()
        assert state.get("current_session_id") is None
        await runner.stop()

    @pytest.mark.asyncio
    async def test_cannot_start_twice(self, runner):
        await runner.start(speed=100, probes=2)
        result = await runner.start(speed=100, probes=2)
        assert "error" in result
        await runner.stop()

    @pytest.mark.asyncio
    async def test_stop_disconnects_device_without_ending_session(self, runner, history):
        """Stop must mark the synthetic device disconnected but leave any
        user-owned session alone — stopping the sim mid-cook shouldn't end
        the user's cook."""
        await runner.start(speed=100, probes=2)
        # User starts a cook that includes the sim device.
        session_info = await history.start_session(
            addresses=[SIM_ADDRESS], reason="user", name="User Cook",
        )
        result = await runner.stop()
        assert result["ok"] is True
        assert not runner.is_running
        device = await runner.store.get_device(SIM_ADDRESS)
        assert device["connected"] is False
        # Session must still be the user's — sim.stop() doesn't end it.
        state = await history.get_session_state()
        assert state.get("current_session_id") == session_info["session_id"]

    @pytest.mark.asyncio
    async def test_produces_readings(self, runner):
        await runner.start(speed=1000, probes=2)
        await asyncio.sleep(0.1)  # Let a few ticks fire
        await runner.stop()
        assert runner._tick > 0

    @pytest.mark.asyncio
    async def test_readings_not_recorded_without_session(self, runner, history):
        """Without an active session, readings publish to WS but do not
        persist to history — the sim is just a live-preview device."""
        await runner.start(speed=1000, probes=2)
        await asyncio.sleep(0.1)
        await runner.stop()
        # No session ever started, so no history rows against the sim.
        async with history._conn.execute(
            "SELECT COUNT(*) AS n FROM probe_readings WHERE address = ?",
            (SIM_ADDRESS,),
        ) as cur:
            row = await cur.fetchone()
            assert row["n"] == 0

    @pytest.mark.asyncio
    async def test_readings_recorded_when_user_session_includes_sim(self, runner, history):
        """Once the user starts a session that includes the sim device,
        the sim's readings start landing in history the same way a real
        device's would."""
        await runner.start(speed=1000, probes=2)
        await history.start_session(
            addresses=[SIM_ADDRESS], reason="user", name="User Cook",
        )
        # Give the loop a beat to observe the session.
        await asyncio.sleep(0.15)
        await runner.stop()

        async with history._conn.execute(
            "SELECT COUNT(*) AS n FROM probe_readings WHERE address = ?",
            (SIM_ADDRESS,),
        ) as cur:
            row = await cur.fetchone()
            assert row["n"] > 0
