#!/usr/bin/env python3
"""
execute_tv.py - Phase 6 executor for the TV pipeline (round 6a).

Reads `series_plan.csv` (produced by inventory_tv.py and reviewed by the
user), processes every row marked AUTO, and atomically renames/moves each
series's episode files into the canonical layout from phase6_design.md §9:

    G:\\TV\\<Series Name> (<Year>) {tmdb-#####}\\
        Season ## (<Year>)\\
            <Series Name> S##E## - <Episode Title>.<ext>

This script is the write-side of the pipeline. Default mode is **dry-run** —
pass `--apply` to actually touch disk. Same paranoid posture as the movie
pipeline's execute.py: every operation is journaled to `moves_tv.jsonl`
before it happens, idempotent on re-run, and rollback-able via the
project's existing rollback.py.

This is round 6c — adds native merge support. Implements:
    Pass 1: pre-flight (basic checks; aborts if anything looks wrong).
            duplicate_target only fires for *real* conflicts (different
            tmdb_series_ids resolving to the same target). Same-tmdb
            collisions are processed as merges instead.
    Pass 4: per-series-group flow. Single-row groups: Stages A, C-F per
            design §5 (classify, fetch TMDB, compute targets, mkdir, move).
            Multi-row groups (merges): all sources classified, episodes
            deduped by SxxExx (hash-equal -> silent drop; content-different
            -> keep largest, quarantine rest to mediaprep_dir/duplicates/).
    Pass 5: cleanup (rmdir empty source dirs).

Deferred to round 6b: NFO writing (Stage G), sidecars/extras (Stage H).
Deferred to round 6d: mixed_content RELOCATE pass (§7).

Usage:
    py -3.13 execute_tv.py --config config.json                # dry-run
    py -3.13 execute_tv.py --config config.json --apply        # real run
    py -3.13 execute_tv.py --config config.json --apply --limit 5
    py -3.13 execute_tv.py --config config.json --apply --series 42,43,44
    py -3.13 execute_tv.py --config config.json --skip-preflight

See phase6_design.md §3-§5 for the full specification.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import os
import re
import shutil
import sys
import time
import traceback
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

# Local imports — all already production-ready.
from tv_parser import (
    VIDEO_EXTS,
    parse_episode,
)
from tv_tmdb_client import (
    TmdbTvClient,
    episode_from_season,
    is_error,
    is_not_found,
)
from tv_episode_matcher import (
    EpisodeMatch,
    episodes_map_from_tmdb_seasons,
    match_filename_to_episode,
    title_similarity,
)


SCRIPT_VERSION = "1.4.1"

# Long-path prefix (Windows). Same as execute.py.
LONG_PATH_PREFIX = "\\\\?\\"

# Filesystem-component sanitization (TV folder/file names).
# Mirrors execute.py's WINDOWS_RESERVED + _WINDOWS_CHAR_MAP.
WINDOWS_RESERVED: Set[str] = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_WINDOWS_CHAR_MAP = {
    ":": " - ",
    "/": " ",
    "\\": " ",
    '"': "'",
    "|": "-",
    "?": "",
    "*": "",
    "<": "(",
    ">": ")",
}

log = logging.getLogger("execute_tv")


# =========================================================================
# Path utilities (mirror execute.py exactly so movie pipeline patterns hold)
# =========================================================================

def _norm_path(p) -> Path:
    """Normalize to Path, replacing forward slashes with backslashes."""
    s = str(p) if not isinstance(p, Path) else str(p)
    s = s.replace("/", "\\")
    return Path(s)


def long(p: Path) -> str:
    r"""Apply the Windows \\?\ long-path prefix when on Windows.

    Cross-drive moves and deeply nested season folders can exceed 260 chars
    on stock Win32 APIs — the prefix bypasses the limit.
    """
    if os.name != "nt":
        return str(p)
    s = str(p)
    if s.startswith(LONG_PATH_PREFIX):
        return s
    if s.startswith("\\\\"):  # UNC
        return LONG_PATH_PREFIX + "UNC\\" + s.lstrip("\\")
    if len(s) >= 2 and s[1] == ":":
        return LONG_PATH_PREFIX + s
    return s


def same_drive(a: Path, b: Path) -> bool:
    """True when two paths share a drive letter / UNC root."""
    try:
        da = os.path.splitdrive(str(a))[0].lower()
        db = os.path.splitdrive(str(b))[0].lower()
        return da == db and da != ""
    except Exception:
        return False


def sanitize_for_windows(name: str) -> str:
    """Replace forbidden characters, collapse whitespace, handle reserved names."""
    if not name:
        return name
    out = name
    for ch, repl in _WINDOWS_CHAR_MAP.items():
        out = out.replace(ch, repl)
    out = re.sub(r"\s+", " ", out).strip()
    out = out.rstrip(". ")
    base, _, ext = out.rpartition(".") if "." in out else (out, "", "")
    test = base if base else out
    if test.upper() in WINDOWS_RESERVED:
        out = (test + "_") + (("." + ext) if ext else "")
    return out


# =========================================================================
# Config
# =========================================================================

@dataclass
class Config:
    """Loaded from config.json. Phase 6 keys per design §14."""
    tmdb_api_key: str
    tv_root: Path
    mediaprep_dir: Path
    language: str = "en-US"
    tv_series_template: str = "{title} ({year}) {{tmdb-{tmdb_id}}}"
    tv_season_template: str = "Season {season:02d} ({season_year})"
    tv_specials_folder: str = "Specials"
    tv_episode_template: str = "{title} S{season:02d}E{episode:02d} - {episode_title}"
    tv_multi_ep_separator: str = "+ "
    tv_skip_dirs: Tuple[str, ...] = (
        ".deletedByTMM", ".sonarr", ".trash", ".tmm", "_mediaprep",
    )
    long_path_prefix: bool = True
    rmdir_empty_sources: bool = True
    verify_hashes_default: bool = False
    hash_algo: str = "blake3"
    tmdb_tv_language: Optional[str] = None
    intake_dir: Optional[Path] = None

    @classmethod
    def load(cls, config_path: Path) -> "Config":
        if not config_path.is_file():
            raise SystemExit(f"Config not found: {config_path}")
        try:
            text = config_path.read_text(encoding="utf-8")
            raw = json.loads(text)
        except json.JSONDecodeError as e:
            lines = text.splitlines()
            lo = max(0, e.lineno - 2)
            hi = min(len(lines), e.lineno + 1)
            window = "\n".join(
                f"  {i+1:4d} {'>' if i+1 == e.lineno else ' '} {lines[i]}"
                for i in range(lo, hi)
            )
            raise SystemExit(
                f"\nJSON parse error in {config_path}:\n"
                f"  line {e.lineno} col {e.colno}: {e.msg}\n\n{window}\n"
            ) from e

        try:
            tmdb_api_key = raw["tmdb_api_key"]
            tv_root = Path(raw["tv_root"])
            mediaprep_dir = Path(raw["mediaprep_dir"])
        except KeyError as e:
            raise SystemExit(f"Missing required config key for execute_tv: {e}")
        if tmdb_api_key in ("", "REPLACE_ME", "YOUR_KEY_HERE"):
            raise SystemExit("tmdb_api_key is not set in config.json")

        skip_dirs = tuple(raw.get("tv_skip_dirs", cls.__dataclass_fields__["tv_skip_dirs"].default))

        return cls(
            tmdb_api_key=tmdb_api_key,
            tv_root=tv_root,
            mediaprep_dir=mediaprep_dir,
            language=str(raw.get("language", "en-US")),
            tv_series_template=str(raw.get("tv_series_template",
                                           cls.__dataclass_fields__["tv_series_template"].default)),
            tv_season_template=str(raw.get("tv_season_template",
                                           cls.__dataclass_fields__["tv_season_template"].default)),
            tv_specials_folder=str(raw.get("tv_specials_folder", "Specials")),
            tv_episode_template=str(raw.get("tv_episode_template",
                                            cls.__dataclass_fields__["tv_episode_template"].default)),
            tv_multi_ep_separator=str(raw.get("tv_multi_ep_separator", "+ ")),
            tv_skip_dirs=skip_dirs,
            long_path_prefix=bool(raw.get("long_path_prefix", True)),
            rmdir_empty_sources=bool(raw.get("rmdir_empty_sources", True)),
            verify_hashes_default=bool(raw.get("verify_hashes_default", False)),
            hash_algo=str(raw.get("hash_algo", "blake3")).lower(),
            tmdb_tv_language=raw.get("tmdb_tv_language"),
            intake_dir=(Path(raw["intake_dir"])
                        if raw.get("intake_dir") else None),
        )


# =========================================================================
# Logging
# =========================================================================

def setup_logging(mediaprep_dir: Path, run_id: str) -> Path:
    mediaprep_dir.mkdir(parents=True, exist_ok=True)
    log_path = mediaprep_dir / f"execute_tv_{run_id}.log"
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    file_h = logging.FileHandler(log_path, encoding="utf-8")
    file_h.setFormatter(fmt)
    file_h.setLevel(logging.DEBUG)
    console_h = logging.StreamHandler(sys.stdout)
    console_h.setFormatter(fmt)
    console_h.setLevel(logging.INFO)
    log.setLevel(logging.DEBUG)
    if not log.handlers:
        log.addHandler(file_h)
        log.addHandler(console_h)
    log.info("execute_tv.py v%s starting; log file: %s", SCRIPT_VERSION, log_path)
    return log_path


# =========================================================================
# Plan-row reader
# =========================================================================

@dataclass
class SeriesPlanRow:
    """One row from series_plan.csv."""
    series_id: int = 0
    source_folder: str = ""
    episode_count: int = 0
    sample_episode: str = ""
    tmdb_series_id: Optional[int] = None
    imdb_series_id: str = ""
    matched_series: str = ""
    matched_year: Optional[int] = None
    target_folder: str = ""
    confidence: int = 0
    action: str = ""
    flags: str = ""
    notes: str = ""
    user_notes: str = ""
    executed_at: str = ""

    @property
    def is_auto(self) -> bool:
        """True iff this row should be processed by execute_tv."""
        return self.action.strip().upper() == "AUTO"

    @property
    def already_executed(self) -> bool:
        return bool(self.executed_at.strip())


def _read_csv_rows(path: Path) -> List[Dict[str, str]]:
    """Tolerant CSV reader (handles BOM, NUL pollution, missing file)."""
    if not path.is_file():
        return []
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = raw.decode("utf-16", errors="replace")
    else:
        text = raw.replace(b"\x00", b"").decode("utf-8-sig", errors="replace")
    try:
        return list(csv.DictReader(text.splitlines()))
    except csv.Error as e:
        log.error("Could not parse %s: %s", path, e)
        return []


def read_series_plan(path: Path) -> List[SeriesPlanRow]:
    """Read series_plan.csv. Returns rows in order, including non-AUTO rows
    (caller filters). Each row's int/Optional[int] fields are coerced."""
    rows: List[SeriesPlanRow] = []
    for raw in _read_csv_rows(path):
        def _int(k: str) -> int:
            try:
                return int((raw.get(k) or "").strip() or 0)
            except ValueError:
                return 0

        def _opt_int(k: str) -> Optional[int]:
            v = (raw.get(k) or "").strip()
            try:
                return int(v) if v else None
            except ValueError:
                return None

        rows.append(SeriesPlanRow(
            series_id=_int("series_id"),
            source_folder=(raw.get("source_folder") or "").strip(),
            episode_count=_int("episode_count"),
            sample_episode=(raw.get("sample_episode") or "").strip(),
            tmdb_series_id=_opt_int("tmdb_series_id"),
            imdb_series_id=(raw.get("imdb_series_id") or "").strip(),
            matched_series=(raw.get("matched_series") or "").strip(),
            matched_year=_opt_int("matched_year"),
            target_folder=(raw.get("target_folder") or "").strip(),
            confidence=_int("confidence"),
            action=(raw.get("action") or "").strip(),
            flags=(raw.get("flags") or "").strip(),
            notes=(raw.get("notes") or "").strip(),
            user_notes=(raw.get("user_notes") or "").strip(),
            executed_at=(raw.get("executed_at") or "").strip(),
        ))
    return rows


