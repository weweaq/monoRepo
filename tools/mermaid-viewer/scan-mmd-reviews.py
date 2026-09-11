#!/usr/bin/env python3
"""scan-mmd-reviews.py

Scan a Mermaid .mmd file for review/fix/idea markers and print a
structured, machine-readable summary of every marker found.

Marker convention (placed inside .mmd as %% comments):
    %% [type:id]  <one-line title>
    %%   loc: <symbol or edge, e.g. S / CL / CL-->PR>
    %%   note: <optional detail, may span following %% lines until next marker>

type is one of: review | fix | idea | question
id  is a positive integer.

Example:
    %% [review:1] Label on CL-->PR is misleading
    %%   loc: CL-->PR
    %%   note: says "pending_images and text" but code routes on image only

Usage:
    python scan-mmd-reviews.py <file.mmd>

Exit code: 0 if no markers, 1 if any markers found (handy for CI gating).
"""

from __future__ import annotations

import re
import sys

TYPE_RE = re.compile(r"^\[\s*(fix|review|idea|question)\s*:\s*(\d+)\s*\]\s*(.*)$", re.I)
LOC_RE = re.compile(r"^\s*(?:loc|location)\s*:\s*(.*)$", re.I)
NOTE_RE = re.compile(r"^\s*(?:note|detail)\s*:\s*(.*)$", re.I)

VALID_TYPES = {"review", "fix", "idea", "question"}


def parse_markers(text: str) -> list[dict]:
    """Return [{type,id,title,loc,note}] for every marker in the text."""
    markers: list[dict] = []
    cur: dict | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        # A marker line looks like: [review:1] title  (commas/punct allowed after)
        m = TYPE_RE.match(line)
        if m:
            if cur is not None:
                markers.append(cur)
            cur = {
                "type": m.group(1).lower(),
                "id": int(m.group(2)),
                "title": m.group(3).strip(),
                "loc": "",
                "note": "",
            }
            continue
        if cur is None:
            continue
        # Continuation lines belong to the current marker.
        lm = LOC_RE.match(line)
        if lm and not cur["loc"]:
            cur["loc"] = lm.group(1).strip()
            continue
        nm = NOTE_RE.match(line)
        if nm and not cur["note"]:
            cur["note"] = nm.group(1).strip()
            continue
        # Ignore other %% comment lines and diagram syntax inside marker block.
        if line.startswith("%%") or not line:
            continue
        # Free-form trailing text: append to current note.
        if cur["note"]:
            cur["note"] += " " + line
        else:
            cur["note"] = line
    if cur is not None:
        markers.append(cur)
    return markers


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python scan-mmd-reviews.py <file.mmd>", file=sys.stderr)
        return 2

    path = sys.argv[1]
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        print(f"[ERROR] cannot read {path}: {exc}", file=sys.stderr)
        return 2

    markers = parse_markers(text)
    if not markers:
        print(f"0 markers in {path}")
        return 0

    # Validate duplicate ids and unknown types.
    seen: dict[int, str] = {}
    ok = True
    for mk in markers:
        if mk["type"] not in VALID_TYPES:
            print(f"[WARN] marker {mk['id']}: unknown type '{mk['type']}' (fix|review|idea|question)", file=sys.stderr)
            ok = False
        if mk["id"] in seen:
            print(f"[WARN] duplicate marker id {mk['id']} (also used by '{seen[mk['id']]}')", file=sys.stderr)
            ok = False
        else:
            seen[mk["id"]] = mk["title"]

    markers.sort(key=lambda m: m["id"])
    print(f"{len(markers)} markers in {path}:")
    for mk in markers:
        line = f"  [{mk['type']}:{mk['id']}] {mk['title']}"
        if mk["loc"]:
            line += f"\n      loc  : {mk['loc']}"
        if mk["note"]:
            line += f"\n      note : {mk['note']}"
        print(line)
    print("\nUse these loc/detail anchors when handing the file to an LLM "
          "so it edits only the pointed symbols (see codemap-traceable-node rule).")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())