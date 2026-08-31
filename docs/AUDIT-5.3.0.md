# Refactor notes — v5.3.0 (rename, edit-in-place, dock error override)

Three user-requested changes, all additive to the v5.2.0 config-flow architecture.

## 1. Edit a tracked vacuum without removing and re-adding it

**Config flow:** new `async_step_edit_vacuum` (pick a tracked vacuum) → `async_show_menu`
with two options, both operating on the same vacuum:

- `async_step_edit_brand_model` re-enters the *same* `select_brand`/`select_model`/
  `custom_capacity` step chain the "Add a vacuum" flow uses, distinguished only by instance
  state (`self._editing`/`self._target_entity_id` already set from the picked vacuum, no
  new entity to choose). Defaults are computed by `_default_brand()`/`_default_model()`,
  which prefer the vacuum's *already-stored* `manufacturer`/`model` over a fresh
  `guess_brand_model()` call — editing should show what's currently configured, not
  re-guess from scratch.
- `async_step_edit_dock_error` is new (see #2 below).

**Persistence:** `_build_new_device_entry` (5.2.0, add-only) is replaced by
`_upsert_device_entry(devices, vacuum_entity, updates, clear_keys=())` — update in place if
the vacuum is already tracked, append if not. One function now backs both the initial add
and every edit path. Switching from a database model to a custom capacity passes
`clear_keys=("brand_profile", "manufacturer", "model")` so a stale brand/model doesn't
survive alongside the new capacity; switching *to* a database model overwrites all of
`brand_profile`/`manufacturer`/`model`/`water_total_ml` directly via `updates`, no
`clear_keys` needed since every relevant field is being explicitly set.

Auto-detected companion fields (`status_sensor`, `area_sensor`, etc.) are untouched by an
edit, since `updates` only contains what the brand/model (or dock-error) step actually
changed — verified by `UpsertDeviceEntryTest.test_updates_in_place_when_present`.

## 2. Dock error sensor override, including attribute-based sources

**The problem:** `tick.py`'s dock-error reader only ever looked at an entity's `.state`
(`_state_value`), and auto-detection only ever matched entities *named* "dock error". A
setup where the water-empty signal lives on an *attribute* of some other entity (e.g. a
docking-station entity whose `.state` is `"docked"` but whose `error` attribute reads
`"Water empty"` when empty) had no way to be picked up at all — auto-detection wouldn't
find it (wrong entity), and there was no manual override.

**Fix:**
- New device field `dock_error_attribute` (optional). `tick.py::_dock_error_value(hass,
  device)` reads `device["dock_error_sensor"]`'s `.attributes[dock_error_attribute]` when
  set, otherwise falls back to `.state` exactly as before — existing auto-detected configs
  are unaffected.
- `_is_water_empty(value)` normalizes via the existing `slugify()` before comparing against
  a small vocabulary (`DOCK_WATER_EMPTY_STATES`), so `"Water empty"`, `"water_empty"`,
  `"Water Empty"`, etc. are all recognized as the same signal — the user shouldn't have to
  type an exact-case match string. The previous code did a bare `== "water_empty"` compare,
  which the raw python-roborock enum value satisfies but a human-readable attribute value
  like `"Water empty"` would not have.
- Config flow: `async_step_edit_dock_error` — entity selector (no domain restriction, since
  the source could be any domain depending on the user's setup) plus an optional text field
  for the attribute name, pre-filled with whatever's currently configured. Leaving the
  entity blank clears both fields and falls back to auto-detection.
  `_dock_error_updates(entity_id, attribute)` is the pure function computing exactly what
  to set/clear, extracted for direct unit testing rather than only exercising it through the
  framework-dependent step method.

**Verification:** `DockErrorResetEndToEndTest.test_reset_fires_from_attribute_with_human_readable_text`
runs two real accounting ticks — first observing a `"Water empty"` attribute value, then
observing it clear to `"Ok"` — and asserts the auto-reset actually fires and zeroes
`used_ml`. Not just the isolated helper functions; the full loop.

## 3. Integration renamed: `ha_vacuum_water_monitor` → `vacuum_water_level`

Requested so this fork doesn't conflict with anyone else's install of the original project
it was forked from. Since HACS installs to `custom_components/<domain>/`, two integrations
sharing the same domain string can't coexist — only a cosmetic display-name change wouldn't
have actually solved that.

**Mechanical scope:** folder rename (`git mv`), `manifest.json` domain + name,
`const.py::DOMAIN`, `hacs.json` name, every module docstring, all four test files' `PKG_DIR`
paths and stub domain strings. `STORAGE_KEY = DOMAIN` and every dispatcher
signal/event name derive from `DOMAIN`, so those needed no separate edits.

**Data safety:** new `__init__.py::_async_migrate_legacy_storage`, called at the top of
`async_setup_entry` before anything else touches storage. Directly instantiates two
`homeassistant.helpers.storage.Store` objects (old domain, new domain) rather than going
through `VacuumWaterStorage`, specifically so it can distinguish "no file on disk yet" from
"file exists with default-shaped content" — loading through the normal storage wrapper
always returns a fully-populated default shape, which would make an empty new install
indistinguishable from a genuinely-empty legacy one. Copies the legacy file forward
byte-for-byte if (a) legacy data exists and (b) the new domain doesn't have its own data
yet; never overwrites existing new-domain data. Runs on every startup (cheap — two file
reads) but is a no-op after the first successful migration, or forever if there was nothing
to migrate.

**What does *not* migrate automatically:** the config entry itself (the "Vacuum water
level" integration card in Settings → Devices & services). Home Assistant has no built-in
way to rename a domain out from under an existing config entry — this shows up as a new
integration to add, distinct from any previous install. The storage migration above ensures
that once added, it immediately has all previously-tracked vacuums and tank history rather
than starting empty.

## Testing

- `tests/test_config_flow_helpers.py`: `_build_new_device_entry` tests replaced with
  `_upsert_device_entry` (append/update-in-place/clear_keys/non-mutation), plus new
  `_find_device_entry` and `_dock_error_updates` coverage. 17 tests in this file.
- `tests/test_tick_auto_detect.py`: new `DockErrorValueTest`, `IsWaterEmptyTest`, and
  `DockErrorResetEndToEndTest` classes. 22 tests in this file.
- `tests/test_storage_migration.py` (new): loads `__init__.py` with a fake in-memory `Store`
  shared across two domain keys, proving the copy-forward/never-overwrite/no-op-when-empty
  behavior directly — not just by inspection. 3 tests.
- Full suite: 59 tests, all passing.
