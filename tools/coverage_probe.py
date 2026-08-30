#!/usr/bin/env python3
"""Coverage probe: catalogue everything the robot broadcasts locally.

Streams `ha core logs --follow` over SSH, parses every `DUMP` line, and
keeps a running set of:

  • topics ever seen
  • per-topic field IDs ever populated (top-level dict keys of the
    decoded protobuf)
  • per-topic field values that ever changed (so we can spot dynamic
    vs constant fields)

Workflow: start the probe, then in the official Narwal app trigger a
feature (single-room, schedule edit, voice pack switch, …). Hit RETURN
in the probe terminal and type a short label — it gets saved as a
"marker" together with the diff of broadcasts since the previous
marker. At exit (Ctrl-C) the probe prints a coverage report:

  • all topics observed
  • for each topic, fields that mutated at least once
  • topics in const.py that never appeared (probably command-only or
    cloud-only)
  • topics that appeared but are not listed in const.py (new!)

Usage::

    python3 tools/coverage_probe.py homeassistant.local
    # … click around in the app, hit RETURN, type "single-room toilet" …
    # Ctrl-C to print report

Output is written to ``coverage_<timestamp>.json`` in the current
directory for offline inspection.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import select
import signal
import subprocess
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

# ANSI escape stripper — `ha core logs` may colourise output depending on tty.
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mGKHF]")
DUMP_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) "
    r"DEBUG .* DUMP (?P<topic>\S+): (?P<payload>.+)$"
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CONST_FILE = REPO_ROOT / "narwal_client" / "const.py"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iter_log_stream(host: str) -> Iterator[str]:
    """Yield log lines from `ha core logs --follow` over SSH."""
    cmd = ["ssh", host, "ha core logs --follow"]
    # Pipe stderr through so SSH connection errors / "ha: command not
    # found" surface immediately instead of silently producing an empty
    # stream — the latter looks identical to "robot is silent".
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=sys.stderr,
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


def _parse_payload(raw: str) -> Any:
    # DUMP payloads are repr()'d Python dicts — ast.literal_eval is safe.
    try:
        return ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return None


def _load_known_topics() -> set[str]:
    """Parse TOPIC_* string constants out of const.py."""
    topics: set[str] = set()
    pat = re.compile(r'^TOPIC_[A-Z_]+\s*=\s*"([^"]+)"')
    for line in CONST_FILE.read_text().splitlines():
        m = pat.match(line)
        if m:
            topics.add(m.group(1))
    return topics


class Coverage:
    def __init__(self) -> None:
        # topic → { "first_seen": iso, "last_seen": iso, "count": int,
        #          "fields": { fid → {"values": set(), "changed": bool} } }
        self.topics: dict[str, dict[str, Any]] = {}
        self.markers: list[dict[str, Any]] = []
        # Snapshot of {topic: {field: value}} at last marker, for diffing.
        self._marker_baseline: dict[str, dict[str, Any]] = defaultdict(dict)
        self._latest_payload: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def observe(self, topic: str, payload: Any, ts: str) -> None:
        with self._lock:
            entry = self.topics.setdefault(topic, {
                "first_seen": ts, "last_seen": ts, "count": 0,
                "fields": defaultdict(lambda: {"changed": False, "samples": []}),
            })
            entry["last_seen"] = ts
            entry["count"] += 1

            if isinstance(payload, dict):
                self._latest_payload[topic] = payload
                for fid, val in payload.items():
                    f = entry["fields"][str(fid)]
                    samples = f["samples"]
                    if val not in samples:
                        if samples:
                            f["changed"] = True
                        # Cap to 5 distinct samples per field — enough to
                        # tell "constant" vs "varies" without ballooning.
                        if len(samples) < 5:
                            samples.append(val)

    def add_marker(self, label: str) -> dict[str, Any]:
        """Snapshot what changed since the last marker, save it, return summary."""
        with self._lock:
            ts = _now_iso()
            diff: dict[str, dict[str, list[Any]]] = {}
            for topic, latest in self._latest_payload.items():
                baseline = self._marker_baseline.get(topic, {})
                changes = {}
                for fid, val in latest.items():
                    fid_s = str(fid)
                    if baseline.get(fid_s) != val:
                        changes[fid_s] = [baseline.get(fid_s), val]
                if changes:
                    diff[topic] = changes
                self._marker_baseline[topic] = dict(latest)

            marker = {
                "ts": ts, "label": label, "diff": diff,
                "topics_changed": sorted(diff.keys()),
            }
            self.markers.append(marker)
            return marker

    def serialise(self) -> dict[str, Any]:
        with self._lock:
            out_topics = {}
            for t, e in self.topics.items():
                out_topics[t] = {
                    "first_seen": e["first_seen"],
                    "last_seen": e["last_seen"],
                    "count": e["count"],
                    "fields": {
                        fid: {
                            "changed": f["changed"],
                            "samples": [_safe(s) for s in f["samples"]],
                        }
                        for fid, f in e["fields"].items()
                    },
                }
            return {"topics": out_topics, "markers": self.markers}


def _safe(v: Any) -> Any:
    """Make value JSON-serialisable; truncate long bytes/strings."""
    if isinstance(v, bytes):
        return f"<{len(v)}B>"
    if isinstance(v, str) and len(v) > 80:
        return v[:77] + "..."
    if isinstance(v, list):
        return [_safe(x) for x in v[:5]] + (["…"] if len(v) > 5 else [])
    if isinstance(v, dict):
        return {k: _safe(val) for k, val in list(v.items())[:10]}
    return v


def _stream_thread(
    host: str, cov: Coverage, stop: threading.Event,
) -> None:
    for line in _iter_log_stream(host):
        if stop.is_set():
            break
        m = DUMP_RE.match(line)
        if not m:
            continue
        payload = _parse_payload(m.group("payload"))
        # client.py logs `DUMP <short_topic>: <decoded>` — already the
        # `category/name` short form, no prefix stripping needed.
        cov.observe(
            topic=m.group("topic"),
            payload=payload,
            ts=m.group("ts"),
        )


def _print_report(cov: Coverage, known: set[str]) -> None:
    data = cov.serialise()
    seen = set(data["topics"].keys())

    print()
    print("=" * 70)
    print("COVERAGE REPORT")
    print("=" * 70)
    print(f"Topics seen:    {len(seen)}")
    print(f"Markers logged: {len(data['markers'])}")
    print()

    print("--- Topics observed ---")
    for t in sorted(seen):
        e = data["topics"][t]
        mutating = sum(1 for f in e["fields"].values() if f["changed"])
        flag = "" if t in known else "  [NEW]"
        print(f"  {t:<40s} count={e['count']:>5d}  "
              f"fields={len(e['fields'])} (mutating={mutating}){flag}")

    print()
    print("--- Known topics never broadcast (probably command-only or cloud-only) ---")
    missing = sorted(known - seen)
    if not missing:
        print("  (none — every known topic was seen at least once)")
    for t in missing:
        print(f"  {t}")

    print()
    print("--- Markers ---")
    for m in data["markers"]:
        topics = ", ".join(m["topics_changed"]) or "(no broadcasts changed)"
        print(f"  [{m['ts']}] {m['label']}")
        print(f"      → {topics}")


def cmd_probe(args: argparse.Namespace) -> int:
    known = _load_known_topics()
    cov = Coverage()
    stop = threading.Event()

    print(f"[probe] streaming logs from {args.host} (Ctrl-C to stop)")
    print(f"[probe] {len(known)} known topics loaded from const.py")
    print("[probe] type a label and press ENTER to mark an event")
    print()

    t = threading.Thread(
        target=_stream_thread, args=(args.host, cov, stop), daemon=True,
    )
    t.start()

    def _on_sigint(signum: int, frame: Any) -> None:  # noqa: ARG001
        stop.set()
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _on_sigint)

    try:
        while not stop.is_set():
            # Non-blocking stdin so the streamer keeps running.
            r, _, _ = select.select([sys.stdin], [], [], 0.5)
            if not r:
                continue
            label = sys.stdin.readline().strip()
            if not label:
                continue
            marker = cov.add_marker(label)
            n = len(marker["topics_changed"])
            print(f"  [marker] '{label}' → {n} topic(s) changed since previous marker")
    except KeyboardInterrupt:
        pass

    stop.set()
    time.sleep(0.3)

    out_path = Path(f"coverage_{int(time.time())}.json")
    out_path.write_text(json.dumps(cov.serialise(), indent=2, default=str))
    print(f"\n[probe] saved raw data → {out_path}")
    _print_report(cov, known)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("host", help="SSH target running Home Assistant")
    args = p.parse_args()
    return cmd_probe(args)


if __name__ == "__main__":
    sys.exit(main())
