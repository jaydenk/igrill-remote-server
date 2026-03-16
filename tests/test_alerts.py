"""Tests for the alert evaluator."""

from service.alerts.evaluator import AlertEvaluator
from service.models.session import TargetConfig


def _probes(temps: dict[int, float]) -> list[dict]:
    """Build a minimal probe list from {index: temperature} pairs."""
    return [
        {"index": idx, "temperature": t, "unplugged": False, "raw": int(t * 10)}
        for idx, t in temps.items()
    ]


def test_no_events_without_targets():
    ev = AlertEvaluator()
    assert ev.evaluate(1, _probes({1: 50.0}), "AA:BB:CC") == []


def test_approaching_event():
    ev = AlertEvaluator()
    ev.set_targets(
        1,
        [TargetConfig(probe_index=1, mode="fixed", target_value=100.0, pre_alert_offset=10.0)],
    )
    events = ev.evaluate(1, _probes({1: 91.0}), "AA:BB:CC")
    assert len(events) == 1
    assert events[0]["type"] == "target_approaching"


def test_reached_event():
    ev = AlertEvaluator()
    ev.set_targets(1, [TargetConfig(probe_index=1, mode="fixed", target_value=100.0)])
    events = ev.evaluate(1, _probes({1: 100.0}), "AA:BB:CC")
    assert any(e["type"] == "target_reached" for e in events)


def test_exceeded_event():
    ev = AlertEvaluator()
    ev.set_targets(1, [TargetConfig(probe_index=1, mode="fixed", target_value=100.0)])
    events = ev.evaluate(1, _probes({1: 105.0}), "AA:BB:CC")
    assert any(e["type"] == "target_exceeded" for e in events)


def test_no_duplicate_alerts():
    ev = AlertEvaluator()
    ev.set_targets(1, [TargetConfig(probe_index=1, mode="fixed", target_value=100.0)])
    ev.evaluate(1, _probes({1: 100.0}), "AA:BB:CC")
    events = ev.evaluate(1, _probes({1: 100.0}), "AA:BB:CC")
    assert "target_reached" not in [e["type"] for e in events]


def test_range_mode():
    ev = AlertEvaluator()
    ev.set_targets(
        1,
        [
            TargetConfig(
                probe_index=1,
                mode="range",
                range_low=90.0,
                range_high=110.0,
                pre_alert_offset=5.0,
            ),
        ],
    )
    # Well below range -- no alerts
    assert ev.evaluate(1, _probes({1: 80.0}), "AA:BB:CC") == []

    # Inside pre-alert band (85-89.99...)
    events = ev.evaluate(1, _probes({1: 86.0}), "AA:BB:CC")
    assert len(events) == 1
    assert events[0]["type"] == "target_approaching"

    # Inside the target range
    events = ev.evaluate(1, _probes({1: 95.0}), "AA:BB:CC")
    assert any(e["type"] == "target_reached" for e in events)


def test_clear_session():
    ev = AlertEvaluator()
    ev.set_targets(1, [TargetConfig(probe_index=1, mode="fixed", target_value=100.0)])
    ev.clear_session(1)
    assert ev.evaluate(1, _probes({1: 100.0}), "AA:BB:CC") == []


def test_unplugged_probe_ignored():
    ev = AlertEvaluator()
    ev.set_targets(1, [TargetConfig(probe_index=1, mode="fixed", target_value=100.0)])
    probes = [{"index": 1, "temperature": None, "unplugged": True, "raw": 63536}]
    assert ev.evaluate(1, probes, "AA:BB:CC") == []
