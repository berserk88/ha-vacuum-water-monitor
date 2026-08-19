"""Server-side water accounting for Vacuum Water Monitor."""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .sensor_calculations import DEFAULT_TANK_ML
from .storage import VacuumWaterStorage

MOP_WASH_STATES = {
    "washing_the_mop",
    "washing_the_mop_2",
    "going_to_wash_the_mop",
    "back_to_dock_washing_duster",
    "clean_mop_cleaning",
    "segment_clean_mop_cleaning",
    "zoned_clean_mop_cleaning",
}

DEFAULT_USAGE_PER_M2 = {"fast": 4, "standard": 6, "deep": 9}
DEFAULT_INTENSITY_FACTOR = {
    "low": 0.8,
    "medium": 1.0,
    "high": 1.2,
    "max": 1.3,
    "custom": 1.0,
    "smart_mode": 1.0,
    "custom_water_flow": 1.0,
}
DEFAULT_WASH_VOLUME_ML = 150
AREA_MIN_DELTA = 0.1
RESET_COOLDOWN_SEC = 60

# Companion-entity roles this integration needs for fully automatic
# server-side accounting, and how to recognize them among the OTHER
# entities that belong to the same HA device as the vacuum entity. Verified
# against the official `roborock` core integration's entity set (Cleaning
# area / Status / Mop mode / Mop intensity / Dock error sensors and
# selects), which all live on the same device as the vacuum entity itself.
# (domain, name-must-contain-any-of, name-must-not-contain-any-of)
_COMPANION_ROLE_HINTS: dict[str, tuple[str, tuple[str, ...], tuple[str, ...]]] = {
    "status_sensor": ("sensor", ("status",), ("dock",)),
    "area_sensor": ("sensor", ("cleaning area", "clean area"), ("total",)),
    "mop_mode_entity": ("select", ("mop mode", "mop route"), ("intensity",)),
    "mop_intensity_entity": ("select", ("mop intensity",), ()),
    "dock_error_sensor": ("sensor", ("dock error",), ()),
}


async def async_ensure_auto_config(
    hass: HomeAssistant, storage: VacuumWaterStorage
) -> bool:
    """Auto-detect tank capacity and companion helper entities (status,
    cleaned area, mop mode/intensity, dock error) for every known vacuum,
    and persist whatever isn't already manually configured.

    This is what makes the integration's "no configuration needed" promise
    actually true. Without it, capacity only resolves when a vacuum's
    entity_id happens to literally match one of the hardcoded profile keys,
    and server-side accounting only runs when the card's Settings tab has
    been used to manually wire each helper entity — neither of which
    happens during plain auto-discovery. Capacity is instead resolved from
    the vacuum's real manufacturer/model (via HA's device registry), and
    companion entities are found by locating other entities on the SAME
    device whose name matches a known role (see _COMPANION_ROLE_HINTS).

    Only fills in missing fields — anything already set (by the user, or by
    a previous run of this function) is left untouched, so manual overrides
    in the card's Settings tab always win. Returns True if anything was
    actually written, so the caller can notify listeners.
    """
    stored = await storage.async_get_state()
    settings = stored["settings"]

    configured = {
        item["vacuum_entity"]: dict(item)
        for item in (settings.get("configured_devices") or [])
        if isinstance(item, dict) and item.get("vacuum_entity")
    }
    user_devices = {
        item["vacuum_entity"]: dict(item)
        for item in (settings.get("user_devices") or [])
        if isinstance(item, dict) and item.get("vacuum_entity")
    }

    changed = False
    for vacuum in list_vacuums(hass):
        entity_id = vacuum["entity_id"]
        # user_devices shadows configured_devices entirely wherever both
        # merges (build_vacuum_devices/_devices_to_tick) are used, so enrich
        # whichever collection actually owns this vacuum; a brand-new
        # auto-discovered vacuum gets a fresh configured_devices entry.
        bucket = user_devices if entity_id in user_devices else configured
        entry = bucket.get(entity_id)
        is_new = entry is None
        if is_new:
            entry = {"vacuum_entity": entity_id, "name": vacuum["name"]}

        before = dict(entry)
        for role, value in _auto_detect_companions(hass, entity_id).items():
            entry.setdefault(role, value)
        if not entry.get("water_total_ml") and not entry.get("brand_profile"):
            resolved_ml = _resolve_default_tank_ml(hass, entity_id)
            if resolved_ml:
                entry["water_total_ml"] = resolved_ml

        if is_new or entry != before:
            bucket[entity_id] = entry
            changed = True

    if not changed:
        return False

    await storage.async_set_settings(
        {
            "configured_devices": list(configured.values()),
            "user_devices": list(user_devices.values()),
        }
    )
    return True


