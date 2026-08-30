# v1.0.5 — Your robot finds itself now

On top of [v1.0.4](RELEASE-NOTES-v1.0.4.md). **No breaking changes** — the first release since v1.0.1 that can say that. Nothing you configured needs revisiting.

The headline is that setup no longer starts with a network scan. Also here: the Narwal JX is confirmed working, a log flood that accounted for **29% of some users' Home Assistant logs** is gone, and the traffic-capture tooling is finally in the repo.

---

## Robots are discovered automatically

From [#78](https://github.com/sjmotew/NarwalIntegration/pull/78), authored by [@StratoGh0st99](https://github.com/StratoGh0st99).

Until now, adding a robot began with finding its IP — router admin page, `nmap`, or guessing. Home Assistant now finds it for you by two independent routes:

- **mDNS / zeroconf** — the robot advertises `_narwal_sweeper._tcp.local.`
- **DHCP** — the robot's hostname matches `narwal_*`

Either one is enough. The robot appears under **Settings → Devices & Services** as a discovered device; click **Configure**, pick your model, and the IP is already filled in.

Adding by IP still works exactly as before, and you will still want it in two cases: a segmented network where multicast does not cross VLANs (see below), and any model that needs a pasted Device ID.

**One detail worth knowing**, because it improved on the original patch: matching on hostname alone is not quite enough — the announced name carries a device-id suffix. Discovery keys on that rather than the bare hostname, so two robots on one network resolve to two distinct entries instead of colliding. Verified with a live mDNS browse against the development Flow.

Field-tested unprompted by [@DeNo64](https://github.com/sjmotew/NarwalIntegration/issues/81) on a network none of us have access to, which is what cleared it for merge.

---

## Narwal JX confirmed working

From [#42](https://github.com/sjmotew/NarwalIntegration/issues/42), confirmed by [@Smiorld](https://github.com/Smiorld).

The JX product key has sat in the codebase as an unverified string extracted from the APK since May, with an explicit warning attached: *nobody has confirmed a JX working with this integration, and I would not buy one on the assumption that it will.*

Somebody now has. Port 9002 is open, it connects through auto-detect, and the map entity loads.

**Narwal JX** is now a selectable model rather than something you reach through Other / Auto-detect, and its key moved into the confirmed block of the discovery list — auto-detect walks that list in order, so a JX is found early instead of near the end.

Scope, stated honestly: connection and map are confirmed. Commands and room cleaning have not been exercised yet. The README says exactly that rather than implying full support.

---

## A log flood that was 29% of everything

From [#82](https://github.com/sjmotew/NarwalIntegration/pull/82), by [@hyeok-yoo](https://github.com/hyeok-yoo).

Two INFO-level lines — `Robot is awake (received broadcast)` and `No broadcast for 15s — robot may have gone to sleep` — produced **5,767 of 19,927 log lines over 37 hours** on a live Flow. About 3,700 lines a day, burying everything else and writing pointlessly to disk.

Both are internal bookkeeping about whether the robot is currently broadcasting, and neither is something you can act on. They are DEBUG now. Connection lifecycle and explicit wake attempts stay at INFO, so a normal log still shows you what matters.

### There is a real bug underneath this, and it is not fixed

Worth reading if you care about what your robot is doing when you are not watching.

Those two lines were the *symptom*. The same stale check that logs them also resets the wake flag and fires a **full wake burst** on the next tick. At the measured rate that is on the order of **1,900 wake bursts a day aimed at a docked, idle robot.**

The cause looks like a missing margin: `BROADCAST_STALE_TIMEOUT` and `KEEPALIVE_INTERVAL` are both 15 seconds, so a single missed broadcast window trips staleness, and the constant's own comment assumes a 1.5s broadcast cadence that holds mid-clean but evidently not while docked.

This release makes it quiet. It does not make it stop. Tracked as [#90](https://github.com/sjmotew/NarwalIntegration/issues/90), and the fix is deliberately waiting on a measurement of how often a docked robot actually broadcasts — picking a new number by reasoning is how the current one got here.

---

## Capture tooling is in the repo

From [#35](https://github.com/sjmotew/NarwalIntegration/pull/35), by [@StratoGh0st99](https://github.com/StratoGh0st99).

Three files that were written months ago and never merged, for an embarrassing reason: `tools/` was gitignored wholesale as "RE artifacts, kept locally", so nobody noticed the one thing in there meant to be shared.

- **`tools/CAPTURE_GUIDE.md`** — how to read the traffic the *app* sends the robot, using a Mac hotspot and Wireshark. The protocol on 9002 is plaintext `ws://`, so no TLS interception is needed. This matters because most remaining protocol unknowns live in the app-to-robot direction and never appear in broadcasts.
- **`tools/narwal_capture.py`** — record, live dashboard, diff and replay of annotated broadcast sessions.
- **`tools/coverage_probe.py`** — catalogues every topic and every field that ever changes, and flags topics the code knows about that never actually appear.

Landing them also required restoring a `DUMP` debug line the client had stopped emitting; both scripts recover payloads by scraping it out of the logs, so without it they would have run perfectly and produced nothing. It is DEBUG-only and formats lazily, and there are now tests that fail if it disappears again.

If you have been asked for a capture on an issue thread, this is the shortest path to producing one.

---

## Documentation

- **Segmented networks / VLANs** ([#81](https://github.com/sjmotew/NarwalIntegration/issues/81), @DeNo64) — robots do not answer connections whose source address is outside their own subnet, ICMP included. Captures now pin this to source filtering rather than a routing failure: the robot has a working default route and simply declines to reply. **SNAT is the fix**, and a static route cannot help because there is no reply to route. Also note mDNS discovery usually will not cross VLANs, so add by IP there.
- **A complete scheduled multi-room automation** ([#83](https://github.com/sjmotew/NarwalIntegration/issues/83), @Leo729n0c0k3) — the README explained the one-time area mapping and then left you to work out the automation. It now shows the whole thing, including the ordering constraint that is easy to get wrong: clean settings are read *at dispatch*, so they must be set before `vacuum.clean_area`, not after.
- **Domain collision** ([#84](https://github.com/sjmotew/NarwalIntegration/issues/84), @weha) — a separate project also installs to `custom_components/narwal`, so HACS can hold only one. Neither project can rename without orphaning its users' config entries, so this is now documented rather than something you discover through a failed install.

---

## Thanks

[@StratoGh0st99](https://github.com/StratoGh0st99) (discovery, capture tooling), [@hyeok-yoo](https://github.com/hyeok-yoo) (the log fix, and the measurement that exposed #90), [@Smiorld](https://github.com/Smiorld) (first confirmed JX), [@DeNo64](https://github.com/DeNo64) (VLAN captures and unprompted field-testing of discovery), [@weha](https://github.com/weha), [@Leo729n0c0k3](https://github.com/Leo729n0c0k3).

270 tests, CI green, deployed to a live Home Assistant instance and verified against real hardware before tagging.
