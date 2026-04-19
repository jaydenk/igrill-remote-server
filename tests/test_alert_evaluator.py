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


# ---------------------------------------------------------------------------
# Target-edit state preservation (C1)
# ---------------------------------------------------------------------------


def test_set_targets_preserves_state_for_unchanged_probes():
    """Editing a target (e.g. raising it above the current temperature)
    must NOT re-fire approaching/reached/exceeded events. The existing
    ProbeAlertState for the same probe_index must be preserved across
    set_targets calls; only probes appearing for the FIRST time get
    fresh state."""
    from service.alerts.evaluator import AlertEvaluator
    from service.models.session import TargetConfig

    evaluator = AlertEvaluator()
    target = TargetConfig(
        probe_index=1, mode="fixed", target_value=60.0,
        range_low=None, range_high=None,
        pre_alert_offset=5.0, reminder_interval_secs=0,
        label="brisket",
    )
    evaluator.set_targets("s1", [target])

    # Walk the temperature up through approaching → reached → exceeded so
    # each one-shot event fires once in the normal course of a cook.
    evaluator.evaluate(
        "s1", [{"index": 1, "temperature": 56.0, "unplugged": False}], "A",
    )
    evaluator.evaluate(
        "s1", [{"index": 1, "temperature": 60.0, "unplugged": False}], "A",
    )
    evaluator.evaluate(
        "s1", [{"index": 1, "temperature": 75.0, "unplugged": False}], "A",
    )
    state_before = evaluator._state[("s1", 1)]
    assert (
        state_before.approaching_sent
        and state_before.reached_sent
        and state_before.exceeded_sent
    )

    # User tweaks the target to 80 (now ABOVE the live temp of 75).
    new_target = TargetConfig(
        probe_index=1, mode="fixed", target_value=80.0,
        range_low=None, range_high=None,
        pre_alert_offset=5.0, reminder_interval_secs=0,
        label="brisket",
    )
    evaluator.set_targets("s1", [new_target])

    # Next poll at 75°C. Under the new 80°C target, 75 is in the
    # "approaching" band. Because state must be PRESERVED, approaching_sent
    # is still True and no new events should fire — no duplicate banner.
    events = evaluator.evaluate(
        "s1", [{"index": 1, "temperature": 75.0, "unplugged": False}], "A",
    )
    assert events == [], \
        f"set_targets wiped state and re-emitted alerts: {events}"


def test_set_targets_fresh_state_for_new_probe_indices():
    """A probe index that wasn't in the previous target set must get a
    fresh ProbeAlertState so its first crossing fires alerts normally."""
    from service.alerts.evaluator import AlertEvaluator
    from service.models.session import TargetConfig

    evaluator = AlertEvaluator()
    evaluator.set_targets("s1", [TargetConfig(
        probe_index=1, mode="fixed", target_value=60.0,
        range_low=None, range_high=None,
        pre_alert_offset=5.0, reminder_interval_secs=0, label="a",
    )])
    # Fire everything on probe 1.
    evaluator.evaluate(
        "s1", [{"index": 1, "temperature": 75.0, "unplugged": False}], "A",
    )

    # Now also track probe 2.
    evaluator.set_targets("s1", [
        TargetConfig(
            probe_index=1, mode="fixed", target_value=60.0,
            range_low=None, range_high=None,
            pre_alert_offset=5.0, reminder_interval_secs=0, label="a",
        ),
        TargetConfig(
            probe_index=2, mode="fixed", target_value=50.0,
            range_low=None, range_high=None,
            pre_alert_offset=5.0, reminder_interval_secs=0, label="b",
        ),
    ])

    # Probe 2's first reading above its target must fire events.
    events = evaluator.evaluate("s1", [
        {"index": 1, "temperature": 76.0, "unplugged": False},
        {"index": 2, "temperature": 55.0, "unplugged": False},
    ], "A")
    fired_for_probe_2 = [e for e in events
                         if e["payload"]["probeIndex"] == 2]
    assert len(fired_for_probe_2) >= 1, \
        "probe 2 should fire target_reached/exceeded on first crossing"


# ---------------------------------------------------------------------------
# Range-mode None handling (C2)
# ---------------------------------------------------------------------------


def test_range_target_with_none_low_does_not_fire_reached():
    """A range-mode target with range_low unset must not collapse to
    '≤ range_high' via the 'or 0' fallback and fire target_reached at
    session start for any plausible temperature."""
    from service.alerts.evaluator import AlertEvaluator
    from service.models.session import TargetConfig

    evaluator = AlertEvaluator()
    target = TargetConfig(
        probe_index=1, mode="range",
        target_value=None,
        range_low=None, range_high=80.0,
        pre_alert_offset=5.0, reminder_interval_secs=0,
        label="bark",
    )
    evaluator.set_targets("s1", [target])
    events = evaluator.evaluate(
        "s1", [{"index": 1, "temperature": 25.0, "unplugged": False}], "A",
    )
    assert events == [], \
        f"range target with None low should emit no events, got {events}"


# ---------------------------------------------------------------------------
# D3: target stored in Fahrenheit, readings in Celsius
# ---------------------------------------------------------------------------


