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
from service.timers import countdown_completer_loop
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
    # Registry of fire-and-forget background tasks (push sends, LA updates,
    # etc.) so shutdown can drain them before closing the store layers they
    # write to. Populated by handlers via ``app["bg_tasks"].add(task)``.
    app["bg_tasks"] = set()

    setup_routes(app)
    setup_dashboard(app)

    return app


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------


async def rehydrate_alert_evaluator(
    history: HistoryStore, evaluator: AlertEvaluator
) -> None:
    """Seed the alert evaluator with targets from any session that was
    resumed by ``recover_orphaned_sessions``.

    Without this, alerts silently stop firing after a server restart: the
    session row survives and BLE readings resume persisting, but the
    evaluator's in-memory target map is empty and ``evaluate()`` returns
    an empty event list for the unknown session id. A user who set a
    brisket alarm before a reboot would otherwise never hear it fire.
    (la-followups Task 2)
    """
    sid = await history.get_current_session_id()
    if sid is None:
        return
    targets = await history.get_targets(sid)
    if targets:
        evaluator.set_targets(sid, targets)
        LOG.info(
            "Rehydrated %d alert target(s) for resumed session %s",
            len(targets), sid,
        )


async def run() -> None:
    """Start the server, BLE scanner, and broadcast loops."""
    config = Config.from_env()

    setup_logging(config)
    _warn_cors_wildcard()
    config.warn_if_misconfigured()

    app = create_app(config)

    # Open the database connection (async) and recover orphans
    history: HistoryStore = app["history"]
    await history.connect()
    await history.recover_orphaned_sessions()
    await rehydrate_alert_evaluator(history, app["evaluator"])

    # Create and connect the push service — owns its own SQLite connection
    # pointing at the same DB file as HistoryStore. WAL + busy_timeout (set
    # on both connections) prevents SQLITE_BUSY on the rare race; running
    # under the same asyncio.Lock as HistoryStore would require threading
    # push writes through the store, which couples layers unnecessarily.
    push_service = PushService(
        db_path=config.db_path,
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

    scan_task = asyncio.create_task(manager.scan_loop())
    long_running_tasks = [
        asyncio.create_task(broadcast_readings(app)),
        asyncio.create_task(broadcast_events(app)),
        asyncio.create_task(countdown_completer_loop(app)),
    ]

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    await stop.wait()
    LOG.info("Shutting down...")

    # 1. Stop accepting new HTTP/WS requests. runner.cleanup() also closes
    #    any in-flight WebSocket connections so their handlers unwind before
    #    we tear down the stores they depend on.
    await runner.cleanup()

    # 2. Stop the BLE scan loop before the device manager, so a mid-shutdown
    #    scan can't spawn a fresh worker while we're cancelling workers.
    scan_task.cancel()
    try:
        await scan_task
    except asyncio.CancelledError:
        pass

    # 3. Stop per-device workers.
    await manager.stop()

    # 4. Cancel the long-running broadcast/countdown tasks and drain.
    for t in long_running_tasks:
        t.cancel()
    await asyncio.gather(*long_running_tasks, return_exceptions=True)

    # 5. Drain any background tasks that handlers scheduled via
    #    app["bg_tasks"] before closing the stores they write to.
    bg_tasks = app.get("bg_tasks") or set()
    if bg_tasks:
        await asyncio.gather(*bg_tasks, return_exceptions=True)

    # 6. Remove any lingering WebSocket clients from the hub registry.
    for client in list(app["hub"].clients):
        await app["hub"].remove(client)

    # 7. Close the push service (owns its own DB connection) before the
    #    history store's DB — a stray in-flight push write would otherwise
    #    hit a closed DB path.
    await push_service.close()
    await history.close()


if __name__ == "__main__":
    asyncio.run(run())
