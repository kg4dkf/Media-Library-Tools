#!/usr/bin/env python3
"""
runtime_scan.py — Walk the migrated library, ffprobe every main video,
classify each by duration. Flags candidates that are likely TV episodes,
TV specials, or unusually long compilations rather than standalone movies.

Reads plan.csv to find each title's main video at its post-migration
path. Outputs runtime_scan.csv with one row per title (or more, if a title
has multiple videos) and a runtime_classification column.

Classification thresholds (configurable via flags):
    <  10 min   trivial       (trailer, sample, blooper) — usually in Extras
    < 25 min    tv_short      strong TV-episode signal
    < 50 min    tv_episode    very likely TV episode
    < 75 min    tv_special    miniseries chunk, comedy special, doc episode
    < 240 min   movie         normal movie length
    >= 240 min  oversized     possibly full-season compilation or extended-extended cut

Usage:
    py runtime_scan.py --config config.json
    py runtime_scan.py --config config.json --include-extras  # also probe extras
    py runtime_scan.py --config config.json --titles 42,87
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent))
from execute import (  # type: ignore
    Config,
    InventoryFile,
    PlanRow,
    VIDEO_EXTS,
    _norm_path,
    _read_csv_dicts,
    attach_main_video_path,
    load_config,
    long,
    read_inventory,
    read_plan,
    title_files,
)


VERSION = "1.0.0"
log = logging.getLogger("runtime_scan")


# =========================================================================
# Logging
# =========================================================================

def setup_logging(mediaprep: Path, run_id: str) -> Path:
    mediaprep.mkdir(parents=True, exist_ok=True)
    log_path = mediaprep / f"runtime_scan_{run_id}.log"
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    log.handlers.clear()
    log.setLevel(logging.DEBUG)
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(logging.Formatter(fmt))
    log.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter(fmt))
    log.addHandler(sh)
    log.info("runtime_scan.py v%s starting; log: %s", VERSION, log_path)
    return log_path


# =========================================================================
# ffprobe wrapper
# =========================================================================

def ffprobe_duration_seconds(path: Path, timeout: int = 60) -> Optional[float]:
    """Return duration in seconds, or None on any failure."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        long(path),
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        log.warning("ffprobe failed for %s: %s", path, e)
        return None
    out = (proc.stdout or "").strip()
    if not out:
        return None
    try:
        return float(out)
    except ValueError:
        return None


# =========================================================================
# Classification
# =========================================================================

@dataclass
class Thresholds:
    trivial_max_s: float = 10 * 60        # < 10 min
    tv_short_max_s: float = 25 * 60       # < 25 min
    tv_episode_max_s: float = 50 * 60     # < 50 min
    tv_special_max_s: float = 75 * 60     # < 75 min
    movie_max_s: float = 240 * 60         # < 240 min (4 h)


def classify(duration_s: float, t: Thresholds) -> str:
    if duration_s < t.trivial_max_s:
        return "trivial"
    if duration_s < t.tv_short_max_s:
        return "tv_short"
    if duration_s < t.tv_episode_max_s:
        return "tv_episode"
    if duration_s < t.tv_special_max_s:
        return "tv_special"
    if duration_s < t.movie_max_s:
        return "movie"
    return "oversized"


def classification_severity(c: str) -> str:
    """Return a 'flag' / 'note' / 'ok' severity for the classification."""
    if c in ("tv_short", "tv_episode"):
        return "flag"     # almost certainly mislabeled
    if c in ("tv_special", "oversized"):
        return "note"     # worth a look but could be legit (concert, miniseries chunk)
    if c == "trivial":
        return "note"     # short — probably extras or trailer
    return "ok"           # 75-240 min, normal movie


# =========================================================================
# Per-title scan
# =========================================================================

@dataclass
class ScanResult:
    title_id: int
    matched_title: str
    target_folder: str
    video_path: str
    duration_s: Optional[float]
    classification: str
    severity: str
    notes: str = ""


