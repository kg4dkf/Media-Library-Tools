#!/usr/bin/env python3
"""
Report subtitle-download progress against a target count.

Usage (simple):
    python progress.py --media-root "G:\\TV" --target 14846

Usage (with the xlsx, includes per-show breakdown):
    python progress.py --media-root "G:\\TV" --target 14846 ^
        --xlsx "H:\\_mediaprep\\series_stats_detail.xlsx" ^
        --sheet series_stats_summary --tmdb-col tmdb_id

The --target number comes from the "Listing done: N subs" line in
fetch_subtitles.py output. Default of 14846 reflects your run; override
if you re-list and get a different number.

Counts both the existing per-show subtitle files saved by
fetch_subtitles.py (G:\\TV\\<show>\\SxxEyy.srt) and any other .srt files
under the media root.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

TMDB_FOLDER_RE = re.compile(r"\{tmdb-(\d+)\}", re.IGNORECASE)


def fmt_eta(remaining: int, rate_per_day: int) -> str:
    if remaining <= 0:
        return "complete"
    days = remaining / max(1, rate_per_day)
    if days < 1:
        hours = days * 24
        return f"~{hours:.1f} hours"
    if days < 14:
        return f"~{days:.1f} days"
    weeks = days / 7
    return f"~{weeks:.1f} weeks ({days:.0f} days)"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--media-root", type=Path, required=True,
                    help="Where fetch_subtitles.py is saving SRTs (e.g. G:\\TV)")
    ap.add_argument("--target", type=int, default=14846,
                    help="Total expected subs (from fetch_subtitles 'Listing done')")
    ap.add_argument("--rate", type=int, default=2000,
                    help="Downloads per day (your OpenSubtitles tier)")
    ap.add_argument("--per-show", action="store_true",
                    help="Show a per-show breakdown of downloaded counts")
    ap.add_argument("--xlsx", type=Path, default=None,
                    help="Optional: series_stats_detail.xlsx to cross-check"
                         " which shows are complete/partial/missing")
    ap.add_argument("--sheet", default="series_stats_summary")
    ap.add_argument("--title-col", default="title")
    ap.add_argument("--tmdb-col", default="tmdb_id")
    args = ap.parse_args()

    if not args.media_root.is_dir():
        sys.exit(f"Not a directory: {args.media_root}")

    # Count SRTs grouped by their show folder (immediate child of media-root).
    by_show: Counter[str] = Counter()
    total_srts = 0
    for srt in args.media_root.rglob("*.srt"):
        try:
            rel = srt.relative_to(args.media_root)
        except ValueError:
            continue
        if len(rel.parts) < 2:
            continue
        show = rel.parts[0]
        by_show[show] += 1
        total_srts += 1

    pct = (total_srts / args.target * 100) if args.target else 0.0
    remaining = max(0, args.target - total_srts)
    eta = fmt_eta(remaining, args.rate)

    print(f"Subtitle download progress:")
    print(f"  Downloaded:    {total_srts:,} / {args.target:,}  ({pct:.1f}%)")
    print(f"  Remaining:     {remaining:,}")
    print(f"  At {args.rate:,}/day:  {eta}")
    print(f"  Shows touched: {len(by_show)} folders under {args.media_root}")

    if args.xlsx:
        try:
            import openpyxl
        except ImportError:
            sys.exit("pip install openpyxl to use --xlsx")
        wb = openpyxl.load_workbook(str(args.xlsx), data_only=True, read_only=True)
        ws = wb[args.sheet]
        rows = ws.iter_rows(values_only=True)
        header = [str(h).strip() if h is not None else "" for h in next(rows)]
        ti = header.index(args.title_col) if args.title_col in header else None
        ki = header.index(args.tmdb_col) if args.tmdb_col in header else None

        # Build a tmdb->folder index so we can map an xlsx row to the folder
        # whose SRT count we've already tallied.
        tmdb_to_folder: dict = {}
        for folder, _ in by_show.items():
            m = TMDB_FOLDER_RE.search(folder)
            if m:
                tmdb_to_folder[m.group(1)] = folder

        complete = partial = missing = 0
        partial_samples = []
        missing_samples = []
        for row in rows:
            if not row or ti is None:
                continue
            title = row[ti]
            tmdb_id = str(row[ki]).strip() if ki is not None and row[ki] else None
            if not title:
                continue
            folder = tmdb_to_folder.get(tmdb_id) if tmdb_id else None
            count = by_show.get(folder, 0) if folder else 0
            if count == 0:
                missing += 1
                if len(missing_samples) < 5:
                    missing_samples.append(f"{title} (tmdb-{tmdb_id})")
            elif count < 3:
                partial += 1
                if len(partial_samples) < 5:
                    partial_samples.append(f"{title}: {count}")
            else:
                complete += 1

        print(f"\nPer-show status (across {complete + partial + missing} shows in xlsx):")
        print(f"  >=3 subs:    {complete}")
        print(f"  1-2 subs:    {partial}")
        print(f"  0 subs:      {missing}")
        if partial_samples:
            print(f"  partial (sample):")
            for s in partial_samples:
                print(f"    - {s}")
        if missing_samples:
            print(f"  missing (sample):")
            for s in missing_samples:
                print(f"    - {s}")

    if args.per_show:
        print(f"\nTop 20 shows by sub count:")
        for folder, n in by_show.most_common(20):
            print(f"  {n:4d}  {folder}")


if __name__ == "__main__":
    main()
