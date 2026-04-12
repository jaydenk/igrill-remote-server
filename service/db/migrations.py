"""Schema migration runner.

Applies sequential DDL migrations to bring the database up to the latest
schema version.  Each migration is a list of SQL statements keyed by the
target version number.  Migrations run inside a transaction and record their
version in ``schema_version`` on success.
"""

import logging

import aiosqlite

LOG = logging.getLogger("igrill.session")

# Map of target version -> list of SQL statements to apply.
# Add new migrations here as the schema evolves.
MIGRATIONS: dict[int, list[str]] = {
    2: [
        "ALTER TABLE sessions ADD COLUMN name TEXT",
        "ALTER TABLE sessions ADD COLUMN notes TEXT",
        "ALTER TABLE session_targets ADD COLUMN label TEXT",
    ],
    3: [
        """
        CREATE TABLE IF NOT EXISTS push_tokens (
            token       TEXT PRIMARY KEY,
            live_activity_token TEXT,
            created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """,
    ],
    4: [
        """
        CREATE TABLE IF NOT EXISTS session_timers (
            session_id       TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            address          TEXT NOT NULL,
            probe_index      INTEGER NOT NULL,
            mode             TEXT NOT NULL,
            duration_secs    INTEGER,
            started_at       TEXT,
            paused_at        TEXT,
            accumulated_secs INTEGER NOT NULL DEFAULT 0,
            completed_at     TEXT,
            PRIMARY KEY (session_id, address, probe_index)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_session_timers_session ON session_timers(session_id)",
    ],
    5: [
        """
        CREATE TABLE IF NOT EXISTS session_notes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            body        TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_session_notes_session ON session_notes(session_id, created_at)",
        # Backfill: promote any existing non-empty sessions.notes values into
        # their own session_notes row. The sessions.notes column is intentionally
        # preserved for one release cycle and will be dropped in a later migration.
        """
        INSERT INTO session_notes (session_id, created_at, updated_at, body)
        SELECT id, started_at, started_at, notes
        FROM sessions
        WHERE notes IS NOT NULL AND notes != ''
        """,
    ],
    6: [
        "ALTER TABLE sessions ADD COLUMN target_duration_secs INTEGER",
    ],
}


async def get_current_version(conn: aiosqlite.Connection) -> int:
    """Return the current schema version, or 0 if not initialised."""
    try:
        cursor = await conn.execute(
            "SELECT MAX(version) FROM schema_version"
        )
        row = await cursor.fetchone()
        return row[0] or 0
    except Exception:
        return 0


async def run_migrations(conn: aiosqlite.Connection) -> None:
    """Apply any pending migrations in order.

    Each migration is a list of SQL statements executed inside a single
    transaction.  If any statement fails the entire migration is rolled
    back and the error is re-raised.
    """
    current = await get_current_version(conn)

    for version in sorted(MIGRATIONS):
        if version <= current:
            continue
        LOG.info("Applying schema migration v%d", version)
        try:
            await conn.execute("BEGIN")
            for statement in MIGRATIONS[version]:
                await conn.execute(statement)
            await conn.execute(
                "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
                (version,),
            )
            await conn.commit()
        except Exception:
            await conn.execute("ROLLBACK")
            LOG.error("Schema migration v%d FAILED — rolled back", version)
            raise
        LOG.info("Schema migration v%d applied successfully", version)
