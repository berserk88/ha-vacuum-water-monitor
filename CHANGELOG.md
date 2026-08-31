# Changelog

## 5.5.1 (2026-08-25)

**Fix: mop-wash dosing wasn't explicitly guarded against vacuum-only mode.**

Prompted by a direct question about whether vacuum-only days could ever contribute to either
tank's dosing. Area-based dosing already explicitly checked `not mop_off`; mop-wash-event
dosing (the fixed volume per wash cycle) did not — it only checked that the vacuum's status
transitioned to a mop-washing state. In practice a vacuum shouldn't report "washing the mop"
if the mop wasn't used, so this was very likely safe, but it was relying on real-world
vacuum behavior rather than an explicit software guarantee.

- `tick_device()`'s mop-wash dosing block now also requires `not mop_off`, matching the
  area-based path.
- Closed a related gap this surfaced: the mop mode/intensity latch previously only updated
  on a *non-off* live value, so a vacuum-only session would leave a stale intensity (e.g.
  `"high"` from the last mop day) sitting in the latch. Since `mop_off` is evaluated against
  the latched value once cleaning ends, that stale value could have defeated the new guard.
  The latch now updates on every valid reading while actively cleaning, `"off"` included —
  only genuinely missing/`"unavailable"` readings are skipped.
- New tests (`VacuumOnlyModeContributesNothingTest`, 3 tests): area-based suppression,
  the stale-latch-gets-overwritten edge case specifically, and a sanity check proving the
  same scenario *does* dose when the mop was genuinely used — confirming the suppression is
  actually due to `mop_off`, not an unrelated test-setup issue. 96 tests total.

## 5.5.0 (2026-08-24)

**New: adaptive calibration — the integration learns per-vacuum consumption/fill rates
from real refill and empty cycles, instead of relying solely on static defaults.**

- **Adaptive calibration model.** Every refill (water) or empty (waste) event is now also
  a calibration checkpoint: comparing the tank's known capacity against how much the model
  estimated was used since the last cycle gives a correction signal, applied per mop
  intensity with a learning rate that decays as more cycles are observed (large adjustments
  early on, small/stable ones once there's history) — see `tick.py::_calibrate_model`'s
  docstring for the exact update rule and its honest framing as an adaptive-calibration
  heuristic, not real regression (there's no ground-truth water-level sensor to train
  against; the refill/empty event is the only real signal available). Cycles where too
  little was estimated to have been used are skipped, so a precautionary top-off can't
  corrupt the model. Mirrors independently for the waste tank (its own correction factors,
  its own cycle count), since the two tanks calibrate against different events.
- **Fixed a real, previously-undiscovered bug found while building this:**
  `mop_mode_raw`/`mop_intensity_raw` were compared/looked-up without normalizing case, so a
  capitalized entity state (`"Off"`, `"Medium"`, `"High"` — a very plausible way for a
  select entity to report its option value) never matched the lowercase `"off"` check or
  the lowercase `DEFAULT_INTENSITY_FACTOR`/`DEFAULT_USAGE_PER_M2` dict keys. Both now
  normalize via the existing `slugify()` before any comparison/lookup.
- **New: mop mode/intensity latching.** These select entities are only meaningful while
  actively cleaning — most integrations (verified against the official `roborock`
  integration) revert them to a default/off value once cleaning ends. But the mop-wash
  dosing event fires *after* cleaning ends (vacuum returns to dock, then washes), so
  re-reading the live entity at that point would see the reverted value, not what was
  actually used during the run being washed off. `tick_device()` now latches the last valid
  value seen while actively cleaning and uses that for anything happening after cleaning
  stops — this was flagged directly and is exactly the scenario
  `select.roborock_qrevo_maxv_mop_intensity` (Off/Low/Medium/High) exhibits.
- **New: mop mode/intensity entities are now manually configurable**, mirroring the dock
  error override from 5.3.0. **Edit a tracked vacuum → Set the mop mode / intensity
  entities** lets you assign the right entity if auto-detection picked the wrong one or
  found nothing — `config_flow.py::_mop_entity_updates` (the two fields are independent;
  clearing one falls back to auto-detection for just that entity).
- **New: `button.<vacuum>_clear_prediction_model`.** Resets both learned models
  (water and waste) back to their unlearned defaults via the new
  `storage.py::async_reset_prediction_models`. Does not touch current tank levels/counters
  or the mop mode/intensity latches — only the learned correction factors and calibration
  history.
- The learned state is visible, not a black box: `sensor.<vacuum>_water_used_since_refill`
  and `sensor.<vacuum>_waste_water_collected` now expose `prediction_cycles_observed`,
  `prediction_correction_factors`, and `prediction_last_calibrated` as attributes.
- New tests: `tests/test_prediction_model.py` (17 tests) — the case-normalization fix, the
  latching behavior (including that a stale latch from a previous session doesn't leak into
  a currently-active one), the calibration primitives in isolation (skip conditions,
  direction of correction, per-intensity isolation, learning-rate decay, clamping), a
  realistic multi-cycle convergence proof (error shrinks monotonically toward the true
  ratio, not just "moves in the right direction once"), and the reset button's storage
  method. Plus 4 new tests for `_mop_entity_updates` in `test_config_flow_helpers.py`. 93
  tests total.

## 5.4.0 (2026-08-23)

**New: dirty/waste water tank tracking, and fully customizable dock error messages.**

- **Customizable dock error messages.** The three trigger phrases the integration matches
  against the dock error source — "clean tank empty", "no error / cleared", and "waste tank
  full" — are now per-vacuum settings (`dock_empty_message`/`dock_ok_message`/
  `dock_full_message`) instead of a hardcoded vocabulary, set via **Edit a tracked vacuum →
  Set the dock error sensor**. Matching is slug-based (`tick.py::matches_dock_message`), so
  exact casing/formatting still doesn't matter — this makes the feature usable with vacuum
  brands/integrations that phrase dock errors completely differently from Roborock's
  wording, which the previous fixed-set implementation couldn't support at all.
