# Simulation UI Controls — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:executing-plans to implement this plan task-by-task.

**Goal:** Add start/stop simulation controls to the Settings tab in the web dashboard.

**Architecture:** Pure frontend change — a new "Simulation" section in the Settings tab with probes/speed dropdowns and a start/stop button. Uses existing REST endpoints (`POST /api/v1/simulate/start|stop`) and detects simulation state via WebSocket `session_start`/`session_end` events by checking for the simulated device address `SIM:UL:AT:ED:00:01`.

**Tech Stack:** Vanilla JS, CSS custom properties, existing HTML structure in `service/web/static/index.html`

---

### Task 1: Add Simulation HTML Section

**Files:**
- Modify: `service/web/static/index.html:1086-1087` (after the Log Levels `</section>`, before the closing `</div>` of `#tab-settings`)

**Step 1: Add the HTML block**

Insert after line 1086 (`</section>` closing Log Levels) and before line 1087 (`</div>` closing `#tab-settings`):

```html

  <!-- Simulation Controls -->
  <section class="settings-section">
    <div class="settings-section-title">Simulation</div>
    <div id="settingsSimulation">
      <div class="sim-controls">
        <div class="sim-field">
          <label class="settings-label" for="simProbes">Probes</label>
          <select id="simProbes">
            <option value="1">1</option>
            <option value="2">2</option>
            <option value="3">3</option>
            <option value="4" selected>4</option>
          </select>
        </div>
        <div class="sim-field">
          <label class="settings-label" for="simSpeed">Speed</label>
          <select id="simSpeed">
            <option value="1">1x</option>
            <option value="5">5x</option>
            <option value="10" selected>10x</option>
            <option value="20">20x</option>
          </select>
        </div>
      </div>
      <button class="btn btn-primary" id="btnSimToggle">Start Simulation</button>
      <div id="simFlash" style="display:none; font-size:0.82rem; margin-top:0.5rem;"></div>
    </div>
  </section>
```

**Step 2: Verify in browser**

Open https://igrill.pimento.home.kerr.host, go to Settings tab. The new section should appear below Log Levels but will be unstyled/non-functional.

**Step 3: Commit**

```bash
git add service/web/static/index.html
git commit -m "Add simulation controls HTML to Settings tab"
```

---

### Task 2: Add Simulation CSS

**Files:**
- Modify: `service/web/static/index.html:729-734` (after the `.flash-error` rule block, before `.settings-no-devices`)

**Step 1: Add CSS rules**

Insert after line 727 (closing `}` of `.log-level-row select.flash-error`) and before the `.settings-no-devices` rule:

```css

    .sim-controls {
      display: flex;
      gap: 1.5rem;
      margin-bottom: 0.75rem;
    }

    .sim-field {
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }

    .sim-field select {
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 4px;
      color: var(--text-primary);
      padding: 0.3rem 0.5rem;
      font-size: 0.82rem;
      cursor: pointer;
      transition: border-color 0.3s;
    }

    .sim-field select:focus {
      outline: none;
      border-color: var(--accent);
    }

    .sim-field select:disabled {
      opacity: 0.4;
      cursor: not-allowed;
    }

    .sim-field .settings-label {
      color: var(--text-secondary);
      font-size: 0.85rem;
      white-space: nowrap;
    }

    #btnSimToggle {
      width: 100%;
      margin-top: 0.25rem;
    }

    @media (max-width: 600px) {
      .sim-controls {
        flex-direction: column;
        gap: 0.75rem;
      }
    }
```

**Step 2: Verify in browser**

Refresh Settings tab. The simulation panel should now match the existing design language — dark card, styled dropdowns, full-width button.

**Step 3: Commit**

```bash
git add service/web/static/index.html
git commit -m "Add simulation controls CSS styling"
```

---

### Task 3: Add Simulation JS — State and DOM References

**Files:**
- Modify: `service/web/static/index.html:~2957-2958` (after the existing `settingsDevicesConn` var declaration, before the log level dropdown setup)

**Step 1: Add simulation state variables and DOM references**

Insert after line 2957 (`var settingsDevicesConn = ...;`) and before the log level setup comment/code:

```javascript

  /* -- Simulation Controls --------------------------------------------- */
  var SIM_ADDRESS = "SIM:UL:AT:ED:00:01";
  var simRunning = false;
  var simSessionId = null;

  var simProbesSelect = document.getElementById("simProbes");
  var simSpeedSelect  = document.getElementById("simSpeed");
  var btnSimToggle    = document.getElementById("btnSimToggle");
  var simFlash        = document.getElementById("simFlash");
```

**Step 2: Commit**

```bash
git add service/web/static/index.html
git commit -m "Add simulation state variables and DOM refs"
```

---

### Task 4: Add Simulation JS — Toggle, API Calls, and Flash Feedback

