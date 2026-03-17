"""Tests for new config fields."""

from service.config import Config


def test_connect_timeout_default():
    cfg = Config.from_env()
    assert cfg.connect_timeout == 10


def test_max_backoff_default():
    cfg = Config.from_env()
    assert cfg.max_backoff == 60


def test_connect_timeout_from_env(monkeypatch):
    monkeypatch.setenv("IGRILL_CONNECT_TIMEOUT", "5")
    cfg = Config.from_env()
    assert cfg.connect_timeout == 5


def test_max_backoff_from_env(monkeypatch):
    monkeypatch.setenv("IGRILL_MAX_BACKOFF", "120")
    cfg = Config.from_env()
    assert cfg.max_backoff == 120


def test_per_subsystem_log_levels(monkeypatch):
    monkeypatch.setenv("IGRILL_LOG_LEVEL_BLE", "DEBUG")
    cfg = Config.from_env()
    assert cfg.log_level_ble == "DEBUG"
    assert cfg.log_level_ws == ""  # not set, falls back to global


def test_all_subsystem_log_levels(monkeypatch):
    monkeypatch.setenv("IGRILL_LOG_LEVEL_BLE", "DEBUG")
    monkeypatch.setenv("IGRILL_LOG_LEVEL_WS", "WARNING")
    monkeypatch.setenv("IGRILL_LOG_LEVEL_SESSION", "ERROR")
    monkeypatch.setenv("IGRILL_LOG_LEVEL_ALERT", "INFO")
    monkeypatch.setenv("IGRILL_LOG_LEVEL_HTTP", "DEBUG")
    cfg = Config.from_env()
    assert cfg.log_level_ble == "DEBUG"
    assert cfg.log_level_ws == "WARNING"
    assert cfg.log_level_session == "ERROR"
    assert cfg.log_level_alert == "INFO"
    assert cfg.log_level_http == "DEBUG"