- **Behavior change: the clean-tank auto-reset is now stricter.** Previously, *any* dock
  error transition away from the empty message triggered a refill-reset. It now requires
  the error to clear specifically to the configured "ok" message — an unrelated new error
  (e.g. a duct blockage reported right after a refill) no longer gets mistaken for a refill.
- **New: dirty/waste water tank tracking**, using the same accounting signals as the clean
  tank (mop-wash events, cleaned area) mirrored into a second counter — the water that
  leaves the clean tank during cleaning is what fills the dirty one, so both are estimated
  from the same activity (`tick.py::tick_device`). Five new entities per vacuum:
  - `sensor.<vacuum>_waste_tank_level` (%, estimated fill — the inverse direction from
    "Water remaining": it fills up rather than empties)
  - `sensor.<vacuum>_waste_water_collected` (mL, since last emptied)
  - `sensor.<vacuum>_last_emptied` (timestamp, diagnostic)
  - `binary_sensor.<vacuum>_waste_tank_full` (on at ≥90% estimated fill, or immediately if
    the dock error source directly reports it — whichever signal fires first)
  - `button.<vacuum>_waste_tank_emptied` (manual reset, mirroring the Refilled button)
- **New: waste-tank auto-empty.** When the dock error source reports the waste tank is full
  and then clears to "ok", the waste counter resets automatically — exactly like pressing
  the Waste tank emptied button — on its own independent cooldown from the clean-tank reset,
  so refilling one tank doesn't silently reset the other.
- Waste tank capacity defaults to the same as the clean tank (`sensor_calculations.py::
  _waste_capacity_ml`), overridable per-vacuum via the same edit step.
- `sensor_calculations.py`: `estimate_waste_state()` mirrors `estimate_water_state()`;
  `parse_waste_reset_datetime()` mirrors `parse_refill_datetime()` (both now share a
  `_parse_stored_datetime()` helper rather than duplicating the ISO/millis fallback logic).
- `storage.py`: tank state gains `waste_used_ml`/`last_waste_reset_iso`/
  `last_waste_reset_ts`, and a new `async_reset_waste_tank()` mirrors `async_reset_tank()`.
- New tests: waste-tank estimate/datetime coverage in `test_sensor_calculations.py`, and
  in `test_tick_auto_detect.py` — dock-message customization (`MatchesDockMessageTest`),
  an end-to-end waste-tank-auto-empty proof, and an end-to-end proof that a fully custom
  message vocabulary works and the built-in defaults correctly do *not* match a
  custom-configured vacuum. 72 tests total.

