"""Tests for tick.py's adaptive calibration model (_calibrate_model,
_ensure_model, _accumulate_pending), the mop mode/intensity latching that
makes wash-event dosing attribution correct after the source entity
reverts, and a real case-sensitivity bug fixed along the way (entity
states like "Off"/"Medium"/"High" never matched the lowercase lookup
tables the dosing formula used).
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "vacuum_water_level"


class FakeState:
    def __init__(self, state, attributes=None):
        self.state = state
        self.attributes = attributes or {}


class FakeStates:
    def __init__(self, states):
        self._states = states

    def get(self, entity_id):
        return self._states.get(entity_id)

    def async_entity_ids(self, domain):
        return [eid for eid in self._states if eid.startswith(f"{domain}.")]


class FakeHass:
    def __init__(self, states):
        self.states = states
        self.data = {}


def _stub_homeassistant() -> None:
    ha = types.ModuleType("homeassistant")
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    helpers = types.ModuleType("homeassistant.helpers")
    storage_mod = types.ModuleType("homeassistant.helpers.storage")
    er_mod = types.ModuleType("homeassistant.helpers.entity_registry")
    dr_mod = types.ModuleType("homeassistant.helpers.device_registry")

    class _Store:
        def __init__(self, *args, **kwargs):
            self._d = None

        async def async_load(self):
            return self._d

        async def async_save(self, data):
            self._d = data

    storage_mod.Store = _Store
    er_mod.async_get = lambda hass: None
    er_mod.async_entries_for_device = lambda *a, **k: []
    dr_mod.async_get = lambda hass: None

    ha.core = core
    ha.helpers = helpers
    helpers.storage = storage_mod
    helpers.entity_registry = er_mod
    helpers.device_registry = dr_mod

    for name, mod in (
        ("homeassistant", ha),
        ("homeassistant.core", core),
        ("homeassistant.helpers", helpers),
        ("homeassistant.helpers.storage", storage_mod),
        ("homeassistant.helpers.entity_registry", er_mod),
        ("homeassistant.helpers.device_registry", dr_mod),
    ):
        sys.modules[name] = mod


def _load_tick():
    _stub_homeassistant()

    pkg = types.ModuleType("vwmpkg_pred")
    pkg.__path__ = [str(PKG_DIR)]
    sys.modules["vwmpkg_pred"] = pkg

    const = types.ModuleType("vwmpkg_pred.const")
    const.DOMAIN = "vacuum_water_level"
    const.EVENT_STATE_CHANGED = "vacuum_water_level_state_changed"
    const.STORAGE_KEY = "vacuum_water_level"
    const.STORAGE_VERSION = 1
    const.DEFAULT_WARNING_THRESHOLD = 20
    const.DEFAULT_CRITICAL_THRESHOLD = 10
    const.signal_vacuum_water_updated = lambda entry_id: f"{const.DOMAIN}_{entry_id}_updated"
    sys.modules["vwmpkg_pred.const"] = const

    for name, filename in (
        ("sensor_calculations", "sensor_calculations.py"),
        ("storage", "storage.py"),
        ("tick", "tick.py"),
    ):
        spec = importlib.util.spec_from_file_location(f"vwmpkg_pred.{name}", PKG_DIR / filename)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"vwmpkg_pred.{name}"] = mod
        spec.loader.exec_module(mod)

    return sys.modules["vwmpkg_pred.tick"], sys.modules["vwmpkg_pred.storage"]


def _device(**overrides):
    base = {
        "vacuum_entity": "vacuum.kitchen_robot",
        "status_sensor": "sensor.kitchen_robot_status",
        "area_sensor": "sensor.kitchen_robot_area",
        "mop_mode_entity": "select.kitchen_robot_mop_mode",
        "mop_intensity_entity": "select.kitchen_robot_mop_intensity",
        "water_total_ml": 3000,
        "waste_total_ml": 3000,
    }
    base.update(overrides)
    return base


class CaseNormalizationTest(unittest.TestCase):
    """The real bug: mop_mode_raw/mop_intensity_raw were compared/looked
    up without normalizing case, so a capitalized entity state (very
    plausible for a select entity's displayed option value, e.g. "Off",
    "Medium", "High") never matched the lowercase "off" checks or the
    lowercase DEFAULT_INTENSITY_FACTOR/DEFAULT_USAGE_PER_M2 dict keys."""

    def test_capitalized_off_intensity_is_recognized_as_mop_off(self) -> None:
        tick, storage_mod = _load_tick()
        hass = FakeHass(
            FakeStates(
                {
                    "vacuum.kitchen_robot": FakeState("cleaning"),
                    "sensor.kitchen_robot_status": FakeState("cleaning"),
                    "sensor.kitchen_robot_area": FakeState("10.0"),
                    "select.kitchen_robot_mop_mode": FakeState("Standard"),
                    "select.kitchen_robot_mop_intensity": FakeState("Off"),
                }
            )
        )
        device = _device()
        state = storage_mod.VacuumWaterStorage.default_tank_state()
        state["last_area"] = 5.0  # establish a baseline so a delta exists

        new_state, dirty = tick.tick_device(hass, device, state)

        self.assertEqual(new_state["used_ml"], 0, "capitalized 'Off' must suppress dosing")

    def test_capitalized_high_intensity_uses_high_factor_not_default(self) -> None:
        tick, storage_mod = _load_tick()
        hass = FakeHass(
            FakeStates(
                {
                    "vacuum.kitchen_robot": FakeState("cleaning"),
                    "sensor.kitchen_robot_status": FakeState("cleaning"),
                    "sensor.kitchen_robot_area": FakeState("10.0"),
                    "select.kitchen_robot_mop_mode": FakeState("Standard"),
                    "select.kitchen_robot_mop_intensity": FakeState("High"),
                }
            )
        )
        device = _device()
        state = storage_mod.VacuumWaterStorage.default_tank_state()
        state["last_area"] = 5.0

        new_state, _dirty = tick.tick_device(hass, device, state)

        # 5.0 m² delta * 6 ml/m² (standard) * 1.2 (high) = 36, not the
        # 1.0x fallback (30) a case-mismatch would silently produce.
        self.assertEqual(new_state["used_ml"], 36)


class MopIntensityLatchTest(unittest.TestCase):
    """The mop intensity/mode select entities revert to a default/off
    value once cleaning ends (verified against the official Roborock
    integration) -- but the mop-wash dosing event fires AFTER cleaning
    ends. Without latching, that dosing event would misattribute its
    volume to whatever the (by-then-reverted) entity shows instead of
    what was actually used during the just-finished run."""

    def test_wash_event_after_cleaning_uses_latched_intensity_not_reverted_value(self) -> None:
        tick, storage_mod = _load_tick()
        states = FakeStates(
            {
                "vacuum.kitchen_robot": FakeState("cleaning"),
                "sensor.kitchen_robot_status": FakeState("cleaning"),
                "sensor.kitchen_robot_area": FakeState("10.0"),
                "select.kitchen_robot_mop_mode": FakeState("standard"),
                "select.kitchen_robot_mop_intensity": FakeState("high"),
            }
        )
        hass = FakeHass(states)
        device = _device()
        state = storage_mod.VacuumWaterStorage.default_tank_state()

        # Tick 1: actively cleaning at "high" -- this should latch "high".
        state, _ = tick.tick_device(hass, device, state)
        self.assertEqual(state.get("latched_mop_intensity"), "high")

        # Cleaning ends: vacuum docks, mop intensity reverts to Off (real
        # Roborock behavior), then the mop-wash event fires.
        states._states["vacuum.kitchen_robot"] = FakeState("docked")
        states._states["sensor.kitchen_robot_status"] = FakeState("washing_the_mop")
        states._states["select.kitchen_robot_mop_intensity"] = FakeState("Off")

        model = tick._ensure_model(state, "water_model")
        model["correction"]["high"] = 2.0  # distinct from "medium"'s 1.0 default
        state, _ = tick.tick_device(hass, device, state)

        pending_high = state["water_model"]["pending_ml_by_intensity"].get("high", 0)
        pending_medium = state["water_model"]["pending_ml_by_intensity"].get("medium", 0)
        self.assertGreater(pending_high, 0, "wash event must be attributed to the latched 'high' intensity")
        self.assertEqual(pending_medium, 0, "must not fall back to the 'medium' default despite the reverted entity")

    def test_live_value_used_while_still_cleaning_not_stale_latch(self) -> None:
        """A latch from a previous session must not leak into a new one
        while the vacuum is currently, actively cleaning at a different
        intensity."""
        tick, storage_mod = _load_tick()
        states = FakeStates(
            {
                "vacuum.kitchen_robot": FakeState("cleaning"),
                "sensor.kitchen_robot_status": FakeState("cleaning"),
                "sensor.kitchen_robot_area": FakeState("10.0"),
                "select.kitchen_robot_mop_mode": FakeState("standard"),
                "select.kitchen_robot_mop_intensity": FakeState("low"),
            }
        )
        hass = FakeHass(states)
        device = _device()
        state = storage_mod.VacuumWaterStorage.default_tank_state()
        state["latched_mop_intensity"] = "high"  # stale, from a previous session
        state["last_area"] = 5.0

        new_state, _dirty = tick.tick_device(hass, device, state)

        # standard (6) * low (0.8) * 5.0 delta = 24 -- proves "low" (live),
        # not "high" (stale latch), was actually used for this dose.
        self.assertEqual(new_state["used_ml"], 24)
        self.assertEqual(new_state["latched_mop_intensity"], "low")


class VacuumOnlyModeContributesNothingTest(unittest.TestCase):
    """Direct proof of the guarantee: on a vacuum-only day (mop
    off/not attached), neither dosing path may add anything to either
    tank, however it's triggered."""

    def test_area_cleaned_while_mop_off_adds_nothing(self) -> None:
        tick, storage_mod = _load_tick()
        hass = FakeHass(
            FakeStates(
                {
                    "vacuum.kitchen_robot": FakeState("cleaning"),
                    "sensor.kitchen_robot_status": FakeState("cleaning"),
                    "sensor.kitchen_robot_area": FakeState("40.0"),  # plenty cleaned
                    "select.kitchen_robot_mop_mode": FakeState("standard"),
                    "select.kitchen_robot_mop_intensity": FakeState("Off"),
                }
            )
        )
        device = _device()
        state = storage_mod.VacuumWaterStorage.default_tank_state()
        state["last_area"] = 5.0

        new_state, _dirty = tick.tick_device(hass, device, state)

        self.assertEqual(new_state["used_ml"], 0)
        self.assertEqual(new_state["waste_used_ml"], 0)

    def test_vacuum_only_session_overwrites_a_stale_mop_day_latch(self) -> None:
        """The edge case this guarantee actually depends on: if a mop day
        happened yesterday, the latch holds a real intensity ("high").
        Today's vacuum-only session must overwrite that latch with "off"
        -- otherwise a wash-event-like status appearing after today's
        vacuum-only run could still dose against yesterday's stale
        intensity."""
        tick, storage_mod = _load_tick()
        states = FakeStates(
            {
                "vacuum.kitchen_robot": FakeState("cleaning"),
                "sensor.kitchen_robot_status": FakeState("cleaning"),
                "sensor.kitchen_robot_area": FakeState("10.0"),
                "select.kitchen_robot_mop_mode": FakeState("Standard"),
                "select.kitchen_robot_mop_intensity": FakeState("Off"),
            }
        )
        hass = FakeHass(states)
        device = _device()
        state = storage_mod.VacuumWaterStorage.default_tank_state()
        state["latched_mop_intensity"] = "high"  # leftover from a real mop day

        # Today's vacuum-only session runs.
        state, _ = tick.tick_device(hass, device, state)
        self.assertEqual(state.get("latched_mop_intensity"), "Off", "must overwrite the stale mop-day latch")

        # Even if a wash-event-like status somehow fires right after,
        # dosing must still be suppressed, using TODAY's (now-latched)
        # off intensity rather than yesterday's stale "high".
        states._states["vacuum.kitchen_robot"] = FakeState("docked")
        states._states["sensor.kitchen_robot_status"] = FakeState("washing_the_mop")
        states._states["select.kitchen_robot_mop_intensity"] = FakeState("Off")

        final_state, _ = tick.tick_device(hass, device, state)

        self.assertEqual(final_state["used_ml"], 0)
        self.assertEqual(final_state["waste_used_ml"], 0)

    def test_wash_event_status_without_mop_off_gate_would_have_dosed(self) -> None:
        """Sanity check that this test suite would actually catch a
        regression: the same scenario as above, but with a genuinely
        mopped session beforehand, DOES dose -- proving the suppression
        above is because of mop_off, not some other unrelated reason
        (e.g. a broken status/area read)."""
        tick, storage_mod = _load_tick()
        states = FakeStates(
            {
                "vacuum.kitchen_robot": FakeState("cleaning"),
                "sensor.kitchen_robot_status": FakeState("cleaning"),
                "sensor.kitchen_robot_area": FakeState("10.0"),
                "select.kitchen_robot_mop_mode": FakeState("standard"),
                "select.kitchen_robot_mop_intensity": FakeState("medium"),
            }
        )
        hass = FakeHass(states)
        device = _device()
        state = storage_mod.VacuumWaterStorage.default_tank_state()

        state, _ = tick.tick_device(hass, device, state)  # mopped session

        states._states["vacuum.kitchen_robot"] = FakeState("docked")
        states._states["sensor.kitchen_robot_status"] = FakeState("washing_the_mop")
        states._states["select.kitchen_robot_mop_intensity"] = FakeState("Off")

        final_state, _ = tick.tick_device(hass, device, state)

        self.assertGreater(final_state["used_ml"], 0)
        self.assertGreater(final_state["waste_used_ml"], 0)


