"""Button entities for Vacuum Water Monitor (manual tank refill)."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.entity import DeviceInfo

from .const import DATA_STORAGE, DOMAIN, MANUFACTURER, MODEL, signal_vacuum_water_updated
from .sensor_calculations import build_vacuum_devices, filter_active_devices, vacuum_slug
from .storage import VacuumWaterStorage
from .tick import list_vacuums

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    """Set up the Refilled button for each tracked vacuum."""
    manager = RefillButtonManager(hass, entry, async_add_entities)
    await manager.async_setup()


class RefillButtonManager:
    """Create a Refilled button for each vacuum tracked via the config flow."""

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
        """Add a button for any newly tracked vacuum."""
        storage = _storage(self.hass)
        stored = await storage.async_get_state()
        settings = stored.get("settings") or {}
        tank_states = stored.get("tank_states") or {}
        try:
            discovered = list_vacuums(self.hass)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Unable to list vacuum entities for refill buttons: %s", err)
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

        entities: list[RefillButton] = []
        for device in devices:
            vacuum_entity = device.get("vacuum_entity")
            if not vacuum_entity or vacuum_entity in self._known:
                continue
            self._known.add(str(vacuum_entity))
            entities.append(RefillButton(self.hass, self.entry, device))

        if entities:
            self.async_add_entities(entities)


class RefillButton(ButtonEntity):
    """Press after filling the tank. Resets the used_ml counter to 0."""

    _attr_has_entity_name = True
    _attr_name = "Refilled"
    _attr_icon = "mdi:cup-water"
    _attr_should_poll = False

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, device: dict[str, Any]
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.vacuum_entity = str(device["vacuum_entity"])
        self.vacuum_slug = vacuum_slug(self.vacuum_entity)
        self._device = dict(device)
        self._attr_unique_id = f"{entry.entry_id}_{self.vacuum_slug}_refill"

    @property
    def device_info(self) -> DeviceInfo:
        """Return the same per-vacuum device the sensors use."""
        name = self._device.get("name") or self.vacuum_entity
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.entry.entry_id}_{self.vacuum_slug}")},
            manufacturer=str(self._device.get("manufacturer") or MANUFACTURER),
            model=str(self._device.get("model") or self._device.get("brand_profile") or MODEL),
            name=str(name),
        )

    async def async_press(self) -> None:
        """Reset the tank counter and refresh sensors immediately."""
        storage = _storage(self.hass)
        now = datetime.now(timezone.utc)
        await storage.async_reset_tank(
            self.vacuum_entity, now.isoformat(), int(now.timestamp() * 1000)
        )
        _LOGGER.info("Refilled %s: tank counter reset", self.vacuum_entity)
        # Don't wait for the next 60s tick -- sensors should update the
        # instant the button is pressed.
        async_dispatcher_send(
            self.hass,
            signal_vacuum_water_updated(self.entry.entry_id),
            {"tank_states": {self.vacuum_entity: {}}},
        )


def _storage(hass: HomeAssistant) -> VacuumWaterStorage:
    return hass.data[DOMAIN][DATA_STORAGE]