## 5.3.0 (2026-08-22)

- **Renamed the integration**, domain `ha_vacuum_water_monitor` → `vacuum_water_level`
  ("Vacuum water level"), so it can be installed without conflicting with anyone else's copy
  of the original project it was forked from. A one-time migration
  (`__init__.py::_async_migrate_legacy_storage`) copies existing storage data forward on
  first setup under the new domain — it never overwrites data that already exists under the
  new domain, so it's safe to run on every startup and a no-op once migrated.
- **Added: edit a tracked vacuum in place**, without removing and re-adding it. New
  **Configure → Edit a tracked vacuum** menu option:
  - **Change brand / model / capacity** — re-runs the add wizard's brand/model picker,
    pre-filled with the vacuum's current selection where known, and overwrites capacity in
    place. Auto-detected companion entities (status/area/mop mode/dock error) are untouched.
  - **Set the dock error sensor** — manually assign the entity (and optionally a specific
    attribute on it) that reports the dock's water-empty error, overriding auto-detection.
- **Added: dock error can be read from an entity attribute, not just its main state.**
  Some setups expose the dock's fault message on an attribute of an entity whose primary
  state is something else (e.g. a docking-station entity with state `docked` and an `error`
  attribute that reads `Water empty` when the tank runs dry) — `tick.py`'s dock error reader
  now supports this via the new `dock_error_attribute` field, set through the edit flow
  above. The water-empty match is also now slug-based (`tick.py::_is_water_empty`), so
  `Water empty`, `water_empty`, etc. are all recognized as the same signal regardless of
  exact casing/formatting.
- `config_flow.py::_upsert_device_entry` is the one persistence helper both the initial add
  and every edit path now share — update in place if the vacuum is already tracked, append
  if not — replacing the add-only `_build_new_device_entry` from 5.2.0.
- New tests: `tests/test_storage_migration.py` (3 tests, the legacy-domain migration),
  plus additions to `tests/test_config_flow_helpers.py` (upsert/edit persistence logic) and
  `tests/test_tick_auto_detect.py` (attribute-based dock error reading, slug-based matching,
  and a full accounting-loop proof that a human-readable attribute value triggers the
  auto-reset). 59 tests total.

## 5.2.0 (2026-08-20)

**Breaking change: the Lovelace card is removed.** This release simplifies the project down
to a pure integration — sensors, a low-water binary sensor, and a Refilled button — managed
entirely through Home Assistant's config flow instead of a bundled card.

- **Removed:** `ha-vacuum-water-monitor.js`, the bundled `www/` copy, `websocket_api.py`,
  and all frontend/HTTP static-path registration in `__init__.py`. The `frontend`, `http`,
  and `websocket_api` manifest dependencies are dropped along with it.
- **Removed:** blanket auto-discovery of every `vacuum.*` entity. Tracking is now opt-in:
  entities are only created for vacuums explicitly added via the config flow. This was a
  deliberate trade-off — the old zero-config behavior never reliably resolved capacity or
  wired up accounting anyway (see the 5.1.13 audit), so making it explicit is both simpler
  and more honest about what the integration actually knows.
- **Added:** a proper multi-step options flow — **Configure → Add a vacuum to track** walks
  through picking the vacuum entity, then brand, then model (pulling capacity straight from
  the same model database the old card used), with a manual capacity fallback for unlisted
  models. If the vacuum's HA device registry entry has a recognizable manufacturer/model,
  brand and model are pre-selected. A **Stop tracking a vacuum** step and **Edit warning
  thresholds** step round out the menu.
- **Added:** `binary_sensor.<vacuum>_water_low` — on when remaining water is at or below the
  warning/critical threshold (`severity` attribute distinguishes them), for automations that
  want a clean on/off trigger instead of a `numeric_state` check against the percentage
  sensor.
- **Added:** `button.<vacuum>_refilled` — replaces the card's Refilled button. Resets the
  tank counter and refreshes sensors immediately rather than waiting for the next tick.
- **Removed:** the "Next maintenance due" sensor. It depended on `maintenance_items`, a
  setting only the removed card's Settings tab could ever write — keeping an entity that
  could never functionally hold data didn't fit a codebase focused on simplicity.
- Companion-entity auto-detection (`tick.py::async_ensure_auto_config`, added in 5.1.13)
  and device-registry-based capacity resolution are unchanged in behavior, but now only run
  for vacuums added via the config flow rather than every discovered vacuum.
