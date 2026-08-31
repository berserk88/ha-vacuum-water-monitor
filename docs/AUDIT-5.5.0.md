# Refactor notes — v5.5.0 (adaptive calibration, mop-intensity latching)

## What was requested vs. what's actually feasible

The person asked for the integration to "learn from previous runs and predict clean water
tank and dirty water tank levels based on past experience." Worth being explicit about the
constraint that shaped the whole design: **there is no water-level sensor anywhere in this
system.** No vacuum reports actual tank contents. The only real-world signal available is
"the person pressed refill/emptied" (or the dock reported the tank state), which tells us
capacity was reached at that moment — one scalar per cycle. That rules out real regression
or trained ML; what's implementable is an *adaptive calibration heuristic* that uses that
one signal per cycle to nudge a correction factor toward whatever would have made the
estimate accurate, converging over repeated cycles. This is documented plainly in the
module docstring, the button/FAQ copy, and here, rather than being oversold as "AI" or "ML."

## The two-layer model

The existing dosing formula (`usage_per_m2 × intensity_factor`, per mop mode/intensity) is
kept as the physically-motivated baseline — it's not thrown away. Layered on top: a
per-mop-intensity multiplicative `correction` factor, starting at 1.0 (no correction) and
adjusted by `_calibrate_model()` at each refill/empty event:

```
effective_dose = base_formula_dose × correction[intensity]
```

**Why per-intensity, not a single global correction:** the person specifically pointed to
the mop intensity entity as "where the mop ran or not" and the natural training signal —
different intensities plausibly consume water at different real-world rates than the static
factors assume, and collapsing everything into one correction would blur that away.

**Why a single scalar can't do more than proportional distribution:** one refill event gives
one number (actual capacity vs. estimated usage this cycle). If three different intensities
contributed dosing this cycle, that one number can't tell you which of the three was "more
wrong." `_calibrate_model` applies the *same relative* correction to every intensity that
contributed evidence this cycle, and leaves untouched whatever didn't contribute — see
`CalibrateModelTest.test_only_updates_intensities_with_pending_data_this_cycle`. Over many
cycles with genuinely different intensity mixes, this still lets each intensity's
correction drift toward its own accurate value, just more slowly than a real multi-variable
fit would.

**Learning rate:** `max(LEARNING_RATE_FLOOR, 1 / (2 + cycles_observed))` — starts at 0.5 for
a from-scratch model, decays toward a 0.05 floor. Verified to produce monotonically
decreasing error toward a known true ratio in
`PredictionModelEndToEndTest.test_correction_converges_toward_true_ratio_over_multiple_refill_cycles`,
which is deliberately a *realistic* simulation (each cycle's pending amount is
`base_estimate × current_correction`, matching how real dosing actually feeds back into the
next cycle's signal — an earlier draft of this test used a fixed pending amount per cycle
regardless of the model's own output, which doesn't reflect the real fixed-point dynamics
and gave a misleading non-convergent result).

**Guardrails**, each independently tested:
- `MIN_CALIBRATION_FRACTION` (0.3): skip a cycle where too little was estimated to have
  been used — e.g. a precautionary top-off of a tank that wasn't really empty — since that
  would imply a wildly wrong error ratio. Skipped cycles don't increment `cycles_observed`.
- `CALIBRATION_RATIO_MIN`/`MAX` (0.2–5.0): clamp how much a single cycle's error ratio can
  itself imply, so one anomalous cycle can't send a correction to an extreme in one step.
- `CORRECTION_MIN`/`MAX` (0.3–3.0): clamp the correction factor itself, regardless of how
  many cycles have pushed it there.

**Waste tank calibrates independently** — its own `correction`/`pending_ml_by_intensity`/
`cycles_observed`/`last_calibrated_iso`, calibrated against empty events and waste capacity
rather than refill events and clean-tank capacity. The two models can and will diverge over
time even though 5.4.0 mirrors the *base* dosing 1:1 into both counters, since real-world
waste-tank behavior (evaporation, incomplete extraction, etc.) isn't necessarily identical
to clean-tank consumption.

## Mop intensity/mode latching

