"""Database schema definitions and initialisation."""

import logging
import sqlite3

import aiosqlite

LOG = logging.getLogger("igrill.db")

SCHEMA_VERSION = 1

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS devices (
    address     TEXT PRIMARY KEY,
    name        TEXT,
    model       TEXT,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id           TEXT PRIMARY KEY,
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    start_reason TEXT NOT NULL,
    end_reason   TEXT
);

CREATE TABLE IF NOT EXISTS session_devices (
    session_id  TEXT NOT NULL REFERENCES sessions(id),
    address     TEXT NOT NULL REFERENCES devices(address),
    joined_at   TEXT NOT NULL,
    left_at     TEXT,
    PRIMARY KEY (session_id, address)
);

CREATE TABLE IF NOT EXISTS probe_readings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(id),
    address     TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    probe_index INTEGER NOT NULL,
    temperature REAL,
    UNIQUE(session_id, address, seq, probe_index)
);

CREATE TABLE IF NOT EXISTS device_readings (
    session_id  TEXT NOT NULL REFERENCES sessions(id),
    address     TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    battery     INTEGER,
    propane     REAL,
    heating_json TEXT,
    PRIMARY KEY (session_id, address, seq)
);

CREATE TABLE IF NOT EXISTS session_targets (
    session_id           TEXT NOT NULL REFERENCES sessions(id),
    address              TEXT NOT NULL,
    probe_index          INTEGER NOT NULL,
    mode                 TEXT NOT NULL DEFAULT 'fixed',
    target_value         REAL,
    range_low            REAL,
    range_high           REAL,
    pre_alert_offset     REAL DEFAULT 5.0,
    reminder_interval_secs INTEGER DEFAULT 0,
    PRIMARY KEY (session_id, address, probe_index)
);

CREATE INDEX IF NOT EXISTS idx_probe_readings_session ON probe_readings(session_id);
CREATE INDEX IF NOT EXISTS idx_probe_readings_lookup ON probe_readings(session_id, address, recorded_at);
CREATE INDEX IF NOT EXISTS idx_device_readings_session ON device_readings(session_id);
CREATE INDEX IF NOT EXISTS idx_session_devices_session ON session_devices(session_id);
CREATE INDEX IF NOT EXISTS idx_session_targets_session ON session_targets(session_id);
"""


async def _drop_legacy_schema(conn: aiosqlite.Connection) -> None:
    """Detect and remove the pre-normalisation schema.

    The original HistoryStore created ``sessions`` with
    ``id INTEGER PRIMARY KEY AUTOINCREMENT``.  The current schema uses
    ``id TEXT PRIMARY KEY`` (UUID hex strings).  Because
    ``CREATE TABLE IF NOT EXISTS`` silently keeps the existing table,
    the old INTEGER column persists and causes ``datatype mismatch``
    errors when inserting text IDs.

    This function checks for the legacy column type and drops all
    incompatible tables so they can be recreated cleanly.
    """
    try:
        cursor = await conn.execute("PRAGMA table_info(sessions)")
        columns = await cursor.fetchall()
    except Exception:
        return  # Table doesn't exist yet — nothing to do

    for col in columns:
        # PRAGMA table_info columns: cid, name, type, notnull, dflt_value, pk
        if col[1] == "id" and col[2].upper() == "INTEGER":
            LOG.warning(
                "Detected legacy schema (sessions.id INTEGER). "
                "Dropping incompatible tables to upgrade."
            )
            await conn.executescript(
                """
                DROP TABLE IF EXISTS readings;
                DROP TABLE IF EXISTS session_targets;
                DROP TABLE IF EXISTS session_devices;
                DROP TABLE IF EXISTS device_readings;
                DROP TABLE IF EXISTS probe_readings;
                DROP TABLE IF EXISTS sessions;
                DROP TABLE IF EXISTS devices;
                DROP TABLE IF EXISTS schema_version;
                """
            )
            return


async def init_db(conn: aiosqlite.Connection) -> None:
    """Create all tables if they don't exist and record schema version."""
    await _drop_legacy_schema(conn)
    await conn.executescript(_SCHEMA_SQL)

    cursor = await conn.execute(
        "SELECT version FROM schema_version WHERE version = ?",
        (SCHEMA_VERSION,),
    )
    existing = await cursor.fetchone()
    if existing is None:
        await conn.execute(
            "INSERT INTO schema_version (version) VALUES (?)",
            (SCHEMA_VERSION,),
        )
        await conn.commit()
