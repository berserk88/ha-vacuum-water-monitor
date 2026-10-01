"""Tests for binary_sensor.py::_dock_error_diagnostics -- the attributes
that let someone verify their dock-error configuration (entity, attribute,
messages, and the last value actually observed) directly from Developer
Tools > States, instead of guessing why an expected refill/empty
auto-reset didn't happen.
"""
from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "vacuum_water_level"


def _load_binary_sensor():
    ha = types.ModuleType("homeassistant")
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    core.callback = lambda fn: fn
    config_entries_mod = types.ModuleType("homeassistant.config_entries")
    config_entries_mod.ConfigEntry = object
    components = types.ModuleType("homeassistant.components")
    binary_sensor_mod = types.ModuleType("homeassistant.components.binary_sensor")

    class _DeviceClass:
        PROBLEM = "problem"

    class _BinarySensorEntity:
        pass

    binary_sensor_mod.BinarySensorDeviceClass = _DeviceClass
    binary_sensor_mod.BinarySensorEntity = _BinarySensorEntity

    helpers = types.ModuleType("homeassistant.helpers")
    dispatcher_mod = types.ModuleType("homeassistant.helpers.dispatcher")
    dispatcher_mod.async_dispatcher_connect = lambda *a, **k: (lambda: None)
    entity_mod = types.ModuleType("homeassistant.helpers.entity")
    entity_mod.DeviceInfo = dict
    storage_mod = types.ModuleType("homeassistant.helpers.storage")

    class _Store:
        def __init__(self, *a, **k):
            self._d = None

        async def async_load(self):
            return self._d

        async def async_save(self, data):
            self._d = data

    storage_mod.Store = _Store
    er_mod = types.ModuleType("homeassistant.helpers.entity_registry")
    er_mod.async_get = lambda hass: None
    er_mod.async_entries_for_device = lambda *a, **k: []
    dr_mod = types.ModuleType("homeassistant.helpers.device_registry")
    dr_mod.async_get = lambda hass: None

    ha.core = core
    ha.config_entries = config_entries_mod
    ha.components = components
    ha.helpers = helpers
    components.binary_sensor = binary_sensor_mod
    helpers.dispatcher = dispatcher_mod
    helpers.entity = entity_mod
    helpers.storage = storage_mod
    helpers.entity_registry = er_mod
    helpers.device_registry = dr_mod

    for name, mod in (
        ("homeassistant", ha),
        ("homeassistant.core", core),
        ("homeassistant.config_entries", config_entries_mod),
        ("homeassistant.components", components),
        ("homeassistant.components.binary_sensor", binary_sensor_mod),
        ("homeassistant.helpers", helpers),
        ("homeassistant.helpers.dispatcher", dispatcher_mod),
        ("homeassistant.helpers.entity", entity_mod),
        ("homeassistant.helpers.storage", storage_mod),
        ("homeassistant.helpers.entity_registry", er_mod),
        ("homeassistant.helpers.device_registry", dr_mod),
    ):
        sys.modules[name] = mod

    pkg = types.ModuleType("vwmpkg_bs")
    pkg.__path__ = [str(PKG_DIR)]
    sys.modules["vwmpkg_bs"] = pkg

    const = types.ModuleType("vwmpkg_bs.const")
    const.DOMAIN = "vacuum_water_level"
    const.DATA_STORAGE = "storage"
    const.MANUFACTURER = "HA Tools"
    const.MODEL = "Vacuum water level"
    const.STORAGE_KEY = "vacuum_water_level"
    const.STORAGE_VERSION = 1
    const.CONF_WARNING_THRESHOLD = "warning_threshold"
    const.CONF_CRITICAL_THRESHOLD = "critical_threshold"
    const.DEFAULT_WARNING_THRESHOLD = 20
    const.DEFAULT_CRITICAL_THRESHOLD = 10
    const.signal_vacuum_water_updated = lambda entry_id: f"vacuum_water_level_{entry_id}_updated"
    sys.modules["vwmpkg_bs.const"] = const

    for name, filename in (
        ("sensor_calculations", "sensor_calculations.py"),
        ("storage", "storage.py"),
        ("tick", "tick.py"),
        ("binary_sensor", "binary_sensor.py"),
    ):
        spec = importlib.util.spec_from_file_location(f"vwmpkg_bs.{name}", PKG_DIR / filename)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"vwmpkg_bs.{name}"] = mod
        spec.loader.exec_module(mod)

    return sys.modules["vwmpkg_bs.binary_sensor"]


class DockErrorDiagnosticsTest(unittest.TestCase):
    def test_reports_configured_entity_and_last_observed_value(self) -> None:
        binary_sensor = _load_binary_sensor()
        device = {
            "dock_error_sensor": "sensor.dock_station",
            "dock_error_attribute": "error",
        }
        tank_state = {"last_dock_err": "Water empty"}

        diagnostics = binary_sensor._dock_error_diagnostics(device, tank_state)

        self.assertEqual(diagnostics["dock_error_sensor"], "sensor.dock_station")
        self.assertEqual(diagnostics["dock_error_attribute"], "error")
        self.assertEqual(diagnostics["dock_error_last_observed_value"], "Water empty")

    def test_unconfigured_device_reports_none_not_an_error(self) -> None:
        """The most common real diagnosis: dock_error_sensor was never
        set (auto-detection found nothing), so auto-reset can never fire
        -- this must show up as an obvious None, not silently look the
        same as a working configuration."""
        binary_sensor = _load_binary_sensor()

        diagnostics = binary_sensor._dock_error_diagnostics({}, {})

        self.assertIsNone(diagnostics["dock_error_sensor"])
        self.assertIsNone(diagnostics["dock_error_attribute"])
        self.assertIsNone(diagnostics["dock_error_last_observed_value"])

    def test_messages_fall_back_to_defaults_when_not_customized(self) -> None:
        binary_sensor = _load_binary_sensor()

        diagnostics = binary_sensor._dock_error_diagnostics({}, {})

        self.assertEqual(diagnostics["dock_empty_message"], binary_sensor.DEFAULT_DOCK_EMPTY_MESSAGE)
        self.assertEqual(diagnostics["dock_ok_message"], binary_sensor.DEFAULT_DOCK_OK_MESSAGE)
        self.assertEqual(diagnostics["dock_full_message"], binary_sensor.DEFAULT_DOCK_FULL_MESSAGE)

    def test_custom_messages_are_reflected_not_the_defaults(self) -> None:
        binary_sensor = _load_binary_sensor()
        device = {
            "dock_empty_message": "TankLow",
            "dock_ok_message": "AllGood",
            "dock_full_message": "DirtyTankFull",
        }

        diagnostics = binary_sensor._dock_error_diagnostics(device, {})

        self.assertEqual(diagnostics["dock_empty_message"], "TankLow")
        self.assertEqual(diagnostics["dock_ok_message"], "AllGood")
        self.assertEqual(diagnostics["dock_full_message"], "DirtyTankFull")


if __name__ == "__main__":
    unittest.main(verbosity=2)
