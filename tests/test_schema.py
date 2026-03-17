"""Tests for the database schema."""

import sqlite3

import pytest

from service.db.schema import init_db, SCHEMA_VERSION


def test_init_db_creates_tables(tmp_db):
    conn = sqlite3.connect(tmp_db)
    init_db(conn)
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = sorted(row[0] for row in cursor.fetchall())
    assert "devices" in tables
    assert "sessions" in tables
    assert "session_devices" in tables
    assert "probe_readings" in tables
    assert "device_readings" in tables
    assert "session_targets" in tables
    assert "schema_version" in tables


def test_schema_version_recorded(tmp_db):
    conn = sqlite3.connect(tmp_db)
    init_db(conn)
    row = conn.execute(
        "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
    ).fetchone()
    assert row[0] == SCHEMA_VERSION


def test_init_db_idempotent(tmp_db):
    conn = sqlite3.connect(tmp_db)
    init_db(conn)
    init_db(conn)  # should not raise
    row = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()
    assert row[0] == 1


def test_probe_readings_unique_constraint(tmp_db):
    """Verify the UNIQUE constraint on probe_readings."""
    conn = sqlite3.connect(tmp_db)
    init_db(conn)
    # Insert a session first
    conn.execute(
        "INSERT INTO sessions (id, started_at, start_reason) "
        "VALUES ('s1', '2026-01-01T00:00:00Z', 'user')"
    )
    # Insert a probe reading
    conn.execute(
        "INSERT INTO probe_readings "
        "(session_id, address, recorded_at, seq, probe_index, temperature) "
        "VALUES ('s1', 'AA:BB:CC', '2026-01-01T00:00:00Z', 1, 0, 72.5)"
    )
    conn.commit()
    # Duplicate should fail
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO probe_readings "
            "(session_id, address, recorded_at, seq, probe_index, temperature) "
            "VALUES ('s1', 'AA:BB:CC', '2026-01-01T00:00:00Z', 1, 0, 73.0)"
        )


def test_session_devices_composite_key(tmp_db):
    """Verify session_devices has composite primary key."""
    conn = sqlite3.connect(tmp_db)
    init_db(conn)
    conn.execute(
        "INSERT INTO sessions (id, started_at, start_reason) "
        "VALUES ('s1', '2026-01-01T00:00:00Z', 'user')"
    )
    conn.execute(
        "INSERT INTO devices (address, name, model, first_seen, last_seen) "
        "VALUES ('AA:BB:CC', 'Test', 'V3', '2026-01-01', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO session_devices (session_id, address, joined_at) "
        "VALUES ('s1', 'AA:BB:CC', '2026-01-01T00:00:00Z')"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO session_devices (session_id, address, joined_at) "
            "VALUES ('s1', 'AA:BB:CC', '2026-01-01T00:01:00Z')"
        )


def test_indexes_created(tmp_db):
    """Verify that expected indexes exist."""
    conn = sqlite3.connect(tmp_db)
    init_db(conn)
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    indexes = {row[0] for row in cursor.fetchall()}
    assert "idx_probe_readings_session" in indexes
    assert "idx_probe_readings_lookup" in indexes
    assert "idx_device_readings_session" in indexes
    assert "idx_session_devices_session" in indexes
    assert "idx_session_targets_session" in indexes