def stamp_executed_at(plan_path: Path, series_id: int, timestamp: str,
                      *, max_attempts: int = 5,
                      retry_delay_s: float = 0.5) -> bool:
    """Atomic update of executed_at for a single series_id in series_plan.csv.

    Rewrites the file with the same column order — preservation logic in
    inventory_tv.py will then carry the timestamp across future re-runs.

    Returns True on success, False on failure. NEVER raises — the journal
    is the source of truth for what happened on disk; series_plan.csv is
    a convenience for resuming. If the file is held open (Excel, AV
    scanner) we retry a few times then degrade to a logged warning so the
    overall run continues. The next --apply pass is idempotent because
    DiskOps.move() recognizes "dst already exists, same size" as a no-op.
    """
    try:
        rows = _read_csv_rows(plan_path)
    except OSError as e:
        log.warning("stamp_executed_at: cannot read %s: %s", plan_path, e)
        return False
    if not rows:
        return False
    fieldnames = list(rows[0].keys())
    updated = False
    for r in rows:
        if str(r.get("series_id", "")).strip() == str(series_id):
            r["executed_at"] = timestamp
            updated = True
            break
    if not updated:
        log.warning("series_id %s not found in %s; can't stamp executed_at",
                    series_id, plan_path)
        return False
    tmp = plan_path.with_suffix(plan_path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8-sig", newline="",
                  errors="replace") as fh:
            w = csv.DictWriter(fh, fieldnames=fieldnames)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in fieldnames})
    except OSError as e:
        log.warning("stamp_executed_at: failed to write tmp file %s: %s",
                    tmp, e)
        return False

    # Retry os.replace — PermissionError happens transiently on Windows
    # when an AV scanner inspects newly-written files, or persistently
    # when the user has series_plan.csv open in Excel.
    last_err: Optional[OSError] = None
    for attempt in range(max_attempts):
        try:
            os.replace(tmp, plan_path)
            return True
        except PermissionError as e:
            last_err = e
            if attempt < max_attempts - 1:
                time.sleep(retry_delay_s * (attempt + 1))
        except OSError as e:
            last_err = e
            break

    log.warning(
        "stamp_executed_at: cannot replace %s (%s). The episode moves "
        "for series_id=%d ALREADY SUCCEEDED — the journal records them. "
        "executed_at will not be stamped this run; rerun --apply --series %d "
        "after closing the file (or any AV holder) and the executor will "
        "no-op the moves and stamp the timestamp. Tmp file left at: %s",
        plan_path, last_err, series_id, series_id, tmp,
    )
    return False


# =========================================================================
# Journal — append-only JSONL of every disk operation
# =========================================================================

class Journal:
    """Append-only JSONL writer. One file per run-mode (real vs. dry-run).

    Mirrors execute.py's Journal exactly, but writes to moves_tv.jsonl
    (or moves_tv_dryrun.jsonl) per phase6_design.md §10.
    """

    def __init__(self, path: Path, run_id: str, dry_run: bool) -> None:
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
        except Exception:  # pragma: no cover
            pass


# =========================================================================
# DiskOps — all journaled, idempotent, dry-run-aware
# =========================================================================

class DiskOps:
    """All disk-touching operations route through this class.

    Adapted from execute.py's DiskOps. Differences vs. movie pipeline:
        * series_id replaces title_id in journal records
        * default stage names reflect TV pipeline phases ("classify",
          "fetch_tmdb", "mkdir_series", "mkdir_season", "move_episode")
    """

    def __init__(self, journal: Journal, dry_run: bool,
                 verify_hashes: bool = False, hash_algo: str = "blake3") -> None:
        self.journal = journal
        self.dry_run = dry_run
        self.verify_hashes = verify_hashes
        self.hash_algo = hash_algo

    # ------------------------------------------------------------------
    def mkdir(self, path: Path, *, series_id: Optional[int] = None,
              stage: str = "") -> bool:
        if path.exists():
            return True
        if self.dry_run:
            self.journal.write(series_id=series_id, stage=stage, op="mkdir",
                               to=str(path), result="dry-run")
            return True
        try:
            os.makedirs(long(path), exist_ok=True)
            self.journal.write(series_id=series_id, stage=stage, op="mkdir",
                               to=str(path), result="ok")
            return True
        except OSError as e:
            self.journal.write(series_id=series_id, stage=stage, op="mkdir",
                               to=str(path), result="error",
                               error=str(e), error_class=type(e).__name__)
            return False

    # ------------------------------------------------------------------
    def rmdir(self, path: Path, *, series_id: Optional[int] = None,
              stage: str = "") -> bool:
        """Remove ONLY if empty. Never deletes non-empty directories."""
        if not path.exists():
            return True
        try:
            entries = list(os.listdir(long(path)))
        except OSError as e:
            self.journal.write(series_id=series_id, stage=stage, op="rmdir",
                               to=str(path), result="error",
                               error=str(e), error_class=type(e).__name__)
            return False
        if entries:
            self.journal.write(series_id=series_id, stage=stage, op="rmdir",
                               to=str(path), result="skipped",
                               reason=f"not empty ({len(entries)} entries)")
            return False
        if self.dry_run:
            self.journal.write(series_id=series_id, stage=stage, op="rmdir",
                               to=str(path), result="dry-run")
            return True
        try:
            os.rmdir(long(path))
            self.journal.write(series_id=series_id, stage=stage, op="rmdir",
                               to=str(path), result="ok")
            return True
        except OSError as e:
            self.journal.write(series_id=series_id, stage=stage, op="rmdir",
                               to=str(path), result="error",
                               error=str(e), error_class=type(e).__name__)
            return False

    # ------------------------------------------------------------------
    def move(self, src: Path, dst: Path, *, series_id: Optional[int] = None,
             stage: str = "", episode_key: str = "",
             op_label: str = "move_episode") -> bool:
        """Move src -> dst. Same-drive: atomic os.rename. Cross-drive:
        copy+(verify)+delete via shutil. Idempotent: a second run where dst
        already matches is a no-op. Records every step in the journal."""
        if not src.exists():
            self.journal.write(series_id=series_id, stage=stage, op=op_label,
                               episode_key=episode_key,
                               from_=str(src), to=str(dst), result="error",
                               error="source missing",
                               error_class="FileNotFoundError")
            return False

        if dst.exists():
            try:
                ssz = os.path.getsize(long(src))
                dsz = os.path.getsize(long(dst))
            except OSError:
                ssz = dsz = 0
            if ssz == dsz and ssz > 0:
                # Idempotency: dst is the right file. Same path? noop. Else
                # delete src to complete the previously-interrupted move.
                if str(src).lower() == str(dst).lower():
                    self.journal.write(series_id=series_id, stage=stage,
                                       op=op_label, episode_key=episode_key,
                                       from_=str(src), to=str(dst),
                                       result="skipped", reason="src == dst")
                    return True
                if self.dry_run:
                    self.journal.write(series_id=series_id, stage=stage,
                                       op=op_label, episode_key=episode_key,
                                       from_=str(src), to=str(dst),
                                       result="dry-run",
                                       reason="dst exists; would delete src")
                    return True
                try:
                    os.remove(long(src))
                    self.journal.write(series_id=series_id, stage=stage,
                                       op="delete", episode_key=episode_key,
                                       from_=str(src), result="ok",
                                       reason="dst already in place")
                    return True
                except OSError as e:
                    self.journal.write(series_id=series_id, stage=stage,
                                       op="delete", episode_key=episode_key,
                                       from_=str(src), result="error",
                                       error=str(e),
                                       error_class=type(e).__name__)
                    return False
            # dst exists with different size — refuse to clobber.
            self.journal.write(series_id=series_id, stage=stage, op=op_label,
                               episode_key=episode_key,
                               from_=str(src), to=str(dst), result="error",
                               error="destination exists with different size",
                               error_class="FileExistsError")
            return False

        if not dst.parent.exists():
            if not self.mkdir(dst.parent, series_id=series_id, stage=stage):
                return False

        try:
            size = os.path.getsize(long(src))
        except OSError:
            size = 0
        drive_same = same_drive(src, dst)

        if self.dry_run:
            self.journal.write(series_id=series_id, stage=stage,
                               op="rename" if drive_same else "copy",
                               episode_key=episode_key,
                               from_=str(src), to=str(dst), size=size,
                               drive_same=drive_same, result="dry-run")
            if not drive_same:
                self.journal.write(series_id=series_id, stage=stage,
                                   op="delete", episode_key=episode_key,
                                   from_=str(src), result="dry-run")
            return True

        try:
            if drive_same:
                os.rename(long(src), long(dst))
                self.journal.write(series_id=series_id, stage=stage,
                                   op="rename", episode_key=episode_key,
                                   from_=str(src), to=str(dst), size=size,
                                   drive_same=True, result="ok")
                return True
            # Cross-drive
            shutil.copy2(long(src), long(dst))
            self.journal.write(series_id=series_id, stage=stage, op="copy",
                               episode_key=episode_key,
                               from_=str(src), to=str(dst), size=size,
                               drive_same=False, result="ok")
            if self.verify_hashes:
                src_h = hash_file(src, self.hash_algo)
                dst_h = hash_file(dst, self.hash_algo)
                ok = src_h == dst_h
                self.journal.write(series_id=series_id, stage=stage,
                                   op="verify", episode_key=episode_key,
                                   from_=str(src), to=str(dst),
                                   src_hash=src_h, dst_hash=dst_h,
                                   algo=self.hash_algo,
                                   result="ok" if ok else "error",
                                   **({} if ok else {
                                       "error": "hash mismatch",
                                       "error_class": "IntegrityError"}))
                if not ok:
                    return False
            os.remove(long(src))
            self.journal.write(series_id=series_id, stage=stage, op="delete",
                               episode_key=episode_key,
                               from_=str(src), result="ok")
            return True
        except OSError as e:
            self.journal.write(series_id=series_id, stage=stage, op=op_label,
                               episode_key=episode_key,
                               from_=str(src), to=str(dst), result="error",
                               error=str(e), error_class=type(e).__name__)
            return False


def hash_file(path: Path, algo: str = "blake3", chunk: int = 1 << 20) -> str:
    """Hash a file. blake3 if available, else sha256."""
    h: Any
    if algo == "blake3":
        try:
            import blake3 as _b3  # type: ignore
            h = _b3.blake3()
        except ImportError:
            import hashlib
            h = hashlib.sha256()
    else:
        import hashlib
        h = hashlib.sha256()
    try:
        with open(long(path), "rb") as f:
            while True:
                buf = f.read(chunk)
                if not buf:
                    break
                h.update(buf)
        return h.hexdigest()
    except OSError as e:
        log.warning("hash_file failed for %s: %s", path, e)
        return ""


