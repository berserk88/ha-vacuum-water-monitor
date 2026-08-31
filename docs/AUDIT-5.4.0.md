# Refactor notes — v5.4.0 (waste tank tracking, customizable dock messages)

## Customizable dock error messages

**Before:** `DOCK_WATER_EMPTY_STATES`, a fixed set of five slugs (`water_empty`,
`clean_water_empty`, `water_box_empty`, `water_tank_empty`, `no_water`), matched via
`_is_water_empty()`. Worked for Roborock (verified against `python-roborock`'s real enum in
the 5.1.13/5.3.0 rounds) but had no path to support a vacuum brand/integration that phrases
dock errors differently — the set was hardcoded, not configurable.

**Now:** three per-device fields — `dock_empty_message`, `dock_ok_message`,
`dock_full_message` — each with a sensible default (`Water empty` / `Ok` / `Waste water tank
full`, still the verified Roborock wording) but fully overridable via **Edit a tracked
vacuum → Set the dock error sensor**. `tick.py::matches_dock_message(value, message)`
replaces `_is_water_empty`, slugifying both sides before comparing so casing/formatting
differences still don't matter — `matches_dock_message("Water empty", "water_empty")` and
`matches_dock_message("Water empty", "Water empty")` both match, but a fully custom message
like `"CleanWaterTankEmpty"` only matches itself, not the built-in default (verified by
`MatchesDockMessageTest.test_custom_message_is_honored` and the end-to-end
`test_custom_messages_are_honored_end_to_end`).

**Behavior change, not just an addition:** the old reset condition was `curr_dock_err !=
"water_empty"` — *any* change away from the empty state fired a reset, including a
transition to some unrelated new error. The new condition requires the error to clear
*specifically* to the configured "ok" message. This was an explicit request ("once the error
message... is cleared (returns back to 'ok'...)"), and it's also a correctness fix: a duct
blockage reported moments after a real refill shouldn't have been treated as evidence of
that refill under the old logic.

## Dirty/waste water tank tracking

**Design decision — mirrored accounting, not independent modeling.** Rather than inventing a
second set of per-model constants (a waste-tank wash volume, waste-tank usage-per-m²,
waste-tank intensity factors), `tick_device()` adds the *same* computed volume to
`waste_used_ml` wherever it adds to `used_ml` — both the mop-wash fixed volume and the
area-based dose. This is explicitly a simplification (documented in the README's "Estimates,
not measurements" callout and in code comments), not a claim that clean-tank outflow exactly
equals waste-tank inflow. It was chosen because: (a) it's what "similar methodology and
logic to the clean water tank" most directly means, (b) there's no real per-model waste-tank
generation-rate data to build a proper independent model from, and (c) it avoids doubling
the size of `MODEL_DATABASE` and the config flow's brand/model picker for numbers that would
be no better than a guess anyway.

**Capacity:** `sensor_calculations.py::_waste_capacity_ml` — explicit `waste_total_ml` if
set (via the same edit step), else falls back to whatever `_water_capacity_ml` resolves for
the clean tank. No separate model-database entry for waste capacity; this keeps the common
case (waste tank is roughly the same size as the clean tank, true for most current-generation
combo docks) zero-config while still being overridable.

**State:** `storage.py`'s tank state dict gains `waste_used_ml` / `last_waste_reset_iso` /
`last_waste_reset_ts`, alongside (not replacing) the existing clean-tank fields — both
counters live in the same per-vacuum dict since they're accounting for the same vacuum's
activity, just two different physical tanks. `async_reset_waste_tank()` mirrors
`async_reset_tank()` exactly, on its own timestamp fields, so pressing one button (or one
auto-reset firing) never touches the other tank's counter or cooldown.

**Auto-empty:** mirrors the clean-tank auto-reset structure exactly — full-message-observed
→ ok-message-observed triggers a reset, gated by its own `RESET_COOLDOWN_SEC` window using
`last_waste_reset_ts` (not `last_reset_ts`, which the clean-tank reset uses). Verified
end-to-end (not just via the isolated matcher) by
`DockErrorResetEndToEndTest`/`EndToEndAccountingTest.test_waste_tank_auto_empties_on_full_to_ok_transition`,
which also asserts the clean tank's `used_ml`/`last_reset_iso` are completely untouched by a
waste-tank-specific empty event.

**Entities:** five new ones per vacuum (`sensor.py`: `WasteTankLevelSensor`,
`WasteWaterCollectedSensor`, `LastEmptiedSensor`; `binary_sensor.py`:
`WasteTankFullBinarySensor`; `button.py`: `EmptiedButton`), each a close structural mirror of
its clean-tank counterpart. `WasteTankFullBinarySensor` is the one asymmetry: it turns on
from *either* the estimated percentage crossing `WASTE_FULL_THRESHOLD_PERCENT` (90, a module
constant — not yet a UI-configurable setting, flagged in-code for a future revision if
wanted) *or* the dock error source directly reporting the full message, favoring whichever
signal is available/fires first, since the direct dock signal is more authoritative than the
accumulated estimate when it exists.

## Shared refactors

- `sensor_calculations.py`: `parse_refill_datetime`/`parse_waste_reset_datetime` now both
  delegate to a private `_parse_stored_datetime(tank_state, iso_key, ts_key)` rather than
  duplicating the ISO-parse-with-millis-fallback logic.
- `config_flow.py::_dock_error_updates` extended from 2 params (entity, attribute) to 6
  (+ the three messages + waste capacity), still returning the same `(updates, clear_keys)`
  shape `_upsert_device_entry` expects. Clearing the entity clears everything tied to it
  (attribute, all three messages, waste capacity) since none of them mean anything without a
  source entity; clearing an individual message/capacity field falls back to just that
  field's default without disturbing the rest — verified by
  `test_blank_message_clears_that_message_only`.

## Testing

- `tests/test_sensor_calculations.py`: new `WasteTankCalculationTests` class (7 tests) —
  fill-percent-from-collected-ml, capacity fallback-to-clean-tank, explicit-capacity
  override, unknown-capacity handling, 100%-clamp, and datetime parsing independence from
  the clean-tank refill timestamp. (Also fixed an unrelated pre-existing structural issue in
  this file where a prior edit had accidentally merged two test classes into one via a
  misplaced class boundary — restored to the original grouping.)
- `tests/test_tick_auto_detect.py`: `IsWaterEmptyTest` replaced with `MatchesDockMessageTest`
  (5 tests covering the generalized matcher); `EndToEndAccountingTest` gained
  `test_waste_tank_auto_empties_on_full_to_ok_transition` and
  `test_custom_messages_are_honored_end_to_end`; the existing mop-wash/area-dosing E2E test
  now also asserts `waste_used_ml` mirrors `used_ml`.
- `tests/test_config_flow_helpers.py`: `DockErrorUpdatesTest` extended from 4 to 8 tests
  covering the new message/capacity parameters, including the "clear one field without
  disturbing the others" and "zero/negative capacity clears the override" cases.
- Full suite: 72 tests, all passing.
