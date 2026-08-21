# v1.0.4 — Consumable alerts never worked. Now they do.

On top of [v1.0.3](RELEASE-NOTES-v1.0.3.md). **Upgrade if you own any supported model** — one of the fixes here means two of your entities have been giving you a confidently wrong answer since the day they shipped.

Also in this release: **Freo Z Ultra (CX7) support**, and a rename of the fan-speed tiers.

---

## The consumable alerts were always empty

`binary_sensor.…_maintenance_required` and `binary_sensor.…_replacement_required` have reported **no problem, on every robot, always** — regardless of what the robot actually said.

`consumable/get_consumable_info` returns its two lists as **packed repeated varints**. blackboxprotobuf surfaces a packed field as a `str` whose code points are the encoded bytes, and the parser accepted only "an int, or a list of ints". So `int('\x04\x06\x08\n')` raised, a per-item `except: continue` swallowed it, and both lists came back empty.

An empty list means *nothing needs attention*. That is what made this invisible for so long: **it failed as good news.** Nothing errored, nothing logged, and the sensors looked healthy because "parsed to nothing" and "reported nothing" are indistinguishable from the outside.

Captured from the development Flow (AX12, `v01.08.03.07`), a robot that had been running for months:

```
consumable/get_consumable_info ->
  {'1': {'1': '\x04\x06\x08\n', '2': '\x03\x14'}}

  maintainItems = [4, 6, 8, 10]  wash ribs, universal wheel,
                                 side distance sensor, anti-winding brush
  replaceItems  = [3, 20]        side brush, station bag
```

Six parts wanted attention. Home Assistant said everything was fine.

### After you upgrade

**Look at those two entities.** Whatever they say now is the first honest answer they have given you, and on a robot that has done any real work it will probably not be "fine". Each carries an `items` attribute naming the parts, which makes them usable as automation triggers:

```yaml
trigger:
  - platform: state
    entity_id: binary_sensor.narwal_flow_replacement_required
    to: "on"
```

### Why the test suite did not catch it

The existing test fed `{"1": [1, 9], "2": 8}` — the shape a *hand-written* payload takes, not the shape a robot sends. The test and the parser shared the same wrong assumption, so they agreed with each other and disagreed with the device.

The new tests are built from the capture above, and cover the cases where a subtly wrong decoder still passes: a single-item list, a `bytes` blob rather than `str`, a multi-byte varint, and an empty blob still meaning healthy.

---

## Freo Z Ultra (CX7) now works locally

From [@KakatkarAkshay](https://github.com/sjmotew/NarwalIntegration/pull/76), confirmed independently by [@northwestsupra](https://github.com/sjmotew/NarwalIntegration/issues/5). This model was listed as **Not Compatible** for months. That was right about the device and wrong about the conclusion: the CX7 does answer local WebSocket queries — it just refuses to speak until addressed with its real 32-character Device ID, and it never broadcasts.

**It stays fully local.** The Device ID is a value you paste in once during setup; the integration makes no cloud calls, and there is no account anywhere in the path.

What to expect, stated plainly rather than discovered later:

| | Behaviour |
|---|---|
| State updates | Polled every 60 s, not pushed. The vacuum reaches `cleaning` ~30 s after a start |
| Live map position | **Unavailable** — the model emits no broadcasts |
| `cleaning_time`, `cleaning_area`, `current_room` | **Stay `unknown` during a clean** — all three read broadcast-only fields |
| Everything else | Base status, maps, consumables, commands all work |

Verified on two independent devices (hardware CX7, cloud identity J5, product key `hEA7OEshlx`) on firmware `v01.13.11.02`. The `BYWBPqSxeC` variant reported in [#5](https://github.com/sjmotew/NarwalIntegration/issues/5) is still untested — that issue stays open, and owners of one are asked to report.

---

## Fan speed tiers renamed

The five tiers are now **Quiet, Standard, Strong, Super, Ultra** — previously the top two were spelled "Super powerful" and "Ultra powerful".

**Existing automations keep working.** `FAN_SPEED_MAP` still accepts both old spellings and the original lowercase `quiet` / `normal` / `strong` / `max`. But if an automation *compares* against the displayed value — `state_attr('vacuum.…', 'fan_speed') == 'Super powerful'` — that comparison will now be false. Update those.

### Ultra is withheld on the Freo Z10 Pro / Turbo (AX26)

Three app captures from [@shin906710](https://github.com/sjmotew/NarwalIntegration/issues/70) settled a question open since v1.0.2: the AX26 app's highest suction tier sends `CleanParam` tag 2 = **4** (`DEEP`), with the tier below it sending 3. So value 5 is unreachable from that app, and the five-value enum is otherwise correct.

Leaving Ultra in the picker was worse than cosmetic. The live command `clean/set_fan_level` carries `SweepFanLevel`, which has **no** `SUPER` — the client maps 5 down to `STRONG`. Selecting Ultra mid-clean silently applied Strong. On AX26 that entry is now gone; every other model keeps all five.

If you have an AX26 automation that sets Ultra, point it at Super.

---

## Also in this release

- **`CleanParam` tag 8 identified** — the app's coverage-precision toggle (1 = Standard, 2 = Meticulous), from the same captures. This closes an unknown carried since Phase 9 and is now documented rather than guessed at.
- **Debug logging dumps the whole `base_status` field map.** It previously printed 2 fields out of 30, which made field-level bug reports unanswerable from a log — the reporter had to run a script to produce what a debug log should already contain.
- **`docs/PROTOCOL.md` gains a consumables section** — both data sources, the packed-varint encoding that caused the bug above, both enum tables, and the rule that an absent field means *unknown* rather than zero. The open consumable questions are collected in [#79](https://github.com/sjmotew/NarwalIntegration/issues/79).

---

## Known limitations, unchanged

**Dust bag health reads `unknown` on the Flow (AX12).** It comes from `base_status` field 35, which this model does not send at all. The sensor deliberately reports unknown rather than inventing a zero — but a gauge card bound to it will render "Entity is non-numeric". Use the **Station dust bag** binary sensor instead. Which models send field 35 is an open question in [#79](https://github.com/sjmotew/NarwalIntegration/issues/79).

**"Detergent remaining" is an unverified mapping.** It reads `base_status` field 41, named `heavyDetergentRemainPercent` in the decompiled app, and has only ever been observed as `100`. If your cartridge is visibly low and this still reads 100%, please say so in [#79](https://github.com/sjmotew/NarwalIntegration/issues/79) — a negative result settles it fastest.

---

## Verification

255 tests passing, CI green. Deployed to a live Home Assistant on real hardware before tagging — 28 entities, both alert sensors correctly reporting their item lists, vacuum and camera unaffected. A green test suite has hidden real integration failures on this project before, because the test stubs are not Home Assistant.

**Full changelog:** [`v1.0.3...v1.0.4`](https://github.com/sjmotew/NarwalIntegration/compare/v1.0.3...v1.0.4)
