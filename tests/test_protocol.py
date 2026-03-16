"""Tests for iGrillRemoteServer.service.ble.protocol."""

from unittest.mock import MagicMock

from service.ble.protocol import IGRILLV3_TEMPERATURE_SERVICE_UUID, MODELS, detect_model


def _mock_services(*uuids: str):
    """Return a list of mock BLE service objects with the given UUIDs."""
    return [MagicMock(uuid=u) for u in uuids]


def test_detect_known_model():
    """detect_model should return the correct ModelInfo for a known service UUID."""
    services = _mock_services(
        "180f",  # battery — not a model UUID
        IGRILLV3_TEMPERATURE_SERVICE_UUID,
    )
    result = detect_model(services)
    assert result is not None
    assert result.model_id == "igrill_v3"
    assert result.probe_count == 4
    assert result.is_pulse is False


def test_detect_unknown_model():
    """detect_model should return None when no known service UUID is present."""
    services = _mock_services("00000000-0000-0000-0000-000000000000")
    result = detect_model(services)
    assert result is None


def test_all_models_have_unique_uuids():
    """Every model must advertise a distinct temperature-service UUID."""
    uuids = [m.service_uuid for m in MODELS]
    assert len(uuids) == len(set(uuids)), "Duplicate service UUIDs detected in MODELS"
