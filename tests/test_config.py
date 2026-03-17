"""Tests for service.config."""

from service.config import Config, _read_int_env


def test_config_defaults():
    """Config.from_env() returns sensible defaults with no env vars."""
    cfg = Config.from_env()
    assert cfg.port == 39120
    assert cfg.poll_interval == 15
    assert cfg.timeout == 30
    assert cfg.log_level == "INFO"


def test_config_from_env(monkeypatch):
    """Config picks up environment overrides."""
    monkeypatch.setenv("IGRILL_PORT", "8080")
    monkeypatch.setenv("IGRILL_LOG_LEVEL", "DEBUG")
    cfg = Config.from_env()
    assert cfg.port == 8080
    assert cfg.log_level == "DEBUG"


def test_default_config_dataclass():
    """Config dataclass defaults match expected values."""
    cfg = Config()
    assert cfg.port == 39120
    assert cfg.poll_interval == 15
    assert cfg.mac_prefix == "70:91:8F"


def test_from_env_multiple_overrides(monkeypatch):
    """Config.from_env() picks up multiple environment overrides."""
    monkeypatch.setenv("IGRILL_PORT", "8080")
    monkeypatch.setenv("IGRILL_POLL_INTERVAL", "30")
    monkeypatch.setenv("IGRILL_MAC_PREFIX", "AA:BB:CC")
    cfg = Config.from_env()
    assert cfg.port == 8080
    assert cfg.poll_interval == 30
    assert cfg.mac_prefix == "AA:BB:CC"


def test_poll_interval_clamped(monkeypatch):
    """Poll interval is clamped to configured min/max bounds."""
    monkeypatch.setenv("IGRILL_POLL_INTERVAL", "1")
    cfg = Config.from_env()
    assert cfg.poll_interval == 5

    monkeypatch.setenv("IGRILL_POLL_INTERVAL", "999")
    cfg = Config.from_env()
    assert cfg.poll_interval == 60


def test_invalid_int_env(monkeypatch):
    """Non-integer env var falls back to default."""
    monkeypatch.setenv("IGRILL_PORT", "not_a_number")
    cfg = Config.from_env()
    assert cfg.port == 39120
