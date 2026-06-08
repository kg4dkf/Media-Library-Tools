#!/usr/bin/env python3
"""
inventory_tv.py - Phase 6 of the Media Library Reorganization pipeline.

READ-ONLY script that walks the TV library root, identifies series via TMDB,
detects single-episode-wrapper messes (the "Mr. Bean problem", §6) and
cross-folder content mismatches (§7), and emits six CSVs for human review.

Makes ZERO modifications to the TV library. All output goes to mediaprep_dir
specified in config.json.

Usage:
    py -3.13 inventory_tv.py --config config.json
    py -3.13 inventory_tv.py --config config.json --resume
    py -3.13 inventory_tv.py --config config.json --limit 50
    py -3.13 inventory_tv.py --config config.json --skip-merge-detection

Outputs (in mediaprep_dir):
    inventory_tv.csv         per-file inventory
    series_plan.csv          per-series plan (the human review file)
    episodes_plan.csv        per-episode override (mostly empty by default)
    merge_candidates.csv     reverse-grouping proposals (§6)
    mixed_content.csv        cross-folder content mismatches (§7)
    unresolved_series.csv    TMDB couldn't match
    inventory_tv_<ts>.log    log file

See phase6_design.md for the full specification.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
import traceback
import unicodedata
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

# Optional: tqdm for progress bars; degrade gracefully if not installed.
try:
    from tqdm import tqdm
except ImportError:  # pragma: no cover
    def tqdm(it, **_kw):
        return it

# Local imports — all tested in isolation.
from tv_parser import (
    VIDEO_EXTS,
    derive_series_from_filename,
    is_video_filename,
    leading_episode_number,
    normalize_folder_name,
    parse_episode,
    tokenize_for_clustering,
)
from tv_merge_clusterer import (
    DEFAULT_CONTAINMENT_THRESHOLD,
    DEFAULT_JACCARD_THRESHOLD,
    DEFAULT_MAX_GROUP_SIZE,
    DEFAULT_MIN_GROUP_SIZE,
    DEFAULT_MIN_SHARED_CHARS,
    DEFAULT_MIN_TOKEN_OVERLAP,
    MergeGroup,
    find_merge_candidates,
)
from tv_content_check import (
    DEFAULT_MIN_EPISODES_FOR_CHECK,
    DEFAULT_SIMILARITY_THRESHOLD,
    ContentCheckResult,
    check_folder_content,
)
from tv_tmdb_client import (
    TmdbTvClient,
    is_error,
    is_not_found,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIPT_VERSION = "1.0.0"

# Default skip dirs from phase6_design.md §14, plus our own output folder.
DEFAULT_TV_SKIP_DIRS: Tuple[str, ...] = (
    ".deletedByTMM", ".sonarr", ".trash", ".tmm", "_mediaprep",
)

# File-extension classifications — intentionally aligned with inventory.py for
# cross-pipeline consistency.
NFO_EXTS: Set[str] = {".nfo", ".xml"}
SUBTITLE_EXTS: Set[str] = {".srt", ".sub", ".idx", ".ass", ".ssa", ".vtt", ".sup", ".smi"}
IMAGE_EXTS: Set[str] = {".jpg", ".jpeg", ".png", ".tbn", ".webp", ".gif"}
POSTER_NAMES: Set[str] = {"poster", "cover", "folder", "default", "show", "tvshow"}
FANART_NAMES: Set[str] = {"fanart", "backdrop", "background", "art"}
BANNER_NAMES: Set[str] = {"banner"}

# Confidence thresholds (TV-specific defaults; design §17 calls for higher
# bar than movies because a wrong match propagates to 200+ episodes).
CONFIDENCE_AUTO = 75
CONFIDENCE_REVIEW = 30

# Windows-forbidden chars for sanitization (matches inventory.py exactly).
WINDOWS_RESERVED: Set[str] = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
WINDOWS_FORBIDDEN_RE = re.compile(r'[<>:"/\\|?*\x00-\x1F]')
MULTISPACE_RE = re.compile(r"\s+")

# Series folder name parsing.
RE_FOLDER_YEAR = re.compile(r"\((?:19|20)\d{2}(?:[\s\-–—]+(?:(?:19|20)\d{2}|\d{2})?)?\)")
RE_FOLDER_TMDB_ID = re.compile(r"\{tmdb-(\d+)\}", re.IGNORECASE)
RE_FOLDER_IMDB_ID = re.compile(r"\{imdb-(tt\d+)\}", re.IGNORECASE)
RE_FOLDER_FIRST_YEAR = re.compile(r"\b((?:19|20)\d{2})\b")

# Per-series CSV flush cadence — phase6_design.md §5 says "every 25 successful
# series" for execute_tv.py, but for inventory we batch a bit larger since
# we're only writing one row per series.
INVENTORY_FLUSH_EVERY_SERIES = 50

log = logging.getLogger("inventory_tv")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class Config:
    """Loaded from config.json. Phase 6 keys are documented in §14."""
    # Required
    tmdb_api_key: str
    tv_root: Path
    mediaprep_dir: Path

    # Optional with defaults
    tv_intake_dir: Optional[Path] = None
    language: str = "en-US"
    confidence_auto: int = CONFIDENCE_AUTO
    confidence_review: int = CONFIDENCE_REVIEW
    tv_series_template: str = "{title} ({year}) {{tmdb-{tmdb_id}}}"
    tv_season_template: str = "Season {season:02d} ({season_year})"
    tv_specials_folder: str = "Specials"
    tv_episode_template: str = "{title} S{season:02d}E{episode:02d} - {episode_title}"
    tv_multi_ep_separator: str = "+ "
    tv_skip_dirs: Tuple[str, ...] = DEFAULT_TV_SKIP_DIRS
    # Merge clusterer knobs (phase6_design.md §14 + tuning constants)
    tv_merge_threshold: float = DEFAULT_JACCARD_THRESHOLD
    tv_min_merge_size: int = DEFAULT_MIN_GROUP_SIZE
    tv_merge_containment: float = DEFAULT_CONTAINMENT_THRESHOLD
    tv_merge_min_shared_chars: int = DEFAULT_MIN_SHARED_CHARS
    tv_merge_min_token_overlap: int = DEFAULT_MIN_TOKEN_OVERLAP
    tv_merge_max_group_size: int = DEFAULT_MAX_GROUP_SIZE
    # Content-check knobs (§14 doesn't expose these; defaults align with
    # tv_content_check.py).
    tv_content_similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD
    tv_content_min_episodes: int = DEFAULT_MIN_EPISODES_FOR_CHECK
    # Optional: language for TMDB calls (defaults to top-level `language`).
    tmdb_tv_language: Optional[str] = None

    @classmethod
    def load(cls, config_path: Path) -> "Config":
        if not config_path.is_file():
            raise SystemExit(f"Config not found: {config_path}")
        try:
            text = config_path.read_text(encoding="utf-8")
            raw = json.loads(text)
        except json.JSONDecodeError as e:
            # Show the offending line and a 1-up/1-down context window so
            # the user can spot a missing comma / quote / brace immediately.
            lines = text.splitlines()
            lo = max(0, e.lineno - 2)
            hi = min(len(lines), e.lineno + 1)
            window = "\n".join(
                f"  {i+1:4d} {'>' if i+1 == e.lineno else ' '} {lines[i]}"
                for i in range(lo, hi)
            )
            raise SystemExit(
                f"\nJSON parse error in {config_path}:\n"
                f"  line {e.lineno} col {e.colno}: {e.msg}\n"
                f"\n{window}\n\n"
                f"Common cause: a missing comma at the end of the previous line."
            ) from e

        # Required keys.
        try:
            tmdb_api_key = raw["tmdb_api_key"]
            tv_root = Path(raw["tv_root"])
            mediaprep_dir = Path(raw["mediaprep_dir"])
        except KeyError as e:
            raise SystemExit(f"Missing required config key for inventory_tv: {e}")

        if tmdb_api_key in ("", "REPLACE_ME", "YOUR_KEY_HERE"):
            raise SystemExit("tmdb_api_key is not set in config.json")

        skip_dirs_raw = raw.get("tv_skip_dirs", DEFAULT_TV_SKIP_DIRS)
        if not isinstance(skip_dirs_raw, (list, tuple)):
            raise SystemExit("tv_skip_dirs must be a list of folder names")

        return cls(
            tmdb_api_key=tmdb_api_key,
            tv_root=tv_root,
            mediaprep_dir=mediaprep_dir,
            tv_intake_dir=Path(raw["tv_intake_dir"]) if raw.get("tv_intake_dir") else None,
            language=str(raw.get("language", "en-US")),
            confidence_auto=int(raw.get("confidence_auto", CONFIDENCE_AUTO)),
            confidence_review=int(raw.get("confidence_review", CONFIDENCE_REVIEW)),
            tv_series_template=str(raw.get("tv_series_template",
                                           cls.__dataclass_fields__["tv_series_template"].default)),
            tv_season_template=str(raw.get("tv_season_template",
                                           cls.__dataclass_fields__["tv_season_template"].default)),
            tv_specials_folder=str(raw.get("tv_specials_folder", "Specials")),
            tv_episode_template=str(raw.get("tv_episode_template",
                                            cls.__dataclass_fields__["tv_episode_template"].default)),
            tv_multi_ep_separator=str(raw.get("tv_multi_ep_separator", "+ ")),
            tv_skip_dirs=tuple(skip_dirs_raw),
            tv_merge_threshold=float(raw.get("tv_merge_threshold", DEFAULT_JACCARD_THRESHOLD)),
            tv_min_merge_size=int(raw.get("tv_min_merge_size", DEFAULT_MIN_GROUP_SIZE)),
            tv_merge_containment=float(raw.get("tv_merge_containment", DEFAULT_CONTAINMENT_THRESHOLD)),
            tv_merge_min_shared_chars=int(raw.get("tv_merge_min_shared_chars", DEFAULT_MIN_SHARED_CHARS)),
            tv_merge_min_token_overlap=int(raw.get("tv_merge_min_token_overlap", DEFAULT_MIN_TOKEN_OVERLAP)),
            tv_merge_max_group_size=int(raw.get("tv_merge_max_group_size", DEFAULT_MAX_GROUP_SIZE)),
            tv_content_similarity_threshold=float(
                raw.get("tv_content_similarity_threshold", DEFAULT_SIMILARITY_THRESHOLD)
            ),
            tv_content_min_episodes=int(
                raw.get("tv_content_min_episodes", DEFAULT_MIN_EPISODES_FOR_CHECK)
            ),
            tmdb_tv_language=raw.get("tmdb_tv_language"),
        )


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(mediaprep_dir: Path) -> Path:
    """Configure stdout + file logging. Returns the log file path."""
    mediaprep_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = mediaprep_dir / f"inventory_tv_{ts}.log"

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    file_h = logging.FileHandler(log_path, encoding="utf-8")
    file_h.setFormatter(fmt)
    file_h.setLevel(logging.DEBUG)

    console_h = logging.StreamHandler(sys.stdout)
    console_h.setFormatter(fmt)
    console_h.setLevel(logging.INFO)

    log.setLevel(logging.DEBUG)
    # Avoid double-attaching handlers when imported in tests.
    if not log.handlers:
        log.addHandler(file_h)
        log.addHandler(console_h)
    log.info("inventory_tv.py v%s starting; log file: %s", SCRIPT_VERSION, log_path)
    return log_path


# ---------------------------------------------------------------------------
# Path / sanitization utilities (intentionally mirrors inventory.py)
# ---------------------------------------------------------------------------

def win_long(p: Path) -> str:
    """Apply the Windows long-path prefix when on Windows for absolute paths."""
    s = str(p)
    if os.name == "nt" and not s.startswith("\\\\?\\"):
        if re.match(r"^[A-Za-z]:[\\/]", s) or s.startswith("\\\\"):
            if s.startswith("\\\\"):
                return "\\\\?\\UNC\\" + s[2:]
            return "\\\\?\\" + s
    return s


def sanitize_component(s: str, max_len: int = 180) -> str:
    """Sanitize a single filesystem component for Windows."""
    s = s.replace(":", " - ")
    s = s.replace("/", " ")
    s = s.replace("\\", " ")
    s = s.replace('"', "'")
    s = s.replace("|", "-")
    s = WINDOWS_FORBIDDEN_RE.sub("", s)
    s = s.replace("·", "-")
    s = s.replace("…", "...")
    s = MULTISPACE_RE.sub(" ", s).strip()
    s = s.rstrip(". ")
    if not s:
        s = "_"
    if s.upper() in WINDOWS_RESERVED:
        s = s + "_"
    if len(s) > max_len:
        s = s[:max_len].rstrip(". ")
    return s


# Leading-track-number prefix (e.g. "01 MrBean" -> drop "01 "). Conservative:
# requires a separator after the digits so we don't strip "200 Cartoons".
RE_LEADING_TRACK_PREFIX = re.compile(r"^\s*\d{1,3}\s+")


def parse_series_folder_name(folder_name: str) -> Tuple[str, Optional[int], Optional[int], Optional[str]]:
    """Pull (clean_name, year, existing_tmdb_id, existing_imdb_id) from a folder name.

    Examples:
        "Doctor Who (2005) {tmdb-57243}" -> ("Doctor Who", 2005, 57243, None)
        "Friends (1994)"                 -> ("Friends", 1994, None, None)
        "MrBean"                         -> ("MrBean", None, None, None)
        "01 MrBean"                      -> ("MrBean", None, None, None)
        "02 Good Night MrBean"           -> ("Good Night MrBean", None, None, None)
    """
    name = folder_name.strip()
    existing_tmdb = None
    existing_imdb = None

    m = RE_FOLDER_TMDB_ID.search(name)
    if m:
        existing_tmdb = int(m.group(1))
        name = RE_FOLDER_TMDB_ID.sub("", name)
    m = RE_FOLDER_IMDB_ID.search(name)
    if m:
        existing_imdb = m.group(1)
        name = RE_FOLDER_IMDB_ID.sub("", name)

    year: Optional[int] = None
    m = RE_FOLDER_YEAR.search(name)
    if m:
        # Take just the first 4-digit year inside the paren.
        ym = RE_FOLDER_FIRST_YEAR.search(m.group(0))
        if ym:
            year = int(ym.group(1))
        name = RE_FOLDER_YEAR.sub("", name)

    # Strip a leading "<NN> " track-number prefix (single-episode wrapper
    # convention from phase6_design.md §6). The leading_episode_number()
    # helper in tv_parser is the canonical detector, but here we want to
    # rewrite the name as well — so use a parallel regex with the same
    # "digit + whitespace" structural marker.
    name = RE_LEADING_TRACK_PREFIX.sub("", name)

    name = MULTISPACE_RE.sub(" ", name).strip(" -._,")
    return name, year, existing_tmdb, existing_imdb


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class EpisodeFile:
    """One file inside a series folder (video, nfo, subtitle, image, etc.)."""
    file_id: int = 0
    abs_path: str = ""
    parent_folder: str = ""
    filename: str = ""
    ext: str = ""
    size_bytes: int = 0
    mtime: str = ""
    file_hash: str = ""
    role: str = ""
    # Episode-specific fields (populated only when role == "episode")
    season: Optional[int] = None
    episode: Optional[int] = None
    episode_end: Optional[int] = None
    is_special: bool = False
    is_multi: bool = False
    pattern_matched: str = ""
    parser_confidence: int = 0
    series_name_guess: str = ""
    parser_flags: str = ""


@dataclass
class SeriesFolder:
    """One top-level folder under tv_root. Holds all files inside, plus
    derived data (parsed name, content check, TMDB match) once populated."""
    series_id: int = 0
    source_folder: str = ""           # absolute path
    folder_basename: str = ""          # just the folder name
    parsed_name: str = ""              # after parse_series_folder_name
    parsed_year: Optional[int] = None
    existing_tmdb_id: Optional[int] = None
    existing_imdb_id: Optional[str] = None
    files: List[EpisodeFile] = field(default_factory=list)
    # Filled by identification:
    tmdb_series_id: Optional[int] = None
    imdb_series_id: Optional[str] = None
    matched_series: str = ""
    matched_year: Optional[int] = None
    confidence: int = 0
    target_folder: str = ""
    # Filled by content check:
    content_check: str = ""            # verdict from tv_content_check
    mixed_content_flag: bool = False
    # Diagnostics:
    flags: List[str] = field(default_factory=list)
    notes: str = ""

    @property
    def episode_count(self) -> int:
        return sum(1 for f in self.files if f.role == "episode")

    @property
    def has_specials(self) -> bool:
        return any(f.is_special or f.season == 0
                   for f in self.files if f.role == "episode")

    @property
    def sample_episode(self) -> str:
        for f in self.files:
            if f.role == "episode":
                return f.filename
        return ""


# ---------------------------------------------------------------------------
# File classification
# ---------------------------------------------------------------------------

def classify_role(path: Path) -> str:
    """Return a coarse role tag for a file: episode-candidate / nfo / subtitle /
    poster / fanart / banner / image / other.

    The 'episode-candidate' verdict is upgraded/downgraded later once we know
    the file's place in a series folder structure (e.g. extras vs episodes).
    """
    suffix = path.suffix.lower()
    if suffix in VIDEO_EXTS:
        return "episode-candidate"
    if suffix in NFO_EXTS:
        return "nfo"
    if suffix in SUBTITLE_EXTS:
        return "subtitle"
    if suffix in IMAGE_EXTS:
        stem = path.stem.lower()
        # Direct match: "poster.jpg", "fanart.png", etc.
        if stem in POSTER_NAMES:
            return "poster"
        if stem in FANART_NAMES:
            return "fanart"
        if stem in BANNER_NAMES:
            return "banner"
        # Suffixed: "show-poster.jpg", "season01-banner.png"
        for kw, role in (("poster", "poster"), ("fanart", "fanart"),
                         ("banner", "banner"), ("backdrop", "fanart")):
            if kw in stem:
                return role
        return "image"
    return "other"


def refine_episode_role(file: EpisodeFile, parent_folder_name: str) -> None:
    """Promote 'episode-candidate' -> 'episode' if the parser identifies
    season+episode, else 'extras'. Mutates the file in place."""
    if file.role != "episode-candidate":
        return
    parsed = parse_episode(file.filename, parent_folder=parent_folder_name)
    if parsed.parsed:
        file.role = "episode"
        file.season = parsed.season
        file.episode = parsed.episode
        file.episode_end = parsed.episode_end
        file.is_special = parsed.is_special
        file.is_multi = parsed.is_multi
        file.pattern_matched = parsed.pattern_matched
        file.parser_confidence = parsed.confidence
        file.series_name_guess = parsed.series_name_guess
        file.parser_flags = ",".join(parsed.flags)
    else:
        file.role = "extras"
        file.parser_flags = ",".join(parsed.flags) if parsed.flags else "unparseable"


# ---------------------------------------------------------------------------
# Library walk
# ---------------------------------------------------------------------------

def should_skip_dir(dir_name: str, skip_dirs: Tuple[str, ...]) -> bool:
    """True if a directory name should be excluded from the walk."""
    if not dir_name:
        return False
    if dir_name in skip_dirs:
        return True
    # Hidden dirs starting with "." (Mac/Linux) — skip by default.
    if dir_name.startswith("."):
        return True
    return False


def walk_series_folders(
    tv_root: Path,
    skip_dirs: Tuple[str, ...],
    *,
    limit: int = 0,
) -> Iterable[Tuple[Path, List[Path]]]:
    """Yield (series_folder_path, [file_paths]) for each top-level series folder.

    A "series folder" is any direct child of tv_root that is a directory and
    not in skip_dirs. We recurse INSIDE each series folder to collect every
    file (Season N\\, Specials\\, Extras\\, etc. all roll up to one series).
    """
    if not tv_root.is_dir():
        log.error("tv_root does not exist or is not a directory: %s", tv_root)
        return

    yielded = 0
    try:
        children = sorted(tv_root.iterdir(), key=lambda p: p.name.lower())
    except OSError as e:
        log.error("Cannot list tv_root %s: %s", tv_root, e)
        return

    for child in children:
        if not child.is_dir():
            # Loose video files at tv_root: treat as their own "series folder".
            if is_video_filename(child.name):
                yield child.parent / child.name, [child]  # synthetic single-file group
                yielded += 1
                if limit and yielded >= limit:
                    return
            continue
        if should_skip_dir(child.name, skip_dirs):
            log.debug("Skipping %s (in tv_skip_dirs)", child)
            continue

        files: List[Path] = []
        for dirpath, dirnames, filenames in os.walk(str(child)):
            dirnames[:] = [d for d in dirnames if not should_skip_dir(d, skip_dirs)]
            for fn in filenames:
                files.append(Path(dirpath) / fn)
        yield child, files
        yielded += 1
        if limit and yielded >= limit:
            return


# ---------------------------------------------------------------------------
# Series identification (TMDB)
# ---------------------------------------------------------------------------

def _normalize_name_for_match(s: str) -> str:
    """Aggressive normalization for exact-match comparison."""
    s = unicodedata.normalize("NFKD", s)
    s = s.encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s


def _string_similarity(a: str, b: str) -> float:
    """Cheap normalized similarity 0..1 using difflib SequenceMatcher."""
    import difflib
    a2 = _normalize_name_for_match(a)
    b2 = _normalize_name_for_match(b)
    if not a2 or not b2:
        return 0.0
    return difflib.SequenceMatcher(None, a2, b2).ratio()


def _score_tmdb_match(
    folder_name: str,
    folder_year: Optional[int],
    candidate: Dict[str, Any],
    *,
    is_only_result: bool,
) -> int:
    """Score a TMDB candidate against the folder. 0..100.

    Heuristic favored by phase6_design.md §17: exact name + year match heavy
    (95+); multi-result matches stay around 60 unless the year clinches it.
    """
    candidate_name = str(candidate.get("name", ""))
    original_name = str(candidate.get("original_name", ""))
    first_air = str(candidate.get("first_air_date", ""))[:4]
    cand_year: Optional[int] = int(first_air) if first_air.isdigit() else None

    # Name similarity component (0..50).
    sim = max(
        _string_similarity(folder_name, candidate_name),
        _string_similarity(folder_name, original_name),
    )
    name_pts = int(sim * 50)

    # Exact-name bonus (0..15 extra).
    norm_folder = _normalize_name_for_match(folder_name)
    if norm_folder and (
        norm_folder == _normalize_name_for_match(candidate_name)
        or norm_folder == _normalize_name_for_match(original_name)
    ):
        name_pts += 15

    # Year component (0..30).
    year_pts = 0
    if folder_year and cand_year:
        if folder_year == cand_year:
            year_pts = 30
        elif abs(folder_year - cand_year) <= 1:
            year_pts = 20
        elif abs(folder_year - cand_year) <= 3:
            year_pts = 10
    elif folder_year is None:
        # Folder lacks a year — neutral on year axis.
        year_pts = 10

    # Solo-result bonus (multi-result candidates need stronger evidence).
    bonus = 5 if is_only_result else 0

    total = min(100, name_pts + year_pts + bonus)
    return total


def identify_series(
    series: SeriesFolder,
    tmdb: TmdbTvClient,
) -> None:
    """Populate series.tmdb_series_id / matched_series / confidence / etc.

    Mutates `series` in place. Adds notes/flags on edge cases.
    """
    # Single-episode wrappers (the §6 Mr. Bean shape) are statistically
    # guaranteed to fail TMDB identification because the folder name is an
    # episode title (e.g. "Good Night MrBean"), not the series name. Skip
    # the API call; merge_candidates.csv is where these get handled.
    if "single_episode_wrapper" in series.flags:
        series.notes = (
            "single-episode wrapper; identification deferred to "
            "merge_candidates.csv review"
        )
        return

    # Already identified by an existing {tmdb-NNNN} folder annotation? Skip
    # the search and just fetch details for verification.
    if series.existing_tmdb_id:
        details = tmdb.tv_details(series.existing_tmdb_id)
        if is_error(details):
            series.flags.append("tmdb_details_error")
            series.notes = f"TMDB error fetching pre-tagged id {series.existing_tmdb_id}"
            return
        if is_not_found(details):
            series.flags.append("tmdb_id_not_found")
            series.notes = f"existing tmdb-{series.existing_tmdb_id} returned 404"
            return
        series.tmdb_series_id = int(details.get("id", series.existing_tmdb_id))
        series.matched_series = str(details.get("name", series.parsed_name))
        first_air = str(details.get("first_air_date", ""))[:4]
        series.matched_year = int(first_air) if first_air.isdigit() else None
        ext = details.get("external_ids") or {}
        if ext.get("imdb_id"):
            series.imdb_series_id = ext["imdb_id"]
        series.confidence = 95  # pre-tagged folders are high-trust
        series.flags.append("pretagged_tmdb_id")
        return

    # Search by name + year.
    query = series.parsed_name or series.folder_basename
    if not query.strip():
        series.flags.append("empty_series_name")
        series.notes = "folder name produced no usable query string"
        return

    response = tmdb.search_tv(query, first_air_date_year=series.parsed_year)
    if is_error(response):
        series.flags.append("tmdb_search_error")
        series.notes = response.get("__error__", "TMDB error")
        return

    results = response.get("results", []) or []
    if not results:
        # Retry without the year (some folders have a wrong/missing year).
        if series.parsed_year:
            response2 = tmdb.search_tv(query)
            if not is_error(response2):
                results = response2.get("results", []) or []
        if not results:
            series.flags.append("no_tmdb_results")
            return

    is_only = len(results) == 1
    scored = [
        (_score_tmdb_match(query, series.parsed_year, r, is_only_result=is_only), r)
        for r in results
    ]
    scored.sort(key=lambda x: -x[0])
    best_score, best = scored[0]
    series.confidence = best_score
    series.tmdb_series_id = int(best.get("id", 0)) or None
    series.matched_series = str(best.get("name", "") or best.get("original_name", ""))
    first_air = str(best.get("first_air_date", ""))[:4]
    series.matched_year = int(first_air) if first_air.isdigit() else None

    # Pull external_ids in a follow-up call only when confidence is reasonable;
    # avoids burning rate-limit on garbage matches.
    if best_score >= CONFIDENCE_REVIEW and series.tmdb_series_id:
        details = tmdb.tv_details(series.tmdb_series_id)
        if not is_error(details) and not is_not_found(details):
            ext = details.get("external_ids") or {}
            if ext.get("imdb_id"):
                series.imdb_series_id = ext["imdb_id"]

    if not is_only:
        series.flags.append(f"multi_result_{len(results)}")
    if best_score < CONFIDENCE_REVIEW:
        series.flags.append("low_confidence")


# ---------------------------------------------------------------------------
# Target folder computation
# ---------------------------------------------------------------------------

def compute_target_folder(
    series: SeriesFolder,
    tv_root: Path,
    template: str,
) -> str:
    """Apply tv_series_template to produce the canonical target folder name.

    Template default per §14: "{title} ({year}) {{tmdb-{tmdb_id}}}"
    yields "Doctor Who (2005) {tmdb-57243}" once Python format is applied.
    """
    if not series.tmdb_series_id or not series.matched_series:
        return ""
    title = sanitize_component(series.matched_series)
    year = series.matched_year if series.matched_year else ""
    try:
        rendered = template.format(
            title=title,
            year=year,
            tmdb_id=series.tmdb_series_id,
        )
    except (KeyError, IndexError) as e:
        log.warning("tv_series_template format error %s on series %s",
                    e, series.folder_basename)
        return ""
    rendered = sanitize_component(rendered)
    return str(tv_root / rendered)


# ---------------------------------------------------------------------------
# CSV writers
# ---------------------------------------------------------------------------

def _read_existing_csv_rows(path: Path) -> List[Dict[str, str]]:
    """Read an existing CSV and return its rows as dicts. Tolerant of:
       * UTF-8 BOM (utf-8-sig)
       * UTF-16 (PowerShell Out-File default)
       * NUL byte pollution (PowerShell pipeline glitches)
       * Missing file (returns [])
       * Unparseable CSV (logs warning, returns [])

    Used by review-preservation: each writer reads prior rows before
    overwriting so the user's manual action edits survive a re-run.
    """
    if not path.is_file():
        return []
    try:
        raw = path.read_bytes()
    except OSError as e:
        log.warning("Could not read %s for review preservation: %s", path, e)
        return []
    # Detect UTF-16 BOM
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            text = raw.decode("utf-16", errors="replace")
        except Exception:
            text = raw.replace(b"\x00", b"").decode("utf-8", errors="replace")
    else:
        text = raw.replace(b"\x00", b"").decode("utf-8-sig", errors="replace")
    try:
        return list(csv.DictReader(text.splitlines()))
    except csv.Error as e:
        log.warning("Could not parse %s for review preservation: %s", path, e)
        return []


def _open_csv(path: Path):
    """Open a CSV for writing.

    Uses errors="replace" so that filenames containing lone Unicode surrogates
    (a real-world hazard on Windows: emoji or foreign-language characters that
    lost their pairing somewhere during NTFS encoding) get replaced with "?"
    instead of crashing the entire write step. The actual offending filenames
    are logged via _scrub_surrogates() so you can fix them later.
    """
    return open(path, "w", encoding="utf-8-sig", newline="", errors="replace")


def _scrub_surrogates(s: Any) -> str:
    """Strip lone Unicode surrogates from a string. Logs a warning on first hit
    so the user knows there's a problematic filename to clean up."""
    if s is None:
        return ""
    s = str(s)
    # Detect surrogate codepoints (U+D800..U+DFFF). If any present, scrub.
    for ch in s:
        if 0xD800 <= ord(ch) <= 0xDFFF:
            scrubbed = s.encode("utf-8", errors="replace").decode("utf-8")
            log.warning("filename contains lone surrogate(s); scrubbed for CSV: %r", s)
            return scrubbed
    return s


