"""Tests for the database schema."""

import sqlite3

import aiosqlite
import pytest

from service.db.schema import init_db


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
        assert row[0] == 1


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


@pytest.mark.asyncio
async def test_partial_migration_rolls_back(tmp_db):
    """A migration that fails partway through should not leave the DB in a half-applied state."""
    from service.db.migrations import MIGRATIONS, run_migrations

    original = dict(MIGRATIONS)
    try:
        async with aiosqlite.connect(tmp_db) as conn:
            conn.row_factory = aiosqlite.Row
            await init_db(conn)
            await run_migrations(conn)  # apply v2

            # Now inject a broken migration and attempt it
            MIGRATIONS[99] = [
                "ALTER TABLE sessions ADD COLUMN _test_col_1 TEXT",
                "ALTER TABLE nonexistent_table ADD COLUMN oops TEXT",
            ]

            with pytest.raises(Exception):
                await run_migrations(conn)  # attempt v99, should fail + rollback

            cursor = await conn.execute("PRAGMA table_info(sessions)")
            cols = {row[1] for row in await cursor.fetchall()}
            assert "_test_col_1" not in cols

            cursor = await conn.execute("SELECT MAX(version) FROM schema_version")
            row = await cursor.fetchone()
            assert row[0] == 5
    finally:
        MIGRATIONS.clear()
        MIGRATIONS.update(original)


@pytest.mark.asyncio
async def test_migration_v3_creates_push_tokens(store):
    """Migration v3 should create the push_tokens table."""
    async with store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='push_tokens'"
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None, "push_tokens table should exist after migration"

    # Verify columns
    async with store._conn.execute("PRAGMA table_info(push_tokens)") as cursor:
        columns = {r[1] for r in await cursor.fetchall()}
    assert columns == {"token", "live_activity_token", "created_at", "updated_at"}


@pytest.mark.asyncio
async def test_migration_v4_creates_session_timers(store):
    """Migration v4 should create the session_timers table with expected columns."""
    async with store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='session_timers'"
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None, "session_timers table should exist after migration"

    # Verify columns
    async with store._conn.execute("PRAGMA table_info(session_timers)") as cursor:
        rows = await cursor.fetchall()
    columns = {r[1] for r in rows}
    assert columns == {
        "session_id",
        "address",
        "probe_index",
        "mode",
        "duration_secs",
        "started_at",
        "paused_at",
        "accumulated_secs",
        "completed_at",
    }


@pytest.mark.asyncio
async def test_session_timers_session_index_exists(store):
    """Migration v4 should create idx_session_timers_session for convention parity."""
    async with store._conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND name='idx_session_timers_session'"
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None, (
        "idx_session_timers_session index should exist after migration"
    )


@pytest.mark.asyncio
async def test_session_timers_composite_primary_key(store):
    """session_timers should use (session_id, address, probe_index) as composite PK."""
    # pk column in PRAGMA table_info is column 5 (0-indexed) and is non-zero
    # for columns participating in the primary key, ordered by their position.
    async with store._conn.execute("PRAGMA table_info(session_timers)") as cursor:
        rows = await cursor.fetchall()
    pk_cols = sorted(
        ((r[5], r[1]) for r in rows if r[5] > 0),
        key=lambda item: item[0],
    )
    assert [name for _, name in pk_cols] == ["session_id", "address", "probe_index"]

    # Behaviourally: a duplicate (session_id, address, probe_index) must fail.
    await store._conn.execute(
        "INSERT INTO sessions (id, started_at, start_reason) "
        "VALUES ('st-sess', '2026-01-01T00:00:00Z', 'user')"
    )
    await store._conn.execute(
        "INSERT INTO session_timers "
        "(session_id, address, probe_index, mode, accumulated_secs) "
        "VALUES ('st-sess', 'AA:BB:CC', 0, 'count_up', 0)"
    )
    await store._conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        await store._conn.execute(
            "INSERT INTO session_timers "
            "(session_id, address, probe_index, mode, accumulated_secs) "
            "VALUES ('st-sess', 'AA:BB:CC', 0, 'count_down', 0)"
        )


@pytest.mark.asyncio
async def test_session_timers_cascade_delete(store):
    """Deleting a session should cascade-delete its session_timers rows."""
    # Sanity: foreign keys are enabled by HistoryStore.connect().
    async with store._conn.execute("PRAGMA foreign_keys") as cursor:
        row = await cursor.fetchone()
    assert row[0] == 1, "foreign_keys PRAGMA should be ON"

    await store._conn.execute(
        "INSERT INTO sessions (id, started_at, start_reason) "
        "VALUES ('cascade-sess', '2026-01-01T00:00:00Z', 'user')"
    )
    await store._conn.execute(
        "INSERT INTO session_timers "
        "(session_id, address, probe_index, mode, duration_secs, accumulated_secs) "
        "VALUES ('cascade-sess', 'AA:BB:CC', 0, 'count_down', 600, 0)"
    )
    await store._conn.commit()

    async with store._conn.execute(
        "SELECT COUNT(*) FROM session_timers WHERE session_id = 'cascade-sess'"
    ) as cursor:
        assert (await cursor.fetchone())[0] == 1

    await store._conn.execute("DELETE FROM sessions WHERE id = 'cascade-sess'")
    await store._conn.commit()

    async with store._conn.execute(
        "SELECT COUNT(*) FROM session_timers WHERE session_id = 'cascade-sess'"
    ) as cursor:
        assert (await cursor.fetchone())[0] == 0, (
            "session_timers rows should cascade-delete when parent session is deleted"
        )


