"""Pure Store-derived calculations for Vacuum Water Monitor sensors."""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any

MILLISECONDS_PER_DAY = 86_400_000


def slugify(text: str) -> str:
    """Normalize free text into a stable lowercase_with_underscores slug."""
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")


# Known vacuum models, grouped by brand, for the config flow's brand/model
# selector. Capacity in mL. This is also the source of truth for
# DEFAULT_TANK_ML (see below) — add new models here, not there.
MODEL_DATABASE: dict[str, dict[str, float]] = {
    "Roborock": {
        "S8 MaxV Ultra": 3000,
        "S8 Pro Ultra": 3500,
        "S7 MaxV Ultra": 3000,
        "S7 MaxV": 200,
        "S9 MaxV": 4000,
        "Q Revo": 5000,
        "Q Revo MaxV": 4000,
        "Q7 Max": 350,
        "Q7": 300,
    },
    "Dreame": {
        "X40 Ultra": 4500,
        "X30 Ultra": 4500,
        "L20 Ultra": 4500,
        "L10s Ultra": 2500,
        "L10s Pro Ultra": 4500,
        "D10 Plus": 150,
    },
    "Ecovacs": {
        "X2 Omni": 4000,
        "T20 Omni": 4000,
        "T30 Omni": 4000,
        "N20 Plus": 220,
    },
    "iRobot": {
        "Combo j9": 3000,
        "Combo j7": 210,
        "Combo Essential": 200,
    },
    "Narwal": {
        "Freo X Ultra": 5000,
        "Freo X Plus": 280,
    },
    "Eufy": {
        "X10 Pro Omni": 3000,
    },
    "Samsung": {
        "Jet Bot Combo": 4000,
    },
    "Xiaomi": {
        "X20 Max": 4000,
        "X20 Pro": 4000,
    },
}

# Default tank capacity (ml) per known vacuum model, keyed by
# slugify(f"{brand} {model}") — e.g. "roborock_s8_maxv_ultra". Resolved
# automatically via the vacuum's real HA device registry entry (manufacturer
# + model — see tick.py::_resolve_default_tank_ml), with the same key also
# used for a manually-set brand_profile (config flow) or the vacuum
# entity_id slug, for backward-compatible manual configs. An unrecognised
# model stays "unknown" instead of reporting a misleading percentage.
DEFAULT_TANK_ML: dict[str, float] = {
    slugify(f"{brand} {model}"): tank_ml
    for brand, models in MODEL_DATABASE.items()
    for model, tank_ml in models.items()
}


def vacuum_slug(vacuum_entity: str) -> str:
    """Return a stable slug for a vacuum entity id."""
    return slugify(vacuum_entity) or "unknown"


