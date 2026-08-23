"""Constants for Vacuum Water Monitor."""

from __future__ import annotations

DOMAIN = "ha_vacuum_water_monitor"
VERSION = "5.2.0"  # informational only; keep in sync with manifest.json
MANUFACTURER = "HA Tools"
MODEL = "Vacuum Water Monitor"

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
