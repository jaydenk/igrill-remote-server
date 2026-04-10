"""Tests for the database schema."""

import sqlite3

import aiosqlite
import pytest

from service.db.schema import init_db, SCHEMA_VERSION


@pytest.mark.asyncio
async def test_init_db_creates_tables(tmp_db):
    async with aiosqlite.connect(tmp_db) as conn:
        await init_db(conn)
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        rows = await cursor.fetchall()
        tables = sorted(row[0] for row in rows)
        assert "devices" in tables
        assert "sessions" in tables
        assert "session_devices" in tables
        assert "probe_readings" in tables
        assert "device_readings" in tables
        assert "session_targets" in tables
        assert "schema_version" in tables


@pytest.mark.asyncio
async def test_schema_version_recorded(tmp_db):
    async with aiosqlite.connect(tmp_db) as conn:
        await init_db(conn)
        cursor = await conn.execute(
            "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        assert row[0] == SCHEMA_VERSION


@pytest.mark.asyncio
async def test_init_db_idempotent(tmp_db):
    async with aiosqlite.connect(tmp_db) as conn:
        await init_db(conn)
        await init_db(conn)  # should not raise
        cursor = await conn.execute("SELECT COUNT(*) FROM schema_version")
        row = await cursor.fetchone()
        assert row[0] == 1


@pytest.mark.asyncio
async def test_probe_readings_unique_constraint(tmp_db):
    """Verify the UNIQUE constraint on probe_readings."""
    async with aiosqlite.connect(tmp_db) as conn:
        await init_db(conn)
        await conn.execute(
            "INSERT INTO sessions (id, started_at, start_reason) "
            "VALUES ('s1', '2026-01-01T00:00:00Z', 'user')"
        )
        await conn.execute(
            "INSERT INTO probe_readings "
            "(session_id, address, recorded_at, seq, probe_index, temperature) "
            "VALUES ('s1', 'AA:BB:CC', '2026-01-01T00:00:00Z', 1, 0, 72.5)"
        )
        await conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            await conn.execute(
                "INSERT INTO probe_readings "
                "(session_id, address, recorded_at, seq, probe_index, temperature) "
                "VALUES ('s1', 'AA:BB:CC', '2026-01-01T00:00:00Z', 1, 0, 73.0)"
            )


@pytest.mark.asyncio
async def test_session_devices_composite_key(tmp_db):
    """Verify session_devices has composite primary key."""
    async with aiosqlite.connect(tmp_db) as conn:
        await init_db(conn)
        await conn.execute(
            "INSERT INTO sessions (id, started_at, start_reason) "
            "VALUES ('s1', '2026-01-01T00:00:00Z', 'user')"
        )
        await conn.execute(
            "INSERT INTO devices (address, name, model, first_seen, last_seen) "
            "VALUES ('AA:BB:CC', 'Test', 'V3', '2026-01-01', '2026-01-01')"
        )
        await conn.execute(
            "INSERT INTO session_devices (session_id, address, joined_at) "
            "VALUES ('s1', 'AA:BB:CC', '2026-01-01T00:00:00Z')"
        )
        await conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            await conn.execute(
                "INSERT INTO session_devices (session_id, address, joined_at) "
                "VALUES ('s1', 'AA:BB:CC', '2026-01-01T00:01:00Z')"
            )


@pytest.mark.asyncio
async def test_indexes_created(tmp_db):
    """Verify that expected indexes exist."""
    async with aiosqlite.connect(tmp_db) as conn:
        await init_db(conn)
        cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        rows = await cursor.fetchall()
        indexes = {row[0] for row in rows}
        assert "idx_probe_readings_session" in indexes
        assert "idx_probe_readings_lookup" in indexes
        assert "idx_device_readings_session" in indexes
        assert "idx_session_devices_session" in indexes
        assert "idx_session_targets_session" in indexes


@pytest.mark.asyncio
async def test_legacy_schema_upgrade(tmp_db):
    """Verify that init_db detects and replaces the old INTEGER PK schema."""
    async with aiosqlite.connect(tmp_db) as conn:
        # Create the legacy sessions table (INTEGER PRIMARY KEY)
        await conn.execute(
            """
            CREATE TABLE sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                address TEXT NOT NULL,
                name TEXT,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                start_reason TEXT,
                end_reason TEXT
            )
            """
        )
        await conn.execute(
            "INSERT INTO sessions (address, started_at, start_reason) "
            "VALUES ('AA:BB:CC', '2026-01-01T00:00:00Z', 'user')"
        )
        await conn.commit()

        # init_db should drop the old table and create the new one
        await init_db(conn)

        # Verify sessions.id is now TEXT
        cursor = await conn.execute("PRAGMA table_info(sessions)")
        columns = await cursor.fetchall()
        id_col = [c for c in columns if c[1] == "id"][0]
        assert id_col[2].upper() == "TEXT"

        # Verify we can insert a UUID text ID
        await conn.execute(
            "INSERT INTO sessions (id, started_at, start_reason) "
            "VALUES ('abcdef1234567890abcdef1234567890', '2026-01-01T00:00:00Z', 'user')"
        )
        await conn.commit()


@pytest.mark.asyncio
async def test_session_enrichment_columns(tmp_db):
    """Migration v2 adds name/notes to sessions and label to session_targets."""
    async with aiosqlite.connect(tmp_db) as conn:
        conn.row_factory = aiosqlite.Row
        await init_db(conn)
        from service.db.migrations import run_migrations
        await run_migrations(conn)

        # Verify sessions columns
        cursor = await conn.execute("PRAGMA table_info(sessions)")
        cols = {row[1] for row in await cursor.fetchall()}
        assert "name" in cols, "sessions.name column missing"
        assert "notes" in cols, "sessions.notes column missing"

        # Verify session_targets column
        cursor = await conn.execute("PRAGMA table_info(session_targets)")
        cols = {row[1] for row in await cursor.fetchall()}
        assert "label" in cols, "session_targets.label column missing"
