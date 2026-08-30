#!/usr/bin/env python3
"""Record + watch annotated Narwal broadcasts for protocol RE.

Streams `ha core logs --follow` from a Home Assistant host over SSH,
filters for `DUMP <topic>: <decoded>` lines emitted by the integration
when run with debug logging, and interleaves user-typed annotations
into the output. Captures land in JSONL so they're trivially diffable
and replayable.

Two complementary subcommands, made to run in two separate terminals:

    # Terminal A — annotations + capture (low-latency typing)
    python3 narwal_capture.py record --host root@192.168.178.3 \
        --ssh-key ~/.ssh/openclaw_ha_ed25519 \
        --out captures/session.jsonl

    # Terminal B — live decoded-state dashboard
    python3 narwal_capture.py dashboard --host root@192.168.178.3 \
        --ssh-key ~/.ssh/openclaw_ha_ed25519

Plus offline analysis helpers:

    python3 narwal_capture.py diff captures/before.jsonl captures/after.jsonl
    python3 narwal_capture.py replay captures/session.jsonl

The integration must already be running with debug logging:
    service: logger.set_level
    data:
      custom_components.narwal: debug
      custom_components.narwal.narwal_client: debug

This works because the client logs every decoded broadcast as
`DUMP <topic>: <repr>` at DEBUG level, which `ha core logs --follow`
streams over the supervisor API.
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import json
import re
import shlex
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Iterator, TextIO

# Strips the ANSI colour codes that `ha core logs` wraps each line in.
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# Matches lines like:
#   2026-05-04 23:55:38.917 DEBUG (MainThread) [custom_components.narwal.narwal_client.client] DUMP status/working_status: {...}
DUMP_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) "
    r"DEBUG .*?\[custom_components\.narwal\.[\w.]+\] "
    r"DUMP (?P<topic>\S+): (?P<payload>.+)$"
)


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def _parse_payload(raw: str) -> Any:
    """Best-effort parse of the broadcast repr.

    blackboxprotobuf decodes broadcasts to dicts of int/str/bytes; the
    integration logs them with `%r`, which is a Python literal we can
    round-trip via ast.literal_eval. Falls back to the raw string if
    something more exotic shows up.
    """
    try:
        return ast.literal_eval(raw)
    except (SyntaxError, ValueError):
        return raw


def _iter_log_stream(host: str, ssh_key: str | None = None) -> Iterator[str]:
    """Yield decoded log lines from `ha core logs --follow` over SSH."""
    cmd = ["ssh"]
    if ssh_key:
        cmd.extend(["-i", str(Path(ssh_key).expanduser())])
    cmd.extend([host, "ha core logs --follow"])
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        bufsize=1, text=True,
    )
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            yield ANSI_RE.sub("", line.rstrip("\n"))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


# Topics whose changes we never report — they're either pure noise
# (display_map fires every ~1.5s during cleaning, download_status is
# usually a constant `2`) or reach the change-detector via a different
# topic (working_status duplicates many robot_base_status sub-fields).
_NOISY_TOPICS: frozenset[str] = frozenset({
    "map/display_map",
    "status/download_status",
    "upgrade/upgrade_status",
})

# Top-level robot_base_status keys whose values change every broadcast
# without anything semantic happening (battery jitter, monotonic
# timestamps, session ids). Filtering them keeps the change-hint
# signal-to-noise ratio high.
_NOISE_BASE_KEYS: frozenset[str] = frozenset({
    "2",   # battery float32 — jitters slightly each tick
    "13",  # session id (string)
    "35",  # secondary battery / charging value
    "36",  # last-update timestamp (ms epoch)
})

# Working-status fields that tick every broadcast during a clean
# (elapsed time, area, the always-600 cumulative-time-ish field).
_NOISE_WS_KEYS: frozenset[str] = frozenset({"3", "12", "13", "15"})


def _diff_dict(
    a: dict[str, Any] | None,
    b: dict[str, Any] | None,
    skip: frozenset[str],
) -> list[str]:
    """Return short 'k: x → y' strings for non-noise key changes."""
    if not isinstance(a, dict) or not isinstance(b, dict):
        return []
    keys = (set(a) | set(b)) - skip
    out = []
    for k in sorted(keys, key=lambda x: int(x) if x.isdigit() else 999):
        va, vb = a.get(k), b.get(k)
        if va == vb:
            continue
        # Truncate long values so the hint fits one line.
        sa = repr(va)
        sb = repr(vb)
        if len(sa) > 40:
            sa = sa[:37] + "…"
        if len(sb) > 40:
            sb = sb[:37] + "…"
        out.append(f"{k}: {sa}→{sb}")
    return out


def _writer_thread(
    host: str, out_fp: TextIO, lock: threading.Lock,
    stop: threading.Event, verbose: bool, counter: list[int], ssh_key: str | None = None,
) -> None:
    """Read SSH log stream, write parsed DUMP lines to the JSONL output."""
    last_payload: dict[str, dict[str, Any]] = {}
    for line in _iter_log_stream(host, ssh_key):
        if stop.is_set():
            break
        m = DUMP_RE.match(line)
        if not m:
            continue
        topic = m.group("topic")
        payload = _parse_payload(m.group("payload"))
        record = {
            "kind": "broadcast",
            "ts": _now_iso(),
            "log_ts": m.group("ts"),
            "topic": topic,
            "payload": payload,
        }

        # Detect notable change vs last seen payload on the same topic.
        notable_diff: list[str] = []
        if topic == "status/robot_base_status":
            notable_diff = _diff_dict(
                last_payload.get(topic), payload if isinstance(payload, dict) else None,
                _NOISE_BASE_KEYS,
            )
        elif topic == "status/working_status":
            notable_diff = _diff_dict(
                last_payload.get(topic), payload if isinstance(payload, dict) else None,
                _NOISE_WS_KEYS,
            )
        if isinstance(payload, dict):
            last_payload[topic] = payload

        with lock:
            out_fp.write(json.dumps(record, default=str) + "\n")
            out_fp.flush()
            counter[0] += 1
            if verbose:
                # Verbose mode shares the terminal with the input prompt,
                # so the cursor jumps as broadcasts arrive. Default is
                # silent — `tail -f <out>.jsonl` in another window if
                # you want to watch the stream.
                print(f"  [{record['log_ts']}] {topic}",
                      file=sys.stderr)
            elif notable_diff and topic not in _NOISY_TOPICS:
                # Bell + one-line hint so the user notices an
                # interesting change but can keep typing. Doesn't
                # interrupt the input line — readline redraws on the
                # next keystroke.
                short_topic = topic.rsplit("/", 1)[-1]
                hint = ", ".join(notable_diff[:4])
                if len(notable_diff) > 4:
                    hint += f" (+{len(notable_diff) - 4} more)"
                print(f"\a\n[*] {short_topic}: {hint}",
                      file=sys.stderr, flush=True)
                # Persist the diff alongside the broadcast for replay.
                out_fp.write(json.dumps({
                    "kind": "change",
                    "ts": record["ts"],
                    "log_ts": record["log_ts"],
                    "topic": topic,
                    "diff": notable_diff,
                }) + "\n")
                out_fp.flush()


# --- Decoder for known protocol fields ---------------------------------
#
# What we already understand about Flow 2 broadcasts (validated live).
# Everything else we surface as raw key→value so the user can spot new
# patterns. Keep these dicts in sync with narwal_client.

_WORKING_STATUS = {
    0: "UNKNOWN", 1: "STANDBY", 3: "MOP_WASHING", 4: "CLEANING",
    5: "CLEANING_ALT", 7: "MAPPING", 10: "DOCKED", 14: "CHARGED",
    17: "MOP_DRYING", 19: "MOP_DRYING_ACTIVE", 99: "ERROR",
}
_ERROR_CODES = {
    0x01010036: "clean_water_dirty_tank_anomaly",
    0x01010137: "clean_water_tank_empty",
    0x02310031: "robot_lifted",
}
_SUCTION = {1: "Quiet", 2: "Standard", 3: "Strong", 4: "Super powerful"}
_MOP_HUMIDITY = {1: "Slightly dry", 2: "Standard", 3: "Slightly wet"}
_CLEAN_MODE = {
    1: "Vacuum", 2: "Mop", 3: "Vacuum then mop",
    4: "Vacuum and mop", 5: "Adaptive (Raumanpassung)",
}

# Decoded keys are tracked dynamically by _decode_state — anything it
# consumes is excluded from the "Unknown fields" section. This keeps
# the dashboard honest: if a row appears decoded above, the raw key
# disappears from the bottom; new firmware fields show up immediately.


def _f32(val: Any) -> float | None:
    """Decode a float32 stored as int bits (or already a float)."""
    if isinstance(val, float):
        return val
    if isinstance(val, int):
        try:
            import struct
            return struct.unpack("f", struct.pack("I", val & 0xFFFFFFFF))[0]
        except Exception:
            return None
    return None


def _f48_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize robot_base_status field 48.1 to a list of dicts."""
    f1 = payload.get("48", {}).get("1") if isinstance(payload.get("48"), dict) else None
    if isinstance(f1, list):
        return [e for e in f1 if isinstance(e, dict)]
    if isinstance(f1, dict):
        return [f1]
    return []


