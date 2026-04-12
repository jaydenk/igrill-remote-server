"""Background task that auto-completes expired count_down timers.

A coarse (default 5 s) loop scans ``session_timers`` for running count_down
rows whose effective elapsed time has reached or exceeded ``duration_secs``
and, for each, calls :meth:`HistoryStore.complete_timer` + broadcasts a
``probe_timer_update`` event exactly once.

Accuracy target is ±10 s, so a short polling interval is adequate and a
precisely-scheduled per-timer approach is unnecessary.

The completer is structured so tests can call :meth:`tick` directly without
waiting for the scheduler loop.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from aiohttp import web

from service.api.envelope import make_envelope
from service.history.store import HistoryStore
from service.models.device import DeviceStore

LOG = logging.getLogger("igrill.timers")

DEFAULT_INTERVAL_SECS = 5.0


class CountdownCompleter:
    """Scans for expired running count_down timers and completes them.

    Usage:

    * In production, start the :meth:`run` coroutine as a background task.
    * In tests, call :meth:`tick` directly for deterministic behaviour.
    """

    def __init__(
        self,
        history: HistoryStore,
        store: DeviceStore,
        interval_secs: float = DEFAULT_INTERVAL_SECS,
    ) -> None:
        self._history = history
        self._store = store
        self._interval_secs = interval_secs

    async def tick(self) -> int:
        """Run one scan pass: complete any expired running count_down timers
        and publish a ``probe_timer_update`` event for each.

        Returns the number of timers completed in this pass.
        """
        try:
            expired = await self._history.find_expired_running_countdowns()
        except Exception:
            LOG.exception("find_expired_running_countdowns failed")
            return 0

        if not expired:
            return 0

        completed_count = 0
        for row in expired:
            session_id = row["session_id"]
            address = row["address"]
            probe_index = row["probe_index"]
            try:
                completed_row = await self._history.complete_timer(
                    session_id, address, probe_index,
                )
            except ValueError as exc:
                # Session may have ended between find and complete, or the
                # row may have been deleted — log and move on.
                LOG.debug(
                    "Skipping auto-complete for timer %s/%s/%s: %s",
                    session_id, address, probe_index, exc,
                )
                continue
            except Exception:
                LOG.exception(
                    "complete_timer failed for %s/%s/%s",
                    session_id, address, probe_index,
                )
                continue

            try:
                await self._store.publish_event(
                    make_envelope("probe_timer_update", completed_row)
                )
            except Exception:
                LOG.exception(
                    "Failed to publish probe_timer_update for %s/%s/%s",
                    session_id, address, probe_index,
                )
                continue

            completed_count += 1
            LOG.info(
                "Auto-completed count_down timer session=%s address=%s probe=%d",
                session_id, address, probe_index,
            )

        return completed_count

    async def run(self) -> None:
        """Background loop — scans every ``interval_secs`` forever until cancelled."""
        while True:
            await self.tick()
            await asyncio.sleep(self._interval_secs)


async def countdown_completer_loop(
    app: web.Application, interval_secs: float = DEFAULT_INTERVAL_SECS,
) -> None:
    """Entry point for ``asyncio.create_task`` in :func:`service.main.run`.

    Pulls ``history`` and ``store`` off the application and runs a
    :class:`CountdownCompleter` forever.
    """
    history: HistoryStore = app["history"]
    store: DeviceStore = app["store"]
    completer = CountdownCompleter(history, store, interval_secs=interval_secs)
    app["countdown_completer"] = completer
    await completer.run()
