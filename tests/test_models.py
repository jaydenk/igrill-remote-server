"""Tests for the data models module (device store, readings, session config)."""

import asyncio
import struct

import pytest

from service.models.device import DeviceStore
from service.models.reading import (
    UNPLUGGED_PROBE_CONSTANT,
    build_reading_payload,
    parse_pulse_element,
    parse_temperature_probe,
)
from service.models.session import TargetConfig


# -----------------------------------------------------------------------
# parse_temperature_probe
# -----------------------------------------------------------------------


class TestParseTemperatureProbe:
    def test_normal_reading(self):
        """A valid 2-byte little-endian value should parse to a float."""
        raw_value = 150
        data = struct.pack("<H", raw_value)
        result = parse_temperature_probe(1, data)
        assert result["index"] == 1
        assert result["temperature"] == 150.0
        assert result["raw"] == 150
        assert result["unplugged"] is False

    def test_unplugged_probe(self):
        """The magic constant 63536 signals an unplugged probe."""
        data = struct.pack("<H", UNPLUGGED_PROBE_CONSTANT)
        result = parse_temperature_probe(2, data)
        assert result["index"] == 2
        assert result["temperature"] is None
        assert result["raw"] == UNPLUGGED_PROBE_CONSTANT
        assert result["unplugged"] is True

    def test_short_data(self):
        """Fewer than 2 bytes should return None values safely."""
        result = parse_temperature_probe(3, b"\x01")
        assert result["index"] == 3
        assert result["temperature"] is None
        assert result["raw"] is None
        assert result["unplugged"] is None

    def test_empty_data(self):
        result = parse_temperature_probe(4, b"")
        assert result["index"] == 4
        assert result["temperature"] is None

    def test_zero_temperature(self):
        """Zero is a valid temperature (not unplugged)."""
        data = struct.pack("<H", 0)
        result = parse_temperature_probe(1, data)
        assert result["temperature"] == 0.0
        assert result["unplugged"] is False

    def test_index_is_one_based(self):
        """The index returned should match the index passed in."""
        data = struct.pack("<H", 100)
        for idx in (1, 2, 3, 4):
            assert parse_temperature_probe(idx, data)["index"] == idx


# -----------------------------------------------------------------------
# parse_pulse_element
# -----------------------------------------------------------------------


class TestParsePulseElement:
    def test_full_data(self):
        """All four heating values should parse from well-formed data."""
        # Layout: 1 padding byte, then 3-byte ASCII ints at offsets 1, 5, 9, 13
        # with 1-byte separators between groups (offsets 4, 8, 12).
        raw = b"\x00" + b"225" + b"\x00" + b"230" + b"\x00" + b"250" + b"\x00" + b"255"
        result = parse_pulse_element(raw)
        assert result["heating_actual1"] == 225
        assert result["heating_actual2"] == 230
        assert result["heating_setpoint1"] == 250
        assert result["heating_setpoint2"] == 255

    def test_short_data(self):
        """Short data should return None for fields that cannot be parsed."""
        result = parse_pulse_element(b"\x00123")
        assert result["heating_actual1"] == 123
        assert result["heating_actual2"] is None
        assert result["heating_setpoint1"] is None
        assert result["heating_setpoint2"] is None

    def test_empty_data(self):
        result = parse_pulse_element(b"")
        assert all(v is None for v in result.values())


# -----------------------------------------------------------------------
# build_reading_payload
# -----------------------------------------------------------------------


class TestBuildReadingPayload:
    def test_basic_payload(self):
        device = {
            "address": "70:91:8F:AA:BB:CC",
            "name": "My Grill",
            "model": "igrill_v3",
            "model_name": "IGrill V3",
            "last_update": "2026-01-01T00:00:00Z",
            "unit": "C",
            "battery_percent": 85,
            "propane_percent": None,
            "probes": [{"index": 1, "temperature": 72.0}],
            "connected_probes": [1],
            "probe_status": "probes_connected",
            "pulse": {},
            "error": None,
            "rssi": -55,
        }
        result = build_reading_payload(device, session_id=1, session_start_ts="2026-01-01T00:00:00Z")

        assert result["sensorId"] == "70:91:8F:AA:BB:CC"
        assert result["sessionId"] == 1
        assert result["sessionStartTs"] == "2026-01-01T00:00:00Z"
        assert result["data"]["unit"] == "C"
        assert result["data"]["battery_percent"] == 85
        assert result["data"]["probes"] == [{"index": 1, "temperature": 72.0}]
        # Quality metrics should be present when rssi or battery is set
        assert "q" in result
        assert result["q"]["rssi"] == -55
        assert result["q"]["batteryPct"] == 85

    def test_no_quality_metrics(self):
        """When neither rssi nor battery is available, 'q' should be absent."""
        device = {
            "address": "70:91:8F:00:00:00",
            "name": None,
            "model": None,
            "model_name": None,
            "last_update": None,
            "unit": None,
            "battery_percent": None,
            "propane_percent": None,
            "probes": [],
            "connected_probes": [],
            "probe_status": "unknown",
            "pulse": {},
            "error": None,
            "rssi": None,
        }
        result = build_reading_payload(device, session_id=None, session_start_ts=None)
        assert "q" not in result


# -----------------------------------------------------------------------
# TargetConfig
# -----------------------------------------------------------------------


