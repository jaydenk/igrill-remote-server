"""BLE device manager — scans for iGrill devices and spawns workers.

Extracted from the monolithic ``main.py``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict

from bleak import BleakScanner

from service.ble.device_worker import DeviceWorker
from service.models.device import DeviceStore
from service.history.store import HistoryStore, now_iso
from service.alerts.evaluator import AlertEvaluator

LOG = logging.getLogger("igrill.ble")


class DeviceManager:
    """Discovers iGrill BLE devices via periodic scanning and creates a
    :class:`DeviceWorker` for each new device that matches the configured
    MAC-address prefix."""

    def __init__(
        self,
        store: DeviceStore,
        history: HistoryStore,
        poll_interval: int,
        timeout: int,
        mac_prefix: str,
        scan_interval: int,
        scan_timeout: int,
        evaluator: AlertEvaluator,
        connect_timeout: int = 10,
        max_backoff: float = 60.0,
    ) -> None:
        self.store = store
        self.history = history
        self.poll_interval = poll_interval
        self.timeout = timeout
        self.mac_prefix = mac_prefix.lower()
        self.scan_interval = scan_interval
        self.scan_timeout = scan_timeout
        self._evaluator = evaluator
        self._connect_timeout = connect_timeout
        self._max_backoff = max_backoff
        self._workers: Dict[str, DeviceWorker] = {}
        self._tasks: Dict[str, asyncio.Task] = {}

    async def scan_loop(self) -> None:
        while True:
            try:
                devices = await BleakScanner.discover(timeout=self.scan_timeout, return_adv=True)
                scan_items = []
                if isinstance(devices, dict):
                    for key, value in devices.items():
                        device = None
                        adv_data = None
                        if isinstance(value, tuple):
                            device = value[0]
                            adv_data = value[1] if len(value) > 1 else None
                        elif hasattr(value, "address"):
                            device = value
                        else:
                            adv_data = value
                        address = getattr(device, "address", None) or (key if isinstance(key, str) else None)
                        name = getattr(device, "name", None) or (getattr(adv_data, "local_name", None) if adv_data else None)
                        rssi = getattr(adv_data, "rssi", None) if adv_data else None
                        if address:
                            scan_items.append((address, name, rssi))
                else:
                    for entry in devices:
                        device = None
                        adv_data = None
                        address = None
                        name = None
                        rssi = None
                        if isinstance(entry, tuple):
                            device = entry[0]
                            adv_data = entry[1] if len(entry) > 1 else None
                            address = getattr(device, "address", None)
                            name = getattr(device, "name", None) or (getattr(adv_data, "local_name", None) if adv_data else None)
                            rssi = getattr(adv_data, "rssi", None) if adv_data else None
                        elif hasattr(entry, "address"):
                            device = entry
                            address = getattr(device, "address", None)
                            name = getattr(device, "name", None)
                        elif isinstance(entry, str):
                            address = entry
                        if address:
                            scan_items.append((address, name, rssi))
                for address, name, rssi in scan_items:
                    if not address.lower().startswith(self.mac_prefix):
                        continue
                    LOG.debug("Discovered %s (%s) rssi=%s", address, name, rssi)
                    await self.store.upsert(
                        address,
                        name=name,
                        last_seen=now_iso(),
                        rssi=rssi,
                    )
                    if address not in self._workers:
                        worker = DeviceWorker(
                            address,
                            name,
                            self.store,
                            self.history,
                            self.poll_interval,
                            self.timeout,
                            self._evaluator,
                            connect_timeout=self._connect_timeout,
                            max_backoff=self._max_backoff,
                        )
                        self._workers[address] = worker
                        self._tasks[address] = asyncio.create_task(worker.run())
                    else:
                        self._workers[address].update_name(name)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning("Scan error: %s", exc)

            # Check worker health
            dead_workers = []
            for address, task in list(self._tasks.items()):
                if task.done():
                    exc = task.exception() if not task.cancelled() else None
                    if exc:
                        LOG.warning("worker_died address=%s error=%s", address, exc)
                    else:
                        LOG.info("worker_stopped address=%s", address)
                    dead_workers.append(address)

            for address in dead_workers:
                LOG.info("worker_respawn address=%s", address)
                worker = self._workers[address]
                await self.store.upsert(address, connected=False, error="worker_crashed")
                self._tasks[address] = asyncio.create_task(worker.run())

            await asyncio.sleep(self.scan_interval)

    async def stop(self) -> None:
        for worker in self._workers.values():
            await worker.stop()
        for task in self._tasks.values():
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
