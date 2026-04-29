#!/usr/bin/env python3
"""
verify.py — Phase 4 of the movie library reorganization pipeline.

A read-only sweep over H:\\Movies that confirms the migration matches what
plan.csv says was executed. Default checks are fast (folder/file existence,
NFO IDs, naming patterns, file sizes). `--hash-verify` adds a re-hash of
every main video for byte-integrity (slow — hours).

Usage:
    py verify.py --config config.json                       # fast verify
    py verify.py --config config.json --hash-verify         # also re-hash
    py verify.py --config config.json --titles 42,87,143    # subset
    py verify.py --config config.json --include-skip        # also check SKIP
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

# Reuse helpers from execute.py — they're in the same directory.
sys.path.insert(0, str(Path(__file__).parent))
from execute import (  # type: ignore
    Config,
    InventoryFile,
    PlanRow,
    VIDEO_EXTS,
    SUBTITLE_EXTS,
    NFO_EXTS,
    IMAGE_EXTS,
    SPECIAL_IMAGE_NAMES,
    _nfc,
    _norm_path,
    _read_csv_dicts,
    _unmojibake,
    attach_main_video_path,
    hash_file,
    load_config,
    long,
    read_duplicates,
    read_inventory,
    read_plan,
    render_filename_template,
    same_drive,
    sanitize_for_windows,
    title_files,
)


VERSION = "1.0.0"
log = logging.getLogger("verify")


# =========================================================================
# Logging
# =========================================================================

def setup_logging(mediaprep: Path, run_id: str) -> Path:
    mediaprep.mkdir(parents=True, exist_ok=True)
    log_path = mediaprep / f"verify_{run_id}.log"
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    log.handlers.clear()
    log.setLevel(logging.DEBUG)
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(logging.Formatter(fmt))
    fh.setLevel(logging.DEBUG)
    log.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter(fmt))
    sh.setLevel(logging.INFO)
    log.addHandler(sh)
    log.info("verify.py v%s starting; log file: %s", VERSION, log_path)
    return log_path


# =========================================================================
# Issue model
# =========================================================================

@dataclass
class Issue:
    title_id: int
    code: str           # e.g. "target_missing", "video_missing", "size_drift"
    severity: str       # "error" | "warn" | "info"
    message: str
    path: str = ""
    matched_title: str = ""


# =========================================================================
# NFO parsing
# =========================================================================

def parse_nfo_ids(path: Path) -> Tuple[str, str, str, str]:
    """Return (tmdbid, imdbid, title, year) from an NFO file. Empty
    strings on parse failure or missing tags."""
    try:
        with open(long(path), "rb") as f:
            data = f.read()
        root = ET.fromstring(data)
    except (OSError, ET.ParseError):
        return ("", "", "", "")

    def t(tag: str) -> str:
        e = root.find(tag)
        return (e.text or "").strip() if e is not None and e.text else ""

    return (t("tmdbid"), t("imdbid"), t("title"), t("year"))


# =========================================================================
# Per-title checks
# =========================================================================

def expected_main_video_path(plan_row: PlanRow, cfg: Config) -> Path:
    """Compute the expected destination main video path for a plan row."""
    fields = {
        "title": plan_row.matched_title,
        "year": plan_row.matched_year,
        "imdb_id": plan_row.imdb_id,
        "tmdb_id": plan_row.tmdb_id,
        "matched_title_lowercase": plan_row.matched_title.lower(),
    }
    stem = render_filename_template(cfg.filename_template, fields)
    # Use the source video's extension (preserving lowercase). Fall back to
    # .mkv if we can't tell (no main_video_path attached).
    if plan_row.main_video_path and plan_row.main_video_path.suffix:
        ext = plan_row.main_video_path.suffix.lower()
    elif plan_row.main_video_file and "." in plan_row.main_video_file:
        ext = "." + plan_row.main_video_file.rsplit(".", 1)[-1].lower()
    else:
        ext = ".mkv"
    return _norm_path(str(plan_row.target_folder) + "\\" + stem + ext)


def check_duplicate_loser(plan_row: PlanRow,
                          inv_files: List[InventoryFile],
                          cfg: Config) -> List[Issue]:
    """Verify a dup-loser row: its inventory files should now live under
    cfg.duplicates_dir (mirroring their original H:\\Movies-relative path),
    NOT at target_folder. We don't size/hash check losers — just existence."""
    issues: List[Issue] = []
    tid = plan_row.title_id
    matched = plan_row.matched_title

    def add(code: str, severity: str, message: str, path: str = "") -> None:
        issues.append(Issue(tid, code, severity, message, path, matched))

    movies_root = cfg.movies_root
    main_inv = next((f for f in inv_files if f.role == "main_video"), None)
    if main_inv is None:
        # No main video in inventory — can't verify. Note as warn.
        add("dup_loser_no_inventory", "warn",
            "dup-loser row has no main_video in inventory")
        return issues

    # Compute the expected path under duplicates_dir
    try:
        rel = main_inv.abs_path.resolve().relative_to(movies_root.resolve())
    except (OSError, ValueError):
        rel = Path(main_inv.abs_path.name)
    expected = cfg.duplicates_dir / rel

    if expected.exists():
        # Check size matches (sanity)
        try:
            actual_size = os.path.getsize(long(expected))
            if main_inv.size_bytes and actual_size != main_inv.size_bytes:
                add("dup_loser_size_drift", "warn",
                    f"size {actual_size} != inventory {main_inv.size_bytes}",
                    str(expected))
        except OSError:
            pass
        return issues

    # Not at expected dup path. Check the original location.
    if main_inv.abs_path.exists():
        # File still at original location — Pass 2 didn't actually move it
        # (probably user marked SKIP after the fact, or Pass 2 skipped due to
        # plan/dups disagreement which is fine).
        add("dup_loser_still_at_source", "info",
            f"loser file still at original location: {main_inv.abs_path}",
            str(main_inv.abs_path))
        return issues

    # File missing from both locations.
    add("dup_loser_missing", "error",
        f"loser file not at duplicates_dir or original location: {expected}",
        str(expected))
    return issues


