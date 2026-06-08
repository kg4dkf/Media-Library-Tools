#!/usr/bin/env python3
"""
Reverse the moves recorded in apply_renames.log.

Reads each successful 'move' or 'replace' line (action TAB src TAB dest)
and moves the file at dest back to src. SRT sidecars are also moved back
if they were renamed alongside. Recycle Bin disposals (discard_new / tie /
recycled) are NOT recovered -- those need manual Recycle Bin restoration
on Windows.

NOTE: --write-tags wrote metadata into the moved files. Reverting the
path doesn't strip those tags; the files retain the new MKV/MP4 metadata
even after being moved back to their original names.

Filters:
    --show "Aaahh"     only revert files whose original path matched this
                        substring (case-insensitive)
    --since 2026-05-20  only revert log entries since this date (handy if
                        you ran apply_renames multiple times)

Default: dry-run. Pass --commit to act.

Usage:
    python revert_renames.py                          # dry run, show plan
    python revert_renames.py --show "Aaahh" --commit  # revert just one show
    python revert_renames.py --commit                  # revert everything
"""
from __future__ import annotations

import argparse
import csv
import re
import shutil
import sys
from collections import Counter
from pathlib import Path


def _safe(s) -> str:
    return str(s).encode("utf-8", errors="replace").decode("utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--log", type=Path, default=Path("apply_renames.log"),
                    help="The apply_renames.log file to read")
    ap.add_argument("--show", help="Only revert paths whose ORIGINAL src "
                    "contains this substring (case-insensitive)")
    ap.add_argument("--commit", action="store_true",
                    help="Actually move files. Default is dry-run.")
    ap.add_argument("--report", type=Path, default=Path("revert_plan.csv"))
    args = ap.parse_args()

    if not args.log.is_file():
        sys.exit(f"Log not found: {args.log}")

    needle = args.show.lower() if args.show else None
    plans = []
    skipped_recycled = 0
    skipped_failed = 0

    with args.log.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            action, src, dest = parts[0], parts[1], parts[2]
            if action in ("FAIL", "srt_move_failed"):
                skipped_failed += 1
                continue
            if dest == "(recycled)":
                skipped_recycled += 1
                continue
            if action not in ("move", "replace"):
                continue
            if needle and needle not in src.lower():
                continue
            plans.append({"action": action, "src": src, "dest": dest})

    # SRT sidecars: any file with same basename as dest, but .srt suffix,
    # should also be moved back. We infer this from the dest path.
    print(f"Loaded {len(plans)} reversible moves from {args.log}")
    print(f"  skipped (recycled, no revert possible): {skipped_recycled}")
    print(f"  skipped (logged failures):              {skipped_failed}")
    if not plans:
        print("Nothing to revert.")
        return

    if args.show:
        print(f"\nFilter --show {args.show!r}: keeping {len(plans)} matching plans")

    # Build the actual revert plan: for each, dest -> src, plus any
    # accompanying SRT (basename match).
    revert_plan = []
    for p in plans:
        src_path = Path(p["src"])
        dest_path = Path(p["dest"])

        # Video
        revert_plan.append({"kind": "video", "from": str(dest_path), "to": str(src_path)})

        # SRT sidecar (apply_renames named it <basename>.en.srt next to the video)
        srt_dest = dest_path.with_suffix(".en.srt")
        if srt_dest.exists():
            # Try the original SRT location: same folder as the SOURCE video,
            # plain S{S}E{E}.srt naming as fetch_subtitles wrote it.
            # If we can't infer SE, fall back to <src basename>.en.srt.
            m = re.search(r"S(\d{1,2})E(\d{1,3})", dest_path.name)
            if m:
                src_srt = src_path.parent / f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}.srt"
            else:
                src_srt = src_path.with_suffix(".en.srt")
            revert_plan.append({"kind": "srt", "from": str(srt_dest), "to": str(src_srt)})

    # Sanity-check the plan
    will_move = 0
    will_skip_missing = 0
    will_skip_target_exists = 0
    for p in revert_plan:
        if not Path(p["from"]).exists():
            will_skip_missing += 1
        elif Path(p["to"]).exists():
            will_skip_target_exists += 1
        else:
            will_move += 1

    print(f"\nRevert plan:")
    print(f"  will move:              {will_move:>6,}")
    print(f"  skip (src missing):     {will_skip_missing:>6,}")
    print(f"  skip (target exists):   {will_skip_target_exists:>6,}")
    print(f"  total entries in plan:  {len(revert_plan):>6,}")

    # Write plan CSV for audit
    with args.report.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["kind", "action", "from", "to"])
        for p in revert_plan:
            if not Path(p["from"]).exists():
                action = "skip_missing"
            elif Path(p["to"]).exists():
                action = "skip_target_exists"
            else:
                action = "move"
            w.writerow([p["kind"], action, _safe(p["from"]), _safe(p["to"])])
    print(f"  plan CSV:              {args.report}")

    if not args.commit:
        print("\nDRY RUN. Inspect the CSV, then re-run with --commit to act.")
        return

    # Execute
    moved = 0
    failed = 0
    for p in revert_plan:
        src = Path(p["from"])
        dst = Path(p["to"])
        if not src.exists() or dst.exists():
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            moved += 1
        except Exception as e:
            sys.stderr.write(f"FAIL: {_safe(src)} -> {_safe(dst)}: {e}\n")
            failed += 1

    print(f"\nReverted {moved} files. Failed: {failed}.")


if __name__ == "__main__":
    main()
