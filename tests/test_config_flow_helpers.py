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

PKG_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "ha_vacuum_water_monitor"


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
    const.DOMAIN = "ha_vacuum_water_monitor"
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


class BuildNewDeviceEntryTest(unittest.TestCase):
    def test_database_model_includes_brand_and_model(self) -> None:
        entry = config_flow._build_new_device_entry(
            "vacuum.kitchen",
            water_total_ml=3000,
            brand_profile="roborock_s8_maxv_ultra",
            manufacturer="Roborock",
            model="S8 MaxV Ultra",
        )
        self.assertEqual(
            entry,
            {
                "vacuum_entity": "vacuum.kitchen",
                "water_total_ml": 3000,
                "brand_profile": "roborock_s8_maxv_ultra",
                "manufacturer": "Roborock",
                "model": "S8 MaxV Ultra",
            },
        )

    def test_custom_capacity_stays_minimal(self) -> None:
        """A user-typed capacity for an unlisted model must not fabricate a
        brand/model the user never confirmed."""
        entry = config_flow._build_new_device_entry(
            "vacuum.kitchen", water_total_ml=2750
        )
        self.assertEqual(
            entry, {"vacuum_entity": "vacuum.kitchen", "water_total_ml": 2750}
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
