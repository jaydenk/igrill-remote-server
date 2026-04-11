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


def test_fixed_mode_no_approaching_high():
    """Fixed mode should never fire approaching_high."""
    ev = AlertEvaluator()
    target = TargetConfig(probe_index=1, mode="fixed", target_value=90, pre_alert_offset=5)
    ev.set_targets("s1", [target])
    events = ev.evaluate("s1", [_make_probe(1, 87)], "sensor1")
    assert len(events) == 1
    assert events[0]["type"] == "target_approaching"
    assert "subtype" not in events[0]["payload"]


def test_range_no_re_alert_after_drop():
    """Temperature dropping below range after being in range should not re-alert."""
    ev = AlertEvaluator()
    target = TargetConfig(probe_index=1, mode="range", range_low=110, range_high=130, pre_alert_offset=5)
    ev.set_targets("s1", [target])
    ev.evaluate("s1", [_make_probe(1, 115)], "sensor1")  # reached
    events = ev.evaluate("s1", [_make_probe(1, 100)], "sensor1")  # dropped below
    assert len(events) == 0  # No re-alert


def test_multiple_probes_independent():
    """Each probe tracks its own alert state."""
    ev = AlertEvaluator()
    t1 = TargetConfig(probe_index=1, mode="fixed", target_value=90, pre_alert_offset=5)
    t2 = TargetConfig(probe_index=2, mode="range", range_low=110, range_high=130, pre_alert_offset=5)
    ev.set_targets("s1", [t1, t2])
    events = ev.evaluate("s1", [_make_probe(1, 86), _make_probe(2, 106)], "sensor1")
    assert len(events) == 2
    types = {e["payload"]["probeIndex"]: e["type"] for e in events}
    assert types[1] == "target_approaching"
    assert types[2] == "target_approaching"


def test_zero_pre_alert_offset():
    """With offset=0, approaching should never fire."""
    ev = AlertEvaluator()
    target = TargetConfig(probe_index=1, mode="fixed", target_value=90, pre_alert_offset=0)
    ev.set_targets("s1", [target])
    events = ev.evaluate("s1", [_make_probe(1, 89)], "sensor1")
    assert len(events) == 0  # 89 < 90, and threshold = 90 - 0 = 90, so not approaching
    events = ev.evaluate("s1", [_make_probe(1, 90)], "sensor1")
    assert len(events) == 1  # reached only (exceeded requires temp > target, i.e. strictly greater)
