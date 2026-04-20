# 2026-04-20 — Testing Findings: Server Fixes

First real-iPhone end-to-end test of the session-first flow, using the
new simulator-as-device runner (commit `a21415c`). Four server-side
issues, all around APNS alert delivery and correctness. Prioritised by
user impact.

---

## F1 — APNS alerts deferred until phone wake [blocker]

**Symptom:** Target-approaching / target-reached notifications did not
appear on the lock screen until the user tapped the screen. On screen
wake they landed bundled (screenshot shows a `2` badge on the Target
Exceeded notification — multiple deliveries queued during the deferral).

**Root cause hypothesis:** APNS request is being sent without
`apns-priority: 10` and/or without `interruption-level: time-sensitive`
in the aps payload. Without time-sensitive, iOS defers delivery to the
next user interaction — exactly the behaviour observed.

**Fix plan:**
1. Audit `service/push/service.py` `send_alert()` path — confirm the
   APNS request headers and aps payload shape currently being sent.
2. For **alerts** (target-approaching, target-reached, reminder,
   timer-complete) set:
   - header `apns-priority: 10`
   - header `apns-push-type: alert`
   - aps `"interruption-level": "time-sensitive"`
   - aps `"sound": "default"`
3. Leave **Live Activity** updates at `apns-priority: 5`,
   `apns-push-type: liveactivity` (unchanged — they're not alerts).
4. iOS target side (cross-reference in the app plan): ensure the
   entitlement `com.apple.developer.usernotifications.time-sensitive`
   is present in the app's `.entitlements` so iOS actually honours the
   interruption level.

**Verify:** Phone locked, screen off → start a simulated session with a
low target. Target-reached push must fire immediately on the lock
screen, with the time-sensitive badge visible in Notification Centre.
Log the APNS response and confirm `apns-id` appears on-device within
seconds of the server-side event timestamp.

---

## F2 — Reminder interval (180s) did not repeat [blocker]

**Symptom:** User received one target-reached notification and then no
follow-ups over the next several minutes. The configured reminder
interval is 180s.

**Root cause hypothesis:** `AlertEvaluator` emits `target_reached` on
threshold crossing but the reminder path is broken in one of:
- (a) next-reminder-at timestamp isn't persisted per
  session+address+probe_index, so it's lost across `evaluate()` calls;
- (b) any reading below the threshold resets the "done" state, so a
  brief dip (noise) stops reminders permanently;
- (c) reminder is only emitted on fresh threshold crossings, not while
  probe remains over.

Screenshot shows a `2` badge on the Target Exceeded notification — worth
correlating against server-side `target_reminder` event logs to see
whether emit happened and delivery failed (F1-related), or emit never
happened (F2).

**Fix plan:**
1. Trace `service/alerts/evaluator.py` reminder path end-to-end.
2. Add a unit test that feeds 5 minutes of steady over-target readings;
   assert exactly two events — `target_reached` at t≈0 and
   `target_reminder` at t≈180.
3. Add a second test with a noise dip: target, dip briefly under, back
   over — reminder should still fire at t=180 from original crossing
   (not reset by the dip).
4. Fix the evaluator logic to match the tests.
5. Confirm reminder emits go through the same time-sensitive push path
   from F1.

**Verify:** Real device, low target, probe that'll stay above for 10
minutes. Observe pushes at t=0, t=180, t=360, t=540…

---

## F3 — Per-probe timer notifications don't fire when backgrounded [blocker]

**Symptom:** Set a count-down timer on a probe, app backgrounded, timer
expired — no notification.

**Root cause hypothesis:** Either `service/timers.py` completer loop
emits only over WebSocket (which backgrounded iOS can't receive), or it
emits an APNS push but without the time-sensitive priority (same
underlying issue as F1).

**Fix plan:**
1. Trace `countdown_completer_loop` in `service/main.py` and
   `service/timers.py` — confirm the completion path currently calls
   `push_service.send_alert()` with a suitable payload.
2. If it does: make sure the payload uses the F1 time-sensitive path.
3. If it doesn't: add the APNS send, with the time-sensitive headers.
4. Add an integration test against the mocked APNS client: start a
   short timer, advance the loop past duration, assert an APNS send
   with the expected headers.

**Verify:** Start a 30s count-down on a probe, background the app,
lock the phone. At 30s, a time-sensitive notification appears on the
lock screen.

---

## F4 — Pre-alert fired 1°C late [medium]

**Symptom:** Alert body read "76°C" for a target of 80°C with a 5°C
pre-offset. Expected pre-alert at 75°C. User confirmed the 76°C was
the value carried *in the alert payload itself*, so the evaluator
actually emitted at 76, not a display-time drift.

**Root cause hypothesis (two candidates):**
- **Int rounding in the comparison:** float probe reading is rounded
  to int before comparison, turning 75.4 into 75 for display but 75.4
  for compare — but we only see the rounded value in the alert.
- **Tick granularity:** the simulator's noise + 15s cadence can step
  temperature by more than 1°C per tick (noise amplitude 1–1.5°C
  compounded with exponential approach), so the probe passes from
  below 75 to 76 in a single tick. Evaluator sees 76 ≥ 75 and fires
  with the current value. On real hardware at ~3s cadence + 0.1°C
  resolution this would be much tighter.

**Fix plan:**
1. Read `service/alerts/evaluator.py` — confirm threshold comparison is
   float-based, not int-based.
2. If it's int-based: fix to use the raw float probe value.
3. If it's float-based: this is tick granularity, not a bug. Accept.
   Optionally round the alert's displayed value *down to the threshold*
   ("Approaching 80°C — probe at 75°C") rather than revealing the
   overshoot.
4. Defer a proper verdict until we can test against a real iGrill with
   its native 3s/0.1°C cadence.

**Verify:** Re-run simulator after the fix with a 10x speed setting;
observe that crossing alerts report a value at or below threshold, not
above. Final sign-off needs real hardware.

**Verdict (2026-04-20):** Hypothesis "float-based" confirmed.
`service/alerts/evaluator.py` reads `temp: float = probe["temperature"]`
and compares with `temp >= threshold` / `temp >= effective_target_c`
— no int truncation on the comparison path. `_target_to_celsius` and
`_offset_to_celsius` both return floats. So this is simulator tick
granularity (15 s cadence + ±1.5 °C noise + exponential approach
stepping 0.5–2 °C per tick), not an evaluator bug. The optional
display-clamp in step 3 was not wired — the overshoot value is
still informative, and real iGrill hardware at ~3 s / 0.1 °C
resolution won't exhibit the symptom. Marked closed.

---

## Execution order

1. **F1** first — wrong priority blocks every other alert from
   behaving correctly.
2. **F2** and **F3** together — both share the time-sensitive path and
   exercise the same evaluator/timer plumbing.
3. **F4** last, and pencilled in pending real-hardware verification.

---

## Out of scope

- Simulator tick granularity itself (F4 root cause candidate) — not
  worth investing until the real-hardware parity test.
- iOS-side rendering bugs — tracked separately in the iOS plan.