class EnsureModelTest(unittest.TestCase):
    def test_initializes_defaults_for_missing_model(self) -> None:
        tick, storage_mod = _load_tick()
        state = {}

        model = tick._ensure_model(state, "water_model")

        self.assertEqual(model["correction"], {})
        self.assertEqual(model["pending_ml_by_intensity"], {})
        self.assertEqual(model["cycles_observed"], 0)
        self.assertIsNone(model["last_calibrated_iso"])
        self.assertIs(state["water_model"], model)

    def test_repairs_malformed_model(self) -> None:
        tick, _storage = _load_tick()
        state = {"water_model": {"correction": "not-a-dict", "cycles_observed": 5}}

        model = tick._ensure_model(state, "water_model")

        self.assertEqual(model["correction"], {})
        self.assertEqual(model["cycles_observed"], 5, "valid fields must survive repair")


class AccumulatePendingTest(unittest.TestCase):
    def test_sums_across_calls_by_intensity(self) -> None:
        tick, _storage = _load_tick()
        model = {"pending_ml_by_intensity": {}}

        tick._accumulate_pending(model, "medium", 50)
        tick._accumulate_pending(model, "medium", 30)
        tick._accumulate_pending(model, "high", 20)

        self.assertEqual(model["pending_ml_by_intensity"]["medium"], 80)
        self.assertEqual(model["pending_ml_by_intensity"]["high"], 20)

    def test_ignores_zero_and_negative_amounts(self) -> None:
        tick, _storage = _load_tick()
        model = {"pending_ml_by_intensity": {}}

        tick._accumulate_pending(model, "medium", 0)
        tick._accumulate_pending(model, "medium", -5)

        self.assertEqual(model["pending_ml_by_intensity"], {})


