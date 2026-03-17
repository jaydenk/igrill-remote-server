"""BLE protocol constants for iGrill devices.

BLE GATT UUIDs and model definitions originally reverse-engineered by
Bendik Wang Andreassen for the esphome-igrill project:
https://github.com/bendikwa/esphome-igrill
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# Authentication UUIDs
# ---------------------------------------------------------------------------
AUTHENTICATION_SERVICE_UUID = "64ac0000-4a4b-4b58-9f37-94d3c52ffdf7"
APP_CHALLENGE_UUID = "64ac0002-4a4b-4b58-9f37-94d3c52ffdf7"
DEVICE_CHALLENGE_UUID = "64ac0003-4a4b-4b58-9f37-94d3c52ffdf7"
DEVICE_RESPONSE_UUID = "64ac0004-4a4b-4b58-9f37-94d3c52ffdf7"

# ---------------------------------------------------------------------------
# Temperature service UUIDs (one per device model)
# ---------------------------------------------------------------------------
IGRILL_MINI_TEMPERATURE_SERVICE_UUID = "63c70000-4a82-4261-95ff-92cf32477861"
IDEVICES_KITCHEN_TEMPERATURE_SERVICE_UUID = "19450000-9b05-40bb-80d8-7c85840aec34"
IGRILL_MINIV2_TEMPERATURE_SERVICE_UUID = "9d610c43-ae1d-41a9-9b09-3c7ecd5c6035"
IGRILLV2_TEMPERATURE_SERVICE_UUID = "a5c50000-f186-4bd6-97f2-7ebacba0d708"
IGRILLV202_TEMPERATURE_SERVICE_UUID = "ada7590f-2e6d-469e-8f7b-1822b386a5e9"
IGRILLV3_TEMPERATURE_SERVICE_UUID = "6e910000-58dc-41c7-943f-518b278cea88"
PULSE_1000_TEMPERATURE_SERVICE_UUID = "7e920000-68dc-41c7-943f-518b278cea87"
PULSE_2000_TEMPERATURE_SERVICE_UUID = "7e920000-68dc-41c7-943f-518b278cea88"
PULSE_ELEMENT_SERVICE_UUID = "6c910000-58dc-41c7-943f-518b278ceaaa"

# ---------------------------------------------------------------------------
# Characteristic UUIDs
# ---------------------------------------------------------------------------
TEMPERATURE_UNIT_UUID = "06ef0001-2e06-4b79-9e33-fce2c42805ec"
PROBE_TEMPERATURE_UUIDS = [
    "06ef0002-2e06-4b79-9e33-fce2c42805ec",
    "06ef0004-2e06-4b79-9e33-fce2c42805ec",
    "06ef0006-2e06-4b79-9e33-fce2c42805ec",
    "06ef0008-2e06-4b79-9e33-fce2c42805ec",
]
PULSE_ELEMENT_UUID = "6c91000a-58dc-41c7-943f-518b278ceaaa"

PROPANE_LEVEL_SERVICE_UUID = "f5d40000-3548-4c22-9947-f3673fce3cd9"
PROPANE_LEVEL_UUID = "f5d40001-3548-4c22-9947-f3673fce3cd9"

BATTERY_SERVICE_UUID = "180f"
BATTERY_LEVEL_UUID = "2a19"

# ---------------------------------------------------------------------------
# Model definitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelInfo:
    """Immutable descriptor for a supported iGrill device model."""

    model_id: str
    label: str
    service_uuid: str
    probe_count: int
    is_pulse: bool = False


MODELS: List[ModelInfo] = [
    ModelInfo("igrill_mini", "IGrill mini", IGRILL_MINI_TEMPERATURE_SERVICE_UUID, 1),
    ModelInfo("igrill_miniv2", "IGrill mini V2", IGRILL_MINIV2_TEMPERATURE_SERVICE_UUID, 1),
    ModelInfo("igrill_v2", "IGrill V2", IGRILLV2_TEMPERATURE_SERVICE_UUID, 4),
    ModelInfo("igrill_v202", "IGrill V202", IGRILLV202_TEMPERATURE_SERVICE_UUID, 4),
    ModelInfo("igrill_v3", "IGrill V3", IGRILLV3_TEMPERATURE_SERVICE_UUID, 4),
    ModelInfo(
        "idevices_kitchen",
        "iDevices Kitchen",
        IDEVICES_KITCHEN_TEMPERATURE_SERVICE_UUID,
        2,
    ),
    ModelInfo("pulse_1000", "Pulse 1000", PULSE_1000_TEMPERATURE_SERVICE_UUID, 2, True),
    ModelInfo("pulse_2000", "Pulse 2000", PULSE_2000_TEMPERATURE_SERVICE_UUID, 4, True),
]

# Fast lookup: lowercase service UUID -> ModelInfo
_MODEL_BY_UUID: Dict[str, ModelInfo] = {m.service_uuid.lower(): m for m in MODELS}

# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def detect_model(services) -> Optional[ModelInfo]:
    """Identify the device model from the list of advertised BLE services.

    Args:
        services: An iterable of objects that each expose a ``uuid`` attribute
            (as returned by Bleak's ``BleakClient.services``).

    Returns:
        The matching ``ModelInfo`` if a known temperature-service UUID is
        found among the advertised services, or ``None`` otherwise.
    """
    service_uuids = {service.uuid.lower() for service in services}
    for uuid in service_uuids:
        model = _MODEL_BY_UUID.get(uuid)
        if model is not None:
            return model
    return None