# =========================================================================
# Source-folder classification (Stage A)
# =========================================================================

@dataclass
class ClassifiedFile:
    """One file in a source series folder, classified by role + S/E."""
    path: Path
    role: str = ""   # episode | nfo | subtitle | poster | fanart | banner | extras | other
    season: Optional[int] = None
    episode: Optional[int] = None
    episode_end: Optional[int] = None
    is_special: bool = False
    is_multi: bool = False
    series_name_guess: str = ""

    @property
    def episode_key(self) -> str:
        if self.season is None or self.episode is None:
            return ""
        s, e1 = int(self.season), int(self.episode)
        e2 = self.episode_end or e1
        if e2 != e1:
            return f"S{s:02d}E{e1:02d}-E{e2:02d}"
        return f"S{s:02d}E{e1:02d}"


def target_already_populated(target: Path) -> bool:
    """True iff target folder exists and contains at least one season
    subfolder with at least one video file in it.

    Used to distinguish "this row was never executed" from "this row was
    executed by a previous run but executed_at didn't get stamped" (e.g.
    because series_plan.csv was open in Excel and stamp_executed_at hit
    PermissionError). The former is a real failure; the latter should be
    silently completed and the timestamp written.
    """
    if not target.is_dir():
        return False
    try:
        for child in target.iterdir():
            if not child.is_dir():
                continue
            name = child.name.lower()
            if not (name.startswith("season") or name == "specials"):
                continue
            try:
                for f in child.iterdir():
                    if f.is_file() and f.suffix.lower() in VIDEO_EXTS:
                        return True
            except OSError:
                continue
    except OSError:
        return False
    return False


# Per-episode sidecar file extensions. Files matching `<episode_stem>.<ext>`
# or `<episode_stem><.lang|-suffix>.<ext>` get moved alongside the episode.
SIDECAR_EXTS: Set[str] = {
    ".nfo", ".xml",
    ".srt", ".sub", ".idx", ".ass", ".ssa", ".vtt", ".sup", ".smi",
    ".jpg", ".jpeg", ".png", ".tbn", ".webp", ".gif",
}

# Series-root files (at the source folder's top level, not per-episode).
# These get moved to the target series folder verbatim.
SERIES_ROOT_FILENAMES: Set[str] = {
    "tvshow.nfo", "show.nfo",
    "poster.jpg", "poster.png", "folder.jpg", "folder.png",
    "fanart.jpg", "fanart.png", "backdrop.jpg",
    "banner.jpg", "banner.png",
    "logo.jpg", "logo.png", "clearart.png", "clearlogo.png",
    "characterart.png", "thumb.jpg",
}

# Plex / Kodi convention for season-level art at the SERIES root level:
#   season01-poster.jpg, season02-fanart.png, etc.
# These get moved verbatim to the target series root (NOT into the
# corresponding season folder — that's Plex's convention).
_SEASON_ART_AT_SERIES_ROOT_RE = re.compile(
    r"^season\d{1,3}-(poster|fanart|banner|landscape)\.(jpg|jpeg|png|webp)$",
    re.IGNORECASE,
)

# Files at a season SUBFOLDER's level (e.g., source/Season 1/season.nfo).
# These get moved to the corresponding target season folder.
SEASON_FOLDER_FILENAMES: Set[str] = {
    "season.nfo",
    "poster.jpg", "poster.png", "folder.jpg", "folder.png",
    "fanart.jpg", "fanart.png", "backdrop.jpg",
    "banner.jpg", "banner.png",
    "thumb.jpg", "season-thumb.jpg",
}


def find_sidecars_for_video(video_path: Path,
                             siblings: Optional[Iterable[Path]] = None
                             ) -> List[Path]:
    """Find sidecar files in the same directory as `video_path` whose
    names match the video's stem. Recognized patterns:
        <stem>.<ext>          (basic NFO/subtitle/poster)
        <stem>.<lang>.<ext>   (language-tagged subtitle, e.g. .en.srt)
        <stem>-<suffix>.<ext> (per-episode thumbs, e.g. -thumb.jpg)
        <stem>_<suffix>.<ext>
    Returns a list of Paths. Excludes the video itself.
    """
    parent = video_path.parent
    stem = video_path.stem
    if siblings is None:
        try:
            siblings = list(parent.iterdir())
        except OSError:
            return []
    out: List[Path] = []
    for sib in siblings:
        try:
            if not sib.is_file():
                continue
        except OSError:
            continue
        if sib == video_path:
            continue
        ext = sib.suffix.lower()
        if ext not in SIDECAR_EXTS:
            continue
        sib_stem = sib.stem
        if sib_stem == stem:
            out.append(sib)
            continue
        # <stem>.<lang>.<ext> or <stem>-<suffix>.<ext> — sibling stem
        # starts with the video stem followed by a separator.
        for sep in (".", "-", "_"):
            if sib_stem.startswith(stem + sep):
                out.append(sib)
                break
    return out


def render_sidecar_target_name(sidecar_path: Path, episode_src: Path,
                                episode_dst: Path) -> str:
    """Compute the canonical target filename for a sidecar.

    The sidecar's stem differs from episode_src.stem by an optional suffix
    (e.g. ".en" for .en.srt, "-thumb" for -thumb.jpg). We carry that
    suffix to the new name so the relationship to the new canonical
    episode filename is preserved.
    """
    src_video_stem = episode_src.stem
    dst_video_stem = episode_dst.stem
    sib_stem = sidecar_path.stem
    sib_ext = sidecar_path.suffix
    if sib_stem == src_video_stem:
        return dst_video_stem + sib_ext
    # sib_stem starts with src_video_stem + separator + suffix
    suffix = sib_stem[len(src_video_stem):]  # ".en", "-thumb", etc.
    return dst_video_stem + suffix + sib_ext


def find_series_root_files(source_root: Path) -> List[Path]:
    """Files at the source folder root that should move to the target
    series folder (tvshow.nfo, posters, fanart, season-NN-poster.jpg, etc.)."""
    out: List[Path] = []
    if not source_root.is_dir():
        return out
    try:
        for child in source_root.iterdir():
            try:
                if not child.is_file():
                    continue
            except OSError:
                continue
            name_lc = child.name.lower()
            if name_lc in SERIES_ROOT_FILENAMES:
                out.append(child)
                continue
            if _SEASON_ART_AT_SERIES_ROOT_RE.match(child.name):
                # season01-poster.jpg etc. — Plex convention, lives at series root.
                out.append(child)
    except OSError:
        pass
    return out


def find_season_folder_files(season_subfolder: Path) -> List[Path]:
    """Files at a source season-subfolder's root level that should move
    to the corresponding target season folder (season.nfo, season-level
    posters, etc.). Per-episode sidecars are NOT returned here — they
    should already have been handled by Stage G."""
    out: List[Path] = []
    if not season_subfolder.is_dir():
        return out
    try:
        for child in season_subfolder.iterdir():
            try:
                if not child.is_file():
                    continue
            except OSError:
                continue
            if child.name.lower() in SEASON_FOLDER_FILENAMES:
                out.append(child)
    except OSError:
        pass
    return out


def classify_source_files(source_folder: Path,
                          skip_dirs: Sequence[str] = ()) -> List[ClassifiedFile]:
    """Walk the source series folder and classify every file.

    Episode files (videos that parse to S/E) get role="episode" with parsed
    fields populated. Anything else gets role="extras" or whatever the
    extension implies. Subdirs in skip_dirs are not descended into.
    """
    if not source_folder.is_dir():
        return []
    out: List[ClassifiedFile] = []
    skip_set = set(skip_dirs)
    for dirpath, dirnames, filenames in os.walk(str(source_folder)):
        dirnames[:] = [d for d in dirnames
                       if d not in skip_set and not d.startswith(".")]
        for fn in filenames:
            fp = Path(dirpath) / fn
            cf = ClassifiedFile(path=fp)
            ext = fp.suffix.lower()
            stem = fp.stem.lower()
            parent_name = fp.parent.name

            if ext in VIDEO_EXTS:
                parsed = parse_episode(fp.name, parent_folder=parent_name)
                if parsed.parsed:
                    cf.role = "episode"
                    cf.season = parsed.season
                    cf.episode = parsed.episode
                    cf.episode_end = parsed.episode_end
                    cf.is_special = parsed.is_special
                    cf.is_multi = parsed.is_multi
                    cf.series_name_guess = parsed.series_name_guess
                else:
                    cf.role = "extras"
            elif ext in {".nfo", ".xml"}:
                cf.role = "nfo"
            elif ext in {".srt", ".sub", ".idx", ".ass", ".ssa", ".vtt", ".sup", ".smi"}:
                cf.role = "subtitle"
            elif ext in {".jpg", ".jpeg", ".png", ".tbn", ".webp", ".gif"}:
                if any(kw in stem for kw in ("poster", "cover", "folder")):
                    cf.role = "poster"
                elif any(kw in stem for kw in ("fanart", "backdrop")):
                    cf.role = "fanart"
                elif "banner" in stem:
                    cf.role = "banner"
                else:
                    cf.role = "image"
            else:
                cf.role = "other"
            out.append(cf)
    return out


# =========================================================================
# Target-path computation (Stage D)
# =========================================================================

def render_series_folder(plan: SeriesPlanRow, cfg: Config) -> Path:
    """`<tv_root>/<Series Name> (<Year>) {tmdb-#####}` per design §9."""
    fields = {
        "title": sanitize_for_windows(plan.matched_series),
        "year": plan.matched_year if plan.matched_year else "",
        "tmdb_id": plan.tmdb_series_id or 0,
    }
    rendered = cfg.tv_series_template.format(**fields)
    rendered = sanitize_for_windows(rendered)
    return cfg.tv_root / rendered


def render_season_folder(season_number: int, season_year: Optional[int],
                         cfg: Config) -> str:
    """`Season ## (<Year>)` or `Specials` per design §9."""
    if season_number == 0:
        return cfg.tv_specials_folder
    fields = {
        "season": int(season_number),
        "season_year": season_year if season_year else "",
    }
    rendered = cfg.tv_season_template.format(**fields)
    return sanitize_for_windows(rendered)


def render_episode_filename(plan: SeriesPlanRow, cf: ClassifiedFile,
                            episode_title: str, cfg: Config) -> str:
    """`<Series Name> S##E## - <Episode Title>.<ext>` per design §9."""
    ext = cf.path.suffix
    s = int(cf.season or 0)
    e1 = int(cf.episode or 0)
    e2 = cf.episode_end or e1
    fields = {
        "title": sanitize_for_windows(plan.matched_series),
        "season": s,
        "episode": e1,
        "episode_title": sanitize_for_windows(episode_title) if episode_title else "",
    }
    rendered = cfg.tv_episode_template.format(**fields)
    if e2 != e1:
        # Multi-ep: append "-E##" before " - <title>"
        rendered = re.sub(r"(S\d+E\d+)( - |\.)",
                          rf"\1-E{e2:02d}\2", rendered, count=1)
    rendered = sanitize_for_windows(rendered)
    if not rendered.endswith(ext):
        rendered = rendered + ext
    return rendered


# =========================================================================
# Merge groups (round 6c)
# =========================================================================