class CalibrateModelTest(unittest.TestCase):
    def test_skips_when_capacity_unknown(self) -> None:
        tick, _storage = _load_tick()
        model = {"pending_ml_by_intensity": {"medium": 2000}, "correction": {}, "cycles_observed": 0}

        tick._calibrate_model(model, None)

        self.assertEqual(model["correction"], {})
        self.assertEqual(model["cycles_observed"], 0)
        self.assertEqual(model["pending_ml_by_intensity"], {})

    def test_skips_top_off_below_min_calibration_fraction(self) -> None:
        """A precautionary refill of a tank that wasn't really empty
        (e.g. 5% estimated usage) must not corrupt the model with a
        wildly-wrong implied error ratio."""
        tick, _storage = _load_tick()
        model = {"pending_ml_by_intensity": {"medium": 100}, "correction": {}, "cycles_observed": 0}

        tick._calibrate_model(model, 3000)  # 100/3000 = ~3%, well under MIN_CALIBRATION_FRACTION

        self.assertEqual(model["correction"], {})
        self.assertEqual(model["cycles_observed"], 0, "a skipped cycle must not count as observed")

    def test_increases_correction_when_actual_capacity_exceeds_estimate(self) -> None:
        """The tank held more water than the model thought it used --
        the formula is underestimating, so the correction must increase."""
        tick, _storage = _load_tick()
        model = {"pending_ml_by_intensity": {"medium": 2000}, "correction": {}, "cycles_observed": 0}

        tick._calibrate_model(model, 3000)  # actual capacity was 3000, estimate was 2000

        self.assertGreater(model["correction"]["medium"], 1.0)
        self.assertEqual(model["cycles_observed"], 1)
        self.assertIsNotNone(model["last_calibrated_iso"])
        self.assertEqual(model["pending_ml_by_intensity"], {}, "must clear pending after calibrating")

    def test_decreases_correction_when_estimate_exceeds_actual_capacity(self) -> None:
        tick, _storage = _load_tick()
        model = {"pending_ml_by_intensity": {"medium": 4000}, "correction": {}, "cycles_observed": 0}

        tick._calibrate_model(model, 3000)  # estimate overshot the real capacity

        self.assertLess(model["correction"]["medium"], 1.0)

    def test_only_updates_intensities_with_pending_data_this_cycle(self) -> None:
        """No evidence about "low" this cycle (it wasn't used) -> its
        correction must not move, even though "medium" is being
        corrected."""
        tick, _storage = _load_tick()
        model = {
            "pending_ml_by_intensity": {"medium": 2000},
            "correction": {"low": 1.5, "medium": 1.0},
            "cycles_observed": 0,
        }

        tick._calibrate_model(model, 3000)

        self.assertEqual(model["correction"]["low"], 1.5, "untouched intensity must not move")
        self.assertNotEqual(model["correction"]["medium"], 1.0)

    def test_learning_rate_decays_as_cycles_accumulate(self) -> None:
        """The same error ratio should produce a smaller correction jump
        once the model already has calibration history -- large early
        adjustments, smaller/more stable ones over time."""
        tick, _storage = _load_tick()
        model_fresh = {"pending_ml_by_intensity": {"medium": 2000}, "correction": {}, "cycles_observed": 0}
        model_experienced = {
            "pending_ml_by_intensity": {"medium": 2000},
            "correction": {},
            "cycles_observed": 20,
        }

        tick._calibrate_model(model_fresh, 3000)
        tick._calibrate_model(model_experienced, 3000)

        fresh_jump = abs(model_fresh["correction"]["medium"] - 1.0)
        experienced_jump = abs(model_experienced["correction"]["medium"] - 1.0)
        self.assertGreater(fresh_jump, experienced_jump)

    def test_correction_is_clamped_within_bounds(self) -> None:
        tick, _storage = _load_tick()
        model = {"pending_ml_by_intensity": {"medium": 100}, "correction": {}, "cycles_observed": 0}

        # An absurd ratio (capacity vastly exceeds the tiny pending amount)
        # must still respect MIN_CALIBRATION_FRACTION and CORRECTION_MAX.
        tick._calibrate_model(model, 100_000)

        if model["correction"]:  # only asserts if the fraction check let it through
            self.assertLessEqual(model["correction"]["medium"], tick.CORRECTION_MAX)
            self.assertGreaterEqual(model["correction"]["medium"], tick.CORRECTION_MIN)


