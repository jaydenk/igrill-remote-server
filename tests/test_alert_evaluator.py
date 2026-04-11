import pytest
from service.alerts.evaluator import AlertEvaluator
from service.models.session import TargetConfig


def _make_probe(index, temp, unplugged=False):
    return {"index": index, "temperature": temp, "unplugged": unplugged}


def test_range_approaching_from_below():
    ev = AlertEvaluator()
    target = TargetConfig(probe_index=1, mode="range", range_low=110, range_high=130, pre_alert_offset=5)
    ev.set_targets("s1", [target])
    events = ev.evaluate("s1", [_make_probe(1, 106)], "sensor1")
    assert len(events) == 1
    assert events[0]["type"] == "target_approaching"


def test_range_reached():
    ev = AlertEvaluator()
    target = TargetConfig(probe_index=1, mode="range", range_low=110, range_high=130, pre_alert_offset=5)
    ev.set_targets("s1", [target])
    ev.evaluate("s1", [_make_probe(1, 106)], "sensor1")  # approaching
    events = ev.evaluate("s1", [_make_probe(1, 115)], "sensor1")
    assert len(events) == 1
    assert events[0]["type"] == "target_reached"


def test_range_exceeded():
    ev = AlertEvaluator()
    target = TargetConfig(probe_index=1, mode="range", range_low=110, range_high=130, pre_alert_offset=5)
    ev.set_targets("s1", [target])
    ev.evaluate("s1", [_make_probe(1, 120)], "sensor1")  # reached
    events = ev.evaluate("s1", [_make_probe(1, 135)], "sensor1")
    assert len(events) == 1
    assert events[0]["type"] == "target_exceeded"


def test_range_approaching_high():
    ev = AlertEvaluator()
    target = TargetConfig(probe_index=1, mode="range", range_low=110, range_high=130, pre_alert_offset=5)
    ev.set_targets("s1", [target])
    ev.evaluate("s1", [_make_probe(1, 120)], "sensor1")  # reached
    events = ev.evaluate("s1", [_make_probe(1, 127)], "sensor1")
    assert len(events) == 1
    assert events[0]["type"] == "target_approaching"
    assert events[0]["payload"].get("subtype") == "high"
