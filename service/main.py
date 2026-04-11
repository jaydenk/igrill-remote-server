"""iGrill Remote Server — app factory and entry point."""

import asyncio
import logging
import os
import signal
import time

from aiohttp import web

from service.config import Config
from service.logging_setup import setup_logging
from service.models.device import DeviceStore
from service.history.store import HistoryStore
from service.ble.device_manager import DeviceManager
from service.alerts.evaluator import AlertEvaluator
from service.api.routes import setup_routes
from service.api.websocket import WebSocketHub, broadcast_readings, broadcast_events
from service.push.service import PushService
from service.web.dashboard import setup_dashboard

LOG = logging.getLogger("igrill")

_CORS_ORIGIN = os.getenv("IGRILL_CORS_ORIGIN", "")


# ---------------------------------------------------------------------------
# CORS middleware (permissive for same-origin; configurable if needed)
# ---------------------------------------------------------------------------


@web.middleware
async def cors_middleware(request: web.Request, handler) -> web.StreamResponse:
    """Add CORS headers.  By default only the same origin is permitted;
    set ``IGRILL_CORS_ORIGIN`` to override (e.g. ``*`` for development)."""
    if request.method == "OPTIONS":
        response = web.Response()
    else:
        response = await handler(request)
    if _CORS_ORIGIN:
        response.headers["Access-Control-Allow-Origin"] = _CORS_ORIGIN
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    return response


def _warn_cors_wildcard() -> None:
    """Log a warning if CORS is configured with a wildcard origin."""
    if _CORS_ORIGIN == "*":
        LOG.warning(
            "IGRILL_CORS_ORIGIN is set to '*' — this allows requests from any "
            "origin and should only be used during development"
        )


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(config: Config) -> web.Application:
    """Build a fully-wired :class:`aiohttp.web.Application`."""
    app = web.Application(middlewares=[cors_middleware])
    store = DeviceStore()
    history = HistoryStore(config.db_path, config.reconnect_grace)
    evaluator = AlertEvaluator()
    hub = WebSocketHub()

    app["config"] = config
    app["store"] = store
    app["history"] = history
    app["evaluator"] = evaluator

    from service.simulate.runner import SimulationRunner
    app["simulator"] = SimulationRunner(
        store=store, history=history, evaluator=evaluator,
        poll_interval=config.poll_interval,
    )

    app["hub"] = hub
    app["start_time"] = time.monotonic()

    setup_routes(app)
    setup_dashboard(app)

    return app


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------


async def run() -> None:
    """Start the server, BLE scanner, and broadcast loops."""
    config = Config.from_env()

    setup_logging(config)
    _warn_cors_wildcard()

    app = create_app(config)

    # Open the database connection (async) and recover orphans
    history: HistoryStore = app["history"]
    await history.connect()
    await history.recover_orphaned_sessions()

    # Create and connect the push service (shares the history DB connection)
    push_service = PushService(
        db=history._conn,
        key_path=config.apns_key_path,
        key_id=config.apns_key_id,
        team_id=config.apns_team_id,
        bundle_id=config.apns_bundle_id,
        use_sandbox=config.apns_use_sandbox,
    )
    app["push_service"] = push_service
    await push_service.connect()

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
    await history.close()


if __name__ == "__main__":
    asyncio.run(run())
