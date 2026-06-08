#!/usr/bin/env python3
"""
intake.py — Process newly-ripped movies from an intake directory and move
them into the main library (H:\\Movies) with the same naming, NFOs, and
sidecar handling that execute.py used for the bulk migration.

Default intake directory: H:\\Temp (configurable via config.json
`intake_dir` key or --intake-dir).

Workflow:
    1. Walk intake_dir and group video files into "titles" (one per
       movie). DVD-rip-style files (T00.mp4, ANGRY_BIRDS_Title1.mp4)
       are de-noised before TMDB lookup.
    2. For each title, query TMDB to identify the movie. NFOs in the
       intake folder (with imdb_id) take precedence over filename guesses.
    3. Show a preview table: source → target. Ask for confirmation.
    4. On approval, rename + move to the configured movies_root, write
       a rich NFO, and (optionally) trigger a Plex scan.

Usage:
    py intake.py --config config.json                  # interactive
    py intake.py --config config.json --yes            # skip confirm
    py intake.py --config config.json --dry-run        # preview only
    py intake.py --config config.json --plex-scan      # trigger Plex after
    py intake.py --config config.json --intake-dir D:\\Rips
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import os
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

sys.path.insert(0, str(Path(__file__).parent))
from execute import (  # type: ignore
    Config,
    DiskOps,
    InventoryFile,
    Journal,
    PlanRow,
    TitleResult,
    TmdbRichFetcher,
    VIDEO_EXTS,
    SUBTITLE_EXTS,
    NFO_EXTS,
    IMAGE_EXTS,
    SPECIAL_IMAGE_NAMES,
    _nfc,
    _norm_path,
    _read_csv_dicts,
    build_nfo_xml,
    hash_file,
    load_config,
    long,
    process_title,
    render_filename_template,
    same_drive,
    sanitize_for_windows,
)

import subprocess

try:
    import requests  # type: ignore
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

try:
    from guessit import guessit  # type: ignore
    _HAS_GUESSIT = True
except ImportError:
    _HAS_GUESSIT = False


VERSION = "1.0.0"
log = logging.getLogger("intake")


# ---------------------------------------------------------------------------
# DVD/BD rip filename de-noising — same patterns inventory.py uses
# ---------------------------------------------------------------------------

RIP_SUFFIX_RE = re.compile(
    r"(?:[-_\s]+(?:[ABC]\d+\s+)?[Tt]\d{2,3}"
    r"|[-_\s]+[Tt]itle[\s_]*\d+"
    r"|[-_\s]+Playlist[\s_]*\d+"
    r")\s*$",
    re.IGNORECASE,
)
QUALITY_TAG_RE = re.compile(
    r"[\s\.\-_]*(?:480p|576p|720p|1080p|2160p|4k|hdr|hdr10|dovi|"
    r"bluray|blu-ray|webdl|web-dl|webrip|web|hdrip|brrip|dvdrip|hdtv|"
    r"x264|x265|h264|h265|hevc|aac|ac3|mp3|flac|truehd|dts(?:-hd)?|"
    r"atmos|10bit|nogrp)+",
    re.IGNORECASE,
)
DVD_PREFIX_RE = re.compile(r"^[A-Z]{3,}_(?:[A-Z\d]+_)*", re.IGNORECASE)


def denoise_filename(stem: str) -> str:
    """Strip rip-tool artifacts from a stem so guessit/TMDB can match it."""
    s = stem
    s = RIP_SUFFIX_RE.sub("", s)
    s = QUALITY_TAG_RE.sub("", s)
    s = DVD_PREFIX_RE.sub("", s)
    s = re.sub(r"[\._]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or stem


# ---------------------------------------------------------------------------
# Edition / 3D variant detection
#
# Recognize Plex-style edition markers and 3D format tags in source
# filenames so two variants of the same movie (Theatrical + Extended,
# or 2D + 3D) can coexist in the same folder. The detector strips the
# markers from the stem so the TMDB lookup sees a clean title.
# ---------------------------------------------------------------------------

# Plex's own explicit edition tag — trust the user's choice verbatim.
_PLEX_EDITION_RX = re.compile(r"\{edition-([^}]+)\}", re.IGNORECASE)

# Generic edition keywords. Order matters: longer/more-specific phrases
# must come before their substrings (e.g. "Extended Director's Cut" before
# "Director's Cut" before "Extended").
_EDITION_PATTERNS: List[Tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bExtended[\s_\.]+Director'?s[\s_\.]+Cut\b", re.I), "Extended Director's Cut"),
    (re.compile(r"\bDirector'?s[\s_\.]+Cut\b",                 re.I), "Director's Cut"),
    (re.compile(r"\bUltimate[\s_\.]+Edition\b",                re.I), "Ultimate Edition"),
    (re.compile(r"\bSpecial[\s_\.]+Edition\b",                 re.I), "Special Edition"),
    (re.compile(r"\bExtended[\s_\.]+Edition\b",                re.I), "Extended"),
    (re.compile(r"\bExtended[\s_\.]+Cut\b",                    re.I), "Extended Cut"),
    (re.compile(r"\bTheatrical[\s_\.]+Cut\b",                  re.I), "Theatrical Cut"),
    (re.compile(r"\bFinal[\s_\.]+Cut\b",                       re.I), "Final Cut"),
    (re.compile(r"\bRemastered\b",                             re.I), "Remastered"),
    (re.compile(r"\bUnrated\b",                                re.I), "Unrated"),
    (re.compile(r"\bExtended\b",                               re.I), "Extended"),
    (re.compile(r"\bTheatrical\b",                             re.I), "Theatrical"),
]

# 3D format tags. Plain "3D" with no format is assumed HSBS (most common
# output of MakeMKV/Handbrake 3D encodes). Plain "SBS"/"OU" without "3D"
# context is NOT enough — too ambiguous against English words.
_THREED_FORMAT_RX = re.compile(r"\b(HSBS|FSBS|HOU|FOU)\b", re.I)
_THREED_MARKER_RX = re.compile(r"\b3D\b", re.I)

# Extras suffix — matches "Extra", "Extras", "Extra 2", "Extras 3" at the
# tail end of a stem. Picked up AFTER variant detection so it lives in
# its own dataclass.
_EXTRA_RX = re.compile(r"\bExtras?(?:\s+(\d+))?\s*$", re.IGNORECASE)


def _smart_title(s: str) -> str:
    """Title-case that preserves apostrophe-S (so "director's cut" becomes
    "Director's Cut", not "Director'S Cut" like str.title() would give).
    """
    return re.sub(r"(\w)'(\w)",
                  lambda m: m.group(1) + "'" + m.group(2).lower(),
                  s.title())


@dataclass
class VariantTags:
    edition: str = ""
    is_3d: bool = False
    threed_format: str = ""
    cleaned_stem: str = ""   # stem with markers stripped, safe for TMDB lookup


def detect_variants(stem: str) -> VariantTags:
    """Identify edition + 3D markers in a source filename stem.

    Returns the detected tags along with a 'cleaned_stem' that has the
    markers removed — feed that to guessit/TMDB so the title parsing
    doesn't think the marker is part of the title.
    """
    v = VariantTags(cleaned_stem=stem)
    work = stem

    # 1. Explicit Plex tag {edition-X} (highest precedence). Normalize
    # casing to title case so "{edition-extended}" written by the user
    # ends up as "{edition-Extended}" in the canonical filename.
    m = _PLEX_EDITION_RX.search(work)
    if m:
        v.edition = _smart_title(m.group(1).replace("_", " ").strip())
        work = _PLEX_EDITION_RX.sub(" ", work)
    else:
        # 2. Generic edition keywords.
        for rx, label in _EDITION_PATTERNS:
            if rx.search(work):
                v.edition = label
                work = rx.sub(" ", work)
                break

    # 3. 3D format tags.
    m = _THREED_FORMAT_RX.search(work)
    if m:
        v.threed_format = m.group(1).upper()
        v.is_3d = True
        work = _THREED_FORMAT_RX.sub(" ", work)
    if _THREED_MARKER_RX.search(work):
        v.is_3d = True
        if not v.threed_format:
            v.threed_format = "HSBS"  # MakeMKV/Handbrake default for 3D rips
        work = _THREED_MARKER_RX.sub(" ", work)

    # Normalize internal whitespace. Hyphens are preserved (they may be
    # part of an imdb/tmdb tag like {imdb-tt0499549}) — denoise_filename
    # handles further cleanup before guessit runs.
    work = re.sub(r"[\s\._]+", " ", work).strip(" -_.")
    v.cleaned_stem = work or stem
    return v


@dataclass
class ExtraTags:
    is_extra: bool = False
    extra_label: str = ""        # canonical label: "Extra", "Extras", "Extra 2", ...
    cleaned_stem: str = ""       # stem with the trailing extras marker removed


def detect_extra(stem: str) -> ExtraTags:
    """Detect "Extra" / "Extras" / "Extra N" at the END of a stem.

    The trailing position matters: we don't want "Extra Edition" or a
    movie literally titled "Extra Ordinary" being misclassified. Only a
    tail-position marker (just before the file extension) is treated as
    "this video is a bonus feature of the main movie".
    """
    e = ExtraTags(cleaned_stem=stem)
    m = _EXTRA_RX.search(stem)
    if not m:
        return e
    e.is_extra = True
    num = m.group(1)
    matched = m.group(0).strip()
    plural = matched.lower().startswith("extras")
    if num:
        # "Extra 2" / "Extras 2" → "Extra 2" (singular form looks cleaner
        # when numbered).
        e.extra_label = f"Extra {num}"
    else:
        # Preserve the user's choice of "Extra" vs "Extras" when unnumbered.
        e.extra_label = "Extras" if plural else "Extra"
    e.cleaned_stem = _EXTRA_RX.sub("", stem).strip(" -_.")
    return e


def template_for_variant(base_template: str, edition: str,
                         is_3d: bool, threed_format: str) -> str:
    """Add Plex {edition-X} and ' - 3D.FMT' tags to a filename template.

    The edition tag is inserted BEFORE the imdb/tmdb tag (Plex prefers
    that order for its scanner). The 3D format suffix is appended at
    the very end, just before the extension is added by the caller.
    """
    out = base_template
    if edition:
        edition_tag = "{{edition-" + edition + "}}"
        # The base template ends with "{{imdb-...}}" or "{{tmdb-...}}".
        # Insert the edition tag immediately before that final id tag.
        anchor = out.rfind("{{")
        if anchor > 0:
            out = out[:anchor] + edition_tag + " " + out[anchor:]
        else:
            out = out + " " + edition_tag
    if is_3d:
        out = out + " - 3D." + (threed_format or "HSBS")
    return out


# ---------------------------------------------------------------------------
# Quality probe — ffprobe a video file and return its quality fingerprint
# ---------------------------------------------------------------------------

def ffprobe_video_quality(path: Path, timeout: int = 60) -> Optional[Dict[str, Any]]:
    """Return {width, height, codec, bit_rate, duration_s, size_bytes}, or None."""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,codec_name,bit_rate:format=duration,bit_rate,size",
        "-of", "json",
        long(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return None
    streams = data.get("streams") or [{}]
    fmt = data.get("format") or {}
    s = streams[0] if streams else {}
    try:
        size_bytes = int(fmt.get("size", 0) or 0) or os.path.getsize(long(path))
    except OSError:
        size_bytes = 0
    try:
        duration_s = float(fmt.get("duration") or 0)
    except (ValueError, TypeError):
        duration_s = 0.0
    try:
        bit_rate = int(s.get("bit_rate") or fmt.get("bit_rate") or 0)
    except (ValueError, TypeError):
        bit_rate = 0
    # If bit_rate is missing, derive from size + duration
    if not bit_rate and duration_s > 0:
        bit_rate = int((size_bytes * 8) / duration_s)
    try:
        width = int(s.get("width") or 0)
        height = int(s.get("height") or 0)
    except (ValueError, TypeError):
        width = height = 0
    return {
        "width": width,
        "height": height,
        "codec": s.get("codec_name", "") or "",
        "bit_rate": bit_rate,
        "duration_s": duration_s,
        "size_bytes": size_bytes,
    }


def quality_score(info: Dict[str, Any]) -> Tuple[int, int, int]:
    """Score tuple for sorting; higher is better."""
    pixels = (info.get("width", 0) or 0) * (info.get("height", 0) or 0)
    return (pixels, info.get("bit_rate", 0) or 0, info.get("size_bytes", 0) or 0)


def quality_summary(info: Optional[Dict[str, Any]]) -> str:
    if not info:
        return "(ffprobe failed)"
    w = info.get("width", 0)
    h = info.get("height", 0)
    codec = info.get("codec") or "?"
    kbps = (info.get("bit_rate", 0) or 0) // 1000
    mb = (info.get("size_bytes", 0) or 0) / (1024 * 1024)
    return f"{w}x{h} {codec} {kbps} kbps, {mb:.0f} MB"


# Quality decision constants
QUALITY_KEEP_EXISTING = "keep_existing"   # new file → duplicates
QUALITY_REPLACE       = "replace"          # existing → duplicates, new in
QUALITY_UNDECIDED     = "undecided"        # ffprobe failed; fall back to skip


def compare_quality(new_path: Path, existing_path: Path
                    ) -> Tuple[str, Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Decide which of two videos is higher quality.

    Returns (decision, new_info, existing_info). Decision is one of the
    QUALITY_* constants above. When undecided, callers should fall back
    to the default (skip the new file) for safety.

    Heuristic: compare (pixels, bit_rate, size_bytes) as a tuple. Same
    resolution + same bitrate within 5% → keep existing (no point churning).
    """
    new_info = ffprobe_video_quality(new_path)
    ex_info = ffprobe_video_quality(existing_path)
    if not new_info or not ex_info:
        return (QUALITY_UNDECIDED, new_info, ex_info)

    new_score = quality_score(new_info)
    ex_score = quality_score(ex_info)

    # If resolutions differ, that's the decisive factor.
    if new_score[0] != ex_score[0]:
        return (QUALITY_REPLACE if new_score[0] > ex_score[0]
                else QUALITY_KEEP_EXISTING, new_info, ex_info)

    # Same resolution. Look at bitrate next, with a 5% deadband to avoid
    # churn on near-identical files.
    new_br = new_score[1]
    ex_br = ex_score[1]
    if ex_br > 0:
        diff_pct = (new_br - ex_br) / ex_br
        if abs(diff_pct) < 0.05:
            return (QUALITY_KEEP_EXISTING, new_info, ex_info)
    if new_br > ex_br:
        return (QUALITY_REPLACE, new_info, ex_info)
    return (QUALITY_KEEP_EXISTING, new_info, ex_info)


