# Narwal Dock Task Test Plan

This plan is for discovering which dock commands can run, stop, and conflict on
real hardware. It should be run before loosening the integration's conservative
availability matrix.

Classify every result as:

- `verified`: command result and follow-up telemetry agree, and the result is
  reproduced.
- `inferred`: behavior depends on coarse station activity, a local assumption,
  stale timer data, or a one-off accept/reject result.
- `unknown`: no safe conclusion.

## Setup

Use a test Home Assistant instance first. Repeat ambiguous cases with a direct
client script after disabling the HA integration, because Narwal robots allow
one local connection per client IP.

Enable debug logging for:

```yaml
logger:
  logs:
    custom_components.narwal: debug
```

For every command attempt, capture state at `T0`, `T+2s`, `T+10s`, and `T+30s`.

Record:

- Service or client call.
- Command response code and mapped name.
- Whether the Narwal app shows the same task/state.
- All five dock switch states and attributes.
- Vacuum state and attributes, including task summary and supported commands.
- Raw state fields: `working_status`, `station_activity`, `dock_activity`,
  `dock_sub_state`, `dock_field11`, `dock_field47`, dock presence and off-dock
  indicators, active dock timers, assumed dock task, mop drying remaining time,
  and dock timer freshness.

Preserve log lines about wake, status refresh, command result codes, unmapped
fields, and post-stop task refresh.

## Controls

Home Assistant services:

- `switch.turn_on` and `switch.turn_off` for each dock task switch.
- `vacuum.start`
- `vacuum.pause`
- `vacuum.stop`
- `vacuum.return_to_base`
- `vacuum.locate`
- `narwal.clean_rooms`

Direct client calls:

- `client.empty_dustbin()`
- `client.wash_mop()`
- `client.wash_mop_by_robot_status()`
- `client.dry_mop()`
- `client.dry_dust_bag()`
- `client.dry_station_bag()`
- `client.stop_dock_task(raw_task)`
- `client.get_status(full_update=True)`
- auxiliary mop timer query, if the branch under test exposes one

## Baseline Matrix

| State | Start attempts | Stop attempts | Purpose |
|---|---|---|---|
| Docked idle, awake | all five dock tasks | after each successful start | Baseline task support |
| Docked idle, asleep | all five dock tasks | after each successful start | Wake, refresh, revalidation |
| Off-dock standby | all five dock tasks | none | Unsafe start rejection |
| Active robot clean, off dock | all five dock tasks | none | Robot-session gating |
| Returning to dock | all five dock tasks | none | Transition conflict gating |
| Charging to resume | all five dock tasks | active task if present | Separate recharge from dock maintenance |
| Dock-side phase during clean | none | robot stop, matching dock stop | Keep robot cancellation available |
| Unmapped active station task | all five dock tasks | active task if known | Block unsafe starts |
| `dry_dock_bag` active | room clean, all other dock tasks | `dry_dock_bag` | Validate robot-compatible dock task |
| Robot leaves while `dry_dock_bag` active | no dock starts | `dry_dock_bag` | Preserve dock-owned task after departure |

## Conflict Matrix

For each active dock task, try every other dock task:

| Active task | Start `empty_dustbin` | Start `wash_mop` | Start `dry_mop` | Start `dry_dust_bin` | Start `dry_dock_bag` |
|---|---|---|---|---|---|
| `empty_dustbin` | n/a | record | record | record | record |
| `wash_mop` | record | n/a | record | record | record |
| `dry_mop` | record | record | n/a | record | record |
| `dry_dust_bin` | record | record | record | n/a | record |
| `dry_dock_bag` | record | record | record | record | n/a |

For each active dock task, also try:

- `vacuum.start`
- `narwal.clean_rooms`
- `vacuum.pause`
- `vacuum.stop`
- `vacuum.return_to_base`
- `vacuum.locate`

Expected conservative default before verification:

- Other dock task starts are unavailable.
- Robot start is unavailable except during verified `dry_dock_bag`.
- Robot stop remains available only when the dock task is part of an active
  robot clean session.
- Locate and return-to-base should not run while station work would make the
  result ambiguous.

## Live Evidence

### 2026-08-26, Downstairs Flow 2, firmware `v01.09.08.00`

Tested through Home Assistant service calls against a live docked robot:

| Task | Start from idle | Stop from HA | Follow-up state |
|---|---|---|---|
| `dry_mop` | verified | verified | task cleared and all dock switches returned to idle |
| `dry_dock_bag` | verified | verified | scoped `task/force_end` cleared the task; also verified while `dry_mop` remained active |
| `dry_dust_bin` | verified | verified | scoped `task/force_end` cleared the task; generic robot stop remains blocked |

Notes:

- `dry_mop` exposed a `3h 30m` timer and stopped through the single-task
  generic stop path.
- `dry_dock_bag` exposed a `5h` timer. A stale/unmapped coarse station flag
  initially blocked the scoped stop despite typed dock-bag telemetry; the stop
  gate was corrected so typed dock-bag force-end is allowed while generic stops
  remain blocked. The retry returned HTTP 200 and cleared the task.
- `dry_dust_bin` exposed a `45m` timer and progress. After a bounded payload
  sweep found `task/force_end` payload `0805`, a fresh start/stop repeat
  returned success and cleared the task.
- Parallel dock-task starts were not relaxed. While one task was active, the
  other task switches were unavailable.

### 2026-08-27, Upstairs Flow 2

