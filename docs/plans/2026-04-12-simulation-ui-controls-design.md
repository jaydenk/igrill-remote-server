# Simulation UI Controls — Design

## Goal

Add start/stop simulation controls to the web dashboard Settings tab so users can trigger simulated cook sessions without using the API directly or iOS Shortcuts.

## Location

New "Simulation" panel in the **Settings tab**, below the existing "Log Levels" section. Always visible regardless of auth state — the server enforces auth on the API endpoints.

## Layout

### Idle State

```
┌─ Simulation ──────────────────────────────┐
│                                           │
│  Probes   [▼ 4    ]    Speed   [▼ 10x  ] │
│                                           │
│  [ ● Start Simulation ]                  │
│                                           │
└───────────────────────────────────────────┘
```

### Running State

```
┌─ Simulation ──────────────────────────────┐
│                                           │
│  Probes   [ 4    ]     Speed   [ 10x  ]  │  (disabled)
│                                           │
│  [ ■ Stop Simulation  ]                  │
│                                           │
└───────────────────────────────────────────┘
```

## Components

- **Probes dropdown:** Options 1, 2, 3, 4. Default: 4.
- **Speed dropdown:** Options 1x, 5x, 10x, 20x. Default: 10x.
- **Start/Stop button:** Accent colour for Start, red for Stop. Dropdowns disabled while running.
- **Error feedback:** Same flash pattern as log levels panel (green success / red error, fades after 2s).

## State Detection (WebSocket-aware)

No new backend endpoints or WebSocket message types needed.

- **On `session_start` events:** Check if the session's devices list contains `SIM:UL:AT:ED:00:01`. If so, flip the panel to running state.
- **On `session_end` events:** If the current simulation session matches, flip back to idle state.
- **On initial `status` response:** Check session devices for the simulated address to restore state on page load/reconnect.

## API Calls

- **Start:** `POST /api/v1/simulate/start` with JSON body `{"speed": <number>, "probes": <number>}`
- **Stop:** `POST /api/v1/simulate/stop` with no body

Both require `Authorization: Bearer <token>` header when `IGRILL_SESSION_TOKEN` is configured. On 401, display error flash.

## Backend Changes

None required. Existing simulation endpoints and WebSocket events are sufficient.

## Styling

Follows existing Settings tab conventions:
- Same `.settings-section` card style
- Same `.settings-label` / dropdown styling as log levels
- Same flash feedback mechanism
- Responsive: controls stack vertically on mobile (<600px)