class TestTargetConfig:
    def test_fixed_mode(self):
        tc = TargetConfig(probe_index=1, mode="fixed", target_value=74.0)
        assert tc.effective_target() == 74.0
        assert tc.effective_low() is None

    def test_range_mode(self):
        tc = TargetConfig(
            probe_index=2,
            mode="range",
            range_low=60.0,
            range_high=80.0,
        )
        assert tc.effective_target() == 80.0
        assert tc.effective_low() == 60.0

    def test_from_dict_roundtrip(self):
        original = TargetConfig(
            probe_index=3,
            mode="range",
            target_value=None,
            range_low=55.0,
            range_high=65.0,
            pre_alert_offset=5.0,
            reminder_interval_secs=120,
        )
        d = original.to_dict()
        restored = TargetConfig.from_dict(d)
        assert restored == original

    def test_from_dict_defaults(self):
        """Omitted optional fields should receive sensible defaults."""
        tc = TargetConfig.from_dict({"probe_index": 1})
        assert tc.mode == "fixed"
        assert tc.pre_alert_offset == 10.0
        assert tc.reminder_interval_secs == 300
        assert tc.target_value is None
        assert tc.range_low is None
        assert tc.range_high is None

    def test_to_dict_keys(self):
        tc = TargetConfig(probe_index=1, mode="fixed", target_value=100.0)
        d = tc.to_dict()
        expected_keys = {
            "probe_index",
            "mode",
            "target_value",
            "range_low",
            "range_high",
            "pre_alert_offset",
            "reminder_interval_secs",
            "label",
        }
        assert set(d.keys()) == expected_keys

    def test_unknown_mode_returns_none(self):
        tc = TargetConfig(probe_index=1, mode="unknown")
        assert tc.effective_target() is None
        assert tc.effective_low() is None

    def test_label_field(self):
        data = {"probe_index": 1, "mode": "fixed", "target_value": 93.0, "label": "Brisket Point"}
        tc = TargetConfig.from_dict(data)
        assert tc.label == "Brisket Point"
        d = tc.to_dict()
        assert d["label"] == "Brisket Point"

    def test_label_defaults_none(self):
        data = {"probe_index": 1, "mode": "fixed", "target_value": 93.0}
        tc = TargetConfig.from_dict(data)
        assert tc.label is None


# -----------------------------------------------------------------------
# DeviceStore
# -----------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine synchronously (avoids pytest-asyncio dependency)."""
    return asyncio.run(coro)


class TestDeviceStore:
    def _make_store(self):
        return DeviceStore()

    def test_upsert_and_get(self):
        async def _test():
            store = self._make_store()
            await store.upsert("AA:BB:CC:DD:EE:FF", name="Test Grill")
            device = await store.get_device("AA:BB:CC:DD:EE:FF")
            assert device is not None
            assert device["name"] == "Test Grill"
            assert device["address"] == "AA:BB:CC:DD:EE:FF"
            assert device["connected"] is False  # default

        _run(_test())

    def test_get_unknown_device(self):
        async def _test():
            store = self._make_store()
            assert await store.get_device("UNKNOWN") is None

        _run(_test())

    def test_snapshot(self):
        async def _test():
            store = self._make_store()
            await store.upsert("AA:BB:CC:DD:EE:01")
            await store.upsert("AA:BB:CC:DD:EE:02")
            snap = await store.snapshot()
            assert len(snap) == 2
            assert "AA:BB:CC:DD:EE:01" in snap
            assert "AA:BB:CC:DD:EE:02" in snap

        _run(_test())

    def test_upsert_merges_fields(self):
        async def _test():
            store = self._make_store()
            await store.upsert("AA:BB:CC:DD:EE:FF", name="First")
            await store.upsert("AA:BB:CC:DD:EE:FF", connected=True)
            device = await store.get_device("AA:BB:CC:DD:EE:FF")
            assert device["name"] == "First"
            assert device["connected"] is True

        _run(_test())

    def test_snapshot_returns_copies(self):
        """Mutations to a snapshot should not affect the store."""
        async def _test():
            store = self._make_store()
            await store.upsert("AA:BB:CC:DD:EE:FF", name="Original")
            snap = await store.snapshot()
            snap["AA:BB:CC:DD:EE:FF"]["name"] = "Mutated"
            device = await store.get_device("AA:BB:CC:DD:EE:FF")
            assert device["name"] == "Original"

        _run(_test())

    def test_reading_queue(self):
        async def _test():
            store = self._make_store()
            reading = {"seq": 1, "payload": {"temperature": 72.0}}
            await store.publish_reading(reading)
            result = await store.next_reading()
            assert result == reading

        _run(_test())

    def test_event_queue(self):
        async def _test():
            store = self._make_store()
            event = {"type": "session_start", "sessionId": 1}
            await store.publish_event(event)
            result = await store.next_event()
            assert result == event

        _run(_test())

    def test_reading_queue_drops_oldest_when_full(self):
        """When the queue is full, the oldest item should be discarded."""
        async def _test():
            store = self._make_store()
            for i in range(1000):
                await store.publish_reading({"seq": i})
            # Publish one more -- should drop seq=0
            await store.publish_reading({"seq": 1000})
            first = await store.next_reading()
            assert first["seq"] == 1

        _run(_test())

    def test_default_mutable_fields_are_independent(self):
        """Each device should get its own list/dict instances for defaults."""
        async def _test():
            store = self._make_store()
            await store.upsert("DEVICE_A")
            await store.upsert("DEVICE_B")
            a = await store.get_device("DEVICE_A")
            b = await store.get_device("DEVICE_B")
            # Mutating one should not affect the other
            a["probes"].append({"index": 1})
            b_fresh = await store.get_device("DEVICE_B")
            assert b_fresh["probes"] == []

        _run(_test())
