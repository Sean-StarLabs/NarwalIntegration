# v1.0.8 — The vacuum entity tells you what it can actually do

On top of [v1.0.7](RELEASE-NOTES-v1.0.7.md). **Two breaking changes**, listed first — read them before upgrading if you have automations or dashboard cards on the `current_room` sensor or on the suction tier names.

This is the largest release in the project's history. Four pull requests from [@Sean-StarLabs](https://github.com/Sean-StarLabs) land per-room cleaning profiles, a vacuum entity whose buttons mean what they say, native map trails that survive a restart, and dock task switches. Alongside them: a downloadable diagnostics dump, the fix for a `current_room` sensor that never worked for some robots, and the product-key aliases that were silently costing Flow 2 owners their dock light.

854 tests, up from 280.

---

## ⚠️ Breaking changes

### The `current_room` sensor is gone — it lives on the vacuum entity now

From [#88](https://github.com/sjmotew/NarwalIntegration/pull/88). `sensor.<name>_current_room` no longer exists. Its value, and the task context that used to be scattered or missing, are attributes of the vacuum entity:

| Attribute | Meaning |
|---|---|
| `current_room` | the room being cleaned, while a clean is running |
| `progress` | task progress in percent, while a clean is running |
| `task_status` | `cleaning`, `paused`, `returning`, `remapping`, `error`, `station_active`, `docked`, `idle` or `unknown` |
| `status_summary` | one line suitable for a tile card, e.g. `Kitchen - 42%` |

A config-entry migration removes the stale registry entry on first start. It also purges five registry entries that installs upgraded from before v1.0.2 may still carry (`_status`, `_task_status`, `_task_progress`, `_map_metadata`, `_base_station_cleaning_filter_used_hours`); those sensors were removed long ago and the entries were orphans.

**What to change:** anything reading `sensor.<name>_current_room` reads `state_attr('vacuum.<name>', 'current_room')` instead. A new `sensor.<name>_remaining_time` joins the elapsed-time sensor, and both are available only while they describe the current task.

### Suction tiers are named as the Narwal app names them

From [#91](https://github.com/sjmotew/NarwalIntegration/pull/91). The app's own labels for the five levels are Quiet, Standard, Strong, **Super Powerful**, **Ultra Powerful**. v1.0.4 through v1.0.7 called the top two **Super** and **Ultra**.

The fan speed list now shows the app's names. **The old names still work and are still listed** — `Super`, `Ultra`, and the original lowercase `quiet` / `normal` / `strong` / `max` all map to the same raw values they always did, so no existing automation fails. Home Assistant validates a `select_option` call against the entity's option list *before* the integration can translate it, which is why the old labels have to stay visible rather than merely accepted. The cost is a list with seven entries where two pairs mean the same thing. That will be tidied once the integration can raise a repair to walk you through renaming; until then, prefer the app's names in new automations.

Ultra Powerful is still withheld on the Freo Z10 Pro / Turbo (AX26), where the app itself tops out at Super Powerful ([#70](https://github.com/sjmotew/NarwalIntegration/issues/70)). A stored Ultra Powerful on that model is normalised down rather than silently applying Strong.

### Commands you cannot run right now are not offered

Also from #88, and the change most likely to surprise an automation. The vacuum entity's `supported_features` now reflect the robot's current state, so Home Assistant's own cards show only the buttons that will do something, and a service call for a withdrawn command fails with a validation error instead of silently doing nothing.

| Command | Offered when |
|---|---|
| `start` / `clean_area` | the robot is docked and idle and a clean can be prepared — including when a single drying task has to be stopped first — or a paused clean can resume. Withdrawn during cleaning, returning, blocking dock work, errors, or when an explicit room selection no longer matches the map |
| `stop` | a robot-side clean is live. It is not a generic dock-task stop |
| `pause` | cleaning or remapping, not already paused, and dock work does not block the robot |
| `return_to_base` | the robot is off the dock and can safely return. **It stays advertised while docked and idle, as a harmless no-op**, so an unconditional "send it home at 22:00" keeps working on the nights it is already home. Withdrawn while returning or while dock work blocks robot commands |
| `locate` | unless active dock work blocks robot commands |
| `set_fan_speed` | while configuring a compatible pending clean, or during a live task whose mode uses suction |

The one case that would have broken a common automation — `return_to_base` on an already-docked robot — was raised in review and is deliberately kept as a no-op. Verified against a docked, idle Flow (AX12) with the released code: `RETURN_HOME` advertised, `STOP` and `PAUSE` withdrawn.

---

## Per-room cleaning profiles

From [#87](https://github.com/sjmotew/NarwalIntegration/pull/87), by @Sean-StarLabs.

Until now every room in a clean got the same settings. Narwal's app has let you give the kitchen two mopping passes and the bedroom a quiet vacuum-only run for years; the integration flattened all of that into one global mode.

**Every room now has its own profile.** For each room on the current map you get `select` entities named `<Room> Mode`, `<Room> Suction`, `<Room> Water`, `<Room> Scrub`, `<Room> Route` and `<Room> Passes`, plus a `<Room> selected` switch. Rooms you never touch follow the global settings; rooms you configure keep their own.

**`vacuum.start` uses them.** With no rooms selected it cleans every known room, each with its own profile — the same coverage as before, with per-room behaviour. With one or more `selected` switches on, it cleans only those rooms. A mixed job — five vacuum-only rooms and two vacuum-and-mop rooms, say — goes to the robot as a single Narwal *custom clean* (task type 6), which is how the app does it. The robot reports that job as working status **17**, a value this project had been logging as "unmapped" since [#46](https://github.com/sjmotew/NarwalIntegration/issues/46); it is now decoded as `CUSTOM_CLEANING`.

**Two explicit command surfaces that do not depend on UI selection:**

- `narwal.clean_rooms` — room IDs (or `all`), mode, suction, water, mop strength, passes, route. Built for automations that want to say exactly what they mean.
- `vacuum.clean_segments` — Home Assistant's standard room-cleaning call, for anyone already using the area mapping from v1.0.2.

**Selections and profiles are stored by the integration, not by the entities.** They survive a restart even if the robot is unreachable at startup and the entities come up unavailable — a restart with every room entity unavailable was part of the validation, and the one-room selection and its Bathroom profile came back intact. Stored values are protocol enums, not labels, so this release's label rename cannot alter what a room does.

Validated on a Flow 2 running v01.09.09.05: a seven-room task with two different modes was accepted as task type 6, and the robot retained each room's mode, suction, route and two-pass setting through the clean and across a Home Assistant restart mid-job.

---

## Native map trails

From [#89](https://github.com/sjmotew/NarwalIntegration/pull/89), by @Sean-StarLabs. Closes [#75](https://github.com/sjmotew/NarwalIntegration/issues/75).

The map's cleaning trail used to vanish when you navigated away, and it was never the robot's own route in the first place — it was Home Assistant sampling positions.

Narwal does not send the whole route in every `display_map` packet. It sends a **rolling window of about 30 points**, and consecutive windows share a few exact coordinates. The integration now joins those windows by exact overlap into one retained route, renders only points the robot itself recorded, and keeps the route across Home Assistant restarts and after the clean finishes — until the next clean starts, when it is cleared and rebuilt.

It is deliberately conservative about what it joins. Narwal exposes no cleaning-session identifier, so a cached route is resumed after a restart only if the first live window actually overlaps it; otherwise it is discarded rather than risk stitching two different cleans together. No connector is drawn across missing telemetry, and no coordinates are smoothed or invented.

Validated during a live Flow 2 clean: 30 points persisted immediately before a restart, restored afterwards, and grown to 183 native points as later windows arrived while the robot kept cleaning.

---

## Dock task switches

From [#86](https://github.com/sjmotew/NarwalIntegration/pull/86), by @Sean-StarLabs.

The dock is now its own Home Assistant device, owning the dock light and the tank, consumable and dock-state entities. It gains five switches:

| Switch | What it runs |
|---|---|
| Empty dustbin | station dust collection |
| Wash mop | mop wash |
| Dry mop | mop drying |
| Dry dust bin | dust bin drying |
| Dry dock bag | station bag drying |

A switch is on while its task is active, available when it can be started or safely stopped, and carries the robot's `progress` and `time_left` for that task when the firmware reports them. There is no generic "force end" — stops are scoped to the task that is running, so you cannot cancel the wrong thing.

Robot commands are gated while the dock is busy: a clean start waits for, or safely stops, dock work that would block it, and dock actions and robot starts share one lock. The concurrency policy is conservative on purpose — task combinations nobody has verified on hardware stay blocked. Recorded runs on two Flow 2 units are in [`docs/DOCK_TASK_TEST_PLAN.md`](DOCK_TASK_TEST_PLAN.md).

Verified on the development Flow (AX12): docked and idle, the dock reports no station activity and the vacuum's command gate stays open.

---

## Diagnostics you can download

The single largest cost on this project has been asking reporters for information one comment at a time: which product key, which firmware, what the raw status looks like. The bug template now asks for one file first.

**Settings → Devices & services → Narwal → your robot's device page → ⋮ → Download diagnostics.** The dump includes:

- how the build resolved your robot's model — the reported product key, whether this version recognises it, and the label it chose. This alone would have answered [#81](https://github.com/sjmotew/NarwalIntegration/issues/81) from the first screenshot
- the complete raw `base_status`, with bytes hex-encoded, so an undecoded field is in the file rather than lost
- the robot's feature list, with a timeout guard that reports *why* it failed rather than hanging setup
- consumable alerts and error codes as decoded, alongside the values the sensors show

It redacts the host, device ID and bound-account UUID. **It deliberately does not redact the product key.** A key identifies a model, not a person, and a dump that hides it cannot answer the question it exists to answer.

---

## Fixes

### A regional Flow 2 was "Unknown" — and missing its dock light

From [#81](https://github.com/sjmotew/NarwalIntegration/issues/81), reported by [@DeNo64](https://github.com/DeNo64) on a unit bought in Australia.

One model ships more than one product key. The label lookup knew one key per model, so a Flow 2 reporting `mkbqaprvrb` showed as *Unknown (mkbqaprvrb)* in the device registry — and, because the dock-light feature check keys off the same table, had no dock light entity. A second Flow 2 key, `iSuVlI1If2`, had the same problem all along.

Every key a model is known to ship now resolves to that model. Regional variants are the most likely reason for the spread, and more will turn up; the diagnostics dump above is how they get reported.

**The model selector now defaults to Other / Auto-detect.** Discovery carries no model information, so a pre-selected "Narwal Flow" was a guess presented as a fact — and v1.0.7 was about exactly what that guess cost.

### `current_room` was `unknown` through every clean

From [#93](https://github.com/sjmotew/NarwalIntegration/issues/93), reported by [@jgerschk](https://github.com/jgerschk) with the root cause and a tested patch attached.

The robot answers the first `map/get_map` after a connection with a bare acknowledgement — a success code and no map body — if it is still waking. The client cached that as a valid, empty map. Every retry gate in the coordinator checks whether the map is *missing*, and it no longer was, so the real map was never fetched again. One lost race at startup, permanent for the life of the connection.

`get_map` now refuses a response that carries no active map, leaving the cache empty so the existing retry fires on the next broadcast or poll. The fix arrived through #87's client work and is covered by tests for exactly this payload.

### Product-specific topic prefixes survive auto-detection

From [#85](https://github.com/sjmotew/NarwalIntegration/pull/85), by @Sean-StarLabs. Some models use a topic namespace derived from their product key rather than the default. When `get_device_info` identifies a robot, the product key and device ID from that one response are both kept, so later commands address the robot's actual namespace. Models that announce themselves in broadcasts are unaffected.

---

## Compatibility notes

- **Plain Freo Z10** ([#92](https://github.com/sjmotew/NarwalIntegration/issues/92), [@nicobieri2000](https://github.com/nicobieri2000)): advertises over mDNS like every supported model, then **refuses** port 9002 outright — distinct from the open-but-silent signature of [#5](https://github.com/sjmotew/NarwalIntegration/issues/5) and the timeout of [#81](https://github.com/sjmotew/NarwalIntegration/issues/81). Recorded as under investigation in the compatibility table; a full port scan and an app-to-robot capture are the next step.
- **Freo X Ultra**: the table still says local control is unavailable, per [#4](https://github.com/sjmotew/NarwalIntegration/issues/4). If yours answers on 9002, its product key and `nmap -p 9002` in an issue would overturn months of misinformation.

---

## Thanks

[@Sean-StarLabs](https://github.com/Sean-StarLabs), whose six pull requests are most of this release: roughly fifteen thousand lines across the client, the integration and 574 new tests, every risky path validated on real hardware and written up, and a review question about `return_to_base` answered with code within the day.

[@DeNo64](https://github.com/DeNo64), who has now found seven distinct issues across three releases and tests from master commits rather than waiting for a tag. [@jgerschk](https://github.com/jgerschk), for a first report that arrived with a wire capture, a root cause and a working patch. [@nicobieri2000](https://github.com/nicobieri2000), for ruling out every usual suspect before filing.

854 tests, CI green on the release commit, deployed to a live Home Assistant instance and verified against real hardware before tagging.