def build_vacuum_devices(
    settings: dict[str, Any] | None,
    tank_states: dict[str, Any] | None,
    discovered_vacuums: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build the effective vacuum list from Store settings and known state.

    Tracking is opt-in: a device only appears here if it's in
    configured_devices/user_devices (added via the config flow) or has tank
    history. discovered_vacuums is never used to create new devices — only
    to backfill a friendly display name onto an already-tracked device that
    doesn't have one yet.
    """
    settings = settings if isinstance(settings, dict) else {}
    tank_states = tank_states if isinstance(tank_states, dict) else {}
    discovered_vacuums = discovered_vacuums if isinstance(discovered_vacuums, list) else []

    devices: dict[str, dict[str, Any]] = {}

    for key in ("configured_devices", "user_devices"):
        for item in settings.get(key) or []:
            if not isinstance(item, dict):
                continue
            vacuum_entity = item.get("vacuum_entity")
            if not vacuum_entity:
                continue
            devices[str(vacuum_entity)] = _normalize_device(str(vacuum_entity), item)

    for vacuum_entity in tank_states:
        if not vacuum_entity:
            continue
        devices.setdefault(
            str(vacuum_entity),
            _normalize_device(str(vacuum_entity), {"vacuum_entity": vacuum_entity}),
        )

    # Backfill display names from live discovery: entries seeded from
    # tank_states or configured_devices may carry no name and fall back to the
    # raw entity_id ("vacuum.roborock_s7_maxv" instead of "Roborock S7 MaxV").
    for item in discovered_vacuums:
        if not isinstance(item, dict):
            continue
        entity = str(item.get("entity_id") or item.get("vacuum_entity") or "")
        name = item.get("name")
        if not entity or not name or entity not in devices:
            continue
        device = devices[entity]
        if not device.get("name") or device.get("name") == entity:
            device["name"] = str(name)

    return list(devices.values())


def filter_active_devices(
    devices: list[dict[str, Any]],
    known_entities: set[str],
    tank_states: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Drop ghost devices before entity/device creation.

    A device is kept when its vacuum_entity currently exists in HA (known) or
    has recorded tank history (real vacuum that is temporarily offline).
    Phantom entries — e.g. a profile-default entity id persisted by a pre-5.1.6
    card stub config — match neither and must not create HA devices.
    """
    tank_states = tank_states if isinstance(tank_states, dict) else {}
    kept: list[dict[str, Any]] = []
    for device in devices:
        vacuum_entity = device.get("vacuum_entity")
        if not vacuum_entity:
            continue
        entity = str(vacuum_entity)
        if entity not in known_entities and entity not in tank_states:
            continue
        kept.append(device)
    return kept


def estimate_water_state(
    device: dict[str, Any] | None,
    tank_state: dict[str, Any] | None,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Estimate water state from stored tank usage and stored capacity."""
    device = device if isinstance(device, dict) else {}
    tank_state = tank_state if isinstance(tank_state, dict) else {}
    settings = settings if isinstance(settings, dict) else {}

    used_ml = max(0, _number(tank_state.get("used_ml"), 0))
    total_ml = _water_capacity_ml(device, settings)
    if total_ml is None:
        return {
            "source": "unknown_capacity",
            "total_ml": None,
            "used_ml": _format_number(used_ml),
            "remaining_ml": None,
            "remaining_percent": None,
        }

    remaining_ml = max(0, total_ml - used_ml)
    percent = _clamp((remaining_ml / total_ml) * 100, 0, 100)
    return {
        "source": "stored_estimate",
        "total_ml": _format_number(total_ml),
        "used_ml": _format_number(used_ml),
        "remaining_ml": _format_number(remaining_ml),
        "remaining_percent": _format_number(round(percent, 1)),
    }


def parse_refill_datetime(tank_state: dict[str, Any] | None) -> datetime | None:
    """Parse the Store refill timestamp as an aware UTC datetime."""
    tank_state = tank_state if isinstance(tank_state, dict) else {}
    raw_iso = tank_state.get("last_reset_iso")
    if isinstance(raw_iso, str) and raw_iso.strip():
        try:
            parsed = datetime.fromisoformat(raw_iso.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)

    raw_ts = _optional_number(tank_state.get("last_reset_ts"))
    if raw_ts and raw_ts > 0:
        seconds = raw_ts / 1000 if raw_ts > 10_000_000_000 else raw_ts
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    return None


def next_maintenance_due(
    maintenance_items: list[Any] | None, now_ms: int | None = None
) -> dict[str, Any] | None:
    """Return the most urgent scheduled custom maintenance item."""
    if not isinstance(maintenance_items, list):
        return None
    if now_ms is None:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    candidates: list[dict[str, Any]] = []
    for index, item in enumerate(maintenance_items):
        if not isinstance(item, dict):
            continue
        interval_days = _optional_number(item.get("intervalDays"))
        last_done_ms = _optional_number(item.get("lastDone"))
        if not interval_days or interval_days <= 0 or not last_done_ms:
            continue

        days_since = int((now_ms - last_done_ms) // MILLISECONDS_PER_DAY)
        days_left = int(interval_days) - days_since
        due_at_ms = int(last_done_ms + int(interval_days) * MILLISECONDS_PER_DAY)
        candidate = {
            "index": index,
            "name": str(item.get("name") or "Maintenance item"),
            "icon": item.get("icon"),
            "interval_days": int(interval_days),
            "last_done_ms": int(last_done_ms),
            "last_done_at": _datetime_from_ms(int(last_done_ms)).isoformat(),
            "due_at_ms": due_at_ms,
            "due_at": _datetime_from_ms(due_at_ms).isoformat(),
            "days_since": days_since,
            "days_left": days_left,
            "days_overdue": abs(days_left) if days_left < 0 else 0,
            "overdue": days_left < 0,
        }
        candidates.append(candidate)

    if not candidates:
        return None
    return sorted(candidates, key=lambda item: (item["days_left"], item["due_at_ms"], item["index"]))[0]


def _normalize_device(vacuum_entity: str, item: dict[str, Any]) -> dict[str, Any]:
    device = dict(item)
    device["vacuum_entity"] = vacuum_entity
    if not device.get("name"):
        device["name"] = device.get("device_name") or device.get("label") or vacuum_entity
    return device


def _water_capacity_ml(
    device: dict[str, Any], settings: dict[str, Any]
) -> float | None:
    direct = _optional_number(device.get("water_total_ml"))
    if direct and direct > 0:
        return direct

    custom = settings.get("custom_calibration")
    if isinstance(custom, dict):
        profile_key = device.get("brand_profile") or "default"
        for key in (profile_key, "default"):
            value = custom.get(key)
            if not isinstance(value, dict):
                continue
            tank_ml = _optional_number(value.get("tank_ml"))
            if tank_ml and tank_ml > 0:
                return tank_ml

    # Model database fallback: capacity known from a manually-selected
    # brand/model (config flow) without needing device-registry resolution.
    return _model_tank_ml(device)


def _model_tank_ml(device: dict[str, Any]) -> float | None:
    """Default tank capacity (ml) resolved from the vacuum model database."""
    key = device.get("brand_profile")
    if not (isinstance(key, str) and key in DEFAULT_TANK_ML):
        entity = str(device.get("vacuum_entity") or "").strip().lower()
        key = entity[len("vacuum.") :] if entity.startswith("vacuum.") else ""
    tank_ml = DEFAULT_TANK_ML.get(key)
    return float(tank_ml) if tank_ml and tank_ml > 0 else None


def _number(value: Any, default: float) -> float:
    parsed = _optional_number(value)
    return default if parsed is None else parsed


def _optional_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_number(value: float) -> int | float:
    rounded = round(float(value), 1)
    return int(rounded) if rounded.is_integer() else rounded


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _datetime_from_ms(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
