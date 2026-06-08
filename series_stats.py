#!/usr/bin/env python3
"""
series_stats.py - Library completeness audit.

Walks tv_root for canonical-named series folders, looks each show up
on TMDB to find the expected season + episode counts, compares against
what's actually on disk, and emits two CSVs:

    series_stats_summary.csv   — one row per series (high-level stats)
    series_stats_detail.csv    — one row per series-per-season (granular)

Use the summary to glance at completeness across the library, the detail
to drill into specific gaps (which episodes are missing, etc).

Pure read-only. Uses your existing TMDB cache so re-runs are fast.

Usage:
    py -3.13 series_stats.py --config config.json
    py -3.13 series_stats.py --config config.json \
        --output-dir H:\\reports
    py -3.13 series_stats.py --config config.json \
        --series 57243,1668     # only these tmdb_ids
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import os
import re
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

sys.path.insert(0, str(Path(__file__).parent))
from tv_tmdb_client import TmdbTvClient, is_error, is_not_found


SCRIPT_VERSION = "1.0.0"
LONG_PATH_PREFIX = "\\\\?\\"

VIDEO_EXTS = {
    ".mkv", ".mp4", ".avi", ".mov", ".wmv", ".m4v", ".webm", ".flv",
    ".mpeg", ".mpg", ".ts", ".m2ts", ".mts", ".vob", ".divx", ".ogm",
}

# Canonical series folder name: "<title> (<year>) {tmdb-#####}"
_SERIES_RE = re.compile(
    r"^(?P<title>.+?)\s*\((?P<year>\d{4}|)\)\s*\{tmdb-(?P<tmdb>\d+)\}$",
)
# SxxExx pattern, optionally with multi-ep range like S01E01-E02 or S01E01-02.
_EP_RE = re.compile(
    r"S(?P<season>\d{1,3})E(?P<ep1>\d{1,4})(?:[-eE]+(?P<ep2>\d{1,4}))?",
    re.IGNORECASE,
)
# Season folder name: "Season ## (yyyy)" or "Season ##" or "Specials".
_SEASON_RE = re.compile(
    r"^(?:Season\s*(?P<num>\d{1,3})(?:\s*\(\d{4}\))?|(?P<specials>Specials))\s*$",
    re.IGNORECASE,
)

log = logging.getLogger("series_stats")


def long_path(p: Path) -> str:
    if os.name != "nt":
        return str(p)
    s = str(p)
    if s.startswith(LONG_PATH_PREFIX):
        return s
    if s.startswith("\\\\"):
        return LONG_PATH_PREFIX + "UNC\\" + s.lstrip("\\")
    if len(s) >= 2 and s[1] == ":":
        return LONG_PATH_PREFIX + s
    return s


# =========================================================================
# Filesystem walk: count what's actually on disk
# =========================================================================

def collect_actual_episodes(series_dir: Path
                              ) -> Tuple[Dict[int, Set[int]], int]:
    """Walk series_dir; return ({season_num: {episode_nums...}}, total_video_files).

    Multi-episode files (S01E01-E02) contribute both episode numbers.
    Specials folder is season 0.
    """
    found: Dict[int, Set[int]] = {}
    total_videos = 0
    if not series_dir.is_dir():
        return found, total_videos

    try:
        children = list(series_dir.iterdir())
    except OSError:
        return found, total_videos

    for child in children:
        try:
            if not child.is_dir():
                continue
        except OSError:
            continue
        m = _SEASON_RE.match(child.name)
        if not m:
            continue
        if m.group("specials"):
            sn = 0
        else:
            try:
                sn = int(m.group("num"))
            except (TypeError, ValueError):
                continue
        # Walk this season subfolder for video files.
        try:
            for f in child.iterdir():
                try:
                    if not f.is_file():
                        continue
                except OSError:
                    continue
                if f.suffix.lower() not in VIDEO_EXTS:
                    continue
                total_videos += 1
                em = _EP_RE.search(f.stem)
                if not em:
                    continue
                file_sn = int(em.group("season"))
                ep1 = int(em.group("ep1"))
                ep2_str = em.group("ep2")
                ep2 = int(ep2_str) if ep2_str else ep1
                if ep2 < ep1:
                    ep1, ep2 = ep2, ep1
                # Trust the SxxExx in the filename over the folder
                # numbering — they should agree but if they don't, the
                # filename is more authoritative.
                if file_sn != sn:
                    log.debug("  S%02d folder contained file claiming S%02d: %s",
                              sn, file_sn, f.name)
                bucket = found.setdefault(file_sn, set())
                for n in range(ep1, ep2 + 1):
                    bucket.add(n)
        except OSError:
            continue

    return found, total_videos


# =========================================================================
# TMDB lookup: expected seasons + episodes
# =========================================================================

@dataclass
class ExpectedSeason:
    season_number: int
    season_year: Optional[int]
    episode_numbers: Set[int]    # episodes TMDB lists for this season
    name: str = ""


@dataclass
class ExpectedShow:
    tmdb_id: int
    title: str
    year: Optional[int]
    status: str = ""             # "Returning Series", "Ended", etc.
    seasons: Dict[int, ExpectedSeason] = field(default_factory=dict)


def fetch_expected(tmdb: TmdbTvClient, tmdb_id: int) -> Optional[ExpectedShow]:
    """Get the show + per-season expected episodes from TMDB."""
    details = tmdb.tv_details(tmdb_id)
    if is_error(details) or is_not_found(details):
        return None
    title = details.get("name") or details.get("original_name") or ""
    first_air = str(details.get("first_air_date") or "")[:4]
    year = int(first_air) if first_air.isdigit() else None
    status = details.get("status") or ""
    show = ExpectedShow(tmdb_id=tmdb_id, title=title, year=year, status=status)

    # `seasons` array in tv_details lists every season with its episode_count.
    # We still call tv_season(tmdb_id, sn) per season to get the actual list
    # of episode numbers (handles edge cases like skipped numbers).
    n_seasons = details.get("number_of_seasons") or 0
    season_list = details.get("seasons") or []
    season_numbers_to_fetch: Set[int] = set()
    for sd in season_list:
        if isinstance(sd, dict) and sd.get("season_number") is not None:
            try:
                season_numbers_to_fetch.add(int(sd["season_number"]))
            except (TypeError, ValueError):
                pass
    # Also include 1..n_seasons in case `seasons` array is missing some.
    for n in range(1, int(n_seasons) + 1):
        season_numbers_to_fetch.add(n)
    # Always probe Specials.
    season_numbers_to_fetch.add(0)

    for sn in sorted(season_numbers_to_fetch):
        data = tmdb.tv_season(tmdb_id, sn)
        if is_error(data) or is_not_found(data):
            continue
        ep_nums: Set[int] = set()
        for ep in data.get("episodes") or []:
            try:
                ep_nums.add(int(ep.get("episode_number")))
            except (TypeError, ValueError):
                continue
        if not ep_nums and sn != 0:
            # Empty non-special season — TMDB returned nothing useful.
            continue
        season_year_str = str(data.get("air_date") or "")[:4]
        season_year = int(season_year_str) if season_year_str.isdigit() else None
        show.seasons[sn] = ExpectedSeason(
            season_number=sn,
            season_year=season_year,
            episode_numbers=ep_nums,
            name=data.get("name") or "",
        )
    return show


# =========================================================================
# Comparison + reporting
# =========================================================================

@dataclass
class SeasonStat:
    season: int
    season_year: Optional[int]
    expected: int
    have: int
    missing: List[int]    # episode numbers missing
    extra: List[int]      # episode numbers we have that TMDB doesn't list
    status: str           # complete | incomplete | missing | extra | unexpected


def compare_show(expected: ExpectedShow,
                  actual: Dict[int, Set[int]]
                  ) -> List[SeasonStat]:
    out: List[SeasonStat] = []
    all_seasons: Set[int] = set(expected.seasons.keys()) | set(actual.keys())
    for sn in sorted(all_seasons):
        exp = expected.seasons.get(sn)
        have_eps = actual.get(sn, set())
        if exp is None:
            # We have files for a season TMDB doesn't list.
            out.append(SeasonStat(
                season=sn, season_year=None,
                expected=0, have=len(have_eps),
                missing=[], extra=sorted(have_eps),
                status="unexpected",
            ))
            continue
        exp_eps = exp.episode_numbers
        missing = sorted(exp_eps - have_eps)
        extra = sorted(have_eps - exp_eps)
        if not have_eps:
            status = "missing"
        elif not missing and not extra:
            status = "complete"
        elif missing and not extra:
            status = "incomplete"
        elif extra and not missing:
            status = "extra"
        else:
            status = "incomplete"  # both missing+extra — call it incomplete
        out.append(SeasonStat(
            season=sn, season_year=exp.season_year,
            expected=len(exp_eps), have=len(have_eps),
            missing=missing, extra=extra,
            status=status,
        ))
    return out


def format_range_list(nums: List[int], max_chars: int = 80) -> str:
    """Compact representation: 1,2,3,5,7,8,9 -> '1-3, 5, 7-9'."""
    if not nums:
        return ""
    nums = sorted(set(nums))
    spans: List[str] = []
    start = nums[0]
    prev = nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        spans.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = n
    spans.append(str(start) if start == prev else f"{start}-{prev}")
    s = ", ".join(spans)
    if len(s) > max_chars:
        return s[:max_chars - 3] + "..."
    return s


# =========================================================================
# CSV output
# =========================================================================

SUMMARY_COLS = [
    "tmdb_id", "title", "year", "status",
    "seasons_expected", "seasons_have",
    "regular_episodes_expected", "regular_episodes_have",
    "completeness_pct",
    "specials_expected", "specials_have",
    "missing_seasons", "incomplete_seasons", "unexpected_seasons",
]
DETAIL_COLS = [
    "tmdb_id", "title", "season", "season_year",
    "episodes_expected", "episodes_have",
    "missing_episodes", "extra_episodes",
    "status",
]


def write_csvs(out_dir: Path,
                summary_rows: List[Dict[str, Any]],
                detail_rows: List[Dict[str, Any]]) -> Tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    summary_path = out_dir / "series_stats_summary.csv"
    detail_path = out_dir / "series_stats_detail.csv"

    with open(summary_path, "w", encoding="utf-8-sig", newline="",
              errors="replace") as fh:
        w = csv.DictWriter(fh, fieldnames=SUMMARY_COLS)
        w.writeheader()
        for r in summary_rows:
            w.writerow({k: r.get(k, "") for k in SUMMARY_COLS})

    with open(detail_path, "w", encoding="utf-8-sig", newline="",
              errors="replace") as fh:
        w = csv.DictWriter(fh, fieldnames=DETAIL_COLS)
        w.writeheader()
        for r in detail_rows:
            w.writerow({k: r.get(k, "") for k in DETAIL_COLS})

    return summary_path, detail_path


# =========================================================================
# Main
# =========================================================================

def setup_logging(out_dir: Path, run_id: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / f"series_stats_{run_id}.log"
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt); fh.setLevel(logging.DEBUG)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt); sh.setLevel(logging.INFO)
    log.setLevel(logging.DEBUG)
    if not log.handlers:
        log.addHandler(fh); log.addHandler(sh)
    log.info("series_stats.py v%s starting; log: %s",
             SCRIPT_VERSION, log_path)
    return log_path


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Walk tv_root, look up each series's expected seasons "
                    "+ episodes on TMDB, compare to what's on disk, emit "
                    "summary and detail CSVs.")
    ap.add_argument("--version", action="version",
                    version=f"series_stats.py v{SCRIPT_VERSION}")
    ap.add_argument("--config", required=True, help="Path to config.json")
    ap.add_argument("--output-dir", default=None,
                    help="Where to write CSVs (default: <mediaprep_dir>)")
    ap.add_argument("--series", default="",
                    help="Comma-separated tmdb_ids; default = all")
    return ap.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    mediaprep_dir = Path(cfg.get("mediaprep_dir", "."))
    tv_root = Path(cfg.get("tv_root", "."))
    out_dir = Path(args.output_dir) if args.output_dir else mediaprep_dir

    run_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    setup_logging(mediaprep_dir, run_id)
    log.info("tv_root: %s", tv_root)
    log.info("output:  %s", out_dir)

    if not tv_root.is_dir():
        log.error("tv_root not a directory: %s", tv_root)
        return 1

    series_filter: Optional[Set[int]] = None
    if args.series:
        series_filter = {int(x.strip()) for x in args.series.split(",")
                          if x.strip()}
        log.info("Filtering to %d series_ids: %s",
                 len(series_filter), sorted(series_filter))

    api_key = cfg.get("tmdb_api_key")
    if not api_key:
        raise SystemExit("tmdb_api_key not set in config.json")
    cache_path = mediaprep_dir / "tmdb_tv_cache.sqlite"
    lang = cfg.get("tmdb_tv_language") or cfg.get("language", "en-US")
    tmdb = TmdbTvClient(api_key, cache_path, language=lang)

    # Walk tv_root for canonical series folders.
    candidates: List[Tuple[Path, str, Optional[int], int]] = []
    for child in sorted(tv_root.iterdir()):
        try:
            if not child.is_dir():
                continue
        except OSError:
            continue
        m = _SERIES_RE.match(child.name)
        if not m:
            continue
        title = m.group("title").strip()
        yr_str = m.group("year") or ""
        yr = int(yr_str) if yr_str.isdigit() else None
        try:
            tmdb_id = int(m.group("tmdb"))
        except ValueError:
            continue
        if series_filter is not None and tmdb_id not in series_filter:
            continue
        candidates.append((child, title, yr, tmdb_id))
    log.info("Found %d canonical series folders", len(candidates))

    summary_rows: List[Dict[str, Any]] = []
    detail_rows: List[Dict[str, Any]] = []
    errors = 0

    try:
        for i, (series_dir, title, yr, tmdb_id) in enumerate(candidates,
                                                              start=1):
            expected = fetch_expected(tmdb, tmdb_id)
            if expected is None:
                log.warning("[tmdb-%d] tv_details unavailable; skipping",
                            tmdb_id)
                errors += 1
                continue

            actual, total_videos = collect_actual_episodes(series_dir)
            stats = compare_show(expected, actual)

            # Detail rows.
            for s in stats:
                detail_rows.append({
                    "tmdb_id": tmdb_id,
                    "title": expected.title or title,
                    "season": s.season,
                    "season_year": s.season_year if s.season_year else "",
                    "episodes_expected": s.expected,
                    "episodes_have": s.have,
                    "missing_episodes": format_range_list(s.missing),
                    "extra_episodes": format_range_list(s.extra),
                    "status": s.status,
                })

            # Summary row.
            regular_seasons = [s for s in stats if s.season > 0]
            seasons_expected = len([s for s in regular_seasons if s.expected])
            seasons_have = len([s for s in regular_seasons if s.have])
            reg_eps_expected = sum(s.expected for s in regular_seasons)
            reg_eps_have = sum(min(s.have, s.expected) for s in regular_seasons
                                if s.expected)
            pct = (100.0 * reg_eps_have / reg_eps_expected) if reg_eps_expected else 0.0

            specials = [s for s in stats if s.season == 0]
            specials_expected = sum(s.expected for s in specials)
            specials_have = sum(s.have for s in specials)

            missing_seasons = [str(s.season) for s in regular_seasons
                                if s.status == "missing"]
            incomplete_seasons = [
                f"S{s.season:02d}({s.have}/{s.expected})"
                for s in regular_seasons if s.status == "incomplete"
            ]
            unexpected_seasons = [str(s.season) for s in regular_seasons
                                    if s.status == "unexpected"]

            summary_rows.append({
                "tmdb_id": tmdb_id,
                "title": expected.title or title,
                "year": expected.year or (yr or ""),
                "status": expected.status,
                "seasons_expected": seasons_expected,
                "seasons_have": seasons_have,
                "regular_episodes_expected": reg_eps_expected,
                "regular_episodes_have": reg_eps_have,
                "completeness_pct": f"{pct:.1f}",
                "specials_expected": specials_expected,
                "specials_have": specials_have,
                "missing_seasons": ", ".join(missing_seasons),
                "incomplete_seasons": ", ".join(incomplete_seasons),
                "unexpected_seasons": ", ".join(unexpected_seasons),
            })

            log.info("[%d/%d] %-50s S=%d/%d eps=%d/%d (%.1f%%)",
                     i, len(candidates), title[:50],
                     seasons_have, seasons_expected,
                     reg_eps_have, reg_eps_expected, pct)
    finally:
        tmdb.close()

    # Sort: summary alphabetical by title; detail by title then season.
    summary_rows.sort(key=lambda r: (str(r["title"]).lower(), r["tmdb_id"]))
    detail_rows.sort(key=lambda r: (str(r["title"]).lower(), int(r["season"])))

    summary_path, detail_path = write_csvs(out_dir, summary_rows, detail_rows)

    # Aggregate stats for the final log line.
    total_reg_expected = sum(int(r["regular_episodes_expected"] or 0)
                              for r in summary_rows)
    total_reg_have = sum(int(r["regular_episodes_have"] or 0)
                         for r in summary_rows)
    overall_pct = (100.0 * total_reg_have / total_reg_expected
                    if total_reg_expected else 0.0)

    log.info("=" * 60)
    log.info("Library completeness audit")
    log.info("  series scanned:      %d", len(summary_rows))
    log.info("  total reg episodes:  %d / %d (%.1f%%)",
             total_reg_have, total_reg_expected, overall_pct)
    log.info("  errors:              %d", errors)
    log.info("Summary CSV: %s", summary_path)
    log.info("Detail CSV:  %s", detail_path)
    return 0 if errors == 0 else 3


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        log.error("Unhandled exception:\n%s", traceback.format_exc())
        sys.exit(2)
