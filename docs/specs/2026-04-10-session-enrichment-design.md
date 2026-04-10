# Session Enrichment: Probe Labels, Session Name, Cook Notes

**Date:** 2026-04-10
**Status:** Approved
**Scope:** Server (schema + protocol + web UI), iOS client (models + views)

## Summary

Add three metadata fields to the session model so cooks have human-readable context in both the web UI and the iOS app:

1. **Session name** — optional title (e.g. "Sunday Brisket")
2. **Probe labels** — per-target free-text label (e.g. "Brisket Point", "Pit Temp")
3. **Cook notes** — single free-text field attached to the session

All three are part of the free tier (server + web UI). The iOS app surfaces them with richer UX but does not add server-side logic.

## Data Model

### Session = one cook

A session represents a single cook. Session name and notes attach to the session row. Probe labels attach to the per-target config (one label per target per session).

### Schema Changes

Two new nullable columns on `sessions`:

```sql
ALTER TABLE sessions ADD COLUMN name TEXT;
ALTER TABLE sessions ADD COLUMN notes TEXT;
```

One new nullable column on `targets`:

```sql
ALTER TABLE targets ADD COLUMN label TEXT;
```

These are added via the existing sequential migration runner in `service/db/migrations.py`.

## Protocol Changes

### session_start_request — extended payload

```json
{
  "v": 2,
  "type": "session_start_request",
  "requestId": "...",
  "payload": {
    "deviceAddresses": ["70:91:8F:9B:69:17"],
    "name": "Sunday Brisket",
    "targets": [
      {
        "probe_index": 1,
        "label": "Brisket Point",
        "mode": "fixed",
        "target_value": 93,
        "pre_alert_offset": 5,
        "reminder_interval_secs": 300
      },
      {
        "probe_index": 2,
        "label": "Pit Temp",
        "mode": "range",
        "range_low": 107,
        "range_high": 121,
        "pre_alert_offset": 5,
        "reminder_interval_secs": 0
      }
    ]
  }
}
```

New fields: `name` (optional string), per-target `label` (optional string). Backwards-compatible — omitting them behaves exactly as today.

### New message: session_update_request

Allows updating name and/or notes at any time during or after a session. Useful for adding cook notes during or after a cook. The session does not need to be active — users can annotate completed sessions too.

```json
{
  "v": 2,
  "type": "session_update_request",
  "requestId": "...",
  "payload": {
    "sessionId": "abc123",
    "name": "Sunday Brisket",
    "notes": "Trimmed fat cap to 1/4 inch. Oak and cherry wood. Started at 120°C pit, bumped to 135°C after the stall."
  }
}
```

- `sessionId` is optional — defaults to the current active session if omitted.
- Either `name` or `notes` (or both) may be provided. Omitted fields are not changed.
- If `sessionId` is provided and refers to a completed session, the update still succeeds (for post-cook annotation).
- Requires authorisation if `IGRILL_SESSION_TOKEN` is configured.

Response:

```json
{
  "v": 2,
  "type": "session_update_ack",
  "payload": {
    "ok": true,
    "sessionId": "abc123",
    "name": "Sunday Brisket",
    "notes": "Trimmed fat cap to 1/4 inch..."
  }
}
```

### Extended responses

The following existing responses include the new fields:

| Message type | New fields added |
|---|---|
| `session_start_ack` | `name` |
| `status` | `currentSessionName` in the status payload |
| `sessions` (list) | Each session object includes `name` and `notes` |
| `session_detail` (REST) | `name`, `notes`, and per-target `label` |
| `reading` | No change — readings stay keyed by probe index |
| Alert payloads | `target.label` included in the existing target dict within alerts |

### target_update_request — extended

Per-target `label` is also accepted in `target_update_request`, following the same shape as `session_start_request` targets. Updating the label mid-session is permitted.

## What Does NOT Change

- **Alert evaluator** — labels are display-only metadata. Alert logic operates on probe_index and temperature only.
- **DeviceWorker / BLE layer** — no awareness of labels, names, or notes.
- **Reading payloads** — probe readings stay keyed by `index` (1-based integer). Labels are looked up client-side from the target config.
- **Session lifecycle** — start/end/add-device flow is unchanged. Only the payload shapes grow.
- **History recording** — per-probe readings table is unchanged. Labels are stored in the targets table, not in readings.

## Server Implementation

### Files changed

| File | Change |
|---|---|
| `service/db/migrations.py` | New migration adding `name`, `notes` to sessions and `label` to targets |
| `service/models/session.py` | `TargetConfig` gains a `label` field |
| `service/history/store.py` | `start_session()` accepts `name`; new `update_session()` method for name/notes |
| `service/api/websocket.py` | New `_handle_session_update` handler; extend `_handle_session_start` to pass `name`; extend status/sessions responses |
| `service/api/routes.py` | Extend `session_detail_handler` response to include name/notes/labels |
| `service/web/static/index.html` | Show session name in dashboard, display labels on probes, add notes textarea |

### Files NOT changed

| File | Why |
|---|---|
| `service/ble/*` | BLE layer has no concept of session metadata |
| `service/alerts/evaluator.py` | Labels don't affect alert evaluation |
| `service/models/device.py` | Device store doesn't hold labels |
| `service/models/reading.py` | Reading payloads are label-free |

## iOS Client Implementation

### Files changed

| File | Change |
|---|---|
| `Models/Session.swift` | `TargetSetting` gains a `label` field; `toServerDict()` includes it |
| `Models/ServerMessage.swift` | Decode `name` from session-related payloads; decode `label` from target payloads |
| `Services/WebSocketService.swift` | New `sendSessionUpdate(name:notes:sessionId:)` method |
| `Views/SessionStartView.swift` | Text field for session name, text field for per-probe label |
| `ViewModels/SessionViewModel.swift` | Hold session name, call `sendSessionUpdate` for notes |
| `Views/HistoryListView.swift` | Display session name instead of (or alongside) UUID/timestamp |

## Testing

### Server tests (pytest)

- `test_history_store.py`: new tests for `start_session(name=...)` and `update_session(name=..., notes=...)`.
- `test_models.py`: `TargetConfig.from_dict` / `to_dict` round-trip with `label`.
- `test_integration.py`: session detail endpoint returns name/notes/labels.

### TestClient e2e scenarios

- New scenario: start a session with name and labels, verify `session_start_ack` includes them.
- New scenario: send `session_update_request` with notes, verify `session_update_ack`.
- New scenario: verify `sessions` list includes name/notes after session end.

## Migration Safety

The new columns are all nullable `TEXT`. No NOT NULL constraints, no default values that could conflict with existing rows. The migration is additive-only — safe to run on a database with existing sessions. Existing sessions will have `name=NULL, notes=NULL` and existing targets will have `label=NULL`.

## Free vs Paid Applicability

All of this is data-layer. It lives in the free server + web UI:

- **Free (server):** Store and serve names, labels, notes. Display in web dashboard.
- **Paid (iOS):** Same data, richer UX — cook preset library (populates name + targets + labels from a template), label suggestions ("Brisket", "Ribs", "Pit", "Ambient"), notes with Markdown or photos (client-side rendering), session sharing.
