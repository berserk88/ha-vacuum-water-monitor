"""Config flow for Vacuum water level."""

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
from .tick import (
    DEFAULT_DOCK_EMPTY_MESSAGE,
    DEFAULT_DOCK_FULL_MESSAGE,
    DEFAULT_DOCK_OK_MESSAGE,
    guess_brand_model,
    list_vacuums,
)

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


def _find_device_entry(
    settings: dict[str, Any], vacuum_entity: str
) -> dict[str, Any] | None:
    """Find a tracked vacuum's current stored config, checking
    configured_devices then user_devices (whichever owns it)."""
    for key in ("configured_devices", "user_devices"):
        for item in settings.get(key) or []:
            if isinstance(item, dict) and item.get("vacuum_entity") == vacuum_entity:
                return item
    return None


def _upsert_device_entry(
    devices: list[dict[str, Any]],
    vacuum_entity: str,
    updates: dict[str, Any],
    *,
    clear_keys: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """Return a new device list with `vacuum_entity`'s entry updated in
    place (or created, if missing) — `updates` is merged in and any key
    named in `clear_keys` is removed. One helper for both "Add a vacuum"
    (no existing entry, so this appends) and "Edit a vacuum" (existing
    entry — e.g. changing brand/model, or setting a dock error source)."""
    result = [dict(d) for d in devices]
    for entry in result:
        if entry.get("vacuum_entity") == vacuum_entity:
            entry.update(updates)
            for key in clear_keys:
                entry.pop(key, None)
            return result
    result.append({"vacuum_entity": vacuum_entity, **updates})
    return result


def _dock_error_updates(
    entity_id: str | None,
    attribute: str | None,
    empty_message: str | None = None,
    ok_message: str | None = None,
    full_message: str | None = None,
    waste_total_ml: float | None = None,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Compute the (updates, clear_keys) to apply for a dock-error-source
    edit: entity, attribute, the three customizable trigger messages (so
    this works across vacuum brands/integrations that phrase dock errors
    differently), and the waste/dirty tank capacity. Clearing the entity
    clears everything tied to it, since none of the rest means anything
    without a source entity. Each message/capacity can also be cleared
    independently (left blank) to fall back to the built-in default."""
    entity_id = (entity_id or "").strip() or None
    attribute = (attribute or "").strip() or None
    empty_message = (empty_message or "").strip() or None
    ok_message = (ok_message or "").strip() or None
    full_message = (full_message or "").strip() or None

    if not entity_id:
        return {}, (
            "dock_error_sensor",
            "dock_error_attribute",
            "dock_empty_message",
            "dock_ok_message",
            "dock_full_message",
            "waste_total_ml",
        )

    updates: dict[str, Any] = {"dock_error_sensor": entity_id}
    clear_keys: list[str] = []

    if attribute:
        updates["dock_error_attribute"] = attribute
    else:
        clear_keys.append("dock_error_attribute")

    for key, value in (
        ("dock_empty_message", empty_message),
        ("dock_ok_message", ok_message),
        ("dock_full_message", full_message),
    ):
        if value:
            updates[key] = value
        else:
            clear_keys.append(key)

    if waste_total_ml and waste_total_ml > 0:
        updates["waste_total_ml"] = waste_total_ml
    else:
        clear_keys.append("waste_total_ml")

    return updates, tuple(clear_keys)


def _mop_entity_updates(
    mop_mode_entity: str | None, mop_intensity_entity: str | None
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Compute the (updates, clear_keys) for a mop-entities edit. The two
    fields are independent — clearing one (leaving it blank) falls back
    to auto-detection for that entity specifically, without disturbing
    the other."""
    mop_mode_entity = (mop_mode_entity or "").strip() or None
    mop_intensity_entity = (mop_intensity_entity or "").strip() or None

    updates: dict[str, Any] = {}
    clear_keys: list[str] = []

    if mop_mode_entity:
        updates["mop_mode_entity"] = mop_mode_entity
    else:
        clear_keys.append("mop_mode_entity")

    if mop_intensity_entity:
        updates["mop_intensity_entity"] = mop_intensity_entity
    else:
        clear_keys.append("mop_intensity_entity")

    return updates, tuple(clear_keys)


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
                title="Vacuum water level",
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
    """Add/edit/remove tracked vacuums and edit warning thresholds."""

    def __init__(self) -> None:
        """Initialize per-flow state for the multi-step wizards."""
        # Which vacuum the current brand/model wizard run applies to, and
        # whether it's a new add (append) or an edit of an existing entry
        # (overwrite in place). Shared by both "Add a vacuum" and "Edit a
        # vacuum -> Change brand/model", since the brand/model steps
        # themselves don't need to know which one is in progress.
        self._target_entity_id: str | None = None
        self._target_brand: str | None = None
        self._editing: bool = False

    @property
    def _storage(self) -> VacuumWaterStorage:
        return self.hass.data[DOMAIN][DATA_STORAGE]

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Top-level menu."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["add_vacuum", "edit_vacuum", "remove_vacuum", "thresholds"],
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
            self._target_entity_id = user_input["vacuum_entity"]
            self._target_brand = None
            self._editing = False
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

    # ---- Edit vacuum ---------------------------------------------------

    async def async_step_edit_vacuum(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Pick which tracked vacuum to edit, then show what to change."""
        settings = await self._storage.async_get_settings()
        tracked = sorted(_tracked_vacuum_entities(settings))

        if not tracked:
            return self.async_abort(reason="no_tracked_vacuums")

        if user_input is not None:
            self._target_entity_id = user_input["vacuum_entity"]
            return self.async_show_menu(
                step_id="edit_vacuum_menu",
                menu_options=["edit_brand_model", "edit_dock_error", "edit_mop_entities"],
            )

        return self.async_show_form(
            step_id="edit_vacuum",
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

    async def async_step_edit_brand_model(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Re-run the brand/model wizard for an already-tracked vacuum,
        overwriting its stored capacity/brand/model in place."""
        self._target_brand = None
        self._editing = True
        return await self.async_step_select_brand()

    async def _async_apply_device_updates(
        self, updates: dict[str, Any], clear_keys: tuple[str, ...]
    ) -> config_entries.ConfigFlowResult:
        """Persist `updates`/`clear_keys` onto self._target_entity_id's
        existing device entry (whichever collection currently owns it).
        Shared by every edit step that patches fields on an
        already-tracked vacuum (dock error source, mop entities, ...)."""
        settings = await self._storage.async_get_settings()
        configured = list(settings.get("configured_devices") or [])
        user_devices = list(settings.get("user_devices") or [])
        if any(
            isinstance(d, dict) and d.get("vacuum_entity") == self._target_entity_id
            for d in user_devices
        ):
            user_devices = _upsert_device_entry(
                user_devices, self._target_entity_id, updates, clear_keys=clear_keys
            )
        else:
            configured = _upsert_device_entry(
                configured, self._target_entity_id, updates, clear_keys=clear_keys
            )
        await self._storage.async_set_settings(
            {"configured_devices": configured, "user_devices": user_devices}
        )
        self._target_entity_id = None
        return self.async_create_entry(title="", data=dict(self.config_entry.options))

    async def async_step_edit_dock_error(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Manually assign the entity (and optionally a specific attribute)
        that reports the dock's water-empty / waste-tank-full errors,
        overriding whatever auto-detection found (or didn't find). Some
        setups expose this on an attribute rather than the entity's main
        state — e.g. a docking station entity whose state is "docked" but
        whose "error" (or similar) attribute reads "Water empty" when the
        tank runs dry. The three trigger messages are customizable per
        vacuum, since different brands/integrations phrase them
        differently — clearing any of them falls back to the default
        (verified against the official Roborock integration's wording)."""
        settings = await self._storage.async_get_settings()
        current = _find_device_entry(settings, self._target_entity_id or "") or {}

        if user_input is not None:
            updates, clear_keys = _dock_error_updates(
                user_input.get("dock_error_sensor"),
                user_input.get("dock_error_attribute"),
                user_input.get("dock_empty_message"),
                user_input.get("dock_ok_message"),
                user_input.get("dock_full_message"),
                user_input.get("waste_total_ml"),
            )
            return await self._async_apply_device_updates(updates, clear_keys)

        def _field(key: str, suggested: Any) -> vol.Optional:
            return vol.Optional(key, description={"suggested_value": suggested})

        schema_dict: dict[Any, Any] = {
            _field("dock_error_sensor", current.get("dock_error_sensor")): (
                selector.EntitySelector(selector.EntitySelectorConfig())
            ),
            _field("dock_error_attribute", current.get("dock_error_attribute")): (
                selector.TextSelector(selector.TextSelectorConfig())
            ),
            _field(
                "dock_empty_message",
                current.get("dock_empty_message") or DEFAULT_DOCK_EMPTY_MESSAGE,
            ): selector.TextSelector(selector.TextSelectorConfig()),
            _field(
                "dock_ok_message",
                current.get("dock_ok_message") or DEFAULT_DOCK_OK_MESSAGE,
            ): selector.TextSelector(selector.TextSelectorConfig()),
            _field(
                "dock_full_message",
                current.get("dock_full_message") or DEFAULT_DOCK_FULL_MESSAGE,
            ): selector.TextSelector(selector.TextSelectorConfig()),
            _field(
                "waste_total_ml",
                current.get("waste_total_ml") or current.get("water_total_ml"),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=50,
                    max=10000,
                    step=50,
                    unit_of_measurement="mL",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
        }

        return self.async_show_form(
            step_id="edit_dock_error",
            data_schema=vol.Schema(schema_dict),
            description_placeholders={"entity_id": self._target_entity_id or ""},
        )

    async def async_step_edit_mop_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Manually assign the entities that report mop mode/intensity,
        overriding whatever auto-detection found (or didn't find). The
        intensity entity's state (e.g. Off/Low/Medium/High) is what
        server-side accounting uses both to compute area-based dosing
        rates and to attribute usage for the adaptive calibration model
        — assigning the right entity here matters if auto-detection
        picked the wrong one or found nothing."""
        settings = await self._storage.async_get_settings()
        current = _find_device_entry(settings, self._target_entity_id or "") or {}

        if user_input is not None:
            updates, clear_keys = _mop_entity_updates(
                user_input.get("mop_mode_entity"),
                user_input.get("mop_intensity_entity"),
            )
            return await self._async_apply_device_updates(updates, clear_keys)

        def _field(key: str, suggested: Any) -> vol.Optional:
            return vol.Optional(key, description={"suggested_value": suggested})

        schema_dict: dict[Any, Any] = {
            _field("mop_mode_entity", current.get("mop_mode_entity")): (
                selector.EntitySelector(selector.EntitySelectorConfig())
            ),
            _field("mop_intensity_entity", current.get("mop_intensity_entity")): (
                selector.EntitySelector(selector.EntitySelectorConfig())
            ),
        }

        return self.async_show_form(
            step_id="edit_mop_entities",
            data_schema=vol.Schema(schema_dict),
            description_placeholders={"entity_id": self._target_entity_id or ""},
        )

    # ---- Shared brand/model wizard (used by both Add and Edit) --------

    async def async_step_select_brand(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Pick a brand. Pre-selects the vacuum's current brand when
        editing, otherwise a confident device-registry guess (see
        tick.py::guess_brand_model) so most first-time adds just confirm."""
        if user_input is not None:
            if user_input["brand"] == CUSTOM_OPTION:
                return await self.async_step_custom_capacity()
            self._target_brand = user_input["brand"]
            return await self.async_step_select_model()

        default_brand = await self._default_brand()
        options = [
            selector.SelectOptionDict(value=brand, label=brand)
            for brand in sorted(MODEL_DATABASE)
        ]
        options.append(
            selector.SelectOptionDict(value=CUSTOM_OPTION, label="Other / not listed")
        )
        field = (
            vol.Required("brand", default=default_brand)
            if default_brand
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
            description_placeholders={"entity_id": self._target_entity_id or ""},
        )

    async def async_step_select_model(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Pick a model within the chosen brand."""
        models = MODEL_DATABASE.get(self._target_brand or "", {})

        if user_input is not None:
            if user_input["model"] == CUSTOM_OPTION:
                return await self.async_step_custom_capacity()
            return await self._async_save_vacuum(
                brand_profile=slugify(f"{self._target_brand} {user_input['model']}"),
                water_total_ml=float(models[user_input["model"]]),
                manufacturer=self._target_brand,
                model=user_input["model"],
            )

        default_model = await self._default_model()
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
            description_placeholders={"brand": self._target_brand or ""},
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

        settings = await self._storage.async_get_settings()
        current = _find_device_entry(settings, self._target_entity_id or "") or {}
        default_ml = current.get("water_total_ml") or 3000

        return self.async_show_form(
            step_id="custom_capacity",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "water_total_ml", default=default_ml
                    ): selector.NumberSelector(
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

    async def _default_brand(self) -> str | None:
        """Prefer the vacuum's already-stored brand (edit mode) over a
        fresh device-registry guess."""
        settings = await self._storage.async_get_settings()
        current = _find_device_entry(settings, self._target_entity_id or "")
        if current and current.get("manufacturer") in MODEL_DATABASE:
            return str(current["manufacturer"])
        guess = guess_brand_model(self.hass, self._target_entity_id)
        return guess[0] if guess else None

    async def _default_model(self) -> str | None:
        """Prefer the vacuum's already-stored model (edit mode) over a
        fresh device-registry guess."""
        settings = await self._storage.async_get_settings()
        current = _find_device_entry(settings, self._target_entity_id or "")
        if (
            current
            and current.get("manufacturer") == self._target_brand
            and current.get("model") in MODEL_DATABASE.get(self._target_brand or "", {})
        ):
            return str(current["model"])
        guess = guess_brand_model(self.hass, self._target_entity_id)
        return guess[1] if guess and guess[0] == self._target_brand else None

    async def _async_save_vacuum(
        self,
        *,
        brand_profile: str | None,
        water_total_ml: float,
        manufacturer: str | None = None,
        model: str | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Persist the vacuum's capacity/brand/model — appending a new
        configured_devices entry when adding, or overwriting the existing
        entry in place when editing. Auto-detection
        (tick.py::async_ensure_auto_config) fills in companion helper
        entities (status/area/mop mode/etc.) on the next tick for a new
        add; it never touches fields that are already set, so an edit's
        new values are never clobbered."""
        updates: dict[str, Any] = {"water_total_ml": water_total_ml}
        clear_keys: tuple[str, ...] = ()
        if brand_profile:
            updates["brand_profile"] = brand_profile
            updates["manufacturer"] = manufacturer
            updates["model"] = model
        else:
            # Switched to a custom capacity: don't leave a stale
            # brand/model behind from a previous database pick.
            clear_keys = ("brand_profile", "manufacturer", "model")

        settings = await self._storage.async_get_settings()
        configured = list(settings.get("configured_devices") or [])
        user_devices = list(settings.get("user_devices") or [])
        if any(
            isinstance(d, dict) and d.get("vacuum_entity") == self._target_entity_id
            for d in user_devices
        ):
            user_devices = _upsert_device_entry(
                user_devices, self._target_entity_id, updates, clear_keys=clear_keys
            )
        else:
            configured = _upsert_device_entry(
                configured, self._target_entity_id, updates, clear_keys=clear_keys
            )
        await self._storage.async_set_settings(
            {"configured_devices": configured, "user_devices": user_devices}
        )

        self._target_entity_id = None
        self._target_brand = None
        self._editing = False
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