This was flagged directly and is a real correctness issue, not a hypothetical: the mop
mode/intensity select entities are only meaningful while the vacuum is actively cleaning.
Verified against the official `roborock` integration's behavior — these revert to a
default/off value once cleaning ends. The mop-wash dosing event, however, fires *after*
cleaning ends (vacuum returns to the dock, then washes the mop) — so a naive re-read at
that point would see the reverted value, misattributing the wash volume to the wrong (or no)
intensity bucket, both for the dosing formula's `intensity_factor` lookup and for the
calibration model's per-intensity accumulation.

Fix: `tick_device()` now computes `is_cleaning` first, and while true, latches the last
valid (non-off, non-unavailable) mop mode/intensity value into
`state["latched_mop_mode"]`/`state["latched_mop_intensity"]`. Once cleaning stops, the
*effective* mode/intensity used for everything downstream (dosing rate lookup, calibration
attribution) falls back to the latch instead of re-reading the live entity. Verified in
`MopIntensityLatchTest`:
- `test_wash_event_after_cleaning_uses_latched_intensity_not_reverted_value` — the exact
  scenario described: cleaning ends, intensity reverts to `"Off"` live, mop-wash fires, and
  the dose is still correctly attributed to the intensity that was actually used.
- `test_live_value_used_while_still_cleaning_not_stale_latch` — a latch from a *previous*
  session must not leak into a currently-active session at a different intensity; the live
  value takes priority whenever `is_cleaning` is true.

## A real bug found and fixed along the way

While implementing the above, found that `mop_mode_raw`/`mop_intensity_raw` were compared
against `"off"` and looked up in `DEFAULT_INTENSITY_FACTOR`/`DEFAULT_USAGE_PER_M2` (both
lowercase-keyed) *without any case normalization*. A select entity reporting `"Off"`,
`"Medium"`, `"High"` (capitalized — exactly what the person described for
`select.roborock_qrevo_maxv_mop_intensity`, and a very plausible way for any such entity to
report its state) would never match either the off-check or the rate lookup, silently
falling through to the default 1.0 multiplier and never triggering `mop_off`. Fixed by
running both values through the existing `slugify()` before any comparison — verified by
`CaseNormalizationTest`, which fails against the pre-fix code path (capitalized `"High"`
would previously compute the same dose as an unrecognized/default value, not the actual
high-intensity rate).

## Customizable mop entities

Mirrors the dock-error override pattern from 5.3.0: `config_flow.py::_mop_entity_updates`
plus a new `async_step_edit_mop_entities` step under **Edit a tracked vacuum**. The
`_async_apply_device_updates` persistence helper (previously inlined only in
`async_step_edit_dock_error`) was factored out so this step — and any future one needing
the same "patch fields on an already-tracked vacuum" logic — doesn't triple the boilerplate.

## Clear prediction model button

`button.py::ClearPredictionModelButton`, following the codebase's existing per-button-type
manager convention (matching `RefillButton`/`EmptiedButton` rather than introducing a new
shared-manager abstraction, to avoid touching already-shipped, already-tested code for a
proportionality-only win). Backed by `storage.py::async_reset_prediction_models`, which
resets `water_model`/`waste_model` to their unlearned defaults while explicitly leaving tank
levels, counters, and the mop-intensity latches untouched — verified by
`ResetPredictionModelsTest.test_clears_both_models_but_not_tank_levels_or_latches`.

## Testing

- `tests/test_prediction_model.py` (new, 17 tests): case-normalization fix, latching
  (including the stale-latch-must-not-leak case), `_ensure_model`/`_accumulate_pending`/
  `_calibrate_model` in isolation covering every guardrail, the realistic multi-cycle
  convergence proof, and the reset button's storage method.
- `tests/test_config_flow_helpers.py`: 4 new tests for `_mop_entity_updates`.
- Full suite: 93 tests, all passing. The entire pre-existing suite (72 tests before this
  round) passed *unchanged* after the tick_device rewrite, since a fresh/default correction
  of 1.0 is a no-op on the existing dosing formula — the new layer is purely additive until
  a calibration cycle actually occurs.
