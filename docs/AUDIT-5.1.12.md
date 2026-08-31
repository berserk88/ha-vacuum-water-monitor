# Code audit — v5.1.12 (input focus-loss bug)

Scope: the full Lovelace card (`ha-vacuum-water-monitor.js`, ~3.5k lines) and
the entire Python integration (`custom_components/ha_vacuum_water_monitor/`,
~1.6k lines across 7 files), reviewed against the reported symptom:

> Clicking into any settings field (vacuum model, tank capacity, water usage,
> etc.) causes it to lose focus almost instantly and reject keyboard input,
> so tank capacity can never be saved and shows as "unknown".

## Critical: render-storm destroys focused inputs

**File:** `ha-vacuum-water-monitor.js`, `HAVacuumWaterMonitor.set hass()` / `_ensureServerState()`

Home Assistant's frontend re-assigns a card's `hass` property on essentially
any entity state change anywhere in the system — not just changes relevant
to that card — and does so frequently (often more than once a second on an
active install).

`set hass()` called `this._ensureServerState()` **unconditionally on every
one of those assignments**, before any throttling logic ran. `_ensureServerState()`
guarded only against *concurrent* calls (`this._serverLoadPromise`), and
reset that guard to `null` as soon as it finished — so it was not a one-time
bootstrap, it re-ran its entire body on the next tick too:

1. Two WebSocket round-trips (`get_state`, `list_vacuums`).
2. An unconditional `set_settings` write of `configured_devices` (if the
   card config named a `vacuum_entity`).
3. `this._lastHtml = ''; this._render();` — which deliberately defeated the
   diff check that's supposed to skip unnecessary DOM writes
   (`if (_newHtml !== this._lastHtml)`), forcing `shadowRoot.innerHTML = ...`
   on every single call regardless of whether anything had changed.

Replacing `shadowRoot.innerHTML` destroys and recreates every element in the
card, including whatever `<input>` the user currently has focused. In
practice this meant a rebuild landed within a fraction of a second of
clicking into any field — often before a single keystroke could register —
which is exactly the reported symptom. Since capacity/usage values could
never be saved, `estimate_water_state()` (`sensor_calculations.py`) always
fell through to "unknown capacity."

**Fix:** gate `_ensureServerState()` with `this._serverReady` so it
bootstraps once; subsequent `hass` ticks are a no-op. Live updates
(settings changes, the 60s server tick) continue to arrive through the
existing `VWM_EVENT` subscription, unaffected.

## Contributing / compounding issues (fixed in the same pass)

1. **`_subscribeServerEvents()` also force-cleared `_lastHtml`.** Independent
   of the bug above, the event handler set `this._lastHtml = ''` before every
   `_render()`, bypassing the diff check on every legitimate server event
   too (e.g. the 60s accounting tick, or a save from another browser tab).
   Removed — `_render()`'s own diff check now decides whether the DOM
   actually needs to change.

2. **No focus preservation across re-renders.** Because the card's rendering
   strategy is a full `innerHTML` replace (not a virtual-DOM diff), *any*
   re-render — even a legitimate one — could still interrupt an in-progress
   edit if the timing was unlucky. Added `_captureFocusState()` /
   `_restoreFocusState()`, called around every `shadowRoot.innerHTML`
   assignment in `_render()`, so focus, value, and cursor position now
   survive any re-render, not just the ones caused by the bug above.

3. **Config editor (Title field) lost focus after one keystroke.** Separate
   bug, same symptom, different code path: typing in
   `HaVacuumWaterMonitorEditor`'s Title field dispatches `config-changed`,
   which the Lovelace host echoes straight back into `setConfig()`. That
   triggered a full `_render()` on every character. Fixed by comparing the
   incoming config against the editor's own last dispatch (JSON-string
   equality) and skipping the rebuild on an echo; a genuinely different
   config (switching cards, opening YAML mode) still re-renders as before.

4. **Backend broadcast storm.** `websocket_api.py`'s `_ws_set_settings`
   broadcast `VWM_EVENT` to every connected card unconditionally, even when
   the patch was a no-op — which the render-storm bug made common (the same
   `configured_devices` patch re-sent on every hass tick). Every one of
   those broadcasts forced every open card to run its event handler. Fixed
   by comparing settings before/after the patch and only broadcasting on an
   actual change. This is defense in depth: with fix #1 in place the
   repeated writes mostly stop happening, but multiple browser tabs or
   future callers can still send redundant patches.

## Reviewed, no changes needed

- `storage.py` — patch/merge and empty-list guard logic is correct and
  already covered by `tests/test_user_device_removal.py`.
- `tick.py` — server-side accounting state machine; logic checked against
  its existing test coverage (`tests/test_sensor_calculations.py` covers the
  calculation helpers it shares with `sensor.py`), nothing related to the
  reported symptom.
- `sensor.py`, `sensor_calculations.py` — entity setup and estimate
  calculations; `estimate_water_state()`'s "unknown capacity" fallback is
  correct behavior given no capacity was ever persisted — a symptom of the
  bug above, not a bug itself.
- `config_flow.py`, `__init__.py` — standard setup/options flow, nothing of
  note.

## Maintainability notes (not changed — flagging for awareness)

- **`ha-vacuum-water-monitor.js` exists as two manually-synced copies**
  (repo root, and `custom_components/ha_vacuum_water_monitor/www/`) — the
  one HA actually serves is the `www/` copy
  (`__init__.py::_async_register_frontend`). There's no build step that
  generates one from the other; both must be hand-edited identically or
  they drift. This patch updates both and keeps them byte-identical, but a
  future `npm run build`-style step (or a symlink) would remove the
  footgun.
- No debounce exists on the settings save path *by design* — every save in
  the main card is triggered by an explicit button click, not per-keystroke
  (only the editor's Title field saved on every keystroke, fixed above via
  the echo-guard rather than a debounce, since debouncing a Lovelace
  `config-changed` dispatch would just delay the same problem).

## Verification

- All 16 pre-existing Python tests pass unchanged.
- 2 new Python tests added (`tests/test_websocket_api_broadcast.py`) proving
  a repeated identical patch broadcasts once, and a genuine change still
  broadcasts.
- New permanent jsdom regression test
  (`.github/focus-regression.cjs`, wired into `validate.yml`): mounts the
  card, focuses the tank-capacity input, types a value via real `input`
  events, then fires 8 rapid `hass` re-assignments (simulating unrelated
  entities changing state) — the exact storm that used to wipe focus. Run
  against the pre-fix code it fails (`stillFocused: false`, value wiped);
  against the fixed code both focus and the typed value survive.
- Repo's existing CI checks (`node --check` syntax, theming invariant, a11y
  invariant, jsdom smoke render) all pass on both JS copies.
