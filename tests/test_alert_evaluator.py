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


def test_clear_session_resets_alert_state():
    """After clear_session, re-registering targets for the same session_id and
    re-evaluating should fire alerts again (state reset)."""
    ev = AlertEvaluator()
    target = TargetConfig(probe_index=1, mode="fixed", target_value=90, pre_alert_offset=5)
    ev.set_targets("s1", [target])

    events = ev.evaluate("s1", [_make_probe(1, 86)], "sensor1")
    assert len(events) == 1
    assert events[0]["type"] == "target_approaching"

    # A second evaluate at the same temp should not re-fire (state persists).
    events = ev.evaluate("s1", [_make_probe(1, 86)], "sensor1")
    assert len(events) == 0

    # Clearing the session should wipe per-probe state.
    ev.clear_session("s1")

    # After clearing and re-registering targets, approaching should fire again.
    ev.set_targets("s1", [target])
    events = ev.evaluate("s1", [_make_probe(1, 86)], "sensor1")
    assert len(events) == 1
    assert events[0]["type"] == "target_approaching"


def test_clear_session_unknown_id_is_noop():
    """clear_session on an unknown session_id must not raise."""
    ev = AlertEvaluator()
    # No targets or state registered at all.
    ev.clear_session("does-not-exist")

    # And also when other sessions exist.
    target = TargetConfig(probe_index=1, mode="fixed", target_value=90, pre_alert_offset=5)
    ev.set_targets("s1", [target])
    ev.clear_session("other-session")

    # s1 state should remain intact.
    events = ev.evaluate("s1", [_make_probe(1, 86)], "sensor1")
    assert len(events) == 1
    assert events[0]["type"] == "target_approaching"


def test_clear_session_only_affects_target_session():
    """clear_session must not touch state for other sessions."""
    ev = AlertEvaluator()
    target = TargetConfig(probe_index=1, mode="fixed", target_value=90, pre_alert_offset=5)
    ev.set_targets("s1", [target])
    ev.set_targets("s2", [target])

    # Fire approaching on both sessions.
    ev.evaluate("s1", [_make_probe(1, 86)], "sensor1")
    ev.evaluate("s2", [_make_probe(1, 86)], "sensor2")

    # Clear only s1.
    ev.clear_session("s1")

    # s2's state should still consider approaching already-sent (no re-fire).
    events = ev.evaluate("s2", [_make_probe(1, 86)], "sensor2")
    assert len(events) == 0
