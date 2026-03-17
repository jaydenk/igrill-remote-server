"""Database schema definitions and initialisation."""

import sqlite3

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


def init_db(conn: sqlite3.Connection) -> None:
    """Create all tables if they don't exist and record schema version."""
    conn.executescript(_SCHEMA_SQL)

    existing = conn.execute(
        "SELECT version FROM schema_version WHERE version = ?",
        (SCHEMA_VERSION,),
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO schema_version (version) VALUES (?)",
            (SCHEMA_VERSION,),
        )
        conn.commit()
