# -*- coding: utf-8 -*-
"""Baseline / regression snapshot of verify_file over every artifact in downloads/.

Run BEFORE and AFTER the content-gate change and diff the two JSON files:
nothing that passed before may start failing unless it is one of the known
webpage-shell artifacts.

Usage:
    python tests/verify_snapshot.py baseline.json
    python tests/verify_snapshot.py after.json
"""
from __future__ import annotations

import json
import os
import re
import sys
import logging
import warnings
from pathlib import Path

logging.disable(logging.CRITICAL)
warnings.filterwarnings("ignore")
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from standards_crawler_agent.download import verify_file  # noqa: E402

DL = ROOT / "downloads"
SUFFIXES = {".pdf", ".doc", ".docx", ".xls", ".xlsx"}


def requested_name_for(filename: str) -> str:
    """Approximate the name the pipeline would have been given.

    The real requests come from the spreadsheet, but the snapshot only needs to
    be identical before and after the change, so deriving it from the artifact
    name is enough and keeps the comparison stable.
    """
    stem = Path(filename).stem
    # strip the trailing "_<标准号>" that build_canonical_filename appends
    stem = re.sub(r"_[A-Za-z]{1,8}(?:／|/)?[A-Za-z]{0,4}\s*\d[\d.]*\s*[-—]\s*\d{4}$", "", stem)
    stem = re.sub(r"_[^_]*令\s*第?\s*[\d〇一二三四五六七八九十]+号$", "", stem)
    return stem or filename


def main() -> None:
    out_path = Path(sys.argv[1] if len(sys.argv) > 1 else "snapshot.json")
    snapshot: dict[str, dict] = {}
    for entry in sorted(os.listdir(DL)):
        path = DL / entry
        if not path.is_file() or path.suffix.lower() not in SUFFIXES:
            continue
        name = requested_name_for(entry)
        try:
            result = verify_file(str(path), name)
            snapshot[entry] = {
                "ok": bool(result.ok),
                "pages": result.metadata.get("pages"),
                "scan_only": result.metadata.get("scan_only"),
                "body_absent": result.metadata.get("body_absent"),
                "warnings": list(result.warnings),
                "notes": list(result.notes),
            }
        except Exception as exc:  # keep the snapshot complete even on odd files
            snapshot[entry] = {"ok": None, "error": f"{type(exc).__name__}: {exc}"}

    out_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8")
    ok = sum(1 for v in snapshot.values() if v.get("ok"))
    print(f"files={len(snapshot)}  ok={ok}  not_ok={len(snapshot) - ok}  -> {out_path}")


if __name__ == "__main__":
    main()
