"""Tests for the session reading downsampler."""

import pytest
from datetime import datetime, timezone, timedelta

from service.history.store import HistoryStore


@pytest.mark.asyncio
async def test_downsample_averages_temperatures(store, sample_address):
    """Readings in the same bucket should be averaged."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]

    # Align to a 60-second bucket boundary so all 3 readings land in the
    # same bucket (the 1-7 day tier uses bucket_seconds=60).
    old_time = datetime.now(timezone.utc) - timedelta(days=2)
    bucket_start = old_time.replace(second=0, microsecond=0)
    for i in range(3):
        ts = (bucket_start + timedelta(seconds=i * 10)).isoformat()
        await store.record_reading(
            session_id=sid, address=sample_address, seq=i + 1,
            probes=[{"index": 1, "temperature": 70.0 + i * 10}],
            battery=85, propane=None, heating=None, recorded_at=ts,
        )

    readings_before = await store.get_session_readings(sid)
    assert len(readings_before) == 3

    from service.history.downsampler import downsample_session
    await downsample_session(store, sid)

    readings_after = await store.get_session_readings(sid)
    assert len(readings_after) == 1
    assert readings_after[0]["temperature"] == pytest.approx(80.0)


@pytest.mark.asyncio
async def test_downsample_singleton_buckets_untouched(store, sample_address):
    """A bucket with only one reading should not be modified."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]

    old_time = datetime.now(timezone.utc) - timedelta(days=2)
    await store.record_reading(
        session_id=sid, address=sample_address, seq=1,
        probes=[{"index": 1, "temperature": 72.5}],
        battery=85, propane=None, heating=None, recorded_at=old_time.isoformat(),
    )

    from service.history.downsampler import downsample_session
    await downsample_session(store, sid)

    readings = await store.get_session_readings(sid)
    assert len(readings) == 1
    assert readings[0]["temperature"] == 72.5


@pytest.mark.asyncio
async def test_downsample_empty_range(store, sample_address):
    """Downsampling with no readings in range should be a no-op."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]

    await store.record_reading(
        session_id=sid, address=sample_address, seq=1,
        probes=[{"index": 1, "temperature": 72.5}],
        battery=85, propane=None, heating=None,
    )

    from service.history.downsampler import downsample_session
    await downsample_session(store, sid)

    readings = await store.get_session_readings(sid)
    assert len(readings) == 1


@pytest.mark.asyncio
async def test_downsample_none_temperatures(store, sample_address):
    """Buckets where all temperatures are None should produce None average."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]

    # Align to a 60-second bucket boundary so all 3 readings land in the
    # same bucket (the 1-7 day tier uses bucket_seconds=60).
    old_time = datetime.now(timezone.utc) - timedelta(days=2)
    bucket_start = old_time.replace(second=0, microsecond=0)
    for i in range(3):
        ts = (bucket_start + timedelta(seconds=i * 10)).isoformat()
        await store.record_reading(
            session_id=sid, address=sample_address, seq=i + 1,
            probes=[{"index": 1, "temperature": None}],
            battery=85, propane=None, heating=None, recorded_at=ts,
        )

    from service.history.downsampler import downsample_session
    await downsample_session(store, sid)

    readings = await store.get_session_readings(sid)
    assert len(readings) == 1
    assert readings[0]["temperature"] is None
