"""BLE device worker — maintains a connection to a single iGrill device and
polls it for temperature, battery, and propane readings on a fixed interval.

Extracted from the monolithic ``main.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import warnings
from typing import Any, Dict, List, Optional

from bleak import BleakClient

from service.ble.protocol import (
    APP_CHALLENGE_UUID,
    BATTERY_LEVEL_UUID,
    DEVICE_CHALLENGE_UUID,
    DEVICE_RESPONSE_UUID,
    PROBE_TEMPERATURE_UUIDS,
    PROPANE_LEVEL_UUID,
    PULSE_ELEMENT_UUID,
    TEMPERATURE_UNIT_UUID,
    ModelInfo,
    detect_model,
)
from service.models.device import DeviceStore
from service.models.reading import (
    build_reading_payload,
    parse_pulse_element,
    parse_temperature_probe,
)
from service.history.store import HistoryStore, now_iso, now_iso_utc
from service.alerts.evaluator import AlertEvaluator

LOG = logging.getLogger("igrill")


# ---------------------------------------------------------------------------
# Envelope helper (local, until the full envelope module arrives in Task 7)
# ---------------------------------------------------------------------------

def _make_envelope(
    msg_type: str,
    payload: Dict[str, object],
    seq: Optional[int] = None,
) -> Dict[str, object]:
    """Build a v2 event envelope."""
    env: Dict[str, object] = {
        "v": 2,
        "type": msg_type,
        "ts": now_iso_utc(),
        "payload": payload,
    }
    if seq is not None:
        env["seq"] = seq
    return env


# ---------------------------------------------------------------------------
# DeviceWorker
# ---------------------------------------------------------------------------


class DeviceWorker:
    """Manages the full lifecycle for a single BLE iGrill device: connect,
    authenticate, poll readings, and reconnect on failure."""

    def __init__(
        self,
        address: str,
        name: Optional[str],
        store: DeviceStore,
        history: HistoryStore,
        poll_interval: int,
        timeout: int,
        evaluator: AlertEvaluator,
    ) -> None:
        self.address = address
        self.name = name
        self.store = store
        self.history = history
        self.poll_interval = poll_interval
        self.timeout = timeout
        self._evaluator = evaluator
        self._model: Optional[ModelInfo] = None
        self._stop = asyncio.Event()
        self._connected_logged = False
        self._seq = 0

    # -- Public interface ---------------------------------------------------

    def update_name(self, name: Optional[str]) -> None:
        if name:
            self.name = name

    async def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                LOG.debug("Connecting to %s (%s)", self.address, self.name or "unknown")
                async with BleakClient(self.address, timeout=self.timeout) as client:
                    self._connected_logged = False
                    services = client.services
                    if services is None:
                        with warnings.catch_warnings():
                            warnings.simplefilter("ignore", category=FutureWarning)
                            services = await client.get_services()
                    if services is None:
                        LOG.warning("No services discovered for %s", self.address)
                        await self.store.upsert(self.address, connected=False, error="services_unavailable")
                        await asyncio.sleep(3)
                        continue
                    self._model = detect_model(services)
                    await self._update_model_state()
                    await self._authenticate(client, services)

                    # If a session is active, mark this device as rejoined
                    session_id = await self.history.get_current_session_id()
                    if session_id is not None and await self.history.is_device_in_session(self.address):
                        await self.history.device_rejoined_session(session_id, self.address)

                    await self._poll_loop(client, services)

                    # On disconnect: mark device as left if session is active
                    session_id = await self.history.get_current_session_id()
                    if session_id is not None and await self.history.is_device_in_session(self.address):
                        await self.history.device_left_session(session_id, self.address)

                    await self.history.note_disconnect(self.address, now_iso_utc())
                    await self.store.upsert(self.address, connected=False)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning("Device %s error: %s", self.address, exc)
                await self.store.upsert(
                    self.address,
                    connected=False,
                    error=str(exc),
                )

                # On error disconnect: mark device as left if session is active
                session_id = await self.history.get_current_session_id()
                if session_id is not None:
                    try:
                        if await self.history.is_device_in_session(self.address):
                            await self.history.device_left_session(session_id, self.address)
                    except Exception:
                        LOG.debug("Failed to mark device %s as left on error", self.address)

                await self.history.note_disconnect(self.address, now_iso_utc())
                await asyncio.sleep(3)

    # -- Internal helpers ---------------------------------------------------

    async def _update_model_state(self) -> None:
        if self._model:
            await self.store.upsert(
                self.address,
                connected=True,
                model=self._model.model_id,
                model_name=self._model.label,
                error=None,
            )
        else:
            await self.store.upsert(
                self.address,
                connected=True,
                model="unknown",
                model_name="Unknown",
                error=None,
            )

    async def _authenticate(self, client: BleakClient, services) -> None:  # type: ignore[override]
        if (
            services.get_characteristic(APP_CHALLENGE_UUID) is None
            or services.get_characteristic(DEVICE_CHALLENGE_UUID) is None
            or services.get_characteristic(DEVICE_RESPONSE_UUID) is None
        ):
            LOG.warning("Device %s missing authentication characteristics", self.address)
            return
        LOG.debug("Sending auth challenge to %s", self.address)
        await client.write_gatt_char(APP_CHALLENGE_UUID, bytes(16), response=True)
        challenge = await client.read_gatt_char(DEVICE_CHALLENGE_UUID)
        LOG.debug("Received device challenge from %s: %s", self.address, challenge.hex())
        await client.write_gatt_char(DEVICE_RESPONSE_UUID, challenge, response=True)

    async def _poll_loop(self, client: BleakClient, services) -> None:  # type: ignore[override]
        probe_uuids: List[str] = []
        if self._model:
            probe_uuids = PROBE_TEMPERATURE_UUIDS[: self._model.probe_count]
        while client.is_connected and not self._stop.is_set():
            payload = await self._read_metrics(client, services, probe_uuids)

            # Get current session state (may be None if no session is active)
            session_state = await self.history.get_session_state()
            session_id = session_state.get("current_session_id")
            session_start_ts = session_state.get("current_session_start_ts")

            # Always update device store with latest readings (live dashboard)
            await self.store.upsert(
                self.address,
                session_id=session_id,
                session_start_ts=session_start_ts,
                **payload,
            )

            # Always publish readings to WebSocket (live dashboard works without session)
            self._seq += 1
            device_entry = await self.store.get_device(self.address)
            if device_entry is None:
                await asyncio.sleep(self.poll_interval)
                continue
            reading_payload = build_reading_payload(
                device_entry,
                session_id=session_id,
                session_start_ts=session_start_ts,
            )
            await self.store.publish_reading(
                {
                    "seq": self._seq,
                    "payload": reading_payload,
                }
            )

            # Only record to DB and evaluate alerts when a session is active
            # and this device is part of it
            if session_id is not None and await self.history.is_device_in_session(self.address):
                probes: List[Dict[str, Any]] = payload.get("probes", [])  # type: ignore[assignment]
                await self.history.record_reading(
                    session_id=session_id,
                    address=self.address,
                    seq=self._seq,
                    probes=probes,
                    battery=payload.get("battery_percent"),  # type: ignore[arg-type]
                    propane=payload.get("propane_percent"),  # type: ignore[arg-type]
                    heating=payload.get("pulse"),  # type: ignore[arg-type]
                )

                # Evaluate alert targets and publish any resulting events
                if probes:
                    alert_events = self._evaluator.evaluate(session_id, probes, self.address)
                    for alert_evt in alert_events:
                        envelope = _make_envelope(alert_evt["type"], alert_evt["payload"])
                        await self.store.publish_event(envelope)

            await asyncio.sleep(self.poll_interval)

    async def _read_metrics(
        self, client: BleakClient, services, probe_uuids: List[str]
    ) -> Dict[str, object]:
        payload: Dict[str, object] = {"last_update": now_iso(), "connected": True, "error": None}

        unit_data = await self._read_char(client, TEMPERATURE_UNIT_UUID, services)
        if unit_data:
            raw_unit = unit_data[0]
            LOG.debug("Unit characteristic raw byte: %d for %s", raw_unit, self.address)
        # iGrill probe temperature values are always reported in Celsius by the
        # BLE hardware, regardless of the unit characteristic. The unit byte only
        # reflects the display preference set on the physical device, not the
        # encoding of the temperature data.
        payload["unit"] = "C"

        battery_data = await self._read_char(client, BATTERY_LEVEL_UUID, services)
        if battery_data:
            payload["battery_percent"] = battery_data[0]

        propane_data = await self._read_char(client, PROPANE_LEVEL_UUID, services)
        if propane_data:
            payload["propane_percent"] = propane_data[0] * 25

        probes: List[Dict[str, object]] = []
        for index, uuid in enumerate(probe_uuids, start=1):
            probe_data = await self._read_char(client, uuid, services)
            if not probe_data:
                continue
            probe = parse_temperature_probe(index, probe_data)
            probes.append(probe)
        payload["probes"] = probes
        connected_probes = [probe["index"] for probe in probes if probe.get("unplugged") is False]
        payload["connected_probes"] = connected_probes
        payload["probe_status"] = "probes_connected" if connected_probes else "no_probes_connected"
        device_label = self.name or (self._model.label if self._model else "unknown")
        if not self._connected_logged:
            LOG.info(
                "%s mac_address: %s connected_probes: %s",
                device_label,
                self.address,
                json.dumps(connected_probes),
            )
            self._connected_logged = True
        LOG.info(
            "%s mac_address: %s last_update: %s probes: %s",
            device_label,
            self.address,
            payload["last_update"],
            json.dumps(probes),
        )
        if probes:
            LOG.debug(
                "%s mac_address: %s last_update: %s probes: %s",
                device_label,
                self.address,
                payload["last_update"],
                json.dumps(probes),
            )

        if self._model and self._model.is_pulse:
            pulse_data = await self._read_char(client, PULSE_ELEMENT_UUID, services)
            if pulse_data:
                pulse = parse_pulse_element(pulse_data)
                payload["pulse"] = pulse
        return payload

    async def _read_char(self, client: BleakClient, uuid: str, services) -> Optional[bytes]:
        if services.get_characteristic(uuid) is None:
            return None
        try:
            data = await asyncio.wait_for(client.read_gatt_char(uuid), timeout=self.timeout)
            LOG.debug("Read %s from %s: %s", uuid, self.address, data.hex())
            return data
        except asyncio.TimeoutError:
            LOG.warning("Timeout reading %s from %s", uuid, self.address)
            return None
        except Exception as exc:
            LOG.warning("Read error %s from %s: %s", uuid, self.address, exc)
            return None
