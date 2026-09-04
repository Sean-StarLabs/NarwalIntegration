# Narwal Dock Task Design

This document records the intended Home Assistant contract for Narwal dock
tasks. It is a design note, not protocol proof. Verified field mappings belong
in `docs/PROTOCOL.md` once they have been confirmed on hardware.

## Goals

- Model the robot and dock as separate Home Assistant devices.
- Expose dock work as five stateful task controls.
- Keep task identity, availability, progress, and safety rules inside the
  integration.
- Keep Lovelace simple: render entities and their attributes, without deriving
  Narwal state from raw protocol fields.
- Avoid extra progress, duration, or "current dock task" entities.

Trails and map path rendering are deliberately out of scope for this design.

## Devices

| Device | Identifier | Owns |
|---|---|---|
| Robot | `(narwal, <device_id>)` | Vacuum, camera/map, robot clean metrics, room cleaning, robot diagnostics |
| Dock | `(narwal, <device_id>_dock)` | Dock task switches, dock light, dock/tank/consumable entities |

The dock should use `via_device=(narwal, <device_id>)` so Home Assistant shows
it as the robot's base station rather than an unrelated device.

## Dock Task Entities

Expose exactly five dock task entities:

| Task | Domain | Stable unique ID suffix | Command | Active state |
|---|---|---|---|---|
| Empty dustbin | `switch` | `_empty_dustbin` | `empty_dustbin()` | `emptying_dustbin` |
| Wash mop | `switch` | `_wash_mop` | `wash_mop_by_robot_status()` | `washing_mop` |
| Dry mop | `switch` | `_dry_mop` | `dry_mop()` | `drying_mop` |
| Dry dust bin | `switch` | `_dry_dust_bin` | `dry_dust_bag()` | `dry_dust_bin` |
| Dry dock bag | `switch` | `_dry_dock_bag` | `dry_station_bag()` | `dry_dock_bag` |

The existing unique IDs should be preserved unless an explicit entity registry
migration is added.

Each switch owns:

- `is_on`: true when that exact dock task is active.
- `available`: true only when turning the switch on or off is currently safe.
- `progress`: integer `0..100`, only when a typed timer provides enough data.
- `time_left`: formatted remaining time, for example `10m` or `2h 30m`, only
  when a trustworthy timer exists.

Do not expose separate dock progress sensors such as `progress`,
`progress_percent`, `progress_display`, `station_task`, or
`dry_mop_remaining_time`. Robot cleaning progress can remain on the vacuum
entity or a robot-side sensor; dock task progress belongs to the active dock
task switch.

## Availability Rules

The integration should have one authoritative dock-task state layer. Raw
`station_activity`, `dock_activity`, timer fields, command assumptions, and
polling-only fallbacks should be normalized there before any entity decides
availability.

Rules:

- If Home Assistant has no fresh coordinator state, connected dock task
  switches remain actionable so a command can wake the robot, refresh dock
  state, and revalidate against authoritative data before dispatch.
- If the robot reports a blocking error, all dock tasks are unavailable.
- If the robot is off the dock, all dock task starts are unavailable.
- If the robot is actively cleaning, all dock task starts are unavailable.
- If the dock is idle and the robot is docked, all five task switches are off
  and available.
- If one known task is active, the matching switch is on. It is available only
  when stopping that exact task is safe.
- If station work is active but unmapped, all starts are unavailable and no fake
  sixth task entity is created.
- Active timer fields that are not mapped to one of the five dock tasks are
  treated as unmapped station work until hardware testing identifies them.
- If multiple tasks are active, untyped generic stop must not be exposed as a
  task-specific stop.
- Typed timers beat stale coarse activity fields.
- A bounded local assumption is acceptable only as a polling-only fallback after
  a command was accepted, and must clear on authoritative idle state, explicit
  stop, terminal evidence, or expiry.

Current verified policy should be conservative:

- Do not allow dock-to-dock parallel starts unless hardware testing proves the
  exact combination.
- Allow robot start during `dry_dock_bag` only when there is typed live evidence
  that the active task is dock-bag drying.
- Block robot starts for emptying, washing, mop drying, dust-bin drying, and
  unmapped station activity.
- Keep robot `stop` available during a dock-side phase that belongs to an
  active clean.
- Do not infer charge-to-resume from dock maintenance.

## Integration Boundary

The integration owns:

- Protocol field decoding.
- Raw-to-normalized dock task mapping.
- Task concurrency and conflict rules.
- Wake, refresh, and selected-task revalidation before commands.
- Progress and time-left calculation.
- Polling-only fallback assumptions.
- Robot/dock interaction rules.

Lovelace owns only presentation:

- Show the five switch entities.
- Use switch state for active/inactive display.
- Use `available` for disabled controls.
- Show `progress` and `time_left` attributes if present.
- Use normal problem/consumable entities for alerts.

Lovelace should not inspect `station_activity`, `dock_activity`,
`task_status`, timer field numbers, or raw Narwal task names.

## PR Scope

A clean upstream PR should include only the dock task model:

- Dock device info.
- Five task switches.
- One normalized dock-task state path.
- Safe start/stop gating.
- Typed timer attributes on the task switches.
- Removal or registry cleanup for older dock action buttons and obsolete dock
  progress/task sensors if they exist on that branch.
- Tests for the entity contract and safety matrix.

Keep these out of the dock PR:

- Room-specific cleaning settings.
- Vacuum supported-feature cleanup.
- Cloud accessory consumables.
- Tank-state semantic fixes.
- Trail persistence or native trajectory rendering.
