"""Tests for __init__.py::_async_migrate_legacy_storage.

The integration was renamed from domain `ha_vacuum_water_monitor` to
`vacuum_water_level`. Since each domain gets its own separate Home
Assistant storage file, tracked vacuums and tank history would silently
vanish for anyone updating from the old name unless that data is copied
forward once, on first setup. This test verifies the copy happens exactly
once and never overwrites data that already exists under the new domain.

Only _async_migrate_legacy_storage is under test here, not the rest of
__init__.py's async_setup_entry (platform forwarding, tick scheduling,
etc.) — that needs much heavier config_entries/Platform stubbing that
wouldn't add confidence for what this specific function does.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "vacuum_water_level"


def _load_migration_fn():
    """Load just enough of homeassistant to import __init__.py and hand
    back _async_migrate_legacy_storage plus the fake on-disk store."""
    backing: dict[tuple[int, str], object] = {}

    ha = types.ModuleType("homeassistant")
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    config_entries_mod = types.ModuleType("homeassistant.config_entries")
    config_entries_mod.ConfigEntry = object
    const_mod = types.ModuleType("homeassistant.const")

    class _Platform:
        SENSOR = "sensor"
        BINARY_SENSOR = "binary_sensor"
        BUTTON = "button"

    const_mod.Platform = _Platform

    helpers = types.ModuleType("homeassistant.helpers")
    dispatcher_mod = types.ModuleType("homeassistant.helpers.dispatcher")
    dispatcher_mod.async_dispatcher_send = lambda *a, **k: None
    event_mod = types.ModuleType("homeassistant.helpers.event")
    event_mod.async_track_time_interval = lambda *a, **k: (lambda: None)
    storage_mod = types.ModuleType("homeassistant.helpers.storage")
    er_mod = types.ModuleType("homeassistant.helpers.entity_registry")
    er_mod.async_get = lambda hass: None
    er_mod.async_entries_for_device = lambda *a, **k: []
    dr_mod = types.ModuleType("homeassistant.helpers.device_registry")
    dr_mod.async_get = lambda hass: None

    class _FakeStore:
        def __init__(self, hass, version, key):
            self._k = (version, key)

        async def async_load(self):
            return backing.get(self._k)

        async def async_save(self, data):
            backing[self._k] = data

    storage_mod.Store = _FakeStore

    ha.core = core
    ha.config_entries = config_entries_mod
    ha.const = const_mod
    ha.helpers = helpers
    helpers.dispatcher = dispatcher_mod
    helpers.event = event_mod
    helpers.storage = storage_mod
    helpers.entity_registry = er_mod
    helpers.device_registry = dr_mod

    for name, mod in (
        ("homeassistant", ha),
        ("homeassistant.core", core),
        ("homeassistant.config_entries", config_entries_mod),
        ("homeassistant.const", const_mod),
        ("homeassistant.helpers", helpers),
        ("homeassistant.helpers.dispatcher", dispatcher_mod),
        ("homeassistant.helpers.event", event_mod),
        ("homeassistant.helpers.storage", storage_mod),
        ("homeassistant.helpers.entity_registry", er_mod),
        ("homeassistant.helpers.device_registry", dr_mod),
    ):
        sys.modules[name] = mod

    pkg = types.ModuleType("vwmpkg_init")
    pkg.__path__ = [str(PKG_DIR)]
    sys.modules["vwmpkg_init"] = pkg

    const = types.ModuleType("vwmpkg_init.const")
    const.DOMAIN = "vacuum_water_level"
    const.LEGACY_DOMAIN = "ha_vacuum_water_monitor"
    const.STORAGE_VERSION = 1
    const.STORAGE_KEY = "vacuum_water_level"
    const.CONF_WARNING_THRESHOLD = "warning_threshold"
    const.CONF_CRITICAL_THRESHOLD = "critical_threshold"
    const.DEFAULT_WARNING_THRESHOLD = 20
    const.DEFAULT_CRITICAL_THRESHOLD = 10
    const.DATA_STORAGE = "storage"
    const.DATA_TICK_UNSUB = "tick_unsub"
    const.DEFAULT_TICK_INTERVAL_SECONDS = 60
    const.EVENT_STATE_CHANGED = "vacuum_water_level_state_changed"
    const.signal_vacuum_water_updated = lambda entry_id: f"vacuum_water_level_{entry_id}_updated"
    sys.modules["vwmpkg_init.const"] = const

    for name, filename in (
        ("sensor_calculations", "sensor_calculations.py"),
        ("storage", "storage.py"),
        ("tick", "tick.py"),
    ):
        spec = importlib.util.spec_from_file_location(f"vwmpkg_init.{name}", PKG_DIR / filename)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"vwmpkg_init.{name}"] = mod
        spec.loader.exec_module(mod)

    spec = importlib.util.spec_from_file_location("vwmpkg_init", PKG_DIR / "__init__.py")
    init_mod = importlib.util.module_from_spec(spec)
    sys.modules["vwmpkg_init"] = init_mod
    spec.loader.exec_module(init_mod)

    return init_mod._async_migrate_legacy_storage, backing


class MigrateLegacyStorageTest(unittest.TestCase):
    def test_copies_legacy_data_when_new_domain_is_empty(self) -> None:
        migrate, backing = _load_migration_fn()
        backing[(1, "ha_vacuum_water_monitor")] = {
            "settings": {"configured_devices": [{"vacuum_entity": "vacuum.kitchen"}]},
            "tank_states": {"vacuum.kitchen": {"used_ml": 500}},
        }

        asyncio.run(migrate(hass=None))

        self.assertEqual(
            backing[(1, "vacuum_water_level")]["tank_states"]["vacuum.kitchen"]["used_ml"],
            500,
        )

    def test_never_overwrites_existing_new_domain_data(self) -> None:
        migrate, backing = _load_migration_fn()
        backing[(1, "ha_vacuum_water_monitor")] = {
            "settings": {"configured_devices": [{"vacuum_entity": "vacuum.old"}]},
            "tank_states": {},
        }
        backing[(1, "vacuum_water_level")] = {
            "settings": {"configured_devices": [{"vacuum_entity": "vacuum.new"}]},
            "tank_states": {},
        }

        asyncio.run(migrate(hass=None))

        devices = backing[(1, "vacuum_water_level")]["settings"]["configured_devices"]
        self.assertEqual([d["vacuum_entity"] for d in devices], ["vacuum.new"])

    def test_no_legacy_data_is_a_noop(self) -> None:
        migrate, backing = _load_migration_fn()

        asyncio.run(migrate(hass=None))

        self.assertNotIn((1, "vacuum_water_level"), backing)


if __name__ == "__main__":
    unittest.main(verbosity=2)
