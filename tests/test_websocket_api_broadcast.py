"""Regression test: a settings patch that changes nothing must not broadcast
VWM_EVENT to every connected card.

Before this fix, several frontend code paths re-sent the same
`configured_devices` patch on every hass tick (see the JS-side fix in
_ensureServerState), and _ws_set_settings broadcast unconditionally on every
call. That meant every no-op re-send still forced every open card to
re-render, which was part of the "input fields lose focus instantly" bug.
This test loads websocket_api.py with minimal homeassistant/voluptuous
stubs, matching the repo's existing test style (see test_user_device_removal.py).
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "ha_vacuum_water_monitor"


def _stub_homeassistant() -> list[tuple[str, dict]]:
    """Install minimal homeassistant/voluptuous stubs and return the list
    that async_dispatcher_send calls will be recorded into."""
    dispatcher_calls: list[tuple[str, dict]] = []

    ha = types.ModuleType("homeassistant")
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    helpers = types.ModuleType("homeassistant.helpers")
    storage_mod = types.ModuleType("homeassistant.helpers.storage")
    dispatcher_mod = types.ModuleType("homeassistant.helpers.dispatcher")

    def _async_dispatcher_send(hass, signal, payload):
        dispatcher_calls.append((signal, payload))

    dispatcher_mod.async_dispatcher_send = _async_dispatcher_send

    class _Store:
        def __init__(self, *args, **kwargs):
            self._d = None

        async def async_load(self):
            return self._d

        async def async_save(self, data):
            self._d = data

    storage_mod.Store = _Store

    components = types.ModuleType("homeassistant.components")
    ws_api_mod = types.ModuleType("homeassistant.components.websocket_api")

    def _websocket_command(schema):
        def _decorator(fn):
            return fn
        return _decorator

    def _async_response(fn):
        return fn

    ws_api_mod.websocket_command = _websocket_command
    ws_api_mod.async_response = _async_response
    ws_api_mod.ActiveConnection = object
    ws_api_mod.async_register_command = lambda hass, handler: None

    ha.core = core
    ha.helpers = helpers
    ha.components = components
    helpers.storage = storage_mod
    helpers.dispatcher = dispatcher_mod
    components.websocket_api = ws_api_mod

    # NOTE: overwrite (not setdefault) — each test run needs its own
    # dispatcher_calls closure. setdefault would leave an earlier test's
    # stub (and its now-orphaned dispatcher_calls list) registered, and the
    # freshly-reloaded websocket_api module would silently bind to that
    # stale closure instead of the list this call returns.
    for name, mod in (
        ("homeassistant", ha),
        ("homeassistant.core", core),
        ("homeassistant.helpers", helpers),
        ("homeassistant.helpers.storage", storage_mod),
        ("homeassistant.helpers.dispatcher", dispatcher_mod),
        ("homeassistant.components", components),
        ("homeassistant.components.websocket_api", ws_api_mod),
    ):
        sys.modules[name] = mod

    vol = types.ModuleType("voluptuous")
    vol.Required = lambda value: value
    sys.modules["voluptuous"] = vol

    return dispatcher_calls


def _load_websocket_api():
    dispatcher_calls = _stub_homeassistant()

    pkg = types.ModuleType("vwmpkg_ws")
    pkg.__path__ = [str(PKG_DIR)]
    sys.modules["vwmpkg_ws"] = pkg

    const = types.ModuleType("vwmpkg_ws.const")
    const.DOMAIN = "ha_vacuum_water_monitor"
    const.EVENT_STATE_CHANGED = "ha_vacuum_water_monitor_state_changed"
    const.STORAGE_KEY = "ha_vacuum_water_monitor"
    const.STORAGE_VERSION = 1
    const.DEFAULT_WARNING_THRESHOLD = 20
    const.DEFAULT_CRITICAL_THRESHOLD = 10
    const.signal_vacuum_water_updated = lambda entry_id: f"{const.DOMAIN}_{entry_id}_updated"
    sys.modules["vwmpkg_ws.const"] = const

    for name, filename in (("storage", "storage.py"), ("tick", "tick.py"), ("websocket_api", "websocket_api.py")):
        spec = importlib.util.spec_from_file_location(f"vwmpkg_ws.{name}", PKG_DIR / filename)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"vwmpkg_ws.{name}"] = mod
        spec.loader.exec_module(mod)

    return sys.modules["vwmpkg_ws.websocket_api"], sys.modules["vwmpkg_ws.storage"], dispatcher_calls


class _FakeBus:
    def __init__(self):
        self.fired: list[tuple[str, dict]] = []

    def async_fire(self, event, payload):
        self.fired.append((event, payload))


class _FakeConfigEntries:
    def async_entries(self, domain):
        return []


class _FakeHass:
    def __init__(self):
        self.data = {}
        self.bus = _FakeBus()
        self.config_entries = _FakeConfigEntries()


class _FakeConnection:
    def __init__(self):
        self.results = []
        self.errors = []

    def send_result(self, msg_id, data):
        self.results.append((msg_id, data))

    def send_error(self, msg_id, code, message):
        self.errors.append((msg_id, code, message))


class WebsocketBroadcastTest(unittest.TestCase):
    def test_repeated_identical_patch_broadcasts_only_once(self):
        ws_mod, storage_mod, dispatcher_calls = _load_websocket_api()

        async def scenario():
            hass = _FakeHass()
            storage = storage_mod.VacuumWaterStorage(hass)
            await storage.async_load()
            hass.data[ws_mod.DOMAIN] = {"storage": storage}
            conn = _FakeConnection()

            # Mirrors the old bug: the frontend re-sending the exact same
            # configured_devices patch on every hass tick.
            await ws_mod._ws_set_settings(hass, conn, {"id": 1, "patch": {"warning_threshold": 15}})
            await ws_mod._ws_set_settings(hass, conn, {"id": 2, "patch": {"warning_threshold": 15}})
            await ws_mod._ws_set_settings(hass, conn, {"id": 3, "patch": {"warning_threshold": 15}})
            return dispatcher_calls, hass.bus.fired, conn

        dispatcher_calls, fired, conn = asyncio.run(scenario())
        self.assertEqual(len(dispatcher_calls), 1, "no-op patches must not re-broadcast")
        self.assertEqual(len(fired), 1, "no-op patches must not re-fire the bus event")
        # But every call must still return the current settings to the caller.
        self.assertEqual(len(conn.results), 3)
        for _id, data in conn.results:
            self.assertEqual(data["settings"]["warning_threshold"], 15)

    def test_genuinely_different_patch_still_broadcasts(self):
        ws_mod, storage_mod, dispatcher_calls = _load_websocket_api()

        async def scenario():
            hass = _FakeHass()
            storage = storage_mod.VacuumWaterStorage(hass)
            await storage.async_load()
            hass.data[ws_mod.DOMAIN] = {"storage": storage}
            conn = _FakeConnection()

            await ws_mod._ws_set_settings(hass, conn, {"id": 1, "patch": {"warning_threshold": 15}})
            await ws_mod._ws_set_settings(hass, conn, {"id": 2, "patch": {"warning_threshold": 25}})
            return dispatcher_calls

        dispatcher_calls = asyncio.run(scenario())
        self.assertEqual(len(dispatcher_calls), 2, "an actual change must still broadcast")


if __name__ == "__main__":
    unittest.main(verbosity=2)
