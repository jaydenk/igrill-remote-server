"""Post-session reading downsampler.

After a session ends, reduces reading density based on age:
- Readings < 24 hours old: keep full resolution (no change)
- Readings 1-7 days old: downsample to 1-minute averages
- Readings > 7 days old: downsample to 5-minute averages

Downsampled rows replace the originals (delete old rows, insert averaged rows).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING

import aiosqlite

if TYPE_CHECKING:
    from service.history.store import HistoryStore

LOG = logging.getLogger("igrill.session")


async def downsample_session(store: HistoryStore, session_id: str) -> None:
    """Downsample probe readings for a completed session.

    Delegates to :meth:`HistoryStore.execute_downsampling` so that database
    access goes through the store's lock rather than reaching into private
    attributes.
    """
    now = datetime.now(timezone.utc)
    cutoff_24h = now - timedelta(hours=24)
    cutoff_7d = now - timedelta(days=7)

    # Downsample very old readings first (> 7 days), then medium-old (1-7 days).
    await store.execute_downsampling(
        session_id,
        older_than=cutoff_7d,
        newer_than=None,
        bucket_seconds=300,
        label="7d+",
    )
    await store.execute_downsampling(
        session_id,
        older_than=cutoff_24h,
        newer_than=cutoff_7d,
        bucket_seconds=60,
        label="1-7d",
    )


async def downsample_range(
    conn: aiosqlite.Connection,
    session_id: str,
    older_than: datetime,
    newer_than: datetime | None,
    bucket_seconds: int,
    label: str,
) -> None:
    """Downsample probe_readings in a time range to fixed-size buckets.

    For each ``(address, probe_index)`` group, readings are bucketed
    by ``bucket_seconds``.  Buckets with more than one reading are
    collapsed into a single averaged row; singleton buckets are left
    untouched.

    Must be called while the store's lock is held.
    """
    older_than_iso = older_than.isoformat()

    query = (
        "SELECT id, session_id, address, recorded_at, seq, probe_index, temperature "
        "FROM probe_readings "
        "WHERE session_id = ? AND recorded_at < ?"
    )
    params: list = [session_id, older_than_iso]

    if newer_than is not None:
        query += " AND recorded_at >= ?"
        params.append(newer_than.isoformat())

    query += " ORDER BY address, probe_index, recorded_at"

    cursor = await conn.execute(query, params)
    rows = await cursor.fetchall()
    if not rows:
        return

    # Group readings by (address, probe_index) then by time bucket
    buckets: dict[tuple[str, int, int], list[dict]] = {}
    for row in rows:
        address = row["address"]
        probe_index = row["probe_index"]
        recorded_at = row["recorded_at"]

        dt = datetime.fromisoformat(recorded_at)
        epoch = dt.timestamp()
        bucket_key = int(epoch // bucket_seconds)

        key = (address, probe_index, bucket_key)
        if key not in buckets:
            buckets[key] = []
        buckets[key].append({
            "id": row["id"],
            "session_id": row["session_id"],
            "address": address,
            "recorded_at": recorded_at,
            "seq": row["seq"],
            "probe_index": probe_index,
            "temperature": row["temperature"],
        })

    deleted_count = 0
    inserted_count = 0
    touched_addresses: set[tuple[str, str]] = set()

    try:
        await conn.execute("BEGIN")

        for key, readings in buckets.items():
            if len(readings) <= 1:
                continue

            address, probe_index, bucket_key = key
            temps = [r["temperature"] for r in readings if r["temperature"] is not None]
            avg_temp = sum(temps) / len(temps) if temps else None
            timestamps = [datetime.fromisoformat(r["recorded_at"]) for r in readings]
            mid_ts = min(timestamps) + (max(timestamps) - min(timestamps)) / 2
            mid_ts_iso = mid_ts.isoformat()
            min_seq = min(r["seq"] for r in readings)
            sid = readings[0]["session_id"]
            touched_addresses.add((sid, address))

            ids = [r["id"] for r in readings]
            placeholders = ",".join("?" for _ in ids)
            await conn.execute(
                f"DELETE FROM probe_readings WHERE id IN ({placeholders})", ids,
            )
            deleted_count += len(ids)

            await conn.execute(
                "INSERT INTO probe_readings "
                "(session_id, address, recorded_at, seq, probe_index, temperature) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (sid, address, mid_ts_iso, min_seq, probe_index, avg_temp),
            )
            inserted_count += 1

        for sid_val, addr_val in touched_addresses:
            await conn.execute(
                "DELETE FROM device_readings "
                "WHERE session_id = ? AND address = ? "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM probe_readings "
                "  WHERE probe_readings.session_id = device_readings.session_id "
                "  AND probe_readings.address = device_readings.address "
                "  AND probe_readings.seq = device_readings.seq"
                ")",
                (sid_val, addr_val),
            )

        await conn.commit()
    except Exception:
        await conn.execute("ROLLBACK")
        LOG.error(
            "Downsampling FAILED for session %s [%s] — rolled back",
            session_id, label,
        )
        raise

    if deleted_count > 0 or inserted_count > 0:
        LOG.info(
            "Downsampled session %s [%s]: deleted %d readings, "
            "inserted %d averaged readings (bucket=%ds)",
            session_id,
            label,
            deleted_count,
            inserted_count,
            bucket_seconds,
        )