def scan_title_main_video(plan_row: PlanRow,
                          inv_files: List[InventoryFile],
                          thresholds: Thresholds) -> ScanResult:
    """Probe the title's main video at its post-migration location."""
    tid = plan_row.title_id
    matched = plan_row.matched_title
    target = str(plan_row.target_folder)

    # Find the main video file on disk. The execute.py rename produced a path
    # of the form `<target>/<filename_template_stem>.<ext>`. We don't need
    # to perfectly reproduce the template here; just find the first video
    # file in the target folder.
    target_path = plan_row.target_folder
    if not target_path or not target_path.exists():
        return ScanResult(tid, matched, target, "", None, "missing_target", "flag",
                          "target folder does not exist")

    try:
        entries = [e for e in os.scandir(long(target_path))
                   if e.is_file() and Path(e.name).suffix.lower() in VIDEO_EXTS]
    except OSError as e:
        return ScanResult(tid, matched, target, "", None, "scan_failed", "flag",
                          f"could not scan: {e}")
    if not entries:
        return ScanResult(tid, matched, target, "", None, "no_video", "flag",
                          "no video file in target folder")

    # Use the largest video as the "main" — same heuristic execute.py uses
    entries.sort(key=lambda e: e.stat().st_size if hasattr(e, 'stat') else 0,
                 reverse=True)
    video_path = _norm_path(entries[0].path)

    duration = ffprobe_duration_seconds(video_path)
    if duration is None:
        return ScanResult(tid, matched, target, str(video_path), None,
                          "probe_failed", "flag", "ffprobe could not read duration")

    cls = classify(duration, thresholds)
    sev = classification_severity(cls)
    return ScanResult(tid, matched, target, str(video_path), duration, cls, sev)


# =========================================================================
# Output
# =========================================================================

def write_results_csv(path: Path, results: List[ScanResult]) -> None:
    headers = ["title_id", "severity", "classification", "duration_min",
               "matched_title", "video_path", "target_folder", "notes"]
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        w.writeheader()
        for r in results:
            w.writerow({
                "title_id": r.title_id,
                "severity": r.severity,
                "classification": r.classification,
                "duration_min": (f"{r.duration_s/60:.1f}" if r.duration_s else ""),
                "matched_title": r.matched_title,
                "video_path": r.video_path,
                "target_folder": r.target_folder,
                "notes": r.notes,
            })


def write_summary(path: Path, run_id: str, thresholds: Thresholds,
                  results: List[ScanResult]) -> None:
    by_cls: Dict[str, int] = {}
    by_sev: Dict[str, int] = {}
    for r in results:
        by_cls[r.classification] = by_cls.get(r.classification, 0) + 1
        by_sev[r.severity] = by_sev.get(r.severity, 0) + 1

    lines: List[str] = []
    lines.append(f"runtime_scan.py {VERSION} — run {run_id}")
    lines.append("")
    lines.append(f"Total titles probed: {len(results)}")
    lines.append("")
    lines.append("By classification:")
    order = ["trivial", "tv_short", "tv_episode", "tv_special", "movie",
             "oversized", "missing_target", "no_video", "probe_failed",
             "scan_failed"]
    for k in order:
        n = by_cls.get(k, 0)
        if n:
            lines.append(f"  {n:5d}  {k}")
    lines.append("")
    lines.append("By severity:")
    for k in ("flag", "note", "ok"):
        n = by_sev.get(k, 0)
        if n:
            lines.append(f"  {n:5d}  {k}")
    lines.append("")
    lines.append("Thresholds (in minutes):")
    lines.append(f"  trivial:    < {thresholds.trivial_max_s/60:.0f}")
    lines.append(f"  tv_short:   < {thresholds.tv_short_max_s/60:.0f}")
    lines.append(f"  tv_episode: < {thresholds.tv_episode_max_s/60:.0f}")
    lines.append(f"  tv_special: < {thresholds.tv_special_max_s/60:.0f}")
    lines.append(f"  movie:      < {thresholds.movie_max_s/60:.0f}")
    lines.append(f"  oversized:  >= {thresholds.movie_max_s/60:.0f}")
    lines.append("")

    # Show flagged (likely-TV) entries
    flagged = [r for r in results if r.severity == "flag"]
    if flagged:
        lines.append(f"Flagged entries (severity=flag) — first 50:")
        for r in flagged[:50]:
            dur = f"{r.duration_s/60:.1f}m" if r.duration_s else "??"
            lines.append(f"  [{r.classification:<14}] {dur:>7}  title {r.title_id}: {r.matched_title}")
        if len(flagged) > 50:
            lines.append(f"  ... and {len(flagged)-50} more (see runtime_scan.csv)")
        lines.append("")
    lines.append("Full per-title detail: see runtime_scan.csv")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# =========================================================================
# Main
# =========================================================================

