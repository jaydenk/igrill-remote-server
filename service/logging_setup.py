"""Structured logging setup with per-subsystem level control."""

import logging
from service.config import Config

SUBSYSTEM_LOGGERS = {
    "igrill.ble": "log_level_ble",
    "igrill.ws": "log_level_ws",
    "igrill.session": "log_level_session",
    "igrill.alert": "log_level_alert",
    "igrill.http": "log_level_http",
}

LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s %(message)s"


def setup_logging(config: Config) -> None:
    """Configure root and per-subsystem loggers."""
    global_level = getattr(logging, config.log_level.upper(), logging.INFO)
    logging.basicConfig(level=global_level, format=LOG_FORMAT, force=True)

    for logger_name, config_attr in SUBSYSTEM_LOGGERS.items():
        level_str = getattr(config, config_attr, "")
        if level_str:
            level = getattr(logging, level_str.upper(), None)
            if level is not None:
                logging.getLogger(logger_name).setLevel(level)


def update_log_level(logger_name: str, level_str: str) -> bool:
    """Update a logger's level at runtime. Returns True on success."""
    level = getattr(logging, level_str.upper(), None)
    if level is None:
        return False
    logging.getLogger(logger_name).setLevel(level)
    return True
