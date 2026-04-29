#!/usr/bin/env python3
"""
inventory.py - Phase 1 of the Movie Library Reorganization pipeline.

READ-ONLY script that walks the movies root folder, identifies titles via TMDB,
detects duplicates, and emits CSVs for human review.

Makes ZERO modifications to the movie library. All output goes to the
_mediaprep directory specified in config.json.

Usage:
    py -3.13 inventory.py --config config.json
    py -3.13 inventory.py --config config.json --resume
    py -3.13 inventory.py --config config.json --limit 50   # dev / smoke test

See movie_library_design.md for the full specification.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple, Iterable, Set

# ---------------------------------------------------------------------------
# Third-party imports with friendly errors
# ---------------------------------------------------------------------------

_MISSING: List[str] = []
try:
    import requests
except ImportError:
    _MISSING.append("requests")
try:
    from tqdm import tqdm
except ImportError:
    _MISSING.append("tqdm")
try:
    from blake3 import blake3 as _blake3
except ImportError:
    _MISSING.append("blake3")
try:
    from guessit import guessit  # type: ignore
except ImportError:
    _MISSING.append("guessit")

# lxml is preferred for NFO parsing but stdlib is an acceptable fallback
try:
    from lxml import etree as _ET  # type: ignore
    _LXML = True
except ImportError:
    import xml.etree.ElementTree as _ET  # type: ignore
    _LXML = False

if _MISSING:
    print("Missing required packages: " + ", ".join(_MISSING), file=sys.stderr)
    print("Install with: py -3.13 -m pip install -r requirements.txt", file=sys.stderr)
    sys.exit(2)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIPT_VERSION = "1.0.0"

VIDEO_EXTS = {
    ".mkv", ".mp4", ".avi", ".mov", ".m4v", ".wmv",
    ".mpg", ".mpeg", ".ts", ".m2ts", ".vob", ".iso",
    ".divx", ".flv", ".webm", ".3gp", ".asf", ".ogv",
}
NFO_EXTS = {".nfo", ".xml"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tbn", ".webp", ".gif"}
SUBTITLE_EXTS = {".srt", ".sub", ".idx", ".ass", ".ssa", ".vtt", ".sup", ".smi"}
POSTER_NAMES = {"poster", "cover", "folder", "movie", "default"}
FANART_NAMES = {"fanart", "backdrop", "background", "art"}

# Folders that should be treated as "my parent is the title folder" — DVD/Blu-ray rip
# structures. Their contents get rolled up to the parent.
DISC_STRUCTURE_FOLDERS = {"video_ts", "audio_ts", "bdmv", "certificate", "jar"}

# Folder names we recurse into rather than treating as title folders
# (e.g. "H:\Movies\Action\Inception\..." — Action is a category)
# Detection is heuristic: these are candidates for "not a title folder even
# if it has some loose video files near it"
KNOWN_CATEGORY_FOLDER_HINTS = {
    "movies", "films", "action", "comedy", "drama", "horror", "sci-fi",
    "scifi", "thriller", "romance", "animation", "anime", "documentary",
    "family", "kids", "foreign", "indie", "classics", "new", "old",
}

# Folders we entirely skip
SKIP_FOLDERS = {
    "System Volume Information", "$RECYCLE.BIN", ".Trashes", ".Spotlight-V100",
    ".fseventsd", ".AppleDouble", "@eaDir",
}

EXTRAS_KEYWORDS = {
    "deleted", "behind", "featurette", "interview", "trailer", "outtake",
    "making", "extra", "bonus", "commentary", "scene", "bloopers", "gag",
}

# TMDB configuration
TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_RATE_LIMIT = 35        # requests per window (conservative; TMDB's limit is 40/10s)
TMDB_RATE_WINDOW = 10.0     # seconds
TMDB_TIMEOUT = 20

# Windows reserved names (case-insensitive)
WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# Confidence thresholds
CONFIDENCE_AUTO = 75
CONFIDENCE_REVIEW = 30

# Chunk size for progress/checkpointing
CHECKPOINT_EVERY = 50

log = logging.getLogger("inventory")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class Config:
    tmdb_api_key: str
    movies_root: Path
    mediaprep_dir: Path
    duplicates_dir: Path
    extras_size_threshold_bytes: int = 500 * 1024 * 1024
    confidence_auto: int = CONFIDENCE_AUTO
    confidence_review: int = CONFIDENCE_REVIEW
    strip_collection_suffix: bool = True
    hash_algo: str = "blake3"   # "blake3" or "sha256"
    language: str = "en-US"

    @classmethod
    def load(cls, config_path: Path) -> "Config":
        if not config_path.is_file():
            raise SystemExit(f"Config not found: {config_path}")
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        try:
            movies_root = Path(raw["movies_root"])
            mediaprep_dir = Path(raw["mediaprep_dir"])
            duplicates_dir = Path(raw["duplicates_dir"])
            tmdb_api_key = raw["tmdb_api_key"]
        except KeyError as e:
            raise SystemExit(f"Missing required config key: {e}")

        if tmdb_api_key in ("", "REPLACE_ME", "YOUR_KEY_HERE"):
            raise SystemExit("tmdb_api_key is not set in config.json")

        return cls(
            tmdb_api_key=tmdb_api_key,
            movies_root=movies_root,
            mediaprep_dir=mediaprep_dir,
            duplicates_dir=duplicates_dir,
            extras_size_threshold_bytes=int(raw.get("extras_size_threshold_mb", 500)) * 1024 * 1024,
            confidence_auto=int(raw.get("confidence_auto", CONFIDENCE_AUTO)),
            confidence_review=int(raw.get("confidence_review", CONFIDENCE_REVIEW)),
            strip_collection_suffix=bool(raw.get("strip_collection_suffix", True)),
            hash_algo=str(raw.get("hash_algo", "blake3")).lower(),
            language=str(raw.get("language", "en-US")),
        )


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(mediaprep_dir: Path) -> None:
    mediaprep_dir.mkdir(parents=True, exist_ok=True)
    log_path = mediaprep_dir / f"inventory_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    file_h = logging.FileHandler(log_path, encoding="utf-8")
    file_h.setFormatter(fmt)
    file_h.setLevel(logging.DEBUG)

    console_h = logging.StreamHandler(sys.stdout)
    console_h.setFormatter(fmt)
    console_h.setLevel(logging.INFO)

    log.setLevel(logging.DEBUG)
    log.addHandler(file_h)
    log.addHandler(console_h)
    log.info("inventory.py v%s starting; log file: %s", SCRIPT_VERSION, log_path)


# ---------------------------------------------------------------------------
# Path utilities
# ---------------------------------------------------------------------------

def win_long(p: Path) -> str:
    """Return a string path with Windows long-path prefix where appropriate."""
    s = str(p)
    if os.name == "nt" and not s.startswith("\\\\?\\"):
        # Only absolute paths can use the prefix meaningfully
        if re.match(r"^[A-Za-z]:[\\/]", s) or s.startswith("\\\\"):
            if s.startswith("\\\\"):
                return "\\\\?\\UNC\\" + s[2:]
            return "\\\\?\\" + s
    return s


WINDOWS_FORBIDDEN_RE = re.compile(r'[<>:"/\\|?*\x00-\x1F]')
MULTISPACE_RE = re.compile(r"\s+")

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


def clean_franchise_name(collection_name: str, strip_suffix: bool = True) -> str:
    s = collection_name.strip()
    if strip_suffix and s.lower().endswith(" collection"):
        s = s[: -len(" collection")].strip()
    return sanitize_component(s)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class FileInfo:
    file_id: int
    abs_path: str
    parent_folder: str
    filename: str
    ext: str
    size_bytes: int
    mtime: str
    file_hash: str = ""
    role: str = ""      # main_video, nfo, poster, fanart, subtitle, extra, other

@dataclass
class NFOData:
    imdb_id: str = ""
    tmdb_id: int = 0
    title: str = ""
    year: int = 0
    runtime_min: int = 0
    parse_error: str = ""

@dataclass
class TitleFolder:
    title_id: int
    folder_path: Path                # the "effective" title folder (may be a parent of video files for DVD rips)
    relative_folder: str             # relative to movies_root
    files: List[FileInfo] = field(default_factory=list)
    main_video: Optional[FileInfo] = None
    nfo: Optional[NFOData] = None
    guessit_title: str = ""
    guessit_year: int = 0
    tmdb_id: int = 0
    imdb_id: str = ""
    matched_title: str = ""
    matched_year: int = 0
    collection_name: str = ""
    runtime_file_min: float = 0.0
    runtime_tmdb_min: float = 0.0
    confidence: int = 0
    target_folder: str = ""
    flags: List[str] = field(default_factory=list)
    notes: str = ""
    action: str = ""
    unresolved_reason: str = ""
    duplicate_role: str = ""         # "keeper" | "duplicate_of:<title_id>" | ""


# ---------------------------------------------------------------------------
# File classification
# ---------------------------------------------------------------------------

def classify_file(path: Path) -> str:
    ext = path.suffix.lower()
    name_stem = path.stem.lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in NFO_EXTS:
        return "nfo"
    if ext in SUBTITLE_EXTS:
        return "subtitle"
    if ext in IMAGE_EXTS:
        if any(k in name_stem for k in FANART_NAMES):
            return "fanart"
        if any(k in name_stem for k in POSTER_NAMES):
            return "poster"
        return "image"
    return "other"


def looks_like_extra(file_path: Path, video_size: int) -> bool:
    """Heuristic: does this video file look like an extra rather than the main feature?"""
    name_lower = file_path.stem.lower()
    if any(k in name_lower for k in EXTRAS_KEYWORDS):
        return True
    # If the file is in an "Extras" or similar subfolder relative to the title
    for parent in file_path.parents:
        pname = parent.name.lower()
        if any(k in pname for k in EXTRAS_KEYWORDS):
            return True
    return False


# ---------------------------------------------------------------------------
# NFO parsing
# ---------------------------------------------------------------------------

_NFO_ID_PATTERNS = [
    ("imdb", re.compile(r"(tt\d{7,10})")),
]

def parse_nfo(nfo_path: Path) -> NFOData:
    """Best-effort NFO parser. Handles Plex-style, Kodi-style, and plain-text."""
    data = NFOData()
    try:
        raw = nfo_path.read_bytes()
    except Exception as e:
        data.parse_error = f"read: {e}"
        return data

    # Try XML first
    text = None
    try:
        root = _ET.fromstring(raw)
    except Exception:
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            text = ""
        root = None

    if root is not None:
        def find_text(tags: List[str]) -> str:
            for t in tags:
                el = root.find(t)
                if el is not None and (el.text or "").strip():
                    return el.text.strip()
                # also search children (some NFOs wrap in <movie>)
                for child in root.iter(t):
                    if (child.text or "").strip():
                        return child.text.strip()
            return ""

        imdb_text = find_text(["imdbid", "imdb_id", "imdbnumber", "id"])
        m = re.search(r"(tt\d{7,10})", imdb_text or "")
        if m:
            data.imdb_id = m.group(1)
        # try uniqueid nodes (Kodi/Plex style: <uniqueid type="imdb">...</uniqueid>)
        for uid in root.iter("uniqueid"):
            utype = (uid.get("type") or "").lower()
            val = (uid.text or "").strip()
            if utype == "imdb":
                m = re.search(r"(tt\d{7,10})", val)
                if m:
                    data.imdb_id = m.group(1)
            if utype == "tmdb":
                try:
                    data.tmdb_id = int(re.sub(r"\D", "", val))
                except ValueError:
                    pass
        tmdb_text = find_text(["tmdbid", "tmdb_id"])
        if tmdb_text and not data.tmdb_id:
            try:
                data.tmdb_id = int(re.sub(r"\D", "", tmdb_text))
            except ValueError:
                pass
        data.title = find_text(["title", "originaltitle"])
        year_text = find_text(["year"])
        if year_text:
            m = re.search(r"(\d{4})", year_text)
            if m:
                try:
                    data.year = int(m.group(1))
                except ValueError:
                    pass
        runtime_text = find_text(["runtime"])
        if runtime_text:
            m = re.search(r"(\d+)", runtime_text)
            if m:
                try:
                    data.runtime_min = int(m.group(1))
                except ValueError:
                    pass
    else:
        # Plain-text fallback: just search for IMDb ID pattern
        if text:
            m = re.search(r"(tt\d{7,10})", text)
            if m:
                data.imdb_id = m.group(1)
            m = re.search(r"themoviedb\.org/movie/(\d+)", text)
            if m:
                try:
                    data.tmdb_id = int(m.group(1))
                except ValueError:
                    pass

    return data


# ---------------------------------------------------------------------------
# ffprobe wrapper
# ---------------------------------------------------------------------------

_FFPROBE_CACHE: Dict[str, Dict[str, Any]] = {}

def ffprobe_info(video_path: Path) -> Dict[str, Any]:
    """Return duration_seconds, width, height, bit_rate for a video file."""
    key = str(video_path)
    if key in _FFPROBE_CACHE:
        return _FFPROBE_CACHE[key]
    info: Dict[str, Any] = {}
    cmd = [
        "ffprobe", "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        "-select_streams", "v:0",
        str(video_path),
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        if proc.returncode != 0:
            info["error"] = proc.stderr.strip()[:500] if proc.stderr else "ffprobe failed"
            _FFPROBE_CACHE[key] = info
            return info
        data = json.loads(proc.stdout or "{}")
        fmt = data.get("format", {}) or {}
        streams = data.get("streams", []) or []
        dur = fmt.get("duration")
        if dur:
            try:
                info["duration_seconds"] = float(dur)
            except ValueError:
                pass
        br = fmt.get("bit_rate")
        if br:
            try:
                info["bit_rate"] = int(br)
            except ValueError:
                pass
        if streams:
            vs = streams[0]
            if "width" in vs:
                info["width"] = int(vs["width"])
            if "height" in vs:
                info["height"] = int(vs["height"])
    except subprocess.TimeoutExpired:
        info["error"] = "timeout"
    except FileNotFoundError:
        info["error"] = "ffprobe-not-installed"
    except Exception as e:
        info["error"] = f"ffprobe: {e}"
    _FFPROBE_CACHE[key] = info
    return info


# ---------------------------------------------------------------------------
# TMDB client
# ---------------------------------------------------------------------------

class TMDBClient:
    def __init__(self, api_key: str, cache_path: Path, language: str = "en-US"):
        self.api_key = api_key
        self.language = language
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": f"movie-inventory/{SCRIPT_VERSION}"})
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(cache_path), isolation_level=None)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS cache (
                key TEXT PRIMARY KEY,
                response TEXT NOT NULL,
                ts INTEGER NOT NULL
            )
        """)
        self._request_times: List[float] = []

    def _cache_get(self, key: str) -> Optional[Dict[str, Any]]:
        row = self.db.execute("SELECT response FROM cache WHERE key = ?", (key,)).fetchone()
        if row:
            try:
                return json.loads(row[0])
            except json.JSONDecodeError:
                return None
        return None

    def _cache_put(self, key: str, value: Dict[str, Any]) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO cache (key, response, ts) VALUES (?, ?, ?)",
            (key, json.dumps(value, ensure_ascii=False), int(time.time())),
        )

    def _rate_limit(self) -> None:
        now = time.monotonic()
        self._request_times = [t for t in self._request_times if now - t < TMDB_RATE_WINDOW]
        if len(self._request_times) >= TMDB_RATE_LIMIT:
            wait = TMDB_RATE_WINDOW - (now - self._request_times[0]) + 0.1
            if wait > 0:
                time.sleep(wait)
            self._request_times = []
        self._request_times.append(time.monotonic())

    def _get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(params)
        p.setdefault("language", self.language)
        key = f"{path}?{json.dumps(p, sort_keys=True)}"
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        self._rate_limit()
        p["api_key"] = self.api_key
        url = TMDB_BASE + path
        try:
            r = self.session.get(url, params=p, timeout=TMDB_TIMEOUT)
            if r.status_code == 429:
                # Respect Retry-After
                retry_after = int(r.headers.get("Retry-After", "5"))
                log.warning("TMDB 429, sleeping %ds", retry_after)
                time.sleep(retry_after + 1)
                r = self.session.get(url, params=p, timeout=TMDB_TIMEOUT)
            if r.status_code == 404:
                data = {"__status__": 404}
                self._cache_put(key, data)
                return data
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.warning("TMDB error on %s: %s", path, e)
            return {"__error__": str(e)}
        self._cache_put(key, data)
        return data

    def search_movie(self, title: str, year: Optional[int] = None) -> Dict[str, Any]:
        params = {"query": title}
        if year:
            params["primary_release_year"] = year
        return self._get("/search/movie", params)

    def movie_details(self, tmdb_id: int) -> Dict[str, Any]:
        return self._get(f"/movie/{tmdb_id}", {})

    def find_by_imdb(self, imdb_id: str) -> Dict[str, Any]:
        return self._get(f"/find/{imdb_id}", {"external_source": "imdb_id"})

    def collection(self, collection_id: int) -> Dict[str, Any]:
        return self._get(f"/collection/{collection_id}", {})


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

