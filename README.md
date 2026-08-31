# Vacuum water level

![Preview](banner.png)

Track how much water is left in your robot vacuum's mop water tank — and how full the dirty
water tank is getting — and get refill/empty reminders, without any extra hardware. The
integration estimates both from what your vacuum already reports to Home Assistant (state
changes and cleaned area) and exposes them as sensors, low-water/tank-full binary sensors,
and Refilled/Emptied buttons. No Lovelace card required — use the entities in any dashboard,
automation, or notification the normal Home Assistant way.

[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2024.12+-blue.svg?logo=homeassistant)](https://www.home-assistant.io/) [![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

> Domain: `vacuum_water_level`. Renamed from the original `ha_vacuum_water_monitor` project
> this was forked from, so it can be installed without conflicting with anyone else's copy
> of that integration. See [Upgrading](#upgrading) if you're coming from an earlier version
> of this fork.

## How it works

1. **Add a vacuum from Settings → Devices & services → Vacuum water level → Configure →
   Add a vacuum to track.** Pick the `vacuum.*` entity, then pick its brand and model from a
   database of common robot vacuums (or enter tank capacity manually if yours isn't
   listed). If your vacuum's manufacturer/model is recognized from its Home Assistant
   device info, the brand/model are pre-selected for you — just confirm.
2. **Companion sensors are auto-detected.** For each vacuum you add, the integration looks
   for other entities on the same Home Assistant device — cleaned-area, status, mop
   mode/intensity, dock error — and wires them in automatically. This has been verified
   against the entities the official `roborock` core integration creates; other
   integrations that expose the same kind of companion entities on the vacuum's device
   should also be detected, but this hasn't been verified against every brand. If
   auto-detection picks the wrong entity (or none) for the dock error sensor, you can assign
   it manually — see [Managing tracked vacuums](#managing-tracked-vacuums).
3. **Water accounting runs server-side, every 60 seconds.** Two signals add usage to *both*
   the clean tank's used counter and the dirty/waste tank's collected counter — the water
   that leaves the clean tank during cleaning is what fills the dirty one, so both are
   estimated from the same activity:
   - **Mop-wash events** — when your vacuum reports a mop-washing state (e.g. Roborock's
     `washing_the_mop`), a fixed wash volume is added (default 150 mL).
   - **Cleaned area** — while the vacuum is cleaning with the mop enabled, usage is added
     per m² of newly cleaned area (rate depends on mop mode and intensity, when those
     companion entities were found).
4. **The estimate adapts over time.** Every refill/empty is also a calibration checkpoint:
   the integration compares the tank's known capacity against how much it estimated was
   used since the last cycle, and nudges a per-mop-intensity correction factor toward
   whatever would have made the estimate accurate — more per cycle early on, smaller/more
   stable adjustments as it gathers history. If you mop on some days and vacuum-only on
   others, only the mopping days feed the water/waste models (vacuum-only days correctly
   contribute nothing, since no water is used) — the models simply take longer to build up
   history the less often you mop. This is an adaptive-calibration heuristic, not real
   machine learning: there's no water-level sensor to train against, only the refill/empty
   event itself, which is one signal per cycle. Press **Clear prediction model** to discard
   what's been learned and start over from the defaults.
5. **Refills and emptying.** Press the **Refilled** button after filling the clean tank, or
   the **Waste tank emptied** button after emptying the dirty one. Both can also auto-reset:
   when the dock error source reports the clean tank is empty and then clears, that's treated
   as a refill; when it reports the waste tank is full and then clears, that's treated as
   emptying. The exact messages your vacuum uses for "empty" / "ok" / "full" are fully
   customizable per vacuum (see [Managing tracked vacuums](#managing-tracked-vacuums)) since
   different brands/integrations phrase them differently.
6. **Everything is stored by Home Assistant** (Store, included in backups) — counters and
   the learned calibration survive restarts.

> **Estimates, not measurements.** Robot vacuums don't report actual water level, so the
> numbers are calculated estimates from wash cycles and cleaned area, not a direct sensor
> reading. The waste tank estimate additionally assumes water consumed ≈ water collected,
> which is a simplification, not a physical measurement.

## Installation

1. Open HACS → Custom repositories.
2. Add your fork's URL as category **Integration**.
3. Install **Vacuum water level**.
4. Restart Home Assistant.
5. Go to Settings → Devices & services → Add integration, then search for
   **Vacuum water level**. This step only sets the default warning/critical thresholds.
6. Open the integration's **Configure** button → **Add a vacuum to track** → follow the
   brand/model picker. Repeat for each vacuum.

## Entities

Each tracked vacuum gets its own device with these entities:

| Entity | Platform | Unit | Meaning |
|---|---|---:|---|
| Water remaining | sensor | `%` | Estimated water left in the clean tank |
| Water used since refill | sensor | `mL` | Clean-tank usage accumulated since the last refill |
| Last refill | sensor (diagnostic) | timestamp | When you last pressed Refilled (or auto-reset fired) |
| Water low | binary_sensor | — | On when remaining water is at or below the warning/critical threshold (`severity` attribute distinguishes the two) |
| Refilled | button | — | Press after filling the clean tank to reset its counter |
| Waste tank level | sensor | `%` | Estimated fill level of the dirty/waste tank |
| Waste water collected | sensor | `mL` | Waste collected since the tank was last emptied |
| Last emptied | sensor (diagnostic) | timestamp | When you last pressed Waste tank emptied (or auto-reset fired) |
| Waste tank full | binary_sensor | — | On at/above ~90% estimated fill, or immediately if the dock reports it directly |
| Waste tank emptied | button | — | Press after emptying the dirty tank to reset its counter |
| Clear prediction model | button | — | Resets the learned per-intensity correction factors (water and waste) back to their defaults — doesn't affect current tank levels |

`Water used since refill` and `Waste water collected` also expose `prediction_cycles_observed`,
`prediction_correction_factors`, and `prediction_last_calibrated` as attributes, so you can see
what the model has learned instead of it being a black box.

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

**Waste tank full notification**, same pattern:

```yaml
alias: Vacuum waste tank full
trigger:
  - platform: state
    entity_id: binary_sensor.roborock_s8_maxv_ultra_waste_tank_full
    to: "on"
action:
  - service: notify.mobile_app_phone
    data:
      title: Empty the vacuum's waste tank
      message: >-
        {{ state_attr(trigger.entity_id, 'full_percent') }}% full.
mode: single
```

**Refill/empty reminder buttons on a dashboard** — just add the button entities to any card
(entities card, tile card, etc.) like you would any other Home Assistant button.

## Managing tracked vacuums

Open the integration's **Configure** menu (Settings → Devices & services → Vacuum water
level → Configure) at any time to:

- **Add a vacuum to track** — entity picker, then brand/model (or custom capacity).
- **Edit a tracked vacuum** — pick a vacuum, then either:
  - **Change brand / model / capacity** — re-runs the same picker, pre-filled with the
    vacuum's current brand/model where known, and overwrites its capacity in place. Nothing
    else about the vacuum (tank history, auto-detected companion entities) is touched.
  - **Set the dock error sensor** — manually assign the entity (and, if needed, a specific
    attribute on it) that reports the dock's errors, overriding auto-detection, plus
    customize everything about how those errors are interpreted:
    - The entity and (optionally) the attribute to read from it. Some setups expose the
      error on an attribute rather than the entity's main state — for example, a
      docking-station entity whose state is `docked` but whose `error` (or similarly-named)
      attribute reads `Water empty` when the tank runs dry. Point the picker at that entity
      and put the attribute's name in the second field.
    - The three trigger messages: the "clean tank empty" message, the "no error / cleared"
      message, and the "waste tank full" message. These default to the wording the official
      Roborock integration uses (`Water empty`, `Ok`, `Waste water tank full`), but can be
      set to whatever your vacuum/integration actually reports — matching ignores exact
      casing/formatting (`Water empty` and `water_empty` are treated the same), so you don't
      need to match punctuation exactly, just the words.
    - The waste tank's capacity, if it's different from the clean tank's (defaults to the
      same capacity as the clean tank if left blank).
    Leaving the entity field blank clears the whole override and falls back to
    auto-detection.
  - **Set the mop mode / intensity entities** — manually assign which entities report mop
    mode and mop intensity, overriding auto-detection. The intensity entity's state (e.g.
    `Off`/`Low`/`Medium`/`High` — this is what `select.roborock_qrevo_maxv_mop_intensity`
    and similar entities report) is used both for area-based dosing rates and to train the
    adaptive calibration model per intensity level, so pointing this at the right entity
    matters if auto-detection picked the wrong one or found nothing. The two fields are
    independent — leaving one blank falls back to auto-detection for just that entity.
- **Stop tracking a vacuum** — removes its entities. Tank history is discarded.
- **Edit warning thresholds** — the low/critical percentages used by the Water low binary
  sensor.

## FAQ

**Do I have to configure anything besides adding my vacuum?**
No. Companion sensors (cleaned area, status, mop mode/intensity, dock error) are
auto-detected — there's no manual entity wiring required. You can still override the dock
error source manually (see above) if auto-detection doesn't find the right entity for your
setup.

**Why is "Water remaining" unknown?**
Either the model wasn't matched from your vacuum's device info and you picked a custom
capacity but it wasn't set correctly, or accounting hasn't accumulated any usage yet. Check
Settings → Devices & services → your vacuum's device page for its reported manufacturer/model,
and feel free to open an issue with that info plus your tank size so the database can be
extended. You can also fix this directly via **Edit a tracked vacuum → Change brand / model /
capacity** without removing and re-adding the vacuum.

**Why doesn't the water percentage change even though I've been cleaning?**
This means companion-sensor auto-detection didn't find a usable status or cleaned-area
entity on your vacuum's device — most likely because your integration doesn't expose the
same kind of companion entities the official Roborock integration does. Check your vacuum's
device page in Home Assistant for entities like "Cleaning area" or "Status"; if none exist,
server-side accounting has no signal to work from for your setup. You can also assign the
mop mode/intensity entities manually — see **Edit a tracked vacuum → Set the mop mode /
intensity entities** above.

**How does the prediction model actually learn — is this real machine learning?**
No, and it's worth being precise about this: there's no water-level sensor to train
against, so the model can't do real regression. The only real signal is "the tank was at
~capacity when you pressed refill/emptied (or the dock reported it)" — one number per
cycle. `_calibrate_model` uses that to nudge a per-mop-intensity correction factor toward
whatever would have made the estimate match, with a learning rate that shrinks as more
cycles accumulate. It's an adaptive calibration heuristic that converges over repeated
cycles, not a trained model in the machine-learning sense.

**I mop some days and vacuum-only on others — does that break the model?**
No. Vacuum-only sessions correctly add nothing to either model (no water used, no signal to
learn from) — they just mean the model builds up history more slowly, proportional to how
often you actually mop. Check `prediction_cycles_observed` (an attribute on the Water used
since refill / Waste water collected sensors) to see how many refill/empty cycles have
contributed so far.

**My dock reports errors as an attribute, not its main state.**
Use **Edit a tracked vacuum → Set the dock error sensor** and fill in both the entity and
the attribute name. See [Managing tracked vacuums](#managing-tracked-vacuums) above.

**My vacuum uses different wording than "Water empty" / "Ok" / "Waste water tank full".**
The three trigger messages are fully customizable per vacuum — same place, **Edit a tracked
vacuum → Set the dock error sensor**. Type in whatever your vacuum/integration actually
reports; exact casing and punctuation don't matter, just the words.

**Why is "Waste tank level" unknown?**
Same cause as "Water remaining" being unknown — no capacity resolved (it falls back to the
clean tank's capacity, so if that's unknown, so is this). Fix it via **Edit a tracked vacuum
→ Set the dock error sensor** (to set a waste tank capacity explicitly) or **Change brand /
model / capacity** (to fix the underlying clean-tank capacity it falls back to).

**Is the waste tank estimate as accurate as the clean tank one?**
It's a rougher approximation — it assumes the water collected in the waste tank roughly
equals the water used from the clean tank, which is a simplification of how these vacuums
actually work, not a direct measurement of either tank.

**I see two devices but I only have one vacuum.**
Your robot is likely exposed by two integrations at once (e.g. the vendor integration and
Matter — each creates its own `vacuum.*` entity). Add whichever one you actually want
tracked.

**How accurate is it?**
It's an estimate based on wash cycles and cleaned area, not a direct water-level reading.

## Upgrading

**From any earlier version of this fork** (including the card-based v5.1.x releases and the
initial card-free v5.2.0): this version renamed the integration's domain from
`ha_vacuum_water_monitor` to `vacuum_water_level`, so it no longer conflicts with anyone
else's install of the original project. Each domain has its own separate storage file, so
without help your tracked vacuums and tank history would silently disappear after updating —
the integration copies that data forward automatically, once, the first time it starts up
under the new domain. It never overwrites data that already exists under the new domain, so
this is safe to run repeatedly (e.g. across restarts) and is a no-op if there's nothing to
migrate.

If you're specifically coming from a **card-based v5.1.x install**:

1. Remove the old Lovelace card from any dashboards (it will error since the resource is
   gone — the card was removed in v5.2.0).
2. Update the integration via HACS and restart Home Assistant.
3. Vacuums that were already configured (via the old card) keep their tank history and
   settings — nothing needs to be re-added. Vacuums that only ever relied on the old
   zero-config auto-discovery (never explicitly configured) will need to be added once via
   **Configure → Add a vacuum to track**, since tracking is opt-in per vacuum.

Because the domain changed, Home Assistant treats this as a different integration from any
previous install — you may see a duplicate "Add integration" entry the first time; that's
expected, and the migration above ensures your data still comes along.

## Privacy

- No telemetry, analytics, or tracking.
- No CDN-hosted assets.
- Tank state is stored locally by Home Assistant in its normal storage area and is included
  in Home Assistant backups.

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

## License

MIT, see [LICENSE](LICENSE).
