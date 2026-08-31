# v1.0.6 — Your robot was being poked 1,900 times a day

On top of [v1.0.5](RELEASE-NOTES-v1.0.5.md). **No breaking changes.**

Three fixes, all of them things this integration was doing wrong to every user rather than edge cases: it was waking your docked robot roughly every 46 seconds around the clock, it named auto-detected robots after a raw product key, and a failed Device ID lookup during setup could only be escaped by restarting Home Assistant.

---

## The integration was nagging your robot all day

`v1.0.5` quieted two log lines that were 29% of some installs' logs. Those lines were the symptom. This release fixes the cause.

The keepalive loop treated *silence* as *sleep*. If no broadcast arrived for `BROADCAST_STALE_TIMEOUT` (15 seconds) it declared the robot asleep and, on the next tick, sent a full wake burst. On a docked robot that produced a permanent cycle — measured independently by [@hyeok-yoo](https://github.com/hyeok-yoo) at **one cycle every 46.001 s with 0.417 s standard deviation** over 40 minutes, which is roughly **1,900 wake bursts a day** at a robot that is doing nothing.

That regularity was the tell. A robot broadcasting on its own schedule does not hold half-second phase against our timer for 40 minutes. The period was ours: three 15-second loop ticks, plus the 1.0 s the burst itself takes to send five commands.

### What the robot is actually doing

We suppressed every wake burst on a development Flow (AX12, `v01.08.03.07`) and watched for 775 seconds:

| # | broadcast window | messages | silence before |
|---|---|---|---|
| 1 | 45.5 s | 110 | — |
| 2 | 45.5 s | 115 | 62.5 s |
| 3 | 30.0 s | 73 | 120.5 s |
| 4 | 45.5 s | 115 | 122.5 s |
| 5 | 30.0 s | 76 | 60.0 s |
| 6 | 45.4 s | 112 | 123.6 s |

**The robot has a duty cycle.** It broadcasts for 30 or 45 seconds, goes quiet for one to two minutes, and comes back on its own. Five of those six windows started with nothing sent to the robot at all.

So a docked robot is silent for up to **123.6 seconds** as a matter of normal operation, and the threshold for calling that "asleep" was **15 seconds**. It could never have been satisfied.

### Why the constant is unchanged

The obvious fix is to raise `BROADCAST_STALE_TIMEOUT`. We deliberately didn't.

Its comment reads "~10x the 1.5s broadcast interval". The 1.5 s figure is real — it is the cadence *inside* a window, confirmed at 1.41–1.45 s here. But the robot doesn't broadcast continuously, so scaling a sleep threshold off that cadence measures the wrong thing entirely. Any value derived that way is wrong regardless of how large it is.

Choosing a replacement would mean picking a number from five silences on one robot on one firmware. That is precisely how 15.0 got there in the first place. **The conclusion drawn from the timeout was wrong, not the timeout.**

So the wake burst is now gated on *state* instead: silence from a **docked** robot is normal and the robot is left alone. Off the dock, silence is still a genuine dropout — a stalled clean, a robot asleep away from the dock, a dead socket — and escalates exactly as before, unchanged.

### What you should notice

Nothing, except less noise. Docked state still updates on the 60-second poll, and any command still rouses the robot on demand before it is sent. If you watch your logs at debug level you will see `Docked and quiet for Ns — leaving the robot alone` where a wake burst used to be.

One subtlety worth recording: the broadcast subscription used to be renewed as a side effect of those wake bursts. With the bursts gone it now runs on its own schedule, because letting it lapse is what causes [#73](https://github.com/sjmotew/NarwalIntegration/issues/73) — the vacuum entity freezing at `docked` while the sensors keep updating.

### A second bug fell out of the first

Making the subscription stand on its own exposed something that had been hiding behind the wake bursts.

The "renew every 8 minutes" timer started from zero and asked `monotonic() - 0 > 480`. `time.monotonic()` is only guaranteed to be *monotonic* — on Linux it is system uptime. On a host that had been up for less than eight minutes that comparison is false, so **the very first broadcast subscription was silently deferred**.

That never mattered while every wake burst also carried a subscription. With bursts no longer fired at a docked robot it matters a great deal: no subscription means no broadcasts at all, which is [#73](https://github.com/sjmotew/NarwalIntegration/issues/73), the vacuum entity frozen at `docked`. A Raspberry Pi starting Home Assistant at boot is exactly the affected case.

Fixed by tracking "never sent" explicitly rather than inferring it from a clock whose origin is not defined.

It was CI that caught this — the test runner's uptime is genuinely under eight minutes, so it reproduced the condition that a developer machine, up for days, never will.

---

## Setup fixes

Both reported by [@DeNo64](https://github.com/sjmotew/NarwalIntegration/issues/81) against v1.0.5's new discovery.

**Auto-detected robots are named after their model.** Auto-detect resolves the real product key over the WebSocket, then threw it away and titled the entry `Narwal CGjuB6dzq7`. A key we recognise now names the entry after the model and fills in the model field, so the device registry shows a model rather than a raw string. An unrecognised key still shows the key — at that point it is the only identifying thing anyone has, and it is what a bug report needs. A model you picked yourself is never overridden.

**The Device ID page is no longer a dead end.** When automatic discovery failed, setup sent you to the manual Device ID page, and that page could only ever re-display itself. There was no route back. Worse, Home Assistant resumes an in-progress discovery flow at whatever step it was on, so starting setup again dropped you straight back on the same page — restarting Home Assistant was the only way out.

That page now carries a **"Try automatic detection again"** checkbox. Since the most likely reason a first attempt fails is a robot in deep sleep, retrying is usually all that is needed. A blank Device ID now gives a proper error instead of a raw validation message.

---

## Documentation

- **VLAN traversal is settled** ([#81](https://github.com/sjmotew/NarwalIntegration/issues/81)). Captures show the inbound SYN arriving at the robot and no SYN-ACK returning, while the robot's own outbound cloud connections work fine. It receives the connection and declines to answer. SNAT is the fix, and a static route cannot help because there is no reply to route.
- **mDNS can cross VLANs after all.** The previous claim that you would "usually need to add by IP" was too strong — discovery works end-to-end through a router that reflects multicast, confirmed on v1.0.5. Note the asymmetry: the robot happily announces itself over multicast and still refuses inbound unicast from a foreign subnet, so discovery succeeding while the connection fails without SNAT is normal rather than contradictory.

---

## Thanks

[@hyeok-yoo](https://github.com/hyeok-yoo), who reported a log-noise annoyance and then measured it into a mechanism — 52 intervals, a 0.417 s standard deviation, and a decomposition that predicted the loop's behaviour to within 0.2 s. [@DeNo64](https://github.com/DeNo64), for four findings across two releases, all from packet captures rather than guesses, two of which corrected things written down here wrongly.

279 tests, CI green, deployed to a live Home Assistant instance and verified against real hardware before tagging. The four tests covering the wake fix were each checked against a deliberate mutation of the line they cover, so they are known to fail when the fix is absent.