def _decode_state(
    latest: dict[str, dict[str, Any]],
    consumed_base: set[str] | None = None,
    consumed_ws: set[str] | None = None,
) -> list[tuple[str, str, str]]:
    """Build (label, value, raw_field) rows from the latest broadcasts.

    Mirrors what `narwal_client` actually pulls out of each broadcast,
    plus the Flow-2 specific fields we discovered. Anything we touch
    is added to consumed_{base,ws}; whatever's left is shown as
    raw/unknown by the caller. That keeps the dashboard honest:
    a row appearing here means the integration sees the value, not
    just that we logged the key.
    """
    bs = latest.get("status/robot_base_status") or {}
    ws = latest.get("status/working_status") or {}
    if consumed_base is None:
        consumed_base = set()
    if consumed_ws is None:
        consumed_ws = set()
    rows: list[tuple[str, str, str]] = []

    # Working status (3.1)
    f3 = bs.get("3") if isinstance(bs.get("3"), dict) else {}
    ws_id = f3.get("1") if isinstance(f3, dict) else None
    rows.append((
        "Status",
        f"{_WORKING_STATUS.get(ws_id, '?')} ({ws_id})" if ws_id is not None else "-",
        f"3.1={ws_id}",
    ))
    consumed_base.add("3")  # the whole nested message is decoded below

    # Battery (% from float32 in field 2)
    bat = _f32(bs.get("2"))
    rows.append(("Battery", f"{bat:.1f}%" if bat is not None else "-", "2 (float32)"))
    consumed_base.add("2")

    # Battery health (38, design capacity — always 100 in observed data)
    bh = bs.get("38")
    rows.append(("Battery health", f"{bh}" if bh is not None else "-", "38"))
    consumed_base.add("38")

    # Suction
    s = bs.get("26")
    rows.append(("Suction", f"{_SUCTION.get(s, '?')} ({s})" if s else "-", "26"))
    consumed_base.add("26")

    # Mop humidity
    h = bs.get("29")
    rows.append((
        "Mop humidity",
        f"{_MOP_HUMIDITY.get(h, '?')} ({h})" if h else "-",
        "29",
    ))
    consumed_base.add("29")

    # Dust bag
    db = bs.get("41")
    rows.append(("Dust bag", f"{db}%" if db is not None else "-", "41"))
    consumed_base.add("41")

    # Coverage precision (1 = Standard, absent = Meticulous; tentative)
    cp = bs.get("34")
    if cp == 1:
        rows.append(("Coverage", "Standard (tentative)", "34=1"))
    elif cp is None:
        rows.append(("Coverage", "Meticulous? (34 absent)", "34=∅"))
    else:
        rows.append(("Coverage", f"unknown (34={cp})", "34"))
    consumed_base.add("34")

    # Sub-state from field 3 (paused / returning / dock indicators)
    if isinstance(f3, dict):
        paused = f3.get("2") == 1
        returning = f3.get("7") == 1
        sub = []
        if paused:
            sub.append("paused")
        if returning:
            sub.append("returning")
        rows.append((
            "Sub-state", ", ".join(sub) or "-",
            f"3.2={f3.get('2')} 3.7={f3.get('7')}",
        ))
        rows.append((
            "Dock 3.x",
            f"presence={f3.get('3')} sub={f3.get('10')} activity={f3.get('12')}",
            "3.3 / 3.10 / 3.12",
        ))

    # Top-level dock indicators (mirrors of field 3 sub-fields)
    rows.append((
        "Dock 11/47",
        f"f11={bs.get('11')} (2=docked) f47={bs.get('47')} (3=docked)",
        "11, 47",
    ))
    consumed_base.update({"11", "47"})

    # Field 48: parse markers + clean-task config + error
    entries = _f48_entries(bs)
    markers: list[str] = []
    err_info: dict[str, Any] | None = None
    clean_cfg: dict[str, Any] | None = None
    for e in entries:
        if "10" in e:
            markers.append("dust_bag_drying")
        if "13" in e:
            markers.append("mop_drying")
        if "15" in e:
            markers.append("legacy_15?")
        if "5" in e and isinstance(e.get("5"), dict):
            clean_cfg = e["5"].get("1") if isinstance(e["5"].get("1"), dict) else None
        if "2" in e and isinstance(e.get("2"), dict) and e["2"]:
            err_info = e["2"]
    rows.append(("Station markers", ", ".join(markers) or "-", "48.1.*"))

    # Active clean task config (when running)
    if clean_cfg:
        mode = clean_cfg.get("1")
        mh = clean_cfg.get("2")
        passes = clean_cfg.get("3")
        cfg_str = (
            f"{_CLEAN_MODE.get(mode, '?')} ({mode}), mop={_MOP_HUMIDITY.get(mh, mh)}"
            + (f", passes={passes}" if passes else "")
        )
        rows.append(("Active task", cfg_str, "48.1.*.5.1"))
    else:
        rows.append(("Active task", "-", "48.1.*.5.1"))

    # Error — also check the secondary channel at base.1 (different
    # field order: 1=code, 2=severity, 3=formatted message banner).
    if not err_info and isinstance(bs.get("1"), dict) and bs.get("1"):
        f1 = bs["1"]
        err_info = {
            "1": f1.get("2"),
            "2": f1.get("1"),
            "3": f1.get("3", ""),
        }
    if err_info:
        code = err_info.get("2")
        msg = err_info.get("3", "")
        sev = err_info.get("1", "?")
        ident = _ERROR_CODES.get(code, "unknown") if isinstance(code, int) else "?"
        rows.append((
            "ERROR",
            f"{ident} sev={sev} code={code} ({code:#010x})  «{str(msg)[:30]}»"
            if isinstance(code, int) else f"sev={sev} {err_info!r}",
            "48.1.*.2 / base.1",
        ))
    else:
        rows.append(("Error", "none", "48.1.*.2"))
    consumed_base.add("48")

    # Session id (13) + last-update timestamp (36)
    sid = bs.get("13")
    if sid is not None:
        sid_short = str(sid)[:24] + ("…" if len(str(sid)) > 24 else "")
        rows.append(("Session ID", sid_short, "13"))
        consumed_base.add("13")
    ts36 = bs.get("36")
    if ts36 is not None:
        rows.append(("Timestamp (ms)", str(ts36), "36"))
        consumed_base.add("36")

    # Map signature: base.30 + base.44 form an opaque pair that flips
    # between saved maps (live-confirmed by the multi-map test).
    sig30 = bs.get("30")
    sig44 = bs.get("44")
    if sig30 is not None or sig44 is not None:
        rows.append((
            "Map signature",
            f"30={sig30!s} 44={sig44!s}",
            "30, 44",
        ))
    consumed_base.add("30")
    consumed_base.add("44")

    # User-action prompt: base.3.16 + ws.22 ({1: elapsed, 2: target}).
    # Live-observed prompt types: 2=fill_water, 3=return_to_dock_after_clean,
    # 4=carry_to_dock_to_start. Targets: 600 s (10 min) for fill / start,
    # 3600 s (1 h) for end-of-clean return.
    ua_type = f3.get("16") if isinstance(f3, dict) else None
    f22 = ws.get("22") if isinstance(ws, dict) else None
    if ua_type or (isinstance(f22, dict) and f22):
        ua_names = {
            2: "fill_water_tank",
            3: "return_to_dock_after_clean",
            4: "carry_to_dock_to_start",
        }
        elapsed = f22.get("1", 0) if isinstance(f22, dict) else 0
        target = f22.get("2", 0) if isinstance(f22, dict) else 0
        try:
            elapsed_i = int(elapsed) if elapsed else 0
            target_i = int(target) if target else 0
        except (ValueError, TypeError):
            elapsed_i = target_i = 0
        if target_i > 0:
            remaining = max(target_i - elapsed_i, 0)
            timer = f"{elapsed_i}/{target_i}s ({remaining}s left)"
        else:
            timer = "-"
        rows.append((
            "User action",
            f"{ua_names.get(ua_type, '?')} ({ua_type}) {timer}",
            "3.16 + ws.22",
        ))
    else:
        rows.append(("User action", "-", "3.16 + ws.22"))
    consumed_ws.add("22")

    # working_status: room queue (with completion flags) + current
    # room + cleaning telemetry. Flow 2 ws.5[i].2 = 2 marks a finished room.
    wf5 = ws.get("5") if isinstance(ws, dict) else None
    queue_entries: list[dict[str, Any]] = []
    if isinstance(wf5, list):
        queue_entries = [e for e in wf5 if isinstance(e, dict)]
    elif isinstance(wf5, dict):
        queue_entries = [wf5]
    if queue_entries:
        rendered = [
            f"{e.get('1')}{'✓' if e.get('2') == 2 else ''}"
            for e in queue_entries
        ]
        rows.append(("Room queue", ", ".join(rendered), "ws.5 (✓=done)"))
        done = [str(e.get("1")) for e in queue_entries if e.get("2") == 2]
        rows.append(("Rooms done", ", ".join(done) or "-", "ws.5[*].2=2"))
    else:
        rows.append(("Room queue", "-", "ws.5"))
        rows.append(("Rooms done", "-", "ws.5[*].2=2"))
    consumed_ws.add("5")

    cur = ws.get("6")
    rows.append(("Current room", str(cur) if cur is not None else "-", "ws.6"))
    consumed_ws.add("6")

    # Flow 2: progress % (ws.1 float32) + cleaned area m² (ws.2 float32).
    # Field 13 is a constant 18000 there, so prefer the float values.
    progress = _f32(ws.get("1")) if "1" in ws else None
    if progress is not None and 0 <= progress <= 200:
        rows.append(("Cleaning progress", f"{progress:.1f} %", "ws.1 (float32)"))
        consumed_ws.add("1")
    area = _f32(ws.get("2")) if "2" in ws else None
    if area is not None and 0 <= area <= 10000:
        rows.append(("Clean area", f"{area:.2f} m²", "ws.2 (float32)"))
        consumed_ws.add("2")
    elif "13" in ws:
        # Flow 1 fallback (cm²)
        try:
            area_m2 = int(ws["13"]) / 10000
            rows.append(("Clean area (legacy)", f"{area_m2:.2f} m²", "ws.13"))
        except (ValueError, TypeError):
            pass
        consumed_ws.add("13")

    if "12" in ws:
        rows.append(("Elapsed", f"{ws['12']} s", "ws.12"))
        consumed_ws.add("12")

    # Mop-drying timer (live-confirmed): ws.8 = elapsed seconds since
    # drying started, ws.9 = total target seconds. Silent mode targets
    # 18000 s (5 h); default / smart / strong target 12600 s (3.5 h).
    if "8" in ws or "9" in ws:
        elapsed = ws.get("8", 0)
        target = ws.get("9", 0)
        try:
            elapsed_i = int(elapsed) if elapsed else 0
            target_i = int(target) if target else 0
        except (ValueError, TypeError):
            elapsed_i = target_i = 0
        if target_i > 0:
            pct = (elapsed_i / target_i) * 100
            rows.append((
                "Mop drying",
                f"{elapsed_i//60}:{elapsed_i%60:02} / {target_i//60}:{target_i%60:02} ({pct:.0f}%)",
                "ws.8 / ws.9",
            ))
        elif elapsed_i:
            rows.append(("Mop drying", f"{elapsed_i//60}:{elapsed_i%60:02} elapsed", "ws.8"))
        consumed_ws.add("8")
        consumed_ws.add("9")

    return rows