# ---------------------------------------------------------------------------
# NFO parser — read existing intake NFO for imdb_id hint
# ---------------------------------------------------------------------------

def read_nfo_imdb(path: Path) -> Tuple[str, str]:
    """Return (imdb_id, tmdb_id) from an NFO. Empty strings if not found."""
    try:
        with open(long(path), "rb") as f:
            data = f.read()
    except OSError:
        return ("", "")
    # XML first
    try:
        root = ET.fromstring(data)
        imdb = ""
        tmdb = ""
        for tag in ("imdbid", "imdb_id"):
            e = root.find(tag)
            if e is not None and e.text:
                imdb = e.text.strip()
                break
        if not imdb:
            e = root.find("id")
            if e is not None and e.text and e.text.strip().startswith("tt"):
                imdb = e.text.strip()
        e = root.find("tmdbid")
        if e is not None and e.text:
            tmdb = e.text.strip()
        if imdb or tmdb:
            return (imdb, tmdb)
    except ET.ParseError:
        pass
    # Fallback: grep for tt-id and TMDB url
    text = data.decode("utf-8", errors="replace")
    m = re.search(r"\b(tt\d{7,10})\b", text)
    imdb = m.group(1) if m else ""
    m = re.search(r"themoviedb\.org/movie/(\d+)", text)
    tmdb = m.group(1) if m else ""
    return (imdb, tmdb)


