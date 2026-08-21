# Narwal Robot Vacuum — Home Assistant Integration

A fully **local, cloud-independent** [Home Assistant](https://www.home-assistant.io/) custom integration for Narwal robot vacuums. Communicates directly with your vacuum over your local network via WebSocket — no cloud account or internet connection required.

> **Latest release: [v1.0.4](https://github.com/sjmotew/NarwalIntegration/releases/tag/v1.0.4)** (HACS) — **consumable alerts never worked before this release** and always reported "no problem"; check those two entities after upgrading ([notes](docs/RELEASE-NOTES-v1.0.4.md)). Adds Freo Z Ultra (CX7) support and renames the fan tiers to Quiet/Standard/Strong/Super/Ultra. **Coming from v1.0.1 or earlier? [Read the three breaking changes](docs/RELEASE-NOTES-v1.0.2.md) first.**

> ### ✅ Room cleaning is fixed — shipped in v1.0.2, verified on hardware in v1.0.3
>
> Community reverse-engineering found that **room-specific cleaning had never worked**. The integration sent clean commands to `clean/plan/start`, which is `StartWithPlan{planId, mapId}` — it runs the plan stored in the Narwal app and **discards the rooms we send**, while still returning success. That is why every previous fix appeared to work and changed nothing. `clean/start_clean` is the correct command.
>
> Found independently by [@jgus](https://github.com/sjmotew/NarwalIntegration/pull/49), [@Sean-StarLabs](https://github.com/sjmotew/NarwalIntegration/pull/58) and [@sytchi](https://github.com/sjmotew/NarwalIntegration/issues/37). Merged as [#49](https://github.com/sjmotew/NarwalIntegration/pull/49); [#37](https://github.com/sjmotew/NarwalIntegration/issues/37) is closed.
>
> **Confirmed on hardware, on two independent firmware lines:**
>
> | Reporter | Model | Firmware | Result |
> |---|---|---|---|
> | [@shin906710](https://github.com/sjmotew/NarwalIntegration/issues/70) | Freo Z10 Pro (AX26) | v01.02.00.15 | Two single-room cleans, each cleaned only the selected room |
> | [@Zebble](https://github.com/sjmotew/NarwalIntegration/pull/49) | Flow (AX12) | v01.08.03.07 | Two rooms, correct rooms, correct order, first attempt, ~35 min run |
>
> ### ⚠️ Upgrading from v1.0.1 — three breaking changes
>
> Full notes: [`docs/RELEASE-NOTES-v1.0.2.md`](docs/RELEASE-NOTES-v1.0.2.md). Read this before upgrading:
>
> - **Room names changed** ([#48](https://github.com/sjmotew/NarwalIntegration/pull/48)). The room-type table was wrong for every model. If you built automations or scripts on the old (incorrect) names, expect to redo those mappings.
> - **Fan speed values and tiers changed** ([#49](https://github.com/sjmotew/NarwalIntegration/pull/49)). The suction scale was off by one tier for this project's entire history. The list is now the app's own tiers — Quiet, Standard, Strong, Super, Ultra. Your existing `quiet` / `normal` / `strong` / `max` automations keep working as aliases, but they now map to the correct tier, so **actual suction may differ from what you were getting**. (v1.0.2 and v1.0.3 spelled the top two "Super powerful" / "Ultra powerful"; both spellings are still accepted.)
> - **`vacuum.start` now requires the dock** ([#69](https://github.com/sjmotew/NarwalIntegration/issues/69)). Whole-house start goes through `clean/start_clean` and cleans every room instead of re-running the robot's saved plan. That command only works from the dock, so starting off-dock now returns `NOT_READY` instead of appearing to succeed — a real failure surfacing, since the old path was not starting the clean either.
>
> You will also see **many more entities** — 28 on a Flow, up from 9 — as clean settings, consumable alerts, map options and the dock light become HA entities. Verified on hardware (AX12, v01.08.03.07).
>
> **Fixed in v1.0.3:** the vacuum entity used to freeze at `docked` mid-clean, with the live map stuck and `cleaning_area` / `cleaning_time` never populating ([#73](https://github.com/sjmotew/NarwalIntegration/issues/73)). The robot only broadcasts `working_status` and `display_map` while a subscription is live, that subscription expires after 600 s, and nothing renewed it. Reproduced and fixed on hardware during a real room clean. **v1.0.2 does not contain this fix.**

## Device Compatibility

This integration uses a **local WebSocket connection on port 9002**. Only models that expose this port are supported.

| Model | Status | Notes |
|-------|--------|-------|
| **Narwal Flow** (AX12) | **Working** | Primary development target. Room cleaning confirmed on firmware v01.08.03.07 with [#49](https://github.com/sjmotew/NarwalIntegration/pull/49). On v01.07.22+, `vacuum.start` needs a loaded map ([#36](https://github.com/sjmotew/NarwalIntegration/issues/36)). |
| **Narwal Flow 2** (QxMSPG6VSO) | **Working** | Room cleaning fixed by [#49](https://github.com/sjmotew/NarwalIntegration/pull/49); on v1.0.1 see the warning above before using `vacuum.clean_area` |
| **Freo Z10 Ultra** (CX4) | **Working** | Community confirmed |
| **Freo Z10 Pro / Turbo** (AX26) | **Working** | Same product key and firmware (v01.02.00.15) reported under both names ([#40](https://github.com/sjmotew/NarwalIntegration/issues/40), [#70](https://github.com/sjmotew/NarwalIntegration/issues/70)). Room cleaning confirmed working with [#49](https://github.com/sjmotew/NarwalIntegration/pull/49). |
| **Freo X10 Pro** (AX15) | **Working** | Community confirmed ([#12](https://github.com/sjmotew/NarwalIntegration/issues/12)) |
| **Narwal JX** | **Unconfirmed** | Product key known, no working report yet — testers welcome ([#42](https://github.com/sjmotew/NarwalIntegration/issues/42)) |
| **Freo Z Ultra** (hardware CX7, cloud identity J5) | **Working on tested variant** | Confirmed with product key `hEA7OEshlx` on firmware `v01.13.11.02`. Requires the cloud-assigned Device ID because this model does not broadcast. Base status, maps, consumables, and commands work locally; live cleaning position/progress is unavailable. See the variant note below. |
| **Freo X Ultra** (AX18/AX19) | **Not Compatible** | Uses ZeroMQ (port 6789) + Tuya cloud, not WebSocket ([#4](https://github.com/sjmotew/NarwalIntegration/issues/4)) |
| **Freo X Plus** | **Not Compatible** | Cloud-only — no local API |
| **Narwal J-series** (J1/J4) | **Not Compatible** | J1: HTTP-only (port 8080); J4: cloud-only (Tuya). J5 is the cloud identity of the supported global CX7 listed above. |

Models marked **Not Compatible** use a different protocol or are cloud-only. This is a hardware/firmware limitation.

**Other models?** Check with `nmap -p 9002 <your-vacuum-ip>`. If open, [open an issue](https://github.com/sjmotew/NarwalIntegration/issues/new/choose) with your model and results.

## Features

### Vacuum Control
- **Start / Stop / Pause / Resume** — validated on hardware (see the note above for `start` on newer Flow firmware)
- **Return to dock** / **Locate** (robot announces "Robot is here")
- **Fan speed** — Quiet, Standard, Strong, Super, Ultra (set-only; robot doesn't broadcast current level). Ultra is not offered on the Freo Z10 Pro / Turbo (AX26), where the app's own top tier is Super and value 5 is unreachable ([#70](https://github.com/sjmotew/NarwalIntegration/issues/70)). On v1.0.1 these are `quiet` / `normal` / `strong` / `max` and are off by one tier — see the breaking-change note above
- **Room-specific cleaning** — exposed in the HA UI (requires HA 2026.3+ and a segment-to-area mapping, see Known Limitations). **Fixed in v1.0.2** ([#49](https://github.com/sjmotew/NarwalIntegration/pull/49)); broken in v1.0.1 and earlier

### Clean Settings
Shipped in v1.0.2 ([#50](https://github.com/sjmotew/NarwalIntegration/pull/50)) — applied to both room and whole-house cleans, which previously hardcoded max suction / wet mop / single pass:
- **Work mode** — vacuum, mop, vacuum then mop, vacuum and mop
- **Water level** — dry, normal, wet
- **Mop strength** — normal, high
- **Passes** — 1 to 3

### Sensors
- Battery level, cleaning time, firmware version
- Docked status (binary sensor), charging state (Charging / Fully Charged / Not Charging)
- Cleaning area — reports real covered area as of v1.0.1 ([#51](https://github.com/sjmotew/NarwalIntegration/pull/51))
- Current room being cleaned ([#24](https://github.com/sjmotew/NarwalIntegration/pull/24), v1.0.2)
- Last clean result — why the previous task ended ([#53](https://github.com/sjmotew/NarwalIntegration/pull/53), v1.0.2)
- Dust bag health and detergent remaining ([#52](https://github.com/sjmotew/NarwalIntegration/pull/52), v1.0.2)
- Station and consumable binary sensors — clean water tank, sewage tank, dust box, dust bag, station bag, error ([#52](https://github.com/sjmotew/NarwalIntegration/pull/52), v1.0.2)
- Maintenance and replacement alerts, with the affected parts listed as attributes ([#54](https://github.com/sjmotew/NarwalIntegration/pull/54), v1.0.2)

### Live Map
- Color-coded floor plan with room labels (all rooms — user-named and auto-generated)
- Furniture/obstacle overlay from the robot's stored map data
- Dock marker and live robot trail during cleaning (~1.5s refresh)
- Carpet-map debug image as a second camera ([#67](https://github.com/sjmotew/NarwalIntegration/pull/67), v1.0.2)
- Display toggles for room labels, furniture and furniture labels ([#62](https://github.com/sjmotew/NarwalIntegration/pull/62), v1.0.2)

### Dock
- **Ambient light** — off, fireplace, nightlight, purple, on models with a dock light ([#61](https://github.com/sjmotew/NarwalIntegration/pull/61), v1.0.2)

### Connectivity
- Real-time WebSocket push updates on broadcasting models
- Auto-reconnect with exponential backoff
- Wake system for sleeping robots + keepalive heartbeat
- 60-second polling fallback

## Installation

### HACS (Recommended)

1. Open **HACS** > three-dot menu > **Custom repositories**
2. Add: `https://github.com/sjmotew/NarwalIntegration` (category: Integration)
3. Find **Narwal Flow Robot Vacuum** and click **Download**
4. **Restart Home Assistant**

### Manual

1. Copy `custom_components/narwal/` to your HA `config/custom_components/` directory
2. **Restart Home Assistant**

### Setup

Home Assistant discovers Narwal robots on the local network, so in most cases the
robot appears on its own under **Settings > Devices & Services** as a discovered
device — click **Configure**, pick your model, and you're done. The IP is filled in
for you.

To add one by hand, or if discovery doesn't find it:

1. **Settings > Devices & Services > Add Integration** > search "Narwal"
2. Enter your vacuum's IP address and select your model
3. Entities are created automatically

> **Tip:** Assign a static IP to your vacuum in your router. Discovery re-points an
> existing entry when the address changes, but a static lease avoids the round trip.

<details>
<summary>How discovery finds the robot</summary>

The robot advertises `_narwal_sweeper._tcp.local.` over mDNS, as an instance named
`_app_wss_server_<6hex>` with hostname `NARWAL_<6hex>.local.` on port 9002. Those six
hex characters are the tail of the robot's device ID, which is how a discovery is
matched to a robot you already added manually.

Some networks drop multicast between VLANs or under wireless client isolation, and
mDNS then never arrives. DHCP hostname matching covers that case — Home Assistant
lowercases hostnames before matching, so the declared pattern is `narwal_*`.

</details>

<details>
<summary>If Home Assistant and the robot are on different VLANs</summary>

Robots have been reported not to answer connections whose source address is outside
their own subnet, even when 9002/TCP is explicitly permitted through the firewall and
other devices on the same VLAN are reachable. Both ICMP and the WebSocket handshake
time out, so the symptom is a plain connection failure rather than an integration
error:

```text
Failed to connect to ws://<robot-ip>:9002: timed out during opening handshake
```

**Workaround:** source-NAT the traffic from Home Assistant so it reaches the robot
with an address on the robot's own subnet. This is confirmed working by a user running
Home Assistant on a separate VLAN from an IoT network ([#81]).

The underlying cause is not yet pinned down — it is either the robot filtering by
source subnet, or the robot ignoring the default gateway it was handed by DHCP and so
having no route back. If you can capture on the robot's own switch port and tell us
whether the robot *replies* to an off-subnet SYN, that distinguishes the two, and in
the second case a static route on the robot's side would be a cleaner fix than SNAT.
Please add what you find to [#81].

Note that mDNS discovery is separately affected: most networks do not forward
multicast between VLANs, so the robot will usually need to be added by IP in this
setup regardless.

[#81]: https://github.com/sjmotew/NarwalIntegration/issues/81

</details>

The Freo Z Ultra (CX7) also requires its 32-character cloud-assigned Device ID. Selecting that
model opens a dedicated Device ID page; other models use automatic discovery. The integration
itself never contacts the cloud.

#### Finding the Device ID

The Narwal app does not currently display this value. It is the 32-character hexadecimal
identifier used as the second component of a Narwal MQTT topic:

```text
/<product_key>/<device_id>/status/working_status
```

You can obtain it from one of these sources:

- The `deviceId` field returned by Narwal's authenticated account endpoint
  `/user-device-platform-server/device-info/getDeviceInfoList`.
- A Narwal MQTT capture, where it appears in the topic position shown above.
- The stored device identifier or diagnostics from an existing Narwal cloud integration.

Account and MQTT tooling is deliberately kept separate from this integration so Home Assistant
never receives your Narwal credentials. Do not post the Device ID publicly; treat it as a device
identifier even though it is not an account password or access token.

#### CX7 variants

Local control is currently verified on one global Freo Z Ultra whose cloud identity is J5,
product key is `hEA7OEshlx`, and firmware is `v01.13.11.02`. Issue
[#5](https://github.com/sjmotew/NarwalIntegration/issues/5) also contains reports of firmware
`1.12.10.02` and a `BYWBPqSxeC` identity. That key remains in discovery coverage, but has not
been proven to accept addressed local commands. Reports from additional regions and firmware
versions are needed before support can be considered universal.

### Room cleaning setup (required before `vacuum.clean_area` works)

Home Assistant's room cleaning targets **HA areas**, not the robot's own rooms, so there is a one-time mapping step. Without it the service fails with *"Area mapping is not configured for vacuum.&lt;entity&gt;"*.

1. Create a Home Assistant **area** for each room you want to clean (Settings → Areas & Zones), if you don't already have one.
2. Open the **mapping editor** and match each robot segment to its area (see below).
3. `vacuum.clean_area` can then target those areas, and the robot cleans the matching rooms.

#### Where the mapping editor lives

It hangs off the **entity**, not the integration. There is **no such option on the integration page or the device page** — that is the most common place people look and it is not there.

Any of these three routes opens the same editor:

| Route | Where |
|---|---|
| **Entity settings** (most reliable) | Settings → Devices & Services → **Entities** tab → search your vacuum → open it → **cog icon** → *Map vacuum segments to areas* |
| **First-run prompt** | Open the vacuum → **Clean areas** → **Configure** (only shown while no mapping exists, and only to admins) |
| **Header action** | Open the vacuum → **Clean areas** → header action (use this one once a mapping already exists) |

Home Assistant shows the option when the entity's domain is `vacuum` and it advertises the `CLEAN_AREA` feature. This integration sets that flag, so if the row is missing:

- **Hard-refresh the browser** (<kbd>Ctrl</kbd>+<kbd>Shift</kbd>+<kbd>R</kbd>) — the frontend caches aggressively.
- **Check the entity is not `unavailable`.** The row is gated on a live state object, so it will not render while the robot is unreachable.
- Requires **Home Assistant 2026.3+**, when area mapping was introduced.

Room names come from the robot's map — rooms you named in the Narwal app keep those names, and the rest use the shared room-type table corrected in [#48](https://github.com/sjmotew/NarwalIntegration/pull/48). **Name your rooms in the Narwal app before mapping**: the mapping keys on segment *id*, so renaming later is safe, but naming first means your HA areas, the robot's map and `sensor.current_room` all read the same words.

`cleaning_area_id` accepts an **ordered list**, so you can clean several rooms in one job and the robot follows the order you picked.

> **Remapping resets this.** A fresh full-house map in the Narwal app renumbers segments, which invalidates the mapping. The integration detects the change and raises a Home Assistant repair issue so you know to redo it.

## Requirements

- Narwal vacuum on the same local network as Home Assistant
- Port 9002 reachable (no firewall blocking)
- Home Assistant 2025.1.0+ / Python 3.12+

## Known Limitations

- **Wake from deep sleep is unreliable** — robot may not respond after long idle periods. Opening the Narwal app briefly can help.
- **Single connection** — close the Narwal app before using HA to avoid conflicts.
- **CX7 has no live stream** — it never broadcasts, so cleaning position and progress do not update live. Polled base status, battery, dock state, maps, consumables, and commands remain available. State follows the 60-second poll, so the vacuum entity reaches `cleaning` up to a minute after the robot starts (31 s in a recorded run), and `cleaning_time`, `cleaning_area` and `current_room` stay `unknown` throughout a clean because they are only carried in broadcasts.
- **Fan speed is set-only** — robot doesn't broadcast its current level.
- **All cleaning requires the dock** — `clean/start_clean` returns `NOT_READY` if the robot is not docked when the command is sent. This applies to whole-house `vacuum.start` as well as room cleans.
- **Room cleaning needs a segment-to-area mapping** — `vacuum.clean_area` targets Home Assistant *areas*, not robot rooms, and the mapping editor is on the **entity**, not the integration or device page. See [Room cleaning setup](#room-cleaning-setup-required-before-vacuumclean_area-works). Without it the service fails with "Area mapping is not configured".
- **Only one floor at a time** — the integration uses the robot's *active* map, and never enumerates the others. On a multi-floor home only the rooms of the current map are visible to Home Assistant, and HA floors are not related to robot maps ([#43](https://github.com/sjmotew/NarwalIntegration/issues/43)).
- **Map may be stale** — robot can return an old map. A new clean cycle typically refreshes it.

## Future Features (On Hold)

These features have been researched and probed but are **on hold** pending further reverse engineering:

| Feature | Status | Blocker |
|---------|--------|---------|
| **Camera snapshots** | Client method works (robot returns ~170KB) | Image data is **AES-encrypted** — APK reverse engineering needed for decryption key |
| **Camera LED control** | Partial response from robot | Correct payload format unconfirmed; needs idle-state testing |
| **Vision obstacle overlay** | Built, tested, and removed | Robot broadcasts raw AI candidates (3-6x more than app shows), not confirmed detections. Unusable for map overlay. |
| **Patrol / cruise mode** | Topics identified in APK | Not yet probed; depends on camera working first |

Camera snapshot and LED entities will be added once the AES decryption key is extracted from the Narwal APK.

## Troubleshooting

| Problem | Solution |
|---------|----------|
| "Cannot connect" during setup | Verify IP and that port 9002 is reachable. If it still fails, **open the Narwal app on your phone the moment you press Submit** — a sleeping robot may not answer within the setup timeout ([#40](https://github.com/sjmotew/NarwalIntegration/issues/40)). |
| Room cleaning runs the wrong rooms | Fixed in v1.0.2 ([#49](https://github.com/sjmotew/NarwalIntegration/pull/49)). If you are still on v1.0.1 or earlier this is expected — upgrade. |
| Room clean returns `NOT_READY` | `clean/start_clean` only works from the dock. Send the robot home first, then start the room clean. |
| Entities show "Unavailable" | Robot may be asleep. Open Narwal app briefly to wake it. |
| Map not showing | Map loads after robot wakes. A new clean refreshes a stale map. |
| Commands not responding | Close the Narwal app — only one WebSocket connection at a time. |
| Z10 Ultra disconnects | Re-add the integration with the correct model selected. |

## Project Status

**Where things stand — updated 2026-08-17, at the v1.0.4 release.**

**v1.0.4 is released** — everything below is shipped to HACS. 255 tests passing, CI green, and the integration deployed to a live Home Assistant instance and verified against real hardware before tagging. **Open PRs: [#78](https://github.com/sjmotew/NarwalIntegration/pull/78) (network discovery, awaiting review) and [#35](https://github.com/sjmotew/NarwalIntegration/pull/35).**

| Merged in v1.0.4 | What it does |
|---|---|
| [#80](https://github.com/sjmotew/NarwalIntegration/pull/80) | **Consumable alerts actually report** — the lists were packed varints and had always been discarded, so both alert sensors said "no problem" on every robot, always. See [#79](https://github.com/sjmotew/NarwalIntegration/issues/79) |
| [#76](https://github.com/sjmotew/NarwalIntegration/pull/76) | **Freo Z Ultra (CX7) local control**, via a pasted Device ID — still no cloud. Polling only; no live map position |
| — | Fan tiers renamed to Quiet/Standard/Strong/Super/Ultra; Ultra withheld on AX26, where it silently applied Strong ([#70](https://github.com/sjmotew/NarwalIntegration/issues/70)) |
| — | `CleanParam` tag 8 identified as the coverage-precision toggle; consumables documented in [`docs/PROTOCOL.md`](docs/PROTOCOL.md) |

| Merged since v1.0.1 | What it does |
|---|---|
| [#49](https://github.com/sjmotew/NarwalIntegration/pull/49) | **Room cleaning via `clean/start_clean`** — the headline fix. Closes [#37](https://github.com/sjmotew/NarwalIntegration/issues/37) |
| [#48](https://github.com/sjmotew/NarwalIntegration/pull/48) | Room-type names taken from the app's own strings. Closes [#22](https://github.com/sjmotew/NarwalIntegration/issues/22) |
| [#50](https://github.com/sjmotew/NarwalIntegration/pull/50) | Clean settings as HA entities — work mode, water, mop strength, passes |
| [#63](https://github.com/sjmotew/NarwalIntegration/pull/63) | Interprets live `working_status` telemetry rather than a stale `base_status` |
| [#73](https://github.com/sjmotew/NarwalIntegration/issues/73) | **v1.0.3** — renews the broadcast subscription before it lapses, so `working_status` and `display_map` keep arriving and the entity stops freezing at `docked` |
| [#62](https://github.com/sjmotew/NarwalIntegration/pull/62) | Map rendering options as switches — room labels, furniture, furniture labels |
| [#61](https://github.com/sjmotew/NarwalIntegration/pull/61) | Dock ambient light entity, on models that have one |
| [#24](https://github.com/sjmotew/NarwalIntegration/pull/24) | `sensor.current_room` — the room being cleaned right now |
| [#53](https://github.com/sjmotew/NarwalIntegration/pull/53) / [#54](https://github.com/sjmotew/NarwalIntegration/pull/54) | Last-clean-result sensor; consumable maintenance and replacement alerts |
| [#52](https://github.com/sjmotew/NarwalIntegration/pull/52) | `base_status` field audit; station and consumable diagnostics |
| [#67](https://github.com/sjmotew/NarwalIntegration/pull/67) | Carpet-map camera image; `working_status 7` mapped to remapping |
| [#72](https://github.com/sjmotew/NarwalIntegration/pull/72) | Unknown status values warn once instead of flooding the log. Closes [#46](https://github.com/sjmotew/NarwalIntegration/issues/46) |
| [#71](https://github.com/sjmotew/NarwalIntegration/pull/71) | asyncio deprecation fix for Python 3.12 |
| [#47](https://github.com/sjmotew/NarwalIntegration/pull/47) | Config-flow translation sync |
| [#69](https://github.com/sjmotew/NarwalIntegration/issues/69) | `vacuum.start` routes through `clean/start_clean` instead of silently no-opping |
| — | AX26 in the model selector ([#40](https://github.com/sjmotew/NarwalIntegration/issues/40), [#70](https://github.com/sjmotew/NarwalIntegration/issues/70)); Narwal JX product key ([#42](https://github.com/sjmotew/NarwalIntegration/issues/42)); [`docs/PROTOCOL.md`](docs/PROTOCOL.md) published |

### Next steps

1. **Local discovery** ([#35](https://github.com/sjmotew/NarwalIntegration/pull/35)) — zeroconf and DHCP discovery is the largest outstanding UX win, since [#40](https://github.com/sjmotew/NarwalIntegration/issues/40) shows setup failing outright on the wake timeout. Awaiting a narrowed PR.

### Open protocol questions — help wanted

- ~~**Is there a fifth suction tier?**~~ **Answered** ([#70](https://github.com/sjmotew/NarwalIntegration/issues/70)) — the AX26 app's top tier sends `4` (`DEEP`), so the five-value enum is right and `SUPER` (5) is unreachable there. Ultra is now withheld on that model. Still unknown whether any model exposes 5.
- **What is `CleanParam` tag 8?** The Narwal app sends `8 = 2`; we never send it and cleaning works without it. The best current candidate is the app's two-value coverage-precision toggle ([#25](https://github.com/sjmotew/NarwalIntegration/issues/25)).
- **The complete `WorkingStatus` enum.** Values have been discovered one user bug report at a time. Anyone holding an APK `BuilderInfo` decode can end that ([#46](https://github.com/sjmotew/NarwalIntegration/issues/46)).
- **Narwal JX confirmation.** The product key is known; no working report yet ([#42](https://github.com/sjmotew/NarwalIntegration/issues/42)).

## Reporting Issues

Use the [issue templates](https://github.com/sjmotew/NarwalIntegration/issues/new/choose) — they collect your HA version, model, and debug logs for faster diagnosis.

## Protocol Documentation

[**docs/PROTOCOL.md**](docs/PROTOCOL.md) documents the local WebSocket protocol — frame format, topic reference, message field maps, and the open questions. It also records the assumptions this project got wrong and how they were caught, which is the part most likely to save someone else time.

Corrections and captures are welcome; the doc explains how to take them.

## Disclaimer

This is an **unofficial**, community-developed integration — not affiliated with or endorsed by Narwal. The local protocol was reverse-engineered from network traffic and the Narwal mobile application.

- **Use at your own risk.** No warranty.
- **No cloud dependency.** No external data transmission.
- **Firmware updates** from Narwal may break this integration at any time.

## Contributing

Contributions and testing welcome! If you have a non-Flow Narwal model, testing reports are especially valuable.

## License

MIT