def write_inventory_csv(path: Path, files: List[EpisodeFile]) -> None:
    """File-level inventory with parser fields. Phase 6 §1 columns:
    file_id, abs_path, parent_folder, filename, ext, size_bytes, mtime,
    file_hash, role. Plus diagnostic columns (season/episode/etc.) for
    parser-derived data."""
    cols = [
        "file_id", "abs_path", "parent_folder", "filename", "ext",
        "size_bytes", "mtime", "file_hash", "role",
        "season", "episode", "episode_end", "is_special", "is_multi",
        "pattern_matched", "parser_confidence", "series_name_guess",
        "parser_flags",
    ]
    with _open_csv(path) as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for f in files:
            w.writerow([
                f.file_id, f.abs_path, f.parent_folder, f.filename, f.ext,
                f.size_bytes, f.mtime, f.file_hash, f.role,
                f.season if f.season is not None else "",
                f.episode if f.episode is not None else "",
                f.episode_end if f.episode_end is not None else "",
                "1" if f.is_special else "",
                "1" if f.is_multi else "",
                f.pattern_matched, f.parser_confidence, f.series_name_guess,
                f.parser_flags,
            ])


def write_series_plan_csv(path: Path, series_list: List[SeriesFolder]) -> None:
    """series_plan.csv — the human review file. Columns from §1:
    series_id, source_folder, episode_count, sample_episode, tmdb_series_id,
    imdb_series_id, matched_series, matched_year, target_folder, confidence,
    action, content_check, has_specials, mixed_content_flag, flags, notes,
    executed_at, user_notes.

    User-edit preservation: reads the existing CSV (if present) and keys
    rows by source_folder (stable absolute path). The user-editable
    `action` and `user_notes` columns are preserved verbatim from the
    prior run when the row's source_folder matches.
    """
    cols = [
        "series_id", "source_folder", "episode_count", "sample_episode",
        "tmdb_series_id", "imdb_series_id", "matched_series", "matched_year",
        "target_folder", "confidence", "action",
        "content_check", "has_specials", "mixed_content_flag",
        "flags", "notes", "executed_at", "user_notes",
    ]
    # Load prior user edits. Stable key = source_folder.
    prior_by_folder: Dict[str, Dict[str, str]] = {}
    for row in _read_existing_csv_rows(path):
        key = (row.get("source_folder") or "").strip()
        if key:
            prior_by_folder[key] = row
    preserved = 0

    with _open_csv(path) as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for s in series_list:
            prior = prior_by_folder.get(s.source_folder, {})
            prior_action = (prior.get("action") or "").strip()
            prior_user_notes = (prior.get("user_notes") or "").strip()
            prior_executed_at = (prior.get("executed_at") or "").strip()

            # Algorithm default action.
            default_action = "AUTO" if s.confidence >= CONFIDENCE_AUTO else ""
            action = prior_action if prior_action else default_action
            if prior_action and prior_action != default_action:
                preserved += 1

            w.writerow([
                s.series_id,
                s.source_folder,
                s.episode_count,
                s.sample_episode,
                s.tmdb_series_id or "",
                s.imdb_series_id or "",
                s.matched_series,
                s.matched_year or "",
                s.target_folder,
                s.confidence,
                action,
                s.content_check,
                "1" if s.has_specials else "",
                "1" if s.mixed_content_flag else "",
                ",".join(s.flags),
                s.notes,
                prior_executed_at,  # preserved; populated by execute_tv.py
                prior_user_notes,
            ])
    if preserved:
        log.info("Preserved %d user-edited action(s) in series_plan.csv "
                 "from prior run", preserved)


