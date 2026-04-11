# Simulated Cook Session Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:executing-plans to implement this plan task-by-task.

**Goal:** Add API endpoints to start/stop a simulated cook session that generates realistic temperature readings without BLE hardware, exercising the full WebSocket, history, alert, and push pipeline.

**Architecture:** A `SimulationRunner` class produces temperature readings in a background `asyncio.Task`, feeding them through the existing `DeviceStore.publish_reading()` and `HistoryStore.record_reading()` pipeline. Two HTTP endpoints (`/api/v1/simulate/start` and `/stop`) control the lifecycle. Temperature curves use exponential approach for fixed-target probes and a ramp-overshoot-settle pattern for the range probe.

**Tech Stack:** Python 3.11, aiohttp, asyncio, pytest/pytest-asyncio

---

### Task 1: Temperature Curve Module

**Files:**
- Create: `service/simulate/__init__.py`
- Create: `service/simulate/curves.py`
- Test: `tests/test_simulate_curves.py`

**Step 1: Write the failing tests**

```python
# tests/test_simulate_curves.py
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
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/kerrj/Documents/Development/iGrill\ Remote/iGrillRemoteServer && python -m pytest tests/test_simulate_curves.py -v`
Expected: FAIL — module not found

**Step 3: Implement the curves**

```python
# service/simulate/__init__.py
# (empty)
```

```python
# service/simulate/curves.py
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
```

**Step 4: Run tests to verify they pass**

Run: `cd /Users/kerrj/Documents/Development/iGrill\ Remote/iGrillRemoteServer && python -m pytest tests/test_simulate_curves.py -v`
Expected: PASS

**Step 5: Commit**

```bash
cd /Users/kerrj/Documents/Development/iGrill\ Remote/iGrillRemoteServer
git add service/simulate/__init__.py service/simulate/curves.py tests/test_simulate_curves.py
git commit -m "feat(server): add temperature curve generators for simulation"
```

---

### Task 2: SimulationRunner Core

**Files:**
- Create: `service/simulate/runner.py`
- Test: `tests/test_simulate_runner.py`

**Step 1: Write the failing tests**

```python
# tests/test_simulate_runner.py
import asyncio
import pytest
import pytest_asyncio
from service.models.device import DeviceStore
from service.history.store import HistoryStore
from service.alerts.evaluator import AlertEvaluator
from service.simulate.runner import SimulationRunner

SIM_ADDRESS = "SIM:UL:AT:ED:00:01"

@pytest_asyncio.fixture
async def history(tmp_path):
    s = HistoryStore(str(tmp_path / "test.db"), reconnect_grace=60)
    await s.connect()
    yield s
    await s.close()

@pytest.fixture
def runner(history):
    store = DeviceStore()
    evaluator = AlertEvaluator()
    return SimulationRunner(
        store=store,
        history=history,
        evaluator=evaluator,
        poll_interval=15,
    )

class TestSimulationRunner:
    @pytest.mark.asyncio
    async def test_not_running_initially(self, runner):
        assert not runner.is_running

    @pytest.mark.asyncio
    async def test_start_registers_device(self, runner):
        result = await runner.start(speed=100, probes=2)
        assert result["ok"] is True
        assert result["deviceAddress"] == SIM_ADDRESS
        assert result["probes"] == 2
        device = await runner.store.get_device(SIM_ADDRESS)
        assert device is not None
        assert device["connected"] is True
        assert device["name"] == "Simulated iGrill V2"
        await runner.stop()

    @pytest.mark.asyncio
    async def test_start_creates_session(self, runner):
        result = await runner.start(speed=100, probes=4)
        assert result["sessionId"] is not None
        await runner.stop()

    @pytest.mark.asyncio
    async def test_cannot_start_twice(self, runner):
        await runner.start(speed=100, probes=2)
        result = await runner.start(speed=100, probes=2)
        assert "error" in result
        await runner.stop()

    @pytest.mark.asyncio
    async def test_stop_ends_session(self, runner):
        await runner.start(speed=100, probes=2)
        result = await runner.stop()
        assert result["ok"] is True
        assert not runner.is_running
        device = await runner.store.get_device(SIM_ADDRESS)
        assert device["connected"] is False

    @pytest.mark.asyncio
    async def test_produces_readings(self, runner):
        await runner.start(speed=1000, probes=2)
        await asyncio.sleep(0.1)  # Let a few ticks fire
        await runner.stop()
        # Check that readings were published
        assert runner._tick > 0
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/kerrj/Documents/Development/iGrill\ Remote/iGrillRemoteServer && python -m pytest tests/test_simulate_runner.py -v`
Expected: FAIL — module not found

