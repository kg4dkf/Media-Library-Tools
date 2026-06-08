#!/usr/bin/env python3
"""
Normalize season-folder naming across a TV library to 'Season 01', 'Season 02',
... 'Season 10', etc. (leading-zero, Plex/Sonarr canonical).

Also fixes the misplaced 'Season N-poster.jpg' files at show roots, renaming
them to 'Season 0N-poster.jpg' so Plex picks them up.

What it handles per show folder:
  - 'Season 1' (no leading zero, single-digit) -> renamed to 'Season 01'
  - 'Season 01' and 'Season 1' both exist     -> contents merged into 'Season 01',
                                                  empty 'Season 1' deleted
  - 'Season 1-poster.jpg' at show root         -> renamed to 'Season 01-poster.jpg'
  - 'Season 1.jpg', 'season-1-poster.jpg', etc. -> matched by regex

Two-digit seasons (Season 10 and up) are already correct.

Default: DRY-RUN. Use --commit to actually rename/move files.

Usage:
    python fix_season_folders.py "G:\\TV"           # dry-run
    python fix_season_folders.py "G:\\TV" --commit  # actually fix
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path
from typing import List, Tuple

# Matches folder names like 'Season 1', 'season 2', etc. -- single digit only.
# Two-digit folders ('Season 10'..) are already canonical; ignore them.
NO_ZERO_FOLDER_RE = re.compile(r"^[Ss]eason\s+(\d)$")

# Matches misplaced posters / banners at show root.
# 'Season 1-poster.jpg', 'season-1-poster.jpg', 'Season 1.jpg', etc.
POSTER_NO_ZERO_RE = re.compile(
    r"^([Ss]eason[\s_-])(\d)(?!\d)([-_].+|\.[A-Za-z0-9]+)$"
)


def _safe(s) -> str:
    """Round-trip through UTF-8 with errors='replace' so unpaired Windows
    surrogates in filenames don't crash csv.writer."""
    return str(s).encode("utf-8", errors="replace").decode("utf-8")


def canonical_season_name(num: int) -> str:
    return f"Season {num:02d}"


def fix_folder(parent: Path, folder: Path, num: int,
               commit: bool) -> List[Tuple[str, str, str]]:
    """Handle one mis-named season folder. Returns a list of action tuples
    (kind, source, dest) for reporting."""
    actions: List[Tuple[str, str, str]] = []
    target = parent / canonical_season_name(num)

    if not target.exists():
        actions.append(("rename_folder", str(folder), str(target)))
        if commit:
            folder.rename(target)
        return actions

    # Both 'Season 1' and 'Season 01' exist -- merge contents.
    for child in folder.iterdir():
        dst = target / child.name
        if dst.exists():
            # Don't clobber. Append a marker to the source name and move it
            # alongside; user can inspect and decide.
            dst = target / f"_dup_{child.name}"
        actions.append(("merge", str(child), str(dst)))
        if commit:
            shutil.move(str(child), str(dst))

    actions.append(("rmdir", str(folder), ""))
    if commit:
        try:
            folder.rmdir()
        except OSError as e:
            actions.append(("rmdir_failed", str(folder), str(e)))
    return actions


def fix_poster_file(show_dir: Path, file: Path, num: int,
                    commit: bool) -> List[Tuple[str, str, str]]:
    """Rename 'Season 1-poster.jpg' -> 'Season 01-poster.jpg' (preserving the
    rest of the filename)."""
    m = POSTER_NO_ZERO_RE.match(file.name)
    if not m:
        return []
    prefix, _digit, rest = m.groups()
    new_name = f"{prefix}{num:02d}{rest}"
    target = show_dir / new_name
    if target.exists():
        # Don't overwrite; flag for the user.
        return [("poster_conflict", str(file), str(target))]
    if commit:
        file.rename(target)
    return [("rename_poster", str(file), str(target))]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("media_root", type=Path, help="TV library root (e.g. G:\\TV)")
    ap.add_argument("--commit", action="store_true",
                    help="Actually rename/move. Default is dry-run.")
    ap.add_argument("--report", type=Path, default=Path("season_fix_report.csv"))
    args = ap.parse_args()

    if not args.media_root.is_dir():
        sys.exit(f"Not a directory: {args.media_root}")

    by_kind: defaultdict[str, int] = defaultdict(int)
    all_actions: List[Tuple[str, str, str, str]] = []  # (show, kind, src, dst)

    shows = [p for p in args.media_root.iterdir() if p.is_dir()]
    print(f"Scanning {len(shows)} show folders under {args.media_root}...")

    for show_dir in sorted(shows):
        # Fix mis-named season folders.
        for child in sorted(show_dir.iterdir()):
            if not child.is_dir():
                continue
            m = NO_ZERO_FOLDER_RE.match(child.name)
            if not m:
                continue
            num = int(m.group(1))
            for action in fix_folder(show_dir, child, num, args.commit):
                kind, src, dst = action
                all_actions.append((show_dir.name, kind, src, dst))
                by_kind[kind] += 1

        # Fix misplaced posters/banners at show root.
        for child in show_dir.iterdir():
            if not child.is_file():
                continue
            m = POSTER_NO_ZERO_RE.match(child.name)
            if not m:
                continue
            num = int(m.group(2))
            for action in fix_poster_file(show_dir, child, num, args.commit):
                kind, src, dst = action
                all_actions.append((show_dir.name, kind, src, dst))
                by_kind[kind] += 1

    # Write CSV report so the user can audit before/after.
    import csv
    with args.report.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["show", "action", "source", "destination"])
        for row in all_actions:
            w.writerow([_safe(c) for c in row])

    print(f"\nActions {'committed' if args.commit else 'planned'}:")
    for kind, count in sorted(by_kind.items()):
        print(f"  {kind:18s}  {count}")
    print(f"  total:              {sum(by_kind.values())}")
    print(f"  report:             {args.report}")
    if not args.commit:
        print("\nDRY RUN. Re-run with --commit to apply.")


if __name__ == "__main__":
    main()