- `sensor_calculations.py`'s flat `DEFAULT_TANK_ML` table is now generated from a new
  `MODEL_DATABASE` (brand → {model: capacity}) that also powers the config flow's
  brand/model selectors — same 27 models, same capacities, same lookup keys.
- New tests: `tests/test_config_flow_helpers.py` (8 tests) covering the pure
  selection/persistence logic the options flow steps call (which vacuums are available to
  add, which are tracked, and exactly what gets persisted for a database vs. custom-capacity
  pick) — the ConfigFlow/OptionsFlow framework machinery itself isn't unit-tested, since
  faithfully mocking Home Assistant's config_entries internals would risk validating a fake
  harness rather than real behavior.
- CI: removed `.github/workflows/validate.yml` and the JS-only smoke/focus-regression
  scripts it ran (nothing left to validate). `tests.yml` and `hassfest.yml` are unchanged.

## 5.1.13 (2026-08-19)

**The integration's core promise — auto-discovery with zero configuration — did not actually work. This release fixes it.**

- **Fix (critical): tank capacity never resolved for auto-discovered devices, so sensors always showed "unknown" and the card's water gauge/refill button never rendered.** Capacity resolution only succeeded if the vacuum's `entity_id` happened to be an exact string match against one of ~25 hardcoded keys (e.g. your entity would need to literally be `vacuum.roborock_s8_maxv_ultra`) — which essentially never happens for a real install, since HA generates entity_ids from whatever name the user gave the device. Capacity is now resolved from the vacuum's actual HA device registry entry (manufacturer + model, as reported by whatever integration exposes it — verified against the official `roborock` core integration), with the old entity_id/brand_profile matching kept as a fallback for manual configs. An unrecognised model still correctly falls back to "unknown capacity" rather than guessing.
- **Fix (critical): server-side water accounting could never actually run for auto-discovered devices**, because both accounting signals (mop-wash events, cleaned-area dosing) depended on helper entities (`status_sensor`, `area_sensor`, `mop_mode_entity`, `mop_intensity_entity`, `dock_error_sensor`) that auto-discovery never wired up — they were only ever set by manually filling in the card's Settings tab. New `tick.py::async_ensure_auto_config()` auto-detects these by finding other entities that share the same HA device as the vacuum (e.g. the official Roborock integration's "Cleaning area", "Status", "Mop mode", "Mop intensity", "Dock error" entities) and wires them in automatically, every tick, without ever overwriting anything already manually configured.
- Fix: the refill button wasn't "broken" so much as invisible — the card hides the water gauge/refill button entirely when it can't resolve capacity, so this was really the same root cause as the sensors showing "unknown." Fixed as a side effect of the capacity fix above.
- Fix: the card's own auto-discovery device list (`_getDevices()`, zero-config path) rebuilt bare `{vacuum_entity, name, icon}` objects and silently discarded `configured_devices` — meaning even after the backend resolved capacity, the live card wouldn't show it without a full page reload. It now merges `configured_devices` in, and the backend fires a settings-changed event immediately after auto-detection resolves anything, so an already-open card picks it up live.
- Verified `MOP_WASH_STATES` and the `water_empty` dock-error string used for auto-reset-on-refill against the real Roborock status/dock-error code mappings — both already correct, they just never had a wired-up source entity to read from.
- New tests: `tests/test_tick_auto_detect.py` (9 tests) — companion-entity detection, capacity resolution incl. fuzzy-match guardrails, manual-override preservation, idempotency, and a full end-to-end accounting run proving both dosing signals fire from a simulated official-Roborock-integration entity set with zero manual configuration.

## 5.1.12 (2026-08-19)

