# -*- coding: utf-8 -*-
"""Diff two verify snapshots.

Only files present in BOTH snapshots are compared: downloads/ is a live directory
(an agent batch may add or rename artifacts between runs), so a file that only
exists on one side tells us nothing about the change.

Usage:
    python tests/diff_snapshots.py before.json after.json [out.txt]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.stdout.reconfigure(encoding="utf-8")

before = json.loads((HERE / sys.argv[1]).read_text(encoding="utf-8"))
after = json.loads((HERE / sys.argv[2]).read_text(encoding="utf-8"))

lines: list[str] = []


def say(text: str = "") -> None:
    lines.append(text)


def describe(rec: dict | None) -> list[str]:
    if rec is None:
        return ["<absent>"]
    if "error" in rec:
        return [f"ERROR: {rec['error']}"]
    out = []
    for item in rec.get("warnings") or []:
        out.append(f"WARN : {item}")
    for item in rec.get("notes") or []:
        out.append(f"NOTE : {item}")
    return out or ["<no warnings>"]


common = sorted(set(before) & set(after))
flip_fail = [n for n in common if before[n].get("ok") and not after[n].get("ok")]
flip_pass = [n for n in common if not before[n].get("ok") and after[n].get("ok")]
scan = [n for n in common if after[n].get("scan_only") and not before[n].get("scan_only")]

say(f"before: {len(before)} files, ok={sum(1 for v in before.values() if v.get('ok'))}")
say(f"after : {len(after)} files, ok={sum(1 for v in after.values() if v.get('ok'))}")
say(f"comparable (present in both): {len(common)}")
say(f"only in before: {len(set(before) - set(after))}   only in after: {len(set(after) - set(before))}")
say()
say(f"### NOW FAILING ({len(flip_fail)})")
for name in flip_fail:
    say()
    say(f"  {name}")
    for line in describe(after[name]):
        say(f"      {line}")
say()
say(f"### NOW PASSING ({len(flip_pass)})")
for name in flip_pass:
    say(f"  {name}   (before: {describe(before[name])})")
say()
say(f"### NEWLY FLAGGED scan_only ({len(scan)})")
for name in scan:
    say(f"  {name}")
say()
say(f"### ONLY IN BEFORE ({len(set(before) - set(after))}) - gone/renamed")
for name in sorted(set(before) - set(after)):
    say(f"  {name}")
say()
say(f"### ONLY IN AFTER ({len(set(after) - set(before))}) - new")
for name in sorted(set(after) - set(before)):
    say(f"  {name}")

text = "\n".join(lines) + "\n"
if len(sys.argv) > 3:
    (HERE / sys.argv[3]).write_text(text, encoding="utf-8")
    print(f"written -> {sys.argv[3]}")
else:
    print(text)
