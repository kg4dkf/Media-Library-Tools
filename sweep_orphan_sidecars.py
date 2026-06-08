#!/usr/bin/env python3
"""
sweep_orphan_sidecars.py - Sweep up sidecar files orphaned by earlier
execute_tv runs (pre-Stage G).

Some early --apply runs (before v1.3.0) moved episode video files but
left their .nfo / .srt / -thumb.jpg companions in the source folder.
This script walks every moves_tv.jsonl, finds those successful moves,
then for each AUTO+executed source folder looks for leftover sidecars
whose "base stem" matches a moved video — and relocates them to sit
alongside their canonical sibling under the target series.

Usage:
    py -3.13 sweep_orphan_sidecars.py --config config.json
    py -3.13 sweep_orphan_sidecars.py --config config.json --apply

Default mode is DRY-RUN. Every action is journaled to a separate
sweep_orphan_<run_id>.jsonl so this script's actions are auditable
and (if needed) reversible.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import glob
import json
import logging
import os
import re
import shutil
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple


SCRIPT_VERSION = "1.0.0"
LONG_PATH_PREFIX = "\\\\?\\"

log = logging.getLogger("sweep_orphan_sidecars")


# Sidecar extensions we know how to relocate.
SIDECAR_EXTS: Set[str] = {
    ".nfo", ".xml",
    ".srt", ".sub", ".idx", ".ass", ".ssa", ".vtt", ".sup", ".smi",
    ".jpg", ".jpeg", ".png", ".tbn", ".webp", ".gif",
}

# Video extensions — we use these to decide if a sidecar's base-stem video
# is still present in the source folder (and therefore NOT an orphan).
VIDEO_EXTS: Set[str] = {
    ".mkv", ".mp4", ".avi", ".mov", ".wmv", ".m4v", ".webm", ".flv",
    ".mpeg", ".mpg", ".ts", ".m2ts", ".mts", ".vob", ".divx", ".ogm",
}

# Recognized suffixes that decorate a sidecar stem (e.g., "-thumb").
# Stripping these from a sidecar stem yields the "base stem" we use to
# look up the matching video in the journal.
_SUFFIX_DECORATIONS: tuple = (
    "-thumb", "-poster", "-fanart", "-banner", "-landscape",
    "-clearart", "-clearlogo", "-logo",
)


# =========================================================================
# Path helpers (same conventions as execute_tv.py / rollback_tv.py)
# =========================================================================

def long(p: Path) -> str:
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


def same_drive(a: Path, b: Path) -> bool:
    try:
        da = os.path.splitdrive(str(a))[0].lower()
        db = os.path.splitdrive(str(b))[0].lower()
        return da == db and da != ""
    except Exception:
        return False


def safe_exists(p: Path) -> bool:
    try:
        return os.path.exists(long(p))
    except OSError:
        return False


def stem_of_pathstr(path_str: str) -> str:
    """Return the stem of a Windows OR POSIX path string regardless of
    which OS we're running on. Splits on both `/` and `\\` so Windows
    paths in the journal parse correctly even when the script is being
    smoke-tested on POSIX."""
    name = path_str.replace("\\", "/").rsplit("/", 1)[-1]
    if "." in name:
        return name.rsplit(".", 1)[0]
    return name


def sanitize_filename(name: str) -> str:
    """Mirror execute_tv.sanitize_for_windows just enough to avoid
    introducing illegal chars when synthesizing target filenames."""
    repl = {":": " - ", "/": " ", "\\": " ", '"': "'", "|": "-",
            "?": "", "*": "", "<": "(", ">": ")"}
    out = name
    for k, v in repl.items():
        out = out.replace(k, v)
    out = re.sub(r"\s+", " ", out).strip()
    return out


# =========================================================================
# Sidecar stem analysis
# =========================================================================

def base_stem_for_sidecar(sidecar_path: Path) -> str:
    """Return the stem of the video file this sidecar would have
    accompanied, by stripping recognized suffix decorations and language
    tags."""
    stem = sidecar_path.stem
    # 1. Strip "-thumb"/"-poster"/etc suffixes.
    low = stem.lower()
    for suf in _SUFFIX_DECORATIONS:
        if low.endswith(suf):
            return stem[:-len(suf)]
    # 2. Strip ".<lang>" tag for subtitle files (.en.srt -> base + ".en").
    #    Only strip if the trailing token after the last "." is 2-3 alpha chars.
    if "." in stem:
        head, tail = stem.rsplit(".", 1)
        if 2 <= len(tail) <= 3 and tail.isalpha():
            return head
    return stem


def sidecar_decoration(sidecar_path: Path) -> str:
    """Return the decoration that base_stem_for_sidecar() would strip,
    so we can re-attach it when generating the canonical target name.
    Empty string if no decoration present."""
    stem = sidecar_path.stem
    low = stem.lower()
    for suf in _SUFFIX_DECORATIONS:
        if low.endswith(suf):
            return stem[-len(suf):]
    if "." in stem:
        head, tail = stem.rsplit(".", 1)
        if 2 <= len(tail) <= 3 and tail.isalpha():
            return "." + tail
    return ""


# =========================================================================
# Journal scanning
# =========================================================================

def find_journal_files(mediaprep_dir: Path) -> List[Path]:
    """Locate every moves_tv*.jsonl in mediaprep_dir, oldest first."""
    out: List[Path] = []
    if not mediaprep_dir.is_dir():
        return out
    for child in mediaprep_dir.iterdir():
        if not child.is_file():
            continue
        name = child.name.lower()
        if name.endswith(".jsonl") and name.startswith(("moves_tv",)):
            # Skip dry-run journals (those records aren't real moves).
            if "dryrun" in name or "dry_run" in name:
                continue
            out.append(child)
    out.sort(key=lambda p: p.stat().st_mtime)
    return out


def build_source_to_target_map(journal_files: Iterable[Path]
                                ) -> Dict[str, str]:
    """Walk all journal files; for every successful rename/copy with an
    episode_key, record source_stem (lowercased) -> target_path. Later
    journals overwrite earlier ones on the same key (so we always have
    the most recent move's destination)."""
    out: Dict[str, str] = {}
    for jf in journal_files:
        try:
            fh = open(jf, "r", encoding="utf-8", errors="replace")
        except OSError as e:
            log.warning("Skipping %s: %s", jf, e)
            continue
        with fh:
            for ln, raw in enumerate(fh, start=1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    r = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if r.get("op") not in ("rename", "copy"):
                    continue
                if r.get("result") != "ok":
                    continue
                if not r.get("episode_key"):
                    continue
                src = r.get("from_") or ""
                tgt = r.get("to") or ""
                if not src or not tgt:
                    continue
                src_stem = stem_of_pathstr(src).lower()
                if src_stem:
                    out[src_stem] = tgt
    return out


# =========================================================================
# Sweep journal writer
# =========================================================================

class SweepJournal:
    def __init__(self, path: Path, run_id: str, dry_run: bool):
        self.path = path
        self.run_id = run_id
        self.dry_run = dry_run
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(path, "a", encoding="utf-8", newline="")

    def write(self, **fields: Any) -> None:
        rec: Dict[str, Any] = {
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"),
            "run_id": self.run_id,
        }
        rec.update(fields)
        if self.dry_run and rec.get("result") not in ("dry-run", "skipped"):
            rec["result"] = "dry-run"
        self._fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._fh.flush()
        try:
            os.fsync(self._fh.fileno())
        except OSError:
            pass

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass


# =========================================================================
# Plan reader
# =========================================================================

def read_plan_rows(plan_path: Path) -> List[Dict[str, str]]:
    if not plan_path.is_file():
        raise SystemExit(f"series_plan.csv not found: {plan_path}")
    raw = plan_path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = raw.decode("utf-16", errors="replace")
    else:
        text = raw.replace(b"\x00", b"").decode("utf-8-sig", errors="replace")
    return list(csv.DictReader(text.splitlines()))


# =========================================================================
# Sweep one source folder
# =========================================================================

@dataclass
class SweepStats:
    series_id: int = 0
    swept: int = 0
    no_match: int = 0
    skipped_video_present: int = 0
    errors: int = 0


def sweep_source_folder(source_folder: Path, series_id: int,
                         src_to_tgt: Dict[str, str],
                         journal: SweepJournal,
                         dry_run: bool) -> SweepStats:
    """Walk source_folder; for each leftover sidecar whose base stem
    points to an already-moved video, rename + relocate it next to the
    canonical sibling in target."""
    stats = SweepStats(series_id=series_id)
    if not source_folder.is_dir():
        return stats

    # Index videos still present in source so we can skip non-orphans.
    videos_present: Set[str] = set()
    try:
        for child in source_folder.rglob("*"):
            try:
                if not child.is_file():
                    continue
            except OSError:
                continue
            if child.suffix.lower() in VIDEO_EXTS:
                videos_present.add(child.stem.lower())
    except OSError:
        pass

    try:
        children = list(source_folder.rglob("*"))
    except OSError as e:
        log.warning("[series %d] cannot walk %s: %s",
                    series_id, source_folder, e)
        return stats

    for sidecar in children:
        try:
            if not sidecar.is_file():
                continue
        except OSError:
            continue
        ext = sidecar.suffix.lower()
        if ext not in SIDECAR_EXTS:
            continue
        base = base_stem_for_sidecar(sidecar)
        # If the sidecar's base-stem video is still present here, this
        # sidecar isn't an orphan — Stage G of a future --apply will
        # carry it along when the video moves.
        if base.lower() in videos_present:
            stats.skipped_video_present += 1
            continue

        target = src_to_tgt.get(base.lower())
        if not target:
            stats.no_match += 1
            continue

        target_path = Path(target)
        target_dir = target_path.parent
        target_video_stem = target_path.stem
        decoration = sidecar_decoration(sidecar)
        new_name = sanitize_filename(target_video_stem + decoration + sidecar.suffix)
        new_path = target_dir / new_name

        if safe_exists(new_path):
            # Already there (perhaps from an earlier sweep). Skip silently.
            journal.write(series_id=series_id, op="sweep_skip",
                          from_=str(sidecar), to=str(new_path),
                          reason="dst_already_exists",
                          result="skipped")
            continue

        if dry_run:
            journal.write(series_id=series_id, op="sweep_move",
                          from_=str(sidecar), to=str(new_path),
                          base_stem=base, decoration=decoration,
                          result="dry-run")
            stats.swept += 1
            continue

        # Real move.
        try:
            if not safe_exists(target_dir):
                os.makedirs(long(target_dir), exist_ok=True)
            if same_drive(sidecar, new_path):
                os.rename(long(sidecar), long(new_path))
            else:
                shutil.copy2(long(sidecar), long(new_path))
                os.remove(long(sidecar))
            journal.write(series_id=series_id, op="sweep_move",
                          from_=str(sidecar), to=str(new_path),
                          base_stem=base, decoration=decoration,
                          result="ok")
            stats.swept += 1
        except OSError as e:
            journal.write(series_id=series_id, op="sweep_move",
                          from_=str(sidecar), to=str(new_path),
                          result="error", error=str(e),
                          error_class=type(e).__name__)
            stats.errors += 1

    return stats


# =========================================================================
# Main
# =========================================================================

def setup_logging(out_dir: Path, run_id: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / f"sweep_orphan_{run_id}.log"
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt); fh.setLevel(logging.DEBUG)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt); sh.setLevel(logging.INFO)
    log.setLevel(logging.DEBUG)
    if not log.handlers:
        log.addHandler(fh); log.addHandler(sh)
    log.info("sweep_orphan_sidecars.py v%s starting; log file: %s",
             SCRIPT_VERSION, log_path)
    return log_path


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Sweep up orphan sidecar files (.nfo, .srt, -thumb.jpg) "
                    "left behind by earlier execute_tv runs that moved the "
                    "video but not its companions. Default mode is DRY-RUN.")
    ap.add_argument("--config", required=True,
                    help="Path to execute_tv config.json")
    ap.add_argument("--apply", action="store_true",
                    help="Actually relocate sidecars (default: dry-run)")
    ap.add_argument("--limit", type=int, default=0,
                    help="Process at most N series (smoke testing)")
    ap.add_argument("--series", default="",
                    help="Comma-separated series_ids to sweep; "
                         "defaults to every executed AUTO row")
    return ap.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    cfg_path = Path(args.config)
    if not cfg_path.is_file():
        raise SystemExit(f"Config not found: {cfg_path}")
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    mediaprep_dir = Path(cfg.get("mediaprep_dir", "."))
    plan_path = mediaprep_dir / "series_plan.csv"

    run_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    setup_logging(mediaprep_dir, run_id)

    dry_run = not args.apply
    log.info("Mode: %s",
             "DRY-RUN (use --apply for real moves)" if dry_run else "APPLY")

    journal_files = find_journal_files(mediaprep_dir)
    log.info("Scanning %d historical move journal(s):", len(journal_files))
    for jf in journal_files:
        log.info("  %s", jf.name)
    src_to_tgt = build_source_to_target_map(journal_files)
    log.info("Indexed %d historical episode moves", len(src_to_tgt))

    plan_rows = read_plan_rows(plan_path)
    eligible = [r for r in plan_rows
                if (r.get("action") or "").strip().upper() == "AUTO"
                and (r.get("executed_at") or "").strip()]
    if args.series:
        wanted = {int(x.strip()) for x in args.series.split(",") if x.strip()}
        eligible = [r for r in eligible
                    if int((r.get("series_id") or "0") or 0) in wanted]
    if args.limit:
        eligible = eligible[:args.limit]
    log.info("Sweeping %d executed series", len(eligible))

    sj_path = mediaprep_dir / f"sweep_orphan_{run_id}.jsonl"
    sj = SweepJournal(sj_path, run_id, dry_run)
    sj.write(op="sweep_start",
             plan_rows_eligible=len(eligible),
             move_index_size=len(src_to_tgt),
             result="ok")

    totals = SweepStats()
    per_series: List[SweepStats] = []
    for i, r in enumerate(eligible, start=1):
        try:
            sid = int((r.get("series_id") or "0") or 0)
        except ValueError:
            continue
        src = Path(r.get("source_folder") or "")
        if not src or not safe_exists(src):
            continue
        stats = sweep_source_folder(src, sid, src_to_tgt, sj, dry_run)
        per_series.append(stats)
        totals.swept += stats.swept
        totals.no_match += stats.no_match
        totals.skipped_video_present += stats.skipped_video_present
        totals.errors += stats.errors
        if i % 50 == 0:
            log.info("Progress: %d/%d series (swept=%d no_match=%d errors=%d)",
                     i, len(eligible), totals.swept, totals.no_match,
                     totals.errors)

    sj.write(op="sweep_end",
             swept=totals.swept,
             no_match=totals.no_match,
             skipped_video_present=totals.skipped_video_present,
             errors=totals.errors,
             result="ok")
    sj.close()

    log.info("=" * 60)
    log.info("Sweep summary%s:",
             " (DRY-RUN)" if dry_run else "")
    log.info("  sidecars relocated:        %d", totals.swept)
    log.info("  no journal-match found:    %d", totals.no_match)
    log.info("  skipped (video still in source): %d",
             totals.skipped_video_present)
    log.info("  errors:                    %d", totals.errors)
    log.info("Sweep journal: %s", sj_path)

    # Show top 10 series by swept count.
    top = sorted(per_series, key=lambda s: -s.swept)[:10]
    if top and top[0].swept > 0:
        for s in top:
            if s.swept == 0:
                break
            log.info("  series %-5d  swept=%d  no_match=%d",
                     s.series_id, s.swept, s.no_match)

    return 0 if totals.errors == 0 else 3


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        log.error("Unhandled exception:\n%s", traceback.format_exc())
        sys.exit(2)
