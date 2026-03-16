import os
import pytest
from service.config import Config, _read_int_env


def test_default_config():
    cfg = Config()
    assert cfg.port == 39120
    assert cfg.poll_interval == 15
    assert cfg.mac_prefix == "70:91:8F"


def test_from_env(monkeypatch):
    monkeypatch.setenv("IGRILL_PORT", "8080")
    monkeypatch.setenv("IGRILL_POLL_INTERVAL", "30")
    monkeypatch.setenv("IGRILL_MAC_PREFIX", "AA:BB:CC")
    cfg = Config.from_env()
    assert cfg.port == 8080
    assert cfg.poll_interval == 30
    assert cfg.mac_prefix == "AA:BB:CC"


def test_poll_interval_clamped(monkeypatch):
    monkeypatch.setenv("IGRILL_POLL_INTERVAL", "1")
    cfg = Config.from_env()
    assert cfg.poll_interval == 5

    monkeypatch.setenv("IGRILL_POLL_INTERVAL", "999")
    cfg = Config.from_env()
    assert cfg.poll_interval == 60


def test_invalid_int_env(monkeypatch):
    monkeypatch.setenv("IGRILL_PORT", "not_a_number")
    cfg = Config.from_env()
    assert cfg.port == 39120
