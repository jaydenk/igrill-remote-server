"""APNS push notification service for iGrill Remote.

Sends alert notifications and Live Activity updates to registered iOS
devices via Apple Push Notification Service.  Gracefully degrades to a
no-op when APNS credentials are not configured.
"""

from __future__ import annotations

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


class PushService:
    """Manages APNS push delivery and device-token persistence.

    When any of the required credentials (key_path, key_id, team_id,
    bundle_id) are missing the service marks itself as disabled and all
    public methods become silent no-ops.
    """

    def __init__(
        self,
        db: aiosqlite.Connection,
        key_path: str,
        key_id: str,
        team_id: str,
        bundle_id: str,
        use_sandbox: bool = True,
    ) -> None:
        self._db = db
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
        """Initialise the APNS client.

        Reads the private key from disk and creates an ``aioapns.APNs``
        instance.  Does nothing if the service is disabled.
        """
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

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    async def upsert_token(
        self,
        token: str,
        live_activity_token: Optional[str] = None,
    ) -> None:
        """Insert or replace a device push token."""
        await self._db.execute(
            "INSERT OR REPLACE INTO push_tokens "
            "(token, live_activity_token, created_at, updated_at) "
            "VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), "
            "strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
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
        """
        title = _ALERT_TITLES.get(alert_type, "Alert")

        probe_index = payload.get("probeIndex", 0)
        target = payload.get("target", {})
        label = target.get("label") or None  # treat "" as None
        probe_name = label if label else f"Probe {probe_index}"

        current_temp = payload.get("currentTemp")
        target_value = target.get("target_value")

        if current_temp is not None and target_value is not None:
            body = f"{probe_name} is at {current_temp:.0f}\u00b0 (target: {target_value:.0f}\u00b0)"
        elif current_temp is not None:
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
        through Focus modes on iOS.
        """
        if not self._enabled or self._client is None:
            return

        tokens = await self._get_all_tokens()
        if not tokens:
            return

        alert_type = event.get("type", "")
        payload = event.get("payload", {})
        title, body = self.format_alert(alert_type, payload)

        from aioapns import NotificationRequest, PushType

        for token in tokens:
            request = NotificationRequest(
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

            try:
                result = await self._client.send_notification(request)
                if not result.is_successful:
                    LOG.warning(
                        "APNS push failed for %s…: %s",
                        token[:8],
                        result.description,
                    )
                    if result.description in _INVALID_TOKEN_REASONS:
                        await self.remove_token(token)
                        LOG.info(
                            "Removed invalid token %s… (%s)",
                            token[:8],
                            result.description,
                        )
            except Exception:
                LOG.exception("Failed to send push to %s…", token[:8])

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

        self._last_la_update_ts = time.monotonic()

        content_state = self._build_content_state(reading)

        from aioapns import NotificationRequest, PushType

        la_topic = f"{self._bundle_id}.push-type.liveactivity"

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
                if not result.is_successful:
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

    @staticmethod
    def _build_content_state(reading: dict) -> dict:
        """Build the content-state dict from a reading payload.

        Extracts probe data and device metadata into the format expected
        by the iOS Live Activity.
        """
        data = reading.get("data", reading)
        probes = data.get("probes", [])

        return {
            "probes": probes,
            "batteryPercent": data.get("battery_percent"),
            "unit": data.get("unit", "C"),
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
