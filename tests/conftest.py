"""Shared fixtures for iGrill server tests."""

import pytest
import pytest_asyncio

from service.history.store import HistoryStore


@pytest.fixture
def tmp_db(tmp_path):
    """Return a path for a temporary SQLite database."""
    return str(tmp_path / "test.db")


@pytest_asyncio.fixture
async def store(tmp_db):
    """Create and connect a HistoryStore backed by a temporary database."""
    s = HistoryStore(tmp_db, reconnect_grace=60)
    await s.connect()
    yield s
    await s.close()


@pytest.fixture
def sample_address():
    """Return a consistent test device address."""
    return "70:91:8F:00:00:01"