**Step 3: Implement SimulationRunner**

```python
# service/simulate/runner.py
"""Background task that generates simulated iGrill readings."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from service.alerts.evaluator import AlertEvaluator
from service.api.envelope import make_envelope
from service.history.store import HistoryStore, now_iso_utc
from service.models.device import DeviceStore
from service.models.reading import build_reading_payload
from service.models.session import TargetConfig
from service.simulate.curves import fixed_probe_temp, range_probe_temp

LOG = logging.getLogger("igrill.simulate")

SIM_ADDRESS = "SIM:UL:AT:ED:00:01"
SIM_NAME = "Simulated iGrill V2"
SIM_MODEL = "igrill_v2"
SIM_MODEL_NAME = "iGrill V2"

# Probe configurations: (label, mode, target_value, range_low, range_high, k, noise)
_PROBE_CONFIGS = [
    ("Brisket", "fixed", 90.0, None, None, 0.015, 1.5),
    ("Ribs", "fixed", 80.0, None, None, 0.020, 1.5),
    ("BBQ Temp", "range", None, 110.0, 130.0, None, 5.0),
    ("Pork Belly", "fixed", 75.0, None, None, 0.010, 1.0),
]


class SimulationRunner:
    """Generates simulated temperature readings in a background task."""

    def __init__(
        self,
        store: DeviceStore,
        history: HistoryStore,
        evaluator: AlertEvaluator,
        poll_interval: int = 15,
    ) -> None:
        self.store = store
        self._history = history
        self._evaluator = evaluator
        self._poll_interval = poll_interval
        self._task: Optional[asyncio.Task[None]] = None
        self._session_id: Optional[str] = None
        self._tick = 0

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self, speed: float = 10, probes: int = 4) -> dict[str, Any]:
        if self.is_running:
            return {"error": "simulation already running"}

        probes = max(1, min(4, probes))
        speed = max(0.1, speed)
        self._tick = 0

        # Register fake device
        await self.store.upsert(
            SIM_ADDRESS,
            name=SIM_NAME,
            model=SIM_MODEL,
            model_name=SIM_MODEL_NAME,
            connected=True,
            unit="C",
            battery_percent=85,
            last_seen=now_iso_utc(),
        )

        # Start session
        session_info = await self._history.start_session(
            addresses=[SIM_ADDRESS], reason="simulation", name="Simulated Cook",
        )
        self._session_id = session_info["session_id"]
        session_start_ts = session_info["session_start_ts"]

        # Register targets
        targets = self._build_targets(probes)
        await self._history.save_targets(self._session_id, SIM_ADDRESS, targets)
        self._evaluator.set_targets(self._session_id, targets)

        # Update device with session info
        await self.store.upsert(
            SIM_ADDRESS,
            session_id=self._session_id,
            session_start_ts=session_start_ts,
        )

        # Launch background reading loop
        interval = self._poll_interval / speed
        self._task = asyncio.create_task(
            self._reading_loop(interval, probes, targets)
        )

        LOG.info(
            "Simulation started: session=%s speed=%.1fx probes=%d interval=%.2fs",
            self._session_id, speed, probes, interval,
        )

        return {
            "ok": True,
            "sessionId": self._session_id,
            "deviceAddress": SIM_ADDRESS,
            "speed": speed,
            "probes": probes,
        }

    async def stop(self) -> dict[str, Any]:
        if not self.is_running:
            return {"error": "no simulation running"}

        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

        session_id = self._session_id
        ticks = self._tick

        # End session
        await self._history.end_session(reason="simulation_stopped")

        # Mark device disconnected
        await self.store.upsert(SIM_ADDRESS, connected=False)

        # Clean up evaluator
        if session_id:
            self._evaluator.clear_session(session_id)

        self._session_id = None

        LOG.info("Simulation stopped: session=%s readings=%d", session_id, ticks)

        return {"ok": True, "sessionId": session_id, "readings": ticks}

    def _build_targets(self, probe_count: int) -> list[TargetConfig]:
        targets = []
        for i in range(probe_count):
            label, mode, target_val, r_low, r_high, _, _ = _PROBE_CONFIGS[i]
            targets.append(TargetConfig(
                probe_index=i + 1,
                mode=mode,
                target_value=target_val,
                range_low=r_low,
                range_high=r_high,
                pre_alert_offset=5.0,
                reminder_interval_secs=180,
                label=label,
            ))
        return targets

    async def _reading_loop(
        self,
        interval: float,
        probe_count: int,
        targets: list[TargetConfig],
    ) -> None:
        ws_seq = 0
        battery = 85.0

        while True:
            self._tick += 1
            ws_seq += 1
            battery = max(0, battery - 0.1)

            probes = self._generate_probes(self._tick, probe_count)
            connected_indices = [p["index"] for p in probes if not p["unplugged"]]

            # Update device store
            await self.store.upsert(
                SIM_ADDRESS,
                last_update=now_iso_utc(),
                battery_percent=int(battery),
                probes=probes,
                connected_probes=connected_indices,
                probe_status="probes_connected" if connected_indices else "no_probes_connected",
                unit="C",
            )

            # Get device entry and build reading payload
            device_entry = await self.store.get_device(SIM_ADDRESS)
            if device_entry is None:
                break

            session_state = await self._history.get_session_state()
            session_id = session_state.get("current_session_id")
            session_start_ts = session_state.get("current_session_start_ts")

            reading_payload = build_reading_payload(
                device_entry,
                session_id=session_id,
                session_start_ts=session_start_ts,
            )

            # Publish to WebSocket
            await self.store.publish_reading({
                "seq": ws_seq,
                "payload": reading_payload,
            })

            # Record to history DB
            if session_id and await self._history.is_device_in_session(SIM_ADDRESS):
                next_seq = await self._history.get_max_seq(session_id, SIM_ADDRESS) + 1
                await self._history.record_reading(
                    session_id=session_id,
                    address=SIM_ADDRESS,
                    seq=next_seq,
                    probes=probes,
                    battery=int(battery),
                    propane=None,
                    heating=None,
                )

                # Evaluate alerts
                events = self._evaluator.evaluate(session_id, probes, SIM_ADDRESS)
                for event in events:
                    await self.store.publish_event(
                        make_envelope(event["type"], event["payload"])
                    )

            await asyncio.sleep(interval)

    def _generate_probes(self, tick: int, probe_count: int) -> list[dict[str, Any]]:
        probes: list[dict[str, Any]] = []
        for i in range(4):
            if i >= probe_count:
                probes.append({
                    "index": i + 1,
                    "temperature": None,
                    "raw": 63536,
                    "unplugged": True,
                })
                continue

            label, mode, target_val, r_low, r_high, k, noise_amp = _PROBE_CONFIGS[i]

            if mode == "fixed":
                temp = fixed_probe_temp(
                    tick=tick, target=target_val, start=25.0, k=k, noise=noise_amp,
                )
            else:
                temp = range_probe_temp(
                    tick=tick,
                    range_low=r_low,
                    range_high=r_high,
                    start=25.0,
                    overshoot=135.0,
                    noise=noise_amp,
                )

            probes.append({
                "index": i + 1,
                "temperature": temp,
                "raw": int(temp),
                "unplugged": False,
            })

        return probes
```

