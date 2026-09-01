# v1.0.7 — The model name now matches the robot

On top of [v1.0.6](RELEASE-NOTES-v1.0.6.md). **No breaking changes.** One fix, reported within hours of v1.0.6 by [@DeNo64](https://github.com/sjmotew/NarwalIntegration/issues/81).

---

## Your robot could be correctly set up and still show the wrong model

v1.0.6 made auto-detected robots take their model's name instead of a raw product key. That fix was too narrow, and it missed the case most people actually hit.

The model selector defaults to its first option, **"Narwal Flow"**, and discovery cannot pre-select anything — mDNS carries no model information. So the normal path is: the robot is discovered, the form opens on "Narwal Flow", and you accept it.

Meanwhile the integration was already storing the product key **the robot itself reported**, not the one implied by your selection. The result was an entry that disagreed with itself:

| | stored value |
|---|---|
| `product_key` | `QxMSPG6VSO` — Narwal Flow 2, read off the robot, correct |
| `model` | `Narwal Flow` — from the dropdown default, wrong |

Everything that matters functionally keys off the product key — suction tiers, dock light support, whether the model broadcasts — so those were all correct. But the device registry showed the wrong model on a perfectly configured robot, which is both confusing and exactly the kind of thing that misleads a bug report later.

**The model label now always describes the key the robot reported.** If that key is one we recognise, its name is used regardless of what was selected. If the key is unrecognised there is nothing to correct with, so your choice stands.

The underlying mistake was overriding the user's *key* while respecting their *label*. Trusting the robot for one and the form for the other is what let the two drift apart.

### If you are already affected

Nothing breaks and no action is required. Re-adding the robot will pick up the correct name; leaving it alone keeps the wrong label with correct behaviour.

### A test was deleted to make this work

v1.0.6 shipped a test asserting that an explicitly chosen model is never overridden. That test encoded the bug. It is replaced by one built from @DeNo64's actual scenario — a Flow 2 accepted at the "Narwal Flow" default — and verified to fail against the previous behaviour.

---

## Thanks

[@DeNo64](https://github.com/DeNo64), who has now found five distinct issues across three releases, upgraded to each one within hours, and reported every finding with the evidence that made it actionable.

280 tests, CI green on the release commit, deployed to a live Home Assistant instance and verified against real hardware before tagging.