# ---------------------------------------------------------------------------
# TMDB lookup — simple, uses the same SQLite cache execute.py uses
# ---------------------------------------------------------------------------

class TmdbClient:
    BASE = "https://api.themoviedb.org/3"

    def __init__(self, api_key: str, cache_path: Path, language: str = "en-US"):
        self.api_key = api_key
        self.lang = language
        self.cache_path = cache_path
        self._db = sqlite3.connect(str(cache_path))
        self._db.execute("""CREATE TABLE IF NOT EXISTS rich_movie (
            tmdb_id INTEGER PRIMARY KEY, language TEXT, json TEXT, fetched_at TEXT)""")
        self._db.execute("""CREATE TABLE IF NOT EXISTS search_movie (
            query TEXT PRIMARY KEY, json TEXT, fetched_at TEXT)""")
        self._db.commit()

    def _get(self, url: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not _HAS_REQUESTS:
            return None
        for attempt in range(4):
            try:
                r = requests.get(url, params=params, timeout=20)
                if r.status_code == 429:
                    wait = int(r.headers.get("Retry-After", "5"))
                    time.sleep(wait)
                    continue
                if r.status_code == 404:
                    return None
                r.raise_for_status()
                return r.json()
            except requests.RequestException as e:
                log.warning("TMDB %s attempt %d failed: %s", url, attempt + 1, e)
                time.sleep(2 ** attempt)
        return None

    def search(self, title: str, year: str = "") -> List[Dict[str, Any]]:
        cache_key = f"{title}|{year}|{self.lang}"
        row = self._db.execute(
            "SELECT json FROM search_movie WHERE query=?", (cache_key,)
        ).fetchone()
        if row:
            try:
                return json.loads(row[0]).get("results", [])
            except json.JSONDecodeError:
                pass
        params = {"api_key": self.api_key, "language": self.lang, "query": title}
        if year:
            params["year"] = year
        data = self._get(f"{self.BASE}/search/movie", params)
        if data is None:
            return []
        self._db.execute(
            "INSERT OR REPLACE INTO search_movie (query, json, fetched_at) VALUES (?,?,?)",
            (cache_key, json.dumps(data, ensure_ascii=False),
             dt.datetime.now(dt.timezone.utc).isoformat()),
        )
        self._db.commit()
        return data.get("results", [])

    def find_by_imdb(self, imdb_id: str) -> Optional[Dict[str, Any]]:
        if not imdb_id:
            return None
        params = {"api_key": self.api_key, "external_source": "imdb_id"}
        data = self._get(f"{self.BASE}/find/{imdb_id}", params)
        if not data:
            return None
        results = data.get("movie_results", [])
        return results[0] if results else None

    def get_full(self, tmdb_id: int) -> Optional[Dict[str, Any]]:
        row = self._db.execute(
            "SELECT json FROM rich_movie WHERE tmdb_id=? AND language=?",
            (tmdb_id, self.lang)
        ).fetchone()
        if row:
            try:
                cached = json.loads(row[0])
                # The shared tmdb_cache.sqlite may have been populated by
                # execute.py's TmdbRichFetcher, which doesn't include
                # external_ids. If imdb_id is missing, fetch a /external_ids
                # supplement and merge.
                ext = cached.get("external_ids") or {}
                if ext.get("imdb_id"):
                    return cached
                # Try the dedicated external_ids endpoint as a cheap top-up.
                ext_url = f"{self.BASE}/movie/{tmdb_id}/external_ids"
                ext_data = self._get(ext_url, {"api_key": self.api_key})
                if ext_data and ext_data.get("imdb_id"):
                    cached["external_ids"] = ext_data
                    self._db.execute(
                        "INSERT OR REPLACE INTO rich_movie (tmdb_id, language, json, fetched_at) VALUES (?,?,?,?)",
                        (tmdb_id, self.lang,
                         json.dumps(cached, ensure_ascii=False),
                         dt.datetime.now(dt.timezone.utc).isoformat()),
                    )
                    self._db.commit()
                    return cached
                return cached
            except json.JSONDecodeError:
                pass
        params = {"api_key": self.api_key, "language": self.lang,
                  "append_to_response": "release_dates,credits,external_ids"}
        data = self._get(f"{self.BASE}/movie/{tmdb_id}", params)
        if data is None:
            return None
        self._db.execute(
            "INSERT OR REPLACE INTO rich_movie (tmdb_id, language, json, fetched_at) VALUES (?,?,?,?)",
            (tmdb_id, self.lang, json.dumps(data, ensure_ascii=False),
             dt.datetime.now(dt.timezone.utc).isoformat()),
        )
        self._db.commit()
        return data

    def close(self) -> None:
        try:
            self._db.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Title grouping in the intake dir
# ---------------------------------------------------------------------------

@dataclass
class IntakeTitle:
    folder: Path                     # the title's effective folder (parent of files)
    main_video: Path                 # the largest video found
    files: List[Path] = field(default_factory=list)
    guess_title: str = ""
    guess_year: str = ""
    nfo_imdb_id: str = ""
    nfo_tmdb_id: str = ""
    # After identification:
    tmdb_id: str = ""
    imdb_id: str = ""
    matched_title: str = ""
    matched_year: str = ""
    collection_name: str = ""
    confidence: int = 0
    target_folder: Path = field(default_factory=lambda: Path(""))
    note: str = ""
    candidates: List[Dict[str, Any]] = field(default_factory=list)  # top alternatives
    # Variant tags detected in the source filename (see detect_variants).
    # When set, the target VIDEO filename gets Plex-style suffixes; the
    # folder leaf itself stays "clean" so Plex groups variants together.
    edition: str = ""           # "Theatrical", "Extended", "Director's Cut", ...
    is_3d: bool = False
    threed_format: str = ""     # "HSBS", "HOU", "FSBS"
    is_3d_archive: bool = False  # 3D .mkv source — routes to flat 3D-MKV archive
    # Extras (bonus features) get routed into Movie\Extras\ where Plex
    # auto-recognizes them as extras for the parent movie.
    is_extra: bool = False
    extra_label: str = ""        # "Extra", "Extras", "Extra 2", ...
    final_video_name: str = ""  # the eventual filename inside target_folder


def group_intake(intake_dir: Path) -> List[IntakeTitle]:
    """Walk intake_dir and create one IntakeTitle per video file.

    Each video is its own title — even if multiple videos share a folder,
    they're treated as distinct movies (you ripped a Bond box set, each
    disc is a movie). Sidecars are non-video files in the same folder
    whose name starts with the video's stem (case-insensitive).

    NOTE: walks WITHOUT the long-path prefix so f.parent comparisons work.
    Long-path is only needed for individual file ops, not directory walks.
    """
    if not intake_dir.exists():
        return []

    # Walk with str(intake_dir), NOT long(intake_dir) — the long-path
    # prefix in os.walk's `root` propagates to every joined path and breaks
    # parent equality checks.
    all_files: List[Path] = []
    for root, dirs, files in os.walk(str(intake_dir)):
        for fn in files:
            all_files.append(_norm_path(os.path.join(root, fn)))

    # Group all files by parent folder for fast sidecar lookup
    by_parent: Dict[Path, List[Path]] = {}
    for f in all_files:
        by_parent.setdefault(f.parent, []).append(f)

    titles: List[IntakeTitle] = []
    for parent, files_in_dir in by_parent.items():
        videos = [f for f in files_in_dir if f.suffix.lower() in VIDEO_EXTS]
        for v in videos:
            stem_low = v.stem.lower()
            sidecars: List[Path] = [v]
            for f in files_in_dir:
                if f == v:
                    continue
                # Other videos in the same folder are SEPARATE titles, not
                # sidecars of this one. Don't claim them.
                if f.suffix.lower() in VIDEO_EXTS:
                    continue
                if f.name.lower().startswith(stem_low):
                    sidecars.append(f)
            titles.append(IntakeTitle(folder=parent, main_video=v,
                                      files=sidecars))
    return titles


# ---------------------------------------------------------------------------
# Identification
# ---------------------------------------------------------------------------

_IMDB_IN_FILENAME_RX = re.compile(r"\{imdb-(tt\d{7,10})\}", re.IGNORECASE)
_TMDB_IN_FILENAME_RX = re.compile(r"\{tmdb-(\d+)\}", re.IGNORECASE)


def identify_title(t: IntakeTitle, tmdb: TmdbClient) -> None:
    """Populate t with TMDB-derived identification."""
    # 0a. Detect edition / 3D markers up front. The IMDB tag detection below
    # operates on the full filename and still finds {imdb-tt...} correctly;
    # the cleaned stem is only used for the guessit fallback path.
    v = detect_variants(t.main_video.stem)
    t.edition = v.edition
    t.is_3d = v.is_3d
    t.threed_format = v.threed_format
    # 3D source .mkv files go to the flat archive root, not the Plex tree.
    t.is_3d_archive = v.is_3d and t.main_video.suffix.lower() == ".mkv"

    # 0b. Detect trailing "Extra" / "Extras" / "Extra N" — bonus features
    # belong inside Movie\Extras\ where Plex auto-recognizes them.
    # Extras take precedence over 3D-archive routing (a .mkv extra still
    # belongs in the movie folder's Extras\, not flat 3D-MKV).
    e = detect_extra(v.cleaned_stem)
    if e.is_extra:
        t.is_extra = True
        t.extra_label = e.extra_label
        t.is_3d_archive = False
        cleaned_for_lookup = e.cleaned_stem
    else:
        cleaned_for_lookup = v.cleaned_stem

    # 1a. IMDB id already in the filename? (e.g. "Movie (Year) {imdb-tt1234567}.mp4")
    # This is the most reliable signal — if someone tagged the filename with
    # an IMDB id, trust it. Never fall through to guessit, even if the TMDB
    # lookup fails — the user's intent is clear, and falling through risks
    # producing a wrong fuzzy match.
    m = _IMDB_IN_FILENAME_RX.search(t.main_video.name)
    if m:
        imdb_from_name = m.group(1).lower()
        if imdb_from_name.startswith("tt"):
            match = tmdb.find_by_imdb(imdb_from_name)
            if match:
                _apply_match(t, match, tmdb, confidence=95)
                t.note = f"matched via filename imdb {imdb_from_name}"
                return
            # Lookup failed. Don't fall through to guessit — the filename
            # was explicit. Mark as REVIEW with a clear diagnostic.
            t.imdb_id = imdb_from_name
            t.confidence = 0
            t.note = (f"IMDB id {imdb_from_name} from filename did not resolve "
                      f"to a TMDB record (transient API error, or this id "
                      f"isn't in TMDB's database — verify on themoviedb.org)")
            return

    # 1b. TMDB id in the filename? Same stubborn behavior.
    m = _TMDB_IN_FILENAME_RX.search(t.main_video.name)
    if m:
        tmdb_from_name = m.group(1)
        try:
            full = tmdb.get_full(int(tmdb_from_name))
        except (ValueError, sqlite3.Error):
            full = None
        if full:
            synth = {
                "id": full.get("id"),
                "title": full.get("title"),
                "release_date": full.get("release_date"),
            }
            _apply_match(t, synth, tmdb, confidence=95)
            t.note = f"matched via filename tmdb {tmdb_from_name}"
            return
        t.tmdb_id = tmdb_from_name
        t.confidence = 0
        t.note = (f"TMDB id {tmdb_from_name} from filename did not resolve "
                  f"(transient API error, or removed from TMDB)")
        return

    # 1c. NFO in intake folder?
    nfo_path = next((f for f in t.files if f.suffix.lower() == ".nfo"), None)
    if nfo_path:
        imdb, tmdb_id = read_nfo_imdb(nfo_path)
        t.nfo_imdb_id = imdb
        t.nfo_tmdb_id = tmdb_id

    # 1d. Try the NFO IMDB id
    if t.nfo_imdb_id:
        match = tmdb.find_by_imdb(t.nfo_imdb_id)
        if match:
            _apply_match(t, match, tmdb, confidence=95)
            t.note = f"matched via NFO imdb {t.nfo_imdb_id}"
            return

    # 3. Use guessit on the de-noised, variant-stripped, extras-stripped
    # stem so "Theatrical" / "3D" / "Extra" markers don't bleed into the
    # TMDB title query.
    stem = denoise_filename(cleaned_for_lookup)
    if _HAS_GUESSIT:
        try:
            g = guessit(stem, options={"type": "movie"})
            t.guess_title = str(g.get("title", "")).strip()
            yr = g.get("year")
            t.guess_year = str(yr) if yr else ""
        except Exception as e:
            log.warning("guessit failed on %s: %s", stem, e)
            t.guess_title = stem
            t.guess_year = ""
    else:
        # Fallback: take everything before the first 4-digit year, or the whole stem
        m = re.search(r"\b(19|20)\d{2}\b", stem)
        if m:
            t.guess_title = stem[:m.start()].strip()
            t.guess_year = m.group(0)
        else:
            t.guess_title = stem
            t.guess_year = ""

    # 4. Search TMDB
    if not t.guess_title:
        t.note = "could not extract title from filename"
        t.confidence = 0
        return

    results = tmdb.search(t.guess_title, t.guess_year)
    if not results and t.guess_year:
        # Retry without year
        results = tmdb.search(t.guess_title)

    if not results:
        t.note = "no TMDB matches"
        t.confidence = 0
        return

    best = results[0]
    best_title = (best.get("title") or "").strip()
    title_exact = best_title.lower() == t.guess_title.strip().lower()
    n = len(results)

    # Scoring philosophy: single-result matches are reliable; multi-result
    # no-year matches are NOT (TMDB sorts by popularity, biased toward
    # recent films). When the user has a year in the filename, we can
    # disambiguate; without it, we should not auto-process ambiguous matches.
    if t.guess_year:
        rel_year = (best.get("release_date") or "")[:4]
        if rel_year == t.guess_year:
            confidence = 95 if n <= 3 else 85
        else:
            # Tolerate off-by-one year (release-year vs production-year
            # discrepancy, country-of-release timing, etc.) — still a strong
            # signal if the title also matches exactly.
            try:
                year_delta = abs(int(rel_year) - int(t.guess_year))
            except (ValueError, TypeError):
                year_delta = 99
            if year_delta <= 1 and title_exact:
                confidence = 85 if n <= 3 else 75
            else:
                confidence = 60  # bigger mismatch — likely wrong record
    else:
        if n == 1:
            confidence = 80          # only candidate; trust it
        elif title_exact and n <= 3:
            confidence = 75          # exact match, low ambiguity
        elif title_exact:
            confidence = 60          # exact match BUT many candidates — risky
        else:
            confidence = 40          # ambiguous on every axis

    _apply_match(t, best, tmdb, confidence=confidence)

    # Save up to 5 alternatives so the preview can show them and the user
    # can spot a wrong-era match (e.g. 2025 Superman vs 1978 Superman).
    t.candidates = results[:5]
    if n > 1:
        t.note = (f"TMDB returned {n} candidates; chose #1"
                  + (" (exact title match)" if title_exact else ""))


def _apply_match(t: IntakeTitle, search_result: Dict[str, Any],
                 tmdb: TmdbClient, confidence: int) -> None:
    t.tmdb_id = str(search_result.get("id") or "")
    t.matched_title = search_result.get("title") or ""
    rel = search_result.get("release_date") or ""
    t.matched_year = rel[:4] if rel else ""
    t.confidence = confidence
    # Fetch full record for IMDB id and collection
    if t.tmdb_id:
        try:
            full = tmdb.get_full(int(t.tmdb_id))
        except (ValueError, sqlite3.Error):
            full = None
        if full:
            ext = full.get("external_ids") or {}
            t.imdb_id = ext.get("imdb_id") or t.imdb_id
            coll = full.get("belongs_to_collection") or {}
            if coll.get("name"):
                t.collection_name = coll["name"]
                if t.collection_name.lower().endswith(" collection"):
                    t.collection_name = t.collection_name[: -len(" collection")].strip()


# ---------------------------------------------------------------------------
# Target path computation (matches inventory.py / execute.py convention)
# ---------------------------------------------------------------------------

def compute_target(t: IntakeTitle, cfg: Config) -> Path:
    """Compute target_folder using the same template as the bulk migration.

    Also populates t.final_video_name, which is the eventual filename of
    the video inside target_folder. For plain 2D movies the filename is
    the same as the folder leaf; for editions and 3D versions it picks
    up Plex-style {edition-X} and ' - 3D.HSBS' suffixes so multiple
    variants of the same movie can live in the same folder and be
    grouped by Plex as one library entry.
    """
    if not t.matched_title or not t.matched_year:
        t.final_video_name = ""
        return Path("")

    # Base template — either imdb-tagged (preferred) or tmdb fallback.
    if not t.imdb_id:
        base_template = "{title} ({year}) {{tmdb-{tmdb_id}}}"
    else:
        base_template = cfg.filename_template

    fields = {
        "title": t.matched_title,
        "year": t.matched_year,
        "imdb_id": t.imdb_id,
        "tmdb_id": t.tmdb_id,
        "matched_title_lowercase": t.matched_title.lower(),
    }

    # Clean folder leaf — same for every variant of a given movie. This is
    # what causes Plex to group Theatrical + Extended + 3D under one entry.
    folder_leaf = render_filename_template(base_template, fields)
    ext = t.main_video.suffix.lower()

    # Compute the movie's home folder once; extras and 3D-archive routing
    # may override it but the canonical movie folder is still used as the
    # anchor for Extras\.
    if t.collection_name:
        franchise = sanitize_for_windows(t.collection_name)
        movie_folder = _norm_path(str(cfg.movies_root) + "\\"
                                  + franchise + "\\" + folder_leaf)
    else:
        movie_folder = _norm_path(str(cfg.movies_root) + "\\" + folder_leaf)

    # ---- Extras branch -------------------------------------------------
    # Bonus features land inside Movie\Extras\ with the Plex-recommended
    # "<Title> (<Year>) - <Label>.<ext>" name. No NFO, no edition/3D tags
    # — extras are extras.
    if t.is_extra:
        extras_folder = _norm_path(str(movie_folder) + "\\Extras")
        extra_stem = (f"{t.matched_title} ({t.matched_year}) "
                      f"- {t.extra_label}")
        t.final_video_name = sanitize_for_windows(extra_stem) + ext
        return extras_folder

    # ---- Variant-tagged video filename --------------------------------
    video_template = template_for_variant(base_template,
                                          t.edition, t.is_3d, t.threed_format)
    video_stem = render_filename_template(video_template, fields)
    t.final_video_name = sanitize_for_windows(video_stem) + ext

    # ---- 3D archive routing -------------------------------------------
    # 3D source MKVs are kept outside the Plex Movies tree so Plex never
    # tries to index them. Flat layout per user preference: every archive
    # mkv lives directly under the configured archive root.
    if t.is_3d_archive:
        threed_root_str = cfg.raw.get("threed_mkv_root") or "H:/3D MKV"
        return _norm_path(threed_root_str)

    return movie_folder


# ---------------------------------------------------------------------------
# Plan-row + inventory adapter so we can call execute.py's process_title
# ---------------------------------------------------------------------------

def build_plan_row(t: IntakeTitle) -> PlanRow:
    return PlanRow(
        title_id=hash(str(t.main_video)) & 0x7FFFFFFF,  # synthetic id
        source_folder=t.folder,
        main_video_file=t.main_video.name,
        main_video_path=t.main_video,
        tmdb_id=t.tmdb_id,
        imdb_id=t.imdb_id,
        matched_title=t.matched_title,
        matched_year=t.matched_year,
        collection_name=t.collection_name,
        runtime_file_min="",
        runtime_tmdb_min="",
        confidence=t.confidence,
        target_folder=t.target_folder,
        action="AUTO",
        duplicate_role="",
        notes=t.note,
        flags=[],
        executed_at="",
        raw={},
    )


def build_inv_files(t: IntakeTitle) -> List[InventoryFile]:
    out: List[InventoryFile] = []
    for f in t.files:
        try:
            sz = os.path.getsize(long(f))
        except OSError:
            sz = 0
        role = "main_video" if f == t.main_video else _classify_role(f)
        out.append(InventoryFile(
            file_id=hash(str(f)) & 0x7FFFFFFF,
            abs_path=f,
            parent_folder=f.parent,
            filename=f.name,
            ext=f.suffix.lower(),
            size_bytes=sz,
            role=role,
        ))
    return out


def _classify_role(f: Path) -> str:
    ext = f.suffix.lower()
    nm = f.name.lower()
    if ext in NFO_EXTS:
        return "nfo"
    if ext in SUBTITLE_EXTS:
        return "subtitle"
    if ext in IMAGE_EXTS:
        if "fanart" in nm or "backdrop" in nm:
            return "fanart"
        if "poster" in nm:
            return "poster"
        return "image"
    if ext in VIDEO_EXTS:
        return "extras"
    return "other"


# ---------------------------------------------------------------------------
# Preview & confirmation
# ---------------------------------------------------------------------------

def print_preview(titles: List[IntakeTitle], cfg: Config) -> None:
    print()
    print("=" * 100)
    print(f"INTAKE PREVIEW — {len(titles)} title(s) found")
    print("=" * 100)
    for i, t in enumerate(titles, 1):
        status = "OK" if t.matched_title and t.imdb_id else "REVIEW"
        print(f"\n[{i}] {status}  conf={t.confidence}")
        print(f"    Source : {t.main_video}")
        if t.matched_title:
            yr = t.matched_year or "?"
            print(f"    Match  : {t.matched_title} ({yr})  tmdb={t.tmdb_id}  imdb={t.imdb_id}")
            if t.collection_name:
                print(f"    Set    : {t.collection_name}")
            # Variant line — only shown when something was detected, so
            # plain 2D movies stay uncluttered in the preview.
            variant_bits: List[str] = []
            if t.edition:
                variant_bits.append(f"edition={t.edition}")
            if t.is_3d:
                variant_bits.append(f"3D.{t.threed_format or 'HSBS'}")
            if t.is_3d_archive:
                variant_bits.append("source-archive (.mkv)")
            if t.is_extra:
                variant_bits.append(f"extra ({t.extra_label})")
            if variant_bits:
                print(f"    Variant: {', '.join(variant_bits)}")
            print(f"    Target : {t.target_folder}\\{t.final_video_name}")
        else:
            print(f"    Match  : (none)  guess={t.guess_title!r} year={t.guess_year}")
        if t.note:
            print(f"    Note   : {t.note}")
        # Sidecar count
        sidecars = [f for f in t.files if f != t.main_video]
        if sidecars:
            print(f"    Sidecars ({len(sidecars)}): " +
                  ", ".join(f.name for f in sidecars[:5]) +
                  (" ..." if len(sidecars) > 5 else ""))
        # Show alternative TMDB candidates when the match is ambiguous
        if len(t.candidates) > 1:
            print(f"    Other TMDB candidates (verify the match above is right!):")
            for c in t.candidates[1:5]:
                yr = (c.get("release_date") or "")[:4] or "?"
                print(f"      - {c.get('title','')!r} ({yr})  tmdb={c.get('id')}")
    print()
    print("=" * 100)


def prompt_yes(question: str, default_no: bool = True) -> bool:
    suffix = " [y/N]: " if default_no else " [Y/n]: "
    try:
        r = input(question + suffix).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if not r:
        return not default_no
    return r in ("y", "yes")


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

def setup_logging(mediaprep: Path, run_id: str) -> Path:
    mediaprep.mkdir(parents=True, exist_ok=True)
    log_path = mediaprep / f"intake_{run_id}.log"
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    log.handlers.clear()
    log.setLevel(logging.DEBUG)
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(logging.Formatter(fmt))
    log.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter("%(message)s"))
    sh.setLevel(logging.INFO)
    log.addHandler(sh)
    return log_path


