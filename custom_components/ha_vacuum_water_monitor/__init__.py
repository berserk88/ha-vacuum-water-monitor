"""Vacuum Water Monitor integration entry points."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    CONF_CRITICAL_THRESHOLD,
    CONF_WARNING_THRESHOLD,
    DATA_STORAGE,
    DATA_TICK_UNSUB,
    DEFAULT_TICK_INTERVAL_SECONDS,
    DOMAIN,
    EVENT_STATE_CHANGED,
    signal_vacuum_water_updated,
)
from .storage import VacuumWaterStorage
from .tick import async_ensure_auto_config, async_tick_water_state

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.BUTTON]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Vacuum Water Monitor from a config entry."""
    bucket = hass.data.setdefault(DOMAIN, {})
    storage = VacuumWaterStorage(hass)
    await storage.async_load()
    option_patch = {
        key: entry.options[key]
        for key in (CONF_WARNING_THRESHOLD, CONF_CRITICAL_THRESHOLD)
        if key in entry.options
    }
    if option_patch:
        await storage.async_set_settings(option_patch)
    bucket[DATA_STORAGE] = storage

    await _async_prune_ghost_devices(hass, storage)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _async_start_tick(hass, storage, entry.entry_id)

    # Apply option changes immediately. Without this listener the OptionsFlow
    # wrote the new thresholds to the entry but nothing re-read them, so they
    # only took effect after a Home Assistant restart.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    _LOGGER.debug("Vacuum Water Monitor set up (entry_id=%s)", entry.entry_id)
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Re-apply changed options immediately (no restart required)."""
    bucket = hass.data.get(DOMAIN, {})
    storage = bucket.get(DATA_STORAGE)
    if storage is None:
        return
    option_patch = {
        key: entry.options[key]
        for key in (CONF_WARNING_THRESHOLD, CONF_CRITICAL_THRESHOLD)
        if key in entry.options
    }
    if option_patch:
        await storage.async_set_settings(option_patch)
        _LOGGER.debug("Applied updated options: %s", sorted(option_patch))
    # The options flow's add/remove-vacuum steps write straight to storage
    # and don't change entry.options, so this listener won't fire for them.
    # Nudge listeners anyway in case thresholds changed, since that affects
    # every WaterLowBinarySensor's on/off state immediately.
    fresh_settings = await storage.async_get_settings()
    async_dispatcher_send(
        hass, signal_vacuum_water_updated(entry.entry_id), {"settings": fresh_settings}
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    bucket = hass.data.get(DOMAIN, {})
    if unsub := bucket.pop(DATA_TICK_UNSUB, None):
        unsub()
    bucket.pop(DATA_STORAGE, None)
    _LOGGER.debug("Vacuum Water Monitor unloaded (entry_id=%s)", entry.entry_id)
    return True


async def _async_prune_ghost_devices(
    hass: HomeAssistant, storage: VacuumWaterStorage
) -> None:
    """One-time cleanup of ghost vacuums (pre-5.1.6 card stub configs).

    Older card versions could persist a configured_device pointing at a brand
    profile's default entity id that never existed in this HA instance. Drop
    such entries — no matching entity AND no tank history — and remove their
    leftover device registry entries so users stop seeing a phantom "Vacuum"
    device (issue #1).
    """
    from homeassistant.helpers import device_registry as dr

    from .sensor_calculations import vacuum_slug
    from .tick import list_vacuums

    state = await storage.async_get_state()
    settings = state.get("settings") or {}
    tank_states = state.get("tank_states") or {}
    known = {vacuum["entity_id"] for vacuum in list_vacuums(hass)}

    def _is_ghost(item: object) -> bool:
        if not isinstance(item, dict):
            return True
        entity = str(item.get("vacuum_entity") or "")
        return bool(entity) and entity not in known and entity not in tank_states

    configured = settings.get("configured_devices") or []
    ghosts = [item for item in configured if _is_ghost(item)]
    if not ghosts:
        return

    kept = [item for item in configured if not _is_ghost(item)]
    ghost_entities = [
        str(item.get("vacuum_entity"))
        for item in ghosts
        if isinstance(item, dict) and item.get("vacuum_entity")
    ]
    _LOGGER.info(
        "Pruning ghost configured_devices with no HA entity: %s", ghost_entities
    )
    await storage.async_replace_settings_key("configured_devices", kept)

    registry = dr.async_get(hass)
    for entry in hass.config_entries.async_entries(DOMAIN):
        for entity in ghost_entities:
            device = registry.async_get_device(
                identifiers={(DOMAIN, f"{entry.entry_id}_{vacuum_slug(entity)}")}
            )
            if device:
                registry.async_remove_device(device.id)


def _async_start_tick(
    hass: HomeAssistant, storage: VacuumWaterStorage, entry_id: str
) -> None:
    """Start the 60s server-side accounting task."""
    bucket = hass.data.setdefault(DOMAIN, {})
    if bucket.get(DATA_TICK_UNSUB):
        return

    async def _tick(now=None) -> None:
        try:
            config_changed = await async_ensure_auto_config(hass, storage)
            changed = await async_tick_water_state(hass, storage)
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Water state tick failed: %s", err)
            return
        if changed:
            async_dispatcher_send(
                hass,
                signal_vacuum_water_updated(entry_id),
                {"tank_states": changed},
            )
            hass.bus.async_fire(EVENT_STATE_CHANGED, {"tank_states": changed})
        if config_changed:
            # Auto-detection resolved new companion entities/capacity for a
            # vacuum added via the config flow. Notify sensors so they
            # re-read storage fresh without waiting for the next real
            # tank_states change.
            fresh_settings = (await storage.async_get_state())["settings"]
            async_dispatcher_send(
                hass,
                signal_vacuum_water_updated(entry_id),
                {"settings": fresh_settings},
            )
            hass.bus.async_fire(EVENT_STATE_CHANGED, {"settings": fresh_settings})

    bucket[DATA_TICK_UNSUB] = async_track_time_interval(
        hass, _tick, timedelta(seconds=DEFAULT_TICK_INTERVAL_SECONDS)
    )
    hass.async_create_task(_tick())