def check_title(plan_row: PlanRow,
                inv_files: List[InventoryFile],
                cfg: Config,
                hash_verify: bool) -> List[Issue]:
    """Check one AUTO+executed_at row. Returns a list of issues (empty if OK)."""
    issues: List[Issue] = []
    tid = plan_row.title_id
    matched = plan_row.matched_title

    def add(code: str, severity: str, message: str, path: str = "") -> None:
        issues.append(Issue(tid, code, severity, message, path, matched))

    # 0. Dup-loser rows don't go to target_folder; they go to duplicates_dir.
    # Verify they actually landed there instead. (The keeper of the same group
    # is the one that holds the canonical file at target_folder.)
    if plan_row.duplicate_role.startswith("duplicate_of:"):
        return check_duplicate_loser(plan_row, inv_files, cfg)

    # 1. Target folder must exist
    if not plan_row.target_folder or str(plan_row.target_folder) in ("", "."):
        add("target_folder_missing_field", "error",
            "plan.csv has executed_at but target_folder is empty")
        return issues

    target = plan_row.target_folder
    if not target.exists():
        add("target_folder_missing", "error",
            f"target folder does not exist: {target}",
            str(target))
        return issues  # rest of checks meaningless without folder

    # 2. Main video file exists at expected path
    expected_video = expected_main_video_path(plan_row, cfg)
    if not expected_video.exists():
        # Try to find ANY video file in the target folder (template might
        # not perfectly reproduce the actual filename if user edited).
        try:
            entries = list(os.scandir(long(target)))
        except OSError as e:
            add("target_folder_unreadable", "error",
                f"cannot list target folder: {e}", str(target))
            return issues
        video_candidates = [e for e in entries
                            if e.is_file() and Path(e.name).suffix.lower() in VIDEO_EXTS]
        if not video_candidates:
            add("video_missing", "error",
                f"no main video found in target folder",
                str(expected_video))
            return issues
        # Some video exists; report mismatch as a warning rather than error
        actual = video_candidates[0].path
        add("video_name_mismatch", "warn",
            f"expected {expected_video.name}, found {Path(actual).name}",
            actual)
        actual_video = _norm_path(actual)
    else:
        actual_video = expected_video

    # 3. Video file size matches inventory
    inventory_main = next((f for f in inv_files if f.role == "main_video"), None)
    if inventory_main and inventory_main.size_bytes:
        try:
            actual_size = os.path.getsize(long(actual_video))
            if actual_size != inventory_main.size_bytes:
                add("size_drift", "error",
                    f"video size {actual_size} != inventory {inventory_main.size_bytes}",
                    str(actual_video))
        except OSError as e:
            add("video_unreadable", "error",
                f"cannot stat video: {e}", str(actual_video))

    # 4. NFO exists
    expected_nfo = actual_video.with_suffix(".nfo")
    if not expected_nfo.exists():
        # Maybe NFO disabled via --no-nfo. Treat as warn, not error.
        add("nfo_missing", "warn",
            f"NFO not found at {expected_nfo.name}",
            str(expected_nfo))
    else:
        # 5. NFO has matching tmdbid (and imdbid if plan has one)
        nfo_tmdb, nfo_imdb, nfo_title, nfo_year = parse_nfo_ids(expected_nfo)
        if not nfo_tmdb and not nfo_imdb:
            add("nfo_unparseable", "error",
                "NFO has no tmdbid or imdbid", str(expected_nfo))
        else:
            if plan_row.tmdb_id and nfo_tmdb and nfo_tmdb != plan_row.tmdb_id:
                add("nfo_tmdb_mismatch", "error",
                    f"NFO tmdbid={nfo_tmdb} but plan.csv tmdb_id={plan_row.tmdb_id}",
                    str(expected_nfo))
            if plan_row.imdb_id and nfo_imdb and nfo_imdb != plan_row.imdb_id:
                add("nfo_imdb_mismatch", "error",
                    f"NFO imdbid={nfo_imdb} but plan.csv imdb_id={plan_row.imdb_id}",
                    str(expected_nfo))

    # 6. Hash verify (opt-in)
    if hash_verify and inventory_main and inventory_main.size_bytes:
        # Only check if inventory has a hash (phase 1 may have skipped hashing
        # via --skip-hash). Inventory's file_hash field is read in execute.py;
        # we have to pull it from the raw row here.
        inv_hash = getattr(inventory_main, "file_hash", "")
        if inv_hash:
            try:
                actual_hash = hash_file(actual_video, cfg.hash_algo)
                if actual_hash.lower() != inv_hash.lower():
                    add("hash_mismatch", "error",
                        f"video hash differs from inventory ({cfg.hash_algo})",
                        str(actual_video))
            except OSError as e:
                add("hash_failed", "warn",
                    f"could not hash video: {e}", str(actual_video))

    # 7. Folder name pattern check (lightweight)
    folder_name = target.name
    if "{imdb-tt" not in folder_name and "{tmdb-" not in folder_name:
        # Maybe a franchise member — check if the parent has it
        # If neither folder nor parent has the imdb-tt pattern, warn.
        parent_name = target.parent.name if target.parent else ""
        if "{imdb-tt" not in parent_name and "{tmdb-" not in parent_name:
            add("folder_name_pattern", "warn",
                f"target folder name has no ID marker: {folder_name}")

    return issues


