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


def test_warn_if_misconfigured_partial_apns(caplog):
    """Partial APNS credentials must log a WARN so operators realise
    push is disabled before discovering it hours into a cook."""
    import logging
    from service.config import Config

    cfg = Config(
        apns_key_path="/path/to/key.p8",
        apns_key_id="",
        apns_team_id="TEAM123",
        apns_bundle_id="",
    )
    with caplog.at_level(logging.WARNING, logger="igrill"):
        cfg.warn_if_misconfigured()

    warnings = [r for r in caplog.records
                if r.levelno == logging.WARNING and "APNS" in r.getMessage()]
    assert warnings, "partial APNS config should produce a WARN"
    msg = warnings[0].getMessage()
    assert "IGRILL_APNS_KEY_ID" in msg
    assert "IGRILL_APNS_BUNDLE_ID" in msg


def test_warn_if_misconfigured_all_apns_missing_is_info_only(caplog):
    """If NO APNS credentials are set, that's a clean 'no push configured'
    state — log at INFO, not WARN, so it doesn't cry wolf."""
    import logging
    from service.config import Config

    cfg = Config()
    with caplog.at_level(logging.INFO, logger="igrill"):
        cfg.warn_if_misconfigured()

    warnings = [r for r in caplog.records
                if r.levelno == logging.WARNING and "APNS" in r.getMessage()]
    assert not warnings


def test_warn_if_misconfigured_full_apns_silent(caplog):
    import logging
    from service.config import Config

    cfg = Config(
        apns_key_path="/p", apns_key_id="K", apns_team_id="T", apns_bundle_id="B",
    )
    with caplog.at_level(logging.INFO, logger="igrill"):
        cfg.warn_if_misconfigured()

    apns_records = [r for r in caplog.records if "APNS" in r.getMessage()]
    assert not apns_records


def test_read_int_env_clamps_negative_timeout(monkeypatch, caplog):
    """IGRILL_TIMEOUT must now clamp to a positive minimum rather than
    accepting 0 (which would fail every GATT read instantly)."""
    import logging
    from service.config import Config

    monkeypatch.setenv("IGRILL_TIMEOUT", "-5")
    with caplog.at_level(logging.WARNING, logger="igrill"):
        cfg = Config.from_env()
    assert cfg.timeout >= 1
