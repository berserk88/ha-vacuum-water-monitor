# Vacuum Water Monitor

![Preview](banner.png)

Track how much water is left in your robot vacuum's mop water tank — and get refill
reminders — without any extra hardware. The integration estimates water usage from what
your vacuum already reports to Home Assistant (state changes and cleaned area) and exposes
it as sensors, a low-water binary sensor, and a Refilled button. No Lovelace card required —
use the entities in any dashboard, automation, or notification the normal Home Assistant way.

[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2024.12+-blue.svg?logo=homeassistant)](https://www.home-assistant.io/) [![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

## How it works

1. **Add a vacuum from Settings → Devices & services → Vacuum Water Monitor → Configure →
   Add a vacuum.** Pick the `vacuum.*` entity, then pick its brand and model from a
   database of common robot vacuums (or enter tank capacity manually if yours isn't
   listed). If your vacuum's manufacturer/model is recognized from its Home Assistant
   device info, the brand/model are pre-selected for you — just confirm.
2. **Companion sensors are auto-detected.** For each vacuum you add, the integration looks
   for other entities on the same Home Assistant device — cleaned-area, status, mop
   mode/intensity, dock error — and wires them in automatically. This has been verified
   against the entities the official `roborock` core integration creates; other
   integrations that expose the same kind of companion entities on the vacuum's device
   should also be detected, but this hasn't been verified against every brand.
3. **Water accounting runs server-side, every 60 seconds.** Two signals add water usage:
   - **Mop-wash events** — when your vacuum reports a mop-washing state (e.g. Roborock's
     `washing_the_mop`), a fixed wash volume is added (default 150 mL).
   - **Cleaned area** — while the vacuum is cleaning with the mop enabled, usage is added
     per m² of newly cleaned area (rate depends on mop mode and intensity, when those
     companion entities were found).
4. **Refills.** Press the **Refilled** button entity after filling the tank. Optionally the
   counter can also auto-reset when the auto-detected dock-error sensor's `water_empty`
   error clears.
5. **Everything is stored by Home Assistant** (Store, included in backups) — counters
   survive restarts.

> **Estimates, not measurements.** Robot vacuums don't report actual water level, so the
> numbers are calculated estimates from wash cycles and cleaned area, not a direct sensor
> reading.

## Installation

1. Open HACS → Custom repositories.
2. Add your fork's URL as category **Integration**.
3. Install **Vacuum Water Monitor**.
4. Restart Home Assistant.
5. Go to Settings → Devices & services → Add integration, then search for
   **Vacuum Water Monitor**. This step only sets the default warning/critical thresholds.
6. Open the integration's **Configure** button → **Add a vacuum to track** → follow the
   brand/model picker. Repeat for each vacuum.

## Entities

Each tracked vacuum gets its own device with these entities:

| Entity | Platform | Unit | Meaning |
|---|---|---:|---|
| Water remaining | sensor | `%` | Estimated water left in the tank |
| Water used since refill | sensor | `mL` | Usage accumulated since the last refill |
| Last refill | sensor (diagnostic) | timestamp | When you last pressed Refilled (or auto-reset fired) |
| Water low | binary_sensor | — | On when remaining water is at or below the warning/critical threshold (`severity` attribute distinguishes the two) |
| Refilled | button | — | Press after filling the tank to reset the counter |

Use them like any other entity — dashboards, template sensors, and automations.

**Low-water phone notification**, using the binary sensor so it fires exactly once per
threshold crossing rather than needing a `numeric_state` trigger:

```yaml
alias: Vacuum water low
trigger:
  - platform: state
    entity_id: binary_sensor.roborock_s8_maxv_ultra_water_low
    to: "on"
action:
  - service: notify.mobile_app_phone
    data:
      title: Vacuum water low
      message: >-
        {{ state_attr(trigger.entity_id, 'severity') }} — 
        {{ state_attr(trigger.entity_id, 'remaining_percent') }}% water remaining.
mode: single
```

**Refill reminder button on a dashboard** — just add the button entity to any card
(entities card, tile card, etc.) like you would any other Home Assistant button.

## Managing tracked vacuums

Open the integration's **Configure** menu (Settings → Devices & services → Vacuum Water
Monitor → Configure) at any time to:

- **Add a vacuum to track** — entity picker, then brand/model (or custom capacity).
- **Stop tracking a vacuum** — removes its entities. Tank history is discarded.
- **Edit warning thresholds** — the low/critical percentages used by the Water low binary
  sensor.

## FAQ

**Do I have to configure anything besides adding my vacuum?**
No. Companion sensors (cleaned area, status, mop mode/intensity, dock error) are
auto-detected — there's no manual entity wiring.

**Why is "Water remaining" unknown?**
Either the model wasn't matched from your vacuum's device info and you picked a custom
capacity but it wasn't set correctly, or accounting hasn't accumulated any usage yet. Check
Settings → Devices & services → your vacuum's device page for its reported manufacturer/model,
and feel free to open an issue with that info plus your tank size so the database can be
extended.

**Why doesn't the water percentage change even though I've been cleaning?**
This means companion-sensor auto-detection didn't find a usable status or cleaned-area
entity on your vacuum's device — most likely because your integration doesn't expose the
same kind of companion entities the official Roborock integration does. Check your vacuum's
device page in Home Assistant for entities like "Cleaning area" or "Status"; if none exist,
server-side accounting has no signal to work from for your setup.

**I see two devices but I only have one vacuum.**
Your robot is likely exposed by two integrations at once (e.g. the vendor integration and
Matter — each creates its own `vacuum.*` entity). Add whichever one you actually want
tracked.

**How accurate is it?**
It's an estimate based on wash cycles and cleaned area, not a direct water-level reading.

## Upgrading from a card-based v5.1.x install

v5.1.13 and earlier bundled a Lovelace card and used the card's Settings tab for
configuration. This version removes the card entirely in favor of the config flow above.

1. Remove the old Lovelace card from any dashboards (it will error since the resource is
   gone).
2. Update the integration via HACS and restart Home Assistant.
3. Vacuums that were already configured (via the old card) keep their tank history and
   settings — nothing needs to be re-added. Vacuums that only ever relied on the old
   zero-config auto-discovery (never explicitly configured) will need to be added once via
   **Configure → Add a vacuum to track**, since tracking is now opt-in per vacuum.

## Privacy

- No telemetry, analytics, or tracking.
- No CDN-hosted assets.
- Tank state is stored locally by Home Assistant in its normal storage area and is included
  in Home Assistant backups.

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

## License

MIT, see [LICENSE](LICENSE).
