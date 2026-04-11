"""Evaluates probe readings against target temperatures and generates alert events."""

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

from service.models.session import TargetConfig

LOG = logging.getLogger("igrill.alert")


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
        """Register (or replace) the target configs for *session_id*."""
        self._targets[session_id] = targets
        for t in targets:
            self._state[(session_id, t.probe_index)] = ProbeAlertState()

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

            effective_target: Optional[float] = target.effective_target()
            if effective_target is None:
                continue

            base_payload = {
                "sensorId": sensor_id,
                "sessionId": session_id,
                "probeIndex": target.probe_index,
                "currentTemp": temp,
                "target": target.to_dict(),
            }

            approaching_high = False

            if target.mode == "fixed":
                reached = temp >= effective_target
                exceeded = temp > effective_target
                threshold = effective_target - target.pre_alert_offset
                approaching = temp >= threshold and not reached
            else:  # range
                low = target.effective_low() or 0
                reached = low <= temp <= effective_target
                exceeded = temp > effective_target
                approaching = temp >= (low - target.pre_alert_offset) and temp < low
                approaching_high = (
                    temp > (effective_target - target.pre_alert_offset)
                    and temp <= effective_target
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
