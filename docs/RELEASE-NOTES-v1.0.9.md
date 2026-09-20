# v1.0.9 — Phantom consumable alerts stopped, room controls opt-in

On top of [v1.0.8](RELEASE-NOTES-v1.0.8.md). **No breaking changes.** Two fixes reported by users within days of v1.0.8, one new model, and a tool for the room-picker dashboard.

863 tests, up from 854.

---

## The maintenance and replacement sensors no longer fire on nothing

From [#100](https://github.com/sjmotew/NarwalIntegration/pull/100) by [@TgMrP](https://github.com/TgMrP), closing [#99](https://github.com/sjmotew/NarwalIntegration/issues/99).

On a Flow 2 running firmware v01.09.09.05, `binary_sensor.<name>_maintenance_required` and `binary_sensor.<name>_replacement_required` turned `on` roughly once an hour with `items: ["1000"]`, while the Narwal app showed no alert at all. The same thing is visible in a Freo 20's diagnostics on firmware v01.00.36.11, so it is not one robot's quirk.

The cause is a response shape nobody had seen. `consumable/get_consumable_info` is documented, and on the original Flow behaves, as two repeated lists of consumable ids. These firmwares instead answer with an envelope: five scalar fields all equal to `1000`, a `30000`, and the robot's firmware string. The parser read the first two scalars as one-element id lists, and `1000` is not a consumable, so both sensors lit up for a part that does not exist.

**An id the integration cannot name no longer raises a problem.** Both sensors now filter their lists through the known consumable enums before deciding, and the unrecognised values move to a new `unknown_ids` attribute so they stay visible without alarming anyone. The parser itself is unchanged, so a real single alert arriving as a bare scalar, which protobuf does for one-element repeated fields, still works exactly as it did.

**The raw response is now in the diagnostics download** as `consumables.raw_consumable_info`. Command responses never reach the debug log's `DUMP` line, which is why this took a local patch to capture; from now on a diagnostics file shows what the robot actually sent.

What the five `1000`s and the `30000` mean is still open. They are not per-mille remaining life: the app lists a different remaining figure for every part.

## Per-room controls are opt-in

From [#96](https://github.com/sjmotew/NarwalIntegration/pull/96) by [@Sean-StarLabs](https://github.com/Sean-StarLabs), closing [#94](https://github.com/sjmotew/NarwalIntegration/issues/94).

v1.0.8's per-room cleaning profiles create seven entities per room. On a house with 24 rooms that is 168 entities appearing in the registry at once, most of them for rooms nobody will ever configure individually.

**New installs now get the room profile selects disabled and the room-selection switches hidden** in the entity registry. Enable the ones you want from the device page; they behave exactly as before once enabled. Existing installs are untouched — Home Assistant applies these defaults only when an entity is first registered, so anything you already had enabled stays enabled.

## Narwal Freo 20 added to the model selector

Closing [#97](https://github.com/sjmotew/NarwalIntegration/issues/97). Confirmed by [@kvkessler](https://github.com/kvkessler): map streaming, current room, and start / pause / return / room clean all work from Home Assistant on firmware v01.00.35.03 and v01.00.36.11. Product key `fjhpiem4ba` is recognised, so the robot is named correctly instead of showing as `Unknown (fjhpiem4ba)`. The Freo 20 is a distinct model from the JX; it shares nothing with it but the answer to [#42](https://github.com/sjmotew/NarwalIntegration/issues/42).

## Room-picker dashboard generator

`tools/gen_dashboard.py` builds a Rooms section, a Dock section, an `input_select` room picker and a `script` that cleans one room *with its per-room profile* — which is the only way to do that from a dashboard, since the built-in Clean-areas picker sends the global settings. It reads a copy of Home Assistant's entity registry and emits YAML you splice into your own dashboard. See the script's docstring for the invocation.

---

## Also this cycle

- [#98](https://github.com/sjmotew/NarwalIntegration/issues/98) — a Z10 Ultra stuck at `task_status: returning` after docking. The fix is in review as [#95](https://github.com/sjmotew/NarwalIntegration/pull/95) and did not make this release.
- [#101](https://github.com/sjmotew/NarwalIntegration/issues/101) — a Freo Z Ultra (CX7) coming up unavailable after a Home Assistant restart. Newly reported; a debug log is requested.
- The first two Freo Z Ultra diagnostics sets arrived on [#5](https://github.com/sjmotew/NarwalIntegration/issues/5). The CX7 entry in the model list is now backed by live data.

## Thanks

[@TgMrP](https://github.com/TgMrP), whose first contribution came with a 50-hour capture, the parser left alone, and tests for the case that must keep working. [@Sean-StarLabs](https://github.com/Sean-StarLabs) for #96. [@kvkessler](https://github.com/kvkessler) for confirming the Freo 20 end to end, and [@northwestsupra](https://github.com/northwestsupra) and [@thorsten-gehrig](https://github.com/thorsten-gehrig) for the CX7 diagnostics.

863 tests, CI green on the release commit, deployed to a live Home Assistant instance and verified against real hardware before tagging.
