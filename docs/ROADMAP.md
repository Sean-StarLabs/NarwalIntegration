# Roadmap and open questions

What is planned, what is parked, and what a capture from the community could settle. Shipped
work is in the [CHANGELOG](../CHANGELOG.md); the protocol-level open questions with their
evidence are in [PROTOCOL.md §11](PROTOCOL.md#11-open-questions). Work targeted at a specific
release is tracked on the repository's
[milestones](https://github.com/sjmotew/NarwalIntegration/milestones).

## Next

1. **Repair issues.** The integration raises a repair only for an invalidated area mapping. v1.0.8 renamed suction tiers and removed a sensor; changes like that should surface as repairs rather than release-notes prose.
2. **Multi-floor map switching** ([#43](https://github.com/sjmotew/NarwalIntegration/issues/43)). The only substantial unimplemented request, now tractable through the `map_id` plumbing that landed with the room profiles.
3. **Last-clean sensors** ([#32](https://github.com/sjmotew/NarwalIntegration/issues/32)).
4. **Home Assistant 2027.8 deprecations.** `via_device` in the entity device info and `async_get_device` in setup both log deprecation warnings on current HA; both need replacing before that release.

## On hold

Researched and probed, parked pending further reverse engineering.

| Feature | Status | Blocker |
|---|---|---|
| **Camera snapshots** | Client method works (robot returns ~170 KB) | Image data is **AES-encrypted**; the key has to come out of the Narwal APK |
| **Camera LED control** | Partial response from robot | Correct payload format unconfirmed; needs idle-state testing |
| **Vision obstacle overlay** | Built, tested, removed | Robot broadcasts raw AI candidates (3 to 6× more than the app shows), not confirmed detections. Unusable for a map overlay |
| **Patrol / cruise mode** | Topics identified in APK | Not yet probed; depends on the camera working first |

## Open protocol questions, help wanted

Each is answerable with a capture rather than an argument. [PROTOCOL.md §12](PROTOCOL.md#12-contributing-captures)
explains how to take one.

- **What is `CleanParam` tag 8?** The Narwal app sends `8 = 2`; we never send it and cleaning works without it. The best current candidate is the app's two-value coverage-precision toggle ([#25](https://github.com/sjmotew/NarwalIntegration/issues/25)).
- **The complete `WorkingStatus` enum.** Values have been discovered one bug report at a time; `17` turned out to be the custom per-room clean and `2` is an active clean on Flow 2 v01.09.10. Anyone holding an APK `BuilderInfo` decode can end that ([#46](https://github.com/sjmotew/NarwalIntegration/issues/46)).
- **Does the Freo X Ultra speak the local protocol?** The compatibility table says no, per [#4](https://github.com/sjmotew/NarwalIntegration/issues/4). A product key and `nmap -p 9002` from an owner settles it either way.
- **Why does the plain Freo Z10 refuse port 9002?** It advertises over mDNS like every other model, then returns `ECONNREFUSED` ([#92](https://github.com/sjmotew/NarwalIntegration/issues/92)). Two units show the same signature, with 22 high ports open; an `nmap -sV` fingerprint and an app-to-robot capture would tell us what it listens on instead.
- **When does a Flow 2 report its clean-water and detergent tanks?** Nothing changes in `base_status` while docked and idle. A dump taken while the dock is washing with a tank removed would show whether the fields ever move ([#128](https://github.com/sjmotew/NarwalIntegration/issues/128)).

Answered since the list began: the fifth suction tier exists and is reachable on the Flow 2 but not from the AX26 app ([#70](https://github.com/sjmotew/NarwalIntegration/issues/70)); see PROTOCOL.md §11 for the detail.
