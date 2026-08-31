"""Minimal VCD (Value Change Dump) parser for module_canvas's waveform tab.

Only handles what chisel3's verilator backend actually emits (`$var wire`
declarations, `$scope`/`$upscope`, and `0`/`1`/`x`/`z`/`b<binary>` value
changes) — not the full VCD spec (no `$dumpvars`/`real`/`port` extensions).
"""

from __future__ import annotations

import re

VAR_RE = re.compile(r"^\$var\s+(\S+)\s+(\d+)\s+(\S+)\s+(\S+)(?:\s+\[[^\]]*\])?\s*\$end\s*$")
SCALAR_RE = re.compile(r"^([01xzXZ])(\S+)$")
VECTOR_RE = re.compile(r"^([bB])([01xzXZ]+)\s+(\S+)$")


def parse_vcd(text: str, max_changes_per_signal: int = 4000) -> dict:
    """Returns {"signals": [{id, name, width, scope}], "changes": {id: [[time, value], ...]}, "endTime": int}."""
    lines = text.split("\n")
    i = 0
    scope_stack: list[str] = []
    signals: dict[str, dict] = {}  # id -> {id, name, width, scope}
    order: list[str] = []

    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("$scope"):
            parts = line.split()
            if len(parts) >= 3:
                scope_stack.append(parts[2])
        elif line.startswith("$upscope"):
            if scope_stack:
                scope_stack.pop()
        elif line.startswith("$var"):
            m = VAR_RE.match(line)
            if m:
                _kind, width, vid, name = m.groups()
                scope = ".".join(scope_stack)
                if vid not in signals:
                    signals[vid] = {"id": vid, "name": name, "width": int(width), "scope": scope}
                    order.append(vid)
        elif line.startswith("$enddefinitions"):
            i += 1
            break
        i += 1

    changes: dict[str, list] = {vid: [] for vid in order}
    time = 0
    end_time = 0
    dropped: set[str] = set()

    for line in lines[i:]:
        line = line.strip()
        if not line:
            continue
        if line[0] == "#":
            try:
                time = int(line[1:])
                end_time = max(end_time, time)
            except ValueError:
                pass
            continue
        if line[0] in "01xzXZ":
            m = SCALAR_RE.match(line)
            if m:
                val, vid = m.groups()
                _record(changes, dropped, vid, time, val.lower(), max_changes_per_signal)
            continue
        if line[0] in "bB":
            m = VECTOR_RE.match(line)
            if m:
                _b, bits, vid = m.groups()
                value = _bits_to_value(bits)
                _record(changes, dropped, vid, time, value, max_changes_per_signal)
            continue
        # ignore $dumpvars/$dumpoff/other directives and blank/unknown lines

    signal_list = [signals[vid] for vid in order if vid in signals]
    return {"signals": signal_list, "changes": changes, "endTime": end_time, "truncated": sorted(dropped)}


def _record(changes, dropped, vid, time, value, cap):
    bucket = changes.setdefault(vid, [])
    if len(bucket) >= cap:
        dropped.add(vid)
        return
    bucket.append([time, value])


def _bits_to_value(bits: str) -> str:
    low = bits.lower()
    if "x" in low or "z" in low:
        return low
    try:
        return str(int(bits, 2))
    except ValueError:
        return low


def parse_vcd_file(path: str, max_changes_per_signal: int = 4000) -> dict:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return parse_vcd(f.read(), max_changes_per_signal)
