# Session Enrichment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add session name, per-target probe labels, and cook notes to the server data model, WebSocket protocol, and REST API.

**Architecture:** Three nullable TEXT columns added via migration (2 on `sessions`, 1 on `session_targets`). One new WS message type (`session_update_request`/`ack`). Existing message payloads extended. No new tables, no BLE changes, no alert evaluator changes.

**Tech Stack:** Python 3.12 / aiohttp / aiosqlite / pytest / pytest-asyncio

**Spec:** `docs/specs/2026-04-10-session-enrichment-design.md`

---

### Task 1: Schema migration — add name, notes, label columns

**Files:**
- Modify: `service/db/migrations.py`
- Test: `tests/test_schema.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_schema.py`:

```python
@pytest.mark.asyncio
async def test_session_enrichment_columns(tmp_path):
    """Migration v2 adds name/notes to sessions and label to session_targets."""
    db_path = str(tmp_path / "test.db")
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        from service.db.schema import init_db
        await init_db(conn)
        from service.db.migrations import run_migrations
        await run_migrations(conn)

        # Verify sessions columns
        cursor = await conn.execute("PRAGMA table_info(sessions)")
        cols = {row[1] for row in await cursor.fetchall()}
        assert "name" in cols, "sessions.name column missing"
        assert "notes" in cols, "sessions.notes column missing"

        # Verify session_targets column
        cursor = await conn.execute("PRAGMA table_info(session_targets)")
        cols = {row[1] for row in await cursor.fetchall()}
        assert "label" in cols, "session_targets.label column missing"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_schema.py::test_session_enrichment_columns -v`
Expected: FAIL — columns don't exist yet

- [ ] **Step 3: Add migration v2**

In `service/db/migrations.py`, replace the empty `MIGRATIONS` dict:

```python
MIGRATIONS: dict[int, list[str]] = {
    2: [
        "ALTER TABLE sessions ADD COLUMN name TEXT",
        "ALTER TABLE sessions ADD COLUMN notes TEXT",
        "ALTER TABLE session_targets ADD COLUMN label TEXT",
    ],
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_schema.py::test_session_enrichment_columns -v`
Expected: PASS

- [ ] **Step 5: Run full test suite**

Run: `python -m pytest tests/ -v`
Expected: All tests pass (including existing schema tests)

- [ ] **Step 6: Commit**

```bash
git add service/db/migrations.py tests/test_schema.py
git commit -m "feat(db): add migration v2 — session name, notes, target label columns"
```

---

### Task 2: TargetConfig.label field

**Files:**
- Modify: `service/models/session.py`
- Test: `tests/test_models.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_models.py` in the `TestTargetConfig` class:

```python
def test_label_field(self):
    """TargetConfig round-trips a label through from_dict/to_dict."""
    data = {
        "probe_index": 1,
        "mode": "fixed",
        "target_value": 93.0,
        "label": "Brisket Point",
    }
    tc = TargetConfig.from_dict(data)
    assert tc.label == "Brisket Point"
    d = tc.to_dict()
    assert d["label"] == "Brisket Point"

def test_label_defaults_none(self):
    """TargetConfig.label defaults to None when omitted."""
    data = {"probe_index": 1, "mode": "fixed", "target_value": 93.0}
    tc = TargetConfig.from_dict(data)
    assert tc.label is None
    d = tc.to_dict()
    assert d["label"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_models.py::TestTargetConfig::test_label_field tests/test_models.py::TestTargetConfig::test_label_defaults_none -v`
Expected: FAIL — `label` attribute doesn't exist

- [ ] **Step 3: Add label to TargetConfig**

In `service/models/session.py`, add the field to the dataclass:

```python
@dataclass
class TargetConfig:
    probe_index: int
    mode: str
    target_value: Optional[float] = None
    range_low: Optional[float] = None
    range_high: Optional[float] = None
    pre_alert_offset: float = 10.0
    reminder_interval_secs: int = 300
    label: Optional[str] = None  # NEW
```

In `from_dict`, after `reminder_interval_secs` parsing, add:

