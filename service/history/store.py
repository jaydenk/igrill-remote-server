"""History store — persists sessions and readings in SQLite.

Rewritten to use the normalised schema from ``service.db.schema``.
Session IDs are UUID hex strings, sessions are user-initiated only
(no auto-session on startup), and probe readings are stored as
individual rows rather than JSON blobs.

Uses ``aiosqlite`` so that database I/O does not block the event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

import aiosqlite

from service.db.migrations import run_migrations
from service.db.schema import init_db
from service.history.downsampler import downsample_session, downsample_range
from service.models.session import TargetConfig

LOG = logging.getLogger("igrill.session")


# ---------------------------------------------------------------------------
# Module-level utility helpers (kept for backward compatibility — other
# modules import these directly).
# ---------------------------------------------------------------------------


def now_iso_utc() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def parse_iso(timestamp: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp, returning *None* on failure.

    Naive strings (no timezone designator) are coerced to UTC. Every
    timestamp the store writes goes out via ``now_iso_utc`` which is
    timezone-aware, but rows written by old migrations, external tools,
    or tests can still be naive — subtracting an aware from a naive
    ``datetime`` raises ``TypeError`` and would otherwise surface from
    ``end_session`` or the countdown completer loop.
    """
    try:
        dt = datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# Typed exceptions
# ---------------------------------------------------------------------------


class TimerCompletedError(ValueError):
    """Raised when an operation is attempted on a completed (terminal) timer.

    Subclasses ``ValueError`` for backwards compatibility with callers that
    already catch ``ValueError``, but provides a distinct type so the
    WebSocket handler can map it to the ``"timer_completed"`` error code
    without fragile string-matching on the exception message.
    """


# ---------------------------------------------------------------------------
# HistoryStore
# ---------------------------------------------------------------------------


