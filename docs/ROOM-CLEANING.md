# Room cleaning setup

Required before `vacuum.clean_area` works. Needs Home Assistant 2026.3 or newer.

Home Assistant's room cleaning targets **HA areas**, not the robot's own rooms, so there is a
one-time mapping step. Without it the service fails with
*"Area mapping is not configured for vacuum.&lt;entity&gt;"*.

1. Create a Home Assistant **area** for each room you want to clean (Settings → Areas & Zones), if you don't already have one.
2. Open the **mapping editor** and match each robot segment to its area (see below).
3. `vacuum.clean_area` can then target those areas, and the robot cleans the matching rooms.

## Where the mapping editor lives

It hangs off the **entity**, not the integration. There is **no such option on the integration
page or the device page**; that is the most common place people look and it is not there.

Any of these three routes opens the same editor:

| Route | Where |
|---|---|
| **Entity settings** (most reliable) | Settings → Devices & Services → **Entities** tab → search your vacuum → open it → **cog icon** → *Map vacuum segments to areas* |
| **First-run prompt** | Open the vacuum → **Clean areas** → **Configure** (only shown while no mapping exists, and only to admins) |
| **Header action** | Open the vacuum → **Clean areas** → header action (use this one once a mapping already exists) |

Home Assistant shows the option when the entity's domain is `vacuum` and it advertises the
`CLEAN_AREA` feature. This integration sets that flag, so if the row is missing:

- **Hard-refresh the browser** (<kbd>Ctrl</kbd>+<kbd>Shift</kbd>+<kbd>R</kbd>). The frontend caches aggressively.
- **Check the entity is not `unavailable`.** The row is gated on a live state object, so it will not render while the robot is unreachable.
- Confirm you are on **Home Assistant 2026.3+**, when area mapping was introduced.

Room names come from the robot's map. Rooms you named in the Narwal app keep those names, and
the rest use the shared room-type table corrected in
[#48](https://github.com/sjmotew/NarwalIntegration/pull/48). **Name your rooms in the Narwal
app before mapping**: the mapping keys on segment *id*, so renaming later is safe, but naming
first means your HA areas, the robot's map and the vacuum entity's current-room attribute all
read the same words.

`cleaning_area_id` accepts an **ordered list**, so you can clean several rooms in one job and
the robot follows the order you picked.

> **Remapping resets this.** A fresh full-house map in the Narwal app renumbers segments, which
> invalidates the mapping. The integration detects the change and raises a Home Assistant
> repair issue so you know to redo it.

## Per-room profiles and the dashboard

v1.0.8 added per-room cleaning profiles: six selects and a switch **per room**, 168 entities
on a 24-room map, which is right for automations and wrong for a dashboard. Room profile
controls are disabled by default; selection switches stay enabled but hidden so a saved
selection can never become an unreachable state.

Enable the room profile entities you actually use, then run
[`tools/gen_dashboard.py`](../tools/gen_dashboard.py) against your Home Assistant entity
registry. It emits a **room-picker section**: one dropdown, and only the chosen room's controls
on screen, plus a Whole-house panel for the defaults, Start / Clear-selection buttons, a
`script` that cleans exactly one room with its own profile, and a Dock-tasks section. Paste the
YAML into any sections-view dashboard. Needs the `state-switch` card from HACS.

## Example: scheduled multi-room clean with settings

Clean settings are read **at the moment the job is dispatched**, so an automation sets the
entities first and calls `vacuum.clean_area` last. Requested on
[#83](https://github.com/sjmotew/NarwalIntegration/issues/83).

```yaml
alias: Narwal — weekday morning clean
description: Kitchen then hallway, vacuum + mop, two passes
triggers:
  - trigger: time
    at: "09:30:00"
conditions:
  - condition: time
    weekday: [mon, tue, wed, thu, fri]
  # Don't start if the robot is already busy or has a fault
  - condition: state
    entity_id: vacuum.narwal_flow_vacuum
    state: docked
actions:
  # 1. Settings first — these are read when the job is dispatched
  - action: select.select_option
    target:
      entity_id: select.narwal_flow_clean_mode
    data:
      option: Vacuum and mop

  - action: select.select_option
    target:
      entity_id: select.narwal_flow_mopping_humidity
    data:
      option: Slightly wet

  - action: select.select_option
    target:
      entity_id: select.narwal_flow_mop_strength
    data:
      option: High

  - action: number.set_value
    target:
      entity_id: number.narwal_flow_cleaning_passes
    data:
      value: 2

  - action: vacuum.set_fan_speed
    target:
      entity_id: vacuum.narwal_flow_vacuum
    data:
      fan_speed: Strong

  # 2. Then dispatch the rooms, in the order you want them cleaned
  - action: vacuum.clean_area
    target:
      entity_id: vacuum.narwal_flow_vacuum
    data:
      cleaning_area_id:
        - kitchen
        - hallway
mode: single
```

Notes:

- **Substitute your own entity ids.** They are derived from your device name, so yours will
  differ; check Settings → Devices & Services → Entities.
- `cleaning_area_id` takes **HA area ids**, not robot room names, and the segment-to-area
  mapping above must exist first.
- The clean-settings entities apply to the *next* job. Changing them mid-clean does not alter
  the job in progress.
- `select.select_option` takes the **displayed** option (`Vacuum and mop`), not the underlying
  key (`vacuum_and_mop`).
- Fan speed is a separate call because it lives on the vacuum entity rather than in the
  clean-settings block.
- Omit the settings steps you don't care about; whatever you leave alone keeps its current
  value.