```python
        label = data.get("label")
        if label is not None:
            label = str(label)
```

And pass `label=label` to the `cls(...)` constructor call.

In `to_dict`, add `"label": self.label` to the returned dict.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_models.py::TestTargetConfig -v`
Expected: All TestTargetConfig tests pass

- [ ] **Step 5: Commit**

```bash
git add service/models/session.py tests/test_models.py
git commit -m "feat(model): add label field to TargetConfig"
```

---

### Task 3: HistoryStore — session name, notes, update method

**Files:**
- Modify: `service/history/store.py`
- Test: `tests/test_history_store.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_history_store.py`:

```python
@pytest.mark.asyncio
async def test_start_session_with_name(store, sample_address):
    """start_session accepts an optional name parameter."""
    result = await store.start_session([sample_address], "user", name="Sunday Brisket")
    sid = result["session_id"]
    # Verify name is in the DB
    async with store._lock:
        cursor = await store._conn.execute(
            "SELECT name FROM sessions WHERE id = ?", (sid,)
        )
        row = await cursor.fetchone()
    assert row["name"] == "Sunday Brisket"


@pytest.mark.asyncio
async def test_update_session_name_and_notes(store, sample_address):
    """update_session sets name and notes on an existing session."""
    result = await store.start_session([sample_address], "user")
    sid = result["session_id"]
    await store.update_session(sid, name="Sunday Brisket", notes="Oak and cherry.")
    async with store._lock:
        cursor = await store._conn.execute(
            "SELECT name, notes FROM sessions WHERE id = ?", (sid,)
        )
        row = await cursor.fetchone()
    assert row["name"] == "Sunday Brisket"
    assert row["notes"] == "Oak and cherry."


@pytest.mark.asyncio
async def test_update_session_partial(store, sample_address):
    """update_session only updates provided fields."""
    result = await store.start_session([sample_address], "user", name="Original")
    sid = result["session_id"]
    await store.update_session(sid, notes="Added notes only")
    async with store._lock:
        cursor = await store._conn.execute(
            "SELECT name, notes FROM sessions WHERE id = ?", (sid,)
        )
        row = await cursor.fetchone()
    assert row["name"] == "Original"  # unchanged
    assert row["notes"] == "Added notes only"


@pytest.mark.asyncio
async def test_list_sessions_includes_name_notes(store, sample_address):
    """list_sessions includes name and notes in the returned dicts."""
    await store.start_session([sample_address], "user", name="Cook 1")
    await store.update_session(
        (await store.get_session_state())["current_session_id"],
        notes="Tasty"
    )
    sessions = await store.list_sessions(limit=5)
    assert sessions[0]["name"] == "Cook 1"
    assert sessions[0]["notes"] == "Tasty"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_history_store.py::test_start_session_with_name tests/test_history_store.py::test_update_session_name_and_notes tests/test_history_store.py::test_update_session_partial tests/test_history_store.py::test_list_sessions_includes_name_notes -v`
Expected: FAIL — `name` parameter not accepted, `update_session` doesn't exist

- [ ] **Step 3: Implement changes in store.py**

**3a.** Modify `start_session` signature to accept `name`:

```python
async def start_session(
    self, addresses: list[str], reason: str, name: Optional[str] = None
) -> dict:
```

Change the INSERT to include name:

```python
await self._conn.execute(
    "INSERT INTO sessions (id, started_at, start_reason, name) VALUES (?, ?, ?, ?)",
    (session_id, now_ts, reason, name),
)
```

Add `name` to the returned `start_event`:

```python
start_event = {
    "sessionId": session_id,
    "sessionStartTs": now_ts,
    "reason": reason,
    "name": name,
}
```

**3b.** Add `update_session` method (after `end_session`):

```python
async def update_session(
    self,
    session_id: str,
    name: Optional[str] = None,
    notes: Optional[str] = None,
) -> Optional[dict]:
    """Update name and/or notes on an existing session.

    Only the provided fields are changed; None values are skipped.
    Returns the updated fields dict, or None if the session doesn't exist.
    """
    async with self._lock:
        cursor = await self._conn.execute(
            "SELECT id FROM sessions WHERE id = ?", (session_id,)
        )
        if await cursor.fetchone() is None:
            return None

        updates: list[str] = []
        params: list[object] = []
        if name is not None:
            updates.append("name = ?")
            params.append(name)
        if notes is not None:
            updates.append("notes = ?")
            params.append(notes)

        if updates:
            params.append(session_id)
            await self._conn.execute(
                f"UPDATE sessions SET {', '.join(updates)} WHERE id = ?",
                params,
            )
            await self._conn.commit()

        # Fetch final state
        cursor = await self._conn.execute(
            "SELECT name, notes FROM sessions WHERE id = ?",
            (session_id,),
        )
        row = await cursor.fetchone()
        return {"name": row["name"], "notes": row["notes"]}
