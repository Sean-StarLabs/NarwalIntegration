# v1.0.10 — Robots no longer vanish after a Home Assistant restart

On top of [v1.0.9](RELEASE-NOTES-v1.0.9.md). **No breaking changes.** One fix, found from a user's debug log within two days of being reported.

872 tests, up from 863.

---

## A discovered IPv6 address no longer kills the integration at startup

Closing [#101](https://github.com/sjmotew/NarwalIntegration/issues/101), reported by [@thorsten-gehrig](https://github.com/thorsten-gehrig) on a Freo Z Ultra (CX7).

After some Home Assistant restarts both the robot and dock devices came up **unavailable** and never recovered. Deleting the integration and adding the robot again worked immediately. It happened on some restarts and not others, which made it look like a timing problem with the CX7's polled-only startup. It was not.

The debug log showed setup crashing before the first frame was sent:

```
ValueError: Port could not be cast to integer value as '1170:789a:20:998a:c982:df15:ab22:9002'
```

Between two restarts the stored address of the robot had changed from its IPv4 to its IPv6 address. Three defects stacked:

1. **Discovery rewrote the host on every start.** mDNS discovery runs each time Home Assistant boots. When it recognised an already-configured robot it updated the stored address to whatever address it had just seen — and on a dual-stack network that is sometimes the AAAA record. A working IPv4 address was silently replaced by an IPv6 one.
2. **The WebSocket URL had no brackets.** `ws://<host>:9002` with an IPv6 literal parses the last group as the port, and the connection library refused it.
3. **The error was one the integration did not expect**, so it escaped setup entirely. Home Assistant marks such an entry as *failed* rather than *not ready*, and failed entries are never retried. That is why nothing short of delete + re-add recovered it.

**What changed.** Zeroconf discovery now prefers the IPv4 address when both families are advertised and never replaces a stored host with an IPv6 one. IPv6 literals are bracketed correctly in the URL, so a robot on an IPv6-only network can still be added. A URL the library cannot parse is reported as a connection failure, and any other unexpected exception during setup becomes a retry instead of a dead entry. An installation already stuck on an IPv6 host heals itself the next time discovery sees the robot's IPv4 address.

This is not CX7-specific. Any robot on a dual-stack network could hit it; the CX7 just happened to be the one whose owner kept restarting and kept logs.

---

## Also this cycle

- [#102](https://github.com/sjmotew/NarwalIntegration/issues/102) — [@fishscrounger](https://github.com/fishscrounger) has a fork branch with substantial Freo Z Ultra work: a way to drive the robot from Home Assistant when its polled state lags reality, battery health and charge-cycle data from `developer/get_robot_info`, and a large set of newly answered protocol topics. They also found that `get_robot_info` returns the Wi-Fi password in cleartext over the unauthenticated socket. Invited to submit as issue-backed PRs.
- [#95](https://github.com/sjmotew/NarwalIntegration/pull/95) — the fix for [#98](https://github.com/sjmotew/NarwalIntegration/issues/98) (Z10 Ultra stuck at `task_status: returning`) is still in draft.
- Two Home Assistant deprecation warnings appear in current logs (`via_device` and `async_get_device`). Both stop working in HA 2027.8 and are on the list.

## Thanks

[@thorsten-gehrig](https://github.com/thorsten-gehrig), who restarted three times, captured logs both when it worked and when it did not, and asked whether the files were right. They were.

872 tests, CI green on the release commit, deployed to a live Home Assistant instance and verified before tagging.
