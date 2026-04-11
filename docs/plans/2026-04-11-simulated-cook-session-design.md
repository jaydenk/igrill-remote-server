# Simulated Cook Session

**Date:** 2026-04-11
**Status:** Approved

## Overview

An API-triggered simulation mode that generates realistic temperature readings without BLE hardware. Exercises the full pipeline — WebSocket broadcast, history recording, alert evaluation, push notifications, and Live Activity updates — using the same code paths as real devices.

## API

**Start:** `POST /api/v1/simulate/start`
- Requires authorisation (Bearer token).
- Optional JSON body:
  - `speed` (number, default `10`) — multiplier. `1` = real-time (one reading per `poll_interval`), `10` = 10x faster.
  - `probes` (integer, default `4`, range 1–4) — number of active probes.
- Returns: `{"ok": true, "sessionId": "...", "deviceAddress": "SIM:UL:AT:ED:00:01", "speed": 10, "probes": 4}`
- Error if a simulation is already running.

**Stop:** `POST /api/v1/simulate/stop`
- Requires authorisation.
- Ends the session and stops the background task.
- Returns: `{"ok": true, "sessionId": "...", "readings": <count>}`

## Simulated Device

- Address: `SIM:UL:AT:ED:00:01`
- Name: `Simulated iGrill V2`
- Model: `igrill_v2` / model_name: `iGrill V2`
- Battery: starts at 85%, decreases ~0.1% per reading.
- Unit: `C` (Celsius).

## Probes

The `probes` parameter controls how many are active. Inactive slots are unplugged.

| Probe | Label | Target Mode | Behaviour |
|-------|-------|-------------|-----------|
| 1 | Brisket | Fixed: 90°C | Logarithmic rise from 25°C, ±1.5°C noise. Alerts at ~85°C (approaching), 90°C (reached). |
| 2 | Ribs | Fixed: 80°C | Slightly faster logarithmic rise from 25°C, ±1.5°C noise. Reaches target after probe 1. |
| 3 | BBQ Temp | Range: 110–130°C | Fast linear ramp to ~135°C (overshoots), exponential decay to midpoint, stabilises within range with ±5°C fluctuation. |
| 4 | Pork Belly | Fixed: 75°C | Slowest logarithmic rise from 25°C, ±1°C noise. Reaches target last. |

### Temperature Model

**Fixed-target probes:** `T(t) = target - (target - start) * e^(-k*t) + noise`
- `k` varies per probe to control rise speed.
- `noise = uniform(-n, +n)` where `n` is the probe's noise amplitude.

**BBQ probe (range mode):**
- Phase 1 (ramp): linear rise at ~2°C/reading until overshoot (~135°C).
- Phase 2 (settle): exponential decay toward range midpoint (120°C).
- Phase 3 (steady): hold at midpoint with ±5°C random fluctuation.

## Architecture

**New file:** `service/simulate/runner.py`

**Class:** `SimulationRunner`
- Holds a reference to `DeviceStore`, `HistoryStore`, `AlertEvaluator`, and `Config`.
- `start(speed, probes)` — registers the fake device, starts a session with targets, launches the background `asyncio.Task`.
- `stop()` — cancels the task, ends the session, marks device disconnected.
- `is_running` property.

**Background task loop:**
1. Calculate current temperatures for each probe based on elapsed ticks.
2. Call `store.upsert(address, ...)` with the new readings.
3. Call `store.publish_reading({"seq": ..., "payload": build_reading_payload(...)})`.
4. Call `history.record_reading(...)` to persist to SQLite.
5. Sleep `poll_interval / speed` seconds.
6. Repeat.

**Routes:** Added to `service/api/routes.py` — `simulate_start_handler` and `simulate_stop_handler`.

**No changes to existing code** — the simulator uses the same `DeviceStore.publish_reading()` → `broadcast_readings()` → WebSocket pipeline as real devices. Alert evaluation happens automatically via the existing `AlertEvaluator`.

## Session Completion

The simulation runs indefinitely after all probes reach their targets (steady-state hold phase). It continues emitting readings until explicitly stopped via the stop endpoint. The session appears in history identically to a real cook.
