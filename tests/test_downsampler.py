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


@pytest.mark.asyncio
async def test_downsample_averages_battery_and_propane(store, sample_address):
    """The surviving device_readings row for a collapsed bucket must
    carry the AVERAGE of battery / propane across the bucket — not the
    earliest sample's snapshot. Previously the downsampler kept the
    first sample's device_readings row by virtue of the orphan-cleanup
    deleting the rest, which produced non-monotonic battery jumps in
    the joined view."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]

    old_time = datetime.now(timezone.utc) - timedelta(days=2)
    bucket_start = old_time.replace(second=0, microsecond=0)
    batteries = [100, 80, 60]
    propanes = [40.0, 30.0, 20.0]
    for i, (batt, prop) in enumerate(zip(batteries, propanes)):
        ts = (bucket_start + timedelta(seconds=i * 10)).isoformat()
        await store.record_reading(
            session_id=sid, address=sample_address, seq=i + 1,
            probes=[{"index": 1, "temperature": 70.0 + i * 10}],
            battery=batt, propane=prop, heating=None, recorded_at=ts,
        )

    from service.history.downsampler import downsample_session
    await downsample_session(store, sid)

    readings = await store.get_session_readings(sid)
    assert len(readings) == 1
    # 100/80/60 → 80; 40/30/20 → 30.
    assert readings[0]["battery"] == pytest.approx(80, abs=1)
    assert readings[0]["propane"] == pytest.approx(30, abs=1)


@pytest.mark.asyncio
async def test_downsample_preserves_device_readings_when_full_temp_missing(
    store, sample_address,
):
    """If the bucket's earliest sample had no battery_pct (None) but a
    later sample did, the downsampled row must carry the average of the
    non-null values rather than dropping to NULL."""
    start = await store.start_session(addresses=[sample_address], reason="user")
    sid = start["session_id"]

    old_time = datetime.now(timezone.utc) - timedelta(days=2)
    bucket_start = old_time.replace(second=0, microsecond=0)
    batteries = [None, 80, 60]
    for i, batt in enumerate(batteries):
        ts = (bucket_start + timedelta(seconds=i * 10)).isoformat()
        await store.record_reading(
            session_id=sid, address=sample_address, seq=i + 1,
            probes=[{"index": 1, "temperature": 70.0 + i * 10}],
            battery=batt, propane=None, heating=None, recorded_at=ts,
        )

    from service.history.downsampler import downsample_session
    await downsample_session(store, sid)

    readings = await store.get_session_readings(sid)
    assert len(readings) == 1
    # avg(80, 60) → 70.
    assert readings[0]["battery"] == pytest.approx(70, abs=1)