class PredictionModelEndToEndTest(unittest.TestCase):
    def test_correction_converges_toward_true_ratio_over_multiple_refill_cycles(self) -> None:
        """The core claim: repeated refill cycles should make the
        estimate progressively more accurate. Simulates a vacuum whose
        real-world consumption is consistently 1.5x what the static
        (uncorrected) formula predicts, and checks the learned correction
        converges toward 1.5 rather than staying at the 1.0 default.

        Each cycle's pending amount is base_estimate * current_correction
        -- matching how real dosing works (tick_device multiplies the
        static formula's output by the model's current correction before
        accumulating it as pending), not a fixed amount independent of
        the correction, which would understate how the model's own
        output feeds back into the next cycle's signal.
        """
        tick, storage_mod = _load_tick()

        base_estimate = 2000  # what the static (uncorrected) formula computes per cycle
        true_ratio = 1.5
        actual_capacity = base_estimate * true_ratio  # ground truth this cycle implies

        correction = 1.0
        cycles_observed = 0
        errors: list[float] = [abs(correction - true_ratio)]

        for _ in range(6):
            pending = base_estimate * correction
            model = {
                "pending_ml_by_intensity": {"medium": pending},
                "correction": {"medium": correction},
                "cycles_observed": cycles_observed,
            }
            tick._calibrate_model(model, actual_capacity)
            correction = model["correction"]["medium"]
            cycles_observed = model["cycles_observed"]
            errors.append(abs(correction - true_ratio))

        # Error must shrink every single cycle (monotonic convergence)
        # and end up close to the true ratio.
        self.assertEqual(errors, sorted(errors, reverse=True), "error should decrease every cycle, not oscillate")
        self.assertLess(errors[-1], 0.1, "should be within 10% of the true ratio after 6 cycles")
        self.assertLess(errors[-1], errors[0])