def hash_file(path: Path, algo: str = "blake3", buf_size: int = 4 * 1024 * 1024) -> str:
    if algo == "blake3":
        h = _blake3()
    elif algo == "sha256":
        h = hashlib.sha256()
    else:
        raise ValueError(f"Unknown hash algo: {algo}")
    try:
        with open(win_long(path), "rb") as f:
            while True:
                chunk = f.read(buf_size)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except Exception as e:
        log.warning("Hash failed for %s: %s", path, e)
        return ""


# ---------------------------------------------------------------------------
# Filesystem walk & title discovery
# ---------------------------------------------------------------------------

def should_skip_dir(d: Path) -> bool:
    name = d.name
    if name in SKIP_FOLDERS:
        return True
    if name.startswith("_mediaprep") or name.lower() == "duplicates":
        return True
    return False


def walk_library(root: Path) -> Iterable[Path]:
    """Yield every file path under root, respecting skip rules.

    We do NOT apply the Windows long-path prefix here because:
      (a) the registry setting LongPathsEnabled=1 lets modern Python handle
          long paths natively for directory traversal, and
      (b) applying the prefix would propagate it into every file path stored
          in the CSV, breaking downstream comparisons against movies_root.
    The prefix IS applied in hash_file() and os.stat() where per-file Win32
    API calls can still hit the 260-char barrier on edge cases.
    """
    for dirpath, dirnames, filenames in os.walk(str(root)):
        # Modify dirnames in-place to skip unwanted directories
        dirnames[:] = [d for d in dirnames if not should_skip_dir(Path(d))]
        for fn in filenames:
            yield Path(dirpath) / fn