def write_episodes_plan_csv(path: Path) -> None:
    """episodes_plan.csv — per-episode override file. Mostly empty by default.

    Columns let the user paste in TMDB episode IDs or override season/episode
    numbers for outliers without disturbing the per-series flow.
    """
    cols = [
        "series_id", "source_filename",
        "tmdb_episode_id", "season_number", "episode_number",
        "action", "notes",
    ]
    with _open_csv(path) as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        # Intentionally no rows — user fills in only outliers.


def write_merge_candidates_csv(path: Path, groups: List[MergeGroup]) -> None:
    """merge_candidates.csv — output from tv_merge_clusterer.

    User-edit preservation: rows are keyed by the sorted tuple of
    member_folders (stable across re-runs as long as cluster membership
    doesn't change). The user-editable `action`, `candidate_series_name`,
    `suggested_match`, and `notes` columns are preserved verbatim. group_id
    is regenerated each run because it depends on alphabetical order.
    """
    cols = [
        "group_id", "candidate_series_name", "member_folder_count",
        "member_folders", "shared_token_count", "suggested_match",
        "action", "notes",
    ]
    # Build prior lookup keyed by sorted-member tuple.
    prior_by_members: Dict[tuple, Dict[str, str]] = {}
    for row in _read_existing_csv_rows(path):
        members_cell = row.get("member_folders") or ""
        if not members_cell:
            continue
        key = tuple(sorted(m.strip() for m in members_cell.split("|")))
        prior_by_members[key] = row
    preserved = 0

    with _open_csv(path) as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for g in groups:
            row = g.to_csv_row()
            members_key = tuple(sorted(g.members))
            prior = prior_by_members.get(members_key, {})

            # Preserve user-editable fields when set in prior run.
            for field_name in ("action", "candidate_series_name",
                               "suggested_match", "notes"):
                prior_val = (prior.get(field_name) or "").strip()
                if prior_val and prior_val != row.get(field_name, ""):
                    row[field_name] = prior_val
                    preserved += 1
            w.writerow([row.get(c, "") for c in cols])
    if preserved:
        log.info("Preserved %d user-edited field(s) in merge_candidates.csv "
                 "from prior run", preserved)


