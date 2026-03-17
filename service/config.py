"""Server configuration from environment variables."""

import logging
import os
from dataclasses import dataclass

LOG = logging.getLogger("igrill")

DEFAULT_PORT = 39120
DEFAULT_POLL_INTERVAL = 15
DEFAULT_TIMEOUT = 30
MIN_POLL_INTERVAL = 5
MAX_POLL_INTERVAL = 60
DEFAULT_SCAN_INTERVAL = 60
DEFAULT_SCAN_TIMEOUT = 5
DEFAULT_RECONNECT_GRACE = 60
DEFAULT_DB_PATH = "/data/igrill.db"
DEFAULT_MAC_PREFIX = "70:91:8F"
DEFAULT_BIND_ADDRESS = "0.0.0.0"
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_CONNECT_TIMEOUT = 10
DEFAULT_MAX_BACKOFF = 60


def _read_int_env(
    name: str,
    default: int,
    min_value: int | None = None,
    max_value: int | None = None,
) -> int:
    raw = os.getenv(name, "")
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        LOG.warning("%s=%r is not an integer, using default %d", name, raw, default)
        return default
    if min_value is not None and value < min_value:
        LOG.warning("%s=%d below minimum %d, clamping", name, value, min_value)
        value = min_value
    if max_value is not None and value > max_value:
        LOG.warning("%s=%d above maximum %d, clamping", name, value, max_value)
        value = max_value
    return value


@dataclass(frozen=True)
class Config:
    port: int = DEFAULT_PORT
    poll_interval: int = DEFAULT_POLL_INTERVAL
    timeout: int = DEFAULT_TIMEOUT
    scan_interval: int = DEFAULT_SCAN_INTERVAL
    scan_timeout: int = DEFAULT_SCAN_TIMEOUT
    reconnect_grace: int = DEFAULT_RECONNECT_GRACE
    db_path: str = DEFAULT_DB_PATH
    mac_prefix: str = DEFAULT_MAC_PREFIX
    bind_address: str = DEFAULT_BIND_ADDRESS
    log_level: str = DEFAULT_LOG_LEVEL
    session_token: str = ""
    connect_timeout: int = DEFAULT_CONNECT_TIMEOUT
    max_backoff: int = DEFAULT_MAX_BACKOFF
    log_level_ble: str = ""
    log_level_ws: str = ""
    log_level_session: str = ""
    log_level_alert: str = ""
    log_level_http: str = ""

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            port=_read_int_env("IGRILL_PORT", DEFAULT_PORT),
            poll_interval=_read_int_env(
                "IGRILL_POLL_INTERVAL",
                DEFAULT_POLL_INTERVAL,
                MIN_POLL_INTERVAL,
                MAX_POLL_INTERVAL,
            ),
            timeout=_read_int_env("IGRILL_TIMEOUT", DEFAULT_TIMEOUT),
            scan_interval=_read_int_env("IGRILL_SCAN_INTERVAL", DEFAULT_SCAN_INTERVAL),
            scan_timeout=_read_int_env("IGRILL_SCAN_TIMEOUT", DEFAULT_SCAN_TIMEOUT),
            reconnect_grace=_read_int_env(
                "IGRILL_RECONNECT_GRACE", DEFAULT_RECONNECT_GRACE
            ),
            db_path=os.getenv("IGRILL_DB_PATH", DEFAULT_DB_PATH),
            mac_prefix=os.getenv("IGRILL_MAC_PREFIX", DEFAULT_MAC_PREFIX),
            bind_address=os.getenv("IGRILL_BIND_ADDRESS", DEFAULT_BIND_ADDRESS),
            log_level=os.getenv("IGRILL_LOG_LEVEL", DEFAULT_LOG_LEVEL),
            session_token=os.getenv("IGRILL_SESSION_TOKEN", ""),
            connect_timeout=_read_int_env("IGRILL_CONNECT_TIMEOUT", DEFAULT_CONNECT_TIMEOUT, min_value=1),
            max_backoff=_read_int_env("IGRILL_MAX_BACKOFF", DEFAULT_MAX_BACKOFF, min_value=1),
            log_level_ble=os.getenv("IGRILL_LOG_LEVEL_BLE", ""),
            log_level_ws=os.getenv("IGRILL_LOG_LEVEL_WS", ""),
            log_level_session=os.getenv("IGRILL_LOG_LEVEL_SESSION", ""),
            log_level_alert=os.getenv("IGRILL_LOG_LEVEL_ALERT", ""),
            log_level_http=os.getenv("IGRILL_LOG_LEVEL_HTTP", ""),
        )