def _auto_detect_companions(
    hass: HomeAssistant, vacuum_entity_id: str
) -> dict[str, str]:
    """Find companion entities belonging to the same HA device as the
    vacuum, matched by name against _COMPANION_ROLE_HINTS. Only entities
    that exist and currently have a state are returned, so a guess never
    wires in a stale or removed entity."""
    found: dict[str, str] = {}
    try:
        ent_reg = er.async_get(hass)
        entry = ent_reg.async_get(vacuum_entity_id)
        if entry is None or entry.device_id is None:
            return found
        candidates = er.async_entries_for_device(
            ent_reg, entry.device_id, include_disabled_entities=False
        )
    except Exception:  # pragma: no cover - registries are always available in HA
        return found

    for role, (domain, must_any, must_not) in _COMPANION_ROLE_HINTS.items():
        for candidate in candidates:
            if candidate.domain != domain or candidate.entity_id == vacuum_entity_id:
                continue
            name = (candidate.name or candidate.original_name or "").lower()
            if not name or not any(hint in name for hint in must_any):
                continue
            if any(bad in name for bad in must_not):
                continue
            if hass.states.get(candidate.entity_id) is None:
                continue
            found[role] = candidate.entity_id
            break
    return found


def _resolve_default_tank_ml(
    hass: HomeAssistant, vacuum_entity_id: str
) -> float | None:
    """Resolve a default tank capacity from the vacuum's own HA device
    registry entry (manufacturer + model) instead of requiring the
    entity_id to happen to match a hardcoded key. Returns None (shown as
    "unknown capacity") if no confident match is found — the user can
    still set capacity manually via the card's Settings tab."""
    try:
        ent_reg = er.async_get(hass)
        entry = ent_reg.async_get(vacuum_entity_id)
        if entry is None or entry.device_id is None:
            return None
        device = dr.async_get(hass).async_get(entry.device_id)
        if device is None:
            return None
    except Exception:  # pragma: no cover - registries are always available in HA
        return None

    slug = _slugify(f"{device.manufacturer or ''} {device.model or ''}")
    if not slug:
        return None
    if slug in DEFAULT_TANK_ML:
        return DEFAULT_TANK_ML[slug]

    # Fuzzy fallback: accept a known key only if every one of its tokens
    # appears in the detected slug (handles extra words the integration may
    # add, e.g. an internal product code) — never the other way round, to
    # avoid a short/generic key matching too eagerly.
    slug_tokens = set(slug.split("_"))
    best_key, best_score = None, 0
    for key in DEFAULT_TANK_ML:
        key_tokens = set(key.split("_"))
        if key_tokens <= slug_tokens and len(key_tokens) > best_score:
            best_key, best_score = key, len(key_tokens)
    # Require at least a brand token plus one model token so e.g. a bare
    # "roborock" match on an unrecognised model can't win.
    return DEFAULT_TANK_ML[best_key] if best_key and best_score >= 2 else None


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")


async def async_tick_water_state(
    hass: HomeAssistant, storage: VacuumWaterStorage
) -> dict[str, dict[str, Any]]:
    """Tick every known vacuum and persist changed states."""
    stored = await storage.async_get_state()
    devices = _devices_to_tick(hass, stored["settings"])
    previous = stored["tank_states"]
    changed: dict[str, dict[str, Any]] = {}

    for device in devices:
        vacuum_entity = device.get("vacuum_entity")
        if not vacuum_entity:
            continue
        # Respect user's pre-existing DIY automations: if the device config
        # points at an input_number/input_datetime for water tracking AND that
        # entity is registered in HA, the user already has automation/template
        # accounting in place. Skip our own tick to avoid double-counting.
        if _has_user_priv_helpers(hass, device):
            continue
        state = VacuumWaterStorage.default_tank_state()
        state.update(previous.get(vacuum_entity) or {})
        new_state, dirty = tick_device(hass, device, state)
        if dirty:
            changed[vacuum_entity] = new_state

    if changed:
        await storage.async_set_tank_states(changed)
    return changed