def write_mixed_content_csv(path: Path, results: List[ContentCheckResult]) -> None:
    """mixed_content.csv — output from tv_content_check (only flagged rows).

    User-edit preservation: rows keyed by folder_name. User-editable
    `action` (SKIP / RELOCATE / REVIEW) and `manual_resolution` columns
    are preserved across re-runs.
    """
    cols = [
        "folder_name", "dominant_filename_series",
        "episode_count_dominant", "episode_count_other",
        "action", "manual_resolution",
        "similarity", "total_episode_count", "unparseable_count",
        "verdict", "notes",
    ]
    prior_by_folder: Dict[str, Dict[str, str]] = {}
    for row in _read_existing_csv_rows(path):
        key = (row.get("folder_name") or "").strip()
        if key:
            prior_by_folder[key] = row
    preserved = 0

    with _open_csv(path) as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in results:
            if not r.flag_as_mixed:
                continue
            row = r.to_csv_row()
            prior = prior_by_folder.get(r.folder_name, {})
            for field_name in ("action", "manual_resolution"):
                prior_val = (prior.get(field_name) or "").strip()
                if prior_val and prior_val != row.get(field_name, ""):
                    row[field_name] = prior_val
                    preserved += 1
            w.writerow([row.get(c, "") for c in cols])
    if preserved:
        log.info("Preserved %d user-edited field(s) in mixed_content.csv "
                 "from prior run", preserved)


