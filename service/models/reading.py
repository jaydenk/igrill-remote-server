"""Parsing helpers and payload builders for iGrill sensor readings."""

from typing import Dict, Optional

UNPLUGGED_PROBE_CONSTANT = 63536


def parse_temperature_probe(index: int, data: bytes) -> Dict[str, object]:
    """Parse a 2-byte little-endian temperature value from a probe characteristic.

    Parameters
    ----------
    index:
        1-based probe number (matches the physical probe socket label).
    data:
        Raw bytes read from the BLE temperature characteristic.

    Returns
    -------
    dict with keys ``index``, ``temperature`` (float or None), ``raw``, and
    ``unplugged`` (bool or None when data is too short).
    """
    if len(data) < 2:
        return {"index": index, "temperature": None, "raw": None, "unplugged": None}
    raw = data[0] | (data[1] << 8)
    unplugged = raw == UNPLUGGED_PROBE_CONSTANT
    return {
        "index": index,
        "temperature": None if unplugged else float(raw),
        "raw": raw,
        "unplugged": unplugged,
    }


def parse_pulse_element(data: bytes) -> Dict[str, Optional[int]]:
    """Parse the Pulse heating-element characteristic.

    The characteristic contains four 3-byte ASCII-encoded integers at byte
    offsets 1, 5, 9 and 13, representing:

    * ``heating_actual1`` / ``heating_actual2`` -- current element temperatures
    * ``heating_setpoint1`` / ``heating_setpoint2`` -- target set-points
    """

    def _parse_slice(start: int) -> Optional[int]:
        if len(data) < start + 3:
            return None
        try:
            return int(data[start : start + 3].decode("ascii"))
        except (ValueError, UnicodeDecodeError):
            return None

    return {
        "heating_actual1": _parse_slice(1),
        "heating_actual2": _parse_slice(5),
        "heating_setpoint1": _parse_slice(9),
        "heating_setpoint2": _parse_slice(13),
    }


def build_reading_payload(
    device_entry: Dict[str, object],
    session_id: Optional[str],
    session_start_ts: Optional[str],
) -> Dict[str, object]:
    """Build the standardised reading payload broadcast over WebSocket.

    Parameters
    ----------
    device_entry:
        Snapshot of a single device from :class:`DeviceStore`.
    session_id:
        Current history session identifier.
    session_start_ts:
        ISO-8601 timestamp when the current session began.

    Returns
    -------
    dict with top-level keys ``sensorId``, ``sessionId``, ``sessionStartTs``,
    ``data`` and optionally ``q`` (quality metrics).
    """
    data = {
        "name": device_entry.get("name"),
        "model": device_entry.get("model"),
        "model_name": device_entry.get("model_name"),
        "session_id": session_id,
        "session_start_ts": session_start_ts,
        "last_update": device_entry.get("last_update"),
        "unit": device_entry.get("unit"),
        "battery_percent": device_entry.get("battery_percent"),
        "propane_percent": device_entry.get("propane_percent"),
        "probes": device_entry.get("probes", []),
        "connected_probes": device_entry.get("connected_probes", []),
        "probe_status": device_entry.get("probe_status"),
        "pulse": device_entry.get("pulse", {}),
        "error": device_entry.get("error"),
    }
    payload: Dict[str, object] = {
        "sensorId": device_entry.get("address"),
        "sessionId": session_id,
        "sessionStartTs": session_start_ts,
        "data": data,
    }
    q = {
        "rssi": device_entry.get("rssi"),
        "batteryPct": device_entry.get("battery_percent"),
    }
    if q["rssi"] is not None or q["batteryPct"] is not None:
        payload["q"] = q
    return payload
