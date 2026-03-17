"""Tests for logging setup."""

import logging
from service.config import Config
from service.logging_setup import setup_logging, update_log_level


def test_setup_logging_global_level(monkeypatch):
    monkeypatch.setenv("IGRILL_LOG_LEVEL", "WARNING")
    config = Config.from_env()
    setup_logging(config)
    assert logging.getLogger("igrill").getEffectiveLevel() <= logging.WARNING


def test_subsystem_override(monkeypatch):
    monkeypatch.setenv("IGRILL_LOG_LEVEL", "INFO")
    monkeypatch.setenv("IGRILL_LOG_LEVEL_BLE", "DEBUG")
    config = Config.from_env()
    setup_logging(config)
    assert logging.getLogger("igrill.ble").level == logging.DEBUG


def test_runtime_level_update():
    assert update_log_level("igrill.ble", "WARNING") is True
    assert logging.getLogger("igrill.ble").level == logging.WARNING


def test_invalid_level():
    assert update_log_level("igrill.ble", "INVALID") is False