# =========================================================================
# Cross-library checks
# =========================================================================

_IMDB_FROM_FOLDER_RX = re.compile(r"\{imdb-(tt\d+)\}")
_TMDB_FROM_FOLDER_RX = re.compile(r"\{tmdb-(\d+)\}")


def find_imdb_in_name(name: str) -> str:
    m = _IMDB_FROM_FOLDER_RX.search(name)
    return m.group(1) if m else ""


def cross_library_checks(plan: List[PlanRow],
                         cfg: Config) -> Tuple[List[Issue], List[Dict[str, Any]]]:
    """Walk movies_root and detect:
    - Two folders sharing the same IMDB ID
    - Two AUTO rows with the same target_folder/path
    Also returns a list of "extras" — folders not accounted for in plan.csv.
    """
    issues: List[Issue] = []
    extras: List[Dict[str, Any]] = []

    # 1. Duplicate target paths in plan.csv (sanity).
    # Skip dup-loser rows: they don't actually go to target_folder
    # (they go to duplicates_dir), so colliding with their keeper is by design.
    final_paths: Dict[str, int] = {}
    for r in plan:
        if not r.executed_at or r.action.upper() != "AUTO":
            continue
        if not r.target_folder:
            continue
        if r.duplicate_role.startswith("duplicate_of:"):
            continue
        path = expected_main_video_path(r, cfg)
        key = str(path).lower()
        if key in final_paths:
            issues.append(Issue(r.title_id, "duplicate_target", "error",
                                f"two AUTO rows resolve to {key} (also title {final_paths[key]})",
                                str(path), r.matched_title))
        else:
            final_paths[key] = r.title_id

    # 2. Walk movies_root, find {imdb-tt} folders. Detect duplicate IDs.
    movies_root = cfg.movies_root
    if not movies_root.exists():
        log.warning("movies_root %s does not exist", movies_root)
        return issues, extras

    # Build a mapping: imdb_id -> [folder paths]
    imdb_map: Dict[str, List[Path]] = {}
    walked_folders: List[Path] = []

    def walk_for_imdb(root: Path, depth: int = 0) -> None:
        if depth > 3:
            return
        try:
            entries = list(os.scandir(long(root)))
        except OSError as e:
            log.warning("could not scan %s: %s", root, e)
            return
        for e in entries:
            if not e.is_dir():
                continue
            sub = _norm_path(e.path)
            walked_folders.append(sub)
            imdb = find_imdb_in_name(e.name)
            if imdb:
                imdb_map.setdefault(imdb, []).append(sub)
            else:
                # Recurse one more level — could be a franchise parent
                walk_for_imdb(sub, depth + 1)

    walk_for_imdb(movies_root)

    # 3. Duplicate IMDB IDs
    for imdb, paths in imdb_map.items():
        if len(paths) > 1:
            issues.append(Issue(0, "duplicate_imdb_id", "error",
                                f"IMDB id {imdb} appears in {len(paths)} folders: "
                                + " | ".join(str(p) for p in paths),
                                ""))

    # 4. Reconcile {imdb-tt} folders against plan.csv
    plan_imdb_to_tid = {r.imdb_id.lower(): r.title_id for r in plan
                       if r.imdb_id and r.executed_at and r.action.upper() == "AUTO"}
    for imdb, paths in imdb_map.items():
        if imdb.lower() not in plan_imdb_to_tid:
            for p in paths:
                extras.append({
                    "kind": "imdb_folder_not_in_plan",
                    "path": str(p),
                    "imdb_id": imdb,
                    "note": "folder has imdb tag but no matching plan.csv row",
                })

    # 5. Plan rows that should have folders but don't
    folder_paths_lower = {str(p).lower() for p in walked_folders}
    for r in plan:
        if not r.executed_at or r.action.upper() != "AUTO":
            continue
        if not r.target_folder or str(r.target_folder) in ("", "."):
            continue
        if str(r.target_folder).lower() not in folder_paths_lower:
            # Already caught per-title; don't double-report
            pass

    return issues, extras


