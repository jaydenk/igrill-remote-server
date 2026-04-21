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


def test_set_targets_resets_flags_when_target_raised():
    """Raising a target above an already-crossed threshold must re-arm
    all one-shot flags so a fresh crossing of the new higher threshold
    fires target_approaching / target_reached / target_exceeded again."""
    ev = AlertEvaluator()
    ev.set_targets("s1", [TargetConfig(
        probe_index=1, mode="fixed", target_value=70.0,
        pre_alert_offset=5.0, reminder_interval_secs=0, label="meat",
    )])
    # Seed: probe climbs past 70 → all one-shots fire.
    ev.evaluate("s1", [_make_probe(1, 65.0)], "A")  # approaching
    ev.evaluate("s1", [_make_probe(1, 70.0)], "A")  # reached
    ev.evaluate("s1", [_make_probe(1, 72.0)], "A")  # exceeded
    state_before = ev._state[("s1", 1)]
    assert state_before.approaching_sent
    assert state_before.reached_sent
    assert state_before.exceeded_sent

    # User raises target to 90 (strictly above the old 70).
    ev.set_targets("s1", [TargetConfig(
        probe_index=1, mode="fixed", target_value=90.0,
        pre_alert_offset=5.0, reminder_interval_secs=0, label="meat",
    )])

    # Probe now crosses the new 90°C threshold. target_exceeded must
    # re-fire exactly once.
    events = ev.evaluate("s1", [_make_probe(1, 91.0)], "A")
    exceeded = [e for e in events if e["type"] == "target_exceeded"]
    assert len(exceeded) == 1, (
        "target_exceeded must re-arm when the target is raised: "
        f"got events={events}"
    )


def test_set_targets_preserves_flags_when_target_lowered():
    """Regression guard: lowering a target the probe has already
    crossed must NOT re-fire — an exceeded probe that stays over a
    lower target should stay silent."""
    ev = AlertEvaluator()
    ev.set_targets("s1", [TargetConfig(
        probe_index=1, mode="fixed", target_value=70.0,
        pre_alert_offset=5.0, reminder_interval_secs=0, label="meat",
    )])
    ev.evaluate("s1", [_make_probe(1, 72.0)], "A")  # fires reached + exceeded

    # Lower target to 65 (still below probe temp — already exceeded).
    ev.set_targets("s1", [TargetConfig(
        probe_index=1, mode="fixed", target_value=65.0,
        pre_alert_offset=5.0, reminder_interval_secs=0, label="meat",
    )])
    events = ev.evaluate("s1", [_make_probe(1, 72.0)], "A")
    assert not any(e["type"] == "target_exceeded" for e in events), (
        f"lowering must not re-fire exceeded: got {events}"
    )
    assert not any(e["type"] == "target_reached" for e in events), (
        f"lowering must not re-fire reached: got {events}"
    )


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


# ---------------------------------------------------------------------------
# F2 — reminder-interval repeats while probe stays over target
# ---------------------------------------------------------------------------