def group_title_folders(root: Path, all_files: List[Path]) -> Dict[Path, List[Path]]:
    """
    Group files into "title folders."

    Heuristics:
      * A folder containing at least one video file directly is a title folder.
      * Disc-structure children (VIDEO_TS, BDMV, etc.) roll up to the PARENT.
      * Videos loose in `root` each become their own single-file title group.
      * Subfolders of a title folder (Featurettes, Deleted Scenes, Disc 1, etc.)
        roll their contents up into the title.
    """
    # Map every video file to its "effective" title folder
    video_to_title: Dict[Path, Path] = {}
    folders_with_direct_video: Set[Path] = set()

    for f in all_files:
        if f.suffix.lower() not in VIDEO_EXTS:
            continue
        parent = f.parent
        # Roll up disc-structure folders
        if parent.name.lower() in DISC_STRUCTURE_FOLDERS:
            title_folder = parent.parent
        else:
            title_folder = parent
        # If the video is loose in `root`, treat the file itself as its own title
        try:
            if title_folder.resolve() == root.resolve():
                # Put it in a synthetic folder keyed by the file path — we use the file
                # as its own key, handled downstream
                video_to_title[f] = f  # sentinel: the file IS the title
                continue
        except OSError:
            pass
        video_to_title[f] = title_folder
        folders_with_direct_video.add(title_folder)

    # Now for every file, figure out which title folder it belongs to
    title_to_files: Dict[Path, List[Path]] = defaultdict(list)

    # First pass: every video file is claimed by its title folder
    for vf, tf in video_to_title.items():
        title_to_files[tf].append(vf)

    # Second pass: non-video files attach to the nearest ancestor title folder
    for f in all_files:
        if f.suffix.lower() in VIDEO_EXTS:
            continue
        # Walk up from the file's parent to find the first title folder
        attached = False
        p = f.parent
        for _ in range(8):  # safety depth limit
            # If this folder has a direct video, it's the title folder
            if p in folders_with_direct_video:
                title_to_files[p].append(f)
                attached = True
                break
            # If this folder's name is a disc-structure folder, jump to parent
            if p.name.lower() in DISC_STRUCTURE_FOLDERS and p.parent in folders_with_direct_video:
                title_to_files[p.parent].append(f)
                attached = True
                break
            # If we've reached the root, stop
            if p == root or p.parent == p:
                break
            p = p.parent
        if not attached:
            # Orphan — not adjacent to any video. Skip silently (won't appear in plan).
            pass

    return title_to_files