def tick_device(
    hass: HomeAssistant, device: dict[str, Any], state: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    """Translate one v4 `_tickWaterState` pass into Python."""
    vacuum_entity = device.get("vacuum_entity")
    vac = hass.states.get(vacuum_entity) if vacuum_entity else None
    if vac is None:
        return state, False

    dirty = False
    status_sensor = device.get("status_sensor")
    status_state = hass.states.get(status_sensor) if status_sensor else None
    curr_status = (
        status_state.state
        if status_state is not None
        else vac.attributes.get("status") or vac.state
    )

    curr_area = _float_or_none(_state_value(hass, device.get("area_sensor")))
    curr_dock_err = _state_value(hass, device.get("dock_error_sensor"))
    curr_door = (
        _state_value(hass, device.get("reset_door_sensor"))
        if device.get("reset_door_sensor")
        else None
    )

    vac_state = vac.state
    mop_mode_raw = (
        _state_value(hass, device.get("mop_mode_entity"))
        if device.get("mop_mode_entity")
        else None
    )
    mop_intensity_raw = (
        _state_value(hass, device.get("mop_intensity_entity"))
        if device.get("mop_intensity_entity")
        else None
    )
    mop_mode = (
        mop_mode_raw
        if mop_mode_raw and mop_mode_raw not in {"off", "unavailable"}
        else "standard"
    )
    mop_intensity = (
        mop_intensity_raw
        if mop_intensity_raw and mop_intensity_raw != "unavailable"
        else "medium"
    )
    mop_off = mop_mode_raw == "off"

    usage_per_m2 = _mapping_number(
        device.get("usage_ml_per_m2"),
        mop_mode,
        DEFAULT_USAGE_PER_M2.get(mop_mode, 6),
    )
    intensity_factor = _mapping_number(
        device.get("intensity_factor"),
        mop_intensity,
        DEFAULT_INTENSITY_FACTOR.get(mop_intensity, 1.0),
    )
    wash_volume = _number(device.get("wash_volume_ml"), DEFAULT_WASH_VOLUME_ML)

    if (
        state.get("last_status") is not None
        and curr_status != state.get("last_status")
        and curr_status in MOP_WASH_STATES
    ):
        state["used_ml"] = round(_number(state.get("used_ml"), 0) + wash_volume)
        dirty = True

    last_area = _float_or_none(state.get("last_area"))
    if last_area is not None and curr_area is not None and curr_area > last_area:
        delta = curr_area - last_area
        is_cleaning = vac_state == "cleaning" or curr_status == "cleaning"
        can_dose = not mop_off and is_cleaning and delta >= AREA_MIN_DELTA
        if can_dose:
            added = delta * usage_per_m2 * intensity_factor
            state["used_ml"] = round(_number(state.get("used_ml"), 0) + added)
            dirty = True

    now_ts = int(datetime.now(timezone.utc).timestamp() * 1000)
    cooldown_ok = (
        (now_ts - int(state.get("last_reset_ts") or 0)) / 1000
        > RESET_COOLDOWN_SEC
    )
    do_reset = False
    if curr_door and state.get("last_door") == "on" and curr_door == "off":
        do_reset = True
    if (
        state.get("last_dock_err") == "water_empty"
        and curr_dock_err
        and curr_dock_err != "water_empty"
    ):
        do_reset = True

    if do_reset and cooldown_ok:
        state["used_ml"] = 0
        state["last_reset_iso"] = datetime.now(timezone.utc).isoformat()
        state["last_reset_ts"] = now_ts
        dirty = True

    if state.get("last_status") != curr_status:
        state["last_status"] = curr_status
        dirty = True
    if curr_area is not None and state.get("last_area") != curr_area:
        state["last_area"] = curr_area
        dirty = True
    if state.get("last_dock_err") != curr_dock_err:
        state["last_dock_err"] = curr_dock_err
        dirty = True
    if state.get("last_door") != curr_door:
        state["last_door"] = curr_door
        dirty = True

    return state, dirty


def list_vacuums(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Return HA-known vacuum entities for the card."""
    vacuums = []
    for entity_id in sorted(hass.states.async_entity_ids("vacuum")):
        state = hass.states.get(entity_id)
        if state is None:
            continue
        vacuums.append(
            {
                "entity_id": entity_id,
                "name": state.attributes.get("friendly_name") or entity_id,
                "state": state.state,
                "battery": state.attributes.get("battery_level"),
            }
        )
    return vacuums


def _devices_to_tick(
    hass: HomeAssistant, settings: dict[str, Any]
) -> list[dict[str, Any]]:
    devices: dict[str, dict[str, Any]] = {}
    for item in settings.get("configured_devices") or []:
        if isinstance(item, dict) and item.get("vacuum_entity"):
            devices[item["vacuum_entity"]] = dict(item)
    for item in settings.get("user_devices") or []:
        if isinstance(item, dict) and item.get("vacuum_entity"):
            devices[item["vacuum_entity"]] = dict(item)
    for vacuum in list_vacuums(hass):
        devices.setdefault(
            vacuum["entity_id"],
            {
                "vacuum_entity": vacuum["entity_id"],
                "name": vacuum["name"],
            },
        )
    return list(devices.values())


def _has_user_priv_helpers(
    hass: HomeAssistant, device: dict[str, Any]
) -> bool:
    """Return True if the device config references a user-owned helper that
    already tracks water usage server-side (input_number / input_datetime /
    template sensor created by a DIY automation). In that case the integration
    must defer accounting to the user's existing setup and only display state.

    The card mirror (`www/ha-vacuum-water-monitor.js::_hasPrivHelpers`) is kept
    in sync — it queries the same key (`water_used_input`) against `hass.states`.
    """
    input_id = device.get("water_used_input")
    if not input_id:
        return False
    return hass.states.get(input_id) is not None


def _state_value(hass: HomeAssistant, entity_id: str | None) -> str | None:
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    return state.state if state is not None else None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _number(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _mapping_number(value: Any, key: str, default: float) -> float:
    if isinstance(value, dict):
        return _number(value.get(key), default)
    return default
