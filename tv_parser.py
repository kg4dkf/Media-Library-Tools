#!/usr/bin/env python3
"""
tv_parser.py - Phase 6 filename parser for the TV pipeline.

Pure-stdlib library that extracts (season, episode, episode_end, series_name_guess,
flags) from TV episode filenames. Used by inventory_tv.py (Stage B in
phase6_design.md) and reused by the reverse-grouping clusterer (§6) and the
cross-folder content checker (§7) via the helpers exported at the bottom of
this module.

Design priorities, mirroring the movie pipeline:
    safety > reversibility > idempotency > resumability > speed

Specifically: this module is deterministic, side-effect-free, has no third-party
dependencies, and is unit-testable in isolation. It does NOT touch the
filesystem, does NOT call TMDB, and does NOT sanitize for Windows (that's the
job of execute_tv.py at Stage D, which already imports sanitize_component
from the movie execute.py).

Patterns recognized (Stage B in phase6_design.md):
    1. S##E##         e.g. "Show S01E03 Title.mkv", "Show s1e12.mkv"
    2. ##x##          e.g. "Show 1x03 Title.avi", "Show 12x103.mp4"
    3. Bare-N in a `Season N\\` parent folder
                      e.g. parent="Season 1", filename="Episode 03.mkv" -> S01E03
    4. Multi-episode  S01E01-E02, S01E01E02, S01E01-02, 1x01-1x02
    5. Absolute       "Show Name - 145.mkv" -> S00E145 (only when allow_absolute=True;
                      flagged for review per design §15)
    6. Specials       season==0 OR filename markers ("special", "OVA", "Christmas",
                      "Holiday") -> is_special=True

Public API:
    parse_episode(filename, parent_folder=None, *, allow_absolute=False) -> ParsedEpisode
    derive_series_from_filename(filename) -> str
    normalize_folder_name(folder_name) -> str
    tokenize_for_clustering(name) -> set[str]
    is_video_filename(name) -> bool

CLI smoke test:
    py tv_parser.py "Show Name S01E03 Title.mkv"
    py tv_parser.py --parent "Season 1" "Episode 03.mkv"
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Optional, List, Set, Tuple


SCRIPT_VERSION = "1.0.0"

log = logging.getLogger("tv_parser")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Same video extension set as movie inventory.py (kept in sync deliberately).
VIDEO_EXTS: Set[str] = {
    ".mkv", ".mp4", ".avi", ".mov", ".m4v", ".wmv",
    ".mpg", ".mpeg", ".ts", ".m2ts", ".vob", ".iso",
    ".divx", ".flv", ".webm", ".3gp", ".asf", ".ogv",
}

# Quality / release tags stripped before clustering and series-name derivation.
# Matched as whole words, case-insensitive. Order doesn't matter; the regex is
# alternation. Conservative — anything ambiguous (e.g. "HD" alone, "GROUP")
# stays in unless it appears clearly tag-like.
QUALITY_TAG_WORDS: Tuple[str, ...] = (
    # resolution / source
    r"480p", r"576p", r"720p", r"1080p", r"1440p", r"2160p", r"4k", r"uhd", r"hd",
    r"sd", r"hdr10\+?", r"hdr", r"dolby ?vision", r"dovi", r"sdr",
    # rip type
    r"bluray", r"blu-?ray", r"bdrip", r"brrip", r"dvdrip", r"webrip", r"web-?dl",
    r"hdtv", r"hdrip", r"pdtv", r"dsr", r"dsrip", r"remux", r"repack", r"proper",
    r"internal", r"extended", r"director.?s? cut", r"unrated", r"limited",
    # codec
    r"x264", r"x265", r"h\.?264", r"h\.?265", r"hevc", r"avc", r"xvid", r"divx",
    r"vp9", r"av1",
    # audio
    r"mp3", r"aac", r"aac2\.0", r"ac3", r"eac3", r"dts(?:-?hd)?(?:-?ma)?", r"flac",
    r"opus", r"truehd", r"atmos", r"dd5\.1", r"dd2\.0", r"5\.1", r"2\.0", r"7\.1",
    # group / scene markers
    r"yify", r"yts(?:\.\w+)?", r"rarbg", r"ettv", r"eztv", r"nogrp", r"fov", r"sva",
    r"killers", r"lol", r"asap", r"fum", r"dimension", r"immerse",
    # disc structure
    r"disc ?\d+", r"d\d+", r"cd\d+", r"title ?\d+",
)
# Anchor as whole "word" boundaries. Trailing-group dashes ("-NoGRP") are common,
# so we also strip leading "-" before/after a tag.
QUALITY_TAG_RE = re.compile(
    r"(?<![\w'])(?:" + r"|".join(QUALITY_TAG_WORDS) + r")(?![\w'])",
    re.IGNORECASE,
)

# Stopwords removed during folder-name normalization for the merge clusterer
# (phase6_design.md §6). Kept short on purpose; over-stripping causes false-merges.
STOPWORDS: Set[str] = {
    # Articles, conjunctions, prepositions
    "the", "a", "an", "and", "of", "in", "on", "at", "to", "for",
    # Generic packaging words that appear in folder names but never identify
    # a series. "edition", "collection", "volume" cause real false-positive
    # clusters in mixed libraries (see test_real_data_regressions).
    "complete", "show", "shows", "series", "season", "seasons",
    "episode", "episodes", "edition", "collection", "collections",
    "volume", "volumes", "vol",
}

# Filename markers that imply a specials episode (season 0).
SPECIAL_MARKERS_RE = re.compile(
    r"\b(?:specials?|ova|oad|extras?|christmas|holiday|new ?year|"
    r"behind the scenes|making of|deleted scenes?|gag reel|bloopers?)\b",
    re.IGNORECASE,
)

# Episode-number patterns. We try them in order of specificity.
#
# 1. SxxExx (single, multi same-S, range, joined). Captures season, ep1, ep2.
#    Matches S01E03, s1e12, S01E01-E02, S01E01E02, S01E01-02.
#    The `(?:[-eE]+(\d{1,4}))?` group captures a multi-episode end, if present.
RE_S_E = re.compile(
    r"(?<![A-Za-z0-9])"                # left boundary (not mid-word)
    r"[Ss](?P<season>\d{1,3})"
    r"[\s._-]?"
    r"[Ee](?P<ep1>\d{1,4})"
    r"(?:[\s._-]*(?:[eE-])[\s._-]*(?P<ep2>\d{1,4}))?"
    r"(?![A-Za-z0-9])"
)

# 2. NxNN — must not be preceded by a letter+digit (avoids matching "x264", "h264").
#    "1x03", "12x103", "1x01-1x02" (handled as two passes; we capture the first).
RE_X_FORM = re.compile(
    r"(?<![A-Za-z])"
    r"(?<![\d])"
    r"(?P<season>\d{1,3})x(?P<ep1>\d{1,4})"
    r"(?:[\s._-]*-[\s._-]*(?P<ep2_full>\d{1,3})x(?P<ep2_x>\d{1,4})"
    r"|[\s._-]*-[\s._-]*(?P<ep2_short>\d{1,4}))?"
    r"(?![A-Za-z\d])"
)

# 3. "Season N" / "Specials" parent-folder detection.
RE_SEASON_FOLDER = re.compile(
    r"^\s*(?:season\s*(?P<num>\d{1,3})|(?P<specials>specials?))\s*(?:\(\d{4}\))?\s*$",
    re.IGNORECASE,
)

# 4. "Episode 03", "Ep 03", "Ep.03", "E03" (used inside a Season-N parent context).
RE_BARE_EP = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?:episode|ep|e)[\s._-]*"
    r"(?P<ep1>\d{1,4})"
    r"(?:[\s._-]*-[\s._-]*(?P<ep2>\d{1,4}))?"
    r"(?![A-Za-z0-9])",
    re.IGNORECASE,
)

# 5. Standalone digits (last-resort inside a Season-N folder, or for absolute mode).
#    1-4 digit run not adjacent to a letter (so we don't match "x264" or "1080p").
RE_BARE_NUM = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?P<num>\d{1,4})"
    r"(?![A-Za-z0-9])"
)

# Track-number / disc / title prefixes stripped when normalizing folder names
# for the merge clusterer (phase6_design.md §6, step 1).
LEADING_TRACK_RES: Tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*\d{1,3}\s*[-._]?\s+", re.IGNORECASE),       # "01 ", "01-", "01_"
    re.compile(r"^\s*disc\s*\d+\s+", re.IGNORECASE),             # "Disc 2 "
    re.compile(r"^\s*[Tt]\d{2,3}\s+"),                           # "T00 "
    re.compile(r"^\s*_?[Tt]itle\s*\d+\s*", re.IGNORECASE),       # "Title1", "_Title18"
    re.compile(r"^\s*playlist[\s_]*\d+\s*", re.IGNORECASE),      # "Playlist_800"
    re.compile(r"^\s*\[[^\]]+\]\s*"),                            # "[group] "
    re.compile(r"^\s*\([^)]+\)\s*"),                             # "(group) "
)

# The leading episode-number that we extract from a merge-group member folder
# name (e.g. "01 MrBean" -> 1). Used by inventory_tv.py during merge processing
# (§6: "Episode S/E numbers come from the leading number in each source folder name").
#
# Requires whitespace AFTER the number (not just a non-digit char) so that
# DVD-rip artifacts like "200_CARTOONS_DISC1_Title2" are NOT mistaken for
# single-episode wrappers. The space is the structural marker of a
# human-readable "<NN> <Show Name>" folder.
RE_LEADING_NUM = re.compile(r"^\s*(?P<num>\d{1,4})(?=\s)")

# Year token stripped from folder/file names during normalization
# (we keep it elsewhere for TMDB matching; not relevant for clustering).
# Matches (YYYY), (YYYY-), (YYYY-YYYY), (YYYY-YY). The trailing-dash variant
# is common for ongoing series ("Arrow (2012-)").
RE_PAREN_YEAR = re.compile(
    r"\((?:19|20)\d{2}"
    r"(?:[\s\-–—]+(?:(?:19|20)\d{2}|\d{2})?)?"
    r"\)"
)

# Bracketed release-group annotations: "[FoxRG]", "[1080p AAC]", etc.
RE_BRACKETS = re.compile(r"\[[^\]]*\]")


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------

@dataclass
class ParsedEpisode:
    """Structured result of parsing a single episode filename.

    All fields are populated even when parsing fails; check `parsed` first.
    """
    season: Optional[int] = None
    episode: Optional[int] = None         # first episode # (start of range for multi-ep)
    episode_end: Optional[int] = None     # last episode # (== episode for singles)
    series_name_guess: str = ""           # best-effort; what's left of the stem before S/E
    is_special: bool = False              # season==0 OR specials filename marker
    is_absolute: bool = False             # absolute-numbering parse used
    is_multi: bool = False                # multi-episode marker matched
    pattern_matched: str = ""             # 's_e' | 'x_form' | 'season_folder_bare' |
                                          # 'season_folder_ep' | 'absolute' | ''
    confidence: int = 0                   # 0..100 (parser confidence, not TMDB)
    raw_filename: str = ""
    raw_parent_folder: Optional[str] = None
    flags: List[str] = field(default_factory=list)

    @property
    def parsed(self) -> bool:
        """True if at least season+episode are known."""
        return self.season is not None and self.episode is not None

    @property
    def episode_key(self) -> str:
        """Compact identifier matching phase6_design.md §10 ('S##E##')."""
        if not self.parsed:
            return ""
        s = self.season or 0
        e1 = self.episode or 0
        e2 = self.episode_end or e1
        if e2 != e1:
            return f"S{s:02d}E{e1:02d}-E{e2:02d}"
        return f"S{s:02d}E{e1:02d}"


# ---------------------------------------------------------------------------
# Utilities — extension / stem handling
# ---------------------------------------------------------------------------

def is_video_filename(name: str) -> bool:
    """True if the filename has a recognized video extension."""
    if not name:
        return False
    _, ext = os.path.splitext(name)
    return ext.lower() in VIDEO_EXTS


def _strip_known_video_ext(name: str) -> str:
    """Strip a trailing video extension if present, else return name unchanged."""
    stem, ext = os.path.splitext(name)
    if ext.lower() in VIDEO_EXTS:
        return stem
    return name


def _normalize_separators(s: str) -> str:
    """Convert dots/underscores between word-ish runs into spaces.

    DVD-rip filenames love "Show.Name.S01E03.Title.mkv"; this turns those
    delimiter-dots into spaces while leaving real dots (e.g. "Mr. Bean") alone.
    Heuristic: any '.' or '_' between two alphanumeric runs becomes a space.
    """
    if not s:
        return s
    # Replace runs of dots/underscores between alphanumerics with one space.
    return re.sub(r"(?<=\w)[._]+(?=\w)", " ", s)


def _collapse_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------------------
# Public API: parse_episode
# ---------------------------------------------------------------------------

def parse_episode(
    filename: str,
    parent_folder: Optional[str] = None,
    *,
    allow_absolute: bool = False,
) -> ParsedEpisode:
    """Parse a single episode filename into structured fields.

    Args:
        filename: Just the file's basename (with or without extension). Folder
            components must NOT be embedded here — pass them as parent_folder.
        parent_folder: Optional immediate parent folder name. Used to resolve
            bare-N-in-Season-N filenames (case 3 in the design).
        allow_absolute: If True, fall back to absolute-numbering parsing
            (case 5 in the design). Default False per phase6_design.md §15
            ("No anime mode by default; absolute-numbered episodes parsed if
            filename matches but flagged for review").

    Returns:
        A ParsedEpisode. Use .parsed to check whether identification succeeded.
    """
    raw_name = filename or ""
    result = ParsedEpisode(raw_filename=raw_name, raw_parent_folder=parent_folder)

    if not raw_name:
        result.flags.append("empty_filename")
        return result

    stem = _strip_known_video_ext(raw_name)
    norm = _normalize_separators(stem)

    # Specials marker check runs first (orthogonal to S/E parsing).
    if SPECIAL_MARKERS_RE.search(norm):
        result.is_special = True
        result.flags.append("special_marker_in_filename")

    # Pass 1: SxxExx (preferred — most reliable).
    m = RE_S_E.search(norm)
    if m:
        season = int(m.group("season"))
        ep1 = int(m.group("ep1"))
        ep2_raw = m.group("ep2")
        ep2 = int(ep2_raw) if ep2_raw is not None else ep1
        if ep2 < ep1:
            # E.g. "S01E03-01" — accept but flag as suspect.
            result.flags.append("multi_ep_range_descending")
            ep2 = ep1
        result.season = season
        result.episode = ep1
        result.episode_end = ep2
        result.is_multi = ep2 != ep1
        result.pattern_matched = "s_e"
        result.confidence = 95 if not result.is_multi else 90
        result.series_name_guess = _series_guess_before(norm, m.start())
        if season == 0:
            result.is_special = True
        return result

    # Pass 2: NxNN form.
    m = RE_X_FORM.search(norm)
    if m:
        season = int(m.group("season"))
        ep1 = int(m.group("ep1"))
        ep2 = ep1
        if m.group("ep2_x") is not None:
            ep2 = int(m.group("ep2_x"))
        elif m.group("ep2_short") is not None:
            ep2 = int(m.group("ep2_short"))
        if ep2 < ep1:
            result.flags.append("multi_ep_range_descending")
            ep2 = ep1
        result.season = season
        result.episode = ep1
        result.episode_end = ep2
        result.is_multi = ep2 != ep1
        result.pattern_matched = "x_form"
        result.confidence = 88 if not result.is_multi else 82
        result.series_name_guess = _series_guess_before(norm, m.start())
        if season == 0:
            result.is_special = True
        return result

    # Pass 3: Season-N parent folder gives us the season; pull the episode # from
    # the filename via a "Episode 03" / "E03" / bare-digit fallback.
    season_from_parent = _season_from_parent(parent_folder)
    if season_from_parent is not None:
        # Try "Episode 03" / "Ep 03" / "E03" first.
        m = RE_BARE_EP.search(norm)
        if m:
            ep1 = int(m.group("ep1"))
            ep2_raw = m.group("ep2")
            ep2 = int(ep2_raw) if ep2_raw is not None else ep1
            if ep2 < ep1:
                result.flags.append("multi_ep_range_descending")
                ep2 = ep1
            result.season = season_from_parent
            result.episode = ep1
            result.episode_end = ep2
            result.is_multi = ep2 != ep1
            result.pattern_matched = "season_folder_ep"
            result.confidence = 80 if not result.is_multi else 75
            result.series_name_guess = _series_guess_before(norm, m.start())
            if season_from_parent == 0:
                result.is_special = True
            return result

        # Fall back to a standalone digit run inside the filename.
        bare = _first_bare_number(norm)
        if bare is not None:
            num, start = bare
            result.season = season_from_parent
            result.episode = num
            result.episode_end = num
            result.pattern_matched = "season_folder_bare"
            result.confidence = 65
            result.series_name_guess = _series_guess_before(norm, start)
            if season_from_parent == 0:
                result.is_special = True
            return result

    # Pass 4 (gated): absolute-numbering. "Show Name - 145" -> S00E145, flagged.
    if allow_absolute:
        bare = _first_bare_number(norm, min_value=2)
        if bare is not None:
            num, start = bare
            result.season = 0          # convention from design §17 / Specials handling
            result.episode = num
            result.episode_end = num
            result.is_absolute = True
            result.pattern_matched = "absolute"
            result.confidence = 40
            result.flags.append("absolute_numbering_assumed")
            result.series_name_guess = _series_guess_before(norm, start)
            return result

    # No parse.
    result.flags.append("unparseable")
    result.series_name_guess = _collapse_spaces(QUALITY_TAG_RE.sub(" ", norm))
    return result


def _season_from_parent(parent: Optional[str]) -> Optional[int]:
    """Return the season number implied by a parent folder name, or None."""
    if not parent:
        return None
    name = parent.strip()
    # Strip trailing year suffix "Season 1 (1986)".
    name_no_year = RE_PAREN_YEAR.sub("", name).strip()
    m = RE_SEASON_FOLDER.match(name_no_year)
    if not m:
        return None
    if m.group("specials") is not None:
        return 0
    return int(m.group("num"))


def _first_bare_number(s: str, min_value: int = 1) -> Optional[Tuple[int, int]]:
    """Find the first standalone digit run (>=min_value) and its start offset."""
    for m in RE_BARE_NUM.finditer(s):
        # Skip 4-digit years that aren't likely episode numbers.
        n = int(m.group("num"))
        if 1900 <= n <= 2099 and len(m.group("num")) == 4:
            continue
        if n < min_value:
            continue
        return n, m.start()
    return None


def _series_guess_before(norm_stem: str, marker_start: int) -> str:
    """Return the cleaned series-name candidate from text BEFORE the S/E marker.

    Used both to populate ParsedEpisode.series_name_guess and (via
    derive_series_from_filename) to power the cross-folder content checker.
    """
    head = norm_stem[:marker_start]
    head = RE_BRACKETS.sub(" ", head)
    head = QUALITY_TAG_RE.sub(" ", head)
    head = RE_PAREN_YEAR.sub(" ", head)
    head = re.sub(r"[-_.]+", " ", head)
    head = _collapse_spaces(head)
    head = head.strip(" -._,")
    return head


# ---------------------------------------------------------------------------
# Public API: derive_series_from_filename (cross-folder content check, §7)
# ---------------------------------------------------------------------------

def derive_series_from_filename(filename: str) -> str:
    """Best-effort series-name extracted from an episode filename.

    Implements step 1-3 of phase6_design.md §7:
        1. Take the file's stem
        2. Strip S##E## / quality tags
        3. Compute the "filename-derived series name"

    Returns "" when no clear series name can be inferred.
    """
    if not filename:
        return ""
    stem = _strip_known_video_ext(filename)
    norm = _normalize_separators(stem)

    # Try each marker pattern; keep only the head before whichever matches.
    earliest = len(norm)
    for rgx in (RE_S_E, RE_X_FORM, RE_BARE_EP):
        m = rgx.search(norm)
        if m and m.start() < earliest:
            earliest = m.start()
    head = norm[:earliest] if earliest < len(norm) else norm

    head = RE_BRACKETS.sub(" ", head)
    head = QUALITY_TAG_RE.sub(" ", head)
    head = RE_PAREN_YEAR.sub(" ", head)
    head = re.sub(r"[-_.]+", " ", head)
    head = _collapse_spaces(head)
    head = head.strip(" -._,")
    return head


# ---------------------------------------------------------------------------
# Public API: normalize_folder_name + tokenize_for_clustering (merge §6)
# ---------------------------------------------------------------------------

def normalize_folder_name(folder_name: str) -> str:
    """Canonicalize a folder name for the reverse-grouping clusterer.

    Implements steps 1-4 of phase6_design.md §6:
        1. Strip leading track numbers / disc / Title / Playlist prefixes
        2. Strip quality tags
        3. Lowercase + drop non-alphanumeric (as separators)
        4. Drop stopwords

    Returns a space-separated string of stable tokens. Two folder names that
    "should" cluster will share most tokens.
    """
    if not folder_name:
        return ""
    name = folder_name

    # Strip trailing slashes/whitespace defensively.
    name = name.rstrip("\\/").strip()

    # Drop a leading paren-year so "Mr. Bean (1990)" and "Mr Bean" share tokens.
    name = RE_PAREN_YEAR.sub(" ", name)
    # Drop bracketed release-group annotations.
    name = RE_BRACKETS.sub(" ", name)

    # Normalize underscore/dot separators between word chars FIRST. The
    # quality and leading-track regexes use \b-style anchors; without this
    # pass, names like "200_CARTOONS_DISC1_Title2" leave underscores adjacent
    # to "DISC1"/"Title2" and the regex can't see them as word-bounded tokens.
    name = _normalize_separators(name)

    # Strip leading track / disc / title prefixes (loop in case of "Disc 2 T00 Foo").
    prev = None
    while prev != name:
        prev = name
        for rgx in LEADING_TRACK_RES:
            name = rgx.sub("", name)

    # Strip quality / release tags anywhere.
    name = QUALITY_TAG_RE.sub(" ", name)

    # Lowercase, drop remaining non-alphanumerics.
    name = name.lower()
    name = re.sub(r"[^a-z0-9]+", " ", name)
    name = _collapse_spaces(name)

    # Drop stopwords AND length-1 tokens token-by-token. Length-1 tokens come
    # from possessive splits ("Dexter's" -> "dexter s") and isolated digits
    # in folder names ("Bluey Seasons 1, 2, and 3"); both bridge unrelated
    # folders if kept. Preserve order for deterministic output.
    tokens = [t for t in name.split() if t and len(t) > 1 and t not in STOPWORDS]
    return " ".join(tokens)


def tokenize_for_clustering(name: str) -> Set[str]:
    """Token set for Jaccard / overlap scoring in the clusterer.

    Same normalization pipeline as normalize_folder_name, but returns a set so
    callers can compute set-theoretic similarity directly.
    """
    return set(normalize_folder_name(name).split())


def leading_episode_number(folder_name: str) -> Optional[int]:
    """Extract the leading episode number from a merge-group member folder name.

    Per phase6_design.md §6: "Episode S/E numbers come from the leading number
    in each source folder name (`01 MrBean` → S01E01, `02 ...` → S01E02)."

    Returns None when the folder doesn't start with a number.
    """
    if not folder_name:
        return None
    m = RE_LEADING_NUM.match(folder_name.strip())
    if not m:
        return None
    n = int(m.group("num"))
    # Avoid mistaking a leading year ("1990 Foo") for an episode number.
    if 1900 <= n <= 2099 and len(m.group("num")) == 4:
        return None
    return n


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

def _format_result(p: ParsedEpisode) -> str:
    lines = [
        f"  filename:          {p.raw_filename!r}",
        f"  parent_folder:     {p.raw_parent_folder!r}",
        f"  parsed:            {p.parsed}",
        f"  episode_key:       {p.episode_key}",
        f"  season:            {p.season}",
        f"  episode:           {p.episode}",
        f"  episode_end:       {p.episode_end}",
        f"  is_multi:          {p.is_multi}",
        f"  is_special:        {p.is_special}",
        f"  is_absolute:       {p.is_absolute}",
        f"  pattern_matched:   {p.pattern_matched!r}",
        f"  confidence:        {p.confidence}",
        f"  series_name_guess: {p.series_name_guess!r}",
        f"  flags:             {p.flags}",
    ]
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Parse a TV episode filename (smoke test).")
    ap.add_argument("filename", help="Episode filename (with or without extension)")
    ap.add_argument("--parent", default=None, help="Immediate parent folder name (e.g. 'Season 1')")
    ap.add_argument("--allow-absolute", action="store_true",
                    help="Enable absolute-numbering fallback (anime-style)")
    ap.add_argument("--show-derive", action="store_true",
                    help="Also print derive_series_from_filename and normalize_folder_name results")
    args = ap.parse_args(argv)

    p = parse_episode(args.filename, parent_folder=args.parent, allow_absolute=args.allow_absolute)
    print("parse_episode:")
    print(_format_result(p))
    if args.show_derive:
        print()
        print(f"derive_series_from_filename: {derive_series_from_filename(args.filename)!r}")
        if args.parent:
            print(f"normalize_folder_name:       {normalize_folder_name(args.parent)!r}")
            print(f"tokenize_for_clustering:     {sorted(tokenize_for_clustering(args.parent))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