def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="runtime_scan.py",
        description="ffprobe every main video and flag likely TV episodes by duration.",
    )
    p.add_argument("--config", default="config.json",
                   help="path to config.json")
    p.add_argument("--titles", default="",
                   help="comma-separated title_ids to probe exclusively")
    p.add_argument("--limit", type=int, default=0,
                   help="probe only N titles")
    p.add_argument("--include-skip", action="store_true",
                   help="also probe rows with action=SKIP (otherwise only AUTO+executed_at)")
    p.add_argument("--all-files", action="store_true",
                   help="probe ALL main videos in inventory.csv (regardless of plan.csv state). "
                        "Useful for spot-checking the unmoved leftover files at H:\\Movies root.")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        cfg = load_config(_norm_path(args.config))
    except Exception as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 3

    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    setup_logging(cfg.mediaprep_dir, run_id)

    thresholds = Thresholds()

    # Sanity: ffprobe in PATH?
    try:
        proc = subprocess.run(["ffprobe", "-version"], capture_output=True,
                              timeout=10, text=True)
        if proc.returncode != 0:
            log.error("ffprobe not working: %s", proc.stderr)
            return 4
    except (FileNotFoundError, OSError):
        log.error("ffprobe not found in PATH. Install ffmpeg and ensure ffprobe.exe is on PATH.")
        return 4

    plan_path = cfg.mediaprep_dir / "plan.csv"
    inv_path = cfg.mediaprep_dir / "inventory.csv"

    log.info("Reading plan.csv ...")
    plan = read_plan(plan_path, cfg.movies_root)
    log.info("Reading inventory.csv ...")
    inv_files_all, inv_by_parent = read_inventory(inv_path)
    sorted_parents = sorted(inv_by_parent.keys())

    inv_by_title: Dict[int, List[InventoryFile]] = {}
    for p in plan:
        files = title_files(p, inv_by_parent, sorted_parents)
        inv_by_title[p.title_id] = files
        attach_main_video_path(p, files)

    # Build target list
    candidates = []
    if args.all_files:
        # Everything in inventory.csv that's a main_video — uses original
        # paths so this catches files that didn't migrate to a target.
        log.info("Mode: --all-files (probing all main_video entries in inventory)")
        for f in inv_files_all:
            if f.role != "main_video":
                continue
            candidates.append(("inv", f))
    else:
        for r in plan:
            a = r.action.upper()
            if a == "AUTO" and r.executed_at:
                candidates.append(("plan", r))
            elif a == "SKIP" and args.include_skip:
                candidates.append(("plan", r))

    if args.titles:
        wanted = set(int(t) for t in args.titles.split(",") if t.strip())
        candidates = [(k, x) for (k, x) in candidates
                      if (k == "plan" and x.title_id in wanted)
                      or (k == "inv" and x.file_id in wanted)]
    if args.limit:
        candidates = candidates[:args.limit]

    log.info("Probing %d videos. Estimated time: %d-%d minutes.",
             len(candidates), len(candidates) // 60, max(1, len(candidates) // 30))

    results: List[ScanResult] = []
    t0 = time.time()
    for i, (kind, item) in enumerate(candidates, 1):
        if i % 100 == 0 or i == len(candidates):
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed > 0 else 0
            log.info("[%d/%d] probed (%.1f videos/sec)", i, len(candidates), rate)

        if kind == "plan":
            r = item
            inv_files = inv_by_title.get(r.title_id) or []
            try:
                result = scan_title_main_video(r, inv_files, thresholds)
            except Exception as e:
                log.exception("title %s: scan crashed", r.title_id)
                result = ScanResult(r.title_id, r.matched_title,
                                    str(r.target_folder), "", None,
                                    "scan_crash", "flag",
                                    f"{type(e).__name__}: {e}")
            results.append(result)
        else:
            # kind == "inv": probe a raw inventory file (used by --all-files)
            f = item
            duration = ffprobe_duration_seconds(f.abs_path)
            if duration is None:
                results.append(ScanResult(f.file_id, "", "", str(f.abs_path),
                                          None, "probe_failed", "flag",
                                          "ffprobe failed"))
                continue
            cls = classify(duration, thresholds)
            sev = classification_severity(cls)
            results.append(ScanResult(f.file_id, "", "", str(f.abs_path),
                                       duration, cls, sev))

    log.info("Probed %d videos in %.1fs", len(results), time.time() - t0)

    # Sort by severity (flag first), then duration (shortest first)
    results.sort(key=lambda r: (
        {"flag": 0, "note": 1, "ok": 2}.get(r.severity, 3),
        r.duration_s if r.duration_s is not None else 0,
    ))

    csv_path = cfg.mediaprep_dir / "runtime_scan.csv"
    summary_path = cfg.mediaprep_dir / "runtime_scan_report.txt"
    write_results_csv(csv_path, results)
    write_summary(summary_path, run_id, thresholds, results)
    log.info("Wrote: %s", csv_path)
    log.info("Wrote: %s", summary_path)

    flagged = sum(1 for r in results if r.severity == "flag")
    log.info("Summary: %d flagged, %d notes, %d ok",
             flagged,
             sum(1 for r in results if r.severity == "note"),
             sum(1 for r in results if r.severity == "ok"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
