"""Config flow for Vacuum Water Monitor."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers import selector

from .const import (
    CONF_CRITICAL_THRESHOLD,
    CONF_WARNING_THRESHOLD,
    DATA_STORAGE,
    DEFAULT_CRITICAL_THRESHOLD,
    DEFAULT_WARNING_THRESHOLD,
    DOMAIN,
)
from .sensor_calculations import MODEL_DATABASE, slugify
from .storage import VacuumWaterStorage
from .tick import guess_brand_model, list_vacuums


# Sentinel option value for "this model isn't in the database" — picking it
# routes to a manual capacity input instead of a database lookup.
CUSTOM_OPTION = "__custom__"


def _tracked_vacuum_entities(settings: dict[str, Any]) -> set[str]:
    """Every vacuum_entity already present in configured_devices or
    user_devices — i.e. already opted in to tracking."""
    return {
        str(item.get("vacuum_entity"))
        for key in ("configured_devices", "user_devices")
        for item in (settings.get(key) or [])
        if isinstance(item, dict) and item.get("vacuum_entity")
    }


def _available_vacuum_entities(
    all_entity_ids: list[str], settings: dict[str, Any]
) -> list[str]:
    """Vacuum entities that exist in HA but aren't tracked yet, for the
    "Add vacuum" step's entity picker."""
    tracked = _tracked_vacuum_entities(settings)
    return sorted(entity_id for entity_id in all_entity_ids if entity_id not in tracked)


