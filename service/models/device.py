"""In-memory device state store with async-safe access and message queues."""

import asyncio
from typing import Dict, Optional


class DeviceStore:
    """Thread-safe in-memory store for discovered iGrill device state.

    Each device is keyed by its BLE MAC address and carries the full set of
    fields that the rest of the system queries (connection status, probe
    readings, battery level, etc.).

    Two bounded queues (`_reading_queue` and `_event_queue`) allow producers
    (BLE workers) and consumers (WebSocket broadcasters) to communicate
    without tight coupling.
    """

    _DEFAULT_FIELDS: Dict[str, object] = {
        "name": None,
        "model": None,
        "model_name": None,
        "connected": False,
        "session_id": None,
        "session_start_ts": None,
        "last_seen": None,
        "last_update": None,
        "unit": None,
        "battery_percent": None,
        "propane_percent": None,
        "probes": [],
        "pulse": {},
        "connected_probes": [],
        "probe_status": "unknown",
        "error": None,
        "rssi": None,
    }

    def __init__(self) -> None:
        self._devices: Dict[str, Dict[str, object]] = {}
        self._lock = asyncio.Lock()
        self._reading_queue: asyncio.Queue[Dict[str, object]] = asyncio.Queue(
            maxsize=1000
        )
        self._event_queue: asyncio.Queue[Dict[str, object]] = asyncio.Queue(
            maxsize=1000
        )

    # ------------------------------------------------------------------
    # Device CRUD
    # ------------------------------------------------------------------

    async def upsert(self, address: str, **fields: object) -> None:
        """Create or update the device entry for *address*.

        Unknown addresses are initialised with the default field set before
        the supplied *fields* are merged in.
        """
        async with self._lock:
            entry = self._devices.setdefault(
                address,
                {"address": address, **{k: _copy_default(v) for k, v in self._DEFAULT_FIELDS.items()}},
            )
            entry.update(fields)

    async def snapshot(self) -> Dict[str, Dict[str, object]]:
        """Return a shallow copy of every device entry, keyed by address."""
        async with self._lock:
            return {key: dict(value) for key, value in self._devices.items()}

    async def get_device(self, address: str) -> Optional[Dict[str, object]]:
        """Return a copy of a single device entry, or ``None``."""
        async with self._lock:
            if address not in self._devices:
                return None
            return dict(self._devices[address])

    # ------------------------------------------------------------------
    # Reading queue (BLE worker -> WebSocket broadcaster)
    # ------------------------------------------------------------------

    async def publish_reading(self, reading: Dict[str, object]) -> None:
        """Enqueue a reading, dropping the oldest if the queue is full."""
        if self._reading_queue.full():
            try:
                self._reading_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        self._reading_queue.put_nowait(reading)

    async def next_reading(self) -> Dict[str, object]:
        """Block until a reading is available and return it."""
        return await self._reading_queue.get()

    # ------------------------------------------------------------------
    # Event queue (session lifecycle -> WebSocket broadcaster)
    # ------------------------------------------------------------------

    async def publish_event(self, event: Dict[str, object]) -> None:
        """Enqueue an event, dropping the oldest if the queue is full."""
        if self._event_queue.full():
            try:
                self._event_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        self._event_queue.put_nowait(event)

    async def next_event(self) -> Dict[str, object]:
        """Block until an event is available and return it."""
        return await self._event_queue.get()


def _copy_default(value: object) -> object:
    """Return a fresh copy of mutable default values (lists / dicts)."""
    if isinstance(value, list):
        return list(value)
    if isinstance(value, dict):
        return dict(value)
    return value