@dataclass
class MergeGroup:
    """A set of AUTO plan rows that share the same target_folder.

    Single-source groups (followers == []) are processed identically to the
    pre-merge code path. Multi-source groups (followers non-empty) are a
    legitimate merge: all members must share the same tmdb_series_id (this
    is enforced by build_merge_groups() — anything else surfaces as a
    duplicate_target preflight error).

    The primary is the row with the lowest series_id; it owns the journal's
    series_id for series-level operations (mkdir_series, mkdir_season). Each
    individual file move is journaled with the *source* row's series_id so
    per-row accountability survives.
    """
    primary: SeriesPlanRow
    followers: List[SeriesPlanRow] = field(default_factory=list)

    @property
    def all_rows(self) -> List[SeriesPlanRow]:
        return [self.primary] + self.followers

    @property
    def is_merge(self) -> bool:
        return bool(self.followers)

    @property
    def target_folder(self) -> str:
        return self.primary.target_folder

    @property
    def tmdb_series_id(self) -> Optional[int]:
        return self.primary.tmdb_series_id


def build_merge_groups(plan: List[SeriesPlanRow]
                       ) -> Tuple[List[MergeGroup], List[Tuple[str, List[int]]]]:
    """Group AUTO rows by lowercased target_folder.

    Returns ``(groups, conflicts)``:
      * groups - every AUTO row appears in exactly one MergeGroup. Groups
        with len(all_rows) > 1 are merges; their members all share the same
        tmdb_series_id.
      * conflicts - lists of ``(target_folder, [series_ids...])`` where 2+
        rows resolve to the same target but have *different* tmdb_series_ids
        — these stay duplicate_target preflight errors (the user has to
        decide which row is correct).
    """
    auto_rows = [p for p in plan if p.is_auto and not p.already_executed]
    by_tgt: Dict[str, List[SeriesPlanRow]] = {}
    for p in auto_rows:
        if not p.target_folder:
            continue
        key = p.target_folder.replace("/", "\\").lower()
        by_tgt.setdefault(key, []).append(p)

    groups: List[MergeGroup] = []
    conflicts: List[Tuple[str, List[int]]] = []
    for tgt, rows in by_tgt.items():
        rows.sort(key=lambda r: r.series_id)
        if len(rows) == 1:
            groups.append(MergeGroup(primary=rows[0]))
            continue
        # Legit merge requires all rows agree on tmdb_series_id (and it's set).
        tmdb_ids = {r.tmdb_series_id for r in rows if r.tmdb_series_id}
        if len(tmdb_ids) == 1 and None not in {r.tmdb_series_id for r in rows}:
            groups.append(MergeGroup(primary=rows[0], followers=rows[1:]))
        else:
            conflicts.append((tgt, [r.series_id for r in rows]))

    # Also yield rows with no target_folder as their own (single) group so
    # they continue to be processed and reach the missing_target_folder check.
    for p in auto_rows:
        if not p.target_folder:
            groups.append(MergeGroup(primary=p))

    return groups, conflicts


# =========================================================================
# Pre-flight (Pass 1)
# =========================================================================

@dataclass
class PreflightError:
    series_id: int = 0
    source_folder: str = ""
    code: str = ""
    detail: str = ""


def preflight(plan: List[SeriesPlanRow], cfg: Config) -> List[PreflightError]:
    """Static checks that don't need disk reads. Returns a list of errors;
    empty list means safe to proceed."""
    errors: List[PreflightError] = []

    auto_rows = [p for p in plan if p.is_auto and not p.already_executed]

    # Check 1: required fields populated for every AUTO row.
    for p in auto_rows:
        if not p.tmdb_series_id:
            errors.append(PreflightError(p.series_id, p.source_folder,
                                         "missing_tmdb_id",
                                         "AUTO row has no tmdb_series_id"))
        if not p.matched_series:
            errors.append(PreflightError(p.series_id, p.source_folder,
                                         "missing_matched_series",
                                         "AUTO row has no matched_series"))
        if not p.target_folder:
            errors.append(PreflightError(p.series_id, p.source_folder,
                                         "missing_target_folder",
                                         "AUTO row has no target_folder"))

    # Check 2: target_folder under tv_root.
    tv_root_norm = str(cfg.tv_root).replace("/", "\\").rstrip("\\").lower()
    for p in auto_rows:
        if not p.target_folder:
            continue
        tgt_norm = p.target_folder.replace("/", "\\").lower()
        if not tgt_norm.startswith(tv_root_norm):
            errors.append(PreflightError(p.series_id, p.source_folder,
                                         "target_outside_tv_root",
                                         f"target_folder={p.target_folder!r} "
                                         f"not under tv_root={cfg.tv_root}"))

    # Check 3: source folder exists.
    for p in auto_rows:
        if p.source_folder and not Path(p.source_folder).is_dir():
            errors.append(PreflightError(p.series_id, p.source_folder,
                                         "source_missing",
                                         f"source_folder doesn't exist on disk"))

    # Check 4: real conflicts only — same target, different tmdb_series_id.
    # Legitimate merges (same target, same tmdb_id) are handled at runtime by
    # process_series_group and are NOT errors.
    groups, conflicts = build_merge_groups(plan)
    for tgt, ids in conflicts:
        errors.append(PreflightError(
            0, tgt, "duplicate_target",
            f"series_ids={ids} share target but disagree on tmdb_series_id"))

    # Check 5: merge sanity. Any merge group whose source folder names share
    # no distinctive token with matched_series almost certainly indicates
    # corrupted plan data (off-by-one shift, manual paste error, etc.). We
    # refuse to proceed rather than dump episodes into the wrong target.
    for g in groups:
        if not g.is_merge:
            continue
        for row in g.all_rows:
            if not _source_matches_target(row.source_folder, row.matched_series):
                errors.append(PreflightError(
                    row.series_id, row.source_folder,
                    "merge_source_mismatch",
                    f"source folder shares no distinctive word with "
                    f"matched_series={row.matched_series!r}; refusing to "
                    f"merge into target={row.target_folder!r}"))

    return errors


# Heuristic stopwords for the merge-source-mismatch check.
_MERGE_CHECK_STOPWORDS = frozenset({
    "the", "a", "an", "of", "and", "to", "in", "for", "on", "at",
    "show", "series", "complete", "collection", "volume", "vol", "disc",
    "dvd", "dvdrip", "pack", "xvid", "x264", "x265", "rip", "bluray",
    "hdtv", "pdtv", "axxo",
})


def _source_matches_target(source_folder: str, matched_series: str) -> bool:
    """True iff the source folder's leaf name shares any distinctive (>3
    char, non-stopword) token with matched_series.

    This is the merge sanity gate. It's deliberately permissive: if the
    source has *any* informative word in common with the target, we pass.
    The cases it catches are gross mis-pairings — e.g. a source folder
    named "The Addams Family" that's been assigned tmdb_id and matched
    series for "The Abbott and Costello Show".
    """
    if not source_folder or not matched_series:
        return True  # caught by other preflight checks
    # Strip year and tmdb tags before tokenizing.
    leaf = os.path.basename(source_folder.rstrip("\\").rstrip("/"))
    leaf = re.sub(r"\(\d{4}(?:-\d{4})?\)", "", leaf)
    leaf = re.sub(r"\{[^}]*\}", "", leaf)
    target = re.sub(r"\(\d{4}(?:-\d{4})?\)", "", matched_series)
    target = re.sub(r"\{[^}]*\}", "", target)

    def _toks(s: str) -> Set[str]:
        s = re.sub(r"[^a-zA-Z0-9]+", " ", s).lower()
        return {t for t in s.split()
                if len(t) > 3 and t not in _MERGE_CHECK_STOPWORDS}

    src_toks = _toks(leaf)
    tgt_toks = _toks(target)
    if not src_toks:
        return True  # nothing distinctive to check; assume ok
    return bool(src_toks & tgt_toks)


def write_preflight_report(path: Path, errors: List[PreflightError]) -> None:
    cols = ["series_id", "source_folder", "code", "detail"]
    with open(path, "w", encoding="utf-8-sig", newline="",
              errors="replace") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for e in errors:
            w.writerow([e.series_id, e.source_folder, e.code, e.detail])


# =========================================================================
# Per-series flow (Pass 4) — Stages A, C-F
# =========================================================================

@dataclass
class SeriesResult:
    series_id: int
    success: bool = False
    moved: int = 0
    skipped: int = 0
    errors: int = 0
    extras: int = 0
    notes: str = ""


# Title-match upgrade: cap on number of seasons we'll fetch trying to
# resolve title-only filenames. 30 covers everything realistic; the cache
# makes subsequent runs cheap.
TITLE_MATCH_MAX_SEASONS = 30


def upgrade_extras_via_title_match(
    files: List[ClassifiedFile],
    *,
    plan: SeriesPlanRow,
    tmdb: TmdbTvClient,
    season_data: Dict[int, Dict[str, Any]],
) -> Tuple[int, int]:
    """Walk every extras-role video file in `files`, try to match its
    filename against TMDB episode titles for the series, and upgrade
    matches to role=episode (with season/episode set) in place.

    Mutates the passed `files` list AND extends `season_data` with any
    additional TMDB season responses we needed to fetch.

    Returns (matched, considered). `matched` is the count of extras
    that got upgraded; `considered` is how many extras-videos we
    examined (= matched + still-extras).
    """
    candidates = [cf for cf in files
                  if cf.role == "extras" and cf.path.suffix.lower() in VIDEO_EXTS]
    if not candidates or not plan.tmdb_series_id:
        return (0, len(candidates))

    # Fetch the show details to find total season count, then load each
    # season we don't already have. Capped to avoid runaway requests.
    n_seasons = TITLE_MATCH_MAX_SEASONS
    details = tmdb.tv_details(plan.tmdb_series_id)
    if not is_error(details) and not is_not_found(details):
        try:
            n_seasons = min(int(details.get("number_of_seasons") or
                                  TITLE_MATCH_MAX_SEASONS),
                              TITLE_MATCH_MAX_SEASONS)
        except (TypeError, ValueError):
            pass

    # Include season 0 (Specials) in the fetch loop.
    for sn in [0] + list(range(1, n_seasons + 1)):
        if sn in season_data:
            continue
        data = tmdb.tv_season(plan.tmdb_series_id, sn)
        if is_error(data) or is_not_found(data):
            continue
        season_data[sn] = data

    episodes_map = episodes_map_from_tmdb_seasons(season_data.values())
    if not episodes_map:
        return (0, len(candidates))

    matched = 0
    for cf in candidates:
        m = match_filename_to_episode(
            cf.path.name, episodes_map,
            series_name=plan.matched_series or None,
        )
        if m is None or m.confidence != "HIGH":
            continue
        # Upgrade in place.
        cf.role = "episode"
        cf.season = m.season
        cf.episode = m.episode
        cf.episode_end = None
        cf.is_special = (m.season == 0)
        cf.is_multi = False
        matched += 1
    return (matched, len(candidates))


