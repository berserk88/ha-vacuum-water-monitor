"""Constants for Vacuum water level."""

from __future__ import annotations

DOMAIN = "vacuum_water_level"
# Domain this integration used before being renamed (to avoid colliding with
# other users' installs of the original ha_vacuum_water_monitor project).
# Used once, at setup, to migrate any existing storage/config data forward.
LEGACY_DOMAIN = "ha_vacuum_water_monitor"
VERSION = "5.5.1"  # informational only; keep in sync with manifest.json
MANUFACTURER = "HA Tools"
MODEL = "Vacuum water level"

EVENT_STATE_CHANGED = f"{DOMAIN}_state_changed"

CONF_WARNING_THRESHOLD = "warning_threshold"
CONF_CRITICAL_THRESHOLD = "critical_threshold"

DEFAULT_WARNING_THRESHOLD = 20
DEFAULT_CRITICAL_THRESHOLD = 10
DEFAULT_TICK_INTERVAL_SECONDS = 60

DATA_STORAGE = "storage"
DATA_TICK_UNSUB = "tick_unsub"
DATA_TICK_TASK = "tick_task"

STORAGE_KEY = DOMAIN
STORAGE_VERSION = 1


def signal_vacuum_water_updated(entry_id: str) -> str:
    """Return the dispatcher signal for Store-backed sensor refreshes."""
    return f"{DOMAIN}_{entry_id}_updated"
