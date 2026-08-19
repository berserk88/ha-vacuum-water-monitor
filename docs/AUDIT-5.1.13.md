# Code audit — v5.1.13 (auto-discovery didn't actually work)

Scope: functional audit against the integration's advertised behavior —
"press Refilled once, everything else is automatic" — after v5.1.12 fixed
the UI-level input-focus bug but before it was confirmed that the
underlying accounting actually worked. Reported symptoms: sensors stuck on
"unknown", water % never changes after real cleaning cycles, refill button
not working, on a fresh install with the official `roborock` core
integration.

## Root cause

Every one of those symptoms traced back to the same thing: **auto-discovery
never actually resolved a device's tank capacity**, and **server-side water
accounting could never run for an auto-discovered device**, because both
depended on data that was only ever populated by manual configuration.

### Capacity resolution (`sensor_calculations.py::_model_tank_ml`)

Resolution order was: `device.water_total_ml` (manual) → `custom_calibration`
(manual) → a hardcoded table keyed by `brand_profile` (manual) **or by an
exact string match of the vacuum's `entity_id`** against ~25 keys like
`roborock_s8_maxv_ultra`. For that last fallback to ever match, the user's
actual `entity_id` would need to literally be
`vacuum.roborock_s8_maxv_ultra` — but HA generates entity_ids from whatever
name the user (or the vendor default) gave the device at setup, so this
essentially never matches a real install. A purely auto-discovered device
(no manual config at all) has none of the other three, so capacity was
*always* unresolved → sensors show "Unknown" → the card's `noWaterTracking`
check (mirrors the same broken logic client-side) hides the water gauge
*and the refill button* entirely, replacing them with "doesn't track water
levels." The refill button wasn't malfunctioning; it was never rendered.

### Server-side accounting (`tick.py::tick_device`)

Both dosing signals require per-device config keys that auto-discovery
never sets:
- Mop-wash detection has a reasonable fallback
  (`vac.attributes.get("status") or vac.state`), but the official Roborock
  integration exposes granular status (e.g. `washing_the_mop`) as a
  *separate* `sensor.<slug>_status` entity, not as an attribute of the
  vacuum entity itself — confirmed against home-assistant.io's Roborock
  integration docs. Without `status_sensor` configured, that fallback only
  ever sees the vacuum's coarse `VacuumActivity` state (cleaning / docked /
  idle / …), which never equals a `MOP_WASH_STATES` value.
- Cleaned-area dosing (`device.get("area_sensor")`) has **no fallback at
  all** — it's `None` unless a helper sensor is manually wired in the
  card's Settings tab.

So for a genuinely zero-config install, neither signal could ever fire:
`used_ml` stays at 0 forever, independent of the capacity problem above.

## Fix

Rather than guessing entity-naming conventions, both problems are solved by
reading Home Assistant's own registries — the actual source of truth for
"what does this vacuum's integration expose":

1. **`tick.py::_resolve_default_tank_ml`** — looks up the vacuum's HA
   device registry entry and matches `manufacturer + model` (normalized to
   a slug) against `DEFAULT_TANK_ML`, with a conservative token-subset
   fuzzy match (every token of a known key must appear in the detected
   slug; a bare "roborock" can't win on its own). Falls back to `None`
   ("unknown capacity") for anything unrecognized, exactly matching the
   integration's own stated design for unknown models — this is a strict
   improvement, not a behavior change for the "truly unknown model" case.

2. **`tick.py::_auto_detect_companions`** — finds the *other* entities that
   share the same HA device as the vacuum entity (via the entity registry's
   `device_id`), and matches them to a role (`status_sensor`, `area_sensor`,
   `mop_mode_entity`, `mop_intensity_entity`, `dock_error_sensor`) by name.
   Verified against the real entity set the official Roborock integration
   creates (Cleaning area / Total cleaning area / Status / Mop mode / Mop
   intensity / Dock error), including correctly excluding "Total cleaning
   area" (lifetime counter) in favor of "Cleaning area" (current-run,
   which is what the delta-based dosing logic expects).

3. **`tick.py::async_ensure_auto_config`** — runs every tick (idempotent;
   a no-op once resolved), fills in only whatever isn't already manually
   set on each known device, and persists the result into
   `configured_devices`/`user_devices` — whichever collection already owns
   that device, respecting the existing merge precedence
   (`user_devices` shadows `configured_devices` in both
   `build_vacuum_devices` and `_devices_to_tick`). Never overwrites a
   manual value.

4. **`__init__.py`** — the tick loop now runs auto-config detection before
   accounting each cycle, and notifies both the dispatcher signal (sensors
   re-read storage fresh) and the `VWM_EVENT` bus event with a settings
   payload (an already-open card picks up newly-resolved capacity live,
   without a page reload).

5. **`ha-vacuum-water-monitor.js::_getDevices`** — the pure-auto-discovery
   branch previously rebuilt bare `{vacuum_entity, name, icon}` objects and
   silently discarded `configured_devices` entirely, so even a fully
   backend-resolved device would never show a capacity in the card. Now
   merges `configured_devices` in by `vacuum_entity`.

## Verified against real integration data, not assumed

- Companion entity names/domains: home-assistant.io Roborock integration
  docs (Sensor/Select sections).
- `washing_the_mop` and the dock-error `water_empty` string: cross-checked
  against `python-roborock`'s `code_mappings.py` dock-error enum (0=ok,
  38=water_empty, …) via a GitHub issue quoting it directly, and
  `python-miio`'s `STATE_CODE_TO_STRING` (23 = "Washing the mop") for the
  underlying protocol semantics. Both strings the integration already
  hardcoded were correct — they simply never had a wired-up entity to read
  from.
- The vacuum entity's own `state`/`attributes` being coarse
  (`VacuumActivity` enum) rather than carrying granular status: confirmed
  via a `humbertogontijo/homeassistant-roborock` issue describing the
  `activity` property migration, and the official docs listing "Status" as
  a separate sensor entity.

## Verification

- `tests/test_tick_auto_detect.py` (new, 10 tests): companion detection
  correctness (including the "don't pick Total cleaning area" guard),
  capacity resolution (exact match, no-match, and a guard against a bare
  brand name matching too eagerly), `async_ensure_auto_config` persistence
  behavior (fills gaps, preserves manual overrides, second run is a no-op),
  and a full end-to-end test that runs two accounting ticks against a
  simulated official-Roborock-integration entity set and confirms both
  mop-wash and area-based dosing actually add `used_ml` — with zero manual
  configuration, matching the exact scenario the feature exists for.
- Full existing suite (28 tests total) still passes.
- jsdom smoke test and syntax/theming/a11y CI checks still pass.
