---
gsd_state_version: 1.0
milestone: v0.5
milestone_name: Map Validation & Polish
status: "v1.0.3 RELEASED 2026-08-08 — #73 root-caused and fixed on hardware; queue drained"
stopped_at: "**v1.0.3 published 2026-08-08** (latest, manifest 1.0.3). #73 root cause found on hardware during a live Pantry clean: the active_robot_publish subscription lasts 600s and was never renewed, so working_status/display_map stopped and the entity froze at docked. Renewal now runs unconditionally every 240s. Counts: expired 423/1/1 -> renewing 411/148/148. #73 CLOSED. v1.0.2 notes corrected in repo and on the release. Announced on #37/#55/#66/#40."
last_updated: "2026-08-08T00:00:00.000Z"
last_activity: 2026-08-08
progress:
  total_phases: 9
  completed_phases: 7
  total_plans: 15
  completed_plans: 13
  percent: 78
---

# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-03-01)

**Core value:** Users can control and monitor their Narwal Flow vacuum entirely locally — start/stop/pause, see status, view a live floor map — without any cloud dependency.
**Current focus:** Phase 15 — room-clean rewrite via clean/start_clean + consolidating four contributor forks (issue #66)

## Current Position

Phase: 15 (room-clean-rewrite-and-fork-consolidation) — in ROADMAP.md as of 2026-08-07
Last activity: 2026-08-07

**RESUME HERE:** see `.planning/.continue-here.md` (HANDOFF.json consumed and deleted)

Progress: Phase 15 executed as issue/PR work, not formal plans — 8 of 9 merge steps done, v1.0.2 pending

## Accumulated Context

### Roadmap Evolution

- Phase 15 added: Room-Clean Rewrite and Fork Consolidation (backfilled 2026-08-07; ran as maintainer work from 2026-07-27)

### Key Decisions (Phase 8)

- Entity availability uses coordinator.last_update_success, not client.connected
- 5 consecutive poll failures before marking unavailable (~5 min grace period)
- Removed client.connect() from poll loop to avoid racing with listener
- Mock HA framework via sys.modules stubs (ha_stubs.py) instead of pytest-homeassistant-custom-component
- Test config flow with __new__ + mocked base methods for isolated async_step_user testing

### Key Decisions (Phase 7)

- Coordinate transform: factor 1.0, pixel = raw - origin (no scaling)
- is_returning requires BOTH field 3.7 AND 3.10 (prevents false positives)
- Room data is 100% local — ROOM_TYPE enum + instance_index for names
- Furniture obstacles are LOCAL (field 2.32, typeId = APK furniture enum). Vision obstacles likely also local — needs probing during active clean.
- Trail segment breaks are obstacle avoidance, not a rendering bug (deferred)
- Label overlap matches Narwal app behavior (not an issue to fix)

### Key Decisions (Phase 9)

- Room IDs encoded as repeated varint in field 1.2 of CleanTask protobuf
- Segment.group uses Rooms/Utility based on RoomInfo.category
- Empty room_ids in start_rooms() falls back to whole-house clean
- Bare roomId in field 1.2 is IGNORED by robot; each room entry needs full MapCleanParamInfo fields (cleanMode=2, cleanTimes=1, sweepMode=3, mopMode=2)
- Room-clean response returns code=0 with config data (not usual code=1 ack)

### Pending Todos

- "Self test paused" unmapped working_status
- CleanTask payload hardcodes max suction / wet mop / single pass
- Validate: does start work WITHOUT CleanTask payload?

### Key Decisions (Phase 10)

- Obstacle positions are LOCAL (field 2.32), not cloud-only — corrects Phase 7 assumption
- typeId IS the specific furniture enum from APK map_furniture.json (NOT category codes — corrected after user validation)
- Pass obstacles + origin to render_base_map (not pre-computed grid coords)
- Obstacles render on base map (static, cached) not overlay
- Skip rotation for v1 — axis-aligned rectangles sufficient

### Key Decisions (Phase 11)

- Vision obstacle data source: display_map field 9 (confirmed by probe in plan 11-01)
- detection_seq (field 2) used as dedup ID — robot provides unique incrementing counter
- VisionObstacleInfo is a separate dataclass from ObstacleInfo — different lifecycle and data source
- render_overlay extended with backward-compatible vision_obstacles + origin_x/origin_y params (default None/0)
- Vision obstacles cleared in _reset_trail() — same lifecycle hook as trail (fires on new cleaning session)
- Field 9 has type_id + detection_seq but NOT coordinates — field 12 coordinate parsing deferred

### Key Decisions (Phase 12, Plan 01)

- Snapshot camera is_streaming=False — privacy-first, only fires on explicit button press or service call
- AES-encrypted images stored as raw bytes until APK decryption key extracted — NarwalSnapshotCamera will not display correctly until future plan
- HA test stubs for ButtonEntity/SwitchEntity/Camera use plain class stubs (not MagicMock) to avoid __setattr__ MRO conflicts when entities are instantiated in tests
- Service registered idempotently via has_service guard in async_setup_entry

### Key Decisions (Phase 13, Plan 01)

- X10 Pro (AX15/CNbforyZWI) inserted before "Other / Auto-detect" to preserve auto as last dropdown entry
- Room clean warning embeds per-code guidance text (CONFLICT/NOT_APPLICABLE) for actionable debugging without source knowledge
- FIX-01 (ba53ddb) was pre-committed — plan correctly noted as no-op, not redone

### Blockers/Concerns

- ~~**PR #49 likely fails on our firmware (v01.08.03.07).**~~ **RESOLVED 2026-08-06, MERGED 2026-08-07 (`05af870`).** @Zebble ran #49 on a Flow 1 (AX12) at v01.08.03.07 — our exact rig: two-room clean, SUCCESS first attempt, correct rooms and order, ~35 min, no errors. `ZoneOption` field 4 is **not** required on any known firmware. Second confirmation after @shin906710 (AX26, v01.02.00.15). We never had to move the robot.
- #37 **closed** 2026-08-07.
- Open, non-blocking: is `FanLevel.DEEP` a real fifth suction tier, or does the label need model-gating? AX26's app UI shows four (#70). `CleanParam` tag 8 still unexplained (#25).
- Still conflicting and awaiting contributor rebases: #50, #24, #35.

### Key Decisions (Phase 15)

- REVERSED: `clean/plan/start` is a plan-runner that discards payloads; `clean/start_clean` is the real room-clean command. Confirmed independently by three contributors. ~2 months of payload-schema work was against the wrong topic.
- REVERSED: `ROOM_TYPE_NAMES` is misaligned from index 5 for all models; room naming takes no model argument. The Flow 2 override map (5b4dac7) was a band-aid and PR #48 deletes it.
- @jgus's stack (#47-#54) is the merge base over @Sean-StarLabs's larger #58-#65 stack — one concern per PR beats take-it-or-leave-it.
- Declined `cloud.py` (#65) on "fully local, no cloud" positioning.
- Shipped README known-broken warnings before the fixes merged (e3aef09).

## Session Continuity

Last session: 2026-09-04
Stopped at: Resumed via /gsd:resume-work. #81 root-caused and fixed, #92 answered, PR #86 merged, #87-#91 stack reviewed. Master 2ad8b68, 422 tests, CI green, unreleased.
Resume file: .planning/.continue-here.md
