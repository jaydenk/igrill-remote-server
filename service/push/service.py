"""APNS push notification service for iGrill Remote.

Sends alert notifications and Live Activity updates to registered iOS
devices via Apple Push Notification Service.  Gracefully degrades to a
no-op when APNS credentials are not configured.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional
from uuid import uuid4

import aiosqlite

LOG = logging.getLogger("igrill.push")

# Throttle interval for Live Activity pushes (seconds).
_LA_UPDATE_INTERVAL = 15

# APNS response descriptions that indicate a device token is no longer valid.
# "Unregistered" (HTTP 410) — token was valid but the device unregistered.
# "BadDeviceToken" (HTTP 400) — token is malformed or otherwise invalid.
_INVALID_TOKEN_REASONS = frozenset({
    "Unregistered",
    "BadDeviceToken",
})

# Human-readable titles for each alert type.
_ALERT_TITLES: dict[str, str] = {
    "target_approaching": "Approaching Target",
    "target_reached": "Target Reached",
    "target_exceeded": "Target Exceeded",
    "target_reminder": "Still Exceeded \u2014 Reminder",
}


def _target_to_c(value: Optional[float], unit: str) -> Optional[float]:
    """Convert a target temperature *value* in *unit* to Celsius, passing
    None through unchanged. Used to normalise target-related numbers before
    they cross the wire to iOS (LA content-state) or land in an APNS alert
    body alongside a C reading."""
    if value is None:
        return None
    return (value - 32.0) * 5.0 / 9.0 if str(unit).upper() == "F" else value


class PushService:
    """Manages APNS push delivery and device-token persistence.

    When any of the required credentials (key_path, key_id, team_id,
    bundle_id) are missing the service marks itself as disabled and all
    public methods become silent no-ops.

    Owns its own aiosqlite connection (opened in :meth:`connect`) so its
    writes — push-token inserts, content-state reads — cannot interleave
    inside a ``HistoryStore`` multi-statement transaction. Both
    connections run in WAL mode with a 5s busy timeout, so the SQLite
    writer lock is shared cooperatively without either side surfacing
    ``SQLITE_BUSY`` in the happy path.
    """

    def __init__(
        self,
        db_path: str,
        key_path: str,
        key_id: str,
        team_id: str,
        bundle_id: str,
        use_sandbox: bool = True,
    ) -> None:
        self._db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None
        self._key_path = key_path
        self._key_id = key_id
        self._team_id = team_id
        self._bundle_id = bundle_id
        self._use_sandbox = use_sandbox

        self._enabled = all([key_path, key_id, team_id, bundle_id])
        self._client: Any = None  # aioapns.APNs once connected
        self._last_la_update_ts: float = 0.0

        if self._enabled:
            LOG.info("Push notifications enabled (sandbox=%s)", use_sandbox)
        else:
            LOG.info("Push notifications disabled — APNS credentials not configured")

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """Whether push notifications are configured and available."""
        return self._enabled

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open the SQLite connection and initialise the APNS client.

        The SQLite connection is opened unconditionally (even when push is
        disabled) so that ``upsert_token`` / ``remove_token`` still work
        — iOS clients always register tokens, even on a server with no
        APNS credentials, because a later configuration change should
        pick them up without needing the client to re-register.
        """
        await self._open_db()
        await self._init_apns_client()

    async def _open_db(self) -> None:
        """Open the owned SQLite connection with WAL + busy_timeout PRAGMAs.

        Separate from APNS init so tests can exercise token DB behaviour
        without needing a real APNS key file on disk.
        """
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute("PRAGMA busy_timeout=5000")

    async def _init_apns_client(self) -> None:
        if not self._enabled:
            return

        try:
            from aioapns import APNs

            with open(self._key_path, "r") as f:
                key_content = f.read()

            self._client = APNs(
                key=key_content,
                key_id=self._key_id,
                team_id=self._team_id,
                topic=self._bundle_id,
                use_sandbox=self._use_sandbox,
            )
            LOG.info("APNS client connected")
        except FileNotFoundError:
            LOG.error("APNS key file not found: %s — push disabled", self._key_path)
            self._enabled = False
        except Exception:
            LOG.exception("Failed to initialise APNS client — push disabled")
            self._enabled = False

    async def close(self) -> None:
        """Close the APNS client and the SQLite connection."""
        if self._client is not None:
            try:
                close = getattr(self._client, "close", None)
                if close is not None:
                    result = close()
                    if hasattr(result, "__await__"):
                        await result
            except Exception:
                LOG.exception("APNS client close failed")
            self._client = None
        if self._db is not None:
            await self._db.close()
            self._db = None

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    async def upsert_token(
        self,
        token: str,
        live_activity_token: Optional[str] = None,
    ) -> None:
        """Insert or update a device push token.

        The LA token is merged with COALESCE: passing ``None`` for
        ``live_activity_token`` no longer nulls a previously-stored
        value. iOS clients re-register just the APNS token on rotation
        without a current LA token, and the old ``INSERT OR REPLACE``
        silently dropped the LA token in that path — breaking Live
        Activity updates until the user started another session.
        """
        await self._db.execute(
            "INSERT INTO push_tokens "
            "(token, live_activity_token, created_at, updated_at) "
            "VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), "
            "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) "
            "ON CONFLICT(token) DO UPDATE SET "
            "live_activity_token = COALESCE(excluded.live_activity_token, "
            "live_activity_token), "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
            (token, live_activity_token),
        )
        await self._db.commit()
        LOG.debug("Upserted push token: %s…", token[:8])

    async def remove_token(self, token: str) -> None:
        """Remove a device push token."""
        await self._db.execute(
            "DELETE FROM push_tokens WHERE token = ?",
            (token,),
        )
        await self._db.commit()
        LOG.debug("Removed push token: %s…", token[:8])

    async def _get_all_tokens(self) -> list[str]:
        """Return all registered device push tokens."""
        cursor = await self._db.execute(
            "SELECT token FROM push_tokens",
        )
        rows = await cursor.fetchall()
        return [row["token"] for row in rows]

    async def _get_la_tokens(self) -> list[str]:
        """Return all non-null Live Activity tokens."""
        cursor = await self._db.execute(
            "SELECT live_activity_token FROM push_tokens "
            "WHERE live_activity_token IS NOT NULL",
        )
        rows = await cursor.fetchall()
        return [row["live_activity_token"] for row in rows]

    # ------------------------------------------------------------------
    # Alert formatting
    # ------------------------------------------------------------------

    @staticmethod
    def format_alert(alert_type: str, payload: dict) -> tuple[str, str]:
        """Format an alert event into a human-readable (title, body) pair.

        Uses the probe label from the target config if available,
        otherwise falls back to "Probe N".

        ``currentTemp`` is always Celsius (readings are C). Target values may
        be stored in either unit; we convert them to Celsius for the body so
        the two numbers share one scale — otherwise a user who configured the
        target in Fahrenheit would see a message like "is at 70° (target:
        165°)" which is nonsensical.
        """
        title = _ALERT_TITLES.get(alert_type, "Alert")

        probe_index = payload.get("probeIndex", 0)
        target = payload.get("target", {})
        label = target.get("label") or None  # treat "" as None
        probe_name = label if label else f"Probe {probe_index}"

        current_temp = payload.get("currentTemp")
        target_unit = str(target.get("unit") or "C").upper()
        target_value = _target_to_c(target.get("target_value"), target_unit)
        range_low = _target_to_c(target.get("range_low"), target_unit)
        range_high = _target_to_c(target.get("range_high"), target_unit)

        if current_temp is not None:
            if target_value is not None:
                body = f"{probe_name} is at {current_temp:.0f}\u00b0 (target: {target_value:.0f}\u00b0)"
            elif range_low is not None and range_high is not None:
                body = f"{probe_name} is at {current_temp:.0f}\u00b0 (range: {range_low:.0f}\u2013{range_high:.0f}\u00b0)"
            else:
                body = f"{probe_name} is at {current_temp:.0f}\u00b0"
        else:
            body = probe_name

        return title, body

    # ------------------------------------------------------------------
    # Push delivery
    # ------------------------------------------------------------------

    async def send_alert(self, event: dict) -> None:
        """Send a push notification for an alert event to all registered tokens.

        Uses time-sensitive interruption level so notifications break
        through Focus modes on iOS. Tokens are dispatched concurrently via
        ``asyncio.gather`` so one slow/stalled peer cannot delay the rest
        of the fan-out — a three-second APNS timeout on one client used to
        delay every other client behind it. A transient 5xx response is
        retried once after a 500 ms backoff; permanent 4xx responses
        (BadDeviceToken, Unregistered) drop the token and never retry.
        """
        if not self._enabled or self._client is None:
            return

        tokens = await self._get_all_tokens()
        if not tokens:
            return

        alert_type = event.get("type", "")
        payload = event.get("payload", {})
        title, body = self.format_alert(alert_type, payload)

        await asyncio.gather(
            *(self._send_alert_to_token(token, title, body) for token in tokens),
            return_exceptions=True,
        )

    async def _send_alert_to_token(
        self, token: str, title: str, body: str,
    ) -> None:
        """Send one alert push with a single retry on transient 5xx.

        Extracted from ``send_alert`` so the fan-out can schedule one
        coroutine per token. Exceptions are caught here (rather than
        bubbling up to ``gather``) so a 4xx-invalid-token path can still
        execute its token-removal side effect.
        """
        from aioapns import NotificationRequest, PushType

        def _build_request() -> "NotificationRequest":
            return NotificationRequest(
                device_token=token,
                message={
                    "aps": {
                        "alert": {"title": title, "body": body},
                        "sound": "default",
                        "interruption-level": "time-sensitive",
                    },
                },
                notification_id=str(uuid4()),
                push_type=PushType.ALERT,
            )

        for attempt in range(2):  # initial + at most one retry
            try:
                result = await self._client.send_notification(_build_request())
            except Exception:
                LOG.exception("Failed to send push to %s…", token[:8])
                return

            if result.is_successful:
                return

            status = getattr(result, "status", None)
            description = result.description

            if description in _INVALID_TOKEN_REASONS:
                await self.remove_token(token)
                LOG.info(
                    "Removed invalid token %s… (%s)", token[:8], description,
                )
                return

            # Retry only on transient 5xx (or unknown status — treat as
            # retryable once). 4xx client errors that aren't invalid-token
            # are permanent; retrying wastes APNS traffic.
            retryable = status is None or (
                isinstance(status, int) and status >= 500
            )
            if not retryable or attempt == 1:
                LOG.warning(
                    "APNS push failed for %s… (status=%s): %s",
                    token[:8], status, description,
                )
                return

            LOG.info(
                "APNS push transient failure for %s… (status=%s) — retrying",
                token[:8], status,
            )
            await asyncio.sleep(0.5)

    # ------------------------------------------------------------------
    # Live Activity updates
    # ------------------------------------------------------------------

    def should_send_la_update(self) -> bool:
        """Return True if enough time has elapsed since the last LA update."""
        if self._last_la_update_ts == 0.0:
            return True
        return (time.monotonic() - self._last_la_update_ts) >= _LA_UPDATE_INTERVAL

    async def send_live_activity_update(self, reading: dict) -> None:
        """Send a Live Activity content-state update to all LA tokens.

        The push uses ``push-type: liveactivity`` and targets the
        ``{bundle_id}.push-type.liveactivity`` topic.
        """
        if not self._enabled or self._client is None:
            return

        if not self.should_send_la_update():
            return

        la_tokens = await self._get_la_tokens()
        if not la_tokens:
            return

        content_state = await self._build_content_state(reading)

        from aioapns import NotificationRequest, PushType

        la_topic = f"{self._bundle_id}.push-type.liveactivity"

        any_success = False
        for token in la_tokens:
            request = NotificationRequest(
                device_token=token,
                message={
                    "aps": {
                        "timestamp": int(time.time()),
                        "event": "update",
                        "content-state": content_state,
                    },
                },
                notification_id=str(uuid4()),
                push_type=PushType.LIVEACTIVITY,
                apns_topic=la_topic,
            )

            try:
                result = await self._client.send_notification(request)
                if result.is_successful:
                    any_success = True
                else:
                    LOG.warning(
                        "APNS LA update failed for %s…: %s",
                        token[:8],
                        result.description,
                    )
                    if result.description in _INVALID_TOKEN_REASONS:
                        await self._remove_la_token(token)
                        LOG.info(
                            "Removed invalid LA token %s… (%s)",
                            token[:8],
                            result.description,
                        )
            except Exception:
                LOG.exception("Failed to send LA update to %s…", token[:8])

        # Only advance the throttle when at least one push succeeded —
        # otherwise a dead APNS connection during this window would
        # silently lock out the next ~15s of updates as well.
        if any_success:
            self._last_la_update_ts = time.monotonic()

    async def end_live_activities(self) -> None:
        """Send ``aps.event = "end"`` to every registered Live Activity
        token so the lock-screen widget dismisses when the session ends.

        Invoked from ``broadcast_events`` on ``session_end`` and
        ``session_discarded``. Without this the LA stays alive on the
        user's lock screen with stale probe data for up to 8–12 hours
        (iOS's LA TTL) after a cook finishes.
        """
        if not self._enabled or self._client is None:
            return

        la_tokens = await self._get_la_tokens()
        if not la_tokens:
            return

        from aioapns import NotificationRequest, PushType

        la_topic = f"{self._bundle_id}.push-type.liveactivity"

        for token in la_tokens:
            request = NotificationRequest(
                device_token=token,
                message={
                    "aps": {
                        "timestamp": int(time.time()),
                        "event": "end",
                    },
                },
                notification_id=str(uuid4()),
                push_type=PushType.LIVEACTIVITY,
                apns_topic=la_topic,
            )
            try:
                result = await self._client.send_notification(request)
                if not result.is_successful:
                    LOG.warning(
                        "APNS LA end failed for %s…: %s",
                        token[:8], result.description,
                    )
                    if result.description in _INVALID_TOKEN_REASONS:
                        await self._remove_la_token(token)
            except Exception:
                LOG.exception("Failed to send LA end to %s…", token[:8])

    async def _build_content_state(self, reading: dict) -> dict:
        """Build the content-state dict from a reading payload.

        Queries the DB for per-probe labels, targets, and timer state so the
        payload satisfies the iOS ``CookSessionAttributes.ContentState`` schema.
        All non-optional fields (``index``, ``label``, ``unplugged``,
        ``recentTemps``) are always present; optional fields are included when
        data is available.

        JSON field names are camelCase to match the Swift struct property names
        (Swift uses property names as coding keys by default when no CodingKeys
        enum is declared).
        """
        payload = reading.get("payload", {})
        data = payload.get("data", {})
        raw_probes = data.get("probes", [])
        session_id = payload.get("sessionId")
        address = payload.get("sensorId")
        unit = data.get("unit", "C")

        # la-followups Task 8: fetch targets and timers for the FULL
        # session so the LA content-state spans every device, not just
        # the firing one. Filtering by sensorId (as before) made
        # multi-device Live Activities flicker between disjoint probe
        # sets on every push. Key by (address, probe_index) so probes
        # with the same index on different devices don't collide.
        targets_by_key: dict[tuple[str, int], dict] = {}
        if session_id:
            try:
                cursor = await self._db.execute(
                    "SELECT address, probe_index, label, mode, target_value, "
                    "range_low, range_high, unit "
                    "FROM session_targets WHERE session_id = ?",
                    (session_id,),
                )
                rows = await cursor.fetchall()
                for row in rows:
                    targets_by_key[(row["address"], row["probe_index"])] = {
                        "label": row["label"],
                        "mode": row["mode"],
                        "target_value": row["target_value"],
                        "range_low": row["range_low"],
                        "range_high": row["range_high"],
                        "unit": row["unit"] if row["unit"] else "C",
                    }
            except Exception:
                LOG.warning(
                    "Failed to fetch session_targets for session=%s",
                    session_id, exc_info=True,
                )

        timers_by_key: dict[tuple[str, int], dict] = {}
        if session_id:
            try:
                cursor = await self._db.execute(
                    "SELECT address, probe_index, mode, duration_secs, "
                    "started_at, paused_at, accumulated_secs, completed_at "
                    "FROM session_timers WHERE session_id = ?",
                    (session_id,),
                )
                rows = await cursor.fetchall()
                for row in rows:
                    timers_by_key[(row["address"], row["probe_index"])] = {
                        "mode": row["mode"],
                        "durationSecs": row["duration_secs"],
                        "startedAt": row["started_at"],
                        "pausedAt": row["paused_at"],
                        "accumulatedSecs": row["accumulated_secs"],
                        "completedAt": row["completed_at"],
                    }
            except Exception:
                LOG.warning(
                    "Failed to fetch session_timers for session=%s",
                    session_id, exc_info=True,
                )

        # Helper: turn a (address, probe_index, target_row, timer_row,
        # optional live-temp, optional unplugged-flag) into one
        # ProbeState dict matching the iOS schema.
        def _probe_state(
            addr: str,
            index: int,
            temperature: float | None,
            unplugged: bool,
        ) -> dict:
            target_row = targets_by_key.get((addr, index), {})
            mode = target_row.get("mode", "fixed")
            target_unit = target_row.get("unit", "C")
            raw_fixed_target = (
                target_row.get("target_value") if mode == "fixed" else None
            )
            target_value = _target_to_c(raw_fixed_target, target_unit)
            target_low = _target_to_c(
                target_row.get("range_low") if mode == "range" else None,
                target_unit,
            )
            target_high = _target_to_c(
                target_row.get("range_high") if mode == "range" else None,
                target_unit,
            )
            label_raw = target_row.get("label") or ""
            label = label_raw if label_raw else f"Probe {index}"
            timer_row = timers_by_key.get((addr, index))

            state: dict = {
                "index": index,
                # la-followups Task 8: always stamp deviceAddress so iOS
                # can distinguish probes across devices.
                "deviceAddress": addr,
                "label": label,
                "unplugged": unplugged,
                "recentTemps": [],
            }
            if temperature is not None:
                state["temperature"] = temperature
            if target_value is not None:
                state["target"] = target_value
            if mode == "range" and (
                target_low is not None or target_high is not None
            ):
                state["targetMode"] = "range"
                if target_low is not None:
                    state["targetLow"] = target_low
                if target_high is not None:
                    state["targetHigh"] = target_high
            elif mode == "fixed" and target_value is not None:
                state["targetMode"] = "fixed"
            if timer_row is not None:
                state["timer"] = timer_row
            return state

        # Live probes from the firing reading.
        probe_states: list[dict] = []
        seen: set[tuple[str, int]] = set()
        for probe in raw_probes:
            index = probe.get("index", 0)
            temperature = probe.get("temperature")
            firing_addr = address or ""
            probe_states.append(_probe_state(
                firing_addr, index, temperature, temperature is None,
            ))
            seen.add((firing_addr, index))

        # Stub entries for probes on OTHER devices in the same session,
        # so the LA shows a stable probe set across pushes rather than
        # swapping to just the firing device's probes each time.
        for (other_addr, other_index) in targets_by_key.keys():
            if (other_addr, other_index) in seen:
                continue
            probe_states.append(_probe_state(
                other_addr, other_index, None, True,
            ))

        return {
            "probes": probe_states,
            "unit": unit,
            "featuredProbeIndex": None,
        }

    async def _remove_la_token(self, la_token: str) -> None:
        """Clear a Live Activity token from the push_tokens table."""
        await self._db.execute(
            "UPDATE push_tokens SET live_activity_token = NULL "
            "WHERE live_activity_token = ?",
            (la_token,),
        )
        await self._db.commit()

    # ------------------------------------------------------------------
    # Test push
    # ------------------------------------------------------------------

    async def send_test(self) -> dict[str, Any]:
        """Send a test push notification to all registered tokens.

        Returns a summary of delivery results per token.
        """
        if not self._enabled or self._client is None:
            return {"error": "push notifications not configured"}

        tokens = await self._get_all_tokens()
        if not tokens:
            return {"error": "no push tokens registered"}

        from aioapns import NotificationRequest, PushType

        results: list[dict[str, Any]] = []
        for token in tokens:
            request = NotificationRequest(
                device_token=token,
                message={
                    "aps": {
                        "alert": {
                            "title": "iGrill Remote — Test",
                            "body": "Push notifications are working.",
                        },
                        "sound": "default",
                    },
                },
                notification_id=str(uuid4()),
                push_type=PushType.ALERT,
            )
            try:
                result = await self._client.send_notification(request)
                results.append({
                    "token": f"{token[:8]}…",
                    "success": result.is_successful,
                    "description": result.description if not result.is_successful else None,
                })
                if not result.is_successful and result.description in _INVALID_TOKEN_REASONS:
                    await self.remove_token(token)
            except Exception as exc:
                results.append({
                    "token": f"{token[:8]}…",
                    "success": False,
                    "description": str(exc),
                })

        return {"sent": len(results), "results": results}
