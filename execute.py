#!/usr/bin/env python3
"""
execute.py — Phase 3 of the movie library reorganization pipeline.

Reads plan.csv (produced by inventory.py and reviewed by the human in
phase 2) and carries out the file moves. Default mode is dry-run; pass
--apply for the real thing.

Design priorities (in order): safety, reversibility, idempotency,
resumability, then speed. See phase3_design.md for the full design.

Usage:
    py execute.py --config config.json                  # dry-run
    py execute.py --config config.json --apply          # real run
    py execute.py --config config.json --apply --limit 5
    py execute.py --config config.json --apply --titles 42,43,44
    py execute.py status --config config.json           # report state
"""

from __future__ import annotations

import argparse
import bisect
import csv
import datetime as dt
import hashlib
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

# ---- optional deps -------------------------------------------------------

try:
    import blake3  # type: ignore
    _HAS_BLAKE3 = True
except ImportError:
    _HAS_BLAKE3 = False

try:
    import requests  # type: ignore
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False


VERSION = "1.0.0"
log = logging.getLogger("execute")


# =========================================================================
# Constants and helpers
# =========================================================================

# File extensions we recognize (lowercased)
VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".wmv", ".flv",
              ".mpg", ".mpeg", ".m2ts", ".ts", ".vob", ".iso", ".webm",
              ".divx", ".asf", ".rm", ".rmvb", ".ogm"}
SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".sup", ".smi"}
NFO_EXTS = {".nfo"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tbn", ".webp"}
MISC_EXTS = {".txt", ".url", ".lnk", ".par2", ".sfv", ".md5", ".sha1"}

# Filenames that are recognized as posters/fanart regardless of stem.
SPECIAL_IMAGE_NAMES = {
    "poster.jpg", "poster.jpeg", "poster.png",
    "fanart.jpg", "fanart.jpeg", "fanart.png",
    "folder.jpg", "folder.jpeg", "folder.png",
    "banner.jpg", "banner.jpeg", "banner.png",
    "cover.jpg", "cover.jpeg", "cover.png",
    "thumb.jpg", "thumb.jpeg", "thumb.png",
    "clearart.jpg", "clearlogo.jpg", "discart.jpg", "landscape.jpg",
}

# Suffixes typically appended to art (after the video stem)
SPECIAL_IMAGE_SUFFIXES = ("-poster", "-fanart", "-thumb", "-banner",
                          "-clearart", "-clearlogo", "-discart", "-landscape")

# Quality/release tags stripped before stem comparison in sidecar matching.
# These are common scene release tags; lowercased; longer ones first.
_STEM_NORMALIZE_TOKENS = [
    "bluray", "blu-ray", "remux", "uhd", "hdr10", "hdr", "dovi", "dolby",
    "atmos", "truehd", "dts-hd", "dtshd", "dts", "ddp5", "ddp", "dd5",
    "ac3", "aac", "flac", "h264", "h.264", "x264", "h265", "h.265", "x265",
    "hevc", "10bit", "8bit", "1080p", "2160p", "720p", "480p", "576p",
    "webdl", "web-dl", "webrip", "web", "hdrip", "brrip", "dvdrip", "dvdscr",
    "hdtv", "pdtv", "proper", "repack", "extended", "directors", "uncut",
    "remastered", "criterion", "imax",
]

# Windows reserved device names (case-insensitive)
WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# Forbidden characters mapping for Windows filenames
_WINDOWS_CHAR_MAP = {
    "<": "(",
    ">": ")",
    ":": " -",
    '"': "'",
    "/": "-",
    "\\": "-",
    "|": "-",
    "?": "",
    "*": "",
}

LONG_PATH_PREFIX = "\\\\?\\"

# Default config values (overridden by config.json keys)
_DEFAULT_CONFIG = {
    "extras_subfolder": "Extras",
    "nfo_format": "kodi",
    "nfo_overwrite_policy": "backup-on-conflict",
    "nfo_richness": "rich",
    "filename_template": "{title} ({year}) {{imdb-{imdb_id}}}",
    "long_path_prefix": True,
    "rmdir_empty_sources": True,
    "verify_hashes_default": False,
    "large_extra_threshold_mb": 500,
    "language": "en-US",
}


# =========================================================================
# Path helpers
# =========================================================================

def _norm_path(p: str | Path) -> Path:
    """Normalize a path string to a Path object with backslashes on Windows."""
    if isinstance(p, Path):
        s = str(p)
    else:
        s = p
    s = s.replace("/", "\\")
    return Path(s)


def long(p: Path) -> str:
    r"""Return a path string suitable for Windows long-path APIs.

    Adds the \\?\ prefix when the path is absolute and not already prefixed.
    No-op on non-Windows.
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
    """True if two paths live on the same Windows drive letter (or UNC root)."""
    try:
        da = os.path.splitdrive(str(a))[0].lower()
        db = os.path.splitdrive(str(b))[0].lower()
        return da == db and da != ""
    except Exception:
        return False


def sanitize_for_windows(name: str) -> str:
    """Strip/replace forbidden characters; collapse whitespace; handle reserved names."""
    if not name:
        return name
    out = name
    for ch, repl in _WINDOWS_CHAR_MAP.items():
        out = out.replace(ch, repl)
    # Collapse multiple spaces
    out = re.sub(r"\s+", " ", out).strip()
    # Strip trailing dots/spaces (Windows silently drops them)
    out = out.rstrip(". ")
    # Reserved device names
    base, _, ext = out.rpartition(".") if "." in out else (out, "", "")
    test = base if base else out
    if test.upper() in WINDOWS_RESERVED:
        out = (test + "_") + (("." + ext) if ext else "")
    return out


def render_filename_template(template: str, fields: Dict[str, str]) -> str:
    """Render the filename template with sanitization applied to each field."""
    safe_fields = {k: sanitize_for_windows(str(v)) for k, v in fields.items()}
    try:
        return template.format(**safe_fields)
    except KeyError as e:
        raise ValueError(f"filename_template references unknown field {e}")


# =========================================================================
# Config
# =========================================================================

@dataclass
class Config:
    tmdb_api_key: str
    movies_root: Path
    mediaprep_dir: Path
    duplicates_dir: Path
    language: str
    strip_collection_suffix: bool
    hash_algo: str
    extras_size_threshold_mb: int
    confidence_auto: int
    confidence_review: int
    # Phase 3 keys
    extras_subfolder: str
    nfo_format: str
    nfo_overwrite_policy: str
    nfo_richness: str
    filename_template: str
    long_path_prefix: bool
    rmdir_empty_sources: bool
    verify_hashes_default: bool
    large_extra_threshold_mb: int
    raw: Dict[str, Any]


def load_config(path: Path) -> Config:
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    merged = {**_DEFAULT_CONFIG, **{k: v for k, v in data.items() if not k.startswith("_")}}
    try:
        cfg = Config(
            tmdb_api_key=merged.get("tmdb_api_key", ""),
            movies_root=_norm_path(merged["movies_root"]),
            mediaprep_dir=_norm_path(merged["mediaprep_dir"]),
            duplicates_dir=_norm_path(merged.get("duplicates_dir", merged.get("duplicates_root", "H:/duplicates"))),
            language=merged.get("language", "en-US"),
            strip_collection_suffix=bool(merged.get("strip_collection_suffix", True)),
            hash_algo=merged.get("hash_algo", "blake3"),
            extras_size_threshold_mb=int(merged.get("extras_size_threshold_mb", 500)),
            confidence_auto=int(merged.get("confidence_auto", 75)),
            confidence_review=int(merged.get("confidence_review", 30)),
            extras_subfolder=merged.get("extras_subfolder", "Extras"),
            nfo_format=merged.get("nfo_format", "kodi"),
            nfo_overwrite_policy=merged.get("nfo_overwrite_policy", "backup-on-conflict"),
            nfo_richness=merged.get("nfo_richness", "rich"),
            filename_template=merged.get("filename_template",
                                         "{title} ({year}) {{imdb-{imdb_id}}}"),
            long_path_prefix=bool(merged.get("long_path_prefix", True)),
            rmdir_empty_sources=bool(merged.get("rmdir_empty_sources", True)),
            verify_hashes_default=bool(merged.get("verify_hashes_default", False)),
            large_extra_threshold_mb=int(merged.get("large_extra_threshold_mb", 500)),
            raw=merged,
        )
    except KeyError as e:
        raise ValueError(f"Config missing required key: {e}")
    return cfg


# =========================================================================
# Logging
# =========================================================================

def setup_logging(mediaprep: Path, run_id: str) -> Path:
    mediaprep.mkdir(parents=True, exist_ok=True)
    log_path = mediaprep / f"execute_{run_id}.log"
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

    log.info("execute.py v%s starting; log file: %s", VERSION, log_path)
    return log_path


# =========================================================================
# Data classes
# =========================================================================

@dataclass
class PlanRow:
    title_id: int
    source_folder: Path           # absolute (movies_root + relative_folder)
    main_video_file: str          # filename only (from plan.csv)
    main_video_path: Path         # absolute, resolved from inventory at attach time
    tmdb_id: str
    imdb_id: str
    matched_title: str
    matched_year: str
    collection_name: str
    runtime_file_min: str         # "" if absent in plan.csv
    runtime_tmdb_min: str
    confidence: int
    target_folder: Path
    action: str
    duplicate_role: str
    notes: str
    flags: List[str]
    executed_at: str
    raw: Dict[str, str]           # full original row, for write-back

    def is_auto(self) -> bool:
        return self.action.strip().upper() == "AUTO"


@dataclass
class InventoryFile:
    file_id: int
    abs_path: Path                # full file path (from inventory.csv abs_path)
    parent_folder: Path
    filename: str
    ext: str
    size_bytes: int
    role: str                     # main_video, nfo, poster, fanart, subtitle, extra, other


@dataclass
class DupGroup:
    group_id: str
    keeper_title_id: int
    loser_title_ids: List[int]
    match_basis: str


# =========================================================================
# CSV reading (resilient)
# =========================================================================

_MOJIBAKE_RX = re.compile(r"[À-Ã][-¿]")


def _unmojibake(s: str) -> str:
    """Repair the typical Excel-on-Windows damage where UTF-8 bytes for
    accented characters were read as latin-1 and stored.

    Example: 'AlegrÃ\xada' (Alegría stored as latin-1 of UTF-8 bytes) →
    'Alegría'.

    Only applied if the string contains the diagnostic two-byte pattern
    'Ã' or similar followed by a continuation byte. Otherwise returned as-is.
    """
    if not s or not _MOJIBAKE_RX.search(s):
        return s
    try:
        recovered = s.encode("latin-1").decode("utf-8")
        return recovered
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def _read_csv_dicts(path: Path) -> List[Dict[str, str]]:
    """Read a CSV resiliently. Tries UTF-8 (with BOM) first, then cp1252,
    then UTF-8 with replacement of bad bytes. Excel on Windows often saves
    CSVs as cp1252 (Windows-1252), which UTF-8 decoders reject."""
    if not path.exists():
        raise FileNotFoundError(f"Required input not found: {path}")
    # Try utf-8-sig (canonical, BOM-tolerant)
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            return [dict(r) for r in csv.DictReader(f)]
    except UnicodeDecodeError as e:
        log.warning("UTF-8 decode failed for %s (%s); trying cp1252",
                    path, e)
    # Fall back to cp1252 (Excel's default on Windows)
    try:
        with open(path, "r", encoding="cp1252", newline="") as f:
            return [dict(r) for r in csv.DictReader(f)]
    except UnicodeDecodeError as e:
        log.warning("cp1252 decode also failed for %s (%s); using latin-1 (lossless)",
                    path, e)
    # Last resort: latin-1 (ISO-8859-1). Every byte 0x00-0xFF maps to a valid
    # codepoint, so this NEVER raises; characters might display oddly but
    # nothing is lost. Better than utf-8-with-replace, which would silently
    # turn non-decodable bytes into U+FFFD.
    with open(path, "r", encoding="latin-1", newline="") as f:
        return [dict(r) for r in csv.DictReader(f)]


def read_plan(path: Path, movies_root: Path) -> List[PlanRow]:
    """Read plan.csv as written by inventory.py.

    inventory.py writes source_folder as a path relative to movies_root, and
    main_video_file as a bare filename. We promote both to absolute paths
    here. Some fields (executed_at, runtime_*) may not exist in plan.csv yet
    on a first run; they default to "".
    """
    rows = _read_csv_dicts(path)
    out: List[PlanRow] = []
    for r in rows:
        try:
            tid = int((r.get("title_id") or "0").strip())
        except ValueError:
            continue
        if not tid:
            continue
        # inventory.py uses comma-separated flags in plan.csv
        flags_raw = r.get("flags", "") or ""
        flags = [f.strip() for f in re.split(r"[;,]", flags_raw) if f.strip()]
        try:
            conf = int(float((r.get("confidence") or "0").strip()))
        except ValueError:
            conf = 0
        # Promote source_folder (relative to movies_root) to absolute.
        # Apply mojibake repair: Excel often double-encodes non-ASCII names.
        rel_src = _unmojibake((r.get("source_folder") or "").strip())
        if rel_src:
            rel_path = _norm_path(rel_src)
            if rel_path.is_absolute():
                src_abs = rel_path
            else:
                src_abs = _norm_path(str(movies_root) + "\\" + rel_src.lstrip("\\/"))
        else:
            src_abs = _norm_path("")
        pr = PlanRow(
            title_id=tid,
            source_folder=src_abs,
            main_video_file=_unmojibake((r.get("main_video_file")
                             or r.get("main_video_path") or "").strip()),
            main_video_path=Path(""),  # filled in by attach_main_video_paths()
            tmdb_id=(r.get("tmdb_id") or "").strip(),
            imdb_id=(r.get("imdb_id") or "").strip(),
            matched_title=(r.get("matched_title") or "").strip(),
            matched_year=(r.get("matched_year") or "").strip(),
            collection_name=(r.get("collection_name") or "").strip(),
            runtime_file_min=(r.get("runtime_file_min") or "").strip(),
            runtime_tmdb_min=(r.get("runtime_tmdb_min") or "").strip(),
            confidence=conf,
            target_folder=_norm_path(r.get("target_folder", "")),
            action=(r.get("action") or "").strip(),
            duplicate_role=(r.get("duplicate_role") or "").strip(),
            notes=(r.get("notes") or "").strip(),
            flags=flags,
            executed_at=(r.get("executed_at") or "").strip(),
            raw=dict(r),
        )
        out.append(pr)
    return out


def write_plan(path: Path, header: List[str], rows: List[PlanRow]) -> None:
    """Write plan.csv back with executed_at updated.

    Atomic-write via temp-file + os.replace so a crash mid-write can never
    leave plan.csv with a NUL tail. If the destination is locked (Excel
    has it open), fall back to a timestamped sibling and write directly.
    """
    if "executed_at" not in header:
        header = list(header) + ["executed_at"]

    def _serialize(file_obj) -> None:
        writer = csv.DictWriter(file_obj, fieldnames=header, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            row = dict(r.raw)
            row["executed_at"] = r.executed_at
            row["action"] = r.action
            writer.writerow(row)
        file_obj.flush()
        try:
            os.fsync(file_obj.fileno())
        except OSError:
            pass

    # Atomic path: write to plan.csv.tmp, fsync, rename over plan.csv.
    tmp_path = path.with_name(path.name + ".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="") as f:
            _serialize(f)
        os.replace(str(tmp_path), str(path))
        return
    except PermissionError as e:
        # plan.csv is locked (Excel). Clean up tmp and fall back.
        try:
            if tmp_path.exists():
                os.remove(str(tmp_path))
        except OSError:
            pass
        ts = time.strftime("%Y%m%d_%H%M%S")
        target = path.with_name(f"{path.stem}_{ts}{path.suffix}")
        log.warning("Could not replace %s (locked by another process — Excel?). "
                    "Writing to %s instead.", path, target)
        with open(target, "w", encoding="utf-8", newline="") as f:
            _serialize(f)


def read_inventory(path: Path) -> Tuple[List[InventoryFile], Dict[str, List[InventoryFile]]]:
    """Read inventory.csv. Returns (flat list, by-parent-folder index).

    inventory.py columns: file_id, abs_path, parent_folder, filename, ext,
    size_bytes, mtime, file_hash, role. There is NO title_id column — files
    are mapped to titles by parent_folder being under the title's source_folder.
    """
    rows = _read_csv_dicts(path)
    files: List[InventoryFile] = []
    by_parent: Dict[str, List[InventoryFile]] = {}
    for r in rows:
        try:
            fid = int((r.get("file_id") or "0").strip())
        except ValueError:
            fid = 0
        try:
            sz = int((r.get("size_bytes") or "0").strip())
        except ValueError:
            sz = 0
        ap = _norm_path(r.get("abs_path", ""))
        if not str(ap):
            continue
        parent = _norm_path(r.get("parent_folder", "")) or ap.parent
        inv = InventoryFile(
            file_id=fid,
            abs_path=ap,
            parent_folder=parent,
            filename=(r.get("filename") or ap.name).strip(),
            ext=((r.get("ext") or ap.suffix) or "").strip().lower(),
            size_bytes=sz,
            role=(r.get("role") or "").strip(),
        )
        files.append(inv)
        # NFC-normalize the parent key so Excel-decomposed source_folder
        # values from plan.csv still match.
        by_parent.setdefault(_nfc(str(parent)).lower(), []).append(inv)
    return files, by_parent


def _nfc(s: str) -> str:
    """NFC normalize a string. Excel's CSV-UTF-8 save can decompose
    accented characters (e.g. í as i + U+0301) while inventory.py wrote
    composed forms — without this, the two strings compare unequal."""
    return unicodedata.normalize("NFC", s)


def title_files(plan_row: PlanRow,
                by_parent: Dict[str, List[InventoryFile]],
                sorted_parents: Optional[List[str]] = None) -> List[InventoryFile]:
    """Return all inventory files belonging to this title.

    Two cases:

    1. Folder-based title — `source_folder` is a real directory under
       movies_root. All inventory files whose `parent_folder` is that
       directory or any subdirectory of it belong to the title. Uses
       binary search on `sorted_parents` for O(log N + K) lookup
       instead of scanning all parents per call.

    2. Flat-file title — `source_folder` ends in a video extension,
       meaning the movie is a single file directly in `movies_root` (no
       enclosing folder). inventory.py records the filename as the
       "title folder" for this case. We match the main video file plus
       any sidecars in the same parent whose stem matches.
    """
    src = plan_row.source_folder
    # NFC-normalize on the lookup side too. by_parent keys are NFC-normalized
    # at build time, so plan.csv values (which Excel may have decomposed
    # during a UTF-8 save) need the same treatment to match.
    src_low = _nfc(str(src)).lower().rstrip("\\")
    out: List[InventoryFile] = []

    # Flat-file case: source_folder ends with a video extension.
    if src.suffix and src.suffix.lower() in VIDEO_EXTS:
        parent_low = _nfc(str(src.parent)).lower().rstrip("\\")
        candidates = by_parent.get(parent_low, [])
        wanted_name_low = _nfc(src.name).lower()
        wanted_stem_low = _nfc(src.stem).lower()
        for f in candidates:
            fn_low = _nfc(f.filename).lower()
            if fn_low == wanted_name_low:
                out.append(f)
            elif fn_low.startswith(wanted_stem_low):
                out.append(f)
        return out

    # Folder-based case. Direct hit (most titles have all files at the
    # top level of source_folder).
    direct = by_parent.get(src_low)
    if direct:
        out.extend(direct)

    # Subdirectory files (DVD rips with VIDEO_TS, etc.). Use binary search
    # over the sorted parent list instead of scanning every parent.
    if sorted_parents is None:
        sorted_parents = sorted(by_parent.keys())
    prefix = src_low + "\\"
    idx = bisect.bisect_left(sorted_parents, prefix)
    while idx < len(sorted_parents):
        p = sorted_parents[idx]
        if p.startswith(prefix):
            out.extend(by_parent[p])
            idx += 1
        else:
            break
    return out


def attach_main_video_path(plan_row: PlanRow,
                           files: List[InventoryFile]) -> None:
    """Set plan_row.main_video_path from the inventory file with role=main_video.
    Falls back to source_folder/main_video_file if no main_video role found."""
    for f in files:
        if f.role == "main_video":
            plan_row.main_video_path = f.abs_path
            return
    # Fallback by filename match
    if plan_row.main_video_file:
        wanted = plan_row.main_video_file.lower()
        for f in files:
            if f.filename.lower() == wanted:
                plan_row.main_video_path = f.abs_path
                return
    # Last resort: assume in source_folder
    if plan_row.main_video_file:
        plan_row.main_video_path = plan_row.source_folder / plan_row.main_video_file


def read_duplicates(path: Path) -> List[DupGroup]:
    if not path.exists():
        return []
    rows = _read_csv_dicts(path)
    out: List[DupGroup] = []
    for r in rows:
        gid = (r.get("group_id") or "").strip()
        try:
            keeper = int((r.get("keeper_title_id") or "0").strip())
        except ValueError:
            continue
        if not keeper:
            continue
        losers_raw = (r.get("duplicate_title_ids") or r.get("loser_title_ids") or "").strip()
        losers: List[int] = []
        for tok in re.split(r"[;,]", losers_raw):
            tok = tok.strip()
            if not tok:
                continue
            try:
                lid = int(tok)
            except ValueError:
                continue
            # Filter out invalid IDs (0 or negative). These can sneak in if
            # Excel mangled a long comma-separated cell.
            if lid <= 0:
                continue
            losers.append(lid)
        out.append(DupGroup(
            group_id=gid or f"group_{keeper}",
            keeper_title_id=keeper,
            loser_title_ids=losers,
            match_basis=(r.get("match_type") or r.get("match_basis") or "").strip(),
        ))
    return out


# =========================================================================
# Journal (append-only JSONL)
# =========================================================================

class Journal:
    """Append-only JSONL writer. One file per run-mode (real vs dry)."""

    def __init__(self, path: Path, run_id: str, dry_run: bool):
        self.path = path
        self.run_id = run_id
        self.dry_run = dry_run
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(path, "a", encoding="utf-8", newline="")

    def write(self, **fields: Any) -> None:
        rec = {
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
# Disk operations (with journaling)
# =========================================================================

class DiskOps:
    """All disk-mutating operations route through this class so they get
    journaled and respect dry-run."""

    def __init__(self, journal: Journal, dry_run: bool, verify_hashes: bool,
                 hash_algo: str):
        self.journal = journal
        self.dry_run = dry_run
        self.verify_hashes = verify_hashes
        self.hash_algo = hash_algo

    # -- mkdir ------------------------------------------------------------

    def mkdir(self, path: Path, *, title_id: Optional[int] = None,
              stage: str = "") -> bool:
        if path.exists():
            self.journal.write(title_id=title_id, stage=stage, op="mkdir",
                               to=str(path), result="skipped",
                               reason="already exists")
            return True
        if self.dry_run:
            self.journal.write(title_id=title_id, stage=stage, op="mkdir",
                               to=str(path), result="dry-run")
            return True
        try:
            os.makedirs(long(path), exist_ok=True)
            self.journal.write(title_id=title_id, stage=stage, op="mkdir",
                               to=str(path), result="ok")
            return True
        except OSError as e:
            self.journal.write(title_id=title_id, stage=stage, op="mkdir",
                               to=str(path), result="error",
                               error=str(e), error_class=type(e).__name__)
            return False

    # -- rmdir ------------------------------------------------------------

    def rmdir(self, path: Path, *, title_id: Optional[int] = None,
              stage: str = "") -> bool:
        if not path.exists():
            self.journal.write(title_id=title_id, stage=stage, op="rmdir",
                               from_=str(path), result="skipped",
                               reason="does not exist")
            return True
        try:
            entries = list(os.listdir(long(path)))
        except OSError as e:
            self.journal.write(title_id=title_id, stage=stage, op="rmdir",
                               from_=str(path), result="error",
                               error=str(e), error_class=type(e).__name__)
            return False
        if entries:
            self.journal.write(title_id=title_id, stage=stage, op="rmdir",
                               from_=str(path), result="skipped",
                               reason="not empty",
                               remaining=";".join(entries[:10]))
            return False
        if self.dry_run:
            self.journal.write(title_id=title_id, stage=stage, op="rmdir",
                               from_=str(path), result="dry-run")
            return True
        try:
            os.rmdir(long(path))
            self.journal.write(title_id=title_id, stage=stage, op="rmdir",
                               from_=str(path), result="ok")
            return True
        except OSError as e:
            self.journal.write(title_id=title_id, stage=stage, op="rmdir",
                               from_=str(path), result="error",
                               error=str(e), error_class=type(e).__name__)
            return False

    # -- move (rename or cross-drive copy) --------------------------------

    def move(self, src: Path, dst: Path, *, title_id: Optional[int] = None,
             stage: str = "", op_label: str = "move") -> bool:
        """Move a file. Same-drive uses os.rename (atomic). Cross-drive uses
        shutil.copy2 + os.remove, journaled as separate copy and delete ops.
        Optional hash verification before delete on cross-drive."""
        if not src.exists():
            self.journal.write(title_id=title_id, stage=stage, op=op_label,
                               from_=str(src), to=str(dst), result="error",
                               error="source missing", error_class="FileNotFoundError")
            return False
        # Idempotency: target already exists with right size.
        if dst.exists():
            try:
                ssz = os.path.getsize(long(src))
                dsz = os.path.getsize(long(dst))
                if ssz == dsz:
                    # Source still around after a previous run? Treat as a re-attempt:
                    # if src and dst are the same path, no-op; else delete src to
                    # complete the move idempotently.
                    if str(src).lower() == str(dst).lower():
                        self.journal.write(title_id=title_id, stage=stage, op=op_label,
                                           from_=str(src), to=str(dst),
                                           result="skipped", reason="src == dst")
                        return True
                    if self.dry_run:
                        self.journal.write(title_id=title_id, stage=stage, op=op_label,
                                           from_=str(src), to=str(dst),
                                           result="dry-run",
                                           reason="dst exists; would delete src")
                        return True
                    try:
                        os.remove(long(src))
                        self.journal.write(title_id=title_id, stage=stage, op="delete",
                                           from_=str(src), result="ok",
                                           reason="dst already in place")
                        return True
                    except OSError as e:
                        self.journal.write(title_id=title_id, stage=stage, op="delete",
                                           from_=str(src), result="error",
                                           error=str(e), error_class=type(e).__name__)
                        return False
            except OSError:
                pass
            # Different size means destination is something else — that's a hard error.
            self.journal.write(title_id=title_id, stage=stage, op=op_label,
                               from_=str(src), to=str(dst), result="error",
                               error="destination exists with different size",
                               error_class="FileExistsError")
            return False

        # Ensure destination directory exists
        if not dst.parent.exists():
            if not self.mkdir(dst.parent, title_id=title_id, stage=stage):
                return False

        try:
            size = os.path.getsize(long(src))
        except OSError:
            size = 0
        drive_same = same_drive(src, dst)

        if self.dry_run:
            self.journal.write(title_id=title_id, stage=stage,
                               op="rename" if drive_same else "copy",
                               from_=str(src), to=str(dst), size=size,
                               drive_same=drive_same, result="dry-run")
            if not drive_same:
                self.journal.write(title_id=title_id, stage=stage, op="delete",
                                   from_=str(src), result="dry-run")
            return True

        try:
            if drive_same:
                os.rename(long(src), long(dst))
                self.journal.write(title_id=title_id, stage=stage, op="rename",
                                   from_=str(src), to=str(dst), size=size,
                                   drive_same=True, result="ok")
                return True
            # Cross-drive: copy+verify+delete
            shutil.copy2(long(src), long(dst))
            self.journal.write(title_id=title_id, stage=stage, op="copy",
                               from_=str(src), to=str(dst), size=size,
                               drive_same=False, result="ok")
            if self.verify_hashes:
                src_h = hash_file(src, self.hash_algo)
                dst_h = hash_file(dst, self.hash_algo)
                self.journal.write(title_id=title_id, stage=stage, op="verify",
                                   from_=str(src), to=str(dst),
                                   src_hash=src_h, dst_hash=dst_h,
                                   algo=self.hash_algo,
                                   result="ok" if src_h == dst_h else "error",
                                   **({} if src_h == dst_h
                                      else {"error": "hash mismatch",
                                            "error_class": "IntegrityError"}))
                if src_h != dst_h:
                    return False
            os.remove(long(src))
            self.journal.write(title_id=title_id, stage=stage, op="delete",
                               from_=str(src), result="ok")
            return True
        except OSError as e:
            self.journal.write(title_id=title_id, stage=stage, op=op_label,
                               from_=str(src), to=str(dst), result="error",
                               error=str(e), error_class=type(e).__name__)
            return False

    # -- write_file --------------------------------------------------------

    def write_file(self, path: Path, content: bytes, *,
                   title_id: Optional[int] = None, stage: str = "") -> bool:
        if path.exists():
            try:
                existing = open(long(path), "rb").read()
                if existing == content:
                    self.journal.write(title_id=title_id, stage=stage,
                                       op="write_file", to=str(path),
                                       result="skipped",
                                       reason="content identical")
                    return True
            except OSError:
                pass
        if self.dry_run:
            self.journal.write(title_id=title_id, stage=stage, op="write_file",
                               to=str(path), size=len(content), result="dry-run")
            return True
        try:
            if not path.parent.exists():
                os.makedirs(long(path.parent), exist_ok=True)
            with open(long(path), "wb") as f:
                f.write(content)
            self.journal.write(title_id=title_id, stage=stage, op="write_file",
                               to=str(path), size=len(content), result="ok")
            return True
        except OSError as e:
            self.journal.write(title_id=title_id, stage=stage, op="write_file",
                               to=str(path), result="error",
                               error=str(e), error_class=type(e).__name__)
            return False

    # -- backup (rename foo.nfo to foo.nfo.bak) ---------------------------

    def backup(self, path: Path, *, title_id: Optional[int] = None,
               stage: str = "") -> Optional[Path]:
        bak = path.with_suffix(path.suffix + ".bak")
        # If a .bak already exists, append a timestamp
        if bak.exists():
            ts = time.strftime("%Y%m%d_%H%M%S")
            bak = path.with_name(path.name + f".{ts}.bak")
        if self.dry_run:
            self.journal.write(title_id=title_id, stage=stage, op="backup",
                               from_=str(path), to=str(bak), result="dry-run")
            return bak
        try:
            os.rename(long(path), long(bak))
            self.journal.write(title_id=title_id, stage=stage, op="backup",
                               from_=str(path), to=str(bak), result="ok")
            return bak
        except OSError as e:
            self.journal.write(title_id=title_id, stage=stage, op="backup",
                               from_=str(path), to=str(bak), result="error",
                               error=str(e), error_class=type(e).__name__)
            return None


def hash_file(path: Path, algo: str = "blake3", chunk_size: int = 1 << 20) -> str:
    """Compute a hex digest for path. Falls back to sha256 if blake3 unavailable."""
    if algo == "blake3" and _HAS_BLAKE3:
        h = blake3.blake3()
    elif algo == "blake3":
        h = hashlib.sha256()
    else:
        h = hashlib.new(algo)
    with open(long(path), "rb") as f:
        while True:
            buf = f.read(chunk_size)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


# =========================================================================
# TMDB rich data fetcher (uses phase 1's SQLite cache)
# =========================================================================

class TmdbRichFetcher:
    """Pulls full movie details from TMDB for NFO writes. Caches in SQLite
    alongside the phase 1 cache (reuses the same DB if present)."""

    BASE = "https://api.themoviedb.org/3"

    def __init__(self, api_key: str, mediaprep: Path, language: str = "en-US"):
        self.api_key = api_key
        self.lang = language
        self.cache_path = mediaprep / "tmdb_cache.sqlite"
        self.mediaprep = mediaprep
        self._db: Optional[sqlite3.Connection] = None
        self._init_db()

    def _init_db(self) -> None:
        self._db = sqlite3.connect(str(self.cache_path))
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS rich_movie (
                tmdb_id INTEGER PRIMARY KEY,
                language TEXT,
                json TEXT,
                fetched_at TEXT
            )
        """)
        self._db.commit()

    def get(self, tmdb_id: int) -> Optional[Dict[str, Any]]:
        if not tmdb_id:
            return None
        # Cache lookup
        cur = self._db.execute(
            "SELECT json FROM rich_movie WHERE tmdb_id=? AND language=?",
            (tmdb_id, self.lang),
        )
        row = cur.fetchone()
        if row:
            try:
                return json.loads(row[0])
            except json.JSONDecodeError:
                pass
        if not _HAS_REQUESTS or not self.api_key:
            log.warning("TMDB rich fetch skipped (no requests lib or no API key) for %s", tmdb_id)
            return None
        # Live fetch
        url = f"{self.BASE}/movie/{tmdb_id}"
        params = {"api_key": self.api_key, "language": self.lang,
                  "append_to_response": "release_dates,credits"}
        for attempt in range(4):
            try:
                r = requests.get(url, params=params, timeout=30)
                if r.status_code == 429:
                    wait = int(r.headers.get("Retry-After", "5"))
                    log.info("TMDB rate limit; sleeping %ds", wait)
                    time.sleep(wait)
                    continue
                if r.status_code == 404:
                    return None
                r.raise_for_status()
                data = r.json()
                self._db.execute(
                    "INSERT OR REPLACE INTO rich_movie (tmdb_id, language, json, fetched_at) VALUES (?,?,?,?)",
                    (tmdb_id, self.lang, json.dumps(data, ensure_ascii=False),
                     dt.datetime.now(dt.timezone.utc).isoformat()),
                )
                self._db.commit()
                return data
            except requests.RequestException as e:
                log.warning("TMDB fetch error for %s (attempt %d): %s",
                            tmdb_id, attempt + 1, e)
                time.sleep(2 ** attempt)
        return None

    def close(self) -> None:
        if self._db is not None:
            try:
                self._db.close()
            except Exception:
                pass


