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

def identify_title(t: IntakeTitle, tmdb: TmdbClient) -> None:
    """Populate t with TMDB-derived identification."""
    # 1. NFO in intake folder?
    nfo_path = next((f for f in t.files if f.suffix.lower() == ".nfo"), None)
    if nfo_path:
        imdb, tmdb_id = read_nfo_imdb(nfo_path)
        t.nfo_imdb_id = imdb
        t.nfo_tmdb_id = tmdb_id

    # 2. Try the NFO IMDB id first
    if t.nfo_imdb_id:
        match = tmdb.find_by_imdb(t.nfo_imdb_id)
        if match:
            _apply_match(t, match, tmdb, confidence=95)
            t.note = f"matched via NFO imdb {t.nfo_imdb_id}"
            return

    # 3. Use guessit on the de-noised stem
    stem = denoise_filename(t.main_video.stem)
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
            confidence = 60  # year mismatch — wrong record likely
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
    """Compute target_folder using the same template as the bulk migration."""
    if not t.matched_title or not t.matched_year:
        return Path("")
    if not t.imdb_id:
        # Fall back to TMDB id if no IMDB
        leaf_template = "{title} ({year}) {{tmdb-{tmdb_id}}}"
    else:
        leaf_template = cfg.filename_template
    leaf = render_filename_template(leaf_template, {
        "title": t.matched_title,
        "year": t.matched_year,
        "imdb_id": t.imdb_id,
        "tmdb_id": t.tmdb_id,
        "matched_title_lowercase": t.matched_title.lower(),
    })
    if t.collection_name:
        franchise = sanitize_for_windows(t.collection_name)
        return _norm_path(str(cfg.movies_root) + "\\" + franchise + "\\" + leaf)
    return _norm_path(str(cfg.movies_root) + "\\" + leaf)


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
            print(f"    Target : {t.target_folder}")
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
                   help="overwrite the destination if a file already exists there "
                        "(default: skip; the existing file is preserved)")
    p.add_argument("--plex-scan", action="store_true",
                   help="trigger Plex Movies library scan after success")
    p.add_argument("--no-nfo", action="store_true",
                   help="skip NFO writing")
    p.add_argument("--keep-source", action="store_true",
                   help="don't rmdir the empty intake source after move")
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
    # to the same target_folder. Almost always means the title parsing
    # produced the same TMDB match for distinct files.
    target_groups: Dict[str, List[IntakeTitle]] = {}
    for t in titles:
        if not t.target_folder or str(t.target_folder) in ("", "."):
            continue
        target_groups.setdefault(str(t.target_folder).lower(), []).append(t)
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
    # migration. Without --replace-existing, skip.
    existing_collisions: Set[int] = set()
    for t in titles:
        if id(t) in collided or not t.target_folder:
            continue
        if not t.target_folder.exists():
            continue
        try:
            entries = [e for e in os.scandir(long(t.target_folder))
                       if e.is_file() and Path(e.name).suffix.lower() in VIDEO_EXTS]
        except OSError:
            continue
        if entries:
            existing_video = entries[0].name
            t.note = (t.note + " | " if t.note else "") + \
                     f"EXISTS: target folder already contains '{existing_video}' " \
                     f"(use --replace-existing to overwrite)"
            existing_collisions.add(id(t))
    if existing_collisions:
        log.warning("Detected %d title(s) that already exist at the target "
                    "(prior migration or earlier intake run). They will be "
                    "skipped unless you use --replace-existing.",
                    len(existing_collisions))

    # 4. Filter low-confidence unless forced. In-batch collisions are NEVER
    # auto-processed. Existing-target collisions are skipped unless
    # --replace-existing is passed.
    auto_titles = [t for t in titles
                   if t.matched_title
                   and id(t) not in collided
                   and (id(t) not in existing_collisions or args.replace_existing)
                   and (t.confidence >= 70 or args.force_low_confidence)]
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
    ok = 0
    fail = 0
    for t in auto_titles:
        plan_row = build_plan_row(t)
        inv_files = build_inv_files(t)
        log.info("[%s] %s -> %s", "DRY" if args.dry_run else "GO",
                 t.main_video.name, t.target_folder.name)

        # If replacing an existing target, move the old video(s) to
        # duplicates_dir/intake_replaced/ first so they're preserved.
        if (id(t) in existing_collisions and args.replace_existing
                and t.target_folder.exists()):
            replaced_dir = cfg.duplicates_dir / "intake_replaced" / t.target_folder.name
            try:
                for e in os.scandir(long(t.target_folder)):
                    if e.is_file() and Path(e.name).suffix.lower() in VIDEO_EXTS:
                        src = _norm_path(e.path)
                        dst = replaced_dir / src.name
                        log.info("  Replacing: archiving %s -> %s", src.name, dst)
                        ops.move(src, dst, title_id=plan_row.title_id,
                                 stage="REPLACE", op_label="replace_archive")
            except OSError as e:
                log.warning("  could not enumerate target folder for replacement: %s", e)

        try:
            result = process_title(plan_row, inv_files, cfg, ops, rich,
                                   large_extras_log, orphans_log)
        except Exception as e:
            log.exception("title %s crashed: %s", plan_row.title_id, e)
            fail += 1
            continue
        if result.success:
            ok += 1
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

    # 8. Optional Plex scan
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