def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="intake.py",
        description="Process newly-ripped movies from an intake directory.",
    )
    p.add_argument("--config", default="config.json")
    p.add_argument("--intake-dir", default="",
                   help="override intake_dir (default: from config.json or H:\\Temp)")
    p.add_argument("--dry-run", action="store_true",
                   help="show what would happen, don't move anything")
    p.add_argument("--yes", action="store_true",
                   help="skip the confirmation prompt")
    p.add_argument("--force-low-confidence", action="store_true",
                   help="process titles with confidence < 70 too (default: skip them)")
    p.add_argument("--replace-existing", action="store_true",
                   help="overwrite the destination even if quality check is inconclusive "
                        "(default: skip when ffprobe can't decide)")
    p.add_argument("--no-quality-check", action="store_true",
                   help="disable ffprobe-based quality comparison for EXISTS collisions; "
                        "fall back to old skip-by-default behavior")
    p.add_argument("--plex-scan", action="store_true",
                   help="trigger Plex Movies library scan after success")
    p.add_argument("--no-nfo", action="store_true",
                   help="skip NFO writing")
    p.add_argument("--keep-source", action="store_true",
                   help="don't rmdir the empty intake source after move")
    p.add_argument("--with-art", action="store_true",
                   help="after processing, fetch posters/fanart/landscape/logo from TMDB")
    p.add_argument("--with-clearart", action="store_true",
                   help="after processing, fetch clearart/clearlogo from Fanart.tv "
                        "(requires fanart_tv_api_key in config.json)")
    p.add_argument("--with-trailer", action="store_true",
                   help="after processing, download a trailer via TMDB+YouTube (requires yt-dlp)")
    p.add_argument("--full", action="store_true",
                   help="shorthand for --with-art --with-clearart --with-trailer --plex-scan")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        cfg = load_config(_norm_path(args.config))
    except Exception as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 3

    if args.no_nfo:
        cfg.nfo_richness = "off"
    if args.keep_source:
        cfg.rmdir_empty_sources = False

    intake_dir = _norm_path(args.intake_dir or cfg.raw.get("intake_dir", "H:/Temp"))
    if not intake_dir.exists():
        print(f"Intake directory does not exist: {intake_dir}", file=sys.stderr)
        return 4

    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    log_path = setup_logging(cfg.mediaprep_dir, run_id)
    log.info("intake.py v%s — scanning %s", VERSION, intake_dir)

    if not _HAS_GUESSIT:
        log.warning("guessit not installed — falling back to regex parsing. "
                    "Install with: pip install guessit")
    if not _HAS_REQUESTS:
        log.error("requests not installed — TMDB lookups will all fail. "
                  "Install with: pip install requests")
        return 4
    if not cfg.tmdb_api_key or cfg.tmdb_api_key == "REPLACE_ME":
        log.error("TMDB API key not configured in config.json")
        return 4

    # 1. Walk + group
    titles = group_intake(intake_dir)
    if not titles:
        log.info("No video files found in %s. Nothing to do.", intake_dir)
        return 0
    log.info("Found %d title(s).", len(titles))

    # 2. Identify
    tmdb = TmdbClient(cfg.tmdb_api_key, cfg.mediaprep_dir / "tmdb_cache.sqlite",
                      cfg.language)
    try:
        for t in titles:
            log.info("Identifying: %s", t.main_video.name)
            identify_title(t, tmdb)
            t.target_folder = compute_target(t, cfg)
    finally:
        tmdb.close()

    # 3a. Detect in-batch collisions — multiple titles in this batch resolving
    # to the same target FILE (folder + filename). Different editions or
    # 3D variants of the same movie share a folder but have distinct
    # filenames, so they should NOT collide; the key includes final_video_name.
    target_groups: Dict[str, List[IntakeTitle]] = {}
    for t in titles:
        if not t.target_folder or str(t.target_folder) in ("", ".") \
                or not t.final_video_name:
            continue
        key = (str(t.target_folder) + "\\" + t.final_video_name).lower()
        target_groups.setdefault(key, []).append(t)
    collided: Set[int] = set()
    for path, group in target_groups.items():
        if len(group) > 1:
            for t in group:
                t.note = (t.note + " | " if t.note else "") + \
                         f"COLLISION (in-batch): {len(group)} titles resolve to same target"
                t.confidence = min(t.confidence, 30)
                collided.add(id(t))
    if collided:
        log.warning("Detected %d title(s) involved in in-batch collisions; "
                    "they will be skipped.", len(collided))

    # 3b. Detect against-existing collisions — the title's target folder
    # already contains a video file from a previous run or the bulk
    # migration. For each, run a quality comparison (ffprobe both files)
    # and tag the decision so phase 4 can route correctly.
    existing_collisions: Set[int] = set()
    # decision tracker: id(t) -> (decision, existing_video_path)
    quality_decisions: Dict[int, Tuple[str, Path]] = {}
    for t in titles:
        if id(t) in collided or not t.target_folder or not t.final_video_name:
            continue
        # 3D archives and extras don't participate in the quality-comparison
        # flow. They have a dedicated mover; if the destination is already
        # taken the move will fail loudly with a clear log line.
        if t.is_3d_archive or t.is_extra:
            continue
        if not t.target_folder.exists():
            continue
        # EXISTS fires only on EXACT filename match. Other unrelated videos
        # in the same folder (e.g. a pre-existing Theatrical when we're
        # adding Extended) are fine — they're distinct variants.
        target_video_path = t.target_folder / t.final_video_name
        try:
            if not target_video_path.exists():
                continue
        except OSError:
            continue

        existing_video = target_video_path
        existing_collisions.add(id(t))

        if args.no_quality_check:
            t.note = (t.note + " | " if t.note else "") + \
                     f"EXISTS: target already has '{existing_video.name}' " \
                     f"(use --replace-existing to overwrite)"
            quality_decisions[id(t)] = (QUALITY_UNDECIDED, existing_video)
            continue

        decision, new_info, ex_info = compare_quality(t.main_video, existing_video)
        quality_decisions[id(t)] = (decision, existing_video)
        new_q = quality_summary(new_info)
        ex_q = quality_summary(ex_info)
        if decision == QUALITY_REPLACE:
            t.note = (t.note + " | " if t.note else "") + \
                     f"EXISTS (quality: NEW WINS): new={new_q}; existing={ex_q}. " \
                     f"New replaces existing; existing → H:\\duplicates\\intake_replaced\\"
        elif decision == QUALITY_KEEP_EXISTING:
            t.note = (t.note + " | " if t.note else "") + \
                     f"EXISTS (quality: KEEP EXISTING): new={new_q}; existing={ex_q}. " \
                     f"New → H:\\duplicates\\intake_replaced\\"
        else:
            t.note = (t.note + " | " if t.note else "") + \
                     f"EXISTS (quality: UNDECIDED — ffprobe failed on one side): " \
                     f"target already has '{existing_video.name}'. Defaulting to skip; " \
                     f"use --replace-existing to overwrite anyway."

    if existing_collisions:
        log.info("Detected %d title(s) that already exist at the target. "
                 "Quality auto-decision is active; see EXISTS notes in preview.",
                 len(existing_collisions))

    # 4. Filter low-confidence unless forced. In-batch collisions are NEVER
    # auto-processed. Existing-target collisions follow the quality decision:
    #   - REPLACE (new is better) → auto-process with archive-then-install
    #   - KEEP_EXISTING (existing is better or equal) → route source to dups
    #   - UNDECIDED (ffprobe failed) → skip unless --replace-existing
    auto_titles = []
    for t in titles:
        if not t.matched_title:
            continue
        if id(t) in collided:
            continue
        if t.confidence < 70 and not args.force_low_confidence:
            continue
        if id(t) in existing_collisions:
            decision, _ = quality_decisions.get(id(t), (QUALITY_UNDECIDED, None))
            if decision == QUALITY_REPLACE:
                pass  # let through; phase 7 will archive existing first
            elif decision == QUALITY_KEEP_EXISTING:
                pass  # let through; phase 7 will route source to dups
            elif decision == QUALITY_UNDECIDED and not args.replace_existing:
                continue
            elif decision == QUALITY_UNDECIDED and args.replace_existing:
                # Treat as a forced replace
                quality_decisions[id(t)] = (QUALITY_REPLACE, quality_decisions[id(t)][1])
        auto_titles.append(t)
    review_titles = [t for t in titles if t not in auto_titles]

    # 5. Preview
    print_preview(titles, cfg)
    if review_titles:
        skipped_collisions = sum(1 for t in review_titles if id(t) in collided)
        msg = f"\n*** {len(review_titles)} title(s) will be SKIPPED"
        if skipped_collisions:
            msg += f" ({skipped_collisions} due to target collision — unsafe even with --force-low-confidence)"
        msg += ". The rest are below the auto-confidence threshold; re-run with --force-low-confidence to push them through. ***\n"
        print(msg)

    if not auto_titles:
        log.info("No titles to process. Exiting.")
        return 0

    # 6. Confirm
    if not args.yes and not args.dry_run:
        if not prompt_yes(f"Process {len(auto_titles)} title(s)?"):
            log.info("Aborted by user.")
            return 0

    # 7. Execute (uses execute.py's process_title for parity with bulk migration)
    journal_path = cfg.mediaprep_dir / ("intake_dryrun.jsonl" if args.dry_run
                                         else "intake_moves.jsonl")
    journal = Journal(journal_path, run_id, args.dry_run)
    journal.write(op="run_start", mode="dry-run" if args.dry_run else "apply",
                  intake_dir=str(intake_dir), title_count=len(auto_titles),
                  result="ok")
    ops = DiskOps(journal, args.dry_run, verify_hashes=False,
                  hash_algo=cfg.hash_algo)
    rich = (TmdbRichFetcher(cfg.tmdb_api_key, cfg.mediaprep_dir, cfg.language)
            if cfg.nfo_richness == "rich" else None)

    large_extras_log: List[Dict[str, Any]] = []
    orphans_log: List[Dict[str, Any]] = []
    succeeded_imdb_ids: List[str] = []
    ok = 0
    fail = 0
    for t in auto_titles:
        plan_row = build_plan_row(t)
        inv_files = build_inv_files(t)

        # ---- Branch: Extras (bonus feature for an existing movie).
        # Lands in Movie\Extras\ with Plex's recommended naming. No NFO
        # is written here — the movie folder's main NFO covers it, and
        # Plex doesn't need per-extra metadata files.
        if t.is_extra:
            target_video_path = plan_row.target_folder / t.final_video_name
            log.info("[%s] %s -> %s (extras)",
                     "DRY" if args.dry_run else "GO",
                     t.main_video.name, target_video_path)
            extras_ok = ops.mkdir(plan_row.target_folder,
                                  title_id=plan_row.title_id, stage="B")
            if extras_ok and target_video_path.exists():
                log.warning("  Extra already exists, skipping: %s",
                            target_video_path.name)
                fail += 1
                continue
            if extras_ok:
                extras_ok = ops.move(
                    t.main_video, target_video_path,
                    title_id=plan_row.title_id, stage="C",
                    op_label="rename" if same_drive(t.main_video,
                                                    target_video_path)
                                       else "move",
                )
            if extras_ok:
                ok += 1
            else:
                fail += 1
                log.error("  Extras install failed: %s",
                          t.main_video.name)
            continue

        # ---- Branch: 3D source MKV archive (flat layout, no NFO/sidecars).
        # process_title is bypassed because archives don't need its full
        # sidecar/NFO stack — they're just raw mkv files we want preserved.
        if t.is_3d_archive:
            target_video_path = plan_row.target_folder / t.final_video_name
            log.info("[%s] %s -> %s (3D source archive)",
                     "DRY" if args.dry_run else "GO",
                     t.main_video.name, target_video_path)
            archive_ok = ops.mkdir(plan_row.target_folder,
                                   title_id=plan_row.title_id, stage="B")
            if archive_ok and target_video_path.exists():
                log.warning("  3D archive already exists, skipping: %s",
                            target_video_path.name)
                fail += 1
                continue
            if archive_ok:
                archive_ok = ops.move(
                    t.main_video, target_video_path,
                    title_id=plan_row.title_id, stage="C",
                    op_label="rename" if same_drive(t.main_video,
                                                    target_video_path)
                                       else "move",
                )
            if archive_ok:
                ok += 1
            else:
                fail += 1
                log.error("  3D archive install failed: %s",
                          t.main_video.name)
            continue

        # Branch on EXISTS quality decision before invoking process_title.
        if id(t) in existing_collisions:
            decision, existing_video = quality_decisions.get(
                id(t), (QUALITY_UNDECIDED, None))

            if decision == QUALITY_KEEP_EXISTING:
                # Existing is better. New source(s) go to duplicates;
                # target folder is left untouched (artwork preserved).
                log.info("[%s] %s -> H:\\duplicates\\ (existing is higher quality)",
                         "DRY" if args.dry_run else "GO", t.main_video.name)
                dup_root = cfg.duplicates_dir / "intake_replaced" / t.target_folder.name
                ok_all = True
                for f in t.files:
                    dst = dup_root / f.name
                    if not ops.move(f.abs_path if hasattr(f, "abs_path") else f,
                                    dst, title_id=plan_row.title_id,
                                    stage="QUALITY_KEEP", op_label="dup_intake"):
                        ok_all = False
                # Try to rmdir the source folder if it's not the intake root
                if t.folder.exists() and t.folder != intake_dir:
                    ops.rmdir(t.folder, title_id=plan_row.title_id,
                              stage="QUALITY_KEEP")
                if ok_all:
                    ok += 1
                else:
                    fail += 1
                continue  # don't run process_title

            elif decision == QUALITY_REPLACE or (
                    decision == QUALITY_UNDECIDED and args.replace_existing):
                # New is better (or --replace-existing forces). Archive
                # existing video(s) first; artwork sidecars in the target
                # folder stay put (so Plex's existing posters/fanart aren't
                # disturbed).
                log.info("[%s] %s -> %s (replacing — new is higher quality)",
                         "DRY" if args.dry_run else "GO",
                         t.main_video.name, t.target_folder.name)
                replaced_dir = cfg.duplicates_dir / "intake_replaced" / t.target_folder.name
                try:
                    for e in os.scandir(long(t.target_folder)):
                        if e.is_file() and Path(e.name).suffix.lower() in VIDEO_EXTS:
                            src = _norm_path(e.path)
                            dst = replaced_dir / src.name
                            log.info("  archiving %s -> %s", src.name, dst)
                            ops.move(src, dst, title_id=plan_row.title_id,
                                     stage="REPLACE", op_label="replace_archive")
                except OSError as e:
                    log.warning("  could not enumerate target folder for replacement: %s", e)
                # Fall through to process_title to install the new video

        else:
            log.info("[%s] %s -> %s", "DRY" if args.dry_run else "GO",
                     t.main_video.name, t.target_folder.name)

        # Variant-aware filename: if this title has edition/3D tags, build a
        # per-call template so process_title's internal stem rendering picks
        # up "{edition-Extended}" or "- 3D.HSBS". The folder leaf is still
        # the clean canonical name (compute_target ensures that), so
        # variants of the same movie share a folder and Plex groups them.
        saved_template = cfg.filename_template
        if t.edition or t.is_3d:
            base_template = (cfg.filename_template if t.imdb_id
                             else "{title} ({year}) {{tmdb-{tmdb_id}}}")
            cfg.filename_template = template_for_variant(
                base_template, t.edition, t.is_3d, t.threed_format,
            )

        try:
            result = process_title(plan_row, inv_files, cfg, ops, rich,
                                   large_extras_log, orphans_log)
        except Exception as e:
            log.exception("title %s crashed: %s", plan_row.title_id, e)
            fail += 1
            continue
        finally:
            cfg.filename_template = saved_template

        if result.success:
            ok += 1
            if t.imdb_id:
                succeeded_imdb_ids.append(t.imdb_id)
        else:
            log.error("FAILED at stage %s: %s", result.error_stage, result.error)
            fail += 1

    journal.write(op="run_end", result="ok", succeeded=ok, failed=fail)
    journal.close()
    if rich:
        rich.close()

    log.info("")
    log.info("Summary: %d processed, %d failed (mode=%s)",
             ok, fail, "dry-run" if args.dry_run else "apply")

    # 8. Post-processing enrichment (artwork, clearart, trailers).
    # Each enrichment is targeted at JUST the new titles via --imdb so we
    # don't waste time re-walking the whole library.
    if args.full:
        args.with_art = args.with_clearart = args.with_trailer = True
        args.plex_scan = True

    do_enrich = (args.with_art or args.with_clearart or args.with_trailer) \
                 and succeeded_imdb_ids and not args.dry_run
    if do_enrich:
        imdb_csv = ",".join(succeeded_imdb_ids)
        log.info("")
        log.info("Enriching %d new title(s) ...", len(succeeded_imdb_ids))
        import subprocess
        script_dir = Path(__file__).parent
        if args.with_art:
            log.info("  → art_fetch.py")
            try:
                subprocess.run([sys.executable,
                                str(script_dir / "art_fetch.py"),
                                "--config", args.config,
                                "--imdb", imdb_csv], check=False)
            except OSError as e:
                log.warning("art_fetch.py invocation failed: %s", e)
        if args.with_clearart:
            log.info("  → clearart_fetch.py")
            try:
                subprocess.run([sys.executable,
                                str(script_dir / "clearart_fetch.py"),
                                "--config", args.config,
                                "--imdb", imdb_csv], check=False)
            except OSError as e:
                log.warning("clearart_fetch.py invocation failed: %s", e)
        if args.with_trailer:
            log.info("  → trailer_fetch.py")
            try:
                subprocess.run([sys.executable,
                                str(script_dir / "trailer_fetch.py"),
                                "--config", args.config,
                                "--imdb", imdb_csv,
                                "--youtube-search-fallback"], check=False)
            except OSError as e:
                log.warning("trailer_fetch.py invocation failed: %s", e)

    # 9. Optional Plex scan (last, so it picks up artwork too)
    if args.plex_scan and ok and not args.dry_run:
        log.info("Triggering Plex scan ...")
        try:
            from plex_scan import cmd_scan, parse_args as plex_parse
            plex_args = plex_parse(["--config", args.config, "--wait"])
            plex_cfg = json.loads(_norm_path(args.config).read_text(encoding="utf-8"))
            cmd_scan(plex_cfg, plex_args)
        except Exception as e:
            log.warning("Plex scan failed: %s", e)

    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
