"""Temperature curve generators for simulated cook sessions."""

import math
import random


def fixed_probe_temp(
    tick: int,
    target: float,
    start: float = 25.0,
    k: float = 0.02,
    noise: float = 1.5,
) -> float:
    """Logarithmic approach to a fixed target with random noise.

    T(t) = target - (target - start) * e^(-k*t) + noise
    """
    base = target - (target - start) * math.exp(-k * tick)
    if noise > 0:
        base += random.uniform(-noise, noise)
    return round(base, 1)


def range_probe_temp(
    tick: int,
    range_low: float,
    range_high: float,
    start: float = 25.0,
    overshoot: float = 135.0,
    noise: float = 5.0,
) -> float:
    """Ramp-overshoot-settle curve for a range-target probe.

    Phase 1 (ramp): linear rise toward overshoot.
    Phase 2 (settle): exponential decay to range midpoint.
    Phase 3 (steady): hold at midpoint with noise.
    """
    midpoint = (range_low + range_high) / 2.0
    ramp_rate = 2.0  # degrees per tick
    ramp_ticks = int((overshoot - start) / ramp_rate)

    if tick < ramp_ticks:
        # Phase 1: linear ramp
        base = start + ramp_rate * tick
    else:
        # Phase 2/3: exponential decay from overshoot to midpoint
        decay_tick = tick - ramp_ticks
        k = 0.03
        base = midpoint + (overshoot - midpoint) * math.exp(-k * decay_tick)

    if noise > 0 and tick > 0:
        base += random.uniform(-noise, noise)
    return round(base, 1)