- **Fix (critical): input fields losing focus instantly, making the card unusable.** `set hass()` called `_ensureServerState()` unconditionally on every hass update — which Home Assistant fires for practically any entity state change system-wide, often more than once a second. Once loaded, that method still re-ran its full body on every call: two WS round-trips, a repeated `set_settings` write, then an unconditional `this._lastHtml = ''; this._render();` that force-rebuilt the entire card via `shadowRoot.innerHTML = ...`. That destroyed whatever input the user had focused (vacuum model, tank capacity, water usage, etc.) — sometimes within a fraction of a second of clicking into the field — so settings, including tank capacity, could never be saved (shown as "unknown capacity"). `_ensureServerState()` now bootstraps once; live updates arrive solely through the existing VWM_EVENT subscription.
- Fix: the event-subscription re-render handler no longer force-clears the render cache, so it only touches the DOM when content actually changed.
- Fix: added focus/value/selection capture-and-restore around every full-card re-render, as defense in depth against any other re-render (e.g. the 60s server tick, or a settings save from another browser tab) interrupting an in-progress edit.
- Fix: the card's config editor (Title field) lost focus after every keystroke — typing dispatched `config-changed`, which the Lovelace host echoed straight back into `setConfig()`, rebuilding the editor. `setConfig()` now skips the rebuild when the incoming config is an echo of its own last dispatch.
- Fix (backend): `set_settings` now only broadcasts `VWM_EVENT` when the patch actually changes something, instead of unconditionally on every call — several frontend paths re-send an identical patch, and each was previously a broadcast every open card had to react to.
- Chore: added a permanent jsdom regression test (`.github/focus-regression.cjs`, wired into CI) and two Python tests (`tests/test_websocket_api_broadcast.py`) covering the above.

## 5.1.11 (2026-07-18)

- Fix: threshold changes made in the integration Options now apply immediately. The options flow saved them, but nothing re-read them, so they previously only took effect after a Home Assistant restart.

## 5.1.10 (2026-07-18)