**Step 4: Run tests to verify they pass**

Run: `cd /Users/kerrj/Documents/Development/iGrill\ Remote/iGrillRemoteServer && python -m pytest tests/test_simulate_runner.py -v`
Expected: PASS

**Step 5: Commit**

```bash
cd /Users/kerrj/Documents/Development/iGrill\ Remote/iGrillRemoteServer
git add service/simulate/runner.py tests/test_simulate_runner.py
git commit -m "feat(server): add SimulationRunner for simulated cook sessions"
```

---

### Task 3: API Endpoints and Route Registration

**Files:**
- Modify: `service/api/routes.py`
- Modify: `service/main.py` (wire up SimulationRunner into the app)
- Test: `tests/test_simulate_api.py`

**Step 1: Write the failing tests**

```python
# tests/test_simulate_api.py
import pytest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop
from service.models.device import DeviceStore
from service.history.store import HistoryStore
from service.alerts.evaluator import AlertEvaluator
from service.simulate.runner import SimulationRunner


@pytest.fixture
async def client(aiohttp_client, tmp_path):
    """Create a minimal aiohttp app with simulation routes."""
    from service.api.routes import setup_routes

    app = web.Application()
    store = DeviceStore()
    history = HistoryStore(str(tmp_path / "test.db"), reconnect_grace=60)
    await history.connect()
    evaluator = AlertEvaluator()

    app["store"] = store
    app["history"] = history
    app["evaluator"] = evaluator
    app["config"] = type("Config", (), {"session_token": "", "poll_interval": 15})()
    app["simulator"] = SimulationRunner(
        store=store, history=history, evaluator=evaluator, poll_interval=15,
    )

    setup_routes(app)
    client = await aiohttp_client(app)
    yield client
    await history.close()


class TestSimulateAPI:
    @pytest.mark.asyncio
    async def test_start_simulation(self, client):
        resp = await client.post("/api/v1/simulate/start", json={"speed": 100, "probes": 2})
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True
        assert "sessionId" in data
        # Stop it
        await client.post("/api/v1/simulate/stop")

    @pytest.mark.asyncio
    async def test_start_twice_returns_error(self, client):
        await client.post("/api/v1/simulate/start", json={"speed": 100})
        resp = await client.post("/api/v1/simulate/start", json={"speed": 100})
        assert resp.status == 409
        await client.post("/api/v1/simulate/stop")

    @pytest.mark.asyncio
    async def test_stop_simulation(self, client):
        await client.post("/api/v1/simulate/start", json={"speed": 100})
        resp = await client.post("/api/v1/simulate/stop")
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True

    @pytest.mark.asyncio
    async def test_stop_without_start_returns_error(self, client):
        resp = await client.post("/api/v1/simulate/stop")
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_default_parameters(self, client):
        resp = await client.post("/api/v1/simulate/start")
        data = await resp.json()
        assert data["speed"] == 10
        assert data["probes"] == 4
        await client.post("/api/v1/simulate/stop")
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/kerrj/Documents/Development/iGrill\ Remote/iGrillRemoteServer && python -m pytest tests/test_simulate_api.py -v`
Expected: FAIL — routes not registered

