#!/usr/bin/env python3
"""
rollback.py — Undo phase 3 operations using the moves.jsonl journal.

The journal that execute.py writes is the source of truth. This script reads
it (newest first), reverses each operation, and writes a rollback.jsonl so
you can trivially re-do an undo if you change your mind.

Usage:
    py rollback.py --config config.json --run-id 2026-04-26T19-55-00
    py rollback.py --config config.json --title 42
    py rollback.py --config config.json --since 2026-04-26T18:00:00
    py rollback.py --config config.json --dry-run --run-id <id>
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


VERSION = "1.0.0"
log = logging.getLogger("rollback")
LONG_PATH_PREFIX = "\\\\?\\"


# ---------------------------------------------------------------------------
# Shared utilities (subset of execute.py — kept self-contained on purpose
# so rollback works even if execute.py is mid-edit)
# ---------------------------------------------------------------------------

def _norm_path(p: str) -> Path:
    return Path(p.replace("/", "\\"))


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


def setup_logging(mediaprep: Path, run_id: str) -> Path:
    mediaprep.mkdir(parents=True, exist_ok=True)
    log_path = mediaprep / f"rollback_{run_id}.log"
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    log.handlers.clear()
    log.setLevel(logging.DEBUG)
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(logging.Formatter(fmt))
    log.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter(fmt))
    log.addHandler(sh)
    log.info("rollback.py v%s starting; log: %s", VERSION, log_path)
    return log_path


# ---------------------------------------------------------------------------
# Journal reading
# ---------------------------------------------------------------------------

def read_journal(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"Journal not found: {path}")
    out: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                log.warning("journal line %d unparseable: %s", ln, e)
                continue
            out.append(rec)
    return out


def filter_records(records: List[Dict[str, Any]],
                   run_id: Optional[str],
                   title_id: Optional[int],
                   since_iso: Optional[str]) -> List[Dict[str, Any]]:
    """Filter records matching any of the criteria. Caller passes exactly one
    of run_id / title_id / since."""
    out = records
    if run_id:
        out = [r for r in out if r.get("run_id") == run_id]
    if title_id is not None:
        out = [r for r in out if r.get("title_id") == title_id]
    if since_iso:
        try:
            since_dt = dt.datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
        except ValueError:
            log.error("Invalid --since timestamp: %s", since_iso)
            return []
        keep = []
        for r in out:
            ts = r.get("ts", "")
            try:
                rec_dt = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                continue
            if rec_dt >= since_dt:
                keep.append(r)
        out = keep
    return out


# ---------------------------------------------------------------------------
# Reversers
# ---------------------------------------------------------------------------

@dataclass
class ReverseResult:
    success: bool
    op: str
    detail: str = ""


def _exists(path: Path) -> bool:
    try:
        return os.path.exists(long(path))
    except OSError:
        return False


def reverse_record(rec: Dict[str, Any], dry_run: bool) -> ReverseResult:
    """Reverse a single journal record. Skips records with result != 'ok'."""
    op = rec.get("op", "")
    result_kind = rec.get("result", "")
    if result_kind != "ok":
        # Skip dry-run, error, skipped — they didn't actually mutate anything,
        # except for "skipped" which still left state as-is (no inverse needed).
        return ReverseResult(True, op, f"skipped (result={result_kind})")

    if op == "rename":
        return _reverse_rename(rec, dry_run)
    if op == "copy":
        return _reverse_copy(rec, dry_run)
    if op == "delete":
        return _reverse_delete(rec, dry_run)
    if op == "mkdir":
        return _reverse_mkdir(rec, dry_run)
    if op == "rmdir":
        return _reverse_rmdir(rec, dry_run)
    if op == "write_file":
        return _reverse_write_file(rec, dry_run)
    if op == "backup":
        return _reverse_backup(rec, dry_run)
    if op == "verify":
        return ReverseResult(True, op, "verify is read-only; nothing to undo")
    if op in ("run_start", "run_end"):
        return ReverseResult(True, op, "marker; no-op")
    if op in ("dup_move", "move"):
        return _reverse_rename(rec, dry_run)  # same shape as rename
    return ReverseResult(False, op, f"unknown op: {op}")


def _reverse_rename(rec: Dict[str, Any], dry_run: bool) -> ReverseResult:
    src = _norm_path(rec.get("from_") or rec.get("from") or "")
    dst = _norm_path(rec.get("to") or "")
    if not src or not dst:
        return ReverseResult(False, "rename", "missing from/to")
    if not _exists(dst):
        if _exists(src):
            return ReverseResult(True, "rename",
                                 f"file already at original location: {src}")
        return ReverseResult(False, "rename",
                             f"neither original nor destination exists: {dst}")
    if _exists(src):
        return ReverseResult(False, "rename",
                             f"both original and destination exist; refusing")
    if dry_run:
        return ReverseResult(True, "rename", f"would rename {dst} -> {src}")
    try:
        os.makedirs(long(src.parent), exist_ok=True)
        os.rename(long(dst), long(src))
        return ReverseResult(True, "rename", f"{dst} -> {src}")
    except OSError as e:
        return ReverseResult(False, "rename", f"{type(e).__name__}: {e}")


def _reverse_copy(rec: Dict[str, Any], dry_run: bool) -> ReverseResult:
    """A 'copy' was followed by a 'delete'. To reverse, we need to put the
    file back at the source (if it's gone) and then optionally remove the
    target. Strategy: copy from dst back to src (overwriting if src exists),
    then delete dst. We do this only if the corresponding 'delete' record's
    reverse has already restored the source — handled implicitly because we
    process records newest-first."""
    src = _norm_path(rec.get("from_") or rec.get("from") or "")
    dst = _norm_path(rec.get("to") or "")
    if not src or not dst:
        return ReverseResult(False, "copy", "missing from/to")
    if not _exists(dst):
        return ReverseResult(False, "copy",
                             f"target {dst} missing; cannot reverse copy")
    if _exists(src):
        # Source already restored (probably by reverse_delete above).
        # Now we just delete the target to complete the reversal.
        if dry_run:
            return ReverseResult(True, "copy", f"would delete {dst}")
        try:
            os.remove(long(dst))
            return ReverseResult(True, "copy", f"deleted {dst}")
        except OSError as e:
            return ReverseResult(False, "copy", f"{type(e).__name__}: {e}")
    # Source missing; copy back from target, then delete target.
    if dry_run:
        return ReverseResult(True, "copy",
                             f"would copy {dst} -> {src}, then delete {dst}")
    try:
        os.makedirs(long(src.parent), exist_ok=True)
        shutil.copy2(long(dst), long(src))
        os.remove(long(dst))
        return ReverseResult(True, "copy", f"restored {src}; deleted {dst}")
    except OSError as e:
        return ReverseResult(False, "copy", f"{type(e).__name__}: {e}")


def _reverse_delete(rec: Dict[str, Any], dry_run: bool) -> ReverseResult:
    """A 'delete' record on its own is paired with a preceding 'copy' (cross-
    drive moves emit copy then delete). We treat reverse_delete as a no-op
    here because reverse_copy will see the missing source and handle the
    restoration. If a delete appears without a paired copy (shouldn't happen
    in our flow), we can't recover automatically."""
    src = _norm_path(rec.get("from_") or rec.get("from") or "")
    if not src:
        return ReverseResult(False, "delete", "missing from")
    return ReverseResult(True, "delete",
                         f"deferred to paired copy reverse for {src}")


def _reverse_mkdir(rec: Dict[str, Any], dry_run: bool) -> ReverseResult:
    p = _norm_path(rec.get("to") or "")
    if not p:
        return ReverseResult(False, "mkdir", "missing path")
    if not _exists(p):
        return ReverseResult(True, "mkdir", f"already gone: {p}")
    try:
        if any(os.scandir(long(p))):
            return ReverseResult(True, "mkdir",
                                 f"not empty; leaving in place: {p}")
    except OSError as e:
        return ReverseResult(False, "mkdir", f"{type(e).__name__}: {e}")
    if dry_run:
        return ReverseResult(True, "mkdir", f"would rmdir {p}")
    try:
        os.rmdir(long(p))
        return ReverseResult(True, "mkdir", f"rmdir {p}")
    except OSError as e:
        return ReverseResult(False, "mkdir", f"{type(e).__name__}: {e}")


def _reverse_rmdir(rec: Dict[str, Any], dry_run: bool) -> ReverseResult:
    p = _norm_path(rec.get("from_") or rec.get("from") or "")
    if not p:
        return ReverseResult(False, "rmdir", "missing path")
    if _exists(p):
        return ReverseResult(True, "rmdir", f"already exists: {p}")
    if dry_run:
        return ReverseResult(True, "rmdir", f"would mkdir {p}")
    try:
        os.makedirs(long(p), exist_ok=True)
        return ReverseResult(True, "rmdir", f"recreated {p}")
    except OSError as e:
        return ReverseResult(False, "rmdir", f"{type(e).__name__}: {e}")


def _reverse_write_file(rec: Dict[str, Any], dry_run: bool) -> ReverseResult:
    p = _norm_path(rec.get("to") or "")
    if not p:
        return ReverseResult(False, "write_file", "missing path")
    if not _exists(p):
        return ReverseResult(True, "write_file", f"already gone: {p}")
    if dry_run:
        return ReverseResult(True, "write_file", f"would delete {p}")
    try:
        os.remove(long(p))
        return ReverseResult(True, "write_file", f"deleted {p}")
    except OSError as e:
        return ReverseResult(False, "write_file", f"{type(e).__name__}: {e}")


def _reverse_backup(rec: Dict[str, Any], dry_run: bool) -> ReverseResult:
    """A 'backup' renamed foo.nfo to foo.nfo.bak. Reverse: rename .bak back."""
    src = _norm_path(rec.get("from_") or rec.get("from") or "")  # original (foo.nfo)
    dst = _norm_path(rec.get("to") or "")                         # .bak path
    if not src or not dst:
        return ReverseResult(False, "backup", "missing paths")
    if not _exists(dst):
        return ReverseResult(False, "backup", f".bak not found: {dst}")
    if _exists(src):
        # The new file exists at src; we'd be clobbering. Delete it first.
        if dry_run:
            return ReverseResult(True, "backup",
                                 f"would delete {src}, then rename {dst} -> {src}")
        try:
            os.remove(long(src))
        except OSError as e:
            return ReverseResult(False, "backup",
                                 f"could not remove new {src}: {e}")
    if dry_run:
        return ReverseResult(True, "backup", f"would rename {dst} -> {src}")
    try:
        os.rename(long(dst), long(src))
        return ReverseResult(True, "backup", f"{dst} -> {src}")
    except OSError as e:
        return ReverseResult(False, "backup", f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="rollback.py",
                                description="Undo phase 3 file operations.")
    p.add_argument("--config", default="config.json",
                   help="path to config.json")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--run-id", help="undo all ops with this run_id")
    g.add_argument("--title", type=int,
                   help="undo all ops for this title_id (most recent run)")
    g.add_argument("--since",
                   help="undo all ops since this UTC ISO timestamp")
    p.add_argument("--dry-run", action="store_true",
                   help="show what would be reversed without doing it")
    p.add_argument("--journal", default="moves.jsonl",
                   help="journal filename in mediaprep_dir (default: moves.jsonl)")
    return p.parse_args(argv)


def load_minimal_config(path: Path) -> Dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        "mediaprep_dir": _norm_path(data["mediaprep_dir"]),
    }


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        cfg = load_minimal_config(_norm_path(args.config))
    except (FileNotFoundError, KeyError, json.JSONDecodeError) as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 3

    run_tag = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    setup_logging(cfg["mediaprep_dir"], run_tag)

    journal_path = cfg["mediaprep_dir"] / args.journal
    log.info("Reading journal: %s", journal_path)
    try:
        records = read_journal(journal_path)
    except FileNotFoundError as e:
        log.error(str(e))
        return 3
    log.info("Loaded %d journal records", len(records))

    # Determine the most recent run_id when --title is given
    target_run_id = args.run_id
    if args.title is not None and not target_run_id:
        for r in reversed(records):
            if r.get("title_id") == args.title:
                target_run_id = r.get("run_id")
                break
        if not target_run_id:
            log.error("No journal records found for title_id %d", args.title)
            return 3
        log.info("Using most recent run_id for title %d: %s",
                 args.title, target_run_id)

    filtered = filter_records(records, target_run_id, args.title, args.since)
    if not filtered:
        log.error("No matching records to reverse")
        return 3
    log.info("Will attempt to reverse %d records", len(filtered))

    # Reverse newest-first
    filtered.sort(key=lambda r: r.get("ts", ""), reverse=True)

    rollback_journal_path = cfg["mediaprep_dir"] / "rollback.jsonl"
    rj = open(rollback_journal_path, "a", encoding="utf-8")
    rj_run_id = run_tag

    ok = 0
    failed = 0
    skipped = 0
    for rec in filtered:
        op = rec.get("op", "")
        try:
            res = reverse_record(rec, args.dry_run)
        except Exception as e:
            log.exception("unhandled error reversing %s", op)
            res = ReverseResult(False, op, f"{type(e).__name__}: {e}")
        out = {
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"),
            "rollback_run_id": rj_run_id,
            "original_run_id": rec.get("run_id"),
            "title_id": rec.get("title_id"),
            "reversed_op": op,
            "result": "ok" if res.success else "error",
            "detail": res.detail,
            "dry_run": args.dry_run,
        }
        rj.write(json.dumps(out, ensure_ascii=False) + "\n")
        rj.flush()
        if res.success:
            if "skipped" in res.detail or "already" in res.detail or "no-op" in res.detail:
                skipped += 1
            else:
                ok += 1
            log.info("[OK] %s: %s", op, res.detail)
        else:
            failed += 1
            log.error("[FAIL] %s: %s", op, res.detail)

    rj.close()
    log.info("Rollback summary: ok=%d skipped=%d failed=%d %s",
             ok, skipped, failed,
             "(dry-run; no files touched)" if args.dry_run else "")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