```

**3c.** Modify `list_sessions` to include name and notes:

In the first SELECT query, add `s.name, s.notes`:

```python
cursor = await self._conn.execute(
    "SELECT s.id, s.started_at, s.ended_at, s.start_reason, s.end_reason, "
    "s.name, s.notes, "
    "(SELECT COUNT(*) FROM probe_readings pr WHERE pr.session_id = s.id) AS reading_count "
    "FROM sessions s "
    "ORDER BY s.started_at DESC "
    "LIMIT ? OFFSET ?",
    (limit, offset),
)
```

And in the results dict, add the fields:

```python
results.append(
    {
        "sessionId": row["id"],
        "startTs": row["started_at"],
        "endTs": row["ended_at"],
        "startReason": row["start_reason"],
        "endReason": row["end_reason"],
        "name": row["name"],
        "notes": row["notes"],
        "readingCount": row["reading_count"],
        "devices": devices_by_session.get(row["id"], []),
    }
)
```

**3d.** Modify `save_targets` and `get_targets` to include label.

`save_targets` INSERT — add `label` column:

```python
await self._conn.execute(
    "INSERT OR REPLACE INTO session_targets "
    "(session_id, address, probe_index, mode, target_value, "
    "range_low, range_high, pre_alert_offset, reminder_interval_secs, label) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    (
        session_id, address, t.probe_index, t.mode,
        t.target_value, t.range_low, t.range_high,
        t.pre_alert_offset, t.reminder_interval_secs,
        t.label,
    ),
)
```

`get_targets` SELECT — add `label`:

```python
cursor = await self._conn.execute(
    "SELECT probe_index, mode, target_value, range_low, range_high, "
    "pre_alert_offset, reminder_interval_secs, label "
    "FROM session_targets WHERE session_id = ?",
    (session_id,),
)
```

And in the TargetConfig constructor, add:

```python
label=r["label"],
```

Also update `update_targets` the same way as `save_targets` (add `label` to INSERT and params).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_history_store.py -v`
Expected: All tests pass

- [ ] **Step 5: Run full suite**

