#!/usr/bin/env python3
"""
Strip the ' - Episode Title' portion from Plex-style renamed files so
they become ambiguity-free 'Show (Year) - SXXEYY.ext'.

Walks <media-root> and matches filenames like:
    Show Name (Year) - S01E18 - Episode Title.mkv
    Show Name (Year) - S01E18 - Episode Title.en.srt
And renames them to:
    Show Name (Year) - S01E18.mkv
    Show Name (Year) - S01E18.en.srt

Handles SRT sidecars including the .en.srt / .es.srt / .fr.srt convention.

Default: dry-run. Pass --commit to act.

Usage:
    python strip_episode_titles.py "G:\\TV"           # dry run
    python strip_episode_titles.py "G:\\TV" --commit  # apply
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

# Captures:
#   group 1 = 'Show Name (Year) - SXXEYY'
#   group 2 = file extension(s), e.g. '.mkv' or '.en.srt'
# Anchors on " - SXXEYY - " so we only strip when there's clearly a title.
TITLE_RE = re.compile(
    r"^(.+?\s+-\s+S\d{1,2}E\d{1,3})\s+-\s+.+?(\.(?:[a-z]{2,3}\.)?[a-z0-9]+)$",
    re.IGNORECASE,
)


def _safe(s) -> str:
    return str(s).encode("utf-8", errors="replace").decode("utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("media_root", type=Path)
    ap.add_argument("--commit", action="store_true",
                    help="Actually rename. Default is dry-run.")
    ap.add_argument("--report", type=Path,
                    default=Path("strip_titles_report.csv"))
    args = ap.parse_args()

    if not args.media_root.is_dir():
        sys.exit(f"Not a directory: {args.media_root}")

    plans = []
    skipped_no_title = 0

    for p in args.media_root.rglob("*"):
        if not p.is_file():
            continue
        m = TITLE_RE.match(p.name)
        if not m:
            continue
        new_name = m.group(1) + m.group(2)
        if new_name == p.name:
            skipped_no_title += 1
            continue
        plans.append({
            "old": p,
            "new_name": new_name,
            "new_path": p.parent / new_name,
        })

    print(f"Files to rename: {len(plans)}")
    if plans:
        print("Sample:")
        for plan in plans[:5]:
            print(f"  {plan['old'].name}")
            print(f"    -> {plan['new_name']}")

    # Write report
    with args.report.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["action", "old_path", "new_path"])
        for plan in plans:
            new_exists = plan["new_path"].exists()
            action = "skip_target_exists" if new_exists else "rename"
            w.writerow([action, _safe(plan["old"]), _safe(plan["new_path"])])

    print(f"  report: {args.report}")

    if not args.commit:
        print("\nDRY RUN. Re-run with --commit to apply.")
        return

    renamed = 0
    target_exists = 0
    failed = 0
    for plan in plans:
        new_path = plan["new_path"]
        if new_path.exists():
            target_exists += 1
            continue
        try:
            plan["old"].rename(new_path)
            renamed += 1
        except Exception as e:
            sys.stderr.write(f"FAIL: {_safe(plan['old'])} -> "
                             f"{_safe(new_path)}: {e}\n")
            failed += 1
    print(f"\nDone. Renamed: {renamed}. Skipped (target exists): "
          f"{target_exists}. Failed: {failed}.")


if __name__ == "__main__":
    main()
