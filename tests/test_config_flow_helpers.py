"""Tests for the pure helper functions in config_flow.py.

The ConfigFlow/OptionsFlow step methods themselves depend on Home
Assistant's config_entries framework (form rendering, flow context, step
routing) which isn't practical to stub faithfully here without risking the
test validating a fake harness instead of real behavior. So the actual
selection/persistence logic is extracted into plain functions
(_available_vacuum_entities, _tracked_vacuum_entities,
_build_new_device_entry) that the flow steps call — this tests that logic
directly, with no framework involved.
"""
from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "vacuum_water_level"


def _load_config_flow_helpers():
    """Load config_flow.py with the minimal stubs it needs at import time,
    then hand back just the pure functions under test."""
    ha = types.ModuleType("homeassistant")
    config_entries_mod = types.ModuleType("homeassistant.config_entries")
    helpers = types.ModuleType("homeassistant.helpers")
    selector_mod = types.ModuleType("homeassistant.helpers.selector")

    class _FakeFlowResult(dict):
        pass

    class _FakeConfigFlow:
        def __init_subclass__(cls, *, domain=None, **kwargs):
            super().__init_subclass__(**kwargs)
            cls._domain = domain

    class _FakeOptionsFlow:
        pass

    config_entries_mod.ConfigFlow = _FakeConfigFlow
    config_entries_mod.OptionsFlow = _FakeOptionsFlow
    config_entries_mod.ConfigEntry = object
    config_entries_mod.ConfigFlowResult = _FakeFlowResult

    for name in (
        "EntitySelector",
        "EntitySelectorConfig",
        "SelectSelector",
        "SelectSelectorConfig",
        "SelectSelectorMode",
        "SelectOptionDict",
        "NumberSelector",
        "NumberSelectorConfig",
        "NumberSelectorMode",
    ):
        setattr(selector_mod, name, object)

    ha.config_entries = config_entries_mod
    ha.helpers = helpers
    helpers.selector = selector_mod
    for name, mod in (
        ("homeassistant", ha),
        ("homeassistant.config_entries", config_entries_mod),
        ("homeassistant.helpers", helpers),
        ("homeassistant.helpers.selector", selector_mod),
    ):
        sys.modules[name] = mod

    vol = types.ModuleType("voluptuous")
    vol.Schema = lambda *a, **k: None
    vol.Required = lambda key, default=None: key
    vol.All = lambda *a, **k: None
    vol.Coerce = lambda t: t
    vol.Range = lambda **k: None
    sys.modules["voluptuous"] = vol

    pkg = types.ModuleType("vwmpkg_cf")
    pkg.__path__ = [str(PKG_DIR)]
    sys.modules["vwmpkg_cf"] = pkg

    const = types.ModuleType("vwmpkg_cf.const")
    const.CONF_WARNING_THRESHOLD = "warning_threshold"
    const.CONF_CRITICAL_THRESHOLD = "critical_threshold"
    const.DATA_STORAGE = "storage"
    const.DEFAULT_WARNING_THRESHOLD = 20
    const.DEFAULT_CRITICAL_THRESHOLD = 10
    const.DOMAIN = "vacuum_water_level"
    sys.modules["vwmpkg_cf.const"] = const

    # sensor_calculations.py and tick.py have no further homeassistant-only
    # dependencies beyond what tick.py needs (already stubbed in the tick
    # test module's own pattern) -- but config_flow.py only calls
    # guess_brand_model/list_vacuums from tick, and MODEL_DATABASE/slugify
    # from sensor_calculations, so stub tick.py minimally rather than
    # loading the real one (which needs device_registry/entity_registry).
    sensor_calc_spec = importlib.util.spec_from_file_location(
        "vwmpkg_cf.sensor_calculations", PKG_DIR / "sensor_calculations.py"
    )
    sensor_calc_mod = importlib.util.module_from_spec(sensor_calc_spec)
    sys.modules["vwmpkg_cf.sensor_calculations"] = sensor_calc_mod
    sensor_calc_spec.loader.exec_module(sensor_calc_mod)

    tick_stub = types.ModuleType("vwmpkg_cf.tick")
    tick_stub.guess_brand_model = lambda hass, entity_id: None
    tick_stub.list_vacuums = lambda hass: []
    tick_stub.DEFAULT_DOCK_EMPTY_MESSAGE = "Water empty"
    tick_stub.DEFAULT_DOCK_OK_MESSAGE = "Ok"
    tick_stub.DEFAULT_DOCK_FULL_MESSAGE = "Waste water tank full"
    sys.modules["vwmpkg_cf.tick"] = tick_stub

    storage_stub = types.ModuleType("vwmpkg_cf.storage")
    storage_stub.VacuumWaterStorage = object
    sys.modules["vwmpkg_cf.storage"] = storage_stub

    spec = importlib.util.spec_from_file_location(
        "vwmpkg_cf.config_flow", PKG_DIR / "config_flow.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["vwmpkg_cf.config_flow"] = mod
    spec.loader.exec_module(mod)
    return mod


config_flow = _load_config_flow_helpers()


class TrackedVacuumEntitiesTest(unittest.TestCase):
    def test_merges_configured_and_user_devices(self) -> None:
        settings = {
            "configured_devices": [{"vacuum_entity": "vacuum.a"}],
            "user_devices": [{"vacuum_entity": "vacuum.b"}],
        }
        self.assertEqual(
            config_flow._tracked_vacuum_entities(settings), {"vacuum.a", "vacuum.b"}
        )

    def test_empty_settings_returns_empty_set(self) -> None:
        self.assertEqual(config_flow._tracked_vacuum_entities({}), set())

    def test_ignores_malformed_entries(self) -> None:
        settings = {"configured_devices": ["not-a-dict", {"no_entity": True}, None]}
        self.assertEqual(config_flow._tracked_vacuum_entities(settings), set())


class AvailableVacuumEntitiesTest(unittest.TestCase):
    def test_excludes_already_tracked(self) -> None:
        settings = {"configured_devices": [{"vacuum_entity": "vacuum.a"}]}
        available = config_flow._available_vacuum_entities(
            ["vacuum.a", "vacuum.b", "vacuum.c"], settings
        )
        self.assertEqual(available, ["vacuum.b", "vacuum.c"])

    def test_all_available_when_nothing_tracked(self) -> None:
        available = config_flow._available_vacuum_entities(
            ["vacuum.b", "vacuum.a"], {}
        )
        self.assertEqual(available, ["vacuum.a", "vacuum.b"])  # sorted

    def test_empty_when_everything_tracked(self) -> None:
        settings = {"configured_devices": [{"vacuum_entity": "vacuum.a"}]}
        self.assertEqual(
            config_flow._available_vacuum_entities(["vacuum.a"], settings), []
        )


class UpsertDeviceEntryTest(unittest.TestCase):
    def test_appends_when_not_present(self) -> None:
        """The "Add a vacuum" case: no existing entry for this vacuum."""
        result = config_flow._upsert_device_entry(
            [{"vacuum_entity": "vacuum.other", "water_total_ml": 200}],
            "vacuum.kitchen",
            {"water_total_ml": 3000, "brand_profile": "roborock_s8_maxv_ultra"},
        )
        self.assertEqual(len(result), 2)
        new_entry = next(d for d in result if d["vacuum_entity"] == "vacuum.kitchen")
        self.assertEqual(new_entry["water_total_ml"], 3000)
        self.assertEqual(new_entry["brand_profile"], "roborock_s8_maxv_ultra")
        # The pre-existing device must be untouched.
        other = next(d for d in result if d["vacuum_entity"] == "vacuum.other")
        self.assertEqual(other["water_total_ml"], 200)

    def test_updates_in_place_when_present(self) -> None:
        """The "Edit a vacuum" case: overwrite fields without duplicating
        the device or disturbing unrelated fields (e.g. an auto-detected
        status_sensor set by a previous tick)."""
        result = config_flow._upsert_device_entry(
            [
                {
                    "vacuum_entity": "vacuum.kitchen",
                    "water_total_ml": 200,
                    "brand_profile": "roborock_s7_maxv",
                    "status_sensor": "sensor.kitchen_robot_status",
                }
            ],
            "vacuum.kitchen",
            {
                "water_total_ml": 3000,
                "brand_profile": "roborock_s8_maxv_ultra",
                "manufacturer": "Roborock",
                "model": "S8 MaxV Ultra",
            },
        )
        self.assertEqual(len(result), 1)
        entry = result[0]
        self.assertEqual(entry["water_total_ml"], 3000)
        self.assertEqual(entry["brand_profile"], "roborock_s8_maxv_ultra")
        # Auto-detected companion entity must survive an unrelated edit.
        self.assertEqual(entry["status_sensor"], "sensor.kitchen_robot_status")

    def test_clear_keys_removes_stale_fields(self) -> None:
        """Switching an edited vacuum from a database model to a custom
        capacity must not leave the old brand/model behind."""
        result = config_flow._upsert_device_entry(
            [
                {
                    "vacuum_entity": "vacuum.kitchen",
                    "water_total_ml": 3000,
                    "brand_profile": "roborock_s8_maxv_ultra",
                    "manufacturer": "Roborock",
                    "model": "S8 MaxV Ultra",
                }
            ],
            "vacuum.kitchen",
            {"water_total_ml": 2750},
            clear_keys=("brand_profile", "manufacturer", "model"),
        )
        entry = result[0]
        self.assertEqual(entry["water_total_ml"], 2750)
        self.assertNotIn("brand_profile", entry)
        self.assertNotIn("manufacturer", entry)
        self.assertNotIn("model", entry)

    def test_does_not_mutate_input_list(self) -> None:
        original = [{"vacuum_entity": "vacuum.kitchen", "water_total_ml": 200}]
        config_flow._upsert_device_entry(
            original, "vacuum.kitchen", {"water_total_ml": 3000}
        )
        self.assertEqual(original[0]["water_total_ml"], 200)


class FindDeviceEntryTest(unittest.TestCase):
    def test_finds_in_configured_devices(self) -> None:
        settings = {"configured_devices": [{"vacuum_entity": "vacuum.a", "water_total_ml": 3000}]}
        entry = config_flow._find_device_entry(settings, "vacuum.a")
        self.assertEqual(entry["water_total_ml"], 3000)

    def test_finds_in_user_devices(self) -> None:
        settings = {"user_devices": [{"vacuum_entity": "vacuum.a", "water_total_ml": 3500}]}
        entry = config_flow._find_device_entry(settings, "vacuum.a")
        self.assertEqual(entry["water_total_ml"], 3500)

    def test_returns_none_when_not_tracked(self) -> None:
        self.assertIsNone(config_flow._find_device_entry({}, "vacuum.missing"))


class MopEntityUpdatesTest(unittest.TestCase):
    def test_both_entities_set(self) -> None:
        updates, clear_keys = config_flow._mop_entity_updates(
            "select.vacuum_mop_mode", "select.vacuum_mop_intensity"
        )
        self.assertEqual(
            updates,
            {
                "mop_mode_entity": "select.vacuum_mop_mode",
                "mop_intensity_entity": "select.vacuum_mop_intensity",
            },
        )
        self.assertEqual(clear_keys, ())

    def test_clearing_one_entity_does_not_affect_the_other(self) -> None:
        updates, clear_keys = config_flow._mop_entity_updates(
            "select.vacuum_mop_mode", ""
        )
        self.assertEqual(updates, {"mop_mode_entity": "select.vacuum_mop_mode"})
        self.assertEqual(clear_keys, ("mop_intensity_entity",))

    def test_both_blank_clears_both(self) -> None:
        updates, clear_keys = config_flow._mop_entity_updates(None, "  ")
        self.assertEqual(updates, {})
        self.assertEqual(clear_keys, ("mop_mode_entity", "mop_intensity_entity"))

    def test_whitespace_only_treated_as_blank(self) -> None:
        updates, clear_keys = config_flow._mop_entity_updates("   ", "select.intensity")
        self.assertEqual(updates, {"mop_intensity_entity": "select.intensity"})
        self.assertEqual(clear_keys, ("mop_mode_entity",))


class DockErrorUpdatesTest(unittest.TestCase):
    def test_entity_and_attribute_both_set(self) -> None:
        updates, clear_keys = config_flow._dock_error_updates("sensor.dock", "error")
        self.assertEqual(
            updates, {"dock_error_sensor": "sensor.dock", "dock_error_attribute": "error"}
        )
        # Nothing else was given, so every other field clears to its default.
        self.assertEqual(
            clear_keys,
            ("dock_empty_message", "dock_ok_message", "dock_full_message", "waste_total_ml"),
        )

    def test_entity_only_clears_stale_attribute(self) -> None:
        """Setting just the entity (no attribute) must clear any
        previously-configured attribute name, so a plain-.state dock error
        source doesn't keep reading a stale attribute."""
        updates, clear_keys = config_flow._dock_error_updates("sensor.dock", "")
        self.assertEqual(updates, {"dock_error_sensor": "sensor.dock"})
        self.assertIn("dock_error_attribute", clear_keys)

    def test_blank_entity_clears_everything(self) -> None:
        """Clearing the entity clears the attribute and all three
        messages/capacity too -- none of them mean anything without a
        source entity."""
        updates, clear_keys = config_flow._dock_error_updates("", "error")
        self.assertEqual(updates, {})
        self.assertEqual(
            clear_keys,
            (
                "dock_error_sensor",
                "dock_error_attribute",
                "dock_empty_message",
                "dock_ok_message",
                "dock_full_message",
                "waste_total_ml",
            ),
        )

    def test_whitespace_only_treated_as_blank(self) -> None:
        updates, clear_keys = config_flow._dock_error_updates("  ", "  ")
        self.assertEqual(updates, {})
        self.assertIn("dock_error_sensor", clear_keys)
        self.assertIn("dock_error_attribute", clear_keys)

    def test_custom_messages_and_waste_capacity_all_set(self) -> None:
        """The full customization scenario: entity + attribute + all three
        trigger messages + waste tank capacity, all in one edit."""
        updates, clear_keys = config_flow._dock_error_updates(
            "sensor.dock_station",
            "error",
            "Water empty",
            "Ok",
            "Waste water tank full",
            2500,
        )
        self.assertEqual(
            updates,
            {
                "dock_error_sensor": "sensor.dock_station",
                "dock_error_attribute": "error",
                "dock_empty_message": "Water empty",
                "dock_ok_message": "Ok",
                "dock_full_message": "Waste water tank full",
                "waste_total_ml": 2500,
            },
        )
        self.assertEqual(clear_keys, ())

    def test_blank_message_clears_that_message_only(self) -> None:
        """Clearing just one message (leaving entity/others set) falls
        back to that message's default without disturbing the rest."""
        updates, clear_keys = config_flow._dock_error_updates(
            "sensor.dock", None, "", "Ok", "Waste water tank full", None
        )
        self.assertEqual(
            updates,
            {
                "dock_error_sensor": "sensor.dock",
                "dock_ok_message": "Ok",
                "dock_full_message": "Waste water tank full",
            },
        )
        self.assertIn("dock_empty_message", clear_keys)
        self.assertIn("waste_total_ml", clear_keys)
        self.assertNotIn("dock_ok_message", clear_keys)

    def test_zero_or_negative_waste_capacity_clears_override(self) -> None:
        updates, clear_keys = config_flow._dock_error_updates(
            "sensor.dock", None, None, None, None, 0
        )
        self.assertNotIn("waste_total_ml", updates)
        self.assertIn("waste_total_ml", clear_keys)


if __name__ == "__main__":
    unittest.main(verbosity=2)
