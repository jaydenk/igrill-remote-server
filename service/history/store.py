"""History store — persists sessions and readings in SQLite.

Extracted from the monolithic ``main.py``.  The only additions over the
original implementation are the ``session_targets`` table and the three
target-related async methods (``save_targets``, ``get_targets``,
``update_targets``).
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Module-level utility helpers
# ---------------------------------------------------------------------------


def now_iso() -> str:
    """Return the current local time as an ISO-8601 string."""
    return datetime.now().astimezone().isoformat()


def now_iso_utc() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def parse_iso(timestamp: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp, returning *None* on failure."""
    try:
        return datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# HistoryStore
# ---------------------------------------------------------------------------


class HistoryStore:
    """SQLite-backed store for grill session history and readings."""

    def __init__(self, db_path: str, reconnect_grace: int) -> None:
        self._db_path = db_path
        self._reconnect_grace = reconnect_grace
        self._lock = asyncio.Lock()
        self._current_session_id: Optional[int] = None
        self._current_session_start_ts: Optional[str] = None
        self._last_session_id: Optional[int] = None
        self._last_activity_ts: Optional[datetime] = None
        self._last_disconnect_ts: Optional[datetime] = None
        self._last_disconnect_sensor: Optional[str] = None
        self._started_from_restart = False
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()
        self._load_session_state()

    # -- Schema --------------------------------------------------------------

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                address TEXT NOT NULL,
                name TEXT,
                model TEXT,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                start_reason TEXT,
                end_reason TEXT
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                address TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                seq INTEGER,
                session_start_ts TEXT,
                unit TEXT,
                battery_percent REAL,
                propane_percent REAL,
                pulse_json TEXT,
                probes_json TEXT,
                data_json TEXT,
                FOREIGN KEY(session_id) REFERENCES sessions(id)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS session_targets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                probe_index INTEGER NOT NULL,
                mode TEXT NOT NULL,
                target_value REAL,
                range_low REAL,
                range_high REAL,
                pre_alert_offset REAL DEFAULT 10.0,
                reminder_interval_secs INTEGER DEFAULT 300,
                FOREIGN KEY(session_id) REFERENCES sessions(id)
            )
            """
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_address ON sessions(address)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_readings_session ON readings(session_id)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_targets_session ON session_targets(session_id)")
        self._ensure_column("readings", "seq", "seq INTEGER")
        self._ensure_column("readings", "data_json", "data_json TEXT")
        self._ensure_column("readings", "session_start_ts", "session_start_ts TEXT")
        self._ensure_column("sessions", "start_reason", "start_reason TEXT")
        self._ensure_column("sessions", "end_reason", "end_reason TEXT")
        self._conn.commit()

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {
            row["name"]
            for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")

    # -- Session state -------------------------------------------------------

    def _load_session_state(self) -> None:
        now_ts = now_iso_utc()
        row = self._conn.execute(
            "SELECT * FROM sessions ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        if row:
            self._last_session_id = row["id"]
            if row["ended_at"] is None:
                self._conn.execute(
                    "UPDATE sessions SET ended_at = ?, end_reason = ? WHERE id = ?",
                    (now_ts, "server_restart", row["id"]),
                )
                self._conn.commit()
                self._last_session_id = row["id"]
        has_history = self._conn.execute("SELECT 1 FROM readings LIMIT 1").fetchone()
        self._started_from_restart = has_history is not None
        session_id = self._conn.execute(
            "INSERT INTO sessions (address, started_at, start_reason) VALUES (?, ?, ?)",
            ("global", now_ts, "server_restart"),
        )
        self._conn.commit()
        session_id = self._conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        self._current_session_id = int(session_id)
        self._current_session_start_ts = now_ts

    async def _create_session(self, start_ts: str, reason: str) -> int:
        self._conn.execute(
            "INSERT INTO sessions (address, started_at, start_reason) VALUES (?, ?, ?)",
            ("global", start_ts, reason),
        )
        session_id = self._conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        self._conn.commit()
        self._current_session_id = int(session_id)
        self._current_session_start_ts = start_ts
        self._last_activity_ts = None
        return int(session_id)

    async def _end_session(self, end_ts: str, reason: str) -> Optional[int]:
        if self._current_session_id is None:
            return None
        session_id = self._current_session_id
        self._conn.execute(
            "UPDATE sessions SET ended_at = ?, end_reason = ? WHERE id = ?",
            (end_ts, reason, session_id),
        )
        self._conn.commit()
        self._last_session_id = session_id
        self._current_session_id = None
        self._current_session_start_ts = None
        return session_id

    async def ensure_session_for_reading(
        self,
        now_ts: str,
        sensor_id: Optional[str],
    ) -> Dict[str, object]:
        now_dt = parse_iso(now_ts) or datetime.now(timezone.utc)
        async with self._lock:
            rolled = False
            end_event = None
            start_event = None
            reason_start = "sensor_reconnect"
            if self._current_session_id is None:
                if self._started_from_restart:
                    reason_start = "server_restart"
                session_id = await self._create_session(now_ts, reason_start)
                start_event = {
                    "sensorId": sensor_id,
                    "sessionId": session_id,
                    "sessionStartTs": now_ts,
                    "reason": reason_start,
                }
            elif self._last_activity_ts and (now_dt - self._last_activity_ts).total_seconds() > self._reconnect_grace:
                rolled = True
                duration_seconds = None
                if self._current_session_start_ts:
                    start_dt = parse_iso(self._current_session_start_ts)
                    if start_dt:
                        duration_seconds = int((now_dt - start_dt).total_seconds())
                end_reason = "idle_timeout"
                if self._last_disconnect_ts:
                    if (now_dt - self._last_disconnect_ts).total_seconds() >= self._reconnect_grace:
                        end_reason = "sensor_disconnect"
                end_session_id = await self._end_session(now_ts, end_reason)
                end_event = {
                    "sensorId": sensor_id,
                    "sessionId": end_session_id,
                    "sessionEndTs": now_ts,
                    "reason": end_reason,
                }
                if duration_seconds is not None:
                    end_event["durationSeconds"] = duration_seconds
                session_id = await self._create_session(now_ts, "sensor_reconnect")
                start_event = {
                    "sensorId": sensor_id,
                    "sessionId": session_id,
                    "sessionStartTs": now_ts,
                    "reason": "sensor_reconnect",
                }
            elif self._last_activity_ts is None and self._current_session_start_ts:
                start_dt = parse_iso(self._current_session_start_ts)
                if start_dt and (now_dt - start_dt).total_seconds() > self._reconnect_grace:
                    rolled = True
                    end_reason = "idle_timeout"
                    if self._last_disconnect_ts:
                        if (now_dt - self._last_disconnect_ts).total_seconds() >= self._reconnect_grace:
                            end_reason = "sensor_disconnect"
                    end_event = {
                        "sensorId": sensor_id,
                        "sessionId": self._current_session_id,
                        "sessionEndTs": now_ts,
                        "reason": end_reason,
                        "durationSeconds": int((now_dt - start_dt).total_seconds()),
                    }
                    await self._end_session(now_ts, end_reason)
                    session_id = await self._create_session(now_ts, "sensor_reconnect")
                    start_event = {
                        "sensorId": sensor_id,
                        "sessionId": session_id,
                        "sessionStartTs": now_ts,
                        "reason": "sensor_reconnect",
                    }
                else:
                    session_id = self._current_session_id
            else:
                session_id = self._current_session_id
            self._last_activity_ts = now_dt
            if session_id is None:
                session_id = await self._create_session(now_ts, reason_start)
                start_event = {
                    "sensorId": sensor_id,
                    "sessionId": session_id,
                    "sessionStartTs": now_ts,
                    "reason": reason_start,
                }
            return {
                "session_id": session_id,
                "session_start_ts": self._current_session_start_ts,
                "rolled": rolled,
                "end_event": end_event,
                "start_event": start_event,
            }

    async def force_new_session(self, now_ts: str, sensor_id: Optional[str], reason: str) -> Dict[str, object]:
        async with self._lock:
            end_event = None
            if self._current_session_id is not None:
                duration_seconds = None
                if self._current_session_start_ts:
                    start_dt = parse_iso(self._current_session_start_ts)
                    end_dt = parse_iso(now_ts)
                    if start_dt and end_dt:
                        duration_seconds = int((end_dt - start_dt).total_seconds())
                end_session_id = await self._end_session(now_ts, reason)
                end_event = {
                    "sensorId": sensor_id,
                    "sessionId": end_session_id,
                    "sessionEndTs": now_ts,
                    "reason": reason,
                }
                if duration_seconds is not None:
                    end_event["durationSeconds"] = duration_seconds
            session_id = await self._create_session(now_ts, reason)
            start_event = {
                "sensorId": sensor_id,
                "sessionId": session_id,
                "sessionStartTs": now_ts,
                "reason": reason,
            }
            return {
                "session_id": session_id,
                "session_start_ts": self._current_session_start_ts,
                "end_event": end_event,
                "start_event": start_event,
            }

    async def end_current_session(self, now_ts: str, reason: str) -> Optional[Dict[str, object]]:
        """End the current session without starting a new one.

        Returns a dict with session details if a session was active, else *None*.
        """
        async with self._lock:
            if self._current_session_id is None:
                return None
            session_id = self._current_session_id
            duration_seconds = None
            if self._current_session_start_ts:
                start_dt = parse_iso(self._current_session_start_ts)
                end_dt = parse_iso(now_ts)
                if start_dt and end_dt:
                    duration_seconds = int((end_dt - start_dt).total_seconds())
            await self._end_session(now_ts, reason)
            result: Dict[str, object] = {
                "sessionId": session_id,
                "sessionEndTs": now_ts,
                "reason": reason,
            }
            if duration_seconds is not None:
                result["durationSeconds"] = duration_seconds
            return result

    async def get_session_state(self) -> Dict[str, object]:
        async with self._lock:
            return {
                "current_session_id": self._current_session_id,
                "current_session_start_ts": self._current_session_start_ts,
                "last_session_id": self._last_session_id,
                "session_timeout_seconds": self._reconnect_grace,
            }

    async def note_disconnect(self, sensor_id: Optional[str], ts: str) -> None:
        async with self._lock:
            self._last_disconnect_ts = parse_iso(ts)
            self._last_disconnect_sensor = sensor_id

    # -- Readings ------------------------------------------------------------

    async def record_reading(
        self,
        session_id: int,
        address: str,
        payload: Dict[str, object],
        reading_data: Dict[str, object],
        seq: int,
        session_start_ts: Optional[str],
    ) -> None:
        async with self._lock:
            self._conn.execute(
                """
                INSERT INTO readings (
                    session_id,
                    address,
                    recorded_at,
                    seq,
                    session_start_ts,
                    unit,
                    battery_percent,
                    propane_percent,
                    pulse_json,
                    probes_json,
                    data_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    address,
                    payload.get("last_update"),
                    seq,
                    session_start_ts,
                    payload.get("unit"),
                    payload.get("battery_percent"),
                    payload.get("propane_percent"),
                    json.dumps(payload.get("pulse", {})),
                    json.dumps(payload.get("probes", [])),
                    json.dumps(reading_data),
                ),
            )
            self._conn.commit()

    async def get_history(self, address: Optional[str] = None) -> List[Dict[str, object]]:
        async with self._lock:
            if address:
                session_rows = self._conn.execute(
                    "SELECT * FROM sessions WHERE address = ? ORDER BY started_at ASC",
                    (address,),
                ).fetchall()
            else:
                session_rows = self._conn.execute(
                    "SELECT * FROM sessions ORDER BY started_at ASC"
                ).fetchall()
            sessions = []
            for session in session_rows:
                readings = self._conn.execute(
                    "SELECT * FROM readings WHERE session_id = ? ORDER BY recorded_at ASC",
                    (session["id"],),
                ).fetchall()
                sessions.append(
                    {
                        "session_id": session["id"],
                        "address": session["address"],
                        "name": session["name"],
                        "model": session["model"],
                        "started_at": session["started_at"],
                        "ended_at": session["ended_at"],
                        "readings": [
                            {
                                "recorded_at": reading["recorded_at"],
                                "unit": reading["unit"],
                                "battery_percent": reading["battery_percent"],
                                "propane_percent": reading["propane_percent"],
                                "pulse": json.loads(reading["pulse_json"] or "{}"),
                                "probes": json.loads(reading["probes_json"] or "[]"),
                            }
                            for reading in readings
                        ],
                    }
                )
            return sessions

    async def has_history(self) -> bool:
        async with self._lock:
            row = self._conn.execute("SELECT 1 FROM readings LIMIT 1").fetchone()
            return row is not None

    async def latest_ts(self) -> Optional[str]:
        async with self._lock:
            row = self._conn.execute(
                "SELECT recorded_at FROM readings ORDER BY recorded_at DESC LIMIT 1"
            ).fetchone()
            if row:
                return row["recorded_at"]
            return None

    async def get_history_items(
        self,
        since_ts: Optional[str],
        until_ts: Optional[str],
        limit: Optional[int],
        session_id: Optional[int],
    ) -> List[Dict[str, object]]:
        query = "SELECT * FROM readings WHERE 1=1"
        params: List[object] = []
        if session_id is not None:
            query += " AND session_id = ?"
            params.append(session_id)
        else:
            if since_ts:
                query += " AND recorded_at >= ?"
                params.append(since_ts)
            if until_ts:
                query += " AND recorded_at <= ?"
                params.append(until_ts)
        query += " ORDER BY recorded_at ASC"
        if limit:
            query += " LIMIT ?"
            params.append(limit)
        async with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        items = []
        for row in rows:
            data_json = row["data_json"]
            if data_json:
                data = json.loads(data_json)
            else:
                data = {
                    "sensorId": row["address"],
                    "data": {
                        "unit": row["unit"],
                        "battery_percent": row["battery_percent"],
                        "propane_percent": row["propane_percent"],
                        "pulse": json.loads(row["pulse_json"] or "{}"),
                        "probes": json.loads(row["probes_json"] or "[]"),
                    },
                }
            items.append(
                {
                    "ts": row["recorded_at"],
                    "seq": row["seq"],
                    "sessionId": row["session_id"],
                    "sessionStartTs": row["session_start_ts"],
                    "payload": data,
                    "data": data,
                }
            )
        return items

    async def list_sessions(self, limit: int) -> List[Dict[str, object]]:
        async with self._lock:
            rows = self._conn.execute(
                "SELECT id, started_at, ended_at FROM sessions ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            session_ids = [row["id"] for row in rows]
            counts = {}
            if session_ids:
                placeholders = ",".join("?" for _ in session_ids)
                count_rows = self._conn.execute(
                    f"SELECT session_id, COUNT(*) as count FROM readings WHERE session_id IN ({placeholders}) GROUP BY session_id",
                    session_ids,
                ).fetchall()
                counts = {row["session_id"]: row["count"] for row in count_rows}
        return [
            {
                "sessionId": row["id"],
                "startTs": row["started_at"],
                "endTs": row["ended_at"],
                "count": counts.get(row["id"], 0),
            }
            for row in rows
        ]

    # -- Session targets (new) -----------------------------------------------

    async def save_targets(self, session_id: int, targets: list) -> None:
        """Save target configs for a session. *targets* is a list of TargetConfig."""
        async with self._lock:
            for t in targets:
                self._conn.execute(
                    """INSERT INTO session_targets
                       (session_id, probe_index, mode, target_value, range_low,
                        range_high, pre_alert_offset, reminder_interval_secs)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (session_id, t.probe_index, t.mode, t.target_value,
                     t.range_low, t.range_high, t.pre_alert_offset,
                     t.reminder_interval_secs),
                )
            self._conn.commit()

    async def get_targets(self, session_id: int) -> list:
        """Get target configs for a session. Returns list of TargetConfig."""
        from service.models.session import TargetConfig
        async with self._lock:
            rows = self._conn.execute(
                "SELECT probe_index, mode, target_value, range_low, range_high, "
                "pre_alert_offset, reminder_interval_secs "
                "FROM session_targets WHERE session_id = ?",
                (session_id,),
            ).fetchall()
        return [
            TargetConfig(
                probe_index=r[0], mode=r[1], target_value=r[2],
                range_low=r[3], range_high=r[4],
                pre_alert_offset=r[5] or 10.0,
                reminder_interval_secs=r[6] or 300,
            )
            for r in rows
        ]

    async def update_targets(self, session_id: int, targets: list) -> None:
        """Replace all targets for a session."""
        async with self._lock:
            self._conn.execute(
                "DELETE FROM session_targets WHERE session_id = ?", (session_id,)
            )
            for t in targets:
                self._conn.execute(
                    """INSERT INTO session_targets
                       (session_id, probe_index, mode, target_value, range_low,
                        range_high, pre_alert_offset, reminder_interval_secs)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (session_id, t.probe_index, t.mode, t.target_value,
                     t.range_low, t.range_high, t.pre_alert_offset,
                     t.reminder_interval_secs),
                )
            self._conn.commit()