# =========================================================================
# Report writers
# =========================================================================

def write_issues_csv(path: Path, issues: List[Issue]) -> None:
    headers = ["title_id", "severity", "code", "matched_title", "message", "path"]
    target = path
    try:
        f = open(target, "w", encoding="utf-8", newline="")
    except PermissionError:
        ts = time.strftime("%Y%m%d_%H%M%S")
        target = path.with_name(f"{path.stem}_{ts}{path.suffix}")
        log.warning("Could not write %s; writing %s instead", path, target)
        f = open(target, "w", encoding="utf-8", newline="")
    with f:
        w = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        w.writeheader()
        for i in issues:
            w.writerow({
                "title_id": i.title_id,
                "severity": i.severity,
                "code": i.code,
                "matched_title": i.matched_title,
                "message": i.message,
                "path": i.path,
            })


def write_extras_csv(path: Path, extras: List[Dict[str, Any]]) -> None:
    if not extras:
        return
    headers = ["kind", "path", "imdb_id", "note"]
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        w.writeheader()
        for e in extras:
            w.writerow(e)


def write_report(path: Path, run_id: str, hash_verify: bool,
                 totals: Dict[str, int], by_code: Dict[str, int],
                 issues: List[Issue]) -> None:
    lines: List[str] = []
    lines.append(f"verify.py {VERSION} — run {run_id}")
    lines.append(f"Mode: {'FAST + HASH' if hash_verify else 'FAST'}")
    lines.append("")
    lines.append("Totals:")
    lines.append(f"  Titles checked:           {totals.get('checked', 0)}")
    lines.append(f"  Titles fully verified:    {totals.get('clean', 0)}")
    lines.append(f"  Titles with errors:       {totals.get('errors', 0)}")
    lines.append(f"  Titles with warnings:     {totals.get('warnings', 0)}")
    lines.append(f"  Titles skipped (no exec): {totals.get('skipped_no_exec', 0)}")
    lines.append(f"  Titles skipped (filter):  {totals.get('filtered', 0)}")
    lines.append(f"  Cross-library issues:     {totals.get('cross_issues', 0)}")
    lines.append(f"  Extras (folders w/o plan):{totals.get('extras', 0)}")
    lines.append("")
    if by_code:
        lines.append("Issues by code:")
        for code, count in sorted(by_code.items(), key=lambda x: -x[1]):
            lines.append(f"  {count:5d}  {code}")
        lines.append("")
    # Sample of error issues
    error_samples = [i for i in issues if i.severity == "error"][:30]
    if error_samples:
        lines.append("First 30 errors:")
        for i in error_samples:
            lines.append(f"  title {i.title_id} [{i.code}]: {i.message}")
        lines.append("")
    lines.append(f"Full per-title issue list: see verify_issues.csv")
    lines.append(f"Folders not in plan.csv:   see verify_extras.csv")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# =========================================================================