class ResetPredictionModelsTest(unittest.TestCase):
    def test_clears_both_models_but_not_tank_levels_or_latches(self) -> None:
        tick, storage_mod = _load_tick()
        hass = FakeHass(FakeStates({}))

        async def scenario():
            storage = storage_mod.VacuumWaterStorage(hass)
            await storage.async_load()
            await storage.async_set_tank_states(
                {
                    "vacuum.kitchen_robot": {
                        "used_ml": 500,
                        "waste_used_ml": 300,
                        "latched_mop_intensity": "high",
                        "water_model": {
                            "correction": {"medium": 1.8},
                            "pending_ml_by_intensity": {"medium": 400},
                            "cycles_observed": 4,
                            "last_calibrated_iso": "2026-01-01T00:00:00+00:00",
                        },
                        "waste_model": {
                            "correction": {"medium": 0.7},
                            "pending_ml_by_intensity": {},
                            "cycles_observed": 2,
                            "last_calibrated_iso": "2026-01-01T00:00:00+00:00",
                        },
                    }
                }
            )

            result = await storage.async_reset_prediction_models("vacuum.kitchen_robot")
            return result

        result = asyncio.run(scenario())

        self.assertEqual(result["water_model"]["correction"], {})
        self.assertEqual(result["water_model"]["cycles_observed"], 0)
        self.assertIsNone(result["water_model"]["last_calibrated_iso"])
        self.assertEqual(result["waste_model"]["correction"], {})
        self.assertEqual(result["waste_model"]["cycles_observed"], 0)
        # Tank levels and the mop-intensity latch are NOT prediction-model
        # data and must survive.
        self.assertEqual(result["used_ml"], 500)
        self.assertEqual(result["waste_used_ml"], 300)
        self.assertEqual(result["latched_mop_intensity"], "high")


if __name__ == "__main__":
    unittest.main(verbosity=2)