# ---------------------------------------------------------------------------
# Identification
# ---------------------------------------------------------------------------

# --- Filename preprocessing for guessit ---------------------------------------
# Real-world libraries are full of DVD/Blu-ray rip artifacts that confuse guessit.
# We strip these BEFORE guessit so the title extraction works.

# Trailing track/playlist suffixes (case insensitive). Examples:
#   "Foo-A1 T00", "Foo-B1 T01", "Foo-C1 T00",
#   "Foo Title1", "Foo_Title18", "Foo_Playlist_800",
#   "Foo T00", "Foo T01"
RIP_SUFFIX_RE = re.compile(
    r"(?:[-_\s]+(?:[ABC]\d+\s+)?[Tt]\d{2,3}"          # -A1 T00 / T01 / T00
    r"|[-_\s]+[Tt]itle[\s_]*\d+"                       # _Title1 / Title18
    r"|[-_\s]+Playlist[\s_]*\d+"                       # _Playlist_800
    r")\s*$",
    re.IGNORECASE,
)

# Leading sequence prefix: "03-Foo", "03 - Foo", "1994 - Foo", "1946 - Foo".
# We strip a numeric prefix followed by separator, but NOT 4-digit years that look
# like the only year in the name. Heuristic: strip if the rest still contains a
# year, OR if the leading number is <= 99 (i.e. a sequence index, not a year).
LEADING_SEQ_RE = re.compile(r"^(\d{1,4})\s*[-.]\s*(.+)$")
ANY_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")

def preprocess_for_guessit(name: str) -> str:
    """Clean rip-artifact patterns from a filename/folder name before guessit."""
    if not name:
        return name
    # Strip extension if present (we'll feed the stem)
    stem, _, ext = name.rpartition(".")
    if stem and len(ext) <= 4 and ext.isalnum():
        name = stem

    # ALL_CAPS_UNDERSCORE → Title Case (e.g. BARBIE_AND_THE_SECRET_DOOR).
    # Heuristic: if the string is mostly uppercase letters with underscores, normalize.
    letters = re.sub(r"[^A-Za-z]", "", name)
    if letters and letters == letters.upper() and "_" in name and len(letters) >= 4:
        name = name.replace("_", " ").title()

    # Strip trailing rip artifacts repeatedly (e.g. "Foo Title1 T00")
    prev = None
    while prev != name:
        prev = name
        name = RIP_SUFFIX_RE.sub("", name).rstrip(" -_.")

    # Strip leading sequence prefix
    m = LEADING_SEQ_RE.match(name)
    if m:
        leading = int(m.group(1))
        rest = m.group(2)
        # Strip if leading number is a sequence (<=99) OR if the rest still has a year
        if leading <= 99 or ANY_YEAR_RE.search(rest):
            name = rest

    # Compress underscores/dots-as-separators inside the title (DVD-style "Foo.Bar.1971")
    # guessit handles this fine on its own, no need to touch.

    return name.strip()


def guess_from_name(folder_name: str, filename: str) -> Tuple[str, Optional[int]]:
    """Run guessit on a folder name or filename, prefer folder info if present.

    Each name is preprocessed to strip DVD/Blu-ray rip artifacts that confuse
    guessit (T00 / Title18 / -A1 T00 / Playlist_800 / leading "03-" prefixes /
    ALL_CAPS_UNDERSCORE).
    """
    title = ""
    year: Optional[int] = None
    for raw_name in (folder_name, filename):
        if not raw_name:
            continue
        cleaned = preprocess_for_guessit(raw_name)
        if not cleaned:
            continue
        try:
            g = guessit(cleaned, options={"type": "movie"})
            if not title and g.get("title"):
                title = str(g.get("title"))
            if not year and g.get("year"):
                try:
                    year = int(g.get("year"))
                except (TypeError, ValueError):
                    pass
            if title and year:
                break
        except Exception as e:
            log.debug("guessit failed on %r: %s", cleaned, e)
    return title, year


def similarity(a: str, b: str) -> float:
    """Cheap normalized similarity 0..1 using difflib."""
    import difflib
    a2 = re.sub(r"[^a-z0-9]", "", a.lower())
    b2 = re.sub(r"[^a-z0-9]", "", b.lower())
    if not a2 or not b2:
        return 0.0
    return difflib.SequenceMatcher(None, a2, b2).ratio()


def identify(title: TitleFolder, tmdb: TMDBClient, strip_collection: bool = True) -> None:
    """Populate title.tmdb_id, imdb_id, matched_title, matched_year, collection_name.

    If strip_collection is True, the collection_name stored on the title has
    any trailing ' Collection' suffix removed (e.g. 'James Bond Collection' ->
    'James Bond'). This keeps the CSV's collection_name column consistent with
    the franchise folder in target_folder.
    """
    nfo = title.nfo
    guess_title = title.guessit_title
    guess_year = title.guessit_year

    movie: Dict[str, Any] = {}

    # 1. Prefer IMDb ID from NFO if present
    if nfo and nfo.imdb_id:
        r = tmdb.find_by_imdb(nfo.imdb_id)
        results = (r or {}).get("movie_results") or []
        if results:
            movie = tmdb.movie_details(int(results[0]["id"]))
            if movie and not movie.get("__error__"):
                title.notes += f"matched via NFO imdb {nfo.imdb_id}; "

    # 2. TMDB ID from NFO
    if not movie.get("id") and nfo and nfo.tmdb_id:
        m = tmdb.movie_details(int(nfo.tmdb_id))
        if m and not m.get("__error__") and m.get("id"):
            movie = m
            title.notes += f"matched via NFO tmdb {nfo.tmdb_id}; "

    # 3. Search by guessit title/year
    if not movie.get("id") and guess_title:
        search = tmdb.search_movie(guess_title, year=guess_year)
        results = (search or {}).get("results") or []
        if results:
            best = None
            year_disambiguated = False
            if guess_year:
                year_matches = [r for r in results
                                if (r.get("release_date") or "")[:4] == str(guess_year)]
                if len(year_matches) == 1:
                    best = year_matches[0]
                    year_disambiguated = True
                elif len(year_matches) > 1:
                    best = year_matches[0]
                # else: no year match, fall through
            if best is None:
                best = results[0]
            movie = tmdb.movie_details(int(best["id"]))
            if len(results) == 1:
                title.flags.append("single-tmdb-match")
            elif len(results) > 1:
                if year_disambiguated:
                    title.flags.append("multi-match-year-disambiguated")
                elif guess_year:
                    title.flags.append("multi-match-year-filter-failed")
                else:
                    title.flags.append("multi-match-no-year")
                title.notes += (f"TMDB returned {len(results)} candidates; chose "
                                f"{best.get('title')} "
                                f"({(best.get('release_date') or '')[:4]}); ")
        else:
            # Retry without year if a year was provided but search came back empty
            if guess_year:
                search2 = tmdb.search_movie(guess_title, year=None)
                results2 = (search2 or {}).get("results") or []
                if results2:
                    best = results2[0]
                    movie = tmdb.movie_details(int(best["id"]))
                    title.flags.append("year-not-matched")
                    title.notes += "TMDB had no year-year match; used first no-year result; "

    if not movie or not movie.get("id"):
        title.unresolved_reason = "no-tmdb-match"
        return

    title.tmdb_id = int(movie["id"])
    title.imdb_id = movie.get("imdb_id") or ""
    title.matched_title = movie.get("title") or ""
    rd = movie.get("release_date") or ""
    if rd and len(rd) >= 4:
        try:
            title.matched_year = int(rd[:4])
        except ValueError:
            pass
    rt = movie.get("runtime")
    if rt:
        try:
            title.runtime_tmdb_min = float(rt)
        except (TypeError, ValueError):
            pass
    coll = movie.get("belongs_to_collection")
    if coll and coll.get("name"):
        raw_name = str(coll["name"])
        title.collection_name = clean_franchise_name(raw_name, strip_suffix=strip_collection)