# Inventory hash field reader
# =========================================================================
# read_inventory in execute.py doesn't preserve file_hash. We need it for
# --hash-verify, so re-read inventory.csv minimally to attach it.

def attach_inventory_hashes(inv_files: List[InventoryFile],
                            inv_csv: Path) -> None:
    if not inv_csv.exists():
        return
    by_path: Dict[str, str] = {}
    for r in _read_csv_dicts(inv_csv):
        ap = (r.get("abs_path") or "").strip()
        h = (r.get("file_hash") or "").strip()
        if ap and h:
            by_path[ap.lower().replace("/", "\\")] = h
    for f in inv_files:
        key = str(f.abs_path).lower().replace("/", "\\")
        h = by_path.get(key)
        if h:
            # Stash on the dataclass via setattr (it's not in the dataclass
            # fields, so this is a dynamic attribute).
            setattr(f, "file_hash", h)


# =========================================================================
# Main
# =========================================================================

def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="verify.py",
        description="Phase 4: verify the migration matches plan.csv.",
    )
    p.add_argument("--config", default="config.json",
                   help="path to config.json (default: ./config.json)")
    p.add_argument("--hash-verify", action="store_true",
                   help="re-hash main video files and compare to inventory hashes (slow)")
    p.add_argument("--titles", default="",
                   help="comma-separated title_ids to verify exclusively")
    p.add_argument("--include-skip", action="store_true",
                   help="also verify rows with action=SKIP")
    p.add_argument("--limit", type=int, default=0,
                   help="check only N titles (smoke testing)")
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
    log.info("Mode: %s", "FAST + HASH" if args.hash_verify else "FAST")

    plan_path = cfg.mediaprep_dir / "plan.csv"
    inv_path = cfg.mediaprep_dir / "inventory.csv"
    dup_path = cfg.mediaprep_dir / "duplicates.csv"

    log.info("Reading plan.csv ...")
    plan = read_plan(plan_path, cfg.movies_root)
    log.info("Reading inventory.csv ...")
    inv_files_all, inv_by_parent = read_inventory(inv_path)
    sorted_parents = sorted(inv_by_parent.keys())

    # Attach inventory file lists + main_video_path to each plan row.
    log.info("Mapping inventory to plan rows ...")
    inv_by_title: Dict[int, List[InventoryFile]] = {}
    for p in plan:
        files = title_files(p, inv_by_parent, sorted_parents)
        inv_by_title[p.title_id] = files
        attach_main_video_path(p, files)

    if args.hash_verify:
        log.info("Loading inventory hashes for hash-verify ...")
        for tid, files in inv_by_title.items():
            attach_inventory_hashes(files, inv_path)
        log.info("Hashing each main video; this will take a while ...")

    # Filter to AUTO+executed_at rows (or also SKIP if requested)
    candidates = []
    for r in plan:
        a = r.action.upper()
        if a == "AUTO" and r.executed_at:
            candidates.append(r)
        elif a == "SKIP" and args.include_skip:
            candidates.append(r)

    filtered_count = 0
    if args.titles:
        wanted = set(int(t) for t in args.titles.split(",") if t.strip())
        before = len(candidates)
        candidates = [r for r in candidates if r.title_id in wanted]
        filtered_count = before - len(candidates)
    if args.limit:
        before = len(candidates)
        candidates = candidates[:args.limit]
        filtered_count += before - len(candidates)

    skipped_no_exec = sum(1 for r in plan
                          if r.action.upper() == "AUTO" and not r.executed_at)
    log.info("Verifying %d titles (skipped %d without executed_at, %d via filter)",
             len(candidates), skipped_no_exec, filtered_count)

    # Per-title checks
    all_issues: List[Issue] = []
    titles_with_errors: Set[int] = set()
    titles_with_warnings: Set[int] = set()
    t0 = time.time()

    for i, r in enumerate(candidates, 1):
        if i % 100 == 0 or i == len(candidates):
            log.info("[%d/%d] verifying ...", i, len(candidates))
        inv_files = inv_by_title.get(r.title_id) or []
        try:
            issues = check_title(r, inv_files, cfg, args.hash_verify)
        except Exception as e:
            log.exception("title %s: unhandled exception in check_title", r.title_id)
            issues = [Issue(r.title_id, "verify_crash", "error",
                            f"{type(e).__name__}: {e}",
                            "", r.matched_title)]
        for issue in issues:
            all_issues.append(issue)
            if issue.severity == "error":
                titles_with_errors.add(r.title_id)
            elif issue.severity == "warn":
                titles_with_warnings.add(r.title_id)

    log.info("Per-title checks complete in %.1fs", time.time() - t0)

    # Cross-library checks
    log.info("Running cross-library checks ...")
    cross_issues, extras = cross_library_checks(plan, cfg)
    all_issues.extend(cross_issues)

    # Aggregate
    by_code: Dict[str, int] = {}
    for issue in all_issues:
        by_code[issue.code] = by_code.get(issue.code, 0) + 1

    clean_count = len(candidates) - len(titles_with_errors | titles_with_warnings)
    totals = {
        "checked": len(candidates),
        "clean": max(0, clean_count),
        "errors": len(titles_with_errors),
        "warnings": len(titles_with_warnings - titles_with_errors),
        "skipped_no_exec": skipped_no_exec,
        "filtered": filtered_count,
        "cross_issues": len(cross_issues),
        "extras": len(extras),
    }

    log.info("Summary: %d clean, %d with errors, %d with warnings (of %d checked)",
             totals["clean"], totals["errors"], totals["warnings"], totals["checked"])
    log.info("Cross-library issues: %d. Extra folders: %d",
             totals["cross_issues"], totals["extras"])

    # Write outputs
    issues_path = cfg.mediaprep_dir / "verify_issues.csv"
    extras_path = cfg.mediaprep_dir / "verify_extras.csv"
    report_path = cfg.mediaprep_dir / "verify_report.txt"
    write_issues_csv(issues_path, all_issues)
    write_extras_csv(extras_path, extras)
    write_report(report_path, run_id, args.hash_verify, totals, by_code, all_issues)

    log.info("Wrote: %s", issues_path)
    if extras:
        log.info("Wrote: %s", extras_path)
    log.info("Wrote: %s", report_path)

    # Exit code: 0 = clean (or all warnings only), 1 = errors found
    return 0 if totals["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