@pytest.mark.asyncio
async def test_migration_v5_creates_session_notes(store):
    """Migration v5 should create the session_notes table with expected columns."""
    async with store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='session_notes'"
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None, "session_notes table should exist after migration"

    async with store._conn.execute("PRAGMA table_info(session_notes)") as cursor:
        rows = await cursor.fetchall()
    # row = (cid, name, type, notnull, dflt_value, pk)
    columns = {r[1]: r for r in rows}
    assert set(columns.keys()) == {
        "id",
        "session_id",
        "created_at",
        "updated_at",
        "body",
    }

    # id is INTEGER PRIMARY KEY AUTOINCREMENT
    assert columns["id"][2].upper() == "INTEGER"
    assert columns["id"][5] == 1, "id should be the primary key"

    # session_id, created_at, updated_at, body are NOT NULL
    for col_name in ("session_id", "created_at", "updated_at", "body"):
        assert columns[col_name][3] == 1, f"{col_name} should be NOT NULL"


@pytest.mark.asyncio
async def test_session_notes_autoincrement(store):
    """session_notes.id should be AUTOINCREMENT (registers in sqlite_sequence)."""
    # sqlite_sequence only gets populated after an insert; insert a parent session first.
    await store._conn.execute(
        "INSERT INTO sessions (id, started_at, start_reason) "
        "VALUES ('ai-sess', '2026-01-01T00:00:00Z', 'user')"
    )
    await store._conn.execute(
        "INSERT INTO session_notes (session_id, created_at, updated_at, body) "
        "VALUES ('ai-sess', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 'hello')"
    )
    await store._conn.commit()

    async with store._conn.execute(
        "SELECT name FROM sqlite_sequence WHERE name='session_notes'"
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None, (
        "session_notes.id should be AUTOINCREMENT (in sqlite_sequence)"
    )


@pytest.mark.asyncio
async def test_session_notes_index_exists(store):
    """Migration v5 should create idx_session_notes_session on (session_id, created_at)."""
    async with store._conn.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type='index' AND name='idx_session_notes_session'"
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None, "idx_session_notes_session index should exist"
    # Verify it covers the expected columns
    assert "session_id" in row[1]
    assert "created_at" in row[1]


@pytest.mark.asyncio
async def test_session_notes_cascade_delete(store):
    """Deleting a session should cascade-delete its session_notes rows."""
    await store._conn.execute(
        "INSERT INTO sessions (id, started_at, start_reason) "
        "VALUES ('notes-cascade', '2026-01-01T00:00:00Z', 'user')"
    )
    await store._conn.execute(
        "INSERT INTO session_notes (session_id, created_at, updated_at, body) "
        "VALUES ('notes-cascade', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 'x')"
    )
    await store._conn.commit()

    await store._conn.execute("DELETE FROM sessions WHERE id = 'notes-cascade'")
    await store._conn.commit()

    async with store._conn.execute(
        "SELECT COUNT(*) FROM session_notes WHERE session_id = 'notes-cascade'"
    ) as cursor:
        assert (await cursor.fetchone())[0] == 0, (
            "session_notes rows should cascade-delete when parent session is deleted"
        )


@pytest.mark.asyncio
async def test_migration_v5_backfills_notes_from_sessions(tmp_db):
    """Migration v5 should backfill session_notes from non-empty sessions.notes values."""
    from service.db.migrations import run_migrations

    async with aiosqlite.connect(tmp_db) as conn:
        conn.row_factory = aiosqlite.Row
        await init_db(conn)

        # Seed sessions BEFORE v5 runs. We need v2 applied first so sessions.notes exists.
        # Apply only v2, v3, v4 by temporarily suppressing v5.
        from service.db import migrations as migrations_mod

        full = dict(migrations_mod.MIGRATIONS)
        try:
            # Apply v2..v4 only
            migrations_mod.MIGRATIONS.clear()
            migrations_mod.MIGRATIONS.update(
                {v: full[v] for v in full if v <= 4}
            )
            await run_migrations(conn)

            # Seed sessions with varied notes states
            await conn.execute(
                "INSERT INTO sessions (id, started_at, start_reason, notes) "
                "VALUES ('s-with-notes', '2026-02-01T10:00:00Z', 'user', 'meat was good')"
            )
            await conn.execute(
                "INSERT INTO sessions (id, started_at, start_reason, notes) "
                "VALUES ('s-null-notes', '2026-02-02T10:00:00Z', 'user', NULL)"
            )
            await conn.execute(
                "INSERT INTO sessions (id, started_at, start_reason, notes) "
                "VALUES ('s-empty-notes', '2026-02-03T10:00:00Z', 'user', '')"
            )
            await conn.commit()

            # Now apply v5
            migrations_mod.MIGRATIONS.clear()
            migrations_mod.MIGRATIONS.update(full)
            await run_migrations(conn)

            # Only the session with real notes should produce a row
            cursor = await conn.execute(
                "SELECT session_id, created_at, updated_at, body "
                "FROM session_notes ORDER BY session_id"
            )
            rows = await cursor.fetchall()
            assert len(rows) == 1
            row = rows[0]
            assert row["session_id"] == "s-with-notes"
            assert row["body"] == "meat was good"
            assert row["created_at"] == "2026-02-01T10:00:00Z"
            assert row["updated_at"] == "2026-02-01T10:00:00Z"
        finally:
            migrations_mod.MIGRATIONS.clear()
            migrations_mod.MIGRATIONS.update(full)


@pytest.mark.asyncio
async def test_migration_v5_preserves_sessions_notes_column(store):
    """v5 must NOT drop sessions.notes — it is kept for one release cycle."""
    async with store._conn.execute("PRAGMA table_info(sessions)") as cursor:
        cols = {r[1] for r in await cursor.fetchall()}
    assert "notes" in cols, "sessions.notes column should still exist after v5"
