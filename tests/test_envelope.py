"""Round-trip tests for WebSocket envelope construction and message types."""

from __future__ import annotations

import json

import pytest

from service.api import envelope as env_mod
from service.api.envelope import (
    PROTOCOL_VERSION,
    TYPE_PROBE_TIMER_ACK,
    TYPE_PROBE_TIMER_REQUEST,
    TYPE_PROBE_TIMER_UPDATE,
    TYPE_SESSION_DISCARD_ACK,
    TYPE_SESSION_DISCARD_REQUEST,
    TYPE_SESSION_DISCARDED,
    TYPE_SESSION_NOTES_UPDATE,
    TYPE_SESSION_NOTES_UPDATE_ACK,
    TYPE_SESSION_NOTES_UPDATE_REQUEST,
    make_envelope,
)


def _roundtrip(env: dict) -> dict:
    """Serialise then deserialise an envelope via JSON to mimic the wire."""
    return json.loads(json.dumps(env))


@pytest.mark.parametrize(
    "msg_type,payload",
    [
        (
            TYPE_SESSION_DISCARD_REQUEST,
            {"sessionId": "sess-123"},
        ),
        (
            TYPE_SESSION_DISCARD_ACK,
            {"ok": True, "sessionId": "sess-123"},
        ),
        (
            TYPE_SESSION_DISCARDED,
            {"sessionId": "sess-123", "discardedAt": "2026-04-12T10:00:00Z"},
        ),
        (
            TYPE_PROBE_TIMER_REQUEST,
            {
                "sessionId": "sess-123",
                "deviceAddress": "AA:BB:CC:DD:EE:FF",
                "probeIndex": 1,
                "mode": "start",
                "durationSecs": 900,
            },
        ),
        (
            TYPE_PROBE_TIMER_ACK,
            {
                "ok": True,
                "sessionId": "sess-123",
                "deviceAddress": "AA:BB:CC:DD:EE:FF",
                "probeIndex": 1,
                "mode": "running",
            },
        ),
        (
            TYPE_PROBE_TIMER_UPDATE,
            {
                "sessionId": "sess-123",
                "deviceAddress": "AA:BB:CC:DD:EE:FF",
                "probeIndex": 1,
                "mode": "running",
                "startedAt": "2026-04-12T10:00:00Z",
                "durationSecs": 900,
            },
        ),
        (
            TYPE_SESSION_NOTES_UPDATE_REQUEST,
            {"sessionId": "sess-123", "notes": "Charcoal started at 1000"},
        ),
        (
            TYPE_SESSION_NOTES_UPDATE_ACK,
            {"ok": True, "sessionId": "sess-123"},
        ),
        (
            TYPE_SESSION_NOTES_UPDATE,
            {
                "sessionId": "sess-123",
                "notes": "Charcoal started at 1000",
                "updatedAt": "2026-04-12T10:00:00Z",
            },
        ),
    ],
)
def test_new_session_first_types_roundtrip(msg_type: str, payload: dict) -> None:
    """Each new session-first type encodes and decodes through JSON with
    fields preserved and the correct envelope metadata attached."""
    env = make_envelope(msg_type, payload, request_id="req-42")
    decoded = _roundtrip(env)

    assert decoded["v"] == PROTOCOL_VERSION
    assert decoded["type"] == msg_type
    assert decoded["payload"] == payload
    assert decoded["requestId"] == "req-42"
    assert isinstance(decoded["ts"], str) and decoded["ts"]


def test_broadcast_envelope_omits_request_id() -> None:
    """Broadcasts (no requestId, no seq) serialise without those fields."""
    env = make_envelope(
        TYPE_SESSION_DISCARDED,
        {"sessionId": "sess-123", "discardedAt": "2026-04-12T10:00:00Z"},
    )
    decoded = _roundtrip(env)
    assert "requestId" not in decoded
    assert "seq" not in decoded
    assert decoded["type"] == "session_discarded"


def test_new_type_constants_have_expected_wire_values() -> None:
    """Guard against accidental rename of wire-format strings."""
    assert env_mod.TYPE_SESSION_DISCARD_REQUEST == "session_discard_request"
    assert env_mod.TYPE_SESSION_DISCARD_ACK == "session_discard_ack"
    assert env_mod.TYPE_SESSION_DISCARDED == "session_discarded"
    assert env_mod.TYPE_PROBE_TIMER_REQUEST == "probe_timer_request"
    assert env_mod.TYPE_PROBE_TIMER_ACK == "probe_timer_ack"
    assert env_mod.TYPE_PROBE_TIMER_UPDATE == "probe_timer_update"
    assert env_mod.TYPE_SESSION_NOTES_UPDATE_REQUEST == "session_notes_update_request"
    assert env_mod.TYPE_SESSION_NOTES_UPDATE_ACK == "session_notes_update_ack"
    assert env_mod.TYPE_SESSION_NOTES_UPDATE == "session_notes_update"
