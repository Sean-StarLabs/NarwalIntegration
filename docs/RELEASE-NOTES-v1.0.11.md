# v1.0.11 — A docked robot stays connected, and a cleaning robot says so

On top of [v1.0.10](RELEASE-NOTES-v1.0.10.md). **No breaking changes.** Eleven pull requests from five contributors, plus four fixes and three documentation updates of our own.

981 tests, up from 872.

---

## A docked robot no longer reconnects every six minutes

[#113](https://github.com/sjmotew/NarwalIntegration/pull/113) from [@wawtor](https://github.com/wawtor), closing [#90](https://github.com/sjmotew/NarwalIntegration/issues/90).

The robot closes its WebSocket 60 seconds after the last *command* it received (close code 1000, reason "Idle timeout"); WebSocket pings do not reset that timer. Since v1.0.6 stopped waking a quiet docked robot, the only regular command left was the 60-second status poll, which raced the timeout. A docked install therefore reconnected about every 350 seconds, ten to twelve times an hour, and every reconnect fired a wake burst that woke the robot for 45 seconds.

The quiet docked branch of the keepalive now sends the app heartbeat on each tick. The robot never answers it and it does not wake the robot: sent every 30 seconds for four minutes to a quiet docked Freo X10 Pro, the socket stayed open, no response came back, and no wake-up followed. Re-measured on the project's Flow before merging, with the same result.

A side finding corrected the protocol notes: a docked Narwal has no unprompted broadcast duty cycle. A fresh socket that sends nothing receives nothing; every broadcast window follows something we sent.

## Responses go to the command that asked for them

[#110](https://github.com/sjmotew/NarwalIntegration/pull/110), [#111](https://github.com/sjmotew/NarwalIntegration/pull/111) and [#112](https://github.com/sjmotew/NarwalIntegration/pull/112) from [@wawtor](https://github.com/wawtor), closing [#108](https://github.com/sjmotew/NarwalIntegration/issues/108) reported by [@bst2k](https://github.com/bst2k).

`send_command()` took whatever response arrived next. A response that landed after its own command had timed out was handed to the *next* command, so `get_map()` could receive a `robot_base_status` payload and crash. Three fixes:

1. **Responses are matched to requests by send order.** Every request that is owed a response takes a place in a ledger; each response goes to the oldest place still open. A timed-out command keeps its place for ten seconds so its late answer is discarded rather than misdelivered. Fire-and-forget sends the robot acknowledges (wake burst, topic subscription) take a place too; the app heartbeat, which is never answered, takes none.
2. **A wrong-shaped `get_map` response parses to an empty map** instead of raising `AttributeError` out of the caller.
3. **`narwal.clean_rooms` with explicit room IDs no longer refreshes the map first.** When that refresh timed out the service failed with "Narwal map could not be refreshed" before the clean was sent. IDs the cached map already knows are used directly; `all`, and any unknown ID, still refresh so room edits made in the app are picked up.

## A Flow 2 on firmware v01.09.10 is no longer shown idle while it cleans

[#126](https://github.com/sjmotew/NarwalIntegration/pull/126), closing [#124](https://github.com/sjmotew/NarwalIntegration/issues/124) reported by [@Fuchschr11](https://github.com/Fuchschr11), with a matching payload from [@MiLeX84](https://github.com/MiLeX84) in [#115](https://github.com/sjmotew/NarwalIntegration/issues/115).

Flow 2 firmware v01.09.10.02 reports `working_status 2` while the robot is out cleaning. That value had been named `DOCKED_V2` from a single v01.07.23 capture taken while charging. Nothing promoted it to cleaning, so the vacuum sat at `idle`, `task_status` fell to `unknown`, every `base_status` packet wiped the cleaning time and current room the broadcast had just set, and Stop and Pause disappeared from the entity mid-clean.

Status 2 with sub-field 4 = 3 is now decoded as an active clean at parse time, so every predicate agrees; `raw_base_status` keeps the wire value. The charging capture still reads as docked.

## Freo Z Ultra (CX7): commands work even though its state lags

[#105](https://github.com/sjmotew/NarwalIntegration/pull/105) from [@fishscrounger](https://github.com/fishscrounger), closing [#102](https://github.com/sjmotew/NarwalIntegration/issues/102); documentation in [#106](https://github.com/sjmotew/NarwalIntegration/pull/106).

The CX7 never broadcasts, and its polled `base_status` lags the robot badly enough that every capability predicate evaluated false while it drove, so the vacuum entity advertised `STATE` alone and no command could be sent. For non-broadcasting models the local vetoes are now skipped, the full feature set is advertised, and the robot arbitrates: `NOT_APPLICABLE`, `CONFLICT` or `NOT_READY` is surfaced as a Home Assistant error. Broadcasting models are untouched.

The README now explains why discovery matches a CX7 on its mDNS hostname, why the Device ID has to come from an MQTT capture (with the relay recipe), and which topics stay silent on this model. `docs/PROTOCOL.md` gains the table of topics a CX7 answers locally.

## A clean started from the app is reconciled correctly

[#95](https://github.com/sjmotew/NarwalIntegration/pull/95) from [@Sean-StarLabs](https://github.com/Sean-StarLabs), closing [#98](https://github.com/sjmotew/NarwalIntegration/issues/98) reported by [@remussek](https://github.com/remussek).

Task metrics and `base_status` can arrive out of order, especially for cleans started outside Home Assistant, and a Z10 Ultra was left at `task_status: returning` after it had docked. Terminal and error packets are now authoritative; an externally started clean is recognised only once directional telemetry confirms it; pause and resume stay coherent when terminal telemetry lands while a resume is in flight; partial task details survive without carrying stale room metadata across room changes.

---

## Smaller fixes

- [#114](https://github.com/sjmotew/NarwalIntegration/pull/114), [@wawtor](https://github.com/wawtor) — **no more phantom `display_map` dropouts.** The first push after every connect logged a "999 s" dropout and re-subscribed a fraction of a second after startup already had; mid-clean, a Freo X10 Pro's normal 9-30 s quiet spells tripped it behind the keepalive's own wake burst. Silence is now measured from the later of the last `display_map` and the last subscription we sent.
- [#117](https://github.com/sjmotew/NarwalIntegration/pull/117), [@wawtor](https://github.com/wawtor) — the first dropout recovery was held back until the *host* had been up for 45 s, because the cooldown compared against a monotonic clock that started at zero.
- [#116](https://github.com/sjmotew/NarwalIntegration/pull/116), [@turmacar](https://github.com/turmacar) — **the detergent sensor is disabled by default on the Flow and Flow 2.** Field 41 reads 100 on those models regardless of the tank; see [#79](https://github.com/sjmotew/NarwalIntegration/issues/79) and [#128](https://github.com/sjmotew/NarwalIntegration/issues/128). Follow-up: the rule keys off the model, so regional Flow 2 product keys are covered too.
- [#109](https://github.com/sjmotew/NarwalIntegration/issues/109) — **Narwal Freo 20 Edge** (SKU YJCC041, key `ulonq49mm1`) in the model selector, confirmed end to end by [@cwisor](https://github.com/cwisor).
- Manifest: Pillow removed from `requirements` (Home Assistant ships it, and Hassfest now rejects it) and `CONFIG_SCHEMA` declared. No runtime change.
- Docs: the Narwal T10 is the cloud-only J1 and not compatible ([#44](https://github.com/sjmotew/NarwalIntegration/issues/44)); `docs/PROTOCOL.md` records which burst commands the robot acknowledges and the 60-second idle timeout; capture guide frame diagram corrected.

## Also this cycle

- [#128](https://github.com/sjmotew/NarwalIntegration/issues/128) — eight side-by-side diagnostics from [@DeNo64](https://github.com/DeNo64)'s Flow 2 settle the tank fields on that model: field 24 is the sewage tank (2 = full *or* removed), and removing the clean-water tank or the detergent tank changes nothing in `base_status` while the robot sits docked. The clean-water-tank sensor cannot fire on the Flow 2 until the real source of that signal is found; noted under Known Limitations.
- [#92](https://github.com/sjmotew/NarwalIntegration/issues/92) — a second plain Freo Z10 (firmware v01.04.50.01) shows the same signature as the first: mDNS advertised, port 9002 refused, every closed port answering RST, 22 high ports open. Service fingerprinting of those ports is the next step.
- Open pull requests from [@Sean-StarLabs](https://github.com/Sean-StarLabs) — [#120](https://github.com/sjmotew/NarwalIntegration/pull/120) persistent room order, [#121](https://github.com/sjmotew/NarwalIntegration/pull/121) dock-stop reconciliation, [#122](https://github.com/sjmotew/NarwalIntegration/pull/122) suction options, [#103](https://github.com/sjmotew/NarwalIntegration/pull/103) native trails across room handoffs — are in their final review round and target v1.0.12. [#127](https://github.com/sjmotew/NarwalIntegration/pull/127) from [@jarostrug](https://github.com/jarostrug) ([#119](https://github.com/sjmotew/NarwalIntegration/issues/119)) is waiting on a rebase.
- The two Home Assistant deprecation warnings (`via_device`, `async_get_device`) remain on the list; both stop working in HA 2027.8.

## Thanks

[@wawtor](https://github.com/wawtor), for five pull requests and the measurement method that settled the idle-timeout question; [@fishscrounger](https://github.com/fishscrounger), [@Sean-StarLabs](https://github.com/Sean-StarLabs), [@turmacar](https://github.com/turmacar) and [@cwisor](https://github.com/cwisor); and [@bst2k](https://github.com/bst2k), [@Fuchschr11](https://github.com/Fuchschr11), [@MiLeX84](https://github.com/MiLeX84), [@remussek](https://github.com/remussek) and [@DeNo64](https://github.com/DeNo64) for reports that came with the payloads needed to fix them.

981 tests, CI green on the release commit, deployed to a live Home Assistant instance and verified before tagging.
