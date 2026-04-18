"""Evaluates probe readings against target temperatures and generates alert events."""

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

from service.models.session import TargetConfig

LOG = logging.getLogger("igrill.alert")


def _target_to_celsius(value: float, unit: str) -> float:
    """Convert a target temperature *value* expressed in *unit* to Celsius.
    BLE readings are always Celsius; targets may be stored in either unit."""
    return (value - 32.0) * 5.0 / 9.0 if unit == "F" else value


def _offset_to_celsius(offset: float, unit: str) -> float:
    """Convert a temperature *delta* (not an absolute temperature) to Celsius.
    The 32°F baseline cancels for a delta, so it's just the scaling factor."""
    return offset * 5.0 / 9.0 if unit == "F" else offset


@dataclass
class ProbeAlertState:
    """Tracks which alert stages have already been dispatched for a probe."""

    approaching_sent: bool = False
    approaching_high_sent: bool = False
    reached_sent: bool = False
    exceeded_sent: bool = False
    last_reminder_ts: float = 0.0


class AlertEvaluator:
    """Compares live probe temperatures against session targets and emits
    one-shot alert events (approaching, reached, exceeded) plus periodic
    reminders when the target has been exceeded.
    """

    def __init__(self) -> None:
        self._state: dict[tuple[str, int], ProbeAlertState] = {}  # (session_id, probe_index)
        self._targets: dict[str, list[TargetConfig]] = {}  # session_id -> targets

    def set_targets(self, session_id: str, targets: list[TargetConfig]) -> None:
        """Register (or replace) the target configs for *session_id*.

        Preserves ``ProbeAlertState`` for probe indices that were already
        being tracked — a user tweaking an in-flight target (e.g. raising
        it above the current temperature) must NOT re-fire the
        approaching/reached/exceeded one-shots that already dispatched
        for that probe. Only probe indices that weren't tracked before
        receive a fresh state.
        """
        self._targets[session_id] = targets
        incoming = {t.probe_index for t in targets}
        # Drop state for probes no longer in the target set — they're
        # not being evaluated any more, so their state is dead weight.
        stale = [
            key for key in self._state
            if key[0] == session_id and key[1] not in incoming
        ]
        for key in stale:
            del self._state[key]
        # Preserve existing state; only create fresh state for new probes.
        for t in targets:
            key = (session_id, t.probe_index)
            if key not in self._state:
                self._state[key] = ProbeAlertState()

    def clear_session(self, session_id: str) -> None:
        """Remove all targets and state for *session_id*."""
        self._targets.pop(session_id, None)
        keys = [k for k in self._state if k[0] == session_id]
        for k in keys:
            del self._state[k]

    def evaluate(
        self,
        session_id: str,
        probes: list[dict[str, Any]],
        sensor_id: str,
    ) -> list[dict[str, Any]]:
        """Evaluate all probes against targets.

        Returns a list of alert event dicts, each with ``type`` and ``payload``
        keys.  Possible types:

        * ``target_approaching`` -- temperature crossed the pre-alert threshold.
        * ``target_reached`` -- temperature hit the target.
        * ``target_exceeded`` -- temperature went above the target.
        * ``target_reminder`` -- periodic nudge while still above target.
        """
        targets = self._targets.get(session_id)
        if not targets:
            return []

        events: list[dict[str, Any]] = []
        now = time.monotonic()

        for target in targets:
            probe = next(
                (p for p in probes if p.get("index") == target.probe_index),
                None,
            )
            if probe is None or probe.get("unplugged") or probe.get("temperature") is None:
                continue

            temp: float = probe["temperature"]
            key = (session_id, target.probe_index)
            state = self._state.setdefault(key, ProbeAlertState())

            raw_effective_target: Optional[float] = target.effective_target()
            if raw_effective_target is None:
                continue

            # BLE readings are Celsius; convert stored target values into the
            # same unit before comparison. The offset is a delta, so it uses a
            # simpler scaling conversion with no 32° shift.
            effective_target_c = _target_to_celsius(raw_effective_target, target.unit)
            offset_c = _offset_to_celsius(target.pre_alert_offset, target.unit)

            base_payload = {
                "sensorId": sensor_id,
                "sessionId": session_id,
                "probeIndex": target.probe_index,
                "currentTemp": temp,
                "target": target.to_dict(),
            }

            approaching_high = False

            if target.mode == "fixed":
                reached = temp >= effective_target_c
                exceeded = temp > effective_target_c
                threshold = effective_target_c - offset_c
                approaching = temp >= threshold and not reached
            else:  # range
                raw_low = target.effective_low()
                if raw_low is None:
                    # Without a low bound a range target is unevaluable:
                    # the old `or 0` fallback silently collapsed the rule
                    # to "≤ high", firing target_reached at session start
                    # for every plausible temperature.
                    continue
                low_c = _target_to_celsius(raw_low, target.unit)
                reached = low_c <= temp <= effective_target_c
                exceeded = temp > effective_target_c
                approaching = temp >= (low_c - offset_c) and temp < low_c
                approaching_high = (
                    temp > (effective_target_c - offset_c)
                    and temp <= effective_target_c
                    and not exceeded
                )

            if approaching and not state.approaching_sent:
                state.approaching_sent = True
                events.append({"type": "target_approaching", "payload": base_payload})

            if target.mode == "range" and approaching_high and not state.approaching_high_sent:
                state.approaching_high_sent = True
                events.append({"type": "target_approaching", "payload": {
                    **base_payload, "subtype": "high"
                }})

            if reached and not state.reached_sent:
                state.reached_sent = True
                state.approaching_sent = True
                events.append({"type": "target_reached", "payload": base_payload})

            if exceeded and not state.exceeded_sent:
                state.exceeded_sent = True
                state.reached_sent = True
                state.approaching_sent = True
                state.last_reminder_ts = now
                events.append({"type": "target_exceeded", "payload": base_payload})

            if exceeded and target.reminder_interval_secs > 0:
                elapsed = now - state.last_reminder_ts
                if state.exceeded_sent and elapsed >= target.reminder_interval_secs:
                    state.last_reminder_ts = now
                    events.append({"type": "target_reminder", "payload": base_payload})

        return events