class _FakeClock:
    """Test double for time.monotonic() that lets individual tests step
    the clock forward deterministically. Without this the reminder tests
    would have to sleep() for real seconds — 10+ minutes per case —
    which is impractical in CI."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def __call__(self) -> float:  # matches time.monotonic signature
        return self.now


def test_reminder_fires_every_interval_while_over_target(monkeypatch):
    """F2: with a 180s reminder interval and temperature held steady over
    the target for 10 minutes, the evaluator must emit target_reminder
    at t=180, t=360, and t=540 — not just once."""
    import service.alerts.evaluator as ev_mod

    clock = _FakeClock()
    monkeypatch.setattr(ev_mod.time, "monotonic", clock)

    ev = AlertEvaluator()
    target = TargetConfig(
        probe_index=1, mode="fixed", target_value=75.0,
        pre_alert_offset=5.0, reminder_interval_secs=180,
    )
    ev.set_targets("s1", [target])

    # t=0 — probe crosses the target (82°C for a 75°C target): both
    # reached and exceeded fire on the same tick. No reminder yet —
    # the interval anchor starts here.
    events = ev.evaluate("s1", [_make_probe(1, 82.0)], "sensor1")
    types = [e["type"] for e in events]
    assert types == ["target_reached", "target_exceeded"], (
        f"expected reached+exceeded on crossing, got {types}"
    )

    # Intermediate ticks within the interval emit nothing.
    clock.advance(60)
    assert ev.evaluate("s1", [_make_probe(1, 82.0)], "sensor1") == []
    clock.advance(60)  # t=120
    assert ev.evaluate("s1", [_make_probe(1, 82.0)], "sensor1") == []

    # t=180 — first reminder fires.
    clock.advance(60)
    events = ev.evaluate("s1", [_make_probe(1, 82.0)], "sensor1")
    assert [e["type"] for e in events] == ["target_reminder"], (
        f"expected target_reminder at t=180, got {[e['type'] for e in events]}"
    )

    # t=240 — still over-target, but only 60s since the last reminder →
    # silent. Verifies the anchor advances on each reminder, not just on
    # the original crossing.
    clock.advance(60)
    assert ev.evaluate("s1", [_make_probe(1, 82.0)], "sensor1") == []

    # t=360 — second reminder.
    clock.advance(120)
    events = ev.evaluate("s1", [_make_probe(1, 82.0)], "sensor1")
    assert [e["type"] for e in events] == ["target_reminder"]

    # t=540 — third reminder.
    clock.advance(180)
    events = ev.evaluate("s1", [_make_probe(1, 82.0)], "sensor1")
    assert [e["type"] for e in events] == ["target_reminder"]


def test_reminder_survives_brief_dip_below_target(monkeypatch):
    """F2: a short noise dip below the target must not cancel the
    reminder schedule. Real iGrill readings bounce around — if any
    sub-threshold reading reset the reminder, the user would stop
    getting nudges whenever sensor noise crossed the line, which is
    most of a cook."""
    import service.alerts.evaluator as ev_mod

    clock = _FakeClock()
    monkeypatch.setattr(ev_mod.time, "monotonic", clock)

    ev = AlertEvaluator()
    target = TargetConfig(
        probe_index=1, mode="fixed", target_value=75.0,
        pre_alert_offset=5.0, reminder_interval_secs=180,
    )
    ev.set_targets("s1", [target])

    # t=0 — cross the target.
    ev.evaluate("s1", [_make_probe(1, 82.0)], "sensor1")

    # t=90 — temperature briefly dips under the target (noise). No
    # new events — reached/exceeded already sent, no reminder due yet.
    clock.advance(90)
    assert ev.evaluate("s1", [_make_probe(1, 73.0)], "sensor1") == []

    # t=120 — back over. Still no reminder due.
    clock.advance(30)
    assert ev.evaluate("s1", [_make_probe(1, 82.0)], "sensor1") == []

    # t=180 — reminder is due 180s after the ORIGINAL crossing, not
    # after the dip. Dip must not reset the schedule anchor.
    clock.advance(60)
    events = ev.evaluate("s1", [_make_probe(1, 82.0)], "sensor1")
    assert [e["type"] for e in events] == ["target_reminder"], (
        "noise dip reset the reminder schedule — reminders must anchor "
        "on crossings and reminders, not cancel on dips"
    )


def test_set_targets_resets_flags_when_old_target_had_no_effective():
    """Re-arming guard: if the previous target had no effective target
    (e.g. range with range_high unset), and the new target does, flags
    must reset so the probe crossing the new threshold still fires."""
    ev = AlertEvaluator()
    # Seed with a fully-defined target and arm the flags.
    ev.set_targets("s1", [TargetConfig(
        probe_index=1, mode="fixed", target_value=70.0,
        pre_alert_offset=5.0, reminder_interval_secs=0, label="x",
    )])
    ev.evaluate("s1", [_make_probe(1, 72.0)], "A")
    assert ev._state[("s1", 1)].exceeded_sent

    # Replace with a range target that has range_high unset —
    # effective_target() is None.
    ev.set_targets("s1", [TargetConfig(
        probe_index=1, mode="range", range_low=None, range_high=None,
        pre_alert_offset=5.0, reminder_interval_secs=0, label="x",
    )])
    # With no effective target, evaluate() returns no events for this probe.
    assert ev.evaluate("s1", [_make_probe(1, 72.0)], "A") == []

    # Now swap back to a concrete fixed target. Flags should have been
    # reset when the None-effective target was set (or at the latest
    # when the new concrete target replaces the None one — both are
    # acceptable; test the end state).
    ev.set_targets("s1", [TargetConfig(
        probe_index=1, mode="fixed", target_value=90.0,
        pre_alert_offset=5.0, reminder_interval_secs=0, label="x",
    )])
    events = ev.evaluate("s1", [_make_probe(1, 91.0)], "A")
    assert any(e["type"] == "target_exceeded" for e in events), (
        f"flags must have re-armed across the None-effective target: got {events}"
    )


def test_reminder_never_fires_when_interval_zero(monkeypatch):
    """Guard rail: an interval of 0 means reminders are disabled. The
    evaluator must emit no target_reminder events no matter how long
    the probe stays over target."""
    import service.alerts.evaluator as ev_mod

    clock = _FakeClock()
    monkeypatch.setattr(ev_mod.time, "monotonic", clock)

    ev = AlertEvaluator()
    target = TargetConfig(
        probe_index=1, mode="fixed", target_value=75.0,
        pre_alert_offset=5.0, reminder_interval_secs=0,
    )
    ev.set_targets("s1", [target])

    ev.evaluate("s1", [_make_probe(1, 82.0)], "sensor1")  # crossing
    for _ in range(20):
        clock.advance(180)
        events = ev.evaluate("s1", [_make_probe(1, 82.0)], "sensor1")
        assert all(e["type"] != "target_reminder" for e in events)
