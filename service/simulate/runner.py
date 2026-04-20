"""Background task that generates simulated iGrill readings.

The simulator presents a synthetic BLE device to the server. It registers
that device and broadcasts probe readings continuously, exactly the way a
real iGrill would. It does **not** start, end, or otherwise touch cook
sessions — users drive sessions through the normal Start Session flow,
whether the underlying device is real or simulated.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from service.alerts.evaluator import AlertEvaluator
from service.api.envelope import make_envelope
from service.history.store import HistoryStore, now_iso_utc
from service.models.device import DeviceStore
from service.models.reading import build_reading_payload
from service.simulate.curves import fixed_probe_temp, range_probe_temp

LOG = logging.getLogger("igrill.simulate")

SIM_ADDRESS = "SIM:UL:AT:ED:00:01"
SIM_NAME = "Simulated iGrill V2"
SIM_MODEL = "igrill_v2"
SIM_MODEL_NAME = "iGrill V2"

# (label, mode, target_value, range_low, range_high, k, noise, start_delay_ticks)
_PROBE_CONFIGS = [
    ("BBQ Temp", "range", None, 110.0, 130.0, None, 5.0, 0),
    ("Brisket", "fixed", 90.0, None, None, 0.008, 1.5, 120),
    ("Ribs", "fixed", 80.0, None, None, 0.009, 1.5, 120),
    ("Pork Belly", "fixed", 75.0, None, None, 0.010, 1.0, 120),
]


class SimulationRunner:
    """Generates simulated temperature readings in a background task.

    Behaves like a BLE device from the server's point of view: register,
    broadcast readings, disconnect on stop. Session lifecycle is handled
    by users through the normal session APIs — the same way it is for
    real devices.
    """

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
        self._tick = 0

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(
        self,
        speed: float = 10,
        probes: int = 4,
    ) -> dict[str, Any]:
        if self.is_running:
            return {"error": "simulation already running"}

        probes = max(1, min(4, probes))
        speed = max(0.1, speed)
        self._tick = 0

        # Register fake device — no session work here. The device appears
        # in the device list and starts broadcasting readings; any session
        # handling happens through the normal user-driven flow.
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

        # Publish a device-state change so web/iOS clients see the sim
        # device come online the same way a real BLE connect would.
        await self.store.publish_event(
            make_envelope(
                "device_state_change",
                {"address": SIM_ADDRESS, "state": "connected"},
            )
        )

        # Launch background reading loop
        interval = self._poll_interval / speed
        self._task = asyncio.create_task(self._reading_loop(interval, probes))

        LOG.info(
            "Simulation started: device=%s speed=%.1fx probes=%d interval=%.2fs",
            SIM_ADDRESS, speed, probes, interval,
        )

        return {
            "ok": True,
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

        ticks = self._tick

        # Mark device disconnected — leave any user-owned session alone.
        await self.store.upsert(SIM_ADDRESS, connected=False)
        await self.store.publish_event(
            make_envelope(
                "device_state_change",
                {"address": SIM_ADDRESS, "state": "disconnected"},
            )
        )

        LOG.info("Simulation stopped: readings=%d", ticks)

        return {"ok": True, "readings": ticks}

    async def _reading_loop(
        self,
        interval: float,
        probe_count: int,
    ) -> None:
        ws_seq = 0
        battery = 85.0

        while True:
            self._tick += 1
            ws_seq += 1
            battery = max(0, battery - 0.1)

            probes = self._generate_probes(self._tick, probe_count)
            connected_indices = [p["index"] for p in probes if not p["unplugged"]]

            await self.store.upsert(
                SIM_ADDRESS,
                last_update=now_iso_utc(),
                battery_percent=int(battery),
                probes=probes,
                connected_probes=connected_indices,
                probe_status="probes_connected" if connected_indices else "no_probes_connected",
                unit="C",
            )

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

            # Always publish — live probe temps show up in device cards
            # regardless of whether a cook session is active.
            await self.store.publish_reading({
                "seq": ws_seq,
                "payload": reading_payload,
            })

            # Only record into history and evaluate alerts when the user
            # has actually started a session that includes this device.
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

                if probes:
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

            label, mode, target_val, r_low, r_high, k, noise_amp, start_delay = _PROBE_CONFIGS[i]

            # Probe hasn't been "plugged in" yet
            if tick < start_delay:
                probes.append({
                    "index": i + 1,
                    "temperature": None,
                    "raw": 63536,
                    "unplugged": True,
                })
                continue

            effective_tick = tick - start_delay

            if mode == "fixed":
                temp = fixed_probe_temp(
                    tick=effective_tick, target=target_val, start=25.0, k=k, noise=noise_amp,
                )
            else:
                temp = range_probe_temp(
                    tick=effective_tick,
                    range_low=r_low,
                    range_high=r_high,
                    start=25.0,
                    overshoot=135.0,
                    noise=noise_amp,
                )

            probes.append({
                "index": i + 1,
                "temperature": temp,
                "raw": round(temp),
                "unplugged": False,
            })

        return probes
