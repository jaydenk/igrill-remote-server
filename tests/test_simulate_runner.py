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
    async def test_start_creates_session(self, runner):
        result = await runner.start(speed=100, probes=4)
        assert result["sessionId"] is not None
        await runner.stop()

    @pytest.mark.asyncio
    async def test_cannot_start_twice(self, runner):
        await runner.start(speed=100, probes=2)
        result = await runner.start(speed=100, probes=2)
        assert "error" in result
        await runner.stop()

    @pytest.mark.asyncio
    async def test_stop_ends_session(self, runner):
        await runner.start(speed=100, probes=2)
        result = await runner.stop()
        assert result["ok"] is True
        assert not runner.is_running
        device = await runner.store.get_device(SIM_ADDRESS)
        assert device["connected"] is False

    @pytest.mark.asyncio
    async def test_produces_readings(self, runner):
        await runner.start(speed=1000, probes=2)
        await asyncio.sleep(0.1)  # Let a few ticks fire
        await runner.stop()
        # Check that readings were published
        assert runner._tick > 0

    @pytest.mark.asyncio
    async def test_start_with_probe_timers_creates_timer_rows(self, runner, history):
        """SimulationRunner.start(probe_timers=...) writes session_timers rows
        so end-to-end LA + per-probe-timer flows can be exercised by the sim."""
        result = await runner.start(
            speed=10,
            probes=2,
            probe_timers={
                1: {"mode": "count_up"},
                2: {"mode": "count_down", "duration_secs": 600},
            },
        )
        assert result["ok"] is True
        session_id = result["sessionId"]

        # Stop immediately so we don't race the loop.
        await runner.stop()

        rows = await history.get_timers(session_id)
        by_index = {r["probe_index"]: r for r in rows}
        assert set(by_index.keys()) == {1, 2}
        assert by_index[1]["mode"] == "count_up"
        assert by_index[1]["duration_secs"] is None
        assert by_index[2]["mode"] == "count_down"
        assert by_index[2]["duration_secs"] == 600
