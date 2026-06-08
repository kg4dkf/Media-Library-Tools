#!/usr/bin/env python3
"""
tv_episode_matcher.py - TMDB-title-based fuzzy matching for video filenames
that don't match SxxExx patterns.

Solves Round 6b's biggest problem: source folders containing video files
named after episode TITLES rather than S##E## numbering.  For example:

    Underdog - Safe Waif.avi               (no SxxExx token)
    Tom_and_Jerry_-_The_Mouse_Trouble.mkv  (Title-only)
    01 - Pilot.mkv                         (just disc-track number + title)

Given the series's TMDB id (and thus access to its full episode list),
we can match these to (season, episode) tuples by comparing the title
text in the filename to the canonical episode title list.

This module is pure-stdlib, decoupled from the rest of the pipeline.
Caller is responsible for fetching the TMDB episodes (typically via
TmdbTvClient.tv_season) and building the episodes_map argument.

The scoring blends three signals:
  * SequenceMatcher ratio (handles char-level edits + word reorderings)
  * Token-set Jaccard on filtered tokens (penalizes missing or extra
    distinctive words while ignoring stopwords + episode-number markers)
  * Length-aware bonus when the filename contains the title as a
    substring (handles common "<show name> - <ep title>.<ext>" pattern)

Confidence buckets:
  HIGH    score >= 0.85 AND beats runner-up by >= 0.20
  MEDIUM  score >= 0.55
  NONE    below threshold (caller should leave file as extras)
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple


# Stopwords that appear in real titles but shouldn't dominate similarity.
# Kept short — we don't want to filter out everything (e.g., "The Office"
# only has stopwords in its name).
_STOPWORDS: frozenset = frozenset({
    "the", "a", "an", "of", "and", "or", "to", "in", "on", "at",
    "for", "is", "with", "by", "as",
})

# Episode-number markers that show up in filenames as cruft we should
# strip before matching against episode titles. NOTE: we deliberately
# do NOT strip "Part N" / "Pt N" — those are typically meaningful parts
# of episode titles (e.g., "Go Snow Part 4" vs "Go Snow Part 1") and
# removing them would collapse multi-part episodes onto each other.
_EP_MARKER_RE = re.compile(
    r"\b(?:s\d+e\d+|s\d+\s*-?\s*e\d+|"   # SxxExx / S01-E01
    r"season\s*\d+|episode\s*\d+|ep\s*\d+|"
    r"\d{1,3}x\d{1,3})\b",                # 1x05
    re.IGNORECASE,
)

# Standalone leading number: "01 - Pilot.avi" or "001. Pilot.avi" or "1) Pilot.avi"
_LEADING_NUMBER_RE = re.compile(r"^\s*\d{1,4}\s*[\-_.\)]?\s+")

# Trailing release / quality cruft per-file. Heavier than the folder
# version because filenames often have more cruft.
_FILENAME_CRUFT: tuple = (
    "dvdrip", "bluray", "brrip", "bdrip", "hdtv", "pdtv", "webrip", "webdl",
    "x264", "x265", "h264", "h265", "hevc", "xvid", "divx",
    "1080p", "720p", "480p", "576p", "2160p", "4k", "uhd",
    "aac", "ac3", "mp3", "dts", "5.1",
    "axxo", "yify", "rarbg", "ettv", "fgt",
    "complete", "remastered", "extended", "uncut", "proper", "repack",
    "pack", "vol", "volume", "disc",
)

_BRACKET_RE = re.compile(r"[\[(\{][^\]\)\}]*[\]\)\}]")


def _strip_diacritics(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def normalize_for_match(text: str) -> str:
    """Aggressive normalization: lowercase, strip diacritics, replace
    separators with spaces, collapse whitespace. Used on both the filename
    naked-title and the episode title before scoring."""
    if not text:
        return ""
    s = _strip_diacritics(text)
    s = s.lower()
    # Replace common separators with spaces.
    s = re.sub(r"[._\-]+", " ", s)
    # Drop residual non-alphanumeric (apostrophes, parens, etc.).
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    # Normalize whitespace.
    s = re.sub(r"\s+", " ", s).strip()
    return s


def extract_title_hint(filename: str,
                       *,
                       series_name: Optional[str] = None) -> str:
    """Strip cruft from a filename to expose what's likely the episode title.

    Rules applied in order:
      1. Drop file extension.
      2. Drop bracketed tags `[ax]`, `(720p)`, `{tmdb-...}`.
      3. Drop SxxExx / season/episode markers.
      4. Drop leading "01 - " / "001. " disc-track numbering.
      5. Drop the series name prefix if known.
      6. Drop file-format / quality / torrent cruft tokens.
      7. Trim hyphens, dots, whitespace.
    Returns the remaining string, lowercased & cleaned.
    """
    stem = Path(filename).stem
    stem = _BRACKET_RE.sub(" ", stem)
    stem = _EP_MARKER_RE.sub(" ", stem)
    stem = _LEADING_NUMBER_RE.sub("", stem)

    # Drop series prefix (case/punctuation-insensitive).
    if series_name:
        norm_series = normalize_for_match(series_name)
        norm_stem = normalize_for_match(stem)
        if norm_series and norm_stem.startswith(norm_series + " "):
            # We can't easily strip from the original-case stem, so
            # rebuild from normalized.
            stem = norm_stem[len(norm_series):].strip()

    # Tokenize and drop cruft tokens.
    tokens = [t for t in re.split(r"[\s\-_.]+", stem) if t]
    keep: List[str] = []
    for t in tokens:
        low = t.lower().strip("-.,()'\"")
        if not low:
            continue
        if low in _FILENAME_CRUFT:
            continue
        if any(c in low for c in _FILENAME_CRUFT):
            continue
        # Drop pure 4+ digit numbers (years/release tags).
        if low.isdigit() and len(low) >= 4:
            continue
        keep.append(t)
    return " ".join(keep).strip(" -._")


def _tokenize(text: str) -> List[str]:
    norm = normalize_for_match(text)
    return [t for t in norm.split() if t and t not in _STOPWORDS]


def title_similarity(a: str, b: str) -> float:
    """Hybrid similarity between two title strings.

    Returns 0.0 - 1.0. Combines:
      * SequenceMatcher ratio (50%)
      * Token-set Jaccard, stopwords excluded (35%)
      * Substring bonus: 1.0 if one normalized title contains the other,
        else 0.0 (15%) — handles the "<show> - <title>" pattern where
        the filename contains the episode title plus extra prefix.
    """
    if not a or not b:
        return 0.0
    na, nb = normalize_for_match(a), normalize_for_match(b)
    if not na or not nb:
        return 0.0
    seq = SequenceMatcher(None, na, nb).ratio()
    ta, tb = set(_tokenize(a)), set(_tokenize(b))
    if not ta or not tb:
        jac = 0.0
    else:
        jac = len(ta & tb) / len(ta | tb)
    contains = 1.0 if (na in nb or nb in na) else 0.0
    return 0.50 * seq + 0.35 * jac + 0.15 * contains


@dataclass
class EpisodeMatch:
    season: int
    episode: int
    title: str
    score: float
    confidence: str   # "HIGH" | "MEDIUM" | "NONE"


# Tunables — exposed for tests / experimentation.
MATCH_HIGH_THRESHOLD = 0.85
MATCH_HIGH_GAP = 0.20
MATCH_MEDIUM_THRESHOLD = 0.55


def match_filename_to_episode(
    filename: str,
    episodes_map: Dict[Tuple[int, int], str],
    *,
    series_name: Optional[str] = None,
) -> Optional[EpisodeMatch]:
    """Find the best (season, episode) match for a filename.

    Args:
      filename: the source video filename (basename + ext, or full path).
      episodes_map: {(season_num, episode_num): episode_title} for the
                    relevant series. Caller assembles this from TMDB.
      series_name: optional matched_series name; used to strip the
                    show-name prefix from the filename's title hint.

    Returns:
      EpisodeMatch with confidence HIGH / MEDIUM, or None if no decent
      match was found (caller should leave the file as extras).
    """
    if not episodes_map:
        return None
    hint = extract_title_hint(filename, series_name=series_name)
    if not hint or len(normalize_for_match(hint)) < 3:
        return None

    scored: List[Tuple[Tuple[int, int], str, float]] = []
    for (s, e), title in episodes_map.items():
        if not title or not isinstance(title, str):
            continue
        score = title_similarity(hint, title)
        scored.append(((s, e), title, score))
    if not scored:
        return None
    scored.sort(key=lambda t: -t[2])
    top_key, top_title, top_score = scored[0]

    if top_score < MATCH_MEDIUM_THRESHOLD:
        return None

    # HIGH requires both an absolute floor AND a gap over runner-up.
    if len(scored) >= 2:
        runner_score = scored[1][2]
    else:
        runner_score = 0.0
    if top_score >= MATCH_HIGH_THRESHOLD and (top_score - runner_score) >= MATCH_HIGH_GAP:
        confidence = "HIGH"
    elif top_score >= MATCH_HIGH_THRESHOLD and len(scored) == 1:
        # Only one candidate at all — we don't have ambiguity to compare.
        confidence = "HIGH"
    else:
        confidence = "MEDIUM"

    return EpisodeMatch(
        season=top_key[0],
        episode=top_key[1],
        title=top_title,
        score=top_score,
        confidence=confidence,
    )


def episodes_map_from_tmdb_seasons(season_objects: Iterable[Dict[str, Any]]
                                    ) -> Dict[Tuple[int, int], str]:
    """Build the episodes_map argument from a list of TMDB season-detail
    response dicts (each from `client.tv_season(...)`)."""
    out: Dict[Tuple[int, int], str] = {}
    for sd in season_objects:
        if not isinstance(sd, dict):
            continue
        s = sd.get("season_number")
        for ep in sd.get("episodes") or []:
            try:
                en = int(ep.get("episode_number"))
                sn = int(s)
            except (TypeError, ValueError):
                continue
            title = str(ep.get("name") or "")
            if title:
                out[(sn, en)] = title
    return out