def _build_new_device_entry(
    vacuum_entity: str,
    *,
    water_total_ml: float,
    brand_profile: str | None = None,
    manufacturer: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Build the configured_devices entry to persist for a newly-added
    vacuum. Only includes brand/model fields that were actually resolved,
    so a custom-capacity entry stays minimal."""
    entry: dict[str, Any] = {
        "vacuum_entity": vacuum_entity,
        "water_total_ml": water_total_ml,
    }
    if brand_profile:
        entry["brand_profile"] = brand_profile
    if manufacturer:
        entry["manufacturer"] = manufacturer
    if model:
        entry["model"] = model
    return entry


class HAVacuumWaterMonitorConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Single-instance setup flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle initial setup."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            return self.async_create_entry(
                title="Vacuum Water Monitor",
                data={},
                options={
                    CONF_WARNING_THRESHOLD: user_input[CONF_WARNING_THRESHOLD],
                    CONF_CRITICAL_THRESHOLD: user_input[CONF_CRITICAL_THRESHOLD],
                },
            )

        return self.async_show_form(
            step_id="user",
            data_schema=_threshold_schema(
                DEFAULT_WARNING_THRESHOLD,
                DEFAULT_CRITICAL_THRESHOLD,
            ),
        )

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return the options flow. config_entry is provided automatically
        as self.config_entry by the base class — do not store it here."""
        return HAVacuumWaterMonitorOptionsFlow()


class HAVacuumWaterMonitorOptionsFlow(config_entries.OptionsFlow):
    """Add/remove tracked vacuums and edit warning thresholds."""

    def __init__(self) -> None:
        """Initialize per-flow state for the multi-step add-vacuum wizard."""
        self._add_entity_id: str | None = None
        self._add_brand: str | None = None

    @property
    def _storage(self) -> VacuumWaterStorage:
        return self.hass.data[DOMAIN][DATA_STORAGE]

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Top-level menu."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["add_vacuum", "remove_vacuum", "thresholds"],
        )

    # ---- Add vacuum --------------------------------------------------

    async def async_step_add_vacuum(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Step 1: pick which vacuum entity to track."""
        settings = await self._storage.async_get_settings()
        all_entity_ids = [v["entity_id"] for v in list_vacuums(self.hass)]
        available = _available_vacuum_entities(all_entity_ids, settings)

        if not available:
            return self.async_abort(reason="no_new_vacuums")

        if user_input is not None:
            self._add_entity_id = user_input["vacuum_entity"]
            return await self.async_step_select_brand()

        return self.async_show_form(
            step_id="add_vacuum",
            data_schema=vol.Schema(
                {
                    vol.Required("vacuum_entity"): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain="vacuum", include_entities=available
                        )
                    ),
                }
            ),
        )

    async def async_step_select_brand(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Step 2: pick a brand. Pre-selects a confident device-registry
        guess (see tick.py::guess_brand_model) so most users just confirm."""
        if user_input is not None:
            if user_input["brand"] == CUSTOM_OPTION:
                return await self.async_step_custom_capacity()
            self._add_brand = user_input["brand"]
            return await self.async_step_select_model()

        guess = guess_brand_model(self.hass, self._add_entity_id)
        options = [
            selector.SelectOptionDict(value=brand, label=brand)
            for brand in sorted(MODEL_DATABASE)
        ]
        options.append(
            selector.SelectOptionDict(value=CUSTOM_OPTION, label="Other / not listed")
        )
        field = (
            vol.Required("brand", default=guess[0])
            if guess
            else vol.Required("brand")
        )

        return self.async_show_form(
            step_id="select_brand",
            data_schema=vol.Schema(
                {
                    field: selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=options, mode=selector.SelectSelectorMode.DROPDOWN
                        )
                    )
                }
            ),
            description_placeholders={"entity_id": self._add_entity_id or ""},
        )

    async def async_step_select_model(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Step 3: pick a model within the chosen brand."""
        models = MODEL_DATABASE.get(self._add_brand or "", {})

        if user_input is not None:
            if user_input["model"] == CUSTOM_OPTION:
                return await self.async_step_custom_capacity()
            return await self._async_save_vacuum(
                brand_profile=slugify(f"{self._add_brand} {user_input['model']}"),
                water_total_ml=float(models[user_input["model"]]),
                manufacturer=self._add_brand,
                model=user_input["model"],
            )

        guess = guess_brand_model(self.hass, self._add_entity_id)
        default_model = guess[1] if guess and guess[0] == self._add_brand else None
        options = [
            selector.SelectOptionDict(value=name, label=f"{name} ({int(ml)} mL)")
            for name, ml in sorted(models.items())
        ]
        options.append(
            selector.SelectOptionDict(
                value=CUSTOM_OPTION, label="Other / custom capacity"
            )
        )
        field = (
            vol.Required("model", default=default_model)
            if default_model
            else vol.Required("model")
        )

        return self.async_show_form(
            step_id="select_model",
            data_schema=vol.Schema(
                {
                    field: selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=options, mode=selector.SelectSelectorMode.DROPDOWN
                        )
                    )
                }
            ),
            description_placeholders={"brand": self._add_brand or ""},
        )

    async def async_step_custom_capacity(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Manual tank capacity for a model that isn't in the database."""
        if user_input is not None:
            return await self._async_save_vacuum(
                brand_profile=None,
                water_total_ml=float(user_input["water_total_ml"]),
            )

        return self.async_show_form(
            step_id="custom_capacity",
            data_schema=vol.Schema(
                {
                    vol.Required("water_total_ml", default=3000): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=50,
                            max=10000,
                            step=50,
                            unit_of_measurement="mL",
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                }
            ),
        )

    async def _async_save_vacuum(
        self,
        *,
        brand_profile: str | None,
        water_total_ml: float,
        manufacturer: str | None = None,
        model: str | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Persist the new device into configured_devices, then let
        auto-detection (tick.py::async_ensure_auto_config) fill in companion
        helper entities (status/area/mop mode/etc.) on the next tick."""
        settings = await self._storage.async_get_settings()
        configured = list(settings.get("configured_devices") or [])
        configured.append(
            _build_new_device_entry(
                self._add_entity_id,
                water_total_ml=water_total_ml,
                brand_profile=brand_profile,
                manufacturer=manufacturer,
                model=model,
            )
        )
        await self._storage.async_set_settings({"configured_devices": configured})

        self._add_entity_id = None
        self._add_brand = None
        return self.async_create_entry(title="", data=dict(self.config_entry.options))

    # ---- Remove vacuum -------------------------------------------------

    async def async_step_remove_vacuum(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Stop tracking a previously-added vacuum."""
        settings = await self._storage.async_get_settings()
        configured = settings.get("configured_devices") or []
        user_devices = settings.get("user_devices") or []
        tracked = sorted(_tracked_vacuum_entities(settings))

        if not tracked:
            return self.async_abort(reason="no_tracked_vacuums")

        if user_input is not None:
            target = user_input["vacuum_entity"]
            await self._storage.async_replace_settings_key(
                "configured_devices",
                [
                    d
                    for d in configured
                    if not (isinstance(d, dict) and d.get("vacuum_entity") == target)
                ],
            )
            await self._storage.async_replace_settings_key(
                "user_devices",
                [
                    d
                    for d in user_devices
                    if not (isinstance(d, dict) and d.get("vacuum_entity") == target)
                ],
            )
            return self.async_create_entry(
                title="", data=dict(self.config_entry.options)
            )

        return self.async_show_form(
            step_id="remove_vacuum",
            data_schema=vol.Schema(
                {
                    vol.Required("vacuum_entity"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=tracked,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
        )

    # ---- Thresholds ------------------------------------------------------

    async def async_step_thresholds(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Edit the default low-water / critical-water thresholds."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options
        return self.async_show_form(
            step_id="thresholds",
            data_schema=_threshold_schema(
                options.get(CONF_WARNING_THRESHOLD, DEFAULT_WARNING_THRESHOLD),
                options.get(CONF_CRITICAL_THRESHOLD, DEFAULT_CRITICAL_THRESHOLD),
            ),
        )


def _threshold_schema(warning: int, critical: int) -> vol.Schema:
    """Build threshold schema."""
    return vol.Schema(
        {
            vol.Required(CONF_WARNING_THRESHOLD, default=warning): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=99)
            ),
            vol.Required(CONF_CRITICAL_THRESHOLD, default=critical): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=99)
            ),
        }
    )
