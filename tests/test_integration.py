"""Integration tests for the full server."""

import pytest
import pytest_asyncio
from service.main import create_app
from service.config import Config


@pytest.fixture
def config(tmp_db):
    return Config(db_path=tmp_db)


@pytest_asyncio.fixture
async def client(aiohttp_client, config):
    app = create_app(config)
    return await aiohttp_client(app)


@pytest.mark.asyncio
async def test_health_check(client):
    """Health endpoint returns ok."""
    resp = await client.get("/health")
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_no_sessions_on_start(client):
    """No sessions exist initially."""
    resp = await client.get("/api/sessions")
    assert resp.status == 200
    data = await resp.json()
    assert data["sessions"] == []


@pytest.mark.asyncio
async def test_metrics_renders(client):
    """Metrics endpoint serves text."""
    resp = await client.get("/metrics")
    assert resp.status == 200
    assert "text/plain" in resp.content_type


@pytest.mark.asyncio
async def test_dashboard_loads(client):
    """Dashboard HTML loads."""
    resp = await client.get("/")
    assert resp.status == 200
    text = await resp.text()
    assert "iGrill Remote" in text


@pytest.mark.asyncio
async def test_session_detail_empty(client):
    """Session detail for nonexistent ID returns empty."""
    resp = await client.get("/api/sessions/nonexistent")
    assert resp.status == 200
    data = await resp.json()
    assert data["readings"] == []


@pytest.mark.asyncio
async def test_log_levels_update(client):
    """Can update log levels at runtime."""
    resp = await client.put(
        "/api/config/log-levels",
        json={"igrill.ble": "DEBUG"},
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["results"]["igrill.ble"] is True
