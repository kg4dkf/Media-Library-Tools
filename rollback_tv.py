#!/usr/bin/env python3
"""
rollback_tv.py - Reverse successful moves from a moves_tv.jsonl run.

Reads the journal, filters to successful "rename" / "copy" ops for the
specified series_ids, and moves each file back from `to` to `from_`.

Default mode is DRY-RUN; pass --apply to actually move files. Every
operation is journaled to a separate `rollback_<run_id>.jsonl` so this
script's actions are themselves reversible.

Designed for surgical rollbacks: the user identifies a small set of
problem series (e.g. parser bugs, weird data) and rolls back ONLY those.
Other series and their moves are left alone.

Collision policy: if the source path is already occupied (e.g. some other
file with the same name has appeared since the move), the rolled-back
file is renamed with a `.recovered.<ext>` suffix and dropped next to it.

Usage:
    py -3.13 rollback_tv.py --journal H:\\_mediaprep\\moves_tv.jsonl \
        --series 694,742,645,664                                       # dry-run
    py -3.13 rollback_tv.py --journal H:\\_mediaprep\\moves_tv.jsonl \
        --series 694,742,645,664 --apply                               # real

Outputs:
    H:\\_mediaprep\\rollback_<timestamp>.jsonl   (this script's own journal)
    Console: per-series counts of moves restored / collisions / errors
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import shutil
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set


SCRIPT_VERSION = "1.0.0"
LONG_PATH_PREFIX = "\\\\?\\"

log = logging.getLogger("rollback_tv")


# =========================================================================
# Path helpers (same conventions as execute_tv.py)
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
    """Robust existence check that survives long Windows paths and any
    transient OSError. Uses the `long()` prefix to bypass Win32's 260-char
    cap when applicable."""
    try:
        return os.path.exists(long(p))
    except OSError:
        return False


def safe_size(p: Path) -> int:
    try:
        return os.path.getsize(long(p))
    except OSError:
        return 0


# =========================================================================
# Journal reader
# =========================================================================

def read_journal(path: Path) -> List[Dict[str, Any]]:
    """Read a moves_tv.jsonl. Tolerant of malformed lines."""
    out: List[Dict[str, Any]] = []
    if not path.is_file():
        raise SystemExit(f"Journal not found: {path}")
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for ln, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                out.append(json.loads(raw))
            except json.JSONDecodeError as e:
                log.warning("skipping bad line %d: %s", ln, e)
    return out


# =========================================================================
# Rollback journal writer
# =========================================================================

class RollbackJournal:
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
# Restore one move
# =========================================================================

@dataclass
class RestoreStats:
    series_id: int = 0
    restored: int = 0
    skipped_missing: int = 0   # the moved file is no longer at the target
    skipped_already_back: int = 0  # source already has the right file
    collided: int = 0          # source occupied by something else; saved as .recovered
    errors: int = 0


def _resolve_collision_path(src_path: Path) -> Path:
    """If src_path is occupied, return src_path with .recovered before ext.

    e.g. `Foo.avi` -> `Foo.recovered.avi`
    If `.recovered` is also taken, append `.recovered2.avi`, etc.
    """
    if src_path.suffix:
        stem, ext = src_path.stem, src_path.suffix
    else:
        stem, ext = src_path.name, ""
    candidate = src_path.with_name(f"{stem}.recovered{ext}")
    n = 2
    while candidate.exists():
        candidate = src_path.with_name(f"{stem}.recovered{n}{ext}")
        n += 1
        if n > 50:
            raise RuntimeError(f"too many .recovered conflicts at {src_path}")
    return candidate


def restore_one(rec: Dict[str, Any], journal: RollbackJournal,
                dry_run: bool) -> str:
    """Reverse one journal record. Returns a status string:
       restored | already_back | missing | collided | error
    """
    src_dst = rec.get("to", "")     # where the file IS now
    orig_src = rec.get("from_", "")  # where it CAME FROM
    series_id = rec.get("series_id")
    episode_key = rec.get("episode_key", "")

    if not src_dst or not orig_src:
        log.warning("skipping malformed record: %s", rec)
        return "error"

    src_dst_p = Path(src_dst)     # current location (move target)
    orig_src_p = Path(orig_src)   # desired restore location (move source)

    src_dst_exists = safe_exists(src_dst_p)
    orig_src_exists = safe_exists(orig_src_p)

    # Already back?
    if not src_dst_exists and orig_src_exists:
        sz_orig = safe_size(orig_src_p)
        sz_journal = int(rec.get("size") or 0)
        if sz_journal and sz_orig == sz_journal:
            journal.write(series_id=series_id, op="restore",
                          episode_key=episode_key,
                          from_=src_dst, to=orig_src,
                          result="skipped",
                          reason="already_at_source")
            return "already_back"

    # Source file (i.e. the moved-into-target file) missing?
    if not src_dst_exists:
        journal.write(series_id=series_id, op="restore",
                      episode_key=episode_key,
                      from_=src_dst, to=orig_src,
                      result="skipped",
                      reason="target_file_missing")
        return "missing"

    # Compute final destination — handle collision.
    final_dst = orig_src_p
    if orig_src_exists:
        sz_journal = int(rec.get("size") or 0)
        sz_existing = safe_size(orig_src_p)
        sz_target = safe_size(src_dst_p)
        if sz_journal and sz_existing == sz_target and sz_existing > 0:
            # Same content already at source — just delete the moved copy.
            if dry_run:
                journal.write(series_id=series_id, op="delete_redundant",
                              episode_key=episode_key, from_=src_dst,
                              result="dry-run")
            else:
                try:
                    os.remove(long(src_dst_p))
                    journal.write(series_id=series_id, op="delete_redundant",
                                  episode_key=episode_key, from_=src_dst,
                                  result="ok",
                                  reason="source_already_has_identical_copy")
                except OSError as e:
                    journal.write(series_id=series_id, op="delete_redundant",
                                  episode_key=episode_key, from_=src_dst,
                                  result="error", error=str(e))
                    return "error"
            return "already_back"
        # Real collision — pick a .recovered name.
        final_dst = _resolve_collision_path(orig_src_p)

    # Make sure parent exists.
    if not safe_exists(final_dst.parent):
        if dry_run:
            journal.write(series_id=series_id, op="mkdir",
                          to=str(final_dst.parent), result="dry-run")
        else:
            try:
                os.makedirs(long(final_dst.parent), exist_ok=True)
                journal.write(series_id=series_id, op="mkdir",
                              to=str(final_dst.parent), result="ok")
            except OSError as e:
                journal.write(series_id=series_id, op="mkdir",
                              to=str(final_dst.parent), result="error",
                              error=str(e))
                return "error"

    op_label = "restore_collided" if final_dst != orig_src_p else "restore"
    if dry_run:
        journal.write(series_id=series_id, op=op_label,
                      episode_key=episode_key,
                      from_=src_dst, to=str(final_dst),
                      original_dst=orig_src,
                      result="dry-run",
                      collided=(final_dst != orig_src_p))
        return "collided" if final_dst != orig_src_p else "restored"

    try:
        if same_drive(src_dst_p, final_dst):
            os.rename(long(src_dst_p), long(final_dst))
        else:
            shutil.copy2(long(src_dst_p), long(final_dst))
            os.remove(long(src_dst_p))
        journal.write(series_id=series_id, op=op_label,
                      episode_key=episode_key,
                      from_=src_dst, to=str(final_dst),
                      original_dst=orig_src,
                      result="ok",
                      collided=(final_dst != orig_src_p))
        return "collided" if final_dst != orig_src_p else "restored"
    except OSError as e:
        journal.write(series_id=series_id, op=op_label,
                      episode_key=episode_key,
                      from_=src_dst, to=str(final_dst),
                      result="error", error=str(e),
                      error_class=type(e).__name__)
        return "error"


# =========================================================================
# Main
# =========================================================================

def setup_logging(out_dir: Path, run_id: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / f"rollback_{run_id}.log"
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    sh.setLevel(logging.INFO)
    log.setLevel(logging.DEBUG)
    if not log.handlers:
        log.addHandler(fh)
        log.addHandler(sh)
    log.info("rollback_tv.py v%s starting; log file: %s", SCRIPT_VERSION, log_path)
    return log_path


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Reverse successful moves from a moves_tv.jsonl run "
                    "for selected series_ids. Default mode is DRY-RUN.")
    ap.add_argument("--journal", required=True,
                    help="Path to moves_tv.jsonl (the apply run to reverse)")
    ap.add_argument("--series", required=True,
                    help="Comma-separated series_ids to roll back "
                         "(e.g. 694,742,645,664)")
    ap.add_argument("--out-dir", default=None,
                    help="Where to write rollback journal/log "
                         "(default: same dir as --journal)")
    ap.add_argument("--apply", action="store_true",
                    help="Actually move files (default: dry-run)")
    ap.add_argument("--run-id-filter", default=None,
                    help="Optional: only roll back records with this run_id "
                         "(useful if the journal contains multiple runs)")
    return ap.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    journal_path = Path(args.journal)
    out_dir = Path(args.out_dir) if args.out_dir else journal_path.parent
    run_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    setup_logging(out_dir, run_id)

    dry_run = not args.apply
    log.info("Mode: %s",
             "DRY-RUN (use --apply for real moves)" if dry_run else "APPLY")
    log.info("Journal: %s", journal_path)

    try:
        wanted = {int(x.strip()) for x in args.series.split(",") if x.strip()}
    except ValueError:
        log.error("--series must be comma-separated integers")
        return 1
    log.info("Target series_ids: %s", sorted(wanted))

    recs = read_journal(journal_path)
    log.info("Loaded %d journal records", len(recs))

    # Filter to successful rename/copy records for our target series.
    eligible = []
    for r in recs:
        if r.get("series_id") not in wanted:
            continue
        if args.run_id_filter and r.get("run_id") != args.run_id_filter:
            continue
        if r.get("op") not in ("rename", "copy"):
            continue
        if r.get("result") != "ok":
            continue
        if not r.get("from_") or not r.get("to"):
            continue
        eligible.append(r)
    log.info("Eligible move records to reverse: %d", len(eligible))
    if not eligible:
        log.info("Nothing to roll back.")
        return 0

    rj_path = out_dir / f"rollback_{run_id}.jsonl"
    rj = RollbackJournal(rj_path, run_id, dry_run)
    log.info("Rollback journal: %s", rj_path)
    rj.write(op="rollback_start", target_series=sorted(wanted),
             eligible_records=len(eligible),
             source_journal=str(journal_path), result="ok")

    # Reverse in REVERSE chronological order (latest moves first) — that
    # gives the cleanest behavior when one series did a multi-step move
    # (mkdir then several renames; we undo renames first, then nothing
    # else needs cleanup).
    eligible.sort(key=lambda r: r.get("ts", ""), reverse=True)

    stats: Dict[int, RestoreStats] = {sid: RestoreStats(series_id=sid)
                                       for sid in wanted}

    for i, r in enumerate(eligible, start=1):
        sid = r["series_id"]
        try:
            outcome = restore_one(r, rj, dry_run)
        except Exception as e:
            log.error("[series %d] unexpected error: %s\n%s", sid, e,
                      traceback.format_exc())
            stats[sid].errors += 1
            continue
        if outcome == "restored":
            stats[sid].restored += 1
        elif outcome == "collided":
            stats[sid].collided += 1
        elif outcome == "missing":
            stats[sid].skipped_missing += 1
        elif outcome == "already_back":
            stats[sid].skipped_already_back += 1
        else:
            stats[sid].errors += 1

        if i % 200 == 0:
            log.info("Progress: %d/%d records processed", i, len(eligible))

    rj.write(op="rollback_end", per_series={
        sid: {"restored": s.restored, "collided": s.collided,
              "missing": s.skipped_missing,
              "already_back": s.skipped_already_back,
              "errors": s.errors}
        for sid, s in stats.items()},
        result="ok")
    rj.close()

    log.info("=" * 60)
    log.info("Per-series rollback summary%s:",
             " (DRY-RUN)" if dry_run else "")
    log.info("  %-10s %10s %10s %10s %10s %8s",
             "series_id", "restored", "collided", "missing",
             "already_back", "errors")
    for sid in sorted(wanted):
        s = stats[sid]
        log.info("  %-10d %10d %10d %10d %10d %8d",
                 sid, s.restored, s.collided, s.skipped_missing,
                 s.skipped_already_back, s.errors)
    total_err = sum(s.errors for s in stats.values())
    return 0 if total_err == 0 else 3


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        log.error("Unhandled exception:\n%s", traceback.format_exc())
        sys.exit(2)
