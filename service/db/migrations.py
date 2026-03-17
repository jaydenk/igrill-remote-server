"""Schema migration runner. Currently a stub for future use."""

import sqlite3


def get_current_version(conn: sqlite3.Connection) -> int:
    """Return the current schema version, or 0 if not initialised."""
    try:
        row = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()
        return row[0] or 0
    except sqlite3.OperationalError:
        return 0
