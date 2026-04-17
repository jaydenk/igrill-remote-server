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
from service.history.store import HistoryStore, now_iso_utc
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
                # return_adv=True yields dict[BLEDevice, AdvertisementData]
                discovered = await BleakScanner.discover(
                    timeout=self.scan_timeout, return_adv=True,
                )
                scan_items = [
                    (
                        device.address,
                        device.name or getattr(adv, "local_name", None),
                        getattr(adv, "rssi", None),
                    )
                    for device, adv in discovered.values()
                ]
                matches = 0
                new_workers = 0
                for address, name, rssi in scan_items:
                    if not address.lower().startswith(self.mac_prefix):
                        continue
                    matches += 1
                    LOG.debug("Discovered %s (%s) rssi=%s", address, name, rssi)
                    await self.store.upsert(
                        address,
                        name=name,
                        last_seen=now_iso_utc(),
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
                        new_workers += 1
                    else:
                        self._workers[address].update_name(name)
                # Emit a single INFO-level summary per scan so operators can
                # see the scan loop is alive and confirm whether any iGrill
                # devices are advertising. Previously this ran silently,
                # making "no devices" vs "scan not running" indistinguishable.
                LOG.info(
                    "scan_complete total=%d matches=%d workers=%d new=%d",
                    len(scan_items),
                    matches,
                    len(self._workers),
                    new_workers,
                )
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
                old_worker = self._workers.pop(address)
                old_task = self._tasks.pop(address)
                # Drain the dead task. ``done()`` has already returned True so
                # this returns immediately; the await is here to surface any
                # exception that happened during final teardown (rather than
                # leaving an unhandled-exception warning to be GC-logged).
                try:
                    await old_task
                except Exception:
                    LOG.debug(
                        "worker_respawn drained exception for %s",
                        address, exc_info=True,
                    )
                await self.store.upsert(address, connected=False, error="worker_crashed")
                worker = DeviceWorker(
                    address,
                    old_worker.name,
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

            await asyncio.sleep(self.scan_interval)

    async def stop(self) -> None:
        for worker in self._workers.values():
            await worker.stop()
        for task in self._tasks.values():
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
