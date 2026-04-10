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

from service.ble.connection_state import ConnectionState, ConnectionStateMachine
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
from service.api.envelope import make_envelope
from service.models.device import DeviceStore
from service.models.reading import (
    build_reading_payload,
    parse_pulse_element,
    parse_temperature_probe,
)
from service.history.store import HistoryStore, now_iso_utc
from service.alerts.evaluator import AlertEvaluator

LOG = logging.getLogger("igrill.ble")

_AUTH_MAX_RETRIES = 3


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
        connect_timeout: int = 10,
        max_backoff: float = 60.0,
    ) -> None:
        self.address = address
        self.name = name
        self.store = store
        self.history = history
        self.poll_interval = poll_interval
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self._evaluator = evaluator
        self._model: Optional[ModelInfo] = None
        self._stop = asyncio.Event()
        self._connected_logged = False
        self._seq = 0
        # Per-connection disconnect signal — set from BleakClient's
        # disconnected_callback so the poll loop can exit its sleep early
        # instead of waiting out the full poll_interval after a BLE drop.
        self._disconnect_event: Optional[asyncio.Event] = None
        # Probes that previously returned ATT "Unlikely Error" — treated as
        # permanently unplugged for this connection to avoid log spam and
        # wasted GATT reads on empty sockets.
        self._known_unplugged: set[str] = set()
        self._state_machine = ConnectionStateMachine(
            max_backoff=max_backoff,
            on_change=self._on_state_change,
        )

    # -- Public interface ---------------------------------------------------

    @property
    def connection_state(self) -> ConnectionState:
        """Expose the current connection state for external inspection."""
        return self._state_machine.state

    def update_name(self, name: Optional[str]) -> None:
        if name:
            self.name = name

    async def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                self._state_machine.transition(ConnectionState.CONNECTING)
                LOG.debug("Connecting to %s (%s)", self.address, self.name or "unknown")
                # Fresh disconnect signal for each connection attempt. Must be
                # created here (not in __init__) so a prior disconnect does
                # not leave the event in a set state for the next loop.
                self._disconnect_event = asyncio.Event()
                # Reset per-connection probe-unplugged cache — the user may
                # have plugged/unplugged probes while we were disconnected.
                self._known_unplugged.clear()
                async with BleakClient(
                    self.address,
                    timeout=self.connect_timeout,
                    disconnected_callback=self._on_ble_disconnected,
                ) as client:
                    self._connected_logged = False
                    services = client.services
                    if services is None:
                        with warnings.catch_warnings():
                            warnings.simplefilter("ignore", category=FutureWarning)
                            services = await client.get_services()
                    if services is None:
                        LOG.warning("No services discovered for %s", self.address)
                        await self.store.upsert(self.address, connected=False, error="services_unavailable")
                        self._state_machine.transition(ConnectionState.DISCONNECTED)
                        self._state_machine.transition(ConnectionState.BACKOFF)
                        await self._publish_state_change_event()
                        await asyncio.sleep(self._state_machine.backoff_seconds)
                        continue
                    self._model = detect_model(services)
                    await self._update_model_state()

                    self._state_machine.transition(ConnectionState.AUTHENTICATING)
                    await self._authenticate(client, services)

                    self._state_machine.transition(ConnectionState.POLLING)
                    await self._publish_state_change_event()

                    # If a session is active, mark this device as rejoined
                    session_id = await self.history.get_current_session_id()
                    if session_id is not None and await self.history.is_device_in_session(self.address):
                        await self.history.device_rejoined_session(session_id, self.address)

                    await self._poll_loop(client, services)

                # BleakClient context manager has exited — BLE handle released.
                # Now safe to do session cleanup and backoff sleep.
                session_id = await self.history.get_current_session_id()
                if session_id is not None and await self.history.is_device_in_session(self.address):
                    await self.history.device_left_session(session_id, self.address)

                await self._handle_disconnect()
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

                await self._handle_disconnect()

    # -- Internal helpers ---------------------------------------------------

    def _on_state_change(self, old_state: ConnectionState, new_state: ConnectionState) -> None:
        """Synchronous callback invoked on every state transition."""
        LOG.info(
            "state_change address=%s old=%s new=%s",
            self.address,
            old_state.value,
            new_state.value,
        )

    def _on_ble_disconnected(self, _client: BleakClient) -> None:
        """BleakClient disconnected callback — fired by bleak's internal
        BlueZ d-bus watcher the moment the peripheral drops the link. We
        set the per-connection event so the poll loop's interval sleep
        returns immediately instead of waiting out the full poll_interval."""
        LOG.info("ble_disconnected address=%s", self.address)
        event = self._disconnect_event
        if event is not None and not event.is_set():
            event.set()

    async def _publish_state_change_event(self) -> None:
        """Publish a device_state_change event to the store's event queue."""
        envelope = make_envelope(
            "device_state_change",
            {
                "address": self.address,
                "state": self._state_machine.state.value,
            },
        )
        await self.store.publish_event(envelope)

    async def _handle_disconnect(self) -> None:
        """Common disconnect handling: zero out readings, transition to
        DISCONNECTED -> BACKOFF, publish the event, and sleep for backoff."""
        # Zero out probe readings so stale data is not displayed
        await self.store.upsert(
            self.address,
            connected=False,
            probes=[],
            connected_probes=[],
            probe_status="no_probes_connected",
        )

        self._state_machine.transition(ConnectionState.DISCONNECTED)
        self._state_machine.transition(ConnectionState.BACKOFF)
        await self._publish_state_change_event()

        backoff = self._state_machine.backoff_seconds
        LOG.info(
            "Backing off %s for %.1fs before reconnect",
            self.address,
            backoff,
        )
        await asyncio.sleep(backoff)

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

        last_exc: Optional[Exception] = None
        for attempt in range(1, _AUTH_MAX_RETRIES + 1):
            try:
                LOG.debug(
                    "Sending auth challenge to %s (attempt %d/%d)",
                    self.address,
                    attempt,
                    _AUTH_MAX_RETRIES,
                )
                await client.write_gatt_char(APP_CHALLENGE_UUID, bytes(16), response=True)
                challenge = await client.read_gatt_char(DEVICE_CHALLENGE_UUID)
                LOG.debug("Received device challenge from %s: %s", self.address, challenge.hex())
                await client.write_gatt_char(DEVICE_RESPONSE_UUID, challenge, response=True)
                return  # success
            except Exception as exc:
                last_exc = exc
                LOG.warning(
                    "Auth attempt %d/%d failed for %s: %s",
                    attempt,
                    _AUTH_MAX_RETRIES,
                    self.address,
                    exc,
                )
                if attempt < _AUTH_MAX_RETRIES:
                    await asyncio.sleep(1)

        # All retries exhausted
        raise RuntimeError(
            f"Authentication failed after {_AUTH_MAX_RETRIES} attempts for {self.address}"
        ) from last_exc

    async def _poll_loop(self, client: BleakClient, services) -> None:  # type: ignore[override]
        probe_uuids: List[str] = []
        if self._model:
            probe_uuids = PROBE_TEMPERATURE_UUIDS[: self._model.probe_count]

        # Seed _seq from the database to avoid overwriting readings after a
        # worker crash/respawn (INSERT OR REPLACE keys on session+address+seq).
        session_id = await self.history.get_current_session_id()
        if session_id is not None:
            self._seq = await self.history.get_max_seq(session_id, self.address)
        prev_session_id = session_id

        # Separate counter for WebSocket broadcasts (always increments,
        # independent of DB seq which only advances during sessions).
        ws_seq = self._seq

        while client.is_connected and not self._stop.is_set():
            payload = await self._read_metrics(client, services, probe_uuids)

            # Get current session state (may be None if no session is active)
            session_state = await self.history.get_session_state()
            session_id = session_state.get("current_session_id")
            session_start_ts = session_state.get("current_session_start_ts")

            # Re-seed _seq when session changes to avoid conflicts
            if session_id != prev_session_id:
                if session_id is not None:
                    self._seq = await self.history.get_max_seq(session_id, self.address)
                prev_session_id = session_id

            # Always update device store with latest readings (live dashboard)
            await self.store.upsert(
                self.address,
                session_id=session_id,
                session_start_ts=session_start_ts,
                **payload,
            )

            # Always publish readings to WebSocket (live dashboard works without session)
            ws_seq += 1
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
                    "seq": ws_seq,
                    "payload": reading_payload,
                }
            )

            # Only record to DB and evaluate alerts when a session is active
            # and this device is part of it
            if session_id is not None and await self.history.is_device_in_session(self.address):
                self._seq += 1
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
                        envelope = make_envelope(alert_evt["type"], alert_evt["payload"])
                        await self.store.publish_event(envelope)

            # Sleep until next poll, OR wake immediately if the BLE link
            # drops. Without this race, a BLE disconnect during the sleep
            # is detected up to poll_interval seconds late — which looks
            # to clients like stale data followed by a long "connecting"
            # stall on the next iteration.
            await self._wait_until_next_poll()

    async def _read_metrics(
        self, client: BleakClient, services, probe_uuids: List[str]
    ) -> Dict[str, object]:
        payload: Dict[str, object] = {"last_update": now_iso_utc(), "connected": True, "error": None}

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
            payload["propane_percent"] = min(propane_data[0] * 25, 100)

        probes: List[Dict[str, object]] = []
        for index, uuid in enumerate(probe_uuids, start=1):
            if uuid in self._known_unplugged:
                # Probe socket is empty — emit a synthetic unplugged entry
                # so clients can still see the slot exists, but skip the
                # GATT read to avoid the ATT "Unlikely Error" spam.
                probes.append({
                    "index": index,
                    "temperature": None,
                    "raw": None,
                    "unplugged": True,
                })
                continue
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
        # Short-circuit if the BLE link already dropped earlier in this poll
        # cycle. Without this, every subsequent characteristic read in
        # _read_metrics hits "Not connected" and logs a WARNING, producing
        # a noisy burst of errors for what is just the normal disconnect path.
        if self._disconnect_event is not None and self._disconnect_event.is_set():
            return None
        try:
            data = await asyncio.wait_for(client.read_gatt_char(uuid), timeout=self.timeout)
            LOG.debug("Read %s from %s: %s", uuid, self.address, data.hex())
            return data
        except asyncio.TimeoutError:
            LOG.warning("Timeout reading %s from %s (transient)", uuid, self.address)
            return None
        except (BrokenPipeError, ConnectionError, OSError) as exc:
            LOG.warning("Connection error reading %s from %s: %s", uuid, self.address, exc)
            raise
        except Exception as exc:
            exc_text = str(exc)
            # iGrill V2/V202 returns ATT "Unlikely Error" (0x0e) when
            # reading a probe characteristic whose physical socket is
            # empty, instead of the documented UNPLUGGED_PROBE_CONSTANT
            # sentinel value. Treat this as a known "unplugged" signal
            # so we can skip it on subsequent polls without spamming the
            # log every 15 seconds.
            if "Unlikely Error" in exc_text and uuid in PROBE_TEMPERATURE_UUIDS:
                self._known_unplugged.add(uuid)
                LOG.debug(
                    "Probe %s on %s returned Unlikely Error — treating as unplugged",
                    uuid,
                    self.address,
                )
                return None
            # If the BLE link dropped mid-read, the disconnect callback
            # has already set the event by the time we catch the exception.
            # Treat every error in that window as a benign disconnect
            # symptom rather than a real read failure — they come in many
            # flavours ("Not connected", empty-message BleakDBusError,
            # "org.bluez.Error.Failed", etc.) and we don't want to spam
            # a WARNING for each one. The poll loop will exit on the
            # next iteration via the client.is_connected check.
            if (
                self._disconnect_event is not None
                and self._disconnect_event.is_set()
            ):
                LOG.debug(
                    "Read %s from %s aborted by disconnect: %s",
                    uuid,
                    self.address,
                    exc_text or type(exc).__name__,
                )
                return None
            # BlueZ/bleak surfaces "Not connected" even when our own
            # disconnect event hasn't fired yet (e.g. BlueZ lost the
            # link before bleak registered it). Still demote to DEBUG.
            if "Not connected" in exc_text or "org.bluez.Error.NotConnected" in exc_text:
                LOG.debug(
                    "Read %s from %s skipped — bluez reports not connected",
                    uuid,
                    self.address,
                )
                return None
            LOG.warning("Read error %s from %s: %s", uuid, self.address, exc)
            return None

    async def _wait_until_next_poll(self) -> None:
        """Sleep for ``poll_interval`` seconds OR return immediately if the
        BLE link drops. Uses the per-connection disconnect event signalled
        by :meth:`_on_ble_disconnected`. On normal timeout this is a no-op
        and the outer loop's ``client.is_connected`` check proceeds."""
        event = self._disconnect_event
        if event is None:
            # Defensive fallback — should never happen during a live poll,
            # but avoids blowing up if _poll_loop is ever called outside a
            # run() iteration in a test or future refactor.
            await asyncio.sleep(self.poll_interval)
            return
        try:
            await asyncio.wait_for(event.wait(), timeout=self.poll_interval)
        except asyncio.TimeoutError:
            pass  # Normal poll interval elapsed; continue polling.