Tested with Home Assistant Core stopped and a direct local client connected
from the HA SSH environment. The test started one dock task from idle, sent one
robot-side command, captured state at roughly `T+2s` and `T+10s`, then cleaned
up to idle before the next cell.

Verified robot-command behavior while `empty_dustbin` was active:

| Robot command | Response | Follow-up state | Integration rule |
|---|---|---|---|
| `locate` | `SUCCESS` | Emptying continued | Safe but optional to expose |
| `return_to_base` | `CONFLICT` | Emptying continued | Hide while docked/dock task active |
| `pause` | `SUCCESS` | Emptying cleared/idle | Hide; raw command affects dock work |
| `stop` | `SUCCESS` | Emptying cleared/idle | Keep on dock switch only, not vacuum entity |
| `clean/start_clean` | `CONFLICT` | Emptying continued | Hide start/clean-area |

Verified robot-command behavior while `wash_mop` was active:

| Robot command | Response | Follow-up state | Integration rule |
|---|---|---|---|
| `locate` | `SUCCESS` | Washing continued | Safe but optional to expose |
| `return_to_base` | `APPLIED` | Stale paused/docked-looking state | Hide while docked/dock task active |
| `pause` | `SUCCESS` | Stale paused/docked-looking state | Hide; raw command affects dock work |
| `stop` | `SUCCESS` | Transitioned into `dry_mop` | Keep stop scoped to dock task controls |
| `clean/start_clean` | `SUCCESS` | Did not start cleaning; left contradictory dock fields | Hide start/clean-area |

After the raw `clean/start_clean` attempt during `wash_mop`, the robot reported
`working_status=DOCKED_V2` while explicit dock fields reported off-dock
(`dock_field11=1`, `dock_field47=2`, later `dock_presence=0`). Recall was
accepted in that state. The integration must therefore treat explicit off-dock
fields as stronger than the coarse `DOCKED_V2` value for dock readiness and
return-home availability.

Drying-task cross-section rows were not considered verified in this run because
`dry_mop`, `dry_dust_bin`, and `dry_dock_bag` setup starts returned `CONFLICT`
after the contradictory post-wash state. Their start/stop baseline remains the
2026-08-26 Downstairs evidence above until a fresh accepted drying run is
captured.

Follow-up after redeploying Home Assistant on the cleaned branch: HA reported
`switch.upstairs_narwal_dock_dry_and_disinfect_dust_bin` as active with live
`time_left` and `progress` attributes after restart, and the Narwal app
confirmed the same dust-bin drying task was active. Treat that as real device
telemetry, not a restored HA switch assumption. Dock task switches should only
come from robot telemetry or a short accepted-command guard.

## Per-Task Evidence

### Empty dustbin

Verify:

- Start command accepted from docked idle.
- Active state maps to `emptying_dustbin`.
- Matching switch turns on.
- Other dock starts are blocked or rejected.
- Stop either succeeds or is correctly unavailable.
- No progress/time attributes are exposed unless telemetry proves them.

### Wash mop

Verify:

- Whether `supply/wash_mop` or `supply/wash_mop_by_robot_status` is required.
- Active state maps to `washing_mop`.
- Intermediate mop washing during a clean does not hide robot `stop`.
- Stop behavior is task-specific or generic single-task only.

### Dry mop

Verify:

- Active state maps to `drying_mop`.
- Timer fields or mop remaining-time query produce correct `time_left`.
- Progress is omitted when only remaining time is known.
- Fresh typed timer suppresses stale coarse `dock_activity`.

### Dry dust bin

Verify:

- Start command accepted from docked idle.
- Active state maps to `dry_dust_bin`.
- Timer fields `10/11`, if present, produce `progress` and `time_left`.
- Polling-only fallback survives the immediate stale post-command refresh.
- Stop uses scoped `task/force_end`, not generic robot stop.
- Live 2026-08-26: two Flow 2 runs confirmed `task/force_end` payload
  `0805` clears `dry_dust_bin` telemetry and keeps it clear.

### Dry dock bag

Verify:

- Start command accepted from docked idle.
- Active state maps to `dry_dock_bag`.
- Timer fields `12/13`, if present, produce `progress` and `time_left`.
- Robot can start a clean while dock-bag drying continues.
- The switch stays on after the robot leaves the dock if the dock task is still
  running.
- Typed stop stops dock-bag drying without cancelling a robot clean.

## Automated Tests

Unit/entity tests should cover:

- Exactly five dock task switches.
- Dock device ownership and stable unique IDs.
- Idle, off-dock, robot-cleaning, fault, and unavailable coordinator states.
- Each known raw task mapping.
- Unknown active station activity blocks all starts.
- Malformed, zero, expired, and missing timers do not create false attributes.
- Typed timers override stale coarse dock activity.
- Parallel active timers do not allow unscoped stops.
- Wake paths refresh status and revalidate the selected task before command
  dispatch.
- Polling-only command assumptions clear on authoritative idle state or TTL.
- `dry_dock_bag` is preserved during robot departure only when backed by typed
  evidence or a bounded accepted-command fallback.
- Dock maintenance does not create charge-to-resume.
- Robot stop remains available during active-clean dock phases.

## Related Work

Directly related:

- Fork PR #22: dock task switch cleanup.

Keep separate:

- Fork PR #16 and upstream issue #19: room cleaning settings.
- Fork PR #20: vacuum supported-feature cleanup.
- Fork PR #14 and upstream issues #79/#77: consumables and tank semantics.
- Fork PR #21 and upstream issue #75: trails and map path rendering.