**Step 3: Add route handlers and registration**

Add to `service/api/routes.py` (before `setup_routes`):

```python
async def simulate_start_handler(request: web.Request) -> web.Response:
    """POST /api/v1/simulate/start — start a simulated cook session."""
    from service.api.websocket import is_authorized
    if not is_authorized(request):
        return web.json_response({"error": "unauthorised"}, status=401)

    simulator = request.app.get("simulator")
    if not simulator:
        return web.json_response({"error": "simulator not available"}, status=503)

    try:
        body = await request.json()
    except Exception:
        body = {}

    speed = body.get("speed", 10)
    probes = body.get("probes", 4)

    try:
        speed = float(speed)
        probes = int(probes)
    except (TypeError, ValueError):
        return web.json_response({"error": "speed must be a number, probes must be an integer"}, status=400)

    result = await simulator.start(speed=speed, probes=probes)
    if "error" in result:
        return web.json_response(result, status=409)
    return web.json_response(result)


async def simulate_stop_handler(request: web.Request) -> web.Response:
    """POST /api/v1/simulate/stop — stop the running simulation."""
    from service.api.websocket import is_authorized
    if not is_authorized(request):
        return web.json_response({"error": "unauthorised"}, status=401)

    simulator = request.app.get("simulator")
    if not simulator:
        return web.json_response({"error": "simulator not available"}, status=503)

    result = await simulator.stop()
    if "error" in result:
        return web.json_response(result, status=400)
    return web.json_response(result)
```