def _unknown_keys(
    latest: dict[str, dict[str, Any]],
    consumed_base: set[str],
    consumed_ws: set[str],
) -> list[tuple[str, str, str]]:
    """Return (topic, key, raw repr) for fields not consumed by the decoder."""
    out: list[tuple[str, str, str]] = []
    bs = latest.get("status/robot_base_status") or {}
    for k in sorted(bs.keys(), key=lambda x: int(x) if x.isdigit() else 999):
        if k in consumed_base:
            continue
        out.append(("base", k, repr(bs[k])[:60]))
    ws = latest.get("status/working_status") or {}
    for k in sorted(ws.keys(), key=lambda x: int(x) if x.isdigit() else 999):
        if k in consumed_ws:
            continue
        out.append(("working", k, repr(ws[k])[:60]))
    return out



def cmd_dashboard(args: argparse.Namespace) -> int:
    """Live decoded-state dashboard (view only, no input handling).

    Streams `ha core logs --follow` over SSH, decodes each broadcast,
    and redraws an ANSI table on every state change. View-only: the
    `record` subcommand handles annotations in a separate terminal,
    which keeps typing latency-free regardless of how busy the dock is.
    """
    latest: dict[str, dict[str, Any]] = {}
    last_payloads: dict[str, dict[str, Any]] = {}
    last_change_label = "—"
    counter = 0
    # Hide the cursor while the dashboard is running.
    sys.stdout.write("\033[?25l")
    sys.stdout.flush()
    try:
        for line in _iter_log_stream(args.host, args.ssh_key):
            m = DUMP_RE.match(line)
            if not m:
                continue
            topic = m.group("topic")
            log_ts = m.group("ts")
            payload = _parse_payload(m.group("payload"))
            counter += 1

            changed = False
            if isinstance(payload, dict):
                prev = last_payloads.get(topic)
                if topic == "status/robot_base_status":
                    d = _diff_dict(prev, payload, _NOISE_BASE_KEYS)
                    if d:
                        last_change_label = f"[{log_ts}] base: " + ", ".join(d[:3])
                        changed = True
                elif topic == "status/working_status":
                    d = _diff_dict(prev, payload, _NOISE_WS_KEYS)
                    if d:
                        last_change_label = f"[{log_ts}] ws: " + ", ".join(d[:3])
                        changed = True
                if prev != payload:
                    changed = True
                last_payloads[topic] = payload
                latest[topic] = payload

            if not changed:
                continue

            # Clear screen + move cursor home, then redraw.
            out = ["\033[2J\033[H"]
            out.append(f"narwal-dashboard · host={args.host} · broadcasts={counter}\n")
            out.append("─" * 78 + "\n")
            consumed_b: set[str] = set()
            consumed_w: set[str] = set()
            for label, value, raw in _decode_state(latest, consumed_b, consumed_w):
                out.append(f"  {label:<16} {value:<48} [{raw}]\n")
            out.append("\n  Raw / undecoded fields:\n")
            for src, key, val in _unknown_keys(latest, consumed_b, consumed_w):
                out.append(f"    {src}.{key:<6} = {val}\n")
            out.append("\nΔ " + last_change_label + "\n")
            sys.stdout.write("".join(out))
            sys.stdout.flush()
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\033[?25h\n")
        sys.stdout.flush()
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    """Stream + annotate.

    Records every broadcast to JSONL while the user types annotations
    at a `> ` prompt. Pair with the `dashboard` subcommand in another
    terminal to watch the decoded state live without affecting input.
    """
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    stop = threading.Event()
    counter = [0]

    print(
        f"Recording to {out_path}.\n"
        f"Type any text + Enter to mark an annotation. "
        f"Empty line / Ctrl+D / Ctrl+C to stop.\n",
        file=sys.stderr,
    )

    with out_path.open("a") as out_fp:
        # Header line so each capture starts identifiable.
        out_fp.write(json.dumps({
            "kind": "session_start",
            "ts": _now_iso(),
            "host": args.host,
        }) + "\n")
        out_fp.flush()

        worker = threading.Thread(
            target=_writer_thread,
            args=(args.host, out_fp, lock, stop, args.verbose, counter, args.ssh_key),
            daemon=True,
        )
        worker.start()

        try:
            while True:
                try:
                    text = input("> ")
                except (EOFError, KeyboardInterrupt):
                    break
                text = text.strip()
                if not text:
                    break
                with lock:
                    out_fp.write(json.dumps({
                        "kind": "annotation",
                        "ts": _now_iso(),
                        "text": text,
                    }) + "\n")
                    out_fp.flush()
                    print(f"    [annotated; {counter[0]} broadcasts so far]",
                          file=sys.stderr)
        finally:
            stop.set()
            print(f"\nStopped after {counter[0]} broadcasts.", file=sys.stderr)
    return 0


def _flatten(prefix: str, value: Any, out: dict[str, Any]) -> None:
    """Flatten a nested dict to dotted-key form for diffing."""
    if isinstance(value, dict):
        if not value:
            out[prefix] = "{}"
            return
        for k, v in value.items():
            _flatten(f"{prefix}.{k}" if prefix else str(k), v, out)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _flatten(f"{prefix}[{i}]", v, out)
    else:
        out[prefix] = value


def _last_broadcasts(path: Path) -> dict[str, dict[str, Any]]:
    """Return {topic: latest payload dict} from a JSONL capture."""
    latest: dict[str, dict[str, Any]] = {}
    with path.open() as fp:
        for line in fp:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("kind") != "broadcast":
                continue
            topic = rec.get("topic")
            payload = rec.get("payload")
            if isinstance(payload, dict):
                latest[topic] = payload
    return latest


def cmd_diff(args: argparse.Namespace) -> int:
    """Show flat-key diffs between the latest broadcast per topic."""
    a = _last_broadcasts(Path(args.left))
    b = _last_broadcasts(Path(args.right))
    topics = sorted(set(a) | set(b))
    any_diff = False
    for topic in topics:
        if topic not in a:
            print(f"+ {topic}: only in {args.right}")
            any_diff = True
            continue
        if topic not in b:
            print(f"- {topic}: only in {args.left}")
            any_diff = True
            continue
        flat_a: dict[str, Any] = {}
        flat_b: dict[str, Any] = {}
        _flatten("", a[topic], flat_a)
        _flatten("", b[topic], flat_b)
        keys = sorted(set(flat_a) | set(flat_b))
        topic_diffs = [
            (k, flat_a.get(k, "<missing>"), flat_b.get(k, "<missing>"))
            for k in keys
            if flat_a.get(k) != flat_b.get(k)
        ]
        if topic_diffs:
            any_diff = True
            print(f"\n=== {topic} ===")
            for k, l, r in topic_diffs:
                print(f"  {k}: {l!r}  →  {r!r}")
    if not any_diff:
        print("No differences.")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    """Print the timeline grouped by annotation, one block per annotation."""
    path = Path(args.file)
    with path.open() as fp:
        for line in fp:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = rec.get("kind")
            if kind == "session_start":
                print(f"[session @ {rec['ts']} on {rec.get('host', '?')}]")
            elif kind == "annotation":
                print(f"\n>>> {rec['ts']} :: {rec['text']}")
            elif kind == "change":
                ts = rec.get("log_ts", rec.get("ts", "?"))
                short_topic = rec["topic"].rsplit("/", 1)[-1]
                hint = ", ".join(rec.get("diff", [])[:6])
                print(f"  [*] [{ts}] {short_topic}: {hint}")
            elif kind == "broadcast" and args.full:
                ts = rec.get("log_ts", rec.get("ts", "?"))
                print(f"  [{ts}] {rec['topic']}: {rec['payload']!r}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    rec = sub.add_parser("record", help="record annotated broadcasts")
    rec.add_argument("--host", required=True, help="ssh target, e.g. root@192.168.178.3")
    rec.add_argument("--ssh-key", help="optional private key for SSH")
    rec.add_argument("--out", required=True, help="JSONL output path")
    rec.add_argument(
        "--verbose", "-v", action="store_true",
        help="echo each broadcast on stderr (clutters the input prompt)",
    )
    rec.set_defaults(func=cmd_record)

    dash = sub.add_parser(
        "dashboard",
        help="live decoded-state view (run in a separate terminal alongside `record`)",
    )
    dash.add_argument("--host", required=True, help="ssh target, e.g. root@192.168.178.3")
    dash.add_argument("--ssh-key", help="optional private key for SSH")
    dash.set_defaults(func=cmd_dashboard)

    diff = sub.add_parser("diff", help="diff latest broadcast per topic between two captures")
    diff.add_argument("left")
    diff.add_argument("right")
    diff.set_defaults(func=cmd_diff)

    replay = sub.add_parser("replay", help="pretty-print a capture timeline")
    replay.add_argument("file")
    replay.add_argument(
        "--full", action="store_true",
        help="include every raw broadcast line (default: annotations + change hints only)",
    )
    replay.set_defaults(func=cmd_replay)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