# ---------------------------------------------------------------------------
# Confidence scoring
# ---------------------------------------------------------------------------

# Common ligatures and special letters that appear in TMDB titles but not in
# typical filenames (or vice versa). Expanded BEFORE alphanumeric stripping so
# that e.g. "Æon Flux" and "Aeon Flux" compare equal under exact-match.
_LIGATURE_MAP = str.maketrans({
    "æ": "ae", "Æ": "ae",
    "œ": "oe", "Œ": "oe",
    "ß": "ss",
    "ø": "o",  "Ø": "o",
    "ð": "d",  "Ð": "d",
    "þ": "th", "Þ": "th",
})


def _normalize_for_exact_match(s: str) -> str:
    if not s:
        return ""
    s = s.lower()
    # Expand ligatures, then strip Unicode combining marks (accents),
    # then drop everything that isn't alphanumeric.
    s = s.translate(_LIGATURE_MAP)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]", "", s)


def score_confidence(t: TitleFolder) -> int:
    """Confidence scoring rubric (Path A: rescue exact-title-no-year matches):

    Bonuses:
      +50  NFO contains an IMDb/TMDB id that resolved on TMDB
      +30  title similarity >= 0.90
      +18  title similarity >= 0.75
      +5   title similarity >= 0.60
      +15  normalized exact title match (alphanumeric-lowercase)
      +15  exact-title AND runtime within 5% (corroboration bonus)
      +30  year exact match
      +15  year ±1 match
      +15  runtime within 5% of TMDB
      +6   runtime within 10% of TMDB
      +5   single TMDB search result (unambiguous)

    Penalties:
      -15  multi-match where year-filter failed (waived if title is exact-match)
      -10  multi-match with NO year provided (waived if title is exact-match)
       0   multi-match where year-filter cleanly disambiguated to one
      -10  year-not-matched
      -10  no year detected anywhere AND no NFO id (waived if exact-match + runtime)
      -20  main video lives in an extras-like subfolder

    Cap [0..100].
    """
    score = 0

    nfo_id_present = bool(t.nfo and (t.nfo.imdb_id or t.nfo.tmdb_id))
    if nfo_id_present and t.tmdb_id:
        score += 50

    # Normalized exact-title comparison: case + whitespace + punctuation
    # insensitive, with common ligatures expanded (æ → ae, œ → oe, ß → ss).
    norm_g = _normalize_for_exact_match(t.guessit_title)
    norm_m = _normalize_for_exact_match(t.matched_title)
    title_exact = bool(norm_g and norm_m and norm_g == norm_m)

    if t.matched_title and t.guessit_title:
        sim = similarity(t.guessit_title, t.matched_title)
        if sim >= 0.90:
            score += 30
        elif sim >= 0.75:
            score += 18
        elif sim >= 0.60:
            score += 5

    if title_exact:
        score += 15
        if "title-exact-match" not in t.flags:
            t.flags.append("title-exact-match")

    if t.matched_year and t.guessit_year:
        if t.guessit_year == t.matched_year:
            score += 30
        elif abs(t.guessit_year - t.matched_year) == 1:
            score += 15

    runtime_close = False
    if t.runtime_file_min and t.runtime_tmdb_min:
        denom = max(1.0, t.runtime_tmdb_min)
        diff_pct = abs(t.runtime_file_min - t.runtime_tmdb_min) / denom
        if diff_pct <= 0.05:
            score += 15
            runtime_close = True
        elif diff_pct <= 0.10:
            score += 6

    # Exact title + tight runtime is a near-certainty signal even without a year.
    if title_exact and runtime_close:
        score += 15
        if "title-runtime-corroborated" not in t.flags:
            t.flags.append("title-runtime-corroborated")

    if "single-tmdb-match" in t.flags:
        score += 5

    # Multi-match penalties, waived when the title is a normalized exact match —
    # an exact-title pick from a candidate list is not truly ambiguous.
    if not title_exact:
        if "multi-match-year-filter-failed" in t.flags:
            score -= 15
        elif "multi-match-no-year" in t.flags:
            score -= 10
    # multi-match-year-disambiguated: no penalty — year cleanly resolved ambiguity

    if "year-not-matched" in t.flags:
        score -= 10
    # No-year-anywhere penalty waived when exact title + runtime corroborate.
    if (not t.guessit_year and not nfo_id_present and not t.matched_year
            and not (title_exact and runtime_close)):
        score -= 10

    # Main video being in an extras-like subfolder drops confidence significantly
    if t.main_video:
        mv = Path(t.main_video.abs_path)
        if looks_like_extra(mv, t.main_video.size_bytes):
            score -= 20
            if "main-video-looks-like-extra" not in t.flags:
                t.flags.append("main-video-looks-like-extra")

    score = max(0, min(100, score))
    return score


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------

@dataclass
class DupGroup:
    group_id: int
    match_type: str         # "exact-hash" or "same-tmdb-id"
    keeper_title_id: int
    duplicate_title_ids: List[int]
    keeper_path: str
    duplicate_paths: List[str]


_YEAR_FOLDER_RE = re.compile(r"\(\d{4}\)")

def rank_copy(t: TitleFolder, movies_root: Optional[Path] = None) -> Tuple[int, int, int, int, int, int, int, int]:
    """Sort key — higher is better. Used to choose 'keeper' among duplicates.

    Priority order (most significant first):
      1. Resolved (1) over unresolved (0). A title with a TMDB id beats one without.
         This auto-resolves the common case where one copy was identified and the
         other is the messy-named twin.
      2. Folder name contains '(YEAR)' pattern — proper organization wins.
      3. Not loose in the library root — a video in its own dedicated folder wins
         over one sitting bare in H:\\Movies.
      4. Video height (higher resolution).
      5. Video bit_rate.
      6. File size.
      7. Confidence score.
      8. Longer path as a final tiebreaker (prefers more deeply organized files,
         which tend to be the canonical copy).
    """
    mv = t.main_video
    if not mv:
        return (0, 0, 0, 0, 0, 0, 0, 0)

    is_resolved = 0 if t.unresolved_reason else 1

    folder_name = Path(t.folder_path).name
    has_year_pattern = 1 if _YEAR_FOLDER_RE.search(folder_name) else 0

    # "Loose in root" check: the video file's parent IS the movies_root
    not_loose = 0
    if movies_root is not None:
        try:
            mv_parent = Path(mv.abs_path).parent
            if mv_parent.resolve() != movies_root.resolve():
                not_loose = 1
        except OSError:
            not_loose = 1
    else:
        not_loose = 1

    info = ffprobe_info(Path(mv.abs_path))
    h = int(info.get("height", 0) or 0)
    br = int(info.get("bit_rate", 0) or 0)
    sz = int(mv.size_bytes or 0)

    return (is_resolved, has_year_pattern, not_loose, h, br, sz, t.confidence, len(mv.abs_path))