**Files:**
- Modify: `service/web/static/index.html` (immediately after the vars added in Task 3)

**Step 1: Add the core simulation functions**

Insert directly after the DOM reference block from Task 3:

```javascript

  function updateSimUI() {
    btnSimToggle.textContent = simRunning ? "Stop Simulation" : "Start Simulation";
    btnSimToggle.className = simRunning ? "btn btn-danger" : "btn btn-primary";
    simProbesSelect.disabled = simRunning;
    simSpeedSelect.disabled = simRunning;
  }

  function showSimFlash(message, isError) {
    simFlash.textContent = message;
    simFlash.style.display = "block";
    simFlash.style.color = isError ? "var(--red)" : "var(--green)";
    setTimeout(function () { simFlash.style.display = "none"; }, 2500);
  }

  btnSimToggle.addEventListener("click", function () {
    btnSimToggle.disabled = true;

    if (simRunning) {
      fetch("/api/v1/simulate/stop", { method: "POST", headers: { "Content-Type": "application/json" } })
        .then(function (resp) {
          if (!resp.ok) throw new Error("HTTP " + resp.status);
          return resp.json();
        })
        .then(function () {
          showSimFlash("Simulation stopped", false);
        })
        .catch(function (err) {
          console.warn("[iGrill] Simulation stop failed:", err);
          showSimFlash("Failed to stop: " + err.message, true);
        })
        .finally(function () { btnSimToggle.disabled = false; });
    } else {
      var body = {
        probes: parseInt(simProbesSelect.value, 10),
        speed: parseFloat(simSpeedSelect.value)
      };
      fetch("/api/v1/simulate/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      })
        .then(function (resp) {
          if (!resp.ok) throw new Error("HTTP " + resp.status);
          return resp.json();
        })
        .then(function (data) {
          showSimFlash("Simulation started", false);
        })
        .catch(function (err) {
          console.warn("[iGrill] Simulation start failed:", err);
          showSimFlash("Failed to start: " + err.message, true);
        })
        .finally(function () { btnSimToggle.disabled = false; });
    }
  });
```

Note: The actual `simRunning` state toggle is NOT done here in the fetch handlers. It is driven by WebSocket events (Task 5), keeping the UI in sync with the server's actual state.

**Step 2: Commit**

```bash
git add service/web/static/index.html
git commit -m "Add simulation start/stop API calls and flash feedback"
```

---

### Task 5: Add Simulation JS — WebSocket State Detection

**Files:**
- Modify: `service/web/static/index.html` — three touch points:
  1. `handleStatus()` (~line 2249) — detect simulation on page load/reconnect
  2. `handleSessionStart()` (~line 2304) — detect simulation session starting
  3. `handleSessionEnd()` (~line 2332) — detect simulation session ending

**Step 1: Update `handleStatus()`**

Add simulation state check at the end of `handleStatus()`, after the existing `fetchSessionReadings` block (before the closing `}`):

```javascript
    /* Detect active simulation */
    var simDevice = (session.devices || []).indexOf(SIM_ADDRESS) !== -1;
    if (simDevice && session.id) {
      simRunning = true;
      simSessionId = session.id;
    } else {
      simRunning = false;
      simSessionId = null;
    }
    updateSimUI();
```

**Step 2: Update `handleSessionStart()`**

Add simulation detection at the end of `handleSessionStart()`, after `renderDevices();`:

```javascript
    if ((session.devices || []).indexOf(SIM_ADDRESS) !== -1) {
      simRunning = true;
      simSessionId = session.id;
      updateSimUI();
    }
```

**Step 3: Update `handleSessionEnd()`**

Add simulation reset at the end of `handleSessionEnd()`, after `renderDevices();`:

```javascript
    if (simSessionId) {
      simRunning = false;
      simSessionId = null;
      updateSimUI();
    }
```

**Step 4: Test full flow in browser**

1. Open dashboard → Settings tab → verify panel shows "Start Simulation"
2. Click Start → verify button changes to "Stop Simulation", dropdowns disable
3. Switch to Live tab → verify session is active with simulated device
4. Switch back to Settings → verify panel still shows running state
5. Click Stop → verify panel resets to idle state
6. Refresh the page mid-simulation → verify panel correctly shows running state

**Step 5: Commit**

```bash
git add service/web/static/index.html
git commit -m "Wire simulation UI to WebSocket events for real-time state sync"
```

---

### Task 6: Update Documentation

**Files:**
- Modify: `README.md` — add a note about simulation controls in the web dashboard section

**Step 1: Add simulation docs**

In the web dashboard/UI section of the README, add a brief mention that the Settings tab includes simulation controls for starting/stopping simulated cook sessions with configurable probe count and speed.

**Step 2: Commit**

```bash
git add README.md
git commit -m "Document simulation controls in web dashboard"
```