Run: `python -m pytest tests/ -v`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add service/history/store.py tests/test_history_store.py
git commit -m "feat(history): session name, notes, target labels in store"
```

---

### Task 4: WebSocket — session_update handler, extended start/status

**Files:**
- Modify: `service/api/websocket.py`
- Test: `tests/test_integration.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_integration.py`:

```python
@pytest.mark.asyncio
async def test_ws_session_update(aiohttp_client, app):
    """session_update_request sets name and notes on a session."""
    client = await aiohttp_client(app)
    async with client.ws_connect("/ws") as ws:
        # Start a session first
        await ws.send_json({
            "v": 2, "type": "session_start_request", "requestId": "r1",
            "payload": {
                "name": "Test Cook",
                "targets": [{"probe_index": 1, "mode": "fixed", "target_value": 90}],
            },
        })
        start_ack = await ws.receive_json()
        # Skip broadcasts until we find the ack
        while start_ack.get("type") != "session_start_ack":
            start_ack = await ws.receive_json()
        session_id = start_ack["payload"]["sessionId"]
        assert start_ack["payload"].get("name") == "Test Cook"

        # Update notes
        await ws.send_json({
            "v": 2, "type": "session_update_request", "requestId": "r2",
            "payload": {"sessionId": session_id, "notes": "Oak wood."},
        })
        update_ack = await ws.receive_json()
        while update_ack.get("type") != "session_update_ack":
            update_ack = await ws.receive_json()
        assert update_ack["payload"]["ok"] is True
        assert update_ack["payload"]["notes"] == "Oak wood."
        assert update_ack["payload"]["name"] == "Test Cook"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_integration.py::test_ws_session_update -v`
Expected: FAIL — session_update_request not handled

- [ ] **Step 3: Implement WebSocket changes**

**3a.** In `_handle_session_start`, pass `name` from payload to `history.start_session`:

```python
name = ctx.payload.get("name")
session_info = await ctx.history.start_session(
    addresses=device_addresses, reason="user", name=name
)
```

Add `name` to `response_payload`:

```python
response_payload: dict[str, Any] = {
    "ok": True,
    "sessionId": new_session_id,
    "sessionStartTs": session_info["session_start_ts"],
    "name": name,
    "devices": device_addresses,
    "targets": [t.to_dict() for t in targets],
}
```

**3b.** Add new handler:

```python
async def _handle_session_update(ctx: _MessageContext) -> None:
    if not ctx.authorized:
        await send_error(
            ctx.ws, "unauthorized", "Not allowed to update sessions",
            request_id=ctx.request_id,
        )
        return

    session_id = ctx.payload.get("sessionId")
    if session_id is None:
        session_state = await ctx.history.get_session_state()
        session_id = session_state.get("current_session_id")
    if session_id is None:
        await send_error(
            ctx.ws, "no_session",
            "No sessionId provided and no active session.",
            request_id=ctx.request_id,
        )
        return

    name = ctx.payload.get("name")
    notes = ctx.payload.get("notes")

    result = await ctx.history.update_session(session_id, name=name, notes=notes)
    if result is None:
        await send_error(
            ctx.ws, "session_not_found",
            f"Session {session_id} does not exist.",
            request_id=ctx.request_id,
        )
        return

    await send_envelope(
        ctx.ws, "session_update_ack",
        {
            "ok": True,
            "sessionId": session_id,
            "name": result["name"],
            "notes": result["notes"],
        },
        request_id=ctx.request_id,
    )
```

**3c.** Register the handler in `_MESSAGE_HANDLERS`:

```python
_MESSAGE_HANDLERS: dict[str, Any] = {
    "status_request": _handle_status,
    "sessions_request": _handle_sessions,
    "history_request": _handle_history,
    "session_start_request": _handle_session_start,
    "session_end_request": _handle_session_end,
    "target_update_request": _handle_target_update,
    "session_add_device_request": _handle_session_add_device,
    "session_update_request": _handle_session_update,  # NEW
}
```

Add `"session_update_request"` to `_SESSION_CONTROL_TYPES` for rate limiting.

**3d.** In `_handle_status`, add `currentSessionName`:

```python
status_payload["currentSessionName"] = None
if current_sid is not None:
    cursor = await ctx.history._conn.execute(
        "SELECT name FROM sessions WHERE id = ?", (current_sid,)
    )
    row = await cursor.fetchone()
    if row:
        status_payload["currentSessionName"] = row["name"]
```

Wait — accessing `_conn` directly from the handler is poor encapsulation. Better: add a helper to HistoryStore:

```python
async def get_session_name(self, session_id: str) -> Optional[str]:
    async with self._lock:
        cursor = await self._conn.execute(
            "SELECT name FROM sessions WHERE id = ?", (session_id,)
        )
        row = await cursor.fetchone()
        return row["name"] if row else None
```

Then in `_handle_status`:

```python
if current_sid is not None:
    status_payload["currentSessionName"] = await ctx.history.get_session_name(current_sid)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_integration.py -v`
Expected: All pass

- [ ] **Step 5: Run full suite**

Run: `python -m pytest tests/ -v`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add service/api/websocket.py service/history/store.py tests/test_integration.py
git commit -m "feat(ws): add session_update handler, name in start/status"
```