def process_series(plan: SeriesPlanRow, cfg: Config, ops: DiskOps,
                   tmdb: TmdbTvClient) -> SeriesResult:
    """Process one AUTO series. Stages A, C-F per design §5.

    Stage A: classify source files (walk + parse).
    Stage C: fetch TMDB details + per-season data (cached).
    Stage D: compute target paths.
    Stage E: ensure target folder structure exists.
    Stage F: move episodes (atomic same-drive, copy+delete cross-drive).

    Stages G (NFO writing) and H (sidecar handling) are deferred to round 6b.

    Returns a SeriesResult with counts and per-stage outcome.
    """
    result = SeriesResult(series_id=plan.series_id)
    src = Path(plan.source_folder)
    dst = Path(plan.target_folder)

    log.info("[series %d] %s -> %s", plan.series_id, src.name, dst.name)

    # Stage A: classify
    files = classify_source_files(src, cfg.tv_skip_dirs)
    season_data: Dict[int, Dict[str, Any]] = {}

    # Stage A.5: try to upgrade extras-role video files to episodes via
    # TMDB title-matching. This is the Round 6b primary win — folders
    # full of "<show> - <episode title>.avi" files (no SxxExx) get
    # rescued from "no parseable episodes" failure.
    if plan.tmdb_series_id:
        matched, considered = upgrade_extras_via_title_match(
            files, plan=plan, tmdb=tmdb, season_data=season_data)
        if matched:
            log.info("[series %d] title-match upgraded %d/%d extras to episodes",
                     plan.series_id, matched, considered)

    episodes = [f for f in files if f.role == "episode"]
    extras = [f for f in files if f.role != "episode"]
    result.extras = len(extras)
    if not episodes:
        # Distinguish "row was never run" from "row was run, but stamp got
        # lost" (Excel held the CSV; previous version pre-1.1.1 crashed on
        # PermissionError mid-run; etc.). If target already contains
        # episodes, declare success-already-executed so the row gets
        # stamped on this pass.
        if target_already_populated(dst):
            result.success = True
            result.notes = "already executed (source empty, target populated)"
            log.info("[series %d] %s", plan.series_id, result.notes)
            return result
        result.notes = "no parseable episodes in source folder"
        log.warning("[series %d] %s", plan.series_id, result.notes)
        return result
    log.debug("[series %d] %d episodes, %d non-episode files",
              plan.series_id, len(episodes), len(extras))

    # Stage C: fetch any TMDB seasons we don't already have. The title
    # matcher above may have already populated season_data with most of
    # what we need; this just fills in any holes.
    seasons_touched = sorted({int(f.season or 0) for f in episodes
                              if f.season is not None})
    for sn in seasons_touched:
        if sn in season_data:
            continue
        data = tmdb.tv_season(plan.tmdb_series_id, sn)
        if is_error(data):
            log.warning("[series %d] TMDB season %d error: %s",
                        plan.series_id, sn, data.get("__error__"))
            result.errors += 1
            continue
        if is_not_found(data):
            log.warning("[series %d] TMDB season %d not found", plan.series_id, sn)
            continue
        season_data[sn] = data

    # Stage E: ensure series folder + season folders exist.
    if not ops.mkdir(dst, series_id=plan.series_id, stage="mkdir_series"):
        result.errors += 1
        result.notes = "could not create series target folder"
        return result

    season_dirs: Dict[int, Path] = {}
    for sn in seasons_touched:
        sd = season_data.get(sn) or {}
        first_air = str(sd.get("air_date", ""))[:4]
        season_year = int(first_air) if first_air.isdigit() else None
        season_folder_name = render_season_folder(sn, season_year, cfg)
        sd_path = dst / season_folder_name
        if not ops.mkdir(sd_path, series_id=plan.series_id,
                          stage="mkdir_season"):
            result.errors += 1
            continue
        season_dirs[sn] = sd_path

    # Stage A.5: dedup within source. If two source files claim the same
    # SxxExx, the move-everything-blindly approach causes the alphabetically-
    # first file to land at dst and the rest to fail with
    # "destination exists with different size". Same algorithm as the
    # cross-source merge dedup: hash candidates, keep largest by size
    # (or lowest path on hash-tie), quarantine the rest.
    keepers, losers = _dedup_within_source(episodes, cfg)
    if losers:
        log.info("[series %d] within-source dedup: %d keepers, %d quarantined",
                 plan.series_id, len(keepers), len(losers))

    # Stage F: move keepers (with cross-run dedup at the destination).
    cross_run_quarantine_count = 0
    for cf in keepers:
        if cf.season is None or cf.episode is None:
            continue
        sn = int(cf.season)
        sd_path = season_dirs.get(sn)
        if sd_path is None:
            log.warning("[series %d] no season folder for S%02d, skipping episode",
                        plan.series_id, sn)
            result.errors += 1
            continue
        # Episode title from TMDB (preferred) else empty.
        ep_title = ""
        sd = season_data.get(sn)
        if sd:
            ep = episode_from_season(sd, int(cf.episode))
            if ep:
                ep_title = str(ep.get("name", ""))
        episode_filename = render_episode_filename(plan, cf, ep_title, cfg)
        dst_path = sd_path / episode_filename

        # Cross-run dedup: if dst already exists with different content
        # (typically from a previous interrupted --apply), pick a winner
        # via hash/size before attempting the move.
        verdict = resolve_cross_run_conflict(
            cf.path, dst_path,
            series_id=plan.series_id, series_target=dst,
            cfg=cfg, ops=ops,
        )
        if verdict == "use_target":
            # Existing dst wins. Quarantine the source-keeper.
            qroot = cfg.mediaprep_dir / "duplicates" / dst.name
            ops.mkdir(qroot, series_id=plan.series_id,
                      stage="mkdir_quarantine")
            q_name = sanitize_for_windows(
                f"sid{plan.series_id}_{cf.episode_key}_{cf.path.name}")
            q_path = qroot / q_name
            if ops.move(cf.path, q_path, series_id=plan.series_id,
                        stage="cross_run_quarantine",
                        episode_key=cf.episode_key,
                        op_label="quarantine"):
                result.skipped += 1
                cross_run_quarantine_count += 1
            else:
                result.errors += 1
            continue
        if verdict == "error":
            result.errors += 1
            continue
        # verdict == "no_conflict" or "use_source" — proceed with normal move.
        ok = ops.move(cf.path, dst_path,
                      series_id=plan.series_id, stage="move_episode",
                      episode_key=cf.episode_key)
        if ok:
            result.moved += 1
            # Stage G: move per-episode sidecars alongside.
            for sib in find_sidecars_for_video(cf.path):
                sib_dst_name = render_sidecar_target_name(sib, cf.path, dst_path)
                sib_dst = dst_path.parent / sib_dst_name
                if ops.move(sib, sib_dst, series_id=plan.series_id,
                            stage="move_sidecar",
                            episode_key=cf.episode_key,
                            op_label="move_sidecar"):
                    result.moved += 1  # count as moved (sidecars are part of the row)
        else:
            result.errors += 1

    if cross_run_quarantine_count:
        log.info("[series %d] cross-run dedup: %d source-keepers quarantined "
                 "(target had better content)", plan.series_id,
                 cross_run_quarantine_count)

    # Stage H: series-root files (tvshow.nfo, posters, fanart). Only fire
    # when at least one episode landed — otherwise we'd strip a source
    # folder of its metadata for nothing.
    if result.moved > 0:
        for root_file in find_series_root_files(src):
            root_dst = dst / root_file.name
            if ops.move(root_file, root_dst, series_id=plan.series_id,
                        stage="move_series_root",
                        op_label="move_series_root"):
                result.moved += 1

    # Stage I: per-season-folder metadata. For each source subfolder we
    # actually pulled episodes from, look for season-level files
    # (season.nfo, poster.jpg, fanart.jpg, etc.) and move them to the
    # corresponding target season folder.
    if result.moved > 0:
        src_subs_seen: Set[Path] = set()
        for cf in keepers:
            if cf.season is None or cf.episode is None:
                continue
            sn = int(cf.season)
            tgt_season = season_dirs.get(sn)
            if tgt_season is None:
                continue
            src_sub = cf.path.parent
            if src_sub == src or src_sub in src_subs_seen:
                continue
            src_subs_seen.add(src_sub)
            for f in find_season_folder_files(src_sub):
                f_dst = tgt_season / f.name
                if ops.move(f, f_dst, series_id=plan.series_id,
                            stage="move_season_metadata",
                            op_label="move_season_metadata"):
                    result.moved += 1

    # Quarantine within-source dedup losers.
    if losers:
        quarantine_root = cfg.mediaprep_dir / "duplicates" / dst.name
        ops.mkdir(quarantine_root, series_id=plan.series_id,
                  stage="mkdir_quarantine")
        for cf, reason, keeper_path in losers:
            q_name = f"sid{plan.series_id}_{cf.episode_key}_{cf.path.name}"
            q_name = sanitize_for_windows(q_name)
            q_path = quarantine_root / q_name
            ops.journal.write(
                series_id=plan.series_id, stage="within_source_dedup",
                op="dedup_decision", episode_key=cf.episode_key,
                reason=reason, loser=str(cf.path), keeper=keeper_path,
                result="ok",
            )
            if ops.move(cf.path, q_path, series_id=plan.series_id,
                        stage="quarantine", episode_key=cf.episode_key,
                        op_label="quarantine"):
                result.skipped += 1
            else:
                result.errors += 1

    result.success = result.errors == 0
    return result


def _dedup_within_source(
    episodes: List["ClassifiedFile"], cfg: "Config"
) -> Tuple[List["ClassifiedFile"], List[Tuple["ClassifiedFile", str, str]]]:
    """Dedup a list of episodes by episode_key.

    Returns (keepers, losers). Losers are tuples of
    (ClassifiedFile, reason, keeper_path).

    For each key with multiple candidates, hash all of them. If hashes
    match, keep the one with the alphabetically-first source path
    (deterministic) and silently drop the others (still recorded in the
    journal for traceability). If hashes differ, keep the largest file
    by size; route smaller copies to quarantine so the user can decide
    which is canonical.
    """
    by_key: Dict[str, List["ClassifiedFile"]] = {}
    no_key: List["ClassifiedFile"] = []
    for cf in episodes:
        if not cf.episode_key:
            no_key.append(cf)
            continue
        by_key.setdefault(cf.episode_key, []).append(cf)

    keepers: List["ClassifiedFile"] = list(no_key)
    losers: List[Tuple["ClassifiedFile", str, str]] = []
    for ep_key, candidates in by_key.items():
        if len(candidates) == 1:
            keepers.append(candidates[0])
            continue
        sized: List[Tuple["ClassifiedFile", str, int]] = []
        for cf in candidates:
            sz = _safe_size(cf.path)
            h = hash_file(cf.path, cfg.hash_algo) if sz > 0 else ""
            sized.append((cf, h, sz))
        unique_hashes = {h for _, h, _ in sized if h}
        if len(unique_hashes) == 1:
            sized.sort(key=lambda t: str(t[0].path))
            keeper = sized[0][0]
            keepers.append(keeper)
            for cf, _h, _sz in sized[1:]:
                losers.append((cf, "duplicate_identical", str(keeper.path)))
        else:
            sized.sort(key=lambda t: (-t[2], str(t[0].path)))
            keeper = sized[0][0]
            keepers.append(keeper)
            for cf, _h, sz in sized[1:]:
                losers.append((cf, f"duplicate_smaller_size={sz}",
                               str(keeper.path)))
    return keepers, losers


# =========================================================================
# Per-series-group flow — handles single sources AND merges (round 6c)
# =========================================================================

def _safe_size(p: Path) -> int:
    try:
        return os.path.getsize(long(p))
    except OSError:
        return 0


