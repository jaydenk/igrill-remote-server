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

# (label, mode, target_value, range_low, range_high, k, noise, start_delay_ticks)
_PROBE_CONFIGS = [
    ("BBQ Temp", "range", None, 110.0, 130.0, None, 5.0, 0),
    ("Brisket", "fixed", 90.0, None, None, 0.008, 1.5, 120),
    ("Ribs", "fixed", 80.0, None, None, 0.009, 1.5, 120),
    ("Pork Belly", "fixed", 75.0, None, None, 0.010, 1.0, 120),
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

    async def start(
        self,
        speed: float = 10,
        probes: int = 4,
        probe_timers: Optional[dict[int, dict[str, Any]]] = None,
    ) -> dict[str, Any]:
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

        # Broadcast session events so all connected clients (iOS, web) are notified
        if session_info.get("end_event"):
            await self.store.publish_event(
                make_envelope("session_end", session_info["end_event"])
            )
        await self.store.publish_event(
            make_envelope("session_start", session_info["start_event"])
        )

        # Register targets
        targets = self._build_targets(probes)
        await self._history.save_targets(self._session_id, SIM_ADDRESS, targets)
        self._evaluator.set_targets(self._session_id, targets)

        # Optional pre-attached timers — exercised by tests and sim clients
        # that need the per-probe-timer paths without real hardware.
        if probe_timers:
            for probe_index, spec in probe_timers.items():
                mode = spec.get("mode")
                duration_secs = spec.get("duration_secs")
                if mode not in ("count_up", "count_down"):
                    continue
                if mode == "count_down" and duration_secs is None:
                    continue
                await self._history.upsert_timer(
                    self._session_id,
                    SIM_ADDRESS,
                    int(probe_index),
                    mode,
                    duration_secs,
                )

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

        # End session and broadcast event
        end_result = await self._history.end_session(reason="simulation_stopped")
        if end_result:
            await self.store.publish_event(
                make_envelope("session_end", end_result)
            )

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
            label, mode, target_val, r_low, r_high, _, _, _ = _PROBE_CONFIGS[i]
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

            # If the current session is no longer our simulation's own
            # session — e.g. a real client started a cook without first
            # stopping the simulator — stop here. Otherwise the
            # simulator would write synthetic probe data into the real
            # cook's history, corrupting it.
            if self._session_id is not None and session_id != self._session_id:
                LOG.info(
                    "Simulation session %s diverged from current %s — stopping",
                    self._session_id, session_id,
                )
                return

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