def write_unresolved_csv(path: Path, series_list: List[SeriesFolder]) -> None:
    """unresolved_series.csv — series TMDB couldn't match (or matched at
    very low confidence). User can manually identify and promote later."""
    cols = [
        "series_id", "source_folder", "parsed_name", "parsed_year",
        "episode_count", "confidence", "flags", "notes",
    ]
    with _open_csv(path) as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for s in series_list:
            if s.tmdb_series_id and s.confidence >= CONFIDENCE_REVIEW:
                continue
            # Skip single-episode wrappers — they're routed to
            # merge_candidates.csv, not unresolved.
            if "single_episode_wrapper" in s.flags:
                continue
            w.writerow([
                s.series_id,
                s.source_folder,
                s.parsed_name,
                s.parsed_year or "",
                s.episode_count,
                s.confidence,
                ",".join(s.flags),
                s.notes,
            ])


# ---------------------------------------------------------------------------
# State file (--resume)
# ---------------------------------------------------------------------------

def load_state(state_path: Path) -> Dict[str, Any]:
    """Load --resume state. Returns {} when missing / unreadable."""
    if not state_path.is_file():
        return {}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("state file unreadable (%s); starting fresh", e)
        return {}


def save_state(state_path: Path, state: Dict[str, Any]) -> None:
    """Atomic write of state.json."""
    tmp = state_path.with_suffix(state_path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, state_path)
    except OSError as e:
        log.warning("Failed to write state: %s", e)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="TV library inventory (Phase 6 of the Media Library pipeline)",
    )
    ap.add_argument("--config", required=True, help="Path to config.json")
    ap.add_argument("--resume", action="store_true", help="Resume from state.json")
    ap.add_argument("--limit", type=int, default=0,
                    help="Process at most N series folders (smoke testing)")
    ap.add_argument("--skip-merge-detection", action="store_true",
                    help="Skip the §6 reverse-grouping clusterer pass")
    ap.add_argument("--with-hash", action="store_true",
                    help="(Reserved) Hash episodes for duplicate detection. Not yet implemented.")
    return ap.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    cfg = Config.load(Path(args.config))
    cfg.mediaprep_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(cfg.mediaprep_dir)
    log.info(
        "tv_root=%s  mediaprep=%s  language=%s",
        cfg.tv_root, cfg.mediaprep_dir, cfg.language,
    )
    if not cfg.tv_root.is_dir():
        log.error("tv_root does not exist: %s", cfg.tv_root)
        return 1

    state_path = cfg.mediaprep_dir / "state_tv.json"
    state = load_state(state_path) if args.resume else {}

    # ------------------------------------------------------------------
    # Pass 1: walk the library, build EpisodeFile and SeriesFolder objects.
    # ------------------------------------------------------------------
    log.info("Walking %s ...", cfg.tv_root)
    t0 = time.time()
    series_list: List[SeriesFolder] = []
    file_id = 1
    series_id = 1
    walker = walk_series_folders(cfg.tv_root, cfg.tv_skip_dirs, limit=args.limit)
    for folder, files in tqdm(walker, desc="Series folders", unit="series"):
        cleaned, year, t_id, i_id = parse_series_folder_name(folder.name)
        sf = SeriesFolder(
            series_id=series_id,
            source_folder=str(folder),
            folder_basename=folder.name,
            parsed_name=cleaned,
            parsed_year=year,
            existing_tmdb_id=t_id,
            existing_imdb_id=i_id,
        )
        # Flag single-episode-wrapper folders (the §6 Mr. Bean shape).
        # The clusterer's merge_candidates.csv is the actual proposal;
        # this flag just makes the row sortable in series_plan.csv.
        if leading_episode_number(folder.name) is not None:
            sf.flags.append("single_episode_wrapper")
        for fp in files:
            try:
                stat = os.stat(win_long(fp))
            except OSError as e:
                log.warning("stat failed for %s: %s", fp, e)
                continue
            ef = EpisodeFile(
                file_id=file_id,
                abs_path=str(fp),
                parent_folder=str(fp.parent),
                filename=fp.name,
                ext=fp.suffix.lower(),
                size_bytes=stat.st_size,
                mtime=datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                role=classify_role(fp),
            )
            refine_episode_role(ef, fp.parent.name)
            sf.files.append(ef)
            file_id += 1
        series_list.append(sf)
        series_id += 1
    log.info("Walked %d series folders, %d total files in %.1fs",
             len(series_list), file_id - 1, time.time() - t0)

    # ------------------------------------------------------------------
    # Pass 2: cross-folder content check (§7). Pure-local, no network.
    # ------------------------------------------------------------------
    log.info("Running cross-folder content check ...")
    content_results: List[ContentCheckResult] = []
    for s in tqdm(series_list, desc="Content check", unit="series"):
        episode_filenames = [f.filename for f in s.files if f.role == "episode"]
        if not episode_filenames:
            continue
        result = check_folder_content(
            s.folder_basename,
            episode_filenames,
            similarity_threshold=cfg.tv_content_similarity_threshold,
            min_episodes_for_check=cfg.tv_content_min_episodes,
        )
        s.content_check = result.verdict
        s.mixed_content_flag = result.flag_as_mixed
        content_results.append(result)

    # ------------------------------------------------------------------
    # Pass 3: reverse-grouping clusterer (§6).
    # ------------------------------------------------------------------
    merge_groups: List[MergeGroup] = []
    if args.skip_merge_detection:
        log.info("--skip-merge-detection: skipping §6 clusterer")
    else:
        log.info("Running reverse-grouping clusterer ...")
        folder_names = [s.folder_basename for s in series_list]
        merge_groups = find_merge_candidates(
            folder_names,
            jaccard_threshold=cfg.tv_merge_threshold,
            min_token_overlap=cfg.tv_merge_min_token_overlap,
            min_group_size=cfg.tv_min_merge_size,
            containment_threshold=cfg.tv_merge_containment,
            min_shared_chars=cfg.tv_merge_min_shared_chars,
            max_group_size=cfg.tv_merge_max_group_size,
        )
        log.info("Found %d merge candidate group(s)", len(merge_groups))

    # ------------------------------------------------------------------
    # Pass 4: TMDB identification.
    # ------------------------------------------------------------------
    log.info("Identifying series via TMDB ...")
    tmdb_lang = cfg.tmdb_tv_language or cfg.language
    cache_path = cfg.mediaprep_dir / "tmdb_tv_cache.sqlite"
    tmdb = TmdbTvClient(cfg.tmdb_api_key, cache_path, language=tmdb_lang)
    try:
        identified = state.get("identified", {})  # series_id -> dict
        progress_every = max(5, len(series_list) // 50)
        for i, s in enumerate(tqdm(series_list, desc="TMDB identify", unit="series"), start=1):
            cached = identified.get(str(s.series_id))
            if cached:
                # Restore previously-identified series from --resume state.
                s.tmdb_series_id = cached.get("tmdb_series_id")
                s.imdb_series_id = cached.get("imdb_series_id")
                s.matched_series = cached.get("matched_series", "")
                s.matched_year = cached.get("matched_year")
                s.confidence = int(cached.get("confidence", 0))
                s.flags = list(cached.get("flags", []))
                s.notes = cached.get("notes", "")
            else:
                identify_series(s, tmdb)
                identified[str(s.series_id)] = {
                    "tmdb_series_id": s.tmdb_series_id,
                    "imdb_series_id": s.imdb_series_id,
                    "matched_series": s.matched_series,
                    "matched_year": s.matched_year,
                    "confidence": s.confidence,
                    "flags": s.flags,
                    "notes": s.notes,
                }
            s.target_folder = compute_target_folder(s, cfg.tv_root, cfg.tv_series_template)

            if i % progress_every == 0:
                state["identified"] = identified
                save_state(state_path, state)
        # Final flush
        state["identified"] = identified
        save_state(state_path, state)
    finally:
        tmdb.close()

    # ------------------------------------------------------------------
    # Pass 5: emit CSVs.
    # ------------------------------------------------------------------
    log.info("Writing CSVs to %s ...", cfg.mediaprep_dir)
    all_files: List[EpisodeFile] = []
    for s in series_list:
        all_files.extend(s.files)
    write_inventory_csv(cfg.mediaprep_dir / "inventory_tv.csv", all_files)
    write_series_plan_csv(cfg.mediaprep_dir / "series_plan.csv", series_list)
    write_episodes_plan_csv(cfg.mediaprep_dir / "episodes_plan.csv")
    write_merge_candidates_csv(cfg.mediaprep_dir / "merge_candidates.csv", merge_groups)
    write_mixed_content_csv(cfg.mediaprep_dir / "mixed_content.csv", content_results)
    write_unresolved_csv(cfg.mediaprep_dir / "unresolved_series.csv", series_list)

    # Summary stats.
    auto = sum(1 for s in series_list if s.confidence >= CONFIDENCE_AUTO)
    review = sum(1 for s in series_list
                 if 0 < s.confidence < CONFIDENCE_AUTO
                 and "single_episode_wrapper" not in s.flags)
    wrappers = sum(1 for s in series_list if "single_episode_wrapper" in s.flags)
    unresolved = sum(
        1 for s in series_list
        if (not s.tmdb_series_id or s.confidence < CONFIDENCE_REVIEW)
        and "single_episode_wrapper" not in s.flags
    )
    flagged = sum(1 for r in content_results if r.flag_as_mixed)
    log.info(
        "Summary: %d series total | %d AUTO | %d REVIEW | %d unresolved | "
        "%d wrappers (-> merge review) | %d merge groups | %d mixed-content flags",
        len(series_list), auto, review, unresolved, wrappers,
        len(merge_groups), flagged,
    )
    log.info("Done.")
    return 0


def _tb() -> str:
    return "".join(traceback.format_exc())


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        log.error("Unhandled exception:\n%s", _tb())
        sys.exit(2)