# =========================================================================
# Cross-run dedup (Stage F helper, round 6c+)
# =========================================================================
# When a previous --apply was interrupted, killed mid-stream, or had files
# that the within-source dedup got wrong, the canonical target may already
# contain a file at the expected path with different content from what's
# in source. The plain DiskOps.move() refuses to clobber, so the move
# fails with "destination exists with different size" — leaving the user
# with files stranded in the source folder.
#
# This helper handles that case: hash both files, decide a winner via the
# same policy as within-source dedup (keep the larger one when content
# differs, drop one of the duplicates when hashes match), and quarantine
# the loser to mediaprep_dir/duplicates/<series>/. The caller proceeds
# with the normal move only if needed.

def resolve_cross_run_conflict(src: Path, dst: Path, *,
                                series_id: int,
                                series_target: Path,
                                cfg: Config, ops: DiskOps) -> str:
    """Decide a winner if dst already exists with content different from src.

    Returns:
      "no_conflict"  - dst doesn't exist, or src/dst sizes match
                       (idempotent case; caller calls ops.move() normally)
      "use_source"   - dst was the loser; it has been moved to quarantine.
                       Caller should now call ops.move(src, dst) — dst is
                       free, so the move will succeed.
      "use_target"   - src is the loser. Caller should NOT move src to dst
                       (dst is the right file). Instead, caller should
                       quarantine src.
      "error"        - hashing or quarantine failed; caller should record
                       a per-row error and skip this episode.
    """
    if not os.path.exists(long(dst)):
        return "no_conflict"
    ssz = _safe_size(src)
    dsz = _safe_size(dst)
    if ssz == dsz and ssz > 0:
        return "no_conflict"  # let DiskOps.move handle (idempotent path)

    # Real conflict. Hash both.
    src_hash = hash_file(src, cfg.hash_algo) if ssz > 0 else ""
    dst_hash = hash_file(dst, cfg.hash_algo) if dsz > 0 else ""
    quarantine_root = cfg.mediaprep_dir / "duplicates" / series_target.name

    # Identical bytes (different sizes shouldn't happen but if it does,
    # treat as identical and keep dst — caller will quarantine src).
    if src_hash and dst_hash and src_hash == dst_hash:
        ops.journal.write(
            series_id=series_id, stage="cross_run_dedup",
            op="dedup_decision",
            loser=str(src), keeper=str(dst),
            reason="cross_run_identical_hash",
            result="ok",
        )
        return "use_target"

    # Different content. Largest wins.
    if ssz >= dsz:
        # Source wins. Quarantine the existing target file so the caller
        # can move source into the now-free slot.
        ops.mkdir(quarantine_root, series_id=series_id,
                  stage="mkdir_quarantine")
        q_name = sanitize_for_windows(
            f"sid{series_id}_target_{dst.name}")
        q_path = quarantine_root / q_name
        ops.journal.write(
            series_id=series_id, stage="cross_run_dedup",
            op="dedup_decision",
            loser=str(dst), keeper=str(src),
            reason=f"cross_run_target_smaller_size={dsz}",
            result="ok",
        )
        if ops.move(dst, q_path, series_id=series_id,
                    stage="cross_run_quarantine",
                    op_label="quarantine_target"):
            return "use_source"
        return "error"

    # Target wins. Caller should quarantine source.
    ops.journal.write(
        series_id=series_id, stage="cross_run_dedup",
        op="dedup_decision",
        loser=str(src), keeper=str(dst),
        reason=f"cross_run_source_smaller_size={ssz}",
        result="ok",
    )
    return "use_target"


def process_series_group(group: MergeGroup, cfg: Config, ops: DiskOps,
                         tmdb: TmdbTvClient) -> List[SeriesResult]:
    """Process a MergeGroup. Returns one SeriesResult per member row.

    Single-row groups defer to process_series() so behavior is unchanged for
    the 99% case. Multi-row groups merge:

        Stage A: classify every source folder's files.
        Stage B: dedup episodes by SxxExx across sources:
                   - if all candidates hash-equal -> keep lowest series_id
                     row's file, silently delete the others (record as
                     "duplicate_identical" in journal).
                   - else -> keep largest file, quarantine the rest to
                     ``mediaprep_dir/duplicates/<series_folder>/`` with
                     full provenance in moves_tv.jsonl.
        Stage C: TMDB season fetch (once for the shared tmdb_series_id).
        Stage E: mkdir series + season folders (journaled with primary's id).
        Stage F: move keepers to canonical layout, journaled with each
                 keeper's *source* series_id; quarantine losers, also
                 journaled with the loser's source series_id.

    A row's SeriesResult.success requires zero errors *for that row's
    operations*. Rows that contributed only losers (all duplicates) succeed
    with moved=0, skipped=N, errors=0.
    """
    if not group.is_merge:
        return [process_series(group.primary, cfg, ops, tmdb)]

    primary = group.primary
    dst = Path(primary.target_folder)
    members = group.all_rows

    log.info("[merge %s] %d sources -> %s",
             primary.matched_series, len(members), dst.name)
    ops.journal.write(
        series_id=primary.series_id, stage="merge_start", op="merge_start",
        target=str(dst), tmdb_series_id=primary.tmdb_series_id,
        member_series_ids=[r.series_id for r in members],
        member_sources=[r.source_folder for r in members],
        result="ok",
    )

    # One result per member; we accumulate counts across the group.
    results: Dict[int, SeriesResult] = {
        r.series_id: SeriesResult(series_id=r.series_id) for r in members
    }

    # ----- Stage A: classify all source folders -----
    all_classified: List[Tuple[SeriesPlanRow, ClassifiedFile]] = []
    season_data: Dict[int, Dict[str, Any]] = {}
    for row in members:
        src = Path(row.source_folder)
        if not src.is_dir():
            log.warning("[merge %s] series %d source missing: %s",
                        primary.matched_series, row.series_id, src)
            results[row.series_id].notes = "source folder missing"
            results[row.series_id].errors += 1
            continue
        for cf in classify_source_files(src, cfg.tv_skip_dirs):
            all_classified.append((row, cf))

    # Stage A.5: title-match upgrade across the merged sources. Members
    # of a merge group share tmdb_series_id by construction, so we can
    # use the primary as the search key and have one episodes_map cover
    # everyone.
    if primary.tmdb_series_id and all_classified:
        flat_files = [cf for _, cf in all_classified]
        matched, considered = upgrade_extras_via_title_match(
            flat_files, plan=primary, tmdb=tmdb, season_data=season_data)
        if matched:
            log.info("[merge %s] title-match upgraded %d/%d extras to episodes",
                     primary.matched_series, matched, considered)

    # Re-bucket episodes vs extras after potential in-place upgrades.
    all_episodes: List[Tuple[SeriesPlanRow, ClassifiedFile]] = [
        (r, cf) for r, cf in all_classified if cf.role == "episode"
    ]
    all_extras: List[Tuple[SeriesPlanRow, ClassifiedFile]] = [
        (r, cf) for r, cf in all_classified if cf.role != "episode"
    ]
    for row in members:
        results[row.series_id].extras = sum(
            1 for r2, _ in all_extras if r2.series_id == row.series_id)

    if not all_episodes:
        # Same already-executed detection as single-source: if all sources
        # are empty but the target has episodes from a prior run, declare
        # the whole merge group already-executed so all members get stamped.
        if target_already_populated(dst):
            for r in members:
                res = results[r.series_id]
                res.success = True
                res.errors = 0  # clear any "source missing" errors above
                res.notes = ("already executed (sources empty, target "
                             "populated)")
            log.info("[merge %s] already executed (sources empty, target populated)",
                     primary.matched_series)
            ops.journal.write(series_id=primary.series_id, stage="merge_end",
                              op="merge_end", target=str(dst),
                              moved=0, quarantined=0, errors=0,
                              member_series_ids=[r.series_id for r in members],
                              result="ok", reason="already_executed")
            return list(results.values())
        for r in members:
            results[r.series_id].notes = (
                results[r.series_id].notes or
                "no parseable episodes across merged sources")
        ops.journal.write(series_id=primary.series_id, stage="merge_end",
                          op="merge_end", target=str(dst),
                          moved=0, quarantined=0, errors=len(members),
                          result="error", reason="no episodes")
        return list(results.values())

    # ----- Stage B: dedup episodes by SxxExx -----
    by_key: Dict[str, List[Tuple[SeriesPlanRow, ClassifiedFile]]] = {}
    for row, cf in all_episodes:
        k = cf.episode_key
        if not k:
            continue
        by_key.setdefault(k, []).append((row, cf))

    keepers: List[Tuple[SeriesPlanRow, ClassifiedFile]] = []
    losers: List[Tuple[SeriesPlanRow, ClassifiedFile, str, str]] = []

    for ep_key, candidates in by_key.items():
        if len(candidates) == 1:
            keepers.append(candidates[0])
            continue
        sized: List[Tuple[Tuple[SeriesPlanRow, ClassifiedFile], str, int]] = []
        for r, cf in candidates:
            sz = _safe_size(cf.path)
            h = hash_file(cf.path, cfg.hash_algo) if sz > 0 else ""
            sized.append(((r, cf), h, sz))
        unique_hashes = {h for _, h, _ in sized if h}
        if len(unique_hashes) == 1:
            sized.sort(key=lambda t: t[0][0].series_id)
            keeper_entry = sized[0][0]
            keepers.append(keeper_entry)
            keeper_path = str(keeper_entry[1].path)
            for entry, _h, _sz in sized[1:]:
                losers.append((*entry, "duplicate_identical", keeper_path))
        else:
            sized.sort(key=lambda t: (-t[2], t[0][0].series_id))
            keeper_entry = sized[0][0]
            keepers.append(keeper_entry)
            keeper_path = str(keeper_entry[1].path)
            for entry, _h, sz in sized[1:]:
                losers.append((*entry,
                               f"duplicate_smaller_size={sz}",
                               keeper_path))

    # ----- Stage C: TMDB seasons -----
    # season_data may already be populated by the title-match upgrade in
    # Stage A.5; only fetch any remaining touched seasons we don't have.
    seasons_touched = sorted({int(cf.season or 0) for _, cf in keepers
                              if cf.season is not None})
    for sn in seasons_touched:
        if sn in season_data:
            continue
        data = tmdb.tv_season(primary.tmdb_series_id, sn)
        if is_error(data):
            log.warning("[merge %s] TMDB season %d error: %s",
                        primary.matched_series, sn, data.get("__error__"))
            continue
        if is_not_found(data):
            log.warning("[merge %s] TMDB season %d not found",
                        primary.matched_series, sn)
            continue
        season_data[sn] = data

    # ----- Stage E: mkdir series + season folders -----
    if not ops.mkdir(dst, series_id=primary.series_id, stage="mkdir_series"):
        for r in members:
            results[r.series_id].errors += 1
            results[r.series_id].notes = "could not create series target folder"
        ops.journal.write(series_id=primary.series_id, stage="merge_end",
                          op="merge_end", target=str(dst), result="error",
                          reason="mkdir_series failed")
        return list(results.values())

    season_dirs: Dict[int, Path] = {}
    for sn in seasons_touched:
        sd = season_data.get(sn) or {}
        first_air = str(sd.get("air_date", ""))[:4]
        season_year = int(first_air) if first_air.isdigit() else None
        season_folder_name = render_season_folder(sn, season_year, cfg)
        sd_path = dst / season_folder_name
        if ops.mkdir(sd_path, series_id=primary.series_id,
                     stage="mkdir_season"):
            season_dirs[sn] = sd_path

    # ----- Stage F: move keepers (with cross-run dedup) -----
    moved_total = 0
    for row, cf in keepers:
        sn = int(cf.season or 0)
        sd_path = season_dirs.get(sn)
        if sd_path is None:
            log.warning("[merge %s] no season folder for S%02d, skipping %s",
                        primary.matched_series, sn, cf.path.name)
            results[row.series_id].errors += 1
            continue
        ep_title = ""
        sd = season_data.get(sn)
        if sd:
            ep = episode_from_season(sd, int(cf.episode or 0))
            if ep:
                ep_title = str(ep.get("name", ""))
        episode_filename = render_episode_filename(primary, cf, ep_title, cfg)
        dst_path = sd_path / episode_filename

        # Cross-run dedup at the destination.
        verdict = resolve_cross_run_conflict(
            cf.path, dst_path,
            series_id=row.series_id, series_target=dst,
            cfg=cfg, ops=ops,
        )
        if verdict == "use_target":
            # Existing dst wins. Quarantine source-keeper using the same
            # quarantine path as cross-source merge losers.
            q_name = sanitize_for_windows(
                f"sid{row.series_id}_{cf.episode_key}_{cf.path.name}")
            ops.mkdir(cfg.mediaprep_dir / "duplicates" / dst.name,
                      series_id=primary.series_id,
                      stage="mkdir_quarantine")
            q_path = cfg.mediaprep_dir / "duplicates" / dst.name / q_name
            if ops.move(cf.path, q_path, series_id=row.series_id,
                        stage="cross_run_quarantine",
                        episode_key=cf.episode_key,
                        op_label="quarantine"):
                results[row.series_id].skipped += 1
            else:
                results[row.series_id].errors += 1
            continue
        if verdict == "error":
            results[row.series_id].errors += 1
            continue
        if ops.move(cf.path, dst_path, series_id=row.series_id,
                    stage="merge_move", episode_key=cf.episode_key):
            results[row.series_id].moved += 1
            moved_total += 1
            # Stage G: per-episode sidecars.
            for sib in find_sidecars_for_video(cf.path):
                sib_dst_name = render_sidecar_target_name(sib, cf.path, dst_path)
                sib_dst = dst_path.parent / sib_dst_name
                if ops.move(sib, sib_dst, series_id=row.series_id,
                            stage="merge_sidecar",
                            episode_key=cf.episode_key,
                            op_label="move_sidecar"):
                    results[row.series_id].moved += 1
                    moved_total += 1
        else:
            results[row.series_id].errors += 1

    # ----- Quarantine losers -----
    quarantine_root = cfg.mediaprep_dir / "duplicates" / dst.name
    quarantined_total = 0
    if losers:
        ops.mkdir(quarantine_root, series_id=primary.series_id,
                  stage="mkdir_quarantine")
    for row, cf, reason, keeper_path in losers:
        q_name = f"sid{row.series_id}_{cf.episode_key}_{cf.path.name}"
        q_name = sanitize_for_windows(q_name)
        q_path = quarantine_root / q_name
        ops.journal.write(
            series_id=row.series_id, stage="merge_dedup",
            op="dedup_decision", episode_key=cf.episode_key,
            reason=reason, loser=str(cf.path), keeper=keeper_path,
            result="ok",
        )
        if ops.move(cf.path, q_path, series_id=row.series_id,
                    stage="quarantine", episode_key=cf.episode_key,
                    op_label="quarantine"):
            results[row.series_id].skipped += 1
            quarantined_total += 1
            if not results[row.series_id].notes:
                results[row.series_id].notes = (
                    f"merged into series_id={primary.series_id}; "
                    f"{reason}")
        else:
            results[row.series_id].errors += 1

    # ----- Stage H: series-root files from each member's source -----
    contributing_rows = {
        r.series_id for r in members
        if results[r.series_id].moved > 0
    }
    for row in members:
        if row.series_id not in contributing_rows:
            continue
        src_root = Path(row.source_folder)
        for root_file in find_series_root_files(src_root):
            root_dst = dst / root_file.name
            if ops.move(root_file, root_dst, series_id=row.series_id,
                        stage="merge_series_root",
                        op_label="move_series_root"):
                results[row.series_id].moved += 1
                moved_total += 1

    # ----- Stage I: per-season metadata from each member's source -----
    src_subs_seen: Set[Path] = set()
    for row, cf in keepers:
        if cf.season is None or cf.episode is None:
            continue
        sn = int(cf.season)
        tgt_season = season_dirs.get(sn)
        if tgt_season is None:
            continue
        src_sub = cf.path.parent
        if src_sub == Path(row.source_folder) or src_sub in src_subs_seen:
            continue
        src_subs_seen.add(src_sub)
        for f in find_season_folder_files(src_sub):
            f_dst = tgt_season / f.name
            if ops.move(f, f_dst, series_id=row.series_id,
                        stage="merge_season_metadata",
                        op_label="move_season_metadata"):
                results[row.series_id].moved += 1
                moved_total += 1

    # ----- Member-level success summary -----
    for r in members:
        res = results[r.series_id]
        res.success = res.errors == 0
        if res.success and r.series_id != primary.series_id and not res.notes:
            res.notes = f"merged into series_id={primary.series_id}"
    if results[primary.series_id].success and not results[primary.series_id].notes:
        results[primary.series_id].notes = (
            f"merged from series_ids="
            f"{[r.series_id for r in group.followers]}")

    ops.journal.write(series_id=primary.series_id, stage="merge_end",
                      op="merge_end", target=str(dst),
                      moved=moved_total, quarantined=quarantined_total,
                      errors=sum(r.errors for r in results.values()),
                      member_series_ids=[r.series_id for r in members],
                      result="ok")

    return list(results.values())