def detect_duplicates(titles: List[TitleFolder], hash_algo: str, movies_root: Path) -> List[DupGroup]:
    """
    Two-phase duplicate detection:
      A. Group main-video files by exact byte size. Singletons skip.
      B. For each non-singleton size group, hash the main video; matching hashes
         = exact binary duplicates.
      C. After identification, any tmdb_id with more than one title is a content duplicate.
    """
    groups: List[DupGroup] = []
    next_id = 1

    # Only consider titles with a main video
    by_size: Dict[int, List[TitleFolder]] = defaultdict(list)
    for t in titles:
        if t.main_video and t.main_video.size_bytes > 0:
            by_size[t.main_video.size_bytes].append(t)

    # Candidate hash groups
    hashed: Dict[Tuple[int, str], List[TitleFolder]] = defaultdict(list)
    for size, ts in by_size.items():
        if len(ts) < 2:
            continue
        for t in tqdm(ts, desc=f"Hashing size-group {size}", unit="file", leave=False):
            mv = t.main_video
            assert mv is not None
            h = hash_file(Path(mv.abs_path), algo=hash_algo)
            mv.file_hash = h
            if h:
                hashed[(size, h)].append(t)

    for (_size, _h), ts in hashed.items():
        if len(ts) < 2:
            continue
        ts_sorted = sorted(ts, key=lambda t: rank_copy(t, movies_root), reverse=True)
        keeper = ts_sorted[0]
        losers = ts_sorted[1:]
        keeper.duplicate_role = "keeper"
        for l in losers:
            l.duplicate_role = f"duplicate_of:{keeper.title_id}"
        groups.append(DupGroup(
            group_id=next_id,
            match_type="exact-hash",
            keeper_title_id=keeper.title_id,
            duplicate_title_ids=[l.title_id for l in losers],
            keeper_path=keeper.main_video.abs_path if keeper.main_video else "",
            duplicate_paths=[l.main_video.abs_path if l.main_video else "" for l in losers],
        ))
        next_id += 1

    # Content-level duplicates by tmdb_id (excluding already-flagged exact dupes)
    by_tmdb: Dict[int, List[TitleFolder]] = defaultdict(list)
    for t in titles:
        if t.tmdb_id and not t.duplicate_role:
            by_tmdb[t.tmdb_id].append(t)
    for tmdb_id, ts in by_tmdb.items():
        if len(ts) < 2:
            continue
        ts_sorted = sorted(ts, key=lambda t: rank_copy(t, movies_root), reverse=True)
        keeper = ts_sorted[0]
        losers = ts_sorted[1:]
        keeper.duplicate_role = "keeper"
        for l in losers:
            l.duplicate_role = f"duplicate_of:{keeper.title_id}"
        groups.append(DupGroup(
            group_id=next_id,
            match_type="same-tmdb-id",
            keeper_title_id=keeper.title_id,
            duplicate_title_ids=[l.title_id for l in losers],
            keeper_path=keeper.main_video.abs_path if keeper.main_video else "",
            duplicate_paths=[l.main_video.abs_path if l.main_video else "" for l in losers],
        ))
        next_id += 1

    return groups


# ---------------------------------------------------------------------------
# Target folder computation
# ---------------------------------------------------------------------------

def compute_target_folder(t: TitleFolder, movies_root: Path, strip_collection: bool) -> str:
    if not t.matched_title or not t.matched_year or not t.imdb_id:
        return ""
    title_part = sanitize_component(t.matched_title)
    year_part = f"({t.matched_year})"
    id_part = f"{{imdb-{t.imdb_id}}}"
    leaf = f"{title_part} {year_part} {id_part}"
    if t.collection_name:
        franchise = clean_franchise_name(t.collection_name, strip_suffix=strip_collection)
        # If franchise name becomes empty after cleaning, treat as no collection
        if franchise:
            return str(movies_root / franchise / leaf)
    return str(movies_root / leaf)


# ---------------------------------------------------------------------------
# CSV writers
# ---------------------------------------------------------------------------

def _open_csv_for_write(path: Path):
    """Open `path` for writing. If it's locked by another process (e.g. Excel
    has it open), fall back to a timestamped sibling path and log a warning so
    the user knows where the new copy went.
    """
    try:
        return open(path, "w", newline="", encoding="utf-8"), path
    except PermissionError:
        ts = time.strftime("%Y%m%d_%H%M%S")
        alt = path.with_name(f"{path.stem}_{ts}{path.suffix}")
        log.warning(
            "Could not write %s (locked by another process — Excel?). "
            "Writing to %s instead. Close the original and rename when convenient.",
            path, alt,
        )
        return open(alt, "w", newline="", encoding="utf-8"), alt


def write_inventory_csv(path: Path, files: List[FileInfo]) -> None:
    f, _ = _open_csv_for_write(path)
    with f:
        w = csv.writer(f)
        w.writerow(["file_id", "abs_path", "parent_folder", "filename",
                    "ext", "size_bytes", "mtime", "file_hash", "role"])
        for fi in files:
            w.writerow([fi.file_id, fi.abs_path, fi.parent_folder, fi.filename,
                        fi.ext, fi.size_bytes, fi.mtime, fi.file_hash, fi.role])


def write_plan_csv(path: Path, titles: List[TitleFolder]) -> None:
    cols = ["title_id", "source_folder", "main_video_file", "tmdb_id", "imdb_id",
            "matched_title", "matched_year", "collection_name", "target_folder",
            "confidence", "action", "duplicate_role", "flags", "notes"]
    f, _ = _open_csv_for_write(path)
    with f:
        w = csv.writer(f)
        w.writerow(cols)
        for t in titles:
            if t.unresolved_reason:
                continue
            w.writerow([
                t.title_id,
                t.relative_folder,
                (t.main_video.filename if t.main_video else ""),
                t.tmdb_id or "",
                t.imdb_id,
                t.matched_title,
                t.matched_year or "",
                t.collection_name,
                t.target_folder,
                t.confidence,
                t.action,
                t.duplicate_role,
                ",".join(t.flags),
                t.notes.strip("; "),
            ])


