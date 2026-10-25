# Setup details

The [README](../README.md#installation) covers the normal path: install from HACS, let
discovery find the robot, pick the model. This page is for everything that can go sideways
on the way there.

## Only one Narwal integration at a time

A separate project, [nadavbau/narwal-integration](https://github.com/nadavbau/narwal-integration),
also installs to `custom_components/narwal`, so HACS cannot hold both; installing one replaces
the other. Neither project can rename without orphaning its users' existing config entries and
entity ids, so this is expected rather than a bug you can work around. Pick whichever suits
your hardware ([#84](https://github.com/sjmotew/NarwalIntegration/issues/84)).

## How discovery finds the robot

The robot advertises `_narwal_sweeper._tcp.local.` over mDNS on port 9002. The six hex
characters in its name are the tail of the robot's device ID, which is how a discovery is
matched to a robot you already added manually.

The name's shape varies by model, so the **hostname** is what discovery reads:

| Model | Instance | Hostname |
|---|---|---|
| Flow (AX12) | `_app_wss_server_<6hex>` | `NARWAL_<6hex>.local.` |
| Freo Z Ultra (CX7) | `_app_ws_server` (no suffix) | `NARWAL_<6hex>-<3digits>.local.` |

On the CX7 the instance name carries no device-ID tail at all, so anything matching on the
instance rather than the hostname will not find it.

Some networks drop multicast between VLANs or under wireless client isolation, and mDNS then
never arrives. DHCP hostname matching covers that case. Home Assistant lowercases hostnames
before matching, so the declared pattern is `narwal_*`.

Discovery re-points an existing entry when the robot's address changes, but a static DHCP
lease avoids the round trip. Since v1.0.10 discovery prefers IPv4 and never downgrades a stored
IPv4 host to an IPv6 one ([#101](https://github.com/sjmotew/NarwalIntegration/issues/101)).

## Home Assistant and the robot on different VLANs

Robots have been reported not to answer connections whose source address is outside their own
subnet, even when 9002/TCP is explicitly permitted through the firewall and other devices on
the same VLAN are reachable. Both ICMP and the WebSocket handshake time out, so the symptom is
a plain connection failure rather than an integration error:

```text
Failed to connect to ws://<robot-ip>:9002: timed out during opening handshake
```

**Workaround:** source-NAT the traffic from Home Assistant so it reaches the robot with an
address on the robot's own subnet. Confirmed working by a user running Home Assistant on a
separate VLAN from an IoT network ([#81]).

**Why SNAT is the fix and not just a workaround.** Two causes could produce this: the robot
filtering by source subnet, or the robot ignoring its DHCP-supplied default gateway and so
having no route back. Packet captures in [#81] settle it as the first. On the router's IoT
interface, which every packet in this exchange must cross, the inbound SYN **is** visible
arriving at the robot, and **no SYN-ACK** comes back. The robot also demonstrably has a working
default route, since it reaches Narwal's cloud servers and those SYN-ACKs are visible outbound.
So it receives the connection and declines to answer, rather than answering into a black hole.
ICMP being dropped the same way fits a rule about the source address rather than anything
specific to 9002. That rules out a static route as a cleaner alternative: there is no reply to
route.

**Discovery across VLANs.** mDNS is multicast and most networks do not forward it between
VLANs, so by default the robot will not be discovered from another VLAN and you add it by IP.
If your router reflects or proxies mDNS across VLANs, discovery does work; reported end-to-end
on v1.0.5 with a reflector plus the SNAT above ([#81]). Note the asymmetry: the robot is happy
to *announce* itself over multicast and still refuses inbound unicast from a foreign subnet, so
discovery finding the robot and the connection then failing without SNAT is a normal
combination, not a contradiction.

[#81]: https://github.com/sjmotew/NarwalIntegration/issues/81

## Freo Z Ultra (CX7) Device ID

The Freo Z Ultra requires its 32-character cloud-assigned Device ID. Selecting that model opens
a dedicated Device ID page; other models use automatic discovery. The integration itself never
contacts the cloud.

### Finding the Device ID

The Narwal app does not display this value. It is the 32-character hexadecimal identifier used
as the second component of a Narwal MQTT topic:

```text
/<product_key>/<device_id>/status/working_status
```

You can obtain it from one of these sources:

- **A Narwal MQTT capture**, where it appears in the topic position shown above. This is the
  reliable route: the app's MQTT client does not verify the broker's certificate, so a relay
  with a self-signed certificate can read the topics. Point the broker hostname
  (`<region>-mqtt.narwaltech.com`, e.g. `eu-mqtt`) at a machine on your LAN, relay port 8883
  to the real broker, and reopen the app. The first frames carry
  `/<product_key>/<device_id>/...`. The app keeps a long-lived MQTT connection, so it must be
  **force-stopped** for it to re-resolve the hostname.
- The stored device identifier or diagnostics from an existing Narwal cloud integration.

> **The account API cannot give you this value.** Earlier revisions of this document pointed at
> `/user-device-platform-server/device-info/getDeviceInfoList`; that endpoint returns 404 on
> every Narwal host (`eu-app`, `us-app`, `il-app`, `cn-app`, `app`, `usaclient`, `universal`).
> The API has no endpoint that enumerates your devices at all. Every per-device route requires
> the `device_id` you are trying to find, and answers `err_code 101502`
> ("has no privilege of device") without it. Login itself is unsigned email + password against
> `/user-authentication-server/v2/login/loginByEmail`, and the auth header is `Auth-Token`;
> anything else returns `err_code 130105`.

A cross-check once you have it: the six hex characters in the robot's mDNS hostname are the
**tail of the device ID**, so `NARWAL_c07174-….local.` confirms an ID ending `c07174`.

Account and MQTT tooling is deliberately kept separate from this integration so Home Assistant
never receives your Narwal credentials. Do not post the Device ID publicly; treat it as a device
identifier even though it is not an account password or access token.

### CX7 variants

Local control is verified on the global Freo Z Ultra whose cloud identity is J5, product key
`hEA7OEshlx`, firmware `v01.13.11.02`. See [MODELS.md](MODELS.md#freo-z-ultra-hardware-cx7-cloud-identity-j5)
for the other reported identity and what is still unproven.