# =========================================================================
# Cleanup (Pass 5)
# =========================================================================

def cleanup_empty_source_dirs(plan: List[SeriesPlanRow], cfg: Config,
                              ops: DiskOps) -> int:
    """rmdir source folders whose contents are now all moved out. Walks
    bottom-up so leaf dirs get removed first."""
    if not cfg.rmdir_empty_sources:
        return 0
    removed = 0
    for p in plan:
        if not p.is_auto:
            continue
        src = Path(p.source_folder)
        if not src.exists():
            continue
        for dirpath, dirnames, _ in os.walk(str(src), topdown=False):
            dp = Path(dirpath)
            if ops.rmdir(dp, series_id=p.series_id, stage="cleanup"):
                removed += 1
    return removed


# =========================================================================
# Main
# =========================================================================

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=("TV library executor (Phase 6, round 6c+). Reads "
                     "series_plan.csv AUTO rows and renames/moves episodes "
                     "into the canonical layout. Default mode is dry-run; "
                     "pass --apply for the real thing."),
    )
    ap.add_argument("--config", required=True, help="Path to config.json")
    ap.add_argument("--apply", action="store_true",
                    help="Actually touch disk (default: dry-run)")
    ap.add_argument("--limit", type=int, default=0,
                    help="Process at most N groups (smoke testing)")
    ap.add_argument("--series", type=str, default="",
                    help="Comma-separated series_ids to process; ignores others")
    ap.add_argument("--skip-preflight", action="store_true",
                    help="Skip Pass 1 pre-flight checks (use only after a "
                         "successful dry-run)")
    ap.add_argument("--no-cleanup", action="store_true",
                    help="Skip Pass 5 (don't rmdir empty source folders)")
    ap.add_argument("--verify-hashes", action="store_true",
                    help="Hash-verify cross-drive copies before delete")
    ap.add_argument("--intake-dir", default=None,
                    help="Intake mode: walk this directory for video files, "
                         "match each to a series_plan row by series name, "
                         "and route into the canonical library layout. "
                         "Skips the normal Pass 1/4 plan flow. Use after "
                         "Sonarr/qbit drops new episodes into a staging dir. "
                         "If omitted, falls back to config.json's "
                         "\"intake_dir\" field (if set there).")
    ap.add_argument("--no-intake", action="store_true",
                    help="Force normal pipeline mode even if config.json "
                         "sets intake_dir. Useful for one-off rebuilds.")
    ap.add_argument("--verbose", "-v", action="store_true")
    return ap.parse_args(argv)


# =========================================================================
# Intake mode (Round 6d) — Sonarr-style staging-folder ingest
# =========================================================================

@dataclass
class IntakeStats:
    considered: int = 0      # video files found in intake dir
    matched_moved: int = 0   # successfully routed to canonical
    matched_skipped: int = 0 # cross-run dedup said target wins; source quarantined
    no_parse: int = 0        # filename didn't yield SxxExx
    no_series_match: int = 0 # parse ok but no plan row matched the series guess
    errors: int = 0


# Threshold for series-name matching against series_plan.csv. Same idea
# as the title-matcher: lower than HIGH but conservative enough to avoid
# routing files to the wrong show.
INTAKE_SERIES_MATCH_THRESHOLD = 0.80


