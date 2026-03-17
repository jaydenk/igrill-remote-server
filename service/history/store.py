"""History store — persists sessions and readings in SQLite.

Rewritten to use the normalised schema from ``service.db.schema``.
Session IDs are UUID hex strings, sessions are user-initiated only
(no auto-session on startup), and probe readings are stored as
individual rows rather than JSON blobs.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from service.db.schema import init_db
from service.models.session import TargetConfig


# ---------------------------------------------------------------------------
# Module-level utility helpers (kept for backward compatibility — other
# modules import these directly).
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
    """SQLite-backed store for grill session history and readings.

    Uses the normalised schema defined in ``service.db.schema``.  Session
    IDs are 32-character UUID hex strings.  No session is created on
    construction — call :meth:`start_session` explicitly.
    """

    def __init__(self, db_path: str, reconnect_grace: int) -> None:
        self._db_path = db_path
        self._reconnect_grace = reconnect_grace
        self._lock = asyncio.Lock()
        self._current_session_id: Optional[str] = None
        self._current_session_start_ts: Optional[str] = None
        self._last_session_id: Optional[str] = None
        self._last_disconnect_ts: Optional[datetime] = None
        self._last_disconnect_sensor: Optional[str] = None

        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        init_db(self._conn)

    # ------------------------------------------------------------------
    # Session lifecycle (user-initiated only)
    # ------------------------------------------------------------------

    async def start_session(
        self, addresses: list[str], reason: str
    ) -> dict:
        """Create a new session.

        If a session is already active, ends it first.  Creates
        ``session_devices`` entries for each address and ensures each
        address is present in the ``devices`` table.

        Returns a dict with ``session_id``, ``session_start_ts``,
        ``start_event``, and ``end_event`` (the latter from ending the
        previous session, or ``None``).
        """
        async with self._lock:
            end_event = None
            if self._current_session_id is not None:
                end_event = self._end_session_locked(reason)

            now_ts = now_iso_utc()
            session_id = uuid.uuid4().hex

            self._conn.execute(
                "INSERT INTO sessions (id, started_at, start_reason) VALUES (?, ?, ?)",
                (session_id, now_ts, reason),
            )

            for addr in addresses:
                self._register_device_locked(addr, name=None, model=None)
                self._conn.execute(
                    "INSERT OR IGNORE INTO session_devices (session_id, address, joined_at) "
                    "VALUES (?, ?, ?)",
                    (session_id, addr, now_ts),
                )

            self._conn.commit()

            self._current_session_id = session_id
            self._current_session_start_ts = now_ts

            start_event = {
                "sessionId": session_id,
                "sessionStartTs": now_ts,
                "reason": reason,
            }

            return {
                "session_id": session_id,
                "session_start_ts": now_ts,
                "start_event": start_event,
                "end_event": end_event,
            }

    async def end_session(self, reason: str) -> Optional[dict]:
        """End the current session without starting a new one.

        Returns a dict with session details if a session was active,
        else ``None``.
        """
        async with self._lock:
            return self._end_session_locked(reason)

    def _end_session_locked(self, reason: str) -> Optional[dict]:
        """End the current session (must be called while holding ``_lock``)."""
        if self._current_session_id is None:
            return None

        now_ts = now_iso_utc()
        session_id = self._current_session_id

        duration_seconds = None
        if self._current_session_start_ts:
            start_dt = parse_iso(self._current_session_start_ts)
            end_dt = parse_iso(now_ts)
            if start_dt and end_dt:
                duration_seconds = int((end_dt - start_dt).total_seconds())

        self._conn.execute(
            "UPDATE sessions SET ended_at = ?, end_reason = ? WHERE id = ?",
            (now_ts, reason, session_id),
        )
        self._conn.commit()

        self._last_session_id = session_id
        self._current_session_id = None
        self._current_session_start_ts = None

        result: dict = {
            "sessionId": session_id,
            "sessionEndTs": now_ts,
            "reason": reason,
        }
        if duration_seconds is not None:
            result["durationSeconds"] = duration_seconds
        return result

    async def get_session_state(self) -> dict:
        """Return the current session state."""
        async with self._lock:
            return {
                "current_session_id": self._current_session_id,
                "current_session_start_ts": self._current_session_start_ts,
                "last_session_id": self._last_session_id,
                "session_timeout_seconds": self._reconnect_grace,
            }

    async def is_session_active(self) -> bool:
        """Quick check whether a session is currently active."""
        async with self._lock:
            return self._current_session_id is not None

    # ------------------------------------------------------------------
    # Multi-device management within sessions
    # ------------------------------------------------------------------

    async def device_left_session(self, session_id: str, address: str) -> None:
        """Mark a device as having left the session."""
        async with self._lock:
            now_ts = now_iso_utc()
            self._conn.execute(
                "UPDATE session_devices SET left_at = ? "
                "WHERE session_id = ? AND address = ?",
                (now_ts, session_id, address),
            )
            self._conn.commit()

    async def device_rejoined_session(self, session_id: str, address: str) -> None:
        """Clear the ``left_at`` timestamp so the device is active again."""
        async with self._lock:
            self._conn.execute(
                "UPDATE session_devices SET left_at = NULL "
                "WHERE session_id = ? AND address = ?",
                (session_id, address),
            )
            self._conn.commit()

    async def add_device_to_session(self, session_id: str, address: str) -> None:
        """Add a new device to an active session."""
        async with self._lock:
            now_ts = now_iso_utc()
            self._register_device_locked(address, name=None, model=None)
            self._conn.execute(
                "INSERT OR IGNORE INTO session_devices (session_id, address, joined_at) "
                "VALUES (?, ?, ?)",
                (session_id, address, now_ts),
            )
            self._conn.commit()

    async def get_session_devices(self, session_id: str) -> list[dict]:
        """Return devices in a session."""
        async with self._lock:
            rows = self._conn.execute(
                "SELECT sd.session_id, sd.address, sd.joined_at, sd.left_at, "
                "d.name, d.model "
                "FROM session_devices sd "
                "LEFT JOIN devices d ON sd.address = d.address "
                "WHERE sd.session_id = ?",
                (session_id,),
            ).fetchall()
            return [
                {
                    "session_id": row["session_id"],
                    "address": row["address"],
                    "joined_at": row["joined_at"],
                    "left_at": row["left_at"],
                    "name": row["name"],
                    "model": row["model"],
                }
                for row in rows
            ]

    async def all_devices_left(self, session_id: str) -> bool:
        """Return True if all devices in the session have ``left_at`` set."""
        async with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) as total, "
                "COUNT(left_at) as left_count "
                "FROM session_devices WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row["total"] == 0:
                return True
            return row["left_count"] == row["total"]

    # ------------------------------------------------------------------
    # Device registry
    # ------------------------------------------------------------------

    async def register_device(
        self, address: str, name: Optional[str], model: Optional[str]
    ) -> None:
        """Upsert a device into the ``devices`` table."""
        async with self._lock:
            self._register_device_locked(address, name, model)
            self._conn.commit()

    def _register_device_locked(
        self, address: str, name: Optional[str], model: Optional[str]
    ) -> None:
        """Insert or update device (must hold ``_lock``)."""
        now_ts = now_iso_utc()
        existing = self._conn.execute(
            "SELECT address FROM devices WHERE address = ?", (address,)
        ).fetchone()
        if existing is None:
            self._conn.execute(
                "INSERT INTO devices (address, name, model, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, ?)",
                (address, name, model, now_ts, now_ts),
            )
        else:
            parts = ["last_seen = ?"]
            params: list = [now_ts]
            if name is not None:
                parts.append("name = ?")
                params.append(name)
            if model is not None:
                parts.append("model = ?")
                params.append(model)
            params.append(address)
            self._conn.execute(
                f"UPDATE devices SET {', '.join(parts)} WHERE address = ?",
                params,
            )

    async def list_devices(self) -> list[dict]:
        """Return all known devices."""
        async with self._lock:
            rows = self._conn.execute(
                "SELECT address, name, model, first_seen, last_seen FROM devices"
            ).fetchall()
            return [
                {
                    "address": row["address"],
                    "name": row["name"],
                    "model": row["model"],
                    "first_seen": row["first_seen"],
                    "last_seen": row["last_seen"],
                }
                for row in rows
            ]

    # ------------------------------------------------------------------
    # Reading storage (normalised)
    # ------------------------------------------------------------------

    async def record_reading(
        self,
        session_id: str,
        address: str,
        seq: int,
        probes: list[dict],
        battery: Optional[int],
        propane: Optional[float],
        heating: Optional[dict],
        recorded_at: Optional[str] = None,
    ) -> None:
        """Record a reading cycle from a device.

        Inserts one row per probe into ``probe_readings`` and one summary
        row into ``device_readings``.
        """
        async with self._lock:
            ts = recorded_at if recorded_at is not None else now_iso_utc()
            heating_json = json.dumps(heating) if heating is not None else None

            self._conn.execute(
                "INSERT OR REPLACE INTO device_readings "
                "(session_id, address, recorded_at, seq, battery, propane, heating_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, address, ts, seq, battery, propane, heating_json),
            )

            for probe in probes:
                self._conn.execute(
                    "INSERT OR REPLACE INTO probe_readings "
                    "(session_id, address, recorded_at, seq, probe_index, temperature) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        session_id,
                        address,
                        ts,
                        seq,
                        probe["index"],
                        probe.get("temperature"),
                    ),
                )

            self._conn.commit()

    async def get_session_readings(self, session_id: str) -> list[dict]:
        """Return all probe readings for a session, ordered by recorded_at."""
        async with self._lock:
            rows = self._conn.execute(
                "SELECT pr.session_id, pr.address, pr.recorded_at, pr.seq, "
                "pr.probe_index, pr.temperature, "
                "dr.battery, dr.propane, dr.heating_json "
                "FROM probe_readings pr "
                "LEFT JOIN device_readings dr "
                "ON pr.session_id = dr.session_id "
                "AND pr.address = dr.address "
                "AND pr.seq = dr.seq "
                "WHERE pr.session_id = ? "
                "ORDER BY pr.recorded_at ASC, pr.probe_index ASC",
                (session_id,),
            ).fetchall()
            return [
                {
                    "session_id": row["session_id"],
                    "address": row["address"],
                    "recorded_at": row["recorded_at"],
                    "seq": row["seq"],
                    "probe_index": row["probe_index"],
                    "temperature": row["temperature"],
                    "battery": row["battery"],
                    "propane": row["propane"],
                    "heating": json.loads(row["heating_json"])
                    if row["heating_json"]
                    else None,
                }
                for row in rows
            ]

    async def get_history_items(
        self,
        since_ts: Optional[str] = None,
        until_ts: Optional[str] = None,
        limit: Optional[int] = None,
        session_id: Optional[str] = None,
    ) -> list[dict]:
        """Query readings with optional filters.

        Returns probe-level rows joined with device-level data.
        """
        async with self._lock:
            query = (
                "SELECT pr.session_id, pr.address, pr.recorded_at, pr.seq, "
                "pr.probe_index, pr.temperature, "
                "dr.battery, dr.propane, dr.heating_json "
                "FROM probe_readings pr "
                "LEFT JOIN device_readings dr "
                "ON pr.session_id = dr.session_id "
                "AND pr.address = dr.address "
                "AND pr.seq = dr.seq "
                "WHERE 1=1"
            )
            params: list = []

            if session_id is not None:
                query += " AND pr.session_id = ?"
                params.append(session_id)
            else:
                if since_ts:
                    query += " AND pr.recorded_at >= ?"
                    params.append(since_ts)
                if until_ts:
                    query += " AND pr.recorded_at <= ?"
                    params.append(until_ts)

            query += " ORDER BY pr.recorded_at ASC, pr.probe_index ASC"

            if limit:
                query += " LIMIT ?"
                params.append(limit)

            rows = self._conn.execute(query, params).fetchall()
            return [
                {
                    "session_id": row["session_id"],
                    "address": row["address"],
                    "recorded_at": row["recorded_at"],
                    "seq": row["seq"],
                    "probe_index": row["probe_index"],
                    "temperature": row["temperature"],
                    "battery": row["battery"],
                    "propane": row["propane"],
                    "heating": json.loads(row["heating_json"])
                    if row["heating_json"]
                    else None,
                }
                for row in rows
            ]

    # ------------------------------------------------------------------
    # Session listing
    # ------------------------------------------------------------------

    async def list_sessions(self, limit: int, offset: int = 0) -> list[dict]:
        """Return recent sessions with reading counts and device info."""
        async with self._lock:
            rows = self._conn.execute(
                "SELECT s.id, s.started_at, s.ended_at, s.start_reason, s.end_reason, "
                "COUNT(DISTINCT pr.id) as reading_count "
                "FROM sessions s "
                "LEFT JOIN probe_readings pr ON s.id = pr.session_id "
                "GROUP BY s.id "
                "ORDER BY s.started_at DESC "
                "LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()

            results = []
            for row in rows:
                session_id = row["id"]
                devices = self._conn.execute(
                    "SELECT sd.address, d.name, d.model "
                    "FROM session_devices sd "
                    "LEFT JOIN devices d ON sd.address = d.address "
                    "WHERE sd.session_id = ?",
                    (session_id,),
                ).fetchall()
                results.append(
                    {
                        "sessionId": session_id,
                        "startTs": row["started_at"],
                        "endTs": row["ended_at"],
                        "startReason": row["start_reason"],
                        "endReason": row["end_reason"],
                        "readingCount": row["reading_count"],
                        "devices": [
                            {
                                "address": d["address"],
                                "name": d["name"],
                                "model": d["model"],
                            }
                            for d in devices
                        ],
                    }
                )
            return results

    # ------------------------------------------------------------------
    # Targets
    # ------------------------------------------------------------------

    async def save_targets(
        self, session_id: str, address: str, targets: list[TargetConfig]
    ) -> None:
        """Persist target configs for a session and device."""
        async with self._lock:
            for t in targets:
                self._conn.execute(
                    "INSERT OR REPLACE INTO session_targets "
                    "(session_id, address, probe_index, mode, target_value, "
                    "range_low, range_high, pre_alert_offset, reminder_interval_secs) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        session_id,
                        address,
                        t.probe_index,
                        t.mode,
                        t.target_value,
                        t.range_low,
                        t.range_high,
                        t.pre_alert_offset,
                        t.reminder_interval_secs,
                    ),
                )
            self._conn.commit()

    async def get_targets(self, session_id: str) -> list[TargetConfig]:
        """Retrieve all target configs for a session."""
        async with self._lock:
            rows = self._conn.execute(
                "SELECT probe_index, mode, target_value, range_low, range_high, "
                "pre_alert_offset, reminder_interval_secs "
                "FROM session_targets WHERE session_id = ?",
                (session_id,),
            ).fetchall()
        return [
            TargetConfig(
                probe_index=r["probe_index"],
                mode=r["mode"],
                target_value=r["target_value"],
                range_low=r["range_low"],
                range_high=r["range_high"],
                pre_alert_offset=r["pre_alert_offset"] if r["pre_alert_offset"] is not None else 5.0,
                reminder_interval_secs=r["reminder_interval_secs"] if r["reminder_interval_secs"] is not None else 0,
            )
            for r in rows
        ]

    async def update_targets(
        self, session_id: str, address: str, targets: list[TargetConfig]
    ) -> None:
        """Replace all targets for a session and device."""
        async with self._lock:
            self._conn.execute(
                "DELETE FROM session_targets WHERE session_id = ? AND address = ?",
                (session_id, address),
            )
            for t in targets:
                self._conn.execute(
                    "INSERT INTO session_targets "
                    "(session_id, address, probe_index, mode, target_value, "
                    "range_low, range_high, pre_alert_offset, reminder_interval_secs) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        session_id,
                        address,
                        t.probe_index,
                        t.mode,
                        t.target_value,
                        t.range_low,
                        t.range_high,
                        t.pre_alert_offset,
                        t.reminder_interval_secs,
                    ),
                )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Disconnect tracking
    # ------------------------------------------------------------------

    async def note_disconnect(self, sensor_id: str, ts: str) -> None:
        """Record a disconnect timestamp for grace period tracking."""
        async with self._lock:
            self._last_disconnect_ts = parse_iso(ts)
            self._last_disconnect_sensor = sensor_id
