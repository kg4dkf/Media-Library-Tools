#!/usr/bin/env python3
"""
Find and merge duplicate show folders that share a {tmdb-NNNN} marker.

When apply_renames.py created a new show folder with a slightly different
name than your existing one (TMDB returns 'Aaahh!!! Real Monsters' while
you had 'Aaahh! Real monsters' on disk), this tool finds all such pairs
and merges them.

Strategy per duplicate group:
  - Pick the folder with the most files as the KEEPER.
  - Move everything from the smaller folder(s) into the keeper.
  - On filename collision inside the keeper, the incoming file gets a
    '_dup_' prefix so it doesn't overwrite.
  - Remove the now-empty source folder.

Subfolder merging is recursive (handles Season XX subfolders too).

Default: dry-run. Pass --commit to act.

Usage:
    python merge_show_folders.py "G:\\TV"           # dry run
    python merge_show_folders.py "G:\\TV" --commit  # apply
"""
from __future__ import annotations

import argparse
import csv
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path
from typing import List, Tuple

TMDB_FOLDER_RE = re.compile(r"\{tmdb-(\d+)\}", re.IGNORECASE)


def _safe(s) -> str:
    return str(s).encode("utf-8", errors="replace").decode("utf-8")


def folder_count(p: Path) -> int:
    """Total file count in folder (recursive)."""
    n = 0
    try:
        for _ in p.rglob("*"):
            n += 1
    except OSError:
        pass
    return n


def merge_into(src: Path, dst: Path,
               actions: List[Tuple[str, str, str]],
               commit: bool) -> None:
    """Recursively move src/* into dst/. Removes src if empty afterwards."""
    if not src.is_dir():
        return
    for child in list(src.iterdir()):
        target = dst / child.name
        if target.exists() and target.is_dir() and child.is_dir():
            # Recursive subfolder merge (e.g. Season XX folders)
            merge_into(child, target, actions, commit)
            continue
        if target.exists():
            target = dst / f"_dup_{child.name}"
        actions.append(("move", _safe(child), _safe(target)))
        if commit:
            try:
                shutil.move(str(child), str(target))
            except Exception as e:
                actions.append(("move_failed", _safe(child), str(e)))
                continue
    # Try to remove src if it's empty
    try:
        if not any(src.iterdir()):
            actions.append(("rmdir", _safe(src), ""))
            if commit:
                src.rmdir()
    except OSError as e:
        actions.append(("rmdir_failed", _safe(src), str(e)))


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("media_root", type=Path)
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--report", type=Path,
                    default=Path("merge_shows_report.csv"))
    args = ap.parse_args()

    if not args.media_root.is_dir():
        sys.exit(f"Not a directory: {args.media_root}")

    # Group immediate-child folders by their TMDB id
    by_tmdb = defaultdict(list)
    for child in args.media_root.iterdir():
        if not child.is_dir():
            continue
        m = TMDB_FOLDER_RE.search(child.name)
        if m:
            by_tmdb[m.group(1)].append(child)

    dupes = {k: v for k, v in by_tmdb.items() if len(v) > 1}
    print(f"Show folders under {args.media_root}: {sum(len(v) for v in by_tmdb.values())}")
    print(f"TMDB ids with duplicate folders:    {len(dupes)}")
    if not dupes:
        print("Nothing to merge.")
        return

    actions: List[Tuple[str, str, str]] = []

    print()
    for tmdb_id, folders in sorted(dupes.items()):
        sized = sorted(folders, key=folder_count, reverse=True)
        keeper = sized[0]
        print(f"tmdb-{tmdb_id}:")
        for f in sized:
            n = folder_count(f)
            mark = "KEEP" if f == keeper else "merge"
            print(f"  [{mark:5s}] ({n:>4} files) {f.name}")
        for f in sized[1:]:
            merge_into(f, keeper, actions, args.commit)

    # Write report
    with args.report.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["action", "source", "destination"])
        for row in actions:
            w.writerow(row)

    by_kind = defaultdict(int)
    for a in actions:
        by_kind[a[0]] += 1
    print(f"\n{'Committed' if args.commit else 'Planned'} actions:")
    for k, v in sorted(by_kind.items()):
        print(f"  {k:18s}  {v}")
    print(f"  report:           {args.report}")
    if not args.commit:
        print("\nDRY RUN. Re-run with --commit to apply.")


if __name__ == "__main__":
    main()
