# Refactor notes — v5.2.0 (card removed, config-flow driven)

Scope: at the user's request, remove the Lovelace card entirely and rework the integration
around Home Assistant's native config flow — entity/brand/model selection instead of a
card UI, plus new `binary_sensor`/`button` entities for warnings and refills.

## What changed and why

**Card removed.** `ha-vacuum-water-monitor.js`, its bundled `www/` copy, `websocket_api.py`
(purely a frontend-facing WS API), and all frontend/HTTP static-path registration in
`__init__.py` are gone. The `frontend`/`http`/`websocket_api` manifest dependencies go with
them — this integration now only needs Home Assistant core.

**Tracking is now opt-in, not blanket auto-discovery.** Previously, `list_vacuums()`
results were merged into every device list via `setdefault` — meaning every `vacuum.*`
entity in the house silently got sensors, whether the user wanted it tracked or not. This
was also the root of the "still not linking my vacuum to the database" problem: the same
blanket-discovery path had no way to ask the user which brand/model a device was, so
capacity resolution depended entirely on registry auto-detection succeeding on its own. The
config flow now requires an explicit choice per vacuum, resolved into three places that
used to auto-populate the device list:

- `tick.py::_devices_to_tick` — only ticks `configured_devices`/`user_devices`, no more
  `list_vacuums()` fallback loop.
- `tick.py::async_ensure_auto_config` — only enriches vacuums already present in
  `configured_devices`/`user_devices`; it fills in gaps (companion sensors, capacity if the
  config flow's picker wasn't used) but never creates a new entry.
- `sensor_calculations.py::build_vacuum_devices` — `discovered_vacuums` now only backfills a
  friendly name onto an *already-tracked* device; the block that used it to `setdefault` a
  brand-new device entry is removed. This function is shared by `sensor.py`, the new
  `button.py`, and the new `binary_sensor.py`, so the opt-in behavior is consistent across
  every platform without needing to remember to omit an argument in three separate files.

**New config flow (`config_flow.py`).** `OptionsFlow` menu: Add a vacuum / Stop tracking a
vacuum / Edit warning thresholds. "Add a vacuum" is a 2-3 step wizard: entity picker (built
from `EntitySelector(domain="vacuum")`, excluding already-tracked entities) → brand picker
(pre-selected via `tick.py::guess_brand_model`, the same device-registry resolution the
5.1.13 auto-detection uses) → model picker (capacity shown inline, e.g. "S8 MaxV Ultra (3000
mL)") → or "Other / custom capacity" at either step for a manual mL number input. Selection
persists straight into `configured_devices`; `async_ensure_auto_config` picks up companion
sensors (status/area/mop mode/dock error) on the next tick exactly as before.

Used the modern `OptionsFlow` pattern (`self.config_entry` provided automatically by the
base class since HA 2024.12 — no `__init__(self, config_entry)` override, which is now a
deprecation warning heading for removal in 2025.12). `hacs.json`'s minimum HA version is
bumped to 2024.12.0 to match.

**Model database restructured.** `sensor_calculations.py`'s flat `DEFAULT_TANK_ML` dict
(used for capacity lookups) is now *generated from* a new `MODEL_DATABASE: dict[brand,
dict[model, tank_ml]]`, which is what actually powers the config flow's two-step selector.
Verified every one of the 27 existing entries slugifies back to its original flat key, so
nothing already relying on `DEFAULT_TANK_ML` (device-registry resolution, entity_id-slug
fallback) changed behavior.

**New entities, replacing what the card's UI used to provide:**
- `binary_sensor.<vacuum>_water_low` (device_class `problem`) — on at or below the
  warning/critical threshold, with a `severity` attribute. Gives automations a clean
  on/off trigger instead of a `numeric_state` check against the percentage sensor.
- `button.<vacuum>_refilled` — replaces the card's Refilled button. Calls the same
  `storage.async_reset_tank()` the old WS command used, then dispatches immediately so
  sensors update without waiting for the next 60s tick.

**Removed: "Next maintenance due" sensor.** It read `settings.maintenance_items`, a field
only the removed card's Settings tab could ever write. An entity that could never
functionally hold data doesn't belong in a codebase being simplified — `next_maintenance_due()`
itself stays in `sensor_calculations.py` (harmless, still covered by its existing test) in
case a future config-flow step wants to reintroduce it, but it's no longer wired to an
entity.

## Testing approach

The `ConfigFlow`/`OptionsFlow` step methods depend on Home Assistant's config_entries
framework (form rendering, flow context/step routing, selector validation) in ways that
aren't practical to stub faithfully in this repo's dependency-free test environment without
risking the tests validating a hand-built fake harness rather than real behavior. Instead,
the actual decision logic each step calls was extracted into plain functions —
`_available_vacuum_entities`, `_tracked_vacuum_entities`, `_build_new_device_entry` — and
`tests/test_config_flow_helpers.py` (8 tests) covers those directly: which vacuums are
offered for tracking, which are considered already-tracked, and exactly what gets persisted
for a database-matched pick vs. a custom-capacity pick (including that a custom pick must
never fabricate a brand/model the user didn't confirm).

`sensor_calculations.py` and `tick.py` changes are covered by updates to the existing
`tests/test_sensor_calculations.py` and `tests/test_tick_auto_detect.py` suites, asserting
the new opt-in behavior explicitly (e.g. `test_never_creates_entry_for_unadded_vacuum`,
`test_discovery_only_backfills_name_never_creates_device`).

Full suite: 38 tests, all passing. `hassfest.yml` and `tests.yml` CI still apply unchanged;
`validate.yml` (JS-only) is removed since there's no JS left to validate.
