import pytest
from service.simulate.curves import fixed_probe_temp, range_probe_temp

class TestFixedProbeTemp:
    def test_starts_at_ambient(self):
        assert fixed_probe_temp(tick=0, target=90, start=25, k=0.02, noise=0) == 25.0

    def test_approaches_target(self):
        temp = fixed_probe_temp(tick=200, target=90, start=25, k=0.02, noise=0)
        assert 85 < temp < 91

    def test_noise_varies_output(self):
        temps = {fixed_probe_temp(tick=50, target=90, start=25, k=0.02, noise=2) for _ in range(20)}
        assert len(temps) > 1  # noise produces different values

class TestRangeProbeTemp:
    def test_starts_at_ambient(self):
        assert range_probe_temp(tick=0, range_low=110, range_high=130, start=25, overshoot=135, noise=0) == 25.0

    def test_overshoots_then_settles(self):
        # Should overshoot above range_high
        peak_temp = max(
            range_probe_temp(tick=t, range_low=110, range_high=130, start=25, overshoot=135, noise=0)
            for t in range(100)
        )
        assert peak_temp > 130

        # Should settle within range eventually
        late_temp = range_probe_temp(tick=300, range_low=110, range_high=130, start=25, overshoot=135, noise=0)
        assert 108 < late_temp < 132  # within range ± small margin