Add to `setup_routes()`:

```python
app.router.add_post("/api/v1/simulate/start", simulate_start_handler)
app.router.add_post("/api/v1/simulate/stop", simulate_stop_handler)
```

**Step 4: Wire SimulationRunner into main.py**

In the `create_app()` function (or wherever the app is assembled), after `evaluator` is created, add:

```python
from service.simulate.runner import SimulationRunner
app["simulator"] = SimulationRunner(
    store=store, history=history, evaluator=evaluator,
    poll_interval=config.poll_interval,
)
```

**Step 5: Run tests to verify they pass**

Run: `cd /Users/kerrj/Documents/Development/iGrill\ Remote/iGrillRemoteServer && python -m pytest tests/test_simulate_api.py -v`
Expected: PASS

**Step 6: Run full test suite**

Run: `cd /Users/kerrj/Documents/Development/iGrill\ Remote/iGrillRemoteServer && python -m pytest -v`
Expected: All tests pass

**Step 7: Commit**

```bash
cd /Users/kerrj/Documents/Development/iGrill\ Remote/iGrillRemoteServer
git add service/api/routes.py service/main.py tests/test_simulate_api.py
git commit -m "feat(server): add /api/v1/simulate/start and /stop endpoints

API-triggered simulation that generates realistic temperature curves
without BLE hardware. Configurable speed and probe count."
```

---

### Task 4: Deploy and End-to-End Test

**Files:**
- No code changes — deployment and verification only.

**Step 1: Rebuild and deploy the Docker image**

```bash
cd /Users/kerrj/Documents/Development/iGrill\ Remote/iGrillRemoteServer
docker build -t ghcr.io/jaydenk/igrill-remote-server:latest .
docker push ghcr.io/jaydenk/igrill-remote-server:latest
ssh pimento.home.kerr.host "cd /home/kerrj/services/igrill-remote-server && docker compose pull && docker compose up -d"
```

**Step 2: Start a simulation**

```bash
curl -s -X POST https://igrill.pimento.home.kerr.host/api/v1/simulate/start \
  -H 'Content-Type: application/json' \
  -d '{"speed": 10, "probes": 4}' | python3 -m json.tool
```

Expected: `{"ok": true, "sessionId": "...", "deviceAddress": "SIM:UL:AT:ED:00:01", "speed": 10, "probes": 4}`

**Step 3: Verify on iOS app**

- Open the iOS app — should see "Simulated iGrill V2" in the device list.
- Tap it — probe cards should show rising temperatures.
- Chart should populate with live data.
- Alerts should fire as probes approach and reach targets.

**Step 4: Stop the simulation**

```bash
curl -s -X POST https://igrill.pimento.home.kerr.host/api/v1/simulate/stop | python3 -m json.tool
```

Expected: `{"ok": true, "sessionId": "...", "readings": N}`

**Step 5: Verify session in history**

- In the iOS app, open History — the simulated session should appear.
- Tap it — full temperature chart should render from stored history.
