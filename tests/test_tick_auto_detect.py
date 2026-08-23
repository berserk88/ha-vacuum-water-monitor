"""Tests for tick.py's auto-detection: companion helper entities and tank
capacity resolved from the vacuum's real HA device registry entry, instead
of requiring manual configuration or a lucky entity_id string match.

This exercises the exact scenario the official `roborock` core integration
produces: a vacuum.* entity plus separate sensor/select entities (Cleaning
area, Status, Mop mode, Mop intensity, Dock error) all attached to the same
HA device, with manufacturer/model set on that device.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "ha_vacuum_water_monitor"


class FakeState:
    def __init__(self, state, attributes=None):
        self.state = state
        self.attributes = attributes or {}


class FakeStates:
    def __init__(self, states: dict[str, FakeState]):
        self._states = states

    def get(self, entity_id):
        return self._states.get(entity_id)

    def async_entity_ids(self, domain):
        return [eid for eid in self._states if eid.startswith(f"{domain}.")]


class FakeEntityEntry:
    def __init__(self, entity_id, device_id, name=None, original_name=None):
        self.entity_id = entity_id
        self.device_id = device_id
        self.domain = entity_id.split(".", 1)[0]
        self.name = name
        self.original_name = original_name


class FakeEntityRegistry:
    def __init__(self, entries: list[FakeEntityEntry]):
        self._by_id = {e.entity_id: e for e in entries}
        self._entries = entries

    def async_get(self, entity_id):
        return self._by_id.get(entity_id)


class FakeDeviceEntry:
    def __init__(self, manufacturer=None, model=None):
        self.manufacturer = manufacturer
        self.model = model


class FakeDeviceRegistry:
    def __init__(self, devices: dict[str, FakeDeviceEntry]):
        self._devices = devices

    def async_get(self, device_id):
        return self._devices.get(device_id)


class FakeHass:
    def __init__(self, states, entity_registry, device_registry):
        self.states = states
        self.data = {}
        self._entity_registry = entity_registry
        self._device_registry = device_registry


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

    er_mod.async_get = lambda hass: hass._entity_registry
    er_mod.async_entries_for_device = (
        lambda reg, device_id, include_disabled_entities=False: [
            e for e in reg._entries if e.device_id == device_id
        ]
    )
    dr_mod.async_get = lambda hass: hass._device_registry

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

    pkg = types.ModuleType("vwmpkg_tick")
    pkg.__path__ = [str(PKG_DIR)]
    sys.modules["vwmpkg_tick"] = pkg

    const = types.ModuleType("vwmpkg_tick.const")
    const.DOMAIN = "ha_vacuum_water_monitor"
    const.EVENT_STATE_CHANGED = "ha_vacuum_water_monitor_state_changed"
    const.STORAGE_KEY = "ha_vacuum_water_monitor"
    const.STORAGE_VERSION = 1
    const.DEFAULT_WARNING_THRESHOLD = 20
    const.DEFAULT_CRITICAL_THRESHOLD = 10
    const.signal_vacuum_water_updated = lambda entry_id: f"{const.DOMAIN}_{entry_id}_updated"
    sys.modules["vwmpkg_tick.const"] = const

    for name, filename in (
        ("sensor_calculations", "sensor_calculations.py"),
        ("storage", "storage.py"),
        ("tick", "tick.py"),
    ):
        spec = importlib.util.spec_from_file_location(f"vwmpkg_tick.{name}", PKG_DIR / filename)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"vwmpkg_tick.{name}"] = mod
        spec.loader.exec_module(mod)

    return sys.modules["vwmpkg_tick.tick"], sys.modules["vwmpkg_tick.storage"]


def _roborock_fixture():
    """A vacuum entity plus its official-integration companion entities, all
    on the same HA device — the exact shape auto-detection must handle."""
    states = FakeStates(
        {
            "vacuum.kitchen_robot": FakeState("docked"),
            "sensor.kitchen_robot_status": FakeState("docking"),
            "sensor.kitchen_robot_cleaning_area": FakeState("12.5"),
            "sensor.kitchen_robot_total_cleaning_area": FakeState("980.0"),
            "select.kitchen_robot_mop_mode": FakeState("standard"),
            "select.kitchen_robot_mop_intensity": FakeState("medium"),
            "sensor.kitchen_robot_dock_error": FakeState("ok"),
        }
    )
    entities = [
        FakeEntityEntry("vacuum.kitchen_robot", "dev1", original_name=None),
        FakeEntityEntry("sensor.kitchen_robot_status", "dev1", original_name="Status"),
        FakeEntityEntry("sensor.kitchen_robot_cleaning_area", "dev1", original_name="Cleaning area"),
        FakeEntityEntry("sensor.kitchen_robot_total_cleaning_area", "dev1", original_name="Total cleaning area"),
        FakeEntityEntry("select.kitchen_robot_mop_mode", "dev1", original_name="Mop mode"),
        FakeEntityEntry("select.kitchen_robot_mop_intensity", "dev1", original_name="Mop intensity"),
        FakeEntityEntry("sensor.kitchen_robot_dock_error", "dev1", original_name="Dock error"),
    ]
    ent_reg = FakeEntityRegistry(entities)
    dev_reg = FakeDeviceRegistry({"dev1": FakeDeviceEntry(manufacturer="Roborock", model="S8 MaxV Ultra")})
    hass = FakeHass(states, ent_reg, dev_reg)
    return hass


class AutoDetectCompanionsTest(unittest.TestCase):
    def test_finds_companions_by_name_not_total_area(self) -> None:
        tick, _storage = _load_tick()
        hass = _roborock_fixture()

        found = tick._auto_detect_companions(hass, "vacuum.kitchen_robot")

        self.assertEqual(found["status_sensor"], "sensor.kitchen_robot_status")
        self.assertEqual(found["area_sensor"], "sensor.kitchen_robot_cleaning_area")
        self.assertEqual(found["mop_mode_entity"], "select.kitchen_robot_mop_mode")
        self.assertEqual(found["mop_intensity_entity"], "select.kitchen_robot_mop_intensity")
        self.assertEqual(found["dock_error_sensor"], "sensor.kitchen_robot_dock_error")
        # Must not pick the lifetime "Total cleaning area" sensor.
        self.assertNotEqual(found["area_sensor"], "sensor.kitchen_robot_total_cleaning_area")

    def test_no_device_id_returns_empty(self) -> None:
        tick, _storage = _load_tick()
        hass = FakeHass(
            FakeStates({"vacuum.orphan": FakeState("docked")}),
            FakeEntityRegistry([FakeEntityEntry("vacuum.orphan", None)]),
            FakeDeviceRegistry({}),
        )

        self.assertEqual(tick._auto_detect_companions(hass, "vacuum.orphan"), {})

    def test_unregistered_entity_returns_empty(self) -> None:
        tick, _storage = _load_tick()
        hass = FakeHass(FakeStates({}), FakeEntityRegistry([]), FakeDeviceRegistry({}))

        self.assertEqual(tick._auto_detect_companions(hass, "vacuum.missing"), {})


class ResolveDefaultTankMlTest(unittest.TestCase):
    def test_exact_manufacturer_model_match(self) -> None:
        tick, _storage = _load_tick()
        hass = _roborock_fixture()

        self.assertEqual(
            tick._resolve_default_tank_ml(hass, "vacuum.kitchen_robot"), 3000
        )

    def test_unrecognised_model_returns_none(self) -> None:
        tick, _storage = _load_tick()
        ent_reg = FakeEntityRegistry([FakeEntityEntry("vacuum.mystery", "dev2")])
        dev_reg = FakeDeviceRegistry({"dev2": FakeDeviceEntry(manufacturer="Acme", model="Widget 3000")})
        hass = FakeHass(FakeStates({}), ent_reg, dev_reg)

        self.assertIsNone(tick._resolve_default_tank_ml(hass, "vacuum.mystery"))

    def test_bare_brand_alone_does_not_match(self) -> None:
        """A manufacturer name with no recognisable model must not fuzzy-match
        onto an unrelated known key just because "roborock" is a substring."""
        tick, _storage = _load_tick()
        ent_reg = FakeEntityRegistry([FakeEntityEntry("vacuum.mystery2", "dev3")])
        dev_reg = FakeDeviceRegistry({"dev3": FakeDeviceEntry(manufacturer="Roborock", model="")})
        hass = FakeHass(FakeStates({}), ent_reg, dev_reg)

        self.assertIsNone(tick._resolve_default_tank_ml(hass, "vacuum.mystery2"))


class GuessBrandModelTest(unittest.TestCase):
    def test_matches_known_model(self) -> None:
        tick, _storage = _load_tick()
        hass = _roborock_fixture()

        self.assertEqual(
            tick.guess_brand_model(hass, "vacuum.kitchen_robot"),
            ("Roborock", "S8 MaxV Ultra"),
        )

    def test_unrecognised_model_returns_none(self) -> None:
        tick, _storage = _load_tick()
        ent_reg = FakeEntityRegistry([FakeEntityEntry("vacuum.mystery", "dev2")])
        dev_reg = FakeDeviceRegistry({"dev2": FakeDeviceEntry(manufacturer="Acme", model="Widget 3000")})
        hass = FakeHass(FakeStates({}), ent_reg, dev_reg)

        self.assertIsNone(tick.guess_brand_model(hass, "vacuum.mystery"))


class EnsureAutoConfigTest(unittest.TestCase):
    def test_persists_capacity_and_companions_for_manually_added_device(self) -> None:
        tick, storage_mod = _load_tick()
        hass = _roborock_fixture()

        async def scenario():
            storage = storage_mod.VacuumWaterStorage(hass)
            await storage.async_load()
            # Simulates the config flow's "add vacuum" step: the user picked
            # the entity but didn't (or couldn't) pick a brand/model.
            await storage.async_set_settings(
                {"configured_devices": [{"vacuum_entity": "vacuum.kitchen_robot"}]}
            )
            changed = await tick.async_ensure_auto_config(hass, storage)
            state = await storage.async_get_state()
            return changed, state["settings"]

        changed, settings = asyncio.run(scenario())

        self.assertTrue(changed)
        devices = {d["vacuum_entity"]: d for d in settings["configured_devices"]}
        entry = devices["vacuum.kitchen_robot"]
        self.assertEqual(entry["water_total_ml"], 3000)
        self.assertEqual(entry["status_sensor"], "sensor.kitchen_robot_status")
        self.assertEqual(entry["area_sensor"], "sensor.kitchen_robot_cleaning_area")

    def test_never_creates_entry_for_unadded_vacuum(self) -> None:
        """A vacuum that exists in HA but was never added via the config
        flow must not get an entity created for it -- tracking is opt-in."""
        tick, storage_mod = _load_tick()
        hass = _roborock_fixture()

        async def scenario():
            storage = storage_mod.VacuumWaterStorage(hass)
            await storage.async_load()
            changed = await tick.async_ensure_auto_config(hass, storage)
            state = await storage.async_get_state()
            return changed, state["settings"]

        changed, settings = asyncio.run(scenario())
        self.assertFalse(changed)
        self.assertEqual(settings.get("configured_devices") or [], [])

    def test_does_not_overwrite_manual_capacity(self) -> None:
        tick, storage_mod = _load_tick()
        hass = _roborock_fixture()

        async def scenario():
            storage = storage_mod.VacuumWaterStorage(hass)
            await storage.async_load()
            # User already manually set a custom capacity for this vacuum.
            await storage.async_set_settings(
                {
                    "configured_devices": [
                        {"vacuum_entity": "vacuum.kitchen_robot", "water_total_ml": 9999}
                    ]
                }
            )
            await tick.async_ensure_auto_config(hass, storage)
            state = await storage.async_get_state()
            return state["settings"]

        settings = asyncio.run(scenario())
        devices = {d["vacuum_entity"]: d for d in settings["configured_devices"]}
        # Manual value must survive; auto-detection only fills missing fields.
        self.assertEqual(devices["vacuum.kitchen_robot"]["water_total_ml"], 9999)
        # But companion entities (which were never manually set) still get filled in.
        self.assertEqual(
            devices["vacuum.kitchen_robot"]["status_sensor"], "sensor.kitchen_robot_status"
        )

    def test_second_run_is_a_noop(self) -> None:
        tick, storage_mod = _load_tick()
        hass = _roborock_fixture()

        async def scenario():
            storage = storage_mod.VacuumWaterStorage(hass)
            await storage.async_load()
            await storage.async_set_settings(
                {"configured_devices": [{"vacuum_entity": "vacuum.kitchen_robot"}]}
            )
            first = await tick.async_ensure_auto_config(hass, storage)
            second = await tick.async_ensure_auto_config(hass, storage)
            return first, second

        first, second = asyncio.run(scenario())
        self.assertTrue(first)
        self.assertFalse(second, "a fully-resolved device must not be rewritten every tick")


class EndToEndAccountingTest(unittest.TestCase):
    def test_mop_wash_and_area_dosing_fire_via_auto_detected_sensors(self) -> None:
        """The actual scenario this whole feature exists for: a vacuum the
        user added via the config flow's entity picker (brand/model left
        unset) must still get capacity resolved and accumulate used_ml from
        real cleaning activity, with zero further manual configuration."""
        tick, storage_mod = _load_tick()
        hass = _roborock_fixture()

        async def scenario():
            storage = storage_mod.VacuumWaterStorage(hass)
            await storage.async_load()
            # Simulates the config flow's "add vacuum" step.
            await storage.async_set_settings(
                {"configured_devices": [{"vacuum_entity": "vacuum.kitchen_robot"}]}
            )

            # Tick 1: resolves capacity/companions, establishes baseline
            # status/area while actively cleaning — no dosing yet (nothing
            # to compare against on the first observation).
            hass.states._states["vacuum.kitchen_robot"] = FakeState("cleaning")
            await tick.async_ensure_auto_config(hass, storage)
            await tick.async_tick_water_state(hass, storage)

            # Still cleaning, more area covered: area-based dosing fires.
            hass.states._states["sensor.kitchen_robot_cleaning_area"] = FakeState("18.0")
            await tick.async_tick_water_state(hass, storage)

            # Vacuum finishes and starts washing the mop: fixed-volume dosing fires.
            hass.states._states["vacuum.kitchen_robot"] = FakeState("docked")
            hass.states._states["sensor.kitchen_robot_status"] = FakeState("washing_the_mop")
            changed2 = await tick.async_tick_water_state(hass, storage)

            state = await storage.async_get_state()
            return changed2, state["tank_states"]["vacuum.kitchen_robot"]

        changed2, tank_state = asyncio.run(scenario())

        self.assertIn("vacuum.kitchen_robot", changed2)
        # 150ml fixed wash volume + (18.0 - 12.5) m² * 6 ml/m² (standard/medium defaults) = 150 + 33 = 183
        self.assertEqual(tank_state["used_ml"], 183)


if __name__ == "__main__":
    unittest.main(verbosity=2)