def write_unresolved_csv(path: Path, titles: List[TitleFolder]) -> None:
    cols = ["title_id", "source_folder", "main_video_file", "guessit_title",
            "guessit_year", "reason", "flags", "notes"]
    f, _ = _open_csv_for_write(path)
    with f:
        w = csv.writer(f)
        w.writerow(cols)
        for t in titles:
            if not t.unresolved_reason:
                continue
            w.writerow([
                t.title_id,
                t.relative_folder,
                (t.main_video.filename if t.main_video else ""),
                t.guessit_title,
                t.guessit_year or "",
                t.unresolved_reason,
                ",".join(t.flags),
                t.notes.strip("; "),
            ])


def write_duplicates_csv(path: Path, groups: List[DupGroup]) -> None:
    cols = ["group_id", "match_type", "keeper_title_id", "duplicate_title_ids",
            "keeper_path", "duplicate_paths"]
    f, _ = _open_csv_for_write(path)
    with f:
        w = csv.writer(f)
        w.writerow(cols)
        for g in groups:
            w.writerow([
                g.group_id, g.match_type, g.keeper_title_id,
                ",".join(str(i) for i in g.duplicate_title_ids),
                g.keeper_path,
                " | ".join(g.duplicate_paths),
            ])


# ---------------------------------------------------------------------------
# State / resume
# ---------------------------------------------------------------------------

def load_state(state_path: Path) -> Dict[str, Any]:
    if state_path.is_file():
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state_path: Path, state: Dict[str, Any]) -> None:
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(state_path)


# Fields on TitleFolder that capture the *result* of identification.
# These get persisted so --resume actually restores prior work instead of
# silently leaving titles in an "I know this id but the data is gone" hole.
_IDENT_FIELDS = (
    "guessit_title", "guessit_year",
    "tmdb_id", "imdb_id", "matched_title", "matched_year",
    "collection_name",
    "runtime_file_min", "runtime_tmdb_min",
    "confidence", "target_folder",
    "action", "unresolved_reason",
    "duplicate_role",
    "notes",
)


def serialize_title_identification(t: "TitleFolder") -> Dict[str, Any]:
    d: Dict[str, Any] = {"title_id": t.title_id}
    for k in _IDENT_FIELDS:
        d[k] = getattr(t, k)
    d["flags"] = list(t.flags)
    if t.nfo is not None:
        d["nfo"] = {
            "imdb_id": t.nfo.imdb_id,
            "tmdb_id": t.nfo.tmdb_id,
            "title": t.nfo.title,
            "year": t.nfo.year,
            "runtime_min": t.nfo.runtime_min,
        }
    return d