def test_fahrenheit_target_approaching_at_c_equivalent():
    """Target = 165F (= 73.89C), reading = 70C is within 5F (~2.78C) of target
    after converting the offset, so it should fire target_approaching but NOT
    target_reached.

    Without D3's conversion, the evaluator would compare 70 >= 165-5=160 → False
    (no approaching) and 70 >= 165 → False (no reached), silently never firing.
    """
    ev = AlertEvaluator()
    target = TargetConfig(
        probe_index=1, mode="fixed", target_value=165.0,
        pre_alert_offset=5.0, unit="F",
    )
    ev.set_targets("s1", [target])
    events = ev.evaluate("s1", [_make_probe(1, 72.0)], "sensor1")
    # 165F = 73.89C, offset 5F = 2.78C, threshold = 71.11C → 72 >= 71.11 and < 73.89
    assert len(events) == 1
    assert events[0]["type"] == "target_approaching"


def test_fahrenheit_target_reached_at_c_equivalent():
    """Target = 165F (= 73.89C). A C reading exactly at the converted target
    must fire target_reached (but not yet target_exceeded — temp is not
    strictly above)."""
    ev = AlertEvaluator()
    target = TargetConfig(
        probe_index=1, mode="fixed", target_value=165.0,
        pre_alert_offset=5.0, unit="F",
    )
    ev.set_targets("s1", [target])
    # Walk it through the stages so reached doesn't fire before approaching.
    ev.evaluate("s1", [_make_probe(1, 72.0)], "sensor1")  # approaching
    c_at_target = (165.0 - 32.0) * 5.0 / 9.0  # = 73.888..
    events = ev.evaluate("s1", [_make_probe(1, c_at_target)], "sensor1")
    types = {e["type"] for e in events}
    assert "target_reached" in types
    assert "target_exceeded" not in types, (
        "At exactly the target C equivalent, exceeded (temp > target) must be False"
    )


def test_fahrenheit_target_not_reached_below_c_equivalent():
    """Target = 165F (=73.89C), reading at 73.8C (=164.84F) must NOT fire reached."""
    ev = AlertEvaluator()
    target = TargetConfig(
        probe_index=1, mode="fixed", target_value=165.0,
        pre_alert_offset=5.0, unit="F",
    )
    ev.set_targets("s1", [target])
    events = ev.evaluate("s1", [_make_probe(1, 73.8)], "sensor1")
    types = {e["type"] for e in events}
    assert "target_reached" not in types
    assert "target_approaching" in types  # 73.8 is within the 5F offset band


def test_fahrenheit_range_target_evaluation():
    """Range target in Fahrenheit [225, 250] = [107.22C, 121.11C] — a C reading
    of 110 should sit inside the range and fire target_reached."""
    ev = AlertEvaluator()
    target = TargetConfig(
        probe_index=1, mode="range",
        range_low=225.0, range_high=250.0,
        pre_alert_offset=5.0, unit="F",
    )
    ev.set_targets("s1", [target])
    ev.evaluate("s1", [_make_probe(1, 100.0)], "sensor1")  # approaching
    events = ev.evaluate("s1", [_make_probe(1, 110.0)], "sensor1")
    assert any(e["type"] == "target_reached" for e in events)


def test_celsius_target_unchanged_by_conversion():
    """Default unit='C' path must remain identical to pre-D3 behaviour."""
    ev = AlertEvaluator()
    target = TargetConfig(
        probe_index=1, mode="fixed", target_value=90.0,
        pre_alert_offset=5.0,  # unit defaults to 'C'
    )
    ev.set_targets("s1", [target])
    events = ev.evaluate("s1", [_make_probe(1, 87.0)], "sensor1")
    assert len(events) == 1
    assert events[0]["type"] == "target_approaching"


# ---------------------------------------------------------------------------
# la-followups Task 2 — startup rehydration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rehydrate_alert_evaluator_from_resumed_session(tmp_db, sample_address):
    """After a server restart resumes an orphaned session, the in-memory
    AlertEvaluator starts empty and evaluate() returns no events for the
    resumed session id. Users' alerts set before the reboot would silently
    never fire. The startup rehydrate helper must load saved targets for
    the current session so alerts resume firing.
    """
    from service.history.store import HistoryStore
    from service.main import rehydrate_alert_evaluator

    store = HistoryStore(tmp_db, reconnect_grace=10)
    await store.connect()
    try:
        info = await store.start_session(
            addresses=[sample_address], reason="user"
        )
        sid = info["session_id"]
        await store.save_targets(sid, sample_address, [
            TargetConfig(
                probe_index=1, mode="fixed", target_value=75.0,
                range_low=None, range_high=None,
                pre_alert_offset=5.0, reminder_interval_secs=0,
                label="Brisket",
            )
        ])

        # Simulate restart: fresh evaluator, session resumes.
        evaluator = AlertEvaluator()
        assert evaluator._targets == {}

        await rehydrate_alert_evaluator(store, evaluator)

        assert sid in evaluator._targets
        assert len(evaluator._targets[sid]) == 1
        assert evaluator._targets[sid][0].probe_index == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_rehydrate_noop_when_no_resumed_session(tmp_db):
    """With no session to resume the helper must leave the evaluator alone."""
    from service.history.store import HistoryStore
    from service.main import rehydrate_alert_evaluator

    store = HistoryStore(tmp_db, reconnect_grace=10)
    await store.connect()
    try:
        evaluator = AlertEvaluator()
        await rehydrate_alert_evaluator(store, evaluator)
        assert evaluator._targets == {}
    finally:
        await store.close()
