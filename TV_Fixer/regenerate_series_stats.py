#!/usr/bin/env python3
"""
Regenerate series_stats_summary.csv and series_stats_detail.csv from your
current TV library by cross-referencing what's on disk against TMDB.

Walks <media-root>, extracts each show's TMDB id from its {tmdb-NNNN}
folder marker, asks TMDB how many seasons and episodes the show is
supposed to have, counts what you actually have, and produces two CSVs
with the same schema as your original series_stats_detail.xlsx.

Output:
  series_stats_summary.csv  -- one row per show
  series_stats_detail.csv   -- one row per (show, season)

Requires TMDB_API_KEY env var.

Usage:
    python regenerate_series_stats.py "G:\\TV"
    python regenerate_series_stats.py "G:\\TV" --include-specials
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from tqdm import tqdm


TMDB_API = "https://api.themoviedb.org/3"
TMDB_FOLDER_RE = re.compile(r"\{tmdb-(\d+)\}", re.IGNORECASE)
SE_RE = re.compile(r"[Ss](\d{1,2})[\s._-]*[Ee](\d{1,3})")
SEASON_FOLDER_RE = re.compile(r"^[Ss]eason\s+(\d{1,2})(?:\s+\(\d{4}\))?\s*$")

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".ts", ".m2ts",
              ".mpg", ".mpeg", ".wmv", ".webm"}


def _safe(s) -> str:
    return str(s).encode("utf-8", errors="replace").decode("utf-8")


def tmdb_show(sess: requests.Session, tmdb_id: str) -> Optional[dict]:
    try:
        r = sess.get(f"{TMDB_API}/tv/{tmdb_id}", timeout=30)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    except requests.RequestException:
        return None


def scan_show_disk(show_dir: Path) -> Dict[int, set]:
    """For one show folder, return {season_num: {episode_num, ...}} of
    what's on disk. Tries SxxEyy in filename first, then 'Season N'
    folder + numeric guess as fallback."""
    found: Dict[int, set] = defaultdict(set)
    for p in show_dir.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in VIDEO_EXTS:
            continue
        m = SE_RE.search(p.name)
        if m:
            s, e = int(m.group(1)), int(m.group(2))
            found[s].add(e)
            continue
        # Fallback: file in "Season N" folder, episode number somewhere
        mseason = SEASON_FOLDER_RE.match(p.parent.name)
        if mseason:
            s = int(mseason.group(1))
            mep = re.search(r"(?:^|[^0-9])(\d{1,3})(?:[^0-9]|$)", p.name)
            if mep:
                found[s].add(int(mep.group(1)))
    return found


def compact_range(missing: List[int]) -> str:
    if not missing:
        return ""
    runs: List[Tuple[int, int]] = []
    start = prev = missing[0]
    for n in missing[1:]:
        if n == prev + 1:
            prev = n
        else:
            runs.append((start, prev))
            start = prev = n
    runs.append((start, prev))
    return ", ".join(f"{a}" if a == b else f"{a}-{b}" for a, b in runs)


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("media_root", type=Path)
    ap.add_argument("--summary-out", type=Path,
                    default=Path("series_stats_summary.csv"))
    ap.add_argument("--detail-out", type=Path,
                    default=Path("series_stats_detail.csv"))
    ap.add_argument("--include-specials", action="store_true",
                    help="Include season 0 (Specials) in regular episode counts")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    api_key = os.environ.get("TMDB_API_KEY")
    if not api_key:
        sys.exit("Set TMDB_API_KEY env var")

    if not args.media_root.is_dir():
        sys.exit(f"Not a directory: {args.media_root}")

    sess = requests.Session()
    sess.params = {"api_key": api_key}

    # Collect show folders with TMDB markers
    shows = []
    for child in sorted(args.media_root.iterdir()):
        if not child.is_dir():
            continue
        m = TMDB_FOLDER_RE.search(child.name)
        if m:
            shows.append((m.group(1), child))
    if args.limit:
        shows = shows[: args.limit]
    print(f"Processing {len(shows)} show folders...")

    summary_rows = []
    detail_rows = []

    for tmdb_id, show_dir in tqdm(shows, desc="shows", unit="show"):
        meta = tmdb_show(sess, tmdb_id)
        if not meta:
            # No TMDB data -- emit a minimal placeholder
            summary_rows.append({
                "tmdb_id": tmdb_id,
                "title": show_dir.name,
                "year": "",
                "status": "no_tmdb_data",
                "seasons_expected": "",
                "seasons_have": "",
                "regular_episodes_expected": "",
                "regular_episodes_have": "",
                "completeness_pct": "",
                "specials_expected": "",
                "specials_have": "",
                "missing_seasons": "",
                "incomplete_seasons": "",
                "unexpected_seasons": "",
            })
            continue

        title = meta.get("name", show_dir.name)
        first_air = (meta.get("first_air_date") or "")[:4]
        year = int(first_air) if first_air.isdigit() else ""
        status = meta.get("status", "")

        # Expected episodes per season from TMDB
        expected_by_season: Dict[int, Tuple[int, Optional[int]]] = {}
        for s in meta.get("seasons") or []:
            snum = s.get("season_number")
            if snum is None:
                continue
            ec = s.get("episode_count") or 0
            air = (s.get("air_date") or "")[:4]
            sy = int(air) if air.isdigit() else None
            expected_by_season[int(snum)] = (int(ec), sy)

        # Actual episodes on disk
        have_by_season = scan_show_disk(show_dir)

        regular_expected_seasons = sorted(
            s for s in expected_by_season if s > 0
        )
        # Aggregates
        seasons_expected = len(regular_expected_seasons)
        seasons_have = sum(
            1 for s in regular_expected_seasons
            if have_by_season.get(s)
        )
        regular_episodes_expected = sum(
            expected_by_season[s][0] for s in regular_expected_seasons
        )
        regular_episodes_have = sum(
            len(have_by_season.get(s, set())) for s in regular_expected_seasons
        )
        # Specials (season 0)
        specials_expected = expected_by_season.get(0, (0, None))[0]
        specials_have = len(have_by_season.get(0, set()))

        completeness_pct = (
            round(regular_episodes_have / regular_episodes_expected * 100, 1)
            if regular_episodes_expected else 0.0
        )

        # Categorize seasons
        missing_seasons: List[int] = []
        incomplete_seasons: List[str] = []
        unexpected_seasons: List[int] = []
        for s in regular_expected_seasons:
            exp = expected_by_season[s][0]
            have = len(have_by_season.get(s, set()))
            if have == 0 and exp > 0:
                missing_seasons.append(s)
            elif have < exp:
                incomplete_seasons.append(f"S{s:02d}({have}/{exp})")
        for s in have_by_season:
            if s not in expected_by_season:
                unexpected_seasons.append(s)

        summary_rows.append({
            "tmdb_id": tmdb_id,
            "title": _safe(title),
            "year": year,
            "status": status,
            "seasons_expected": seasons_expected,
            "seasons_have": seasons_have,
            "regular_episodes_expected": regular_episodes_expected,
            "regular_episodes_have": regular_episodes_have,
            "completeness_pct": completeness_pct,
            "specials_expected": specials_expected,
            "specials_have": specials_have,
            "missing_seasons": ", ".join(str(s) for s in missing_seasons),
            "incomplete_seasons": ", ".join(incomplete_seasons),
            "unexpected_seasons": ", ".join(str(s) for s in sorted(unexpected_seasons)),
        })

        # Per-season detail rows
        all_seasons = sorted(set(expected_by_season) | set(have_by_season))
        for s in all_seasons:
            exp, sy = expected_by_season.get(s, (0, None))
            have_set = have_by_season.get(s, set())
            have = len(have_set)
            if s == 0 and not args.include_specials and exp == 0:
                # Skip empty Specials rows unless asked
                pass
            missing_eps = compact_range(sorted(
                set(range(1, exp + 1)) - have_set
            )) if exp else ""
            extra_eps = compact_range(sorted(
                have_set - set(range(1, exp + 1))
            )) if exp else (compact_range(sorted(have_set)) if have_set else "")
            if exp == 0 and have == 0:
                row_status = "no_data"
            elif exp == 0 and have > 0:
                row_status = "unexpected"
            elif have == 0:
                row_status = "missing"
            elif have < exp:
                row_status = "incomplete"
            elif have == exp:
                row_status = "complete"
            else:
                row_status = "extra"
            detail_rows.append({
                "tmdb_id": tmdb_id,
                "title": _safe(title),
                "season": s,
                "season_year": sy or "",
                "episodes_expected": exp,
                "episodes_have": have,
                "missing_episodes": missing_eps,
                "extra_episodes": extra_eps,
                "status": row_status,
            })

    # Write the CSVs
    s_fields = ["tmdb_id", "title", "year", "status", "seasons_expected",
                "seasons_have", "regular_episodes_expected",
                "regular_episodes_have", "completeness_pct",
                "specials_expected", "specials_have", "missing_seasons",
                "incomplete_seasons", "unexpected_seasons"]
    with args.summary_out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=s_fields)
        w.writeheader()
        for r in summary_rows:
            w.writerow(r)

    d_fields = ["tmdb_id", "title", "season", "season_year",
                "episodes_expected", "episodes_have", "missing_episodes",
                "extra_episodes", "status"]
    with args.detail_out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=d_fields)
        w.writeheader()
        for r in detail_rows:
            w.writerow(r)

    # Summary print
    from collections import Counter
    by_status = Counter(r["status"] for r in summary_rows)
    total_have = sum(int(r["regular_episodes_have"] or 0) for r in summary_rows)
    total_expected = sum(int(r["regular_episodes_expected"] or 0)
                        for r in summary_rows)
    print(f"\nSummary:")
    print(f"  Shows processed:           {len(summary_rows)}")
    print(f"  Total episodes on disk:    {total_have:,}")
    print(f"  Total episodes expected:   {total_expected:,}")
    if total_expected:
        print(f"  Overall completeness:      "
              f"{total_have/total_expected*100:.1f}%")
    print(f"  Show statuses: {dict(by_status)}")
    print(f"  Summary CSV: {args.summary_out}")
    print(f"  Detail CSV:  {args.detail_out}")


if __name__ == "__main__":
    main()