def restore_title_identification(t: "TitleFolder", d: Dict[str, Any]) -> None:
    for k in _IDENT_FIELDS:
        if k in d:
            setattr(t, k, d[k])
    flags = d.get("flags")
    if isinstance(flags, list):
        t.flags = list(flags)
    nfo_d = d.get("nfo")
    if isinstance(nfo_d, dict):
        t.nfo = NFOData(
            imdb_id=nfo_d.get("imdb_id", "") or "",
            tmdb_id=int(nfo_d.get("tmdb_id") or 0),
            title=nfo_d.get("title", "") or "",
            year=int(nfo_d.get("year") or 0),
            runtime_min=int(nfo_d.get("runtime_min") or 0),
        )


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Movie library inventory (Phase 1)")
    parser.add_argument("--config", required=True, help="Path to config.json")
    parser.add_argument("--resume", action="store_true", help="Resume from saved state")
    parser.add_argument("--limit", type=int, default=0, help="Process at most N title folders (testing)")
    parser.add_argument("--skip-hash", action="store_true", help="Skip BLAKE3 duplicate hashing (faster but misses exact duplicates)")
    args = parser.parse_args()

    cfg = Config.load(Path(args.config))
    cfg.mediaprep_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(cfg.mediaprep_dir)

    log.info("Config loaded: root=%s, mediaprep=%s, duplicates=%s",
             cfg.movies_root, cfg.mediaprep_dir, cfg.duplicates_dir)
    if not cfg.movies_root.is_dir():
        log.error("movies_root does not exist: %s", cfg.movies_root)
        return 1

    state_path = cfg.mediaprep_dir / "state.json"
    state = load_state(state_path) if args.resume else {}

    # --- Phase 1a: walk filesystem -------------------------------------------------
    log.info("Walking %s ...", cfg.movies_root)
    t0 = time.time()
    all_paths: List[Path] = []
    for p in tqdm(walk_library(cfg.movies_root), desc="Scanning files", unit="file"):
        all_paths.append(p)
    log.info("Found %d files in %.1fs", len(all_paths), time.time() - t0)

    # Build initial FileInfo entries
    files: List[FileInfo] = []
    file_id_counter = 1
    for p in all_paths:
        try:
            stat = os.stat(win_long(p))
        except OSError as e:
            log.warning("stat failed for %s: %s", p, e)
            continue
        role_kind = classify_file(p)
        fi = FileInfo(
            file_id=file_id_counter,
            abs_path=str(p),
            parent_folder=str(p.parent),
            filename=p.name,
            ext=p.suffix.lower(),
            size_bytes=stat.st_size,
            mtime=datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            role="",
        )
        # Preliminary role assignment; refined once title folders are known
        if role_kind == "video":
            fi.role = "video-candidate"
        elif role_kind == "nfo":
            fi.role = "nfo"
        elif role_kind == "poster":
            fi.role = "poster"
        elif role_kind == "fanart":
            fi.role = "fanart"
        elif role_kind == "image":
            fi.role = "artwork"
        elif role_kind == "subtitle":
            fi.role = "subtitle"
        else:
            fi.role = "other"
        files.append(fi)
        file_id_counter += 1

    path_to_file: Dict[str, FileInfo] = {fi.abs_path: fi for fi in files}

    # --- Phase 1b: group into title folders ----------------------------------------
    log.info("Grouping into title folders ...")
    title_map = group_title_folders(cfg.movies_root, all_paths)
    log.info("Identified %d candidate title folders", len(title_map))

    titles: List[TitleFolder] = []
    tid = 1
    for title_folder, file_list in sorted(title_map.items(), key=lambda kv: str(kv[0])):
        videos = [f for f in file_list if f.suffix.lower() in VIDEO_EXTS]
        if not videos:
            continue
        # Main video = largest that does not obviously look like an extra; else largest overall
        videos_sorted = sorted(videos, key=lambda p: path_to_file[str(p)].size_bytes, reverse=True)
        main_candidate = None
        for v in videos_sorted:
            if not looks_like_extra(v, path_to_file[str(v)].size_bytes):
                main_candidate = v
                break
        if main_candidate is None:
            main_candidate = videos_sorted[0]

        # Compute the "display folder" relative path
        try:
            rel_folder = str(title_folder.relative_to(cfg.movies_root))
        except ValueError:
            rel_folder = str(title_folder)

        tf = TitleFolder(
            title_id=tid,
            folder_path=title_folder,
            relative_folder=rel_folder,
            files=[path_to_file[str(p)] for p in file_list if str(p) in path_to_file],
        )
        tf.main_video = path_to_file[str(main_candidate)]
        tf.main_video.role = "main_video"

        # Mark other videos as extras
        for v in videos_sorted:
            fi = path_to_file[str(v)]
            if fi is tf.main_video:
                continue
            if fi.size_bytes < cfg.extras_size_threshold_bytes:
                fi.role = "extra"
            else:
                fi.role = "extra-large"
                tf.flags.append("large-extra-needs-review")

        titles.append(tf)
        tid += 1

    if args.limit:
        titles = titles[: args.limit]
        log.info("Limiting run to first %d title folders", len(titles))

    # --- Phase 1c: per-title identification ----------------------------------------
    tmdb_cache_path = cfg.mediaprep_dir / "tmdb_cache.sqlite"
    tmdb = TMDBClient(cfg.tmdb_api_key, tmdb_cache_path, language=cfg.language)

    # Restore prior identification from state (if any). Older state.json files
    # may only have `identified_title_ids`; in that case we treat them as
    # invalid and re-identify, since we can't recover the actual results.
    saved_idents = state.get("identified_titles") or []
    saved_idents_by_id: Dict[int, Dict[str, Any]] = {
        int(d["title_id"]): d for d in saved_idents if "title_id" in d
    }
    legacy_done_only = bool(state.get("identified_title_ids")) and not saved_idents_by_id
    if saved_idents_by_id:
        restored = 0
        for t in titles:
            d = saved_idents_by_id.get(t.title_id)
            if d:
                restore_title_identification(t, d)
                restored += 1
        log.info("Resuming: restored identification for %d title folders", restored)
    elif legacy_done_only:
        log.warning(
            "state.json is from an older version (only title_ids, no results). "
            "Re-running identification for all titles. TMDB cache still applies, "
            "so this should be fast."
        )

    done_ids: Set[int] = set(saved_idents_by_id.keys())

    ffprobe_missing_warned = False
    for i, t in enumerate(tqdm(titles, desc="Identifying", unit="title")):
        if t.title_id in done_ids:
            continue
        try:
            # Pick first NFO if present
            nfo_files = [f for f in t.files if f.role == "nfo"]
            if nfo_files:
                t.nfo = parse_nfo(Path(nfo_files[0].abs_path))

            # guessit
            folder_basename = t.folder_path.name
            main_basename = t.main_video.filename if t.main_video else ""
            gt, gy = guess_from_name(folder_basename, main_basename)
            t.guessit_title = gt
            t.guessit_year = gy or 0

            # ffprobe runtime
            if t.main_video:
                info = ffprobe_info(Path(t.main_video.abs_path))
                if info.get("error") == "ffprobe-not-installed" and not ffprobe_missing_warned:
                    log.warning("ffprobe not found on PATH; runtime confidence will not be computed")
                    ffprobe_missing_warned = True
                dur = info.get("duration_seconds")
                if dur:
                    t.runtime_file_min = dur / 60.0

            # TMDB match (pre-cleans collection name per cfg.strip_collection_suffix)
            identify(t, tmdb, strip_collection=cfg.strip_collection_suffix)

            # Confidence
            t.confidence = score_confidence(t)

            # Target folder (only if identified)
            if t.tmdb_id and t.imdb_id and t.matched_title:
                t.target_folder = compute_target_folder(t, cfg.movies_root, cfg.strip_collection_suffix)
                if not t.target_folder:
                    t.flags.append("target-folder-empty")

            # Action assignment
            if t.unresolved_reason:
                t.action = ""  # stays in unresolved
            elif t.confidence >= cfg.confidence_auto and t.target_folder:
                t.action = "AUTO"
            elif t.confidence >= cfg.confidence_review:
                t.action = "REVIEW"
            else:
                t.unresolved_reason = "low-confidence"
                t.action = ""
        except Exception as e:
            log.error("Identify failed for title %d (%s): %s", t.title_id, t.relative_folder, e)
            log.debug("%s", _tb())
            t.unresolved_reason = f"exception: {e.__class__.__name__}"
            t.action = ""

        done_ids.add(t.title_id)
        saved_idents_by_id[t.title_id] = serialize_title_identification(t)
        if (i + 1) % CHECKPOINT_EVERY == 0:
            state["identified_titles"] = list(saved_idents_by_id.values())
            state["identified_title_ids"] = sorted(done_ids)  # legacy compat
            save_state(state_path, state)

    state["identified_titles"] = list(saved_idents_by_id.values())
    state["identified_title_ids"] = sorted(done_ids)  # legacy compat
    save_state(state_path, state)

    # --- Phase 1d: duplicate detection ---------------------------------------------
    if args.skip_hash:
        log.info("Skipping hash-based duplicate detection (--skip-hash)")
        dup_groups: List[DupGroup] = []
        # Still do tmdb-id based duplicates
        by_tmdb: Dict[int, List[TitleFolder]] = defaultdict(list)
        for t in titles:
            if t.tmdb_id:
                by_tmdb[t.tmdb_id].append(t)
        gid = 1
        for tmdb_id, ts in by_tmdb.items():
            if len(ts) < 2:
                continue
            ts_sorted = sorted(ts, key=lambda t: rank_copy(t, cfg.movies_root), reverse=True)
            keeper = ts_sorted[0]
            losers = ts_sorted[1:]
            keeper.duplicate_role = "keeper"
            for l in losers:
                l.duplicate_role = f"duplicate_of:{keeper.title_id}"
            dup_groups.append(DupGroup(
                group_id=gid,
                match_type="same-tmdb-id",
                keeper_title_id=keeper.title_id,
                duplicate_title_ids=[l.title_id for l in losers],
                keeper_path=keeper.main_video.abs_path if keeper.main_video else "",
                duplicate_paths=[l.main_video.abs_path if l.main_video else "" for l in losers],
            ))
            gid += 1
    else:
        log.info("Detecting duplicates ...")
        dup_groups = detect_duplicates(titles, hash_algo=cfg.hash_algo, movies_root=cfg.movies_root)
    log.info("Found %d duplicate groups", len(dup_groups))

    # --- Phase 1e: emit CSVs -------------------------------------------------------
    inv_path = cfg.mediaprep_dir / "inventory.csv"
    plan_path = cfg.mediaprep_dir / "plan.csv"
    unresolved_path = cfg.mediaprep_dir / "unresolved.csv"
    dup_path = cfg.mediaprep_dir / "duplicates.csv"

    write_inventory_csv(inv_path, files)
    write_plan_csv(plan_path, titles)
    write_unresolved_csv(unresolved_path, titles)
    write_duplicates_csv(dup_path, dup_groups)

    # Summary
    auto = sum(1 for t in titles if t.action == "AUTO")
    review = sum(1 for t in titles if t.action == "REVIEW")
    unresolved = sum(1 for t in titles if t.unresolved_reason)
    log.info("Summary: %d titles total | AUTO=%d  REVIEW=%d  UNRESOLVED=%d  DUP_GROUPS=%d",
             len(titles), auto, review, unresolved, len(dup_groups))
    log.info("Wrote: %s", inv_path)
    log.info("Wrote: %s", plan_path)
    log.info("Wrote: %s", unresolved_path)
    log.info("Wrote: %s", dup_path)
    log.info("Done.")
    return 0


def _tb() -> str:
    import traceback as _t
    return _t.format_exc()


if __name__ == "__main__":
    sys.exit(main())
