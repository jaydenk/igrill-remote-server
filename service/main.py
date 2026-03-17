"""iGrill Remote Server — app factory and entry point."""

import asyncio
import logging
import signal
import time

from aiohttp import web

from service.config import Config
from service.logging_setup import setup_logging
from service.metrics import MetricsRegistry
from service.models.device import DeviceStore
from service.history.store import HistoryStore
from service.ble.device_manager import DeviceManager
from service.alerts.evaluator import AlertEvaluator
from service.api.routes import setup_routes
from service.api.websocket import WebSocketHub, broadcast_readings, broadcast_events
from service.web.dashboard import setup_dashboard

LOG = logging.getLogger("igrill")


def create_app(config: Config) -> web.Application:
    """Build a fully-wired :class:`aiohttp.web.Application`."""
    app = web.Application()
    store = DeviceStore()
    history = HistoryStore(config.db_path, config.reconnect_grace)
    evaluator = AlertEvaluator()
    hub = WebSocketHub()
    metrics = MetricsRegistry()

    app["config"] = config
    app["store"] = store
    app["history"] = history
    app["evaluator"] = evaluator
    app["hub"] = hub
    app["metrics"] = metrics
    app["start_time"] = time.monotonic()

    setup_routes(app)
    setup_dashboard(app)

    return app


async def run() -> None:
    """Start the server, BLE scanner, and broadcast loops."""
    config = Config.from_env()

    setup_logging(config)

    app = create_app(config)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.bind_address, config.port)
    await site.start()
    LOG.info("Server listening on %s:%d", config.bind_address, config.port)

    manager = DeviceManager(
        store=app["store"],
        history=app["history"],
        poll_interval=config.poll_interval,
        timeout=config.timeout,
        mac_prefix=config.mac_prefix,
        scan_interval=config.scan_interval,
        scan_timeout=config.scan_timeout,
        evaluator=app["evaluator"],
        connect_timeout=config.connect_timeout,
        max_backoff=config.max_backoff,
    )

    tasks = [
        asyncio.create_task(broadcast_readings(app)),
        asyncio.create_task(broadcast_events(app)),
        asyncio.create_task(manager.scan_loop()),
    ]

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    await stop.wait()
    LOG.info("Shutting down...")

    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await manager.stop()

    for client in list(app["hub"].clients):
        await app["hub"].remove(client)

    await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(run())