- Fix (UI): the small accent dot before section titles no longer detaches from the title text (it was pushed to the opposite edge by the header's flex space-between); it is now pinned next to the title.

## 5.1.9 (2026-07-18)

- Fix (#4): removing a manually-added device now works even when it is the last one. The card used the generic settings patch, whose empty-list guard refused the write, so the device silently reappeared with no feedback. A dedicated remove command persists the deletion and the card shows a confirmation / error toast.
- Fix (#3): the donate/support footer no longer flickers on state changes, tab switches, or view navigation — it is re-injected synchronously before paint.

## 5.1.8 (2026-07-17)

- Fix (UI): responsive tab bar — tabs stretch to fill the card width and wrap on narrow layouts instead of being pinned to content width and clipped (shared HA Tools tab styling).

## [5.1.7] - 2026-07-12

Fixes for [#1](https://github.com/MacSiem/ha-vacuum-water-monitor/issues/1) and
[#2](https://github.com/MacSiem/ha-vacuum-water-monitor/issues/2). Thanks @chris400!

- Fix: ghost "Vacuum" device created for users who added the card from the UI picker.
  The card's stub config carried a brand profile whose default `vacuum_entity` leaked
  into saved settings. The stub is now minimal, brand profiles can no longer inject an
  entity id, and the card only persists config devices whose entity exists in HA.
- Fix: one-time migration prunes previously saved ghost `configured_devices` (no matching
  HA entity and no tank history) and removes their leftover device registry entries.
- Fix: vacuums seeded from stored tank state are now named with their HA friendly name
  (e.g. "Roborock S7 MaxV") instead of the raw entity id.
- Fix: the card now works for non-admin Home Assistant users — websocket commands no
  longer require admin (authentication is still required).
- Docs: rewritten README with a "How it works" section, automatic-vs-manual table, quick
  start, entity/automation examples, and FAQ; new English screenshots (light + dark).
- Chore: removed committed `__pycache__` from the repo; aligned versions across
  `manifest.json`, `const.VERSION`, and the bundled card header (5.1.7).

## [5.1.6] - 2026-06-27

- Fix: large i18n cleanup — the setup banner, all 29 brand-profile notes, the refill notification, the auto-created automation alias, button/status states, and table labels were hardcoded in Polish; they now render in English. The bilingual `_t` table for the main UI is unchanged. The bundled card (repo root + integration `www` copy) is kept in sync.
- Docs: added a real card screenshot to the README.

## [5.1.5] - 2026-06-15

- Theme: dark/light now follows the active Home Assistant theme (luminance of --card-background-color) instead of OS prefers-color-scheme.
- Sync bundled card (root and integration www copy now identical, both themed).


## [5.1.4] - 2026-06-15

- Theme: dark/light now follows the active Home Assistant theme (luminance of --card-background-color) instead of OS prefers-color-scheme.
- Sync bundled card (root and integration www copy now identical, both themed).


## [5.1.3] - 2026-06-15

- Theme: dark/light now follows the active Home Assistant theme (luminance of --card-background-color) instead of OS prefers-color-scheme.
- Sync bundled card (root and integration www copy now identical, both themed).


## [5.1.2] - 2026-06-13

### Fixed

- **Card now auto-resolves the vacuum model profile (tank capacity) from the
  entity id** — a known model such as `vacuum.roborock_s8_maxv_ultra` shows its
  real tank size (e.g. 3000 ml) and tracks water out of the box, without the
  user manually picking a Brand Profile. Previously a discovered vacuum with no
  stored `brand_profile` rendered as "Generic / 300 ml / doesn't track water".
- Added `getGridOptions()` for correct sizing in HA's sections (grid) layout.
- Periodic `hass`-driven re-render is now gated by a vacuum-state signature, so
  the card no longer rebuilds every 10s when nothing changed.

## [5.1.1] - 2026-06-13

### Fixed

- **Water remaining/used sensors now know tank capacity from the vacuum model
  automatically**, without manual calibration. Capacity falls back to a built-in
  per-model database (ported from the card's `CALIBRATION_DATA`), auto-detected
  from the `brand_profile` or the vacuum entity id — e.g. a Roborock S8 MaxV
  Ultra resolves to 3000 ml out of the box. Unrecognised models still report
  `unknown` rather than a misleading percentage (mirrors the card's water calc).

## [5.1.0] - 2026-06-13

### Added

- Added Store-backed `sensor` platform entities for each known vacuum:
  water remaining percentage, water used since refill, last refill timestamp,
  and next custom maintenance due in days.
- Sensor entities refresh from the same Store write/tick path used by the
  bundled card, so automations and dashboards can react without opening the
  card.
- Added pure-python tests for water estimate math, refill timestamp parsing,
  Store device merging, and custom maintenance due derivation.

## [5.0.4] - 2026-05-24

### Fixed

- **`BRAND_PROFILES.roborock_s8_maxv_ultra` no longer pre-fills four
  Maciej-private template/input entities** (`sensor.roborock_water_remaining`,
  `input_number.roborock_water_used_ml`,
  `sensor.roborock_water_used_last_session_2`,
  `input_datetime.roborock_last_water_reset`). On a fresh HACS install those
  entities don't exist, so the card was rendering four blank "unknown" tiles
  even though server-side accounting in `tick.py` was working fine via the
  hybrid-mode fallback. The Roborock profile now exposes only entities created
  by the official `roborock` integration. Advanced users who maintain their
  own DIY counter helpers can wire them in via per-card YAML — see
  [README "Advanced YAML"](README.md#advanced-yaml).
- **Mop dosing now reads the real Roborock select entities** instead of
  always defaulting to `standard` mop_mode and `medium` mop_intensity. Added
  `mop_mode_entity: select.roborock_s8_maxv_ultra_mop_mode` and
  `mop_intensity_entity: select.roborock_s8_maxv_ultra_mop_intensity` to the
  S8 MaxV Ultra brand profile, so the 60s tick uses your actual mop settings.
  Prior behaviour underestimated water usage by ~50% at `deep`/`high`
  (real 9 × 1.3 = 11.7 ml/m² vs default 6 × 1.0 = 6 ml/m²).
- **`_addUserDevice` brand-profile matching is now fuzzy by model suffix**.
  Previously the match required an exact `vacuum_entity` equality, so renamed
  entities (`vacuum.s8_maxv_ultra`, `vacuum.salon_q_revo`,
  `vacuum.parter_s7_maxv`) silently fell through to the generic profile
  (`water_total_ml: 0` → blank water tile, no dock sensors). The matcher now
  accepts entity IDs ending with `_<model_suffix>` or `.<model_suffix>`, then
  forces `vacuum_entity` back to the user's actual entity ID after the spread.

### Notes

- If you upgraded **from v5.0.0 or v5.0.1** at any point on 2026-05-18 and
  also maintain your own `input_number.*_water_used_ml` helper via a template
  sensor / automation, your counter may have been double-counted for a few
  hours (regression window between v5.0.0 publication and the v5.0.2 patch
  that landed the `_hasPrivHelpers` check). Spot-check your counter history
  for that day and reset the input_number manually if numbers look ~2× off.

## [4.1.6] - 2026-05-18

### Fixed
- **Calibration label** now reads from the per-device auto-detected `brand_profile` (e.g. `roborock_s8_maxv_ultra`) instead of the card-level YAML config. Multi-device cards no longer collapse every device to the same calibration; a Roborock S8 MaxV Ultra renders as such (Tank 3000 ml, Mop VibraRise 3.0 dual spinning, ~250 m² per charge) rather than "Generic / Unknown model".
- **Matter-bridge dedup**. When the same physical robot is exposed via both the native vendor integration (e.g. `vacuum.roborock_s8_maxv_ultra`, platform `roborock`) and a Matter bridge (`vacuum.robotic_vacuum_cleaner`, platform `matter`), auto-discovery now drops the Matter exposure if a non-matter alternative with the same manufacturer string exists. Prefers the native entity because it exposes the rich sensor surface (water, dock, mop, brushes). Reads `hass.entities` + `hass.devices` synchronously — no extra WS calls.

## [4.1.5] - 2026-05-18

### Fixed
- **Auto-discovery now picks up all vacuum entities** instead of silently skipping the hardcoded `vacuum.robotic_vacuum_cleaner` ID that leaked in from a prior workaround. Vacuums without native water sensors are still auto-added; estimation falls back to area/state-based dosing per the 'generic' brand profile, and users can remove unwanted vacuums via the Settings tab.

# Changelog — Vacuum Water Monitor

## [5.0.3] - 2026-05-18

### Fixed
- Mirrors v4.1.6 plugin fixes (commit 5546671): per-device `brand_profile` in calibration label + Matter-bridge dedup in auto-discovery. Same bug surface lived in the bundled v5 integration card; both fixes applied verbatim so the card behaves identically whether installed as Lovelace plugin or via the integration.

## [5.0.2] - 2026-05-18

### Fixed
- **Respect user's pre-existing DIY automations.** `_hasPrivHelpers()` in the card was hard-coded to `false`, so the integration always ran its own water accounting even when the user already had an `input_number.*_water_used_ml` helper updated by their own template sensor / automation. Now both the JS card and the Python tick check whether `device.water_used_input` resolves to an existing HA entity and skip integration-side accounting when it does — the card only displays state, never overwrites it. Pairs with the v4 plugin's `_hasPrivHelpers` check (line 1393) which had been working since the standalone-mode aneks 2026-04-18.

## [5.0.1] - 2026-05-18

### Fixed
- **Auto-discovery now picks up all vacuum entities** instead of silently skipping the hardcoded `vacuum.robotic_vacuum_cleaner` ID that leaked in from a prior workaround. Vacuums without native water sensors are still auto-added; estimation falls back to area/state-based dosing per the `generic` brand profile, and users can remove unwanted vacuums via the Settings tab.

## [5.0.0] - 2026-05-18

### Major
- Migrated from a HACS Lovelace plugin to a HACS integration with a bundled Lovelace card.
- Added config flow setup and automatic card registration through the integration frontend path.
- Moved vacuum water tank counters and refill timestamps from browser storage to Home Assistant Store.
- Ported the standalone water accounting loop to a 60-second server-side tick task.
- Added WebSocket commands for vacuum discovery, persisted state, settings, tank reset, and intro dismissal.

## [4.1.3] - 2026-05-12

### Fixed
- Removed Google Fonts CDN @import (1 occurrence(s)); now uses system font stack with Inter as the preferred locally-installed face.
- Normalized bare `font-family: "Inter", sans-serif` declarations to a complete cross-platform system stack.
- Privacy section in README: claim now matches behaviour (no CDN dependencies).

All notable changes to **Vacuum Water Monitor** are documented here.

## [4.0.0] - 2026-05-10

### Major
- **Split from `MacSiem/ha-tools` monorepo** into a dedicated standalone HACS plugin.
- Bundled Bento Design System CSS inline — no shared dependency required.
- Inlined `_haToolsEsc` XSS sanitizer.
- Persistence keys migrated to per-tool namespace `ha-vacuum-water-monitor-…` (clean break — old data under `ha-tools-…` is **not** migrated automatically).
- Donation/support footer added to the panel.
- Cross-tool discovery banner removed; each tool stands on its own.

### Compatibility

- Home Assistant ≥ 2024.1.0
