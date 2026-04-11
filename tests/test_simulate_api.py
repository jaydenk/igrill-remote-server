import pytest
import pytest_asyncio
from aiohttp import web
from service.models.device import DeviceStore
from service.history.store import HistoryStore
from service.alerts.evaluator import AlertEvaluator
from service.simulate.runner import SimulationRunner


@pytest_asyncio.fixture
async def client(aiohttp_client, tmp_path):
    """Create a minimal aiohttp app with simulation routes."""
    from service.api.routes import setup_routes

    app = web.Application()
    store = DeviceStore()
    history = HistoryStore(str(tmp_path / "test.db"), reconnect_grace=60)
    await history.connect()
    evaluator = AlertEvaluator()

    app["store"] = store
    app["history"] = history
    app["evaluator"] = evaluator
    app["config"] = type("Config", (), {"session_token": "", "poll_interval": 15})()
    app["simulator"] = SimulationRunner(
        store=store, history=history, evaluator=evaluator, poll_interval=15,
    )

    setup_routes(app)
    client = await aiohttp_client(app)
    yield client
    await history.close()


class TestSimulateAPI:
    @pytest.mark.asyncio
    async def test_start_simulation(self, client):
        resp = await client.post("/api/v1/simulate/start", json={"speed": 100, "probes": 2})
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True
        assert "sessionId" in data
        # Stop it
        await client.post("/api/v1/simulate/stop")

    @pytest.mark.asyncio
    async def test_start_twice_returns_error(self, client):
        await client.post("/api/v1/simulate/start", json={"speed": 100})
        resp = await client.post("/api/v1/simulate/start", json={"speed": 100})
        assert resp.status == 409
        await client.post("/api/v1/simulate/stop")

    @pytest.mark.asyncio
    async def test_stop_simulation(self, client):
        await client.post("/api/v1/simulate/start", json={"speed": 100})
        resp = await client.post("/api/v1/simulate/stop")
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True

    @pytest.mark.asyncio
    async def test_stop_without_start_returns_error(self, client):
        resp = await client.post("/api/v1/simulate/stop")
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_default_parameters(self, client):
        resp = await client.post("/api/v1/simulate/start")
        data = await resp.json()
        assert data["speed"] == 10
        assert data["probes"] == 4
        await client.post("/api/v1/simulate/stop")
