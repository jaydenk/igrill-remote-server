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
    # Example for future use:
    # 2: [
    #     "ALTER TABLE probe_readings ADD COLUMN unit TEXT DEFAULT 'C'",
    # ],
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

    Each migration is a list of SQL statements executed sequentially.
    The version is recorded in ``schema_version`` after all statements
    for that version succeed.
    """
    current = await get_current_version(conn)

    for version in sorted(MIGRATIONS):
        if version <= current:
            continue
        LOG.info("Applying schema migration v%d", version)
        for statement in MIGRATIONS[version]:
            await conn.execute(statement)
        await conn.execute(
            "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
            (version,),
        )
        await conn.commit()
        LOG.info("Schema migration v%d applied successfully", version)
