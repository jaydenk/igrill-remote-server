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