---

### Task 5: REST — extend session detail endpoint

**Files:**
- Modify: `service/api/routes.py`
- Modify: `service/history/store.py` (minor — add name/notes to session detail query)
- Test: `tests/test_integration.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_integration.py`:

```python
@pytest.mark.asyncio
async def test_session_detail_includes_name_notes(aiohttp_client, app):
    """GET /api/sessions/{id} includes name, notes, and target labels."""
    # Create a session with name via WS
    client = await aiohttp_client(app)
    async with client.ws_connect("/ws") as ws:
        await ws.send_json({
            "v": 2, "type": "session_start_request", "requestId": "r1",
            "payload": {
                "name": "REST Test",
                "targets": [
                    {"probe_index": 1, "mode": "fixed", "target_value": 80, "label": "Brisket"},
                ],
            },
        })
        ack = await ws.receive_json()
        while ack.get("type") != "session_start_ack":
            ack = await ws.receive_json()
        session_id = ack["payload"]["sessionId"]

        # End session
        await ws.send_json({
            "v": 2, "type": "session_end_request", "requestId": "r2",
            "payload": {},
        })
        end_ack = await ws.receive_json()
        while end_ack.get("type") != "session_end_ack":
            end_ack = await ws.receive_json()

    # Query REST endpoint
    resp = await client.get(f"/api/sessions/{session_id}")
    assert resp.status == 200
    body = await resp.json()
    assert body.get("name") == "REST Test"
    assert body["targets"][0]["label"] == "Brisket"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_integration.py::test_session_detail_includes_name_notes -v`
Expected: FAIL — `name` not in response

- [ ] **Step 3: Implement**

In `service/api/routes.py`, modify `session_detail_handler` to fetch and include name/notes:

```python
async def session_detail_handler(request: web.Request) -> web.Response:
    history: HistoryStore = request.app["history"]
    session_id = request.match_info["id"]
    if not await history.session_exists(session_id):
        return web.json_response({"error": "session not found"}, status=404)
    readings = await history.get_session_readings(session_id)
    targets = await history.get_targets(session_id)
    devices = await history.get_session_devices(session_id)

    # Fetch name and notes
    name = await history.get_session_name(session_id)
    notes = await history.get_session_notes(session_id)

    return web.json_response({
        "sessionId": session_id,
        "name": name,
        "notes": notes,
        "devices": devices,
        "targets": [t.to_dict() for t in targets],
        "readings": readings,
    })
```

Add `get_session_notes` helper to `store.py` (alongside `get_session_name`):

```python
async def get_session_notes(self, session_id: str) -> Optional[str]:
    async with self._lock:
        cursor = await self._conn.execute(
            "SELECT notes FROM sessions WHERE id = ?", (session_id,)
        )
        row = await cursor.fetchone()
        return row["notes"] if row else None
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/ -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add service/api/routes.py service/history/store.py tests/test_integration.py
git commit -m "feat(rest): include name, notes, labels in session detail"
```

---

### Task 6: TestClient e2e scenarios + iOS model update

**Files:**
- Modify: `TestClient/Sources/TestClient/main.swift` (add `name` and `update` commands)
- Create: `TestClient/tests/16-session-enrichment.txt`
- Modify: `iGrill Remote/Models/Session.swift` (add `label` to TargetSetting)
- Modify: `iGrill Remote/Services/WebSocketService.swift` (add `sendSessionUpdate`)

- [ ] **Step 1: Add `update` command to TestClient main.swift**

```swift
case "update":
    guard parts.count >= 2 else {
        log("usage: update <name|notes> <value...>")
        return
    }
    let field = parts[1]
    let value = parts.dropFirst(2).joined(separator: " ")
    switch field {
    case "name":
        log("» session_update_request name=\(value)")
        await service.sendSessionUpdate(name: value, notes: nil, sessionId: nil)
    case "notes":
        log("» session_update_request notes=\(value)")
        await service.sendSessionUpdate(name: nil, notes: value, sessionId: nil)
    default:
        log("usage: update <name|notes> <value...>")
    }
```

