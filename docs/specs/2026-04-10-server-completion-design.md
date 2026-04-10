# Server Completion: Web UI, Export, CI/CD, Extended Testing

**Date:** 2026-04-10
**Status:** Approved

## 1. Web UI Updates

Update `service/web/static/index.html` to surface the session enrichment fields (name, labels, notes) that were added to the server data model earlier today.

### Session banner (Live tab)
- When a session is active and has a name, display it prominently in the banner (e.g. bold text above the duration/devices row).
- The `status` response already includes `currentSessionName` — wire it to the UI.

### Session start flow
- Add a text input for "Session name" (optional) above the "Start Session" button in the idle banner.
- Pass `name` in the `session_start_request` payload when the user clicks Start.

### Probe cards
- If the active session has targets with labels, show the label below the "Probe N" heading in each probe card.
- Labels come from the `activeTargets` array in the `status` response, keyed by `probe_index`.

### History list
- Each session card shows name (or falls back to formatted date) as the primary text.
- Show a truncated notes preview (first ~80 chars) if present.
- The `sessions` response already includes `name` and `notes` — wire to the card template.

### History detail
- Show session name as the detail heading.
- Show notes as a subtitle paragraph below the heading.
- In the summary grid, show target labels alongside "Probe N".
- Targets in the detail response already include `label` — wire to the summary template.

### Notes editing
- Add a textarea below the session detail heading that shows `notes`.
- On blur or Enter, send a `session_update_request` with the new notes content.
- Provide visual feedback (brief "Saved" indicator).

## 2. Export Endpoint

### Route
`GET /api/sessions/{id}/export?format=csv`

### Behaviour
- Default format (no query param or `format=json`): returns JSON array of enriched readings with target labels merged.
- `format=csv`: returns CSV with columns: `timestamp,probe_index,label,temperature_c,battery_pct,propane_pct`.
- Sets `Content-Disposition: attachment; filename="session-{name-or-id}.csv"` for CSV.
- Returns 404 if the session doesn't exist.
- No auth required (matches existing REST API policy).

### Implementation
- New handler `export_handler` in `service/api/routes.py`.
- New route registered in `setup_routes`.
- Reads from `history.get_session_readings(session_id)` and `history.get_targets(session_id)` to merge labels.

## 3. CI/CD

- Push `iGrillRemoteServer` main branch to origin.
- GitHub Actions pipeline (`.github/workflows/ci.yml`) builds multi-arch Docker image and pushes to GHCR.
- Push `iGrillRemoteApp` main branch to origin.
- On pimento: `docker compose pull && docker compose up -d` to deploy the official image.
- Verify: health check returns ok, migration v2 columns exist, session enrichment fields round-trip.

## 4. Extended Testing

- Start a TestClient session against pimento with the iGrill active.
- Run for 30 minutes with readings flowing.
- Monitor server logs for WARNINGs, errors, or unexpected state changes.
- Count readings received vs expected (~100 at 18s cadence).
- Verify no memory growth (check container stats before/after).