class HistoryStore:
    """SQLite-backed store for grill session history and readings.

    Uses the normalised schema defined in ``service.db.schema``.  Session
    IDs are 32-character UUID hex strings.  No session is created on
    construction — call :meth:`start_session` explicitly.

    Database operations are performed through ``aiosqlite`` so they do not
    block the event loop.  An ``asyncio.Lock`` serialises multi-statement
    transactions to prevent interleaving.
    """

    def __init__(self, db_path: str, reconnect_grace: int) -> None:
        self._db_path = db_path
        self._reconnect_grace = reconnect_grace
        self._lock = asyncio.Lock()
        self._current_session_id: Optional[str] = None
        self._current_session_start_ts: Optional[str] = None
        self._last_session_id: Optional[str] = None
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        """Open the database connection and initialise the schema.

        Must be called (and awaited) before any other method.
        """
        os.makedirs(os.path.dirname(self._db_path) or ".", exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        # 5s busy timeout: defends against the odd SQLITE_BUSY spike when a
        # separate connection (e.g. push_service or a backup process) holds a
        # write lock at the exact moment we try to begin a transaction. WAL
        # handles most concurrent-reader cases already, but writers still
        # serialise, so a short timeout is the simplest way to keep the event
        # loop fluent instead of instantly surfacing an error.
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await init_db(self._conn)
        await run_migrations(self._conn)

    async def close(self) -> None:
        """Close the database connection cleanly."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Orphaned session recovery
    # ------------------------------------------------------------------

    async def recover_orphaned_sessions(self) -> None:
        """Resume any sessions left open by a previous crash or restart.

        Called once at startup, before the BLE scanner begins. Prior
        behaviour was to **end** orphaned sessions on restart, which was
        wrong for a cooking app: a BBQ or smoke-session routinely outlives
        a server reboot (kernel upgrade, container restart, etc.) and the
        user expects readings to simply resume once devices reconnect.

        Session rows with ``ended_at IS NULL`` are left intact. The most
        recent one becomes the in-memory ``_current_session_id`` so BLE
        workers can continue attaching readings to it. On reconnect, any
        iOS/web client that was attached to the previous session will
        hydrate into the same session from the ``status`` snapshot.

        If a user abandoned a session indefinitely, they can still end or
        discard it via the UI. We don't auto-close on age here — the user
        is the source of truth for "did this cook finish?"
        """
        async with self._lock:
            cursor = await self._conn.execute(
                "SELECT id, started_at FROM sessions "
                "WHERE ended_at IS NULL "
                "ORDER BY started_at DESC"
            )
            orphans = await cursor.fetchall()
            await cursor.close()
            if not orphans:
                return

            # Multiple orphans shouldn't happen (we only keep one active
            # at a time), but if they do, the newest wins and the rest
            # are retroactively ended. That's the closest approximation
            # to "pick up where we left off" given ambiguous state.
            primary = orphans[0]
            self._current_session_id = primary["id"]
            self._current_session_start_ts = primary["started_at"]
            LOG.info(
                "Resumed orphaned session %s (started %s)",
                primary["id"],
                primary["started_at"],
            )

            # A previous graceful shutdown called device_left_session on every
            # active device. The session row survived, but the left_at entries
            # would cause is_device_in_session() to return False after
            # restart — silently breaking reading persistence and alert
            # evaluation. Clear them here so the resumed session behaves
            # identically to one that never stopped. (la-followups Task 1)
            await self._conn.execute(
                "UPDATE session_devices SET left_at = NULL "
                "WHERE session_id = ? AND left_at IS NOT NULL",
                (primary["id"],),
            )
            await self._conn.commit()

            if len(orphans) > 1:
                now_ts = now_iso_utc()
                for row in orphans[1:]:
                    await self._conn.execute(
                        "UPDATE sessions SET ended_at = ?, end_reason = ? WHERE id = ?",
                        (now_ts, "server_restart_duplicate", row["id"]),
                    )
                    LOG.warning(
                        "Ended duplicate orphaned session %s (started %s) — "
                        "kept newer session %s",
                        row["id"],
                        row["started_at"],
                        primary["id"],
                    )
                await self._conn.commit()

    # ------------------------------------------------------------------
    # Session lifecycle (user-initiated only)
    # ------------------------------------------------------------------

    async def start_session(
        self,
        addresses: list[str],
        reason: str,
        name: Optional[str] = None,
        target_duration_secs: Optional[int] = None,
    ) -> dict:
        """Create a new session.

        If a session is already active, ends it first.  Creates
        ``session_devices`` entries for each address and ensures each
        address is present in the ``devices`` table.

        ``target_duration_secs`` is an optional user-specified cook
        duration target (in seconds) which is persisted to the
        ``sessions.target_duration_secs`` column.  ``None`` stores NULL.

        Returns a dict with ``session_id``, ``session_start_ts``,
        ``start_event``, and ``end_event`` (the latter from ending the
        previous session, or ``None``).
        """
        async with self._lock:
            end_event = None
            if self._current_session_id is not None:
                end_event = await self._end_session_locked(reason)

            now_ts = now_iso_utc()
            session_id = uuid.uuid4().hex

            await self._conn.execute(
                "INSERT INTO sessions "
                "(id, started_at, start_reason, name, target_duration_secs) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, now_ts, reason, name, target_duration_secs),
            )

            for addr in addresses:
                await self._register_device_locked(addr, name=None, model=None)
                await self._conn.execute(
                    "INSERT OR IGNORE INTO session_devices (session_id, address, joined_at) "
                    "VALUES (?, ?, ?)",
                    (session_id, addr, now_ts),
                )

            await self._conn.commit()

            self._current_session_id = session_id
            self._current_session_start_ts = now_ts

            # Targets are populated by _handle_session_start AFTER
            # start_session returns, so we can't include them here. The
            # caller (the WebSocket handler) reads back the saved targets
            # and injects them into the start_event payload before
            # publishing — see websocket.py::_handle_session_start.
            start_event = {
                "sessionId": session_id,
                "sessionStartTs": now_ts,
                "reason": reason,
                "name": name,
                "devices": addresses,
                "targetDurationSecs": target_duration_secs,
                "targets": [],
            }

            return {
                "session_id": session_id,
                "session_start_ts": now_ts,
                "target_duration_secs": target_duration_secs,
                "start_event": start_event,
                "end_event": end_event,
            }

    async def end_session(self, reason: str) -> Optional[dict]:
        """End the current session without starting a new one.

        Returns a dict with session details if a session was active,
        else ``None``.
        """
        async with self._lock:
            result = await self._end_session_locked(reason)

        # Run downsampling outside the lock — it's safe because there's no
        # concurrent writer for a just-ended session.
        if result is not None:
            session_id = result["sessionId"]
            try:
                await downsample_session(self, session_id)
            except Exception:
                LOG.exception("Downsampling failed for session %s", session_id)

        return result

    async def _end_session_locked(self, reason: str) -> Optional[dict]:
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

        await self._conn.execute(
            "UPDATE sessions SET ended_at = ?, end_reason = ? WHERE id = ?",
            (now_ts, reason, session_id),
        )
        await self._conn.commit()

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

    async def _discard_session_locked(self, session_id: str) -> bool:
        """Delete a session and all its child rows.

        **Caller must already hold** ``self._lock``.  This helper exists so
        that both the public ``discard_session`` and the atomic
        ``discard_current_session`` can share identical deletion logic without
        re-acquiring the lock (which would deadlock).

        Performs the full BEGIN / child-table deletes / sessions delete /
        COMMIT sequence.  Clears the in-memory ``_current_session_id``,
        ``_current_session_start_ts``, and ``_last_session_id`` pointers on
        success if they matched ``session_id``.

        Returns True if the ``sessions`` row was deleted, False if
        ``session_id`` did not exist.  Raises on DB error after issuing a
        ROLLBACK.
        """
        # Capture which in-memory fields would need clearing so the
        # clears only run after a successful commit.  If the commit
        # fails we leave in-memory state untouched to stay consistent
        # with on-disk state.
        was_current = self._current_session_id == session_id
        was_last = self._last_session_id == session_id

        try:
            await self._conn.execute("BEGIN")
            # Explicit deletes for child tables without ON DELETE CASCADE.
            await self._conn.execute(
                "DELETE FROM probe_readings WHERE session_id = ?",
                (session_id,),
            )
            await self._conn.execute(
                "DELETE FROM device_readings WHERE session_id = ?",
                (session_id,),
            )
            await self._conn.execute(
                "DELETE FROM session_targets WHERE session_id = ?",
                (session_id,),
            )
            await self._conn.execute(
                "DELETE FROM session_devices WHERE session_id = ?",
                (session_id,),
            )
            # session_timers and session_notes cascade via FK, but
            # delete explicitly for clarity / defence in depth.
            await self._conn.execute(
                "DELETE FROM session_timers WHERE session_id = ?",
                (session_id,),
            )
            await self._conn.execute(
                "DELETE FROM session_notes WHERE session_id = ?",
                (session_id,),
            )
            cursor = await self._conn.execute(
                "DELETE FROM sessions WHERE id = ?", (session_id,)
            )
            deleted = cursor.rowcount > 0
            await self._conn.commit()

            # Commit succeeded: safe to clear in-memory state.
            if was_current:
                self._current_session_id = None
                self._current_session_start_ts = None
            if was_last:
                self._last_session_id = None
        except Exception:
            await self._conn.execute("ROLLBACK")
            raise

        return deleted

    async def discard_session(self, session_id: str) -> bool:
        """Hard-delete a session and every associated child row.

        If ``session_id`` is the currently-active session, ``_current_session_id``
        (and related transient fields) are cleared first so no further
        readings/targets/timers are recorded against it.

        Child tables without ``ON DELETE CASCADE`` on their FK
        (``session_devices``, ``session_targets``, ``probe_readings``,
        ``device_readings``) are deleted explicitly in the same
        transaction.  ``session_timers`` and ``session_notes`` cascade
        automatically from the ``sessions`` delete, but we still issue
        explicit deletes for symmetry and to keep behaviour robust if the
        cascade constraint is ever dropped.

        Returns True if the ``sessions`` row was deleted, False if
        ``session_id`` did not exist.
        """
        async with self._lock:
            return await self._discard_session_locked(session_id)

    async def discard_current_session(self) -> Optional[str]:
        """Atomically discard whichever session is current at the moment the
        lock is acquired. Returns the discarded session_id, or None if no
        session is active.

        Exists because the handler previously called get_session_state() to
        read _current_session_id, then called discard_session(sid) under a
        fresh lock acquisition — a racing session_start_request could commit a
        new session between the two, causing the discard to target the new
        session's id. This helper reads _current_session_id and deletes under
        one lock hold so no interleaving is possible.
        """
        async with self._lock:
            sid = self._current_session_id
            if sid is None:
                return None
            deleted = await self._discard_session_locked(sid)
            if not deleted:
                # Defensive: _current_session_id pointed at a non-existent row
                # (only possible under a programming bug). Clear the dangling
                # pointer so subsequent calls don't loop.
                self._current_session_id = None
                self._current_session_start_ts = None
                return None
            return sid

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

    async def update_session(
        self,
        session_id: str,
        name: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> Optional[dict]:
        """Update name and/or notes on an existing session.

        Only provided (non-``None``) fields are changed.  Returns the
        updated ``{"name": ..., "notes": ...}`` dict, or ``None`` if the
        session does not exist.
        """
        async with self._lock:
            cursor = await self._conn.execute(
                "SELECT id FROM sessions WHERE id = ?", (session_id,)
            )
            if await cursor.fetchone() is None:
                return None

            updates, params = [], []
            if name is not None:
                updates.append("name = ?")
                params.append(name)
            if notes is not None:
                updates.append("notes = ?")
                params.append(notes)

            if updates:
                params.append(session_id)
                await self._conn.execute(
                    f"UPDATE sessions SET {', '.join(updates)} WHERE id = ?",
                    params,
                )
                await self._conn.commit()

            cursor = await self._conn.execute(
                "SELECT name, notes FROM sessions WHERE id = ?", (session_id,)
            )
            row = await cursor.fetchone()
            return {"name": row["name"], "notes": row["notes"]}

    async def get_session_metadata(self, session_id: str) -> Optional[dict]:
        """Return name, notes, and target_duration_secs for a session,
        or None if not found."""
        async with self._lock:
            cursor = await self._conn.execute(
                "SELECT name, notes, target_duration_secs FROM sessions "
                "WHERE id = ?",
                (session_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return {
                "name": row["name"],
                "notes": row["notes"],
                "target_duration_secs": row["target_duration_secs"],
            }

    # ------------------------------------------------------------------
    # Multi-device management within sessions
    # ------------------------------------------------------------------

    async def device_left_session(self, session_id: str, address: str) -> None:
        """Mark a device as having left the session."""
        async with self._lock:
            now_ts = now_iso_utc()
            await self._conn.execute(
                "UPDATE session_devices SET left_at = ? "
                "WHERE session_id = ? AND address = ?",
                (now_ts, session_id, address),
            )
            await self._conn.commit()

    async def device_rejoined_session(self, session_id: str, address: str) -> None:
        """Clear the ``left_at`` timestamp so the device is active again."""
        async with self._lock:
            await self._conn.execute(
                "UPDATE session_devices SET left_at = NULL "
                "WHERE session_id = ? AND address = ?",
                (session_id, address),
            )
            await self._conn.commit()

    async def add_device_to_session(self, session_id: str, address: str) -> None:
        """Add a new device to an active session."""
        async with self._lock:
            now_ts = now_iso_utc()
            await self._register_device_locked(address, name=None, model=None)
            await self._conn.execute(
                "INSERT OR IGNORE INTO session_devices (session_id, address, joined_at) "
                "VALUES (?, ?, ?)",
                (session_id, address, now_ts),
            )
            await self._conn.commit()

    async def get_session_devices(self, session_id: str) -> list[dict]:
        """Return devices in a session."""
        async with self._lock:
            cursor = await self._conn.execute(
                "SELECT sd.session_id, sd.address, sd.joined_at, sd.left_at, "
                "d.name, d.model "
                "FROM session_devices sd "
                "LEFT JOIN devices d ON sd.address = d.address "
                "WHERE sd.session_id = ?",
                (session_id,),
            )
            rows = await cursor.fetchall()
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
            cursor = await self._conn.execute(
                "SELECT COUNT(*) as total, "
                "COUNT(left_at) as left_count "
                "FROM session_devices WHERE session_id = ?",
                (session_id,),
            )
            row = await cursor.fetchone()
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
            await self._register_device_locked(address, name, model)
            await self._conn.commit()

    async def _register_device_locked(
        self, address: str, name: Optional[str], model: Optional[str]
    ) -> None:
        """Insert or update device (must hold ``_lock``)."""
        now_ts = now_iso_utc()
        cursor = await self._conn.execute(
            "SELECT address FROM devices WHERE address = ?", (address,)
        )
        existing = await cursor.fetchone()
        if existing is None:
            await self._conn.execute(
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
            await self._conn.execute(
                f"UPDATE devices SET {', '.join(parts)} WHERE address = ?",
                params,
            )

    async def list_devices(self) -> list[dict]:
        """Return all known devices."""
        async with self._lock:
            cursor = await self._conn.execute(
                "SELECT address, name, model, first_seen, last_seen FROM devices"
            )
            rows = await cursor.fetchall()
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

            await self._conn.execute(
                "INSERT OR IGNORE INTO device_readings "
                "(session_id, address, recorded_at, seq, battery, propane, heating_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, address, ts, seq, battery, propane, heating_json),
            )

            for probe in probes:
                await self._conn.execute(
                    "INSERT OR IGNORE INTO probe_readings "
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

            await self._conn.commit()

    async def get_session_readings(self, session_id: str) -> list[dict]:
        """Return all probe readings for a session, ordered by recorded_at."""
        async with self._lock:
            cursor = await self._conn.execute(
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
            )
            rows = await cursor.fetchall()
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

            cursor = await self._conn.execute(query, params)
            results: list[dict] = []
            while True:
                batch = await cursor.fetchmany(500)
                if not batch:
                    break
                for row in batch:
                    results.append(
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
                    )
            return results

    # ------------------------------------------------------------------
    # Session listing
    # ------------------------------------------------------------------

    async def list_sessions(self, limit: int, offset: int = 0) -> list[dict]:
        """Return recent sessions with reading counts and device info.

        Uses a single query with GROUP_CONCAT to avoid N+1 queries.
        """
        async with self._lock:
            # Step 1: fetch sessions with reading counts
            cursor = await self._conn.execute(
                "SELECT s.id, s.started_at, s.ended_at, s.start_reason, s.end_reason, "
                "s.name, s.notes, s.target_duration_secs, "
                "(SELECT COUNT(*) FROM probe_readings pr WHERE pr.session_id = s.id) AS reading_count "
                "FROM sessions s "
                "ORDER BY s.started_at DESC "
                "LIMIT ? OFFSET ?",
                (limit, offset),
            )
            session_rows = await cursor.fetchall()

            if not session_rows:
                return []

            # Step 2: batch-fetch devices for all sessions in one query
            session_ids = [row["id"] for row in session_rows]
            placeholders = ",".join("?" for _ in session_ids)
            cursor = await self._conn.execute(
                "SELECT sd.session_id, sd.address, d.name, d.model "
                "FROM session_devices sd "
                "LEFT JOIN devices d ON sd.address = d.address "
                f"WHERE sd.session_id IN ({placeholders})",
                session_ids,
            )
            device_rows = await cursor.fetchall()

            # Group devices by session_id
            devices_by_session: dict[str, list[dict]] = {}
            for dr in device_rows:
                sid = dr["session_id"]
                devices_by_session.setdefault(sid, []).append({
                    "address": dr["address"],
                    "name": dr["name"],
                    "model": dr["model"],
                })

            results = []
            for row in session_rows:
                results.append(
                    {
                        "sessionId": row["id"],
                        "startTs": row["started_at"],
                        "endTs": row["ended_at"],
                        "startReason": row["start_reason"],
                        "endReason": row["end_reason"],
                        "name": row["name"],
                        "notes": row["notes"],
                        "targetDurationSecs": row["target_duration_secs"],
                        "readingCount": row["reading_count"],
                        "devices": devices_by_session.get(row["id"], []),
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
                await self._conn.execute(
                    "INSERT OR REPLACE INTO session_targets "
                    "(session_id, address, probe_index, mode, target_value, "
                    "range_low, range_high, pre_alert_offset, reminder_interval_secs, label, unit) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                        t.label,
                        t.unit,
                    ),
                )
            await self._conn.commit()

    async def get_targets(self, session_id: str) -> list[TargetConfig]:
        """Retrieve all target configs for a session."""
        async with self._lock:
            cursor = await self._conn.execute(
                "SELECT probe_index, mode, target_value, range_low, range_high, "
                "pre_alert_offset, reminder_interval_secs, label, unit "
                "FROM session_targets WHERE session_id = ?",
                (session_id,),
            )
            rows = await cursor.fetchall()
        return [
            TargetConfig(
                probe_index=r["probe_index"],
                mode=r["mode"],
                target_value=r["target_value"],
                range_low=r["range_low"],
                range_high=r["range_high"],
                pre_alert_offset=r["pre_alert_offset"] if r["pre_alert_offset"] is not None else 5.0,
                reminder_interval_secs=r["reminder_interval_secs"] if r["reminder_interval_secs"] is not None else 0,
                label=r["label"],
                unit=r["unit"] if r["unit"] else "C",
            )
            for r in rows
        ]

    async def get_targets_by_device(
        self, session_id: str
    ) -> dict[str, list[TargetConfig]]:
        """Return the session's saved targets grouped by device address.

        Powers the per-device ``allTargets`` field on the ``target_update``
        and ``session_start`` broadcasts so multi-device peers can rebuild
        their local state without losing another device's targets when
        anyone edits one device. (la-followups Task 7)
        """
        async with self._lock:
            cursor = await self._conn.execute(
                "SELECT address, probe_index, mode, target_value, "
                "range_low, range_high, pre_alert_offset, "
                "reminder_interval_secs, label, unit "
                "FROM session_targets WHERE session_id = ?",
                (session_id,),
            )
            rows = await cursor.fetchall()
        grouped: dict[str, list[TargetConfig]] = {}
        for r in rows:
            grouped.setdefault(r["address"], []).append(TargetConfig(
                probe_index=r["probe_index"],
                mode=r["mode"],
                target_value=r["target_value"],
                range_low=r["range_low"],
                range_high=r["range_high"],
                pre_alert_offset=(
                    r["pre_alert_offset"]
                    if r["pre_alert_offset"] is not None else 5.0
                ),
                reminder_interval_secs=(
                    r["reminder_interval_secs"]
                    if r["reminder_interval_secs"] is not None else 0
                ),
                label=r["label"],
                unit=r["unit"] if r["unit"] else "C",
            ))
        return grouped

    async def update_targets(
        self, session_id: str, address: str, targets: list[TargetConfig]
    ) -> None:
        """Replace all targets for a session and device."""
        async with self._lock:
            await self._conn.execute(
                "DELETE FROM session_targets WHERE session_id = ? AND address = ?",
                (session_id, address),
            )
            for t in targets:
                await self._conn.execute(
                    "INSERT INTO session_targets "
                    "(session_id, address, probe_index, mode, target_value, "
                    "range_low, range_high, pre_alert_offset, reminder_interval_secs, label, unit) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                        t.label,
                        t.unit,
                    ),
                )
            await self._conn.commit()

    # ------------------------------------------------------------------
    # Session timers
    # ------------------------------------------------------------------

    @staticmethod
    def _timer_row_to_dict(row) -> dict:
        """Convert a ``session_timers`` row to a plain dict."""
        return {
            "session_id": row["session_id"],
            "address": row["address"],
            "probe_index": row["probe_index"],
            "mode": row["mode"],
            "duration_secs": row["duration_secs"],
            "started_at": row["started_at"],
            "paused_at": row["paused_at"],
            "accumulated_secs": row["accumulated_secs"],
            "completed_at": row["completed_at"],
        }

    async def _fetch_timer_locked(
        self, session_id: str, address: str, probe_index: int
    ) -> Optional[dict]:
        """Fetch a single timer row as a dict (must hold ``_lock``)."""
        cursor = await self._conn.execute(
            "SELECT session_id, address, probe_index, mode, duration_secs, "
            "started_at, paused_at, accumulated_secs, completed_at "
            "FROM session_timers "
            "WHERE session_id = ? AND address = ? AND probe_index = ?",
            (session_id, address, probe_index),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._timer_row_to_dict(row)

    def _require_active_session_locked(self, session_id: str) -> None:
        """Raise if the given session is not the currently-active session."""
        if self._current_session_id != session_id:
            raise ValueError("Timer operations require active session")

    async def upsert_timer(
        self,
        session_id: str,
        address: str,
        probe_index: int,
        mode: str,
        duration_secs: Optional[int] = None,
    ) -> dict:
        """Create or reset a paused-initial timer row.

        If a row already exists for (session_id, address, probe_index), it
        is replaced — mode and duration_secs are updated and all other
        fields reset to their initial (paused, un-started) state.
        """
        if mode not in ("count_up", "count_down"):
            raise ValueError(
                f"mode must be 'count_up' or 'count_down', got {mode!r}"
            )
        async with self._lock:
            self._require_active_session_locked(session_id)

            # Explicit FK validation for a clearer error than a raw IntegrityError.
            cursor = await self._conn.execute(
                "SELECT id FROM sessions WHERE id = ?", (session_id,)
            )
            if await cursor.fetchone() is None:
                raise ValueError(f"Session {session_id} does not exist")

            await self._conn.execute(
                "INSERT INTO session_timers "
                "(session_id, address, probe_index, mode, duration_secs, "
                "started_at, paused_at, accumulated_secs, completed_at) "
                "VALUES (?, ?, ?, ?, ?, NULL, NULL, 0, NULL) "
                "ON CONFLICT(session_id, address, probe_index) DO UPDATE SET "
                "mode = excluded.mode, "
                "duration_secs = excluded.duration_secs, "
                "started_at = NULL, "
                "paused_at = NULL, "
                "accumulated_secs = 0, "
                "completed_at = NULL",
                (session_id, address, probe_index, mode, duration_secs),
            )
            await self._conn.commit()
            row = await self._fetch_timer_locked(session_id, address, probe_index)
            assert row is not None  # just inserted
            return row

    async def start_timer(
        self, session_id: str, address: str, probe_index: int
    ) -> dict:
        """Start (or resume an un-started) timer.

        Idempotent: if the timer is already running, returns the current
        row unchanged without resetting ``started_at``.

        If the timer is currently paused, this acts as a resume (sets
        ``started_at=now``, clears ``paused_at``, preserves
        ``accumulated_secs``).
        """
        async with self._lock:
            self._require_active_session_locked(session_id)

            row = await self._fetch_timer_locked(session_id, address, probe_index)
            if row is None:
                raise ValueError(
                    f"Timer not found for session {session_id} "
                    f"address {address} probe {probe_index}"
                )

            # Terminal state — completed timers must not be revived.
            if row["completed_at"] is not None:
                raise TimerCompletedError(
                    f"Timer is completed and cannot be started: session {session_id} "
                    f"address {address} probe {probe_index}"
                )

            # Already running — idempotent no-op.
            if row["started_at"] is not None and row["paused_at"] is None:
                return row

            now_ts = now_iso_utc()
            await self._conn.execute(
                "UPDATE session_timers "
                "SET started_at = ?, paused_at = NULL "
                "WHERE session_id = ? AND address = ? AND probe_index = ?",
                (now_ts, session_id, address, probe_index),
            )
            await self._conn.commit()
            row = await self._fetch_timer_locked(session_id, address, probe_index)
            assert row is not None
            return row

    async def pause_timer(
        self, session_id: str, address: str, probe_index: int
    ) -> dict:
        """Pause a running timer, accumulating elapsed seconds.

        No-op if the timer is not running (either never started or already
        paused): returns the current row unchanged.
        """
        async with self._lock:
            self._require_active_session_locked(session_id)

            row = await self._fetch_timer_locked(session_id, address, probe_index)
            if row is None:
                raise ValueError(
                    f"Timer not found for session {session_id} "
                    f"address {address} probe {probe_index}"
                )

            if row["started_at"] is None or row["paused_at"] is not None:
                return row

            now_ts = now_iso_utc()
            now_dt = parse_iso(now_ts)
            started_dt = parse_iso(row["started_at"])
            elapsed = 0
            if now_dt is not None and started_dt is not None:
                elapsed = max(0, int((now_dt - started_dt).total_seconds()))
            new_accum = (row["accumulated_secs"] or 0) + elapsed

            await self._conn.execute(
                "UPDATE session_timers "
                "SET paused_at = ?, started_at = NULL, accumulated_secs = ? "
                "WHERE session_id = ? AND address = ? AND probe_index = ?",
                (now_ts, new_accum, session_id, address, probe_index),
            )
            await self._conn.commit()
            row = await self._fetch_timer_locked(session_id, address, probe_index)
            assert row is not None
            return row

    async def resume_timer(
        self, session_id: str, address: str, probe_index: int
    ) -> dict:
        """Resume a paused timer.

        No-op if the timer is not paused: returns the current row
        unchanged.
        """
        async with self._lock:
            self._require_active_session_locked(session_id)

            row = await self._fetch_timer_locked(session_id, address, probe_index)
            if row is None:
                raise ValueError(
                    f"Timer not found for session {session_id} "
                    f"address {address} probe {probe_index}"
                )

            # Terminal state — completed timers must not be revived.
            if row["completed_at"] is not None:
                raise TimerCompletedError(
                    f"Timer is completed and cannot be resumed: session {session_id} "
                    f"address {address} probe {probe_index}"
                )

            if row["paused_at"] is None:
                return row

            now_ts = now_iso_utc()
            await self._conn.execute(
                "UPDATE session_timers "
                "SET started_at = ?, paused_at = NULL "
                "WHERE session_id = ? AND address = ? AND probe_index = ?",
                (now_ts, session_id, address, probe_index),
            )
            await self._conn.commit()
            row = await self._fetch_timer_locked(session_id, address, probe_index)
            assert row is not None
            return row

    async def reset_timer(
        self, session_id: str, address: str, probe_index: int
    ) -> dict:
        """Reset a timer to the initial paused state.

        Preserves ``mode`` and ``duration_secs``; clears ``started_at``,
        ``paused_at``, ``completed_at`` and zeros ``accumulated_secs``.
        """
        async with self._lock:
            self._require_active_session_locked(session_id)

            row = await self._fetch_timer_locked(session_id, address, probe_index)
            if row is None:
                raise ValueError(
                    f"Timer not found for session {session_id} "
                    f"address {address} probe {probe_index}"
                )

            await self._conn.execute(
                "UPDATE session_timers "
                "SET started_at = NULL, paused_at = NULL, "
                "accumulated_secs = 0, completed_at = NULL "
                "WHERE session_id = ? AND address = ? AND probe_index = ?",
                (session_id, address, probe_index),
            )
            await self._conn.commit()
            row = await self._fetch_timer_locked(session_id, address, probe_index)
            assert row is not None
            return row

    async def complete_timer(
        self, session_id: str, address: str, probe_index: int
    ) -> dict:
        """Mark a timer complete.

        Behaviour depends on the timer's current runtime state:

        * Running (``started_at`` set, ``paused_at`` null): elapsed time
          is accumulated, ``paused_at`` is set to now, ``started_at`` is
          cleared, and ``completed_at`` is set (if not already set).
        * Already paused (``paused_at`` set): only ``completed_at`` is
          set (if not already set). ``paused_at``, ``started_at`` and
          ``accumulated_secs`` are left untouched — the timer was not
          running so there is no elapsed to accumulate.
        * Never started: ``completed_at`` is set (if not already set)
          and ``paused_at`` is set to now so the row has a well-defined
          terminal state.
        """
        async with self._lock:
            self._require_active_session_locked(session_id)

            row = await self._fetch_timer_locked(session_id, address, probe_index)
            if row is None:
                raise ValueError(
                    f"Timer not found for session {session_id} "
                    f"address {address} probe {probe_index}"
                )

            now_ts = now_iso_utc()
            completed_at = row["completed_at"] if row["completed_at"] else now_ts

            if row["started_at"] is not None and row["paused_at"] is None:
                # Running — accumulate elapsed and pause.
                now_dt = parse_iso(now_ts)
                started_dt = parse_iso(row["started_at"])
                elapsed = 0
                if now_dt is not None and started_dt is not None:
                    elapsed = max(0, int((now_dt - started_dt).total_seconds()))
                new_accum = (row["accumulated_secs"] or 0) + elapsed

                await self._conn.execute(
                    "UPDATE session_timers "
                    "SET completed_at = ?, paused_at = ?, started_at = NULL, "
                    "accumulated_secs = ? "
                    "WHERE session_id = ? AND address = ? AND probe_index = ?",
                    (
                        completed_at,
                        now_ts,
                        new_accum,
                        session_id,
                        address,
                        probe_index,
                    ),
                )
            elif row["paused_at"] is not None:
                # Already paused — only set completed_at.
                await self._conn.execute(
                    "UPDATE session_timers "
                    "SET completed_at = ? "
                    "WHERE session_id = ? AND address = ? AND probe_index = ?",
                    (completed_at, session_id, address, probe_index),
                )
            else:
                # Never started — set completed_at and give paused_at a
                # well-defined terminal value.
                await self._conn.execute(
                    "UPDATE session_timers "
                    "SET completed_at = ?, paused_at = ? "
                    "WHERE session_id = ? AND address = ? AND probe_index = ?",
                    (completed_at, now_ts, session_id, address, probe_index),
                )

            await self._conn.commit()
            row = await self._fetch_timer_locked(session_id, address, probe_index)
            assert row is not None
            return row

    async def get_timers(self, session_id: str) -> list[dict]:
        """Return all timer rows for a session (possibly empty).

        Works for any session, not just the active one — needed for
        historical reads.
        """
        async with self._lock:
            cursor = await self._conn.execute(
                "SELECT session_id, address, probe_index, mode, duration_secs, "
                "started_at, paused_at, accumulated_secs, completed_at "
                "FROM session_timers "
                "WHERE session_id = ? "
                "ORDER BY address ASC, probe_index ASC",
                (session_id,),
            )
            rows = await cursor.fetchall()
            return [self._timer_row_to_dict(r) for r in rows]

    async def find_expired_running_countdowns(self) -> list[dict]:
        """Return all running count_down timer rows whose effective elapsed
        time has reached or exceeded ``duration_secs``.

        A row qualifies when:

        * ``mode = 'count_down'``
        * ``started_at`` is set, ``paused_at`` is null, ``completed_at`` is null
        * ``duration_secs`` is not null
        * ``accumulated_secs + (now - started_at) >= duration_secs``

        The SQL-side filter narrows to running, un-completed countdowns with
        a duration; the final arithmetic comparison is done in Python for
        clarity (no SQL julianday tricks).
        """
        async with self._lock:
            cursor = await self._conn.execute(
                "SELECT session_id, address, probe_index, mode, duration_secs, "
                "started_at, paused_at, accumulated_secs, completed_at "
                "FROM session_timers "
                "WHERE mode = 'count_down' "
                "AND started_at IS NOT NULL "
                "AND paused_at IS NULL "
                "AND completed_at IS NULL "
                "AND duration_secs IS NOT NULL"
            )
            rows = await cursor.fetchall()

            now_dt = parse_iso(now_iso_utc())
            if now_dt is None:
                return []

            expired: list[dict] = []
            for row in rows:
                # Defence in depth: a single corrupt row (non-numeric
                # accumulated_secs, bogus timestamp that parse_iso tolerates
                # but arithmetic rejects, etc.) must not abort the iteration
                # and leave every other expired timer unfired for this tick.
                try:
                    started_dt = parse_iso(row["started_at"])
                    if started_dt is None:
                        continue
                    elapsed = max(0, int((now_dt - started_dt).total_seconds()))
                    accumulated = row["accumulated_secs"]
                    if accumulated is None:
                        accumulated = 0
                    elif not isinstance(accumulated, (int, float)):
                        # Malformed row — skip rather than treat as zero,
                        # which would spuriously expire an otherwise-fresh
                        # timer. Caller also logs.
                        raise TypeError(
                            f"accumulated_secs is not numeric: {accumulated!r}"
                        )
                    effective = accumulated + elapsed
                    if effective >= row["duration_secs"]:
                        expired.append(self._timer_row_to_dict(row))
                except (TypeError, ValueError):
                    LOG.exception(
                        "Skipping corrupt session_timers row: %s",
                        dict(row),
                    )
                    continue
            return expired

    # ------------------------------------------------------------------
    # Session notes CRUD
    # ------------------------------------------------------------------

    @staticmethod
    def _note_row_to_dict(row) -> dict:
        """Convert a ``session_notes`` row to a plain dict."""
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "body": row["body"],
        }

    async def _fetch_primary_note_locked(self, session_id: str) -> Optional[dict]:
        """Fetch the earliest-created note row as a dict (must hold ``_lock``)."""
        cursor = await self._conn.execute(
            "SELECT id, session_id, created_at, updated_at, body "
            "FROM session_notes "
            "WHERE session_id = ? "
            "ORDER BY created_at ASC, id ASC "
            "LIMIT 1",
            (session_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._note_row_to_dict(row)

    async def get_primary_note(self, session_id: str) -> Optional[dict]:
        """Return the earliest-created note row for a session, or None.

        The "primary" note is the first one created for a session (lowest
        ``created_at``, breaking ties by lowest ``id``).  Notes are
        readable on any session — active or ended.
        """
        async with self._lock:
            return await self._fetch_primary_note_locked(session_id)

    async def upsert_primary_note(self, session_id: str, body: str) -> dict:
        """Create or update the primary note for a session.

        If no notes row exists, INSERTs one with ``created_at = updated_at = now``.
        Otherwise UPDATEs the earliest-created row, setting ``body`` and
        ``updated_at = now`` while preserving ``created_at``.

        Notes remain editable after a session ends (unlike timers), so no
        active-session guard is applied here.  The legacy ``sessions.notes``
        column is dual-written to ``body`` for one release cycle of
        backwards compatibility — if no matching ``sessions`` row exists
        the FK violation surfaces.
        """
        async with self._lock:
            now_ts = now_iso_utc()
            existing = await self._fetch_primary_note_locked(session_id)

            if existing is None:
                await self._conn.execute(
                    "INSERT INTO session_notes "
                    "(session_id, created_at, updated_at, body) "
                    "VALUES (?, ?, ?, ?)",
                    (session_id, now_ts, now_ts, body),
                )
            else:
                await self._conn.execute(
                    "UPDATE session_notes "
                    "SET body = ?, updated_at = ? "
                    "WHERE id = ?",
                    (body, now_ts, existing["id"]),
                )

            # Dual-write to the legacy sessions.notes column.
            await self._conn.execute(
                "UPDATE sessions SET notes = ? WHERE id = ?",
                (body, session_id),
            )

            await self._conn.commit()
            row = await self._fetch_primary_note_locked(session_id)
            assert row is not None  # just inserted or updated
            return row

    async def get_notes(self, session_id: str) -> list[dict]:
        """Return all notes rows for a session, ordered by created_at ASC, id ASC.

        Empty list if none.  Works for any session, active or ended.
        """
        async with self._lock:
            cursor = await self._conn.execute(
                "SELECT id, session_id, created_at, updated_at, body "
                "FROM session_notes "
                "WHERE session_id = ? "
                "ORDER BY created_at ASC, id ASC",
                (session_id,),
            )
            rows = await cursor.fetchall()
            return [self._note_row_to_dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    async def session_exists(self, session_id: str) -> bool:
        """Return True if a session with the given ID exists."""
        async with self._lock:
            cursor = await self._conn.execute(
                "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
            )
            row = await cursor.fetchone()
            return row is not None

    async def has_history(self) -> bool:
        """Return True if there is at least one recorded reading."""
        async with self._lock:
            cursor = await self._conn.execute(
                "SELECT 1 FROM probe_readings LIMIT 1"
            )
            row = await cursor.fetchone()
            return row is not None

    async def latest_ts(self) -> Optional[str]:
        """Return the most recent recorded_at timestamp, or None."""
        async with self._lock:
            cursor = await self._conn.execute(
                "SELECT MAX(recorded_at) as ts FROM probe_readings"
            )
            row = await cursor.fetchone()
            return row["ts"] if row else None

    async def is_device_in_session(self, address: str) -> bool:
        """Check whether a device is part of the current active session
        and has not left."""
        async with self._lock:
            if self._current_session_id is None:
                return False
            cursor = await self._conn.execute(
                "SELECT 1 FROM session_devices "
                "WHERE session_id = ? AND address = ? AND left_at IS NULL",
                (self._current_session_id, address),
            )
            row = await cursor.fetchone()
            return row is not None

    async def get_current_session_id(self) -> Optional[str]:
        """Return the current session ID, or None if no session is active."""
        async with self._lock:
            return self._current_session_id

    async def get_max_seq(self, session_id: str, address: str) -> int:
        """Return the highest seq number recorded for a device in a session.

        Returns 0 if no readings exist yet.
        """
        async with self._lock:
            cursor = await self._conn.execute(
                "SELECT MAX(seq) as max_seq FROM probe_readings "
                "WHERE session_id = ? AND address = ?",
                (session_id, address),
            )
            row = await cursor.fetchone()
            return row["max_seq"] or 0

    # ------------------------------------------------------------------
    # Downsampling support
    # ------------------------------------------------------------------

    async def execute_downsampling(
        self, session_id: str, older_than, newer_than, bucket_seconds: int, label: str,
    ) -> None:
        """Run a downsampling pass on probe_readings for the given session.

        Called by ``service.history.downsampler`` — provides controlled access
        to the database connection without exposing private attributes.
        """
        async with self._lock:
            await downsample_range(
                self._conn, session_id,
                older_than=older_than,
                newer_than=newer_than,
                bucket_seconds=bucket_seconds,
                label=label,
            )