def _normalize_series_for_intake(name: str) -> str:
    """Lowercase, strip diacritics, collapse separators+punctuation to
    spaces. Mirrors tv_episode_matcher.normalize_for_match but with a
    local copy so we don't add a hard dependency the other direction."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[._\-]+", " ", s.lower())
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def match_intake_to_plan(
    series_name_guess: str,
    plan_index: List[Tuple[str, "SeriesPlanRow"]],
) -> Optional["SeriesPlanRow"]:
    """Find the best plan row whose matched_series fuzzy-matches the
    intake file's parsed series name. plan_index is a pre-built list of
    (normalized_matched_series, SeriesPlanRow) tuples for AUTO rows.
    Returns the row whose normalized name has highest title_similarity
    against the guess, provided the score >= INTAKE_SERIES_MATCH_THRESHOLD.
    """
    if not series_name_guess:
        return None
    guess_norm = _normalize_series_for_intake(series_name_guess)
    if not guess_norm:
        return None
    # Try exact normalized match first.
    for norm_name, row in plan_index:
        if norm_name == guess_norm:
            return row
    # Fuzzy fallback. Reuse the matcher's similarity since it handles
    # token-set + sequence ratio well.
    best_row: Optional[SeriesPlanRow] = None
    best_score = 0.0
    for norm_name, row in plan_index:
        score = title_similarity(guess_norm, norm_name)
        if score > best_score:
            best_score = score
            best_row = row
    if best_score >= INTAKE_SERIES_MATCH_THRESHOLD:
        return best_row
    return None


def build_intake_plan_index(plan: List[SeriesPlanRow]
                             ) -> List[Tuple[str, SeriesPlanRow]]:
    """Pre-build the (normalized_name, row) list once per run."""
    out: List[Tuple[str, SeriesPlanRow]] = []
    seen: Set[int] = set()
    for r in plan:
        if not r.is_auto:
            continue
        if not r.tmdb_series_id or not r.target_folder or not r.matched_series:
            continue
        if r.tmdb_series_id in seen:
            # Multiple rows can share a tmdb_id (merged series). The
            # first one's target_folder is what we want — we only need
            # one entry per tmdb_id.
            continue
        seen.add(r.tmdb_series_id)
        out.append((_normalize_series_for_intake(r.matched_series), r))
    return out


def intake_one_file(
    vfile: Path,
    *,
    plan_index: List[Tuple[str, SeriesPlanRow]],
    season_data_cache: Dict[int, Dict[int, Dict[str, Any]]],
    cfg: Config,
    ops: DiskOps,
    tmdb: TmdbTvClient,
) -> str:
    """Route one intake video file. Returns:
        "moved"        - file relocated to canonical layout
        "skipped"      - cross-run dedup said target wins
        "no_parse"     - filename didn't yield SxxExx
        "no_series"    - parse ok but no plan row matched
        "error"        - couldn't move
    """
    parsed = parse_episode(vfile.name, parent_folder=vfile.parent.name)
    if not parsed.parsed or parsed.season is None or parsed.episode is None:
        log.warning("[intake] no SxxExx parse: %s", vfile.name)
        return "no_parse"

    # Use the parser's series-name guess; fall back to the parent folder
    # if the parser couldn't extract one from the filename.
    name_guess = parsed.series_name_guess or vfile.parent.name
    plan_row = match_intake_to_plan(name_guess, plan_index)
    if plan_row is None:
        log.warning("[intake] cannot match series for %s (guess=%r)",
                    vfile.name, name_guess)
        return "no_series"

    sid = plan_row.series_id
    sn = int(parsed.season)
    en = int(parsed.episode)

    # Cache TMDB season data by (tmdb_id, season_number).
    seasons_for_show = season_data_cache.setdefault(plan_row.tmdb_series_id, {})
    season_obj = seasons_for_show.get(sn)
    if season_obj is None:
        data = tmdb.tv_season(plan_row.tmdb_series_id, sn)
        if not (is_error(data) or is_not_found(data)):
            seasons_for_show[sn] = data
            season_obj = data

    # Compute target paths.
    series_target = Path(plan_row.target_folder)
    sd = season_obj or {}
    first_air = str(sd.get("air_date") or "")[:4]
    season_year = int(first_air) if first_air.isdigit() else None
    season_folder_name = render_season_folder(sn, season_year, cfg)
    target_season_dir = series_target / season_folder_name

    ep_title = ""
    if season_obj:
        ep = episode_from_season(season_obj, en)
        if ep:
            ep_title = str(ep.get("name") or "")

    cf = ClassifiedFile(
        path=vfile,
        role="episode",
        season=sn,
        episode=en,
        episode_end=parsed.episode_end,
        is_special=parsed.is_special,
        is_multi=parsed.is_multi,
    )
    episode_filename = render_episode_filename(plan_row, cf, ep_title, cfg)
    target_path = target_season_dir / episode_filename

    log.info("[intake] %s -> %s", vfile.name, target_path.name)

    if not ops.mkdir(series_target, series_id=sid, stage="intake_mkdir_series"):
        return "error"
    if not ops.mkdir(target_season_dir, series_id=sid,
                     stage="intake_mkdir_season"):
        return "error"

    verdict = resolve_cross_run_conflict(
        vfile, target_path,
        series_id=sid, series_target=series_target,
        cfg=cfg, ops=ops,
    )
    if verdict == "use_target":
        qroot = cfg.mediaprep_dir / "duplicates" / series_target.name
        ops.mkdir(qroot, series_id=sid, stage="intake_mkdir_quarantine")
        q_name = sanitize_for_windows(
            f"sid{sid}_intake_{cf.episode_key}_{vfile.name}")
        if ops.move(vfile, qroot / q_name, series_id=sid,
                    stage="intake_quarantine",
                    episode_key=cf.episode_key,
                    op_label="quarantine"):
            return "skipped"
        return "error"
    if verdict == "error":
        return "error"

    if not ops.move(vfile, target_path, series_id=sid,
                    stage="intake_move", episode_key=cf.episode_key):
        return "error"

    for sib in find_sidecars_for_video(vfile):
        sib_dst_name = render_sidecar_target_name(sib, vfile, target_path)
        sib_dst = target_path.parent / sib_dst_name
        ops.move(sib, sib_dst, series_id=sid,
                  stage="intake_sidecar", episode_key=cf.episode_key,
                  op_label="move_sidecar")

    return "moved"


def intake_pass(intake_dir: Path,
                 plan: List[SeriesPlanRow],
                 cfg: Config,
                 ops: DiskOps,
                 tmdb: TmdbTvClient) -> IntakeStats:
    """Walk intake_dir for video files, route each via intake_one_file."""
    stats = IntakeStats()
    if not intake_dir.is_dir():
        log.error("[intake] not a directory: %s", intake_dir)
        return stats

    plan_index = build_intake_plan_index(plan)
    log.info("[intake] indexed %d AUTO rows for series matching",
             len(plan_index))

    season_data_cache: Dict[int, Dict[int, Dict[str, Any]]] = {}
    for child in intake_dir.rglob("*"):
        try:
            if not child.is_file():
                continue
        except OSError:
            continue
        if child.suffix.lower() not in VIDEO_EXTS:
            continue
        stats.considered += 1
        try:
            outcome = intake_one_file(
                child, plan_index=plan_index,
                season_data_cache=season_data_cache,
                cfg=cfg, ops=ops, tmdb=tmdb,
            )
        except Exception as e:
            log.error("[intake] unhandled exception on %s: %s\n%s",
                      child, e, traceback.format_exc())
            stats.errors += 1
            continue
        if outcome == "moved":
            stats.matched_moved += 1
        elif outcome == "skipped":
            stats.matched_skipped += 1
        elif outcome == "no_parse":
            stats.no_parse += 1
        elif outcome == "no_series":
            stats.no_series_match += 1
        else:
            stats.errors += 1
    return stats


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    cfg = Config.load(Path(args.config))

    run_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    setup_logging(cfg.mediaprep_dir, run_id)

    dry_run = not args.apply
    log.info("Mode: %s", "DRY-RUN (use --apply to actually move files)"
             if dry_run else "APPLY (real moves)")
    log.info("tv_root=%s  mediaprep=%s", cfg.tv_root, cfg.mediaprep_dir)

    if not cfg.tv_root.is_dir():
        log.error("tv_root does not exist: %s", cfg.tv_root)
        return 1

    plan_path = cfg.mediaprep_dir / "series_plan.csv"
    plan = read_series_plan(plan_path)
    if not plan:
        log.error("series_plan.csv is empty or missing: %s", plan_path)
        return 1
    log.info("Loaded %d series_plan rows", len(plan))

    # ---------------- Intake mode short-circuit ----------------
    # Resolve intake_dir from CLI > config > none. --no-intake forces off.
    intake_dir_setting: Optional[Path] = None
    if not args.no_intake:
        if args.intake_dir:
            intake_dir_setting = Path(args.intake_dir)
        elif cfg.intake_dir is not None:
            intake_dir_setting = cfg.intake_dir
            log.info("Intake dir from config.json: %s", intake_dir_setting)
    if intake_dir_setting is not None:
        intake_dir = intake_dir_setting
        if not intake_dir.is_dir():
            log.error("intake_dir is not a directory: %s", intake_dir)
            return 1
        log.info("INTAKE MODE: scanning %s", intake_dir)

        journal_name = ("moves_tv_intake_dryrun.jsonl" if dry_run
                        else "moves_tv_intake.jsonl")
        journal = Journal(cfg.mediaprep_dir / journal_name, run_id, dry_run)
        ops = DiskOps(journal, dry_run, args.verify_hashes, cfg.hash_algo)
        tmdb_lang = cfg.tmdb_tv_language or cfg.language
        tmdb_cache = cfg.mediaprep_dir / "tmdb_tv_cache.sqlite"
        tmdb = TmdbTvClient(cfg.tmdb_api_key, tmdb_cache, language=tmdb_lang)
        try:
            stats = intake_pass(intake_dir, plan, cfg, ops, tmdb)
        finally:
            tmdb.close()
            journal.close()
        log.info("=" * 60)
        log.info("Intake summary%s:", " (DRY-RUN)" if dry_run else "")
        log.info("  videos considered:        %d", stats.considered)
        log.info("  matched + moved:          %d", stats.matched_moved)
        log.info("  matched + quarantined:    %d", stats.matched_skipped)
        log.info("  no SxxExx parse:          %d", stats.no_parse)
        log.info("  no series_plan match:     %d", stats.no_series_match)
        log.info("  errors:                   %d", stats.errors)
        return 0 if stats.errors == 0 else 3

    todo = [p for p in plan if p.is_auto and not p.already_executed]
    if args.series:
        wanted = {int(x.strip()) for x in args.series.split(",") if x.strip()}
        todo = [p for p in todo if p.series_id in wanted]
    if args.limit:
        todo = todo[:args.limit]
    log.info("Processing %d AUTO series (skipped: already_executed=%d, non-AUTO=%d)",
             len(todo),
             sum(1 for p in plan if p.is_auto and p.already_executed),
             sum(1 for p in plan if not p.is_auto))

    if not args.skip_preflight:
        log.info("Pass 1: pre-flight checks ...")
        errors = preflight(todo, cfg)
        if errors:
            report_path = cfg.mediaprep_dir / "preflight_errors_tv.csv"
            write_preflight_report(report_path, errors)
            log.error("Pre-flight failed: %d error(s); see %s",
                      len(errors), report_path)
            return 2
        log.info("Pre-flight ok")

    if not todo:
        log.info("Nothing to do.")
        return 0

    journal_name = "moves_tv_dryrun.jsonl" if dry_run else "moves_tv.jsonl"
    journal = Journal(cfg.mediaprep_dir / journal_name, run_id, dry_run)
    ops = DiskOps(journal, dry_run, args.verify_hashes, cfg.hash_algo)

    tmdb_lang = cfg.tmdb_tv_language or cfg.language
    tmdb_cache = cfg.mediaprep_dir / "tmdb_tv_cache.sqlite"
    tmdb = TmdbTvClient(cfg.tmdb_api_key, tmdb_cache, language=tmdb_lang)

    groups, _conflicts = build_merge_groups(todo)
    n_merge_groups = sum(1 for g in groups if g.is_merge)
    log.info("Pass 4: processing %d series in %d groups (%d merges) ...",
             len(todo), len(groups), n_merge_groups)
    succeeded = 0
    failed = 0
    total_moved = 0
    try:
        for i, group in enumerate(groups, start=1):
            try:
                results = process_series_group(group, cfg, ops, tmdb)
            except Exception as e:
                log.error("[group primary=%d] unhandled exception: %s\n%s",
                          group.primary.series_id, e, traceback.format_exc())
                failed += len(group.all_rows)
                continue
            for result in results:
                if result.success:
                    succeeded += 1
                    total_moved += result.moved
                    if not dry_run:
                        ts = dt.datetime.now(dt.timezone.utc).isoformat(
                            timespec="seconds")
                        try:
                            stamp_executed_at(plan_path, result.series_id, ts)
                        except Exception as e:
                            log.warning(
                                "stamp_executed_at raised for series %d: %s; "
                                "continuing", result.series_id, e)
                else:
                    failed += 1
                    log.warning("[series %d] failed: moved=%d errors=%d notes=%s",
                                result.series_id, result.moved, result.errors,
                                result.notes)
            if i % 5 == 0:
                log.info("Progress: %d/%d groups (succeeded=%d, failed=%d, moved=%d)",
                         i, len(groups), succeeded, failed, total_moved)
    finally:
        tmdb.close()

    if not args.no_cleanup:
        log.info("Pass 5: cleanup empty source folders ...")
        removed = cleanup_empty_source_dirs(todo, cfg, ops)
        log.info("Cleanup: %d empty folder(s) removed", removed)

    journal.close()

    log.info("=" * 60)
    log.info("Summary: %d series succeeded | %d failed | %d files moved%s",
             succeeded, failed, total_moved,
             " (dry-run)" if dry_run else "")
    return 0 if failed == 0 else 3


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        log.error("Unhandled exception:\n%s", traceback.format_exc())
        sys.exit(2)
