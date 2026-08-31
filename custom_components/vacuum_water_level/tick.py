"""Server-side water accounting for Vacuum water level.

Two things in here are easy to get wrong and worth reading before editing:

1. **Mop mode/intensity latching.** The select entities that report mop
   mode/intensity are only meaningful *while the vacuum is actively
   cleaning* -- most integrations (verified against the official
   `roborock` core integration) revert them to a default/off value once
   cleaning ends. But the mop-wash dosing event fires *after* cleaning
   ends (the vacuum returns to the dock, then washes the mop) -- by that
   point, re-reading the live entity would get the reverted value, not
   what was actually used during the run that's being washed off. So
   `tick_device()` latches the last valid value seen while actively
   cleaning (`state["latched_mop_mode"]`/`state["latched_mop_intensity"]`)
   and uses that for anything happening after cleaning ends, rather than
   re-reading the (by-then-unreliable) live state.

2. **Adaptive calibration is a heuristic, not real ML.** There's no
   ground-truth water-level sensor to train against -- the only real
   signal available is "the user pressed refill/emptied" (or the dock
   reported it), which tells us the tank was at ~capacity at that moment.
   `_calibrate_model()` uses that single signal per cycle to nudge a
   per-mop-intensity correction factor multiplicatively, with a learning
   rate that decays as more cycles are observed. It cannot disentangle
   multiple simultaneous unknowns from one scalar signal, so the same
   relative correction is applied to every intensity that contributed
   dosing since the last cycle boundary; intensities untouched that cycle
   are left alone. See _calibrate_model()'s docstring for the exact
   update rule.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .sensor_calculations import DEFAULT_TANK_ML, MODEL_DATABASE, slugify
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

# Adaptive calibration (see _calibrate_model). Each refill/empty cycle
# nudges the per-intensity correction factor toward whatever would have
# made the estimate match the tank's real capacity, with a learning rate
# that decays as more cycles are observed.
MIN_CALIBRATION_FRACTION = 0.3  # ignore cycles where too little was used (e.g. a top-off)
CALIBRATION_RATIO_MIN = 0.2  # clamp how much a single cycle's error can imply
CALIBRATION_RATIO_MAX = 5.0
CORRECTION_MIN = 0.3  # clamp the learned correction itself, however many cycles
CORRECTION_MAX = 3.0
LEARNING_RATE_FLOOR = 0.05

# Default dock-error messages, verified against python-roborock's real
# dockErrorStatus enum (ok / water_empty / waste_water_tank_full / ...).
# Fully customizable per vacuum via the config flow's "Set the dock error
# sensor" edit step (device["dock_empty_message"] / ["dock_ok_message"] /
# ["dock_full_message"]), since different brands/integrations use different
# wording -- these are just the fallback when nothing's been customized.
# Matching is done via slugify() (see matches_dock_message), so casing/formatting
# differences ("Water empty" vs "water_empty") don't require an exact-text
# match.
DEFAULT_DOCK_EMPTY_MESSAGE = "Water empty"
DEFAULT_DOCK_OK_MESSAGE = "Ok"
DEFAULT_DOCK_FULL_MESSAGE = "Waste water tank full"

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
    """Auto-detect companion helper entities (status, cleaned area, mop
    mode/intensity, dock error) — and capacity, if the config flow's
    brand/model picker wasn't used — for every manually-added vacuum.

    This only enriches vacuums the user has explicitly added via the config
    flow; it never creates a new entry for a vacuum the user hasn't chosen
    to track. Capacity is resolved from the vacuum's real manufacturer/model
    (via HA's device registry) as a fallback for configs added before a
    brand/model was picked; companion entities are found by locating other
    entities on the SAME device whose name matches a known role (see
    _COMPANION_ROLE_HINTS) — this is what makes server-side accounting
    actually run without the user hunting down and manually pasting in each
    helper entity ID.

    Only fills in missing fields — anything already set (by the config
    flow, or by a previous run of this function) is left untouched. Runs
    every tick; a no-op once everything is resolved. Returns True if
    anything was actually written, so the caller can notify listeners.
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
    for entity_id in set(configured) | set(user_devices):
        # user_devices shadows configured_devices entirely wherever both
        # merges (build_vacuum_devices/_devices_to_tick) are used, so enrich
        # whichever collection actually owns this vacuum.
        bucket = user_devices if entity_id in user_devices else configured
        entry = bucket[entity_id]

        before = dict(entry)
        for role, value in _auto_detect_companions(hass, entity_id).items():
            entry.setdefault(role, value)
        if not entry.get("water_total_ml") and not entry.get("brand_profile"):
            resolved_ml = _resolve_default_tank_ml(hass, entity_id)
            if resolved_ml:
                entry["water_total_ml"] = resolved_ml

        if entry != before:
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
    still set capacity manually via the config flow's brand/model picker."""
    raw = _device_manufacturer_model(hass, vacuum_entity_id)
    if raw is None:
        return None
    slug = slugify(f"{raw[0]} {raw[1]}")
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


def _device_manufacturer_model(
    hass: HomeAssistant, vacuum_entity_id: str
) -> tuple[str, str] | None:
    """Raw (manufacturer, model) strings from the vacuum's HA device
    registry entry, or None if unavailable."""
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
    return (device.manufacturer or "", device.model or "")


def guess_brand_model(
    hass: HomeAssistant, vacuum_entity_id: str
) -> tuple[str, str] | None:
    """Best-effort guess of (brand, model) display names — as they appear
    in MODEL_DATABASE — for pre-filling the config flow's brand/model
    selectors. Same conservative token-subset matching as
    _resolve_default_tank_ml. Returns None if no confident match; the user
    picks manually in that case."""
    raw = _device_manufacturer_model(hass, vacuum_entity_id)
    if raw is None:
        return None
    slug = slugify(f"{raw[0]} {raw[1]}")
    if not slug:
        return None
    slug_tokens = set(slug.split("_"))
    best: tuple[str, str] | None = None
    best_score = 0
    for brand, models in MODEL_DATABASE.items():
        for model_name in models:
            key_tokens = set(slugify(f"{brand} {model_name}").split("_"))
            if key_tokens <= slug_tokens and len(key_tokens) > best_score:
                best, best_score = (brand, model_name), len(key_tokens)
    return best if best and best_score >= 2 else None


def _ensure_model(state: dict[str, Any], key: str) -> dict[str, Any]:
    """Return state[key] as a well-formed prediction-model dict, replacing
    it with a fresh default if missing or malformed (e.g. tank state
    persisted before this feature existed, or manually edited storage)."""
    model = state.get(key)
    if not isinstance(model, dict):
        model = VacuumWaterStorage.default_prediction_model()
        state[key] = model
    if not isinstance(model.get("correction"), dict):
        model["correction"] = {}
    if not isinstance(model.get("pending_ml_by_intensity"), dict):
        model["pending_ml_by_intensity"] = {}
    model.setdefault("cycles_observed", 0)
    model.setdefault("last_calibrated_iso", None)
    return model


def _accumulate_pending(model: dict[str, Any], intensity_key: str, amount: float) -> None:
    """Track how many mL were dosed at each mop intensity since the last
    calibration cycle, so _calibrate_model can distribute its correction
    proportionally to whichever intensities actually contributed."""
    if not amount or amount <= 0:
        return
    pending = model.setdefault("pending_ml_by_intensity", {})
    pending[intensity_key] = round(_number(pending.get(intensity_key), 0) + amount, 4)


def _calibrate_model(model: dict[str, Any], actual_capacity: float | None) -> None:
    """Adjust the model's per-intensity correction factors based on one
    completed refill/empty cycle.

    The only ground-truth signal available is "the tank was at ~capacity
    when this cycle ended" (a refill or an empty event). Comparing that
    against how much the model estimated was used since the previous
    cycle gives a single error ratio for the whole cycle; the same
    relative correction is applied to every mop intensity that
    contributed dosing this cycle (proportionally nudging each one, not
    overwriting), since one scalar signal can't disentangle which
    intensity was "more wrong" than another. Intensities that didn't
    dose anything this cycle are left untouched -- no evidence, no
    update. The learning rate decays as more cycles accumulate
    (1/(2+cycles), floored at LEARNING_RATE_FLOOR), so early cycles
    adapt quickly and the model settles down as it gathers history.

    Cycles where too little was estimated to have been used
    (< MIN_CALIBRATION_FRACTION of capacity -- e.g. a precautionary
    top-off of a tank that wasn't really empty) are skipped, so they
    can't corrupt the model with a wildly wrong error ratio.
    """
    pending = model.get("pending_ml_by_intensity") or {}
    total_pending = sum(
        v for v in pending.values() if isinstance(v, (int, float)) and v > 0
    )

    if (
        not actual_capacity
        or actual_capacity <= 0
        or total_pending < actual_capacity * MIN_CALIBRATION_FRACTION
    ):
        model["pending_ml_by_intensity"] = {}
        return

    cycles = int(model.get("cycles_observed") or 0)
    learning_rate = max(LEARNING_RATE_FLOOR, 1 / (2 + cycles))
    error_ratio = actual_capacity / total_pending
    error_ratio = min(max(error_ratio, CALIBRATION_RATIO_MIN), CALIBRATION_RATIO_MAX)

    correction = model.setdefault("correction", {})
    for intensity, ml in pending.items():
        if not ml or ml <= 0:
            continue
        old = _number(correction.get(intensity), 1.0)
        new = old * (1 + learning_rate * (error_ratio - 1))
        correction[intensity] = round(min(max(new, CORRECTION_MIN), CORRECTION_MAX), 4)

    model["pending_ml_by_intensity"] = {}
    model["cycles_observed"] = cycles + 1
    model["last_calibrated_iso"] = datetime.now(timezone.utc).isoformat()


def _capacity_for_calibration(device: dict[str, Any], key: str) -> float | None:
    """Resolve the tank capacity to calibrate against. Deliberately reads
    only the explicit device field (already resolved by auto-detection or
    the config flow) rather than importing sensor_calculations' capacity
    resolution -- calibration should use a known, concrete capacity or
    not run at all, never a guessed one."""
    value = device.get(key)
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


async def async_tick_water_state(
    hass: HomeAssistant, storage: VacuumWaterStorage
) -> dict[str, dict[str, Any]]:
    """Tick every known vacuum and persist changed states."""
    stored = await storage.async_get_state()
    devices = _devices_to_tick(stored["settings"])
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
    curr_dock_err = _dock_error_value(hass, device)
    curr_door = (
        _state_value(hass, device.get("reset_door_sensor"))
        if device.get("reset_door_sensor")
        else None
    )

    vac_state = vac.state
    is_cleaning = vac_state == "cleaning" or curr_status == "cleaning"

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

    # Latch: the mop mode/intensity select entities are only meaningful
    # while actively cleaning -- most integrations revert them to a
    # default/off value once cleaning ends (verified against the official
    # Roborock integration). The mop-wash dosing event below fires AFTER
    # cleaning ends, so re-reading the live entity at that point would see
    # the reverted value, not what was actually used this run. Remember
    # the last value seen while cleaning (including "off" -- a vacuum-only
    # session must overwrite a stale non-off latch from a previous mop
    # day, so mop_off below correctly reflects THIS session), and use
    # that once cleaning has stopped. Only "unavailable"/missing readings
    # are skipped, since those aren't informative.
    if is_cleaning:
        if mop_mode_raw and slugify(mop_mode_raw) not in {"", "unavailable"}:
            state["latched_mop_mode"] = mop_mode_raw
        if mop_intensity_raw and slugify(mop_intensity_raw) not in {"", "unavailable"}:
            state["latched_mop_intensity"] = mop_intensity_raw

    effective_mop_mode_raw = mop_mode_raw if is_cleaning else state.get("latched_mop_mode")
    effective_mop_intensity_raw = (
        mop_intensity_raw if is_cleaning else state.get("latched_mop_intensity")
    )

    # Normalize before any comparison/lookup: entities report display-style
    # values ("Off", "Medium", "Custom Water Flow") which must match the
    # lowercase_with_underscores keys DEFAULT_USAGE_PER_M2/
    # DEFAULT_INTENSITY_FACTOR/correction dicts use.
    mop_mode_slug = slugify(effective_mop_mode_raw) if effective_mop_mode_raw else ""
    mop_intensity_slug = slugify(effective_mop_intensity_raw) if effective_mop_intensity_raw else ""

    mop_mode = mop_mode_slug if mop_mode_slug and mop_mode_slug not in {"off", "unavailable"} else "standard"
    mop_intensity = (
        mop_intensity_slug
        if mop_intensity_slug and mop_intensity_slug not in {"off", "unavailable"}
        else "medium"
    )
    # Mop intensity's "Off" option is what the person specifically pointed
    # to as indicating "the mop didn't run this session" (vacuum-only
    # days), so either signal being off counts.
    mop_off = mop_mode_slug == "off" or mop_intensity_slug == "off"

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

    # Adaptive calibration: a per-intensity multiplier layered on top of
    # the static formula above, learned from refill/empty cycles (see
    # _calibrate_model). Starts at 1.0x (no correction) for every
    # intensity until enough cycles have been observed to say otherwise.
    water_model = _ensure_model(state, "water_model")
    waste_model = _ensure_model(state, "waste_model")
    water_correction = _mapping_number(water_model["correction"], mop_intensity, 1.0)
    waste_correction = _mapping_number(waste_model["correction"], mop_intensity, 1.0)

    if (
        state.get("last_status") is not None
        and curr_status != state.get("last_status")
        and curr_status in MOP_WASH_STATES
        and not mop_off
    ):
        water_added = wash_volume * water_correction
        waste_added = wash_volume * waste_correction
        state["used_ml"] = round(_number(state.get("used_ml"), 0) + water_added)
        # The water that leaves the clean tank during a mop wash is what
        # fills the dirty/waste tank -- mirror the same volume there,
        # under its own independently-learned correction.
        state["waste_used_ml"] = round(
            _number(state.get("waste_used_ml"), 0) + waste_added
        )
        _accumulate_pending(water_model, mop_intensity, water_added)
        _accumulate_pending(waste_model, mop_intensity, waste_added)
        dirty = True

    last_area = _float_or_none(state.get("last_area"))
    if last_area is not None and curr_area is not None and curr_area > last_area:
        delta = curr_area - last_area
        can_dose = not mop_off and is_cleaning and delta >= AREA_MIN_DELTA
        if can_dose:
            base_added = delta * usage_per_m2 * intensity_factor
            water_added = base_added * water_correction
            waste_added = base_added * waste_correction
            state["used_ml"] = round(_number(state.get("used_ml"), 0) + water_added)
            state["waste_used_ml"] = round(
                _number(state.get("waste_used_ml"), 0) + waste_added
            )
            _accumulate_pending(water_model, mop_intensity, water_added)
            _accumulate_pending(waste_model, mop_intensity, waste_added)
            dirty = True

    empty_message = device.get("dock_empty_message") or DEFAULT_DOCK_EMPTY_MESSAGE
    ok_message = device.get("dock_ok_message") or DEFAULT_DOCK_OK_MESSAGE
    full_message = device.get("dock_full_message") or DEFAULT_DOCK_FULL_MESSAGE

    now_ts = int(datetime.now(timezone.utc).timestamp() * 1000)

    # Clean-tank auto-reset (refill): the dock error must clear
    # specifically to the configured "ok" message, not merely stop being
    # the "empty" message -- an unrelated new error (e.g. a duct blockage
    # reported right after a refill) must not be mistaken for a refill.
    water_cooldown_ok = (
        (now_ts - int(state.get("last_reset_ts") or 0)) / 1000 > RESET_COOLDOWN_SEC
    )
    do_water_reset = False
    if curr_door and state.get("last_door") == "on" and curr_door == "off":
        do_water_reset = True
    if matches_dock_message(state.get("last_dock_err"), empty_message) and matches_dock_message(
        curr_dock_err, ok_message
    ):
        do_water_reset = True

    if do_water_reset and water_cooldown_ok:
        _calibrate_model(
            water_model, _capacity_for_calibration(device, "water_total_ml")
        )
        state["used_ml"] = 0
        state["last_reset_iso"] = datetime.now(timezone.utc).isoformat()
        state["last_reset_ts"] = now_ts
        dirty = True

    # Dirty/waste-tank auto-reset (emptied): the same dock error source
    # clearing from the configured "full" message to "ok" -- mirrors the
    # clean-tank reset above, on its own independent cooldown so refilling
    # the clean tank doesn't also silently reset the waste counter.
    waste_cooldown_ok = (
        (now_ts - int(state.get("last_waste_reset_ts") or 0)) / 1000
        > RESET_COOLDOWN_SEC
    )
    if (
        matches_dock_message(state.get("last_dock_err"), full_message)
        and matches_dock_message(curr_dock_err, ok_message)
        and waste_cooldown_ok
    ):
        waste_capacity = _capacity_for_calibration(
            device, "waste_total_ml"
        ) or _capacity_for_calibration(device, "water_total_ml")
        _calibrate_model(waste_model, waste_capacity)
        state["waste_used_ml"] = 0
        state["last_waste_reset_iso"] = datetime.now(timezone.utc).isoformat()
        state["last_waste_reset_ts"] = now_ts
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
    """Return all vacuum.* entities currently known to HA (used by the
    config flow's "add vacuum" selector, not for automatic tracking)."""
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


def _devices_to_tick(settings: dict[str, Any]) -> list[dict[str, Any]]:
    """Devices the user has explicitly added via the config flow. Auto-
    discovery of every vacuum.* entity was removed — tracking is opt-in per
    device, added through Settings -> Devices & Services -> Vacuum Water
    Monitor -> Configure -> Add vacuum."""
    devices: dict[str, dict[str, Any]] = {}
    for item in settings.get("configured_devices") or []:
        if isinstance(item, dict) and item.get("vacuum_entity"):
            devices[item["vacuum_entity"]] = dict(item)
    for item in settings.get("user_devices") or []:
        if isinstance(item, dict) and item.get("vacuum_entity"):
            devices[item["vacuum_entity"]] = dict(item)
    return list(devices.values())


def _has_user_priv_helpers(
    hass: HomeAssistant, device: dict[str, Any]
) -> bool:
    """Return True if the device config references a user-owned helper that
    already tracks water usage server-side (input_number / input_datetime /
    template sensor created by a DIY automation). In that case the integration
    must defer accounting to the user's existing setup and only display state.
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


def _dock_error_value(hass: HomeAssistant, device: dict[str, Any]) -> str | None:
    """Read the dock error signal. Usually the entity's main .state, but
    some setups expose a docking-station entity whose primary state is
    something else (e.g. "docked") with the actual fault message in an
    attribute instead (device["dock_error_attribute"], set via the config
    flow's "Edit vacuum" step)."""
    entity_id = device.get("dock_error_sensor")
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None:
        return None
    attribute = device.get("dock_error_attribute")
    if attribute:
        value = state.attributes.get(attribute)
        return str(value) if value is not None else None
    return state.state


def matches_dock_message(value: str | None, message: str | None) -> bool:
    """True if `value` (an entity state or attribute value) matches the
    configured trigger phrase `message`, after normalizing both via
    slugify() -- so exact casing/formatting differences (e.g. "Water
    empty" vs "water_empty") don't matter and the user doesn't need to
    type an exact-case match string."""
    if not value or not message:
        return False
    value_slug, message_slug = slugify(value), slugify(message)
    return bool(value_slug) and value_slug == message_slug


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
