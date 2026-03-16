"""Session-related data models for target temperature configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TargetConfig:
    """Temperature target configuration for a single probe.

    Supports two modes:

    * **fixed** -- a single target temperature (``target_value``).
    * **range** -- a low/high band (``range_low`` / ``range_high``).

    ``pre_alert_offset`` degrees before the effective target the system will
    fire a pre-alert notification, and ``reminder_interval_secs`` controls
    how often the reminder repeats once the target has been reached.
    """

    probe_index: int
    mode: str  # "fixed" or "range"
    target_value: Optional[float] = None
    range_low: Optional[float] = None
    range_high: Optional[float] = None
    pre_alert_offset: float = 10.0
    reminder_interval_secs: int = 300

    def effective_target(self) -> Optional[float]:
        """Return the primary target temperature.

        * **fixed** mode: ``target_value``
        * **range** mode: ``range_high``
        """
        if self.mode == "fixed":
            return self.target_value
        if self.mode == "range":
            return self.range_high
        return None

    def effective_low(self) -> Optional[float]:
        """Return the lower bound, or ``None`` when not applicable.

        Only meaningful in **range** mode.
        """
        if self.mode == "range":
            return self.range_low
        return None

    @classmethod
    def from_dict(cls, data: dict) -> TargetConfig:
        """Construct a :class:`TargetConfig` from a plain dictionary."""
        return cls(
            probe_index=int(data["probe_index"]),
            mode=str(data.get("mode", "fixed")),
            target_value=_opt_float(data.get("target_value")),
            range_low=_opt_float(data.get("range_low")),
            range_high=_opt_float(data.get("range_high")),
            pre_alert_offset=float(data.get("pre_alert_offset", 10.0)),
            reminder_interval_secs=int(data.get("reminder_interval_secs", 300)),
        )

    def to_dict(self) -> dict:
        """Serialise to a plain dictionary suitable for JSON encoding."""
        return {
            "probe_index": self.probe_index,
            "mode": self.mode,
            "target_value": self.target_value,
            "range_low": self.range_low,
            "range_high": self.range_high,
            "pre_alert_offset": self.pre_alert_offset,
            "reminder_interval_secs": self.reminder_interval_secs,
        }


def _opt_float(value: object) -> Optional[float]:
    """Coerce *value* to ``float`` if non-``None``."""
    if value is None:
        return None
    return float(value)  # type: ignore[arg-type]
