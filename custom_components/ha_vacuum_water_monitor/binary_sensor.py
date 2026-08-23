"""Binary sensor entities for Vacuum Water Monitor (low-water warning)."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo

from .const import DATA_STORAGE, DOMAIN, MANUFACTURER, MODEL, signal_vacuum_water_updated
from .sensor_calculations import (
    build_vacuum_devices,
    estimate_water_state,
    filter_active_devices,
    vacuum_slug,
)
from .storage import VacuumWaterStorage
from .tick import list_vacuums

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    """Set up the low-water warning binary sensor for each tracked vacuum."""
    manager = WaterLowBinarySensorManager(hass, entry, async_add_entities)
    await manager.async_setup()


class WaterLowBinarySensorManager:
    """Create a Water low binary sensor for each tracked vacuum."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, async_add_entities
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.async_add_entities = async_add_entities
        self._known: set[str] = set()

    async def async_setup(self) -> None:
        """Subscribe to Store updates and add initial entities."""
        self.entry.async_on_unload(
            async_dispatcher_connect(
                self.hass,
                signal_vacuum_water_updated(self.entry.entry_id),
                self._handle_store_update,
            )
        )
        await self.async_sync_devices()

    @callback
    def _handle_store_update(self, _payload: dict[str, Any] | None = None) -> None:
        self.hass.async_create_task(self.async_sync_devices())

    async def async_sync_devices(self) -> None:
        """Add a binary sensor for any newly tracked vacuum."""
        storage = _storage(self.hass)
        stored = await storage.async_get_state()
        settings = stored.get("settings") or {}
        tank_states = stored.get("tank_states") or {}
        try:
            discovered = list_vacuums(self.hass)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Unable to list vacuum entities for warning sensors: %s", err)
            discovered = []

        known_entities = {
            str(item.get("entity_id"))
            for item in discovered
            if isinstance(item, dict) and item.get("entity_id")
        }
        devices = filter_active_devices(
            build_vacuum_devices(settings, tank_states, discovered),
            known_entities,
            tank_states,
        )

        entities: list[WaterLowBinarySensor] = []
        for device in devices:
            vacuum_entity = device.get("vacuum_entity")
            if not vacuum_entity or vacuum_entity in self._known:
                continue
            self._known.add(str(vacuum_entity))
            entities.append(WaterLowBinarySensor(self.hass, self.entry, device))

        if entities:
            self.async_add_entities(entities, True)


class WaterLowBinarySensor(BinarySensorEntity):
    """On when remaining water is at or below the warning/critical threshold."""

    _attr_has_entity_name = True
    _attr_name = "Water low"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_should_poll = False

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, device: dict[str, Any]
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.vacuum_entity = str(device["vacuum_entity"])
        self.vacuum_slug = vacuum_slug(self.vacuum_entity)
        self._device = dict(device)
        self._fallback_device = dict(device)
        self._attr_unique_id = f"{entry.entry_id}_{self.vacuum_slug}_water_low"

    @property
    def device_info(self) -> DeviceInfo:
        """Return the same per-vacuum device the sensors/button use."""
        name = self._device.get("name") or self.vacuum_entity
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.entry.entry_id}_{self.vacuum_slug}")},
            manufacturer=str(self._device.get("manufacturer") or MANUFACTURER),
            model=str(self._device.get("model") or self._device.get("brand_profile") or MODEL),
            name=str(name),
        )

    @property
    def _storage(self) -> VacuumWaterStorage:
        return _storage(self.hass)

    async def async_added_to_hass(self) -> None:
        """Subscribe to Store writes."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_vacuum_water_updated(self.entry.entry_id),
                self._handle_store_update,
            )
        )
        await self.async_refresh()

    @callback
    def _handle_store_update(self, _payload: dict[str, Any] | None = None) -> None:
        self.hass.async_create_task(self.async_refresh())

    async def async_refresh(self) -> None:
        """Refresh from Store and write state."""
        await self.async_update()
        self.async_write_ha_state()

    async def async_update(self) -> None:
        """Recompute on/off from the same estimate the sensors use."""
        stored = await self._storage.async_get_state()
        settings = stored.get("settings") or {}
        tank_states = stored.get("tank_states") or {}
        for device in build_vacuum_devices(settings, tank_states):
            if device.get("vacuum_entity") == self.vacuum_entity:
                self._device = {**self._fallback_device, **device}
                break
        else:
            self._device = dict(self._fallback_device)

        tank_state = VacuumWaterStorage.default_tank_state()
        stored_tank = tank_states.get(self.vacuum_entity)
        if isinstance(stored_tank, dict):
            tank_state.update(stored_tank)

        estimate = estimate_water_state(self._device, tank_state, settings)
        remaining = estimate["remaining_percent"]
        warning_threshold = settings.get("warning_threshold")
        critical_threshold = settings.get("critical_threshold")

        if remaining is None:
            self._attr_is_on = False
            severity = "unknown"
        elif critical_threshold is not None and remaining <= critical_threshold:
            self._attr_is_on = True
            severity = "critical"
        elif warning_threshold is not None and remaining <= warning_threshold:
            self._attr_is_on = True
            severity = "warning"
        else:
            self._attr_is_on = False
            severity = "ok"

        self._attr_extra_state_attributes = {
            "vacuum_entity": self.vacuum_entity,
            "severity": severity,
            "remaining_percent": remaining,
            "warning_threshold": warning_threshold,
            "critical_threshold": critical_threshold,
        }


def _storage(hass: HomeAssistant) -> VacuumWaterStorage:
    return hass.data[DOMAIN][DATA_STORAGE]