- [ ] **Step 2: Add `sendSessionUpdate` to WebSocketService.swift**

```swift
func sendSessionUpdate(name: String?, notes: String?, sessionId: String?) {
    var payload: [String: Any] = [:]
    if let name { payload["name"] = name }
    if let notes { payload["notes"] = notes }
    if let sessionId { payload["sessionId"] = sessionId }
    send(type: "session_update_request", payload: payload, requestId: UUID().uuidString)
}
```

- [ ] **Step 3: Add `label` to TargetSetting in Session.swift**

```swift
nonisolated struct TargetSetting: Codable, Equatable, Sendable {
    var mode: TargetMode
    var unit: TemperatureUnit
    var targetValue: Double?
    var rangeLow: Double?
    var rangeHigh: Double?
    var preAlertOffset: Double = 10.0
    var reminderIntervalSecs: Int = 300
    var label: String?  // NEW

    func toServerDict() -> [String: Any] {
        var dict: [String: Any] = [
            "mode": mode.rawValue,
            "pre_alert_offset": preAlertOffset,
            "reminder_interval_secs": reminderIntervalSecs,
        ]
        if let label { dict["label"] = label }
        // ... rest unchanged
    }
}
```

- [ ] **Step 4: Extend `start` command to accept name**

Modify the `start` command in main.swift to accept a 7th positional arg for session name:

```swift
let sessionName = parts.count >= 7 ? parts[6...].joined(separator: " ") : nil
```

Pass it to `sendSessionStart` (requires extending that method to accept `name`).

- [ ] **Step 5: Write e2e test scenario**

Create `TestClient/tests/16-session-enrichment.txt`:

```
# Test 16: session enrichment — name, labels, notes.
echo TEST-16-START
wait 0.5
start 70:91:8F:9B:69:17 30 1 5 0 Sunday Brisket
wait 2
update notes Oak and cherry wood. Trimmed fat cap.
wait 2
status
wait 2
end
wait 2
sessions 5
wait 1
echo TEST-16-END
quit
```

- [ ] **Step 6: Build and run test**

```bash
cd TestClient && swift build && ./.build/debug/TestClient igrill.pimento.home.kerr.host < tests/16-session-enrichment.txt
```

Expected: session_start_ack includes name, session_update_ack returns notes, status shows currentSessionName, sessions list shows name+notes.

- [ ] **Step 7: Commit**

```bash
# In iGrillRemoteApp repo
git add TestClient/ "iGrill Remote/Models/Session.swift" "iGrill Remote/Services/WebSocketService.swift"
git commit -m "feat(client): session name, labels, notes in TestClient and iOS models"
```

---

### Task 7: Deploy and verify against live server

**Files:** None (deployment only)

- [ ] **Step 1: Deploy server changes to pimento**

```bash
scp service/db/migrations.py service/models/session.py service/history/store.py service/api/websocket.py service/api/routes.py pimento:/tmp/
ssh pimento "docker cp /tmp/migrations.py igrill-service:/app/service/db/migrations.py && \
             docker cp /tmp/session.py igrill-service:/app/service/models/session.py && \
             docker cp /tmp/store.py igrill-service:/app/service/history/store.py && \
             docker cp /tmp/websocket.py igrill-service:/app/service/api/websocket.py && \
             docker cp /tmp/routes.py igrill-service:/app/service/api/routes.py && \
             docker restart igrill-service"
```

- [ ] **Step 2: Verify migration ran**

```bash
ssh pimento "docker logs --tail 20 igrill-service" | grep -i migration
```

Expected: "Applying schema migration v2" and "Schema migration v2 applied successfully"

- [ ] **Step 3: Run TestClient e2e test 16**

```bash
cd TestClient && ./.build/debug/TestClient igrill.pimento.home.kerr.host < tests/16-session-enrichment.txt
```

Expected: All enrichment fields round-trip correctly.

- [ ] **Step 4: Run full e2e suite**

```bash
cd TestClient && ./tests/run-all.sh
```

Expected: All tests pass (including new test 16).
