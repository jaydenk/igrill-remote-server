"""Shared fixtures for iGrill server tests."""

import pytest


@pytest.fixture
def tmp_db(tmp_path):
    """Return a path for a temporary SQLite database."""
    return str(tmp_path / "test.db")