# =========================================================================
# NFO writer
# =========================================================================

def _us_certification(release_dates: Dict[str, Any]) -> str:
    if not release_dates:
        return ""
    for entry in release_dates.get("results", []):
        if entry.get("iso_3166_1") == "US":
            for rd in entry.get("release_dates", []):
                cert = (rd.get("certification") or "").strip()
                if cert:
                    return cert
    return ""


def build_nfo_xml(plan_row: PlanRow, rich: Optional[Dict[str, Any]],
                  richness: str = "rich") -> bytes:
    """Build a Kodi-compatible movie NFO. Rich mode embeds plot, cast, etc."""
    movie = ET.Element("movie")

    def add(tag: str, text: Any) -> None:
        if text is None or text == "":
            return
        e = ET.SubElement(movie, tag)
        e.text = str(text)

    # Always present
    add("title", plan_row.matched_title)
    add("year", plan_row.matched_year)
    if plan_row.runtime_tmdb_min:
        add("runtime", plan_row.runtime_tmdb_min)
    elif plan_row.runtime_file_min:
        add("runtime", plan_row.runtime_file_min)
    add("tmdbid", plan_row.tmdb_id)
    if plan_row.imdb_id:
        add("imdbid", plan_row.imdb_id)
        # Kodi historically uses "id" too
        add("id", plan_row.imdb_id)
    if plan_row.collection_name:
        coll = ET.SubElement(movie, "set")
        ET.SubElement(coll, "name").text = plan_row.collection_name
        if rich:
            bc = rich.get("belongs_to_collection") or {}
            if bc.get("overview"):
                ET.SubElement(coll, "overview").text = bc["overview"]

    if richness == "rich" and rich:
        add("originaltitle", rich.get("original_title"))
        add("plot", rich.get("overview"))
        add("tagline", rich.get("tagline"))
        if rich.get("release_date"):
            add("premiered", rich["release_date"])
        # Genres
        for g in (rich.get("genres") or []):
            ge = ET.SubElement(movie, "genre")
            ge.text = g.get("name", "")
        # Studios
        for s in (rich.get("production_companies") or []):
            st = ET.SubElement(movie, "studio")
            st.text = s.get("name", "")
        # Countries
        for c in (rich.get("production_countries") or []):
            co = ET.SubElement(movie, "country")
            co.text = c.get("name", "")
        # Ratings
        if rich.get("vote_average"):
            add("rating", f"{float(rich['vote_average']):.1f}")
        if rich.get("vote_count"):
            add("votes", str(rich["vote_count"]))
        # MPAA / certification
        cert = _us_certification(rich.get("release_dates") or {})
        if cert:
            add("mpaa", cert)
        # Cast (top 10)
        credits = rich.get("credits") or {}
        for actor in (credits.get("cast") or [])[:10]:
            a = ET.SubElement(movie, "actor")
            ET.SubElement(a, "name").text = actor.get("name", "")
            ET.SubElement(a, "role").text = actor.get("character", "")
            if actor.get("order") is not None:
                ET.SubElement(a, "order").text = str(actor["order"])
        # Directors
        for crew in (credits.get("crew") or []):
            if crew.get("job") == "Director":
                add("director", crew.get("name", ""))

    # Pretty-print
    try:
        ET.indent(movie, space="  ", level=0)
    except AttributeError:
        # Python < 3.9 (we target 3.13, but defensive)
        pass
    out = io.BytesIO()
    out.write(b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n')
    ET.ElementTree(movie).write(out, encoding="utf-8", xml_declaration=False)
    return out.getvalue()


# =========================================================================
# Sidecar matching
# =========================================================================

def _normalize_stem_for_match(stem: str) -> str:
    """Lowercase, strip release tags and non-alnum, for fuzzy stem comparison."""
    s = stem.lower()
    # Replace dots/underscores with spaces
    s = re.sub(r"[._]+", " ", s)
    # Strip release tags
    for tok in _STEM_NORMALIZE_TOKENS:
        s = re.sub(r"\b" + re.escape(tok) + r"\b", " ", s)
    # Remove non-alnum
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s


def stem_matches(file_stem: str, video_stem: str) -> bool:
    """True if file_stem refers to the same video as video_stem."""
    fs = file_stem.lower()
    vs = video_stem.lower()
    if fs == vs:
        return True
    # File like "movie.en.srt" → "movie" prefix matches "movie"
    if fs.startswith(vs + "."):
        return True
    # Suffixed images: "<videoStem>-poster.jpg" etc.
    for suf in SPECIAL_IMAGE_SUFFIXES:
        if fs == vs + suf or fs.startswith(vs + suf + "."):
            return True
    # Normalized stems
    nfs = _normalize_stem_for_match(file_stem)
    nvs = _normalize_stem_for_match(video_stem)
    if nfs and nvs and (nfs == nvs):
        return True
    return False


def is_special_image(name: str) -> bool:
    return name.lower() in SPECIAL_IMAGE_NAMES


def classify_sidecar(file_path: Path, main_video: Path) -> str:
    """Return one of: 'main_video', 'subtitle', 'nfo', 'image', 'extra_video',
    'misc', 'orphan'."""
    name = file_path.name
    ext = file_path.suffix.lower()
    if file_path == main_video:
        return "main_video"
    main_stem = main_video.stem
    matches = stem_matches(file_path.stem, main_stem)
    if ext in NFO_EXTS:
        return "nfo" if matches else "nfo_orphan"
    if ext in SUBTITLE_EXTS:
        return "subtitle" if matches else "subtitle_orphan"
    if ext in IMAGE_EXTS:
        if is_special_image(name) or matches:
            return "image"
        return "image_orphan"
    if ext in VIDEO_EXTS:
        return "extra_video"
    if ext in MISC_EXTS:
        return "misc" if matches else "misc_orphan"
    # Unknown extension; treat as orphan if it doesn't match
    return "misc" if matches else "orphan"


def derive_sidecar_dest_name(src_name: str, src_video_stem: str,
                             new_video_stem: str) -> str:
    """Compute destination filename for a sidecar given the new video stem.
    Preserves language suffixes on subtitles (e.g. .en.srt)."""
    name = src_name
    # Special images keep their conventional name
    if is_special_image(name):
        return name.lower()
    # Image suffixes: keep the suffix
    base, dot, ext = name.rpartition(".")
    if not dot:
        return name
    stem = base
    sl = stem.lower()
    vsl = src_video_stem.lower()
    # Suffixed image like "stem-poster"
    for suf in SPECIAL_IMAGE_SUFFIXES:
        if sl == vsl + suf:
            return f"{new_video_stem}{suf}.{ext}"
        if sl.startswith(vsl + suf + "."):
            tail = stem[len(vsl + suf):]  # starts with "."
            return f"{new_video_stem}{suf}{tail}.{ext}"
    # Subtitle/NFO with optional language tag(s) after the stem
    if sl == vsl:
        return f"{new_video_stem}.{ext}"
    if sl.startswith(vsl + "."):
        tail = stem[len(vsl):]  # starts with "."
        return f"{new_video_stem}{tail}.{ext}"
    # Fallback: if stems normalize-match but don't prefix-match (different release tags),
    # rebase to new stem with original extension.
    if _normalize_stem_for_match(stem) == _normalize_stem_for_match(src_video_stem):
        return f"{new_video_stem}.{ext}"
    return name


# =========================================================================
# Pre-flight checks
# =========================================================================

@dataclass
class PreflightError:
    title_id: Optional[int]
    severity: str  # "error" or "warning"
    code: str
    message: str
    path: str = ""


def preflight(plan: List[PlanRow],
              inv: Dict[int, List[InventoryFile]],
              dups: List[DupGroup],
              cfg: Config,
              plan_by_id: Optional[Dict[int, PlanRow]] = None
              ) -> Tuple[List[PreflightError], List[PreflightError]]:
    """Returns (errors, warnings). Errors abort; warnings print and continue."""
    errors: List[PreflightError] = []
    warnings: List[PreflightError] = []

    auto_rows = [r for r in plan if r.is_auto() and not r.executed_at]
    log.info("Pre-flight: %d AUTO rows pending (%d total in plan)",
             len(auto_rows), len(plan))

    # 0. Consistency check between plan.csv and duplicates.csv.
    # These are WARNINGS, not errors. Pass 2 will skip any dup-group entry
    # whose plan.csv state disagrees with the dup csv — the user is allowed
    # to clear `duplicate_role` to promote a former loser to a regular
    # keeper, or to delete a row entirely during review.
    if plan_by_id is not None:
        skipped_dups = 0
        for dg in dups:
            keeper = plan_by_id.get(dg.keeper_title_id)
            if keeper is None:
                warnings.append(PreflightError(dg.keeper_title_id, "warning",
                                               "dup_keeper_not_in_plan",
                                               f"duplicates.csv keeper {dg.keeper_title_id} "
                                               f"(group {dg.group_id}) is not in plan.csv "
                                               f"— group will be skipped"))
                continue
            if keeper.duplicate_role and keeper.duplicate_role != "keeper":
                warnings.append(PreflightError(dg.keeper_title_id, "warning",
                                               "dup_role_mismatch",
                                               f"duplicates.csv says title {dg.keeper_title_id} "
                                               f"is keeper of group {dg.group_id} but plan.csv "
                                               f"says duplicate_role={keeper.duplicate_role!r} "
                                               f"— group will be skipped"))
                skipped_dups += 1
            for lid in dg.loser_title_ids:
                loser = plan_by_id.get(lid)
                if loser is None:
                    warnings.append(PreflightError(lid, "warning",
                                                   "dup_loser_not_in_plan",
                                                   f"duplicates.csv loser {lid} "
                                                   f"(group {dg.group_id}) is not in plan.csv "
                                                   f"— loser will be skipped"))
                    continue
                expected = f"duplicate_of:{dg.keeper_title_id}"
                if loser.duplicate_role != expected:
                    warnings.append(PreflightError(lid, "warning",
                                                   "dup_role_mismatch",
                                                   f"duplicates.csv says title {lid} is loser-of-"
                                                   f"{dg.keeper_title_id} but plan.csv says "
                                                   f"duplicate_role={loser.duplicate_role!r} "
                                                   f"— loser will be skipped"))
                    skipped_dups += 1
        if skipped_dups:
            log.info("Pre-flight: %d dup entries flagged for skip due to plan/dups disagreement",
                     skipped_dups)

    # 1. Required fields
    for r in auto_rows:
        for fld in ("target_folder", "tmdb_id", "matched_title"):
            v = getattr(r, fld)
            if not v or (isinstance(v, Path) and str(v) in ("", ".")):
                errors.append(PreflightError(r.title_id, "error",
                                             "missing_field",
                                             f"AUTO row missing {fld}"))

    # 2. target_folder under movies_root
    movies_root_abs = cfg.movies_root.resolve()
    for r in auto_rows:
        if not r.target_folder or str(r.target_folder) in ("", "."):
            continue
        try:
            tf = r.target_folder.resolve()
            tf.relative_to(movies_root_abs)
        except (ValueError, OSError):
            errors.append(PreflightError(r.title_id, "error",
                                         "target_outside_root",
                                         f"target_folder {r.target_folder} not under movies_root",
                                         str(r.target_folder)))

    # 3. Source files exist + size matches
    for r in auto_rows:
        files = inv.get(r.title_id) or []
        if not files:
            errors.append(PreflightError(r.title_id, "error",
                                         "no_inventory_files",
                                         "no inventory rows for this title"))
            continue
        for f in files:
            try:
                if not f.abs_path.exists():
                    errors.append(PreflightError(r.title_id, "error",
                                                 "source_missing",
                                                 f"file gone since inventory: {f.abs_path}",
                                                 str(f.abs_path)))
                    continue
                actual = os.path.getsize(long(f.abs_path))
                if f.size_bytes and actual != f.size_bytes:
                    errors.append(PreflightError(r.title_id, "error",
                                                 "size_drift",
                                                 f"size changed from {f.size_bytes} to {actual}",
                                                 str(f.abs_path)))
            except OSError as e:
                errors.append(PreflightError(r.title_id, "error",
                                             "stat_failed",
                                             f"{type(e).__name__}: {e}",
                                             str(f.abs_path)))

    # 4. No two AUTO rows resolve to the same final video path.
    #    Skip duplicate-of rows: those go to H:\duplicates\ in Pass 2
    #    (the duplicates pass), not to target_folder. Their plan.csv
    #    target_folder may still match the keeper's by design — that's not
    #    a collision because they don't both go there.
    final_paths: Dict[str, int] = {}
    for r in auto_rows:
        if r.duplicate_role.startswith("duplicate_of:"):
            continue
        final_stem = render_filename_template(cfg.filename_template, {
            "title": r.matched_title,
            "year": r.matched_year,
            "imdb_id": r.imdb_id,
            "tmdb_id": r.tmdb_id,
            "matched_title_lowercase": r.matched_title.lower(),
        })
        ext = r.main_video_path.suffix.lower() if r.main_video_path.suffix else ""
        full = str(_norm_path(r.target_folder / (final_stem + ext))).lower()
        if full in final_paths:
            errors.append(PreflightError(r.title_id, "error",
                                         "duplicate_target",
                                         f"target collides with title_id {final_paths[full]}: {full}",
                                         full))
        else:
            final_paths[full] = r.title_id

    # 5. Duplicates root writable
    if not cfg.duplicates_dir.exists():
        try:
            cfg.duplicates_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            errors.append(PreflightError(None, "error",
                                         "duplicates_dir_unwritable",
                                         f"cannot create {cfg.duplicates_dir}: {e}"))
    else:
        # Try writing a tiny file to test
        try:
            test = cfg.duplicates_dir / f".execute_preflight_{int(time.time())}.tmp"
            test.write_text("ok", encoding="utf-8")
            test.unlink()
        except OSError as e:
            errors.append(PreflightError(None, "error",
                                         "duplicates_dir_unwritable",
                                         f"{cfg.duplicates_dir}: {e}"))

    # 6. Cross-drive moves: warn and tally bytes.
    #    Only count rows that have a valid target_folder under movies_root —
    #    rows with empty/bad target_folder will have already been rejected
    #    by checks #1/#2 above.
    cross_drive_bytes = 0
    for r in auto_rows:
        if not r.target_folder or str(r.target_folder) in ("", "."):
            continue
        files = inv.get(r.title_id) or []
        for f in files:
            if not same_drive(f.abs_path, r.target_folder):
                cross_drive_bytes += f.size_bytes
    if cross_drive_bytes > 0:
        gb = cross_drive_bytes / (1024 ** 3)
        warnings.append(PreflightError(None, "warning",
                                       "cross_drive_volume",
                                       f"{gb:.2f} GB will be cross-drive copied"))

    # 7. Free space check (only meaningful for cross-drive)
    if cross_drive_bytes > 0:
        per_drive: Dict[str, int] = {}
        for r in auto_rows:
            if not r.target_folder or str(r.target_folder) in ("", "."):
                continue
            files = inv.get(r.title_id) or []
            for f in files:
                if not same_drive(f.abs_path, r.target_folder):
                    drive = os.path.splitdrive(str(r.target_folder))[0].upper()
                    if not drive:
                        continue  # no usable drive letter; row already an error
                    per_drive[drive] = per_drive.get(drive, 0) + f.size_bytes
        for drive, needed in per_drive.items():
            try:
                free = shutil.disk_usage(drive + "\\").free
                if free < needed * 1.05:
                    errors.append(PreflightError(None, "error",
                                                 "insufficient_space",
                                                 f"drive {drive}: need {needed/1e9:.2f}GB, have {free/1e9:.2f}GB"))
            except OSError as e:
                warnings.append(PreflightError(None, "warning",
                                               "disk_usage_check_failed",
                                               f"drive {drive}: {e}"))

    # 8. Long-path warning
    for r in auto_rows:
        # Estimate final path length
        if r.target_folder and r.matched_title:
            final_stem = render_filename_template(cfg.filename_template, {
                "title": r.matched_title,
                "year": r.matched_year,
                "imdb_id": r.imdb_id,
                "tmdb_id": r.tmdb_id,
                "matched_title_lowercase": r.matched_title.lower(),
            })
            ext = r.main_video_path.suffix.lower() if r.main_video_path.suffix else ""
            full = str(_norm_path(r.target_folder / (final_stem + ext)))
            if len(full) > 240:
                warnings.append(PreflightError(r.title_id, "warning",
                                               "long_path",
                                               f"final path is {len(full)} chars (long-path prefix required)",
                                               full))

    return errors, warnings


def write_preflight_report(path: Path, errors: List[PreflightError],
                           warnings: List[PreflightError]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["severity", "title_id", "code", "message", "path"])
        for e in errors + warnings:
            w.writerow([e.severity, e.title_id or "", e.code, e.message, e.path])


# =========================================================================
# Per-title processing flow
# =========================================================================

@dataclass
class TitleResult:
    title_id: int
    success: bool
    stages_done: List[str] = field(default_factory=list)
    error: str = ""
    error_stage: str = ""


def process_title(plan_row: PlanRow,
                  inv_files: List[InventoryFile],
                  cfg: Config,
                  ops: DiskOps,
                  rich_fetcher: Optional[TmdbRichFetcher],
                  large_extras_log: List[Dict[str, Any]],
                  orphans_log: List[Dict[str, Any]]) -> TitleResult:
    """Run all stages A-G for a single AUTO title."""
    tid = plan_row.title_id
    result = TitleResult(title_id=tid, success=False)

    # ---- Stage A: classify -------------------------------------------------
    main_video = plan_row.main_video_path
    if not main_video or not main_video.exists():
        # Try to find the main_video in inv_files
        candidates = [f for f in inv_files if f.role == "main_video"]
        if candidates:
            main_video = candidates[0].abs_path
        else:
            result.error = "main_video missing on disk and not in inventory"
            result.error_stage = "A"
            return result

    classifications: List[Tuple[InventoryFile, str]] = []
    orphans: List[InventoryFile] = []
    extras: List[InventoryFile] = []
    sidecars_video: List[InventoryFile] = []  # sidecars matching main video stem
    nfo_files: List[InventoryFile] = []

    for f in inv_files:
        if f.abs_path == main_video:
            classifications.append((f, "main_video"))
            continue
        c = classify_sidecar(f.abs_path, main_video)
        classifications.append((f, c))
        if c == "extra_video":
            extras.append(f)
        elif c == "nfo":
            nfo_files.append(f)
            sidecars_video.append(f)
        elif c in ("subtitle", "image", "misc"):
            sidecars_video.append(f)
        elif c.endswith("_orphan") or c == "orphan":
            orphans.append(f)
    result.stages_done.append("A")

    # ---- Stage B: ensure target folder ------------------------------------
    if not ops.mkdir(plan_row.target_folder, title_id=tid, stage="B"):
        result.error = "could not create target folder"
        result.error_stage = "B"
        return result
    # Ensure parent (franchise) folder too — mkdir is idempotent
    result.stages_done.append("B")

    # Compute the new stem for the main video & derived sidecars
    new_stem = render_filename_template(cfg.filename_template, {
        "title": plan_row.matched_title,
        "year": plan_row.matched_year,
        "imdb_id": plan_row.imdb_id,
        "tmdb_id": plan_row.tmdb_id,
        "matched_title_lowercase": plan_row.matched_title.lower(),
    })
    main_ext = main_video.suffix.lower()
    new_video_path = plan_row.target_folder / (new_stem + main_ext)

    # ---- Stage C: move main video -----------------------------------------
    if not ops.move(main_video, new_video_path, title_id=tid, stage="C",
                    op_label="rename" if same_drive(main_video, new_video_path)
                                       else "move"):
        result.error = "main video move failed"
        result.error_stage = "C"
        return result
    result.stages_done.append("C")

    # ---- Stage D: move sidecars -------------------------------------------
    src_video_stem = main_video.stem
    moved_nfo_path: Optional[Path] = None
    seen_dest_names: Set[str] = set([new_video_path.name.lower()])
    for sc in sidecars_video:
        if sc.abs_path == main_video:
            continue
        dest_name = derive_sidecar_dest_name(sc.abs_path.name,
                                             src_video_stem, new_stem)
        dest_name = sanitize_for_windows(dest_name)
        # If we already have a sidecar with this destination name (e.g., two
        # subs in the same language), suffix with .2, .3 etc.
        canonical = dest_name.lower()
        if canonical in seen_dest_names:
            base, _, ext = dest_name.rpartition(".")
            i = 2
            while True:
                cand = f"{base}.{i}.{ext}"
                if cand.lower() not in seen_dest_names:
                    dest_name = cand
                    break
                i += 1
        seen_dest_names.add(dest_name.lower())
        dest_path = plan_row.target_folder / dest_name
        if ops.move(sc.abs_path, dest_path, title_id=tid, stage="D",
                    op_label="rename" if same_drive(sc.abs_path, dest_path)
                                       else "move"):
            if sc.ext == ".nfo":
                moved_nfo_path = dest_path
        else:
            log.warning("title %s: sidecar move failed for %s", tid, sc.abs_path)
    result.stages_done.append("D")

    # ---- Stage E: write NFO ------------------------------------------------
    rich_data = None
    if cfg.nfo_richness == "rich" and rich_fetcher and plan_row.tmdb_id:
        try:
            rich_data = rich_fetcher.get(int(plan_row.tmdb_id))
        except (ValueError, sqlite3.Error) as e:
            log.warning("title %s: rich TMDB fetch failed: %s", tid, e)
    nfo_bytes = build_nfo_xml(plan_row, rich_data, cfg.nfo_richness)
    target_nfo = plan_row.target_folder / (new_stem + ".nfo")
    if target_nfo.exists():
        # Compare semantically (same tmdbid/imdbid/title/year)
        try:
            existing = open(long(target_nfo), "rb").read()
            if _nfo_semantically_equal(existing, nfo_bytes):
                log.info("title %s: existing NFO equivalent; leaving in place", tid)
            else:
                bak = ops.backup(target_nfo, title_id=tid, stage="E")
                if bak is None:
                    result.error = "NFO backup failed"
                    result.error_stage = "E"
                    return result
                if not ops.write_file(target_nfo, nfo_bytes,
                                      title_id=tid, stage="E"):
                    result.error = "NFO write failed"
                    result.error_stage = "E"
                    return result
        except OSError as e:
            log.warning("title %s: could not read existing NFO: %s", tid, e)
            if not ops.write_file(target_nfo, nfo_bytes,
                                  title_id=tid, stage="E"):
                result.error = "NFO write failed"
                result.error_stage = "E"
                return result
    else:
        if not ops.write_file(target_nfo, nfo_bytes, title_id=tid, stage="E"):
            result.error = "NFO write failed"
            result.error_stage = "E"
            return result
    result.stages_done.append("E")

    # ---- Stage F: extras ---------------------------------------------------
    if extras:
        extras_dir = plan_row.target_folder / cfg.extras_subfolder
        if not ops.mkdir(extras_dir, title_id=tid, stage="F"):
            log.warning("title %s: could not create Extras dir; skipping extras", tid)
        else:
            threshold_bytes = cfg.large_extra_threshold_mb * 1024 * 1024
            for ex in extras:
                ex_name = sanitize_for_windows(ex.abs_path.name)
                ex_dest = extras_dir / ex_name
                ok = ops.move(ex.abs_path, ex_dest, title_id=tid, stage="F",
                              op_label="rename" if same_drive(ex.abs_path, ex_dest)
                                                 else "move")
                if ok and ex.size_bytes >= threshold_bytes:
                    large_extras_log.append({
                        "title_id": tid,
                        "matched_title": plan_row.matched_title,
                        "tmdb_id": plan_row.tmdb_id,
                        "extra_path": str(ex_dest),
                        "size_mb": round(ex.size_bytes / (1024 * 1024), 1),
                    })
    result.stages_done.append("F")

    # ---- Stage G: cleanup source folder -----------------------------------
    if cfg.rmdir_empty_sources:
        src_folder = plan_row.source_folder
        # Only attempt rmdir if no orphans left and folder isn't movies_root
        if (src_folder.exists() and src_folder != cfg.movies_root
                and src_folder != plan_row.target_folder):
            if orphans:
                # Log orphans, leave folder
                for o in orphans:
                    orphans_log.append({
                        "title_id": tid,
                        "matched_title": plan_row.matched_title,
                        "source_folder": str(src_folder),
                        "orphan_file": str(o.abs_path),
                        "orphan_size": o.size_bytes,
                        "classification": classify_sidecar(o.abs_path, main_video),
                    })
            else:
                ops.rmdir(src_folder, title_id=tid, stage="G")
    else:
        # Still log orphans even if not rmdir-ing
        if orphans:
            for o in orphans:
                orphans_log.append({
                    "title_id": tid,
                    "matched_title": plan_row.matched_title,
                    "source_folder": str(plan_row.source_folder),
                    "orphan_file": str(o.abs_path),
                    "orphan_size": o.size_bytes,
                    "classification": classify_sidecar(o.abs_path, main_video),
                })
    result.stages_done.append("G")

    result.success = True
    return result


def _nfo_semantically_equal(a: bytes, b: bytes) -> bool:
    """Return True if both NFOs encode the same tmdbid/imdbid/title/year."""
    def keys(blob: bytes) -> Tuple[str, str, str, str]:
        try:
            root = ET.fromstring(blob)
        except ET.ParseError:
            return ("?", "?", "?", "?")
        def t(tag: str) -> str:
            e = root.find(tag)
            return (e.text or "").strip() if e is not None else ""
        return (t("tmdbid"), t("imdbid"), t("title"), t("year"))
    return keys(a) == keys(b)


# =========================================================================
# Duplicates pass
# =========================================================================

def process_duplicates(dups: List[DupGroup],
                       inv: Dict[int, List[InventoryFile]],
                       plan_by_id: Dict[int, PlanRow],
                       cfg: Config,
                       ops: DiskOps) -> Dict[int, bool]:
    """Move all duplicate-loser files under <duplicates_dir>\\<rel-from-movies-root>\\.
    Returns map of title_id -> success.

    Defensive: skip any dup entry where plan.csv and duplicates.csv
    disagree on the title's role. The pre-flight consistency check has
    already warned the user about these.
    """
    results: Dict[int, bool] = {}
    movies_root = cfg.movies_root.resolve()
    for dg in dups:
        # Skip whole group if keeper isn't in plan or has the wrong role.
        keeper_plan = plan_by_id.get(dg.keeper_title_id)
        if keeper_plan is None:
            continue
        if keeper_plan.duplicate_role and keeper_plan.duplicate_role != "keeper":
            continue
        for loser_id in dg.loser_title_ids:
            loser_plan = plan_by_id.get(loser_id)
            # Skip losers that aren't in plan, or whose plan disagrees with the
            # dup csv (most often: user cleared duplicate_role to promote the
            # row to a regular keeper).
            if loser_plan is None:
                continue
            expected = f"duplicate_of:{dg.keeper_title_id}"
            if loser_plan.duplicate_role != expected:
                continue
            # Skip if user marked SKIP during phase 2 review.
            if loser_plan.action.upper() == "SKIP":
                continue
            files = inv.get(loser_id) or []
            if not files:
                results[loser_id] = True
                continue
            if loser_plan.executed_at:
                results[loser_id] = True
                continue
            ok_all = True
            for f in files:
                # Compute path under duplicates_dir mirroring source
                try:
                    rel = f.abs_path.resolve().relative_to(movies_root)
                except ValueError:
                    rel = Path(f.abs_path.name)
                dest = cfg.duplicates_dir / rel
                if not ops.move(f.abs_path, dest, title_id=loser_id,
                                stage="DUP", op_label="dup_move"):
                    ok_all = False
            # Also try to rmdir the source folder
            if loser_plan and loser_plan.source_folder.exists():
                ops.rmdir(loser_plan.source_folder, title_id=loser_id,
                          stage="DUP")
            results[loser_id] = ok_all
            if loser_plan and ok_all:
                loser_plan.executed_at = dt.datetime.now(dt.timezone.utc)\
                    .isoformat(timespec="seconds")
    return results


# =========================================================================
# CSV writers (orphans, large_extras, errors, summary)
# =========================================================================

def write_aux_csv(path: Path, rows: List[Dict[str, Any]],
                  headers: List[str]) -> None:
    if not rows:
        return
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
        for r in rows:
            w.writerow(r)


# =========================================================================
# Status subcommand
# =========================================================================

def cmd_status(cfg: Config) -> int:
    plan_path = cfg.mediaprep_dir / "plan.csv"
    if not plan_path.exists():
        print(f"plan.csv not found at {plan_path}")
        return 3
    plan = read_plan(plan_path, cfg.movies_root)
    counts = {"AUTO_pending": 0, "AUTO_done": 0, "REVIEW": 0, "SKIP": 0, "OTHER": 0}
    for r in plan:
        a = r.action.upper()
        if a == "AUTO":
            if r.executed_at:
                counts["AUTO_done"] += 1
            else:
                counts["AUTO_pending"] += 1
        elif a == "REVIEW" or not a:
            counts["REVIEW"] += 1
        elif a == "SKIP":
            counts["SKIP"] += 1
        else:
            counts["OTHER"] += 1
    print(f"Total titles: {len(plan)}")
    for k, v in counts.items():
        print(f"  {k}: {v}")
    err_path = cfg.mediaprep_dir / "execute_errors.csv"
    if err_path.exists():
        n = sum(1 for _ in open(err_path, "r", encoding="utf-8")) - 1
        print(f"  execute_errors.csv rows: {max(0, n)}")
    return 0


# =========================================================================
# Main run
# =========================================================================

def cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    setup_logging(cfg.mediaprep_dir, run_id)

    apply_mode = bool(args.apply)
    dry_run = not apply_mode
    log.info("Mode: %s", "APPLY (real run)" if apply_mode else "DRY-RUN (no files touched)")
    if not apply_mode:
        log.info("Pass --apply to perform the moves for real.")

    verify_hashes = bool(args.verify_hashes) or cfg.verify_hashes_default
    if verify_hashes:
        log.info("Hash verification: ON for cross-drive moves")

    plan_path = cfg.mediaprep_dir / "plan.csv"
    inv_path = cfg.mediaprep_dir / "inventory.csv"
    dup_path = cfg.mediaprep_dir / "duplicates.csv"

    # Read inputs
    log.info("Reading plan.csv ...")
    plan = read_plan(plan_path, cfg.movies_root)
    plan_header = list(plan[0].raw.keys()) if plan else []
    plan_by_id = {p.title_id: p for p in plan}
    log.info("Reading inventory.csv ...")
    inv_files, inv_by_parent = read_inventory(inv_path)
    log.info("Reading duplicates.csv ...")
    dups = read_duplicates(dup_path)

    # Build a sorted list of parent folder paths once for fast prefix
    # lookups in title_files (folder-based titles with subdirectories).
    sorted_parents = sorted(inv_by_parent.keys())

    # Attach inventory file lists to each plan row + resolve main_video_path
    log.info("Mapping inventory files to %d plan rows ...", len(plan))
    t0 = time.time()
    inv_by_title: Dict[int, List[InventoryFile]] = {}
    for p in plan:
        files = title_files(p, inv_by_parent, sorted_parents)
        inv_by_title[p.title_id] = files
        attach_main_video_path(p, files)
    log.info("Mapped %d titles in %.2fs", len(plan), time.time() - t0)

    log.info("Loaded: %d plan rows, %d inventory files, %d dup groups",
             len(plan), len(inv_files), len(dups))

    # Filter to AUTO rows pending
    auto_rows = [r for r in plan if r.is_auto() and not r.executed_at]
    if args.titles:
        wanted = set(int(t) for t in args.titles.split(",") if t.strip())
        auto_rows = [r for r in auto_rows if r.title_id in wanted]
        log.info("Filtered to %d titles by --titles", len(auto_rows))
    if args.limit:
        auto_rows = auto_rows[:args.limit]
        log.info("Limited to %d titles by --limit", len(auto_rows))

    # Pre-flight
    if not args.skip_preflight:
        log.info("Running pre-flight ...")
        errors, warnings = preflight(auto_rows, inv_by_title, dups, cfg,
                                     plan_by_id=plan_by_id)
        if warnings:
            log.warning("Pre-flight warnings: %d", len(warnings))
            for w in warnings[:20]:
                log.warning("  [%s] title=%s: %s", w.code, w.title_id, w.message)
        if errors:
            log.error("Pre-flight ERRORS: %d (aborting; see preflight_errors.csv)",
                      len(errors))
            for e in errors[:20]:
                log.error("  [%s] title=%s: %s", e.code, e.title_id, e.message)
            write_preflight_report(cfg.mediaprep_dir / "preflight_errors.csv",
                                   errors, warnings)
            return 2
        if warnings:
            write_preflight_report(cfg.mediaprep_dir / "preflight_warnings.csv",
                                   [], warnings)
        log.info("Pre-flight OK.")
    else:
        log.warning("Pre-flight SKIPPED (--skip-preflight)")

    # Open journal
    journal_name = "moves_dryrun.jsonl" if dry_run else "moves.jsonl"
    journal = Journal(cfg.mediaprep_dir / journal_name, run_id, dry_run)
    journal.write(op="run_start", mode="dry-run" if dry_run else "apply",
                  version=VERSION, total_auto=len(auto_rows),
                  result="ok")

    ops = DiskOps(journal, dry_run, verify_hashes, cfg.hash_algo)
    rich_fetcher = (TmdbRichFetcher(cfg.tmdb_api_key, cfg.mediaprep_dir, cfg.language)
                    if cfg.nfo_richness == "rich" else None)

    large_extras_log: List[Dict[str, Any]] = []
    orphans_log: List[Dict[str, Any]] = []
    errors_log: List[Dict[str, Any]] = []

    # Pass 2: duplicates
    if not args.skip_duplicates:
        log.info("Pass 2: relocating %d duplicate groups ...", len(dups))
        process_duplicates(dups, inv_by_title, plan_by_id, cfg, ops)
    else:
        log.warning("Skipping duplicates pass (--skip-duplicates)")

    # Pass 3: per-title flow
    log.info("Pass 3: processing %d AUTO titles ...", len(auto_rows))
    succeeded = 0
    failed = 0
    completed_results: List[TitleResult] = []
    plan_dirty = False

    try:
        for i, r in enumerate(auto_rows, 1):
            # Pass 2 (duplicates) may have set executed_at on duplicate-of
            # rows. Skip them silently — their files were moved to
            # duplicates_dir, not to target_folder, so re-processing here
            # would fail at Stage C with "source missing".
            if r.executed_at:
                continue
            inv_files = inv_by_title.get(r.title_id) or []
            if not inv_files:
                log.warning("title %s: no inventory rows; skipping", r.title_id)
                errors_log.append({"title_id": r.title_id, "stage": "A",
                                   "error": "no inventory rows",
                                   "matched_title": r.matched_title})
                failed += 1
                continue
            log.info("[%d/%d] title %s: %s (%s)", i, len(auto_rows),
                     r.title_id, r.matched_title, r.matched_year)
            try:
                result = process_title(r, inv_files, cfg, ops, rich_fetcher,
                                       large_extras_log, orphans_log)
            except Exception as e:
                log.exception("title %s: unhandled exception", r.title_id)
                errors_log.append({"title_id": r.title_id, "stage": "?",
                                   "error": f"{type(e).__name__}: {e}",
                                   "matched_title": r.matched_title})
                failed += 1
                continue
            completed_results.append(result)
            if result.success:
                succeeded += 1
                if not dry_run:
                    r.executed_at = dt.datetime.now(dt.timezone.utc)\
                        .isoformat(timespec="seconds")
                    plan_dirty = True
                # Batch write back every 50 successful titles
                if plan_dirty and (succeeded % 50 == 0):
                    log.info("Flushing plan.csv (%d successes so far) ...", succeeded)
                    write_plan(plan_path, plan_header, plan)
                    plan_dirty = False
            else:
                failed += 1
                errors_log.append({"title_id": result.title_id,
                                   "stage": result.error_stage,
                                   "error": result.error,
                                   "matched_title": r.matched_title})
                log.error("title %s: FAILED at stage %s: %s",
                          result.title_id, result.error_stage, result.error)

    except KeyboardInterrupt:
        log.warning("Interrupted by user. Flushing plan.csv ...")
        if plan_dirty and not dry_run:
            write_plan(plan_path, plan_header, plan)
        journal.write(op="run_end", result="interrupted",
                      succeeded=succeeded, failed=failed)
        journal.close()
        if rich_fetcher:
            rich_fetcher.close()
        return 130

    # Final flush
    if plan_dirty and not dry_run:
        log.info("Flushing plan.csv ...")
        write_plan(plan_path, plan_header, plan)

    # Aux CSVs
    if large_extras_log:
        write_aux_csv(cfg.mediaprep_dir / "large_extras.csv", large_extras_log,
                      ["title_id", "matched_title", "tmdb_id", "extra_path", "size_mb"])
        log.info("Wrote large_extras.csv (%d entries)", len(large_extras_log))
    if orphans_log:
        write_aux_csv(cfg.mediaprep_dir / "orphans.csv", orphans_log,
                      ["title_id", "matched_title", "source_folder",
                       "orphan_file", "orphan_size", "classification"])
        log.info("Wrote orphans.csv (%d entries)", len(orphans_log))
    if errors_log:
        write_aux_csv(cfg.mediaprep_dir / "execute_errors.csv", errors_log,
                      ["title_id", "stage", "error", "matched_title"])
        log.info("Wrote execute_errors.csv (%d entries)", len(errors_log))

    # Summary
    log.info("Summary: %d succeeded, %d failed (mode=%s)",
             succeeded, failed, "dry-run" if dry_run else "apply")
    write_summary_report(cfg, run_id, dry_run, succeeded, failed,
                         completed_results, large_extras_log, orphans_log,
                         errors_log)

    journal.write(op="run_end", result="ok", succeeded=succeeded, failed=failed)
    journal.close()
    if rich_fetcher:
        rich_fetcher.close()

    if dry_run:
        log.info("Dry-run complete. Review %s\\dryrun_report.txt and run again with --apply.",
                 cfg.mediaprep_dir)
    else:
        log.info("Apply complete. Review %s\\execute_report.txt", cfg.mediaprep_dir)

    return 0 if failed == 0 else 1


def write_summary_report(cfg: Config, run_id: str, dry_run: bool,
                         succeeded: int, failed: int,
                         results: List[TitleResult],
                         large_extras: List[Dict[str, Any]],
                         orphans: List[Dict[str, Any]],
                         errors: List[Dict[str, Any]]) -> None:
    name = "dryrun_report.txt" if dry_run else "execute_report.txt"
    path = cfg.mediaprep_dir / name
    lines: List[str] = []
    lines.append(f"execute.py {VERSION} — run {run_id}")
    lines.append(f"Mode: {'DRY-RUN' if dry_run else 'APPLY'}")
    lines.append("")
    lines.append(f"Titles succeeded: {succeeded}")
    lines.append(f"Titles failed:    {failed}")
    lines.append(f"Large extras flagged: {len(large_extras)}")
    lines.append(f"Orphan files:         {len(orphans)}")
    lines.append("")
    if errors:
        lines.append("Errors:")
        for e in errors[:50]:
            lines.append(f"  title {e['title_id']} stage {e.get('stage','?')}: {e['error']}")
        if len(errors) > 50:
            lines.append(f"  ... and {len(errors)-50} more (see execute_errors.csv)")
        lines.append("")
    lines.append("Tail of journal: " + str(cfg.mediaprep_dir / ("moves_dryrun.jsonl" if dry_run else "moves.jsonl")))
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError as e:
        log.warning("Could not write %s: %s", path, e)


# =========================================================================
# CLI
# =========================================================================

def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="execute.py",
        description="Phase 3: carry out the moves planned by inventory.py.",
    )
    p.add_argument("mode", nargs="?", default="run",
                   choices=["run", "status"],
                   help="'run' (default) executes the plan; 'status' prints "
                        "plan progress and exits")
    p.add_argument("--config", default="config.json",
                   help="path to config.json (default: ./config.json)")
    p.add_argument("--apply", action="store_true",
                   help="REQUIRED to actually move files; without it, runs as dry-run")
    p.add_argument("--limit", type=int, default=0,
                   help="process only N AUTO titles")
    p.add_argument("--titles", default="",
                   help="comma-separated title_ids to process exclusively")
    p.add_argument("--skip-duplicates", action="store_true",
                   help="skip the duplicates pass")
    p.add_argument("--skip-preflight", action="store_true",
                   help="bypass pre-flight checks (use with care)")
    p.add_argument("--no-nfo", action="store_true",
                   help="skip NFO generation")
    p.add_argument("--keep-source-empty-dirs", action="store_true",
                   help="don't rmdir empty source folders")
    p.add_argument("--verify-hashes", action="store_true",
                   help="hash-verify cross-drive moves before deleting source")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        cfg = load_config(_norm_path(args.config))
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 3

    if args.no_nfo:
        cfg.nfo_richness = "off"
    if args.keep_source_empty_dirs:
        cfg.rmdir_empty_sources = False

    if args.mode == "status":
        return cmd_status(cfg)
    return cmd_run(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
