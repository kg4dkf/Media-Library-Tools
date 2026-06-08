#!/usr/bin/env python3
"""
retmdb_skips.py - Re-attempt TMDB lookups for SKIP rows that previously
failed with no_tmdb_results.

Two-phase workflow:

  Phase 1 (propose):  py -3.13 retmdb_skips.py --config config.json
                      Reads series_plan.csv, finds rows with action=SKIP and
                      flag=no_tmdb_results, retries TMDB with a series of
                      cleanup heuristics, and writes retmdb_proposed.csv
                      listing what was found at each confidence level.

  Phase 2 (commit):   py -3.13 retmdb_skips.py --config config.json \
                          --commit H:\\_mediaprep\\retmdb_proposed.csv
                      Reads the proposal CSV (which the user has reviewed
                      and edited), and applies accepted rows back to
                      series_plan.csv (atomic rewrite). Rows where the
                      user picked a tmdb_id get action=AUTO with the new
                      matched_series/year/target_folder filled in. Rows
                      marked 'skip' stay SKIP with a retmdb_attempted flag
                      so we don't rerun them.

Search strategies, tried in sequence per row:

  1. Folder-name cleanup. Strip torrent/release tags ([ax]_, [Erai-raws],
     aXXo, etc.), quality tokens (1080p, x264, BluRay, ...), structural
     cruft (Complete, Vol, Disc, Season N, Pack, Collection), and trailing
     years/braces. Re-search.

  2. Episode-filename hint. List the first few video files inside the
     source folder. Extract the most-common "show name" prefix (text
     before SxxExx or first " - "). Re-search.

  3. Year-augmented search. If a year survived the cleanup, pass it as
     first_air_date_year hint.

For each row, the best candidate (or top 3 with low confidence) is recorded.

Confidence:
  HIGH    - exactly 1 result, name closely matches our search term, vote_count
            non-trivial. Will be auto-committed unless the user blanks the
            'pick' column.
  MEDIUM  - multiple results but a clear leader, OR single weak match. User
            should review and edit the 'pick' column.
  NONE    - all strategies produced no results. Stays SKIP with
            retmdb_attempted flag.

The user can edit retmdb_proposed.csv between phases:
  - Set pick = '' to reject all candidates (stays SKIP with retmdb_attempted)
  - Set pick = N to pick candidate N (1-indexed; 0 = none)

Pure stdlib + tv_tmdb_client. No new external dependencies.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import os
import re
import sys
import traceback
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# Local imports.
sys.path.insert(0, str(Path(__file__).parent))
from tv_tmdb_client import TmdbTvClient, is_error, is_not_found


SCRIPT_VERSION = "1.1.0"

# Video extensions used to find sample episode filenames.
VIDEO_EXTS = {
    ".mkv", ".mp4", ".avi", ".mov", ".wmv", ".m4v", ".webm", ".flv",
    ".mpeg", ".mpg", ".ts", ".m2ts", ".mts", ".vob", ".divx", ".ogm",
}

log = logging.getLogger("retmdb_skips")


# =========================================================================
# Folder-name cleanup
# =========================================================================

# Bracketed tags: [ax], [Erai-raws], [a5d9364b], (1080p), etc.
_BRACKET_RE = re.compile(r"[\[(\{][^\]\)\}]*[\]\)\}]")
# Year (1985), (2003-2007), etc.
_YEAR_RE = re.compile(r"\((\d{4})(?:-(\d{4}))?\)")

# Tokens (case-insensitive) that are almost never part of a real show name.
# NOTE: "show", "the", and "season" are deliberately omitted because they
# appear in real titles (e.g., "The Office", "Mr. Show", "Season Pass").
# The _COMPOUND_CRUFT substring check below catches torrent-style mashups
# like "PackDvDrip" that don't whitespace-split into individual tokens.
_RELEASE_TOKENS: frozenset = frozenset({
    "complete", "completeseries", "completedvd",
    "collection", "volume", "vol", "disc", "discs", "dvd", "dvdrip",
    "bluray", "bd", "brrip", "bdrip", "hdtv", "pdtv", "webrip", "webdl",
    "1080p", "720p", "480p", "576p", "2160p", "4k", "uhd",
    "x264", "x265", "h264", "h265", "hevc", "xvid", "divx", "aac", "ac3",
    "mp3", "dts", "dolby", "remastered", "extended", "uncut",
    "directors", "proper", "repack", "internal", "limited",
    "rerip", "subbed", "dubbed", "uncensored", "censored",
    "axxo", "yify", "rarbg", "ettv", "fgt", "ntb", "evo", "ftp",
    "seasons", "series", "pack", "packs", "boxset",
})

# Substrings that, when found anywhere in a token (case-insensitive), mark
# the whole token as torrent cruft. Catches compound mashups like
# "PackDvDrip", "BluRay1080p", "x264-FGT" that survive token splitting.
_COMPOUND_CRUFT: tuple = (
    "dvdrip", "bluray", "brrip", "bdrip", "hdtv", "pdtv", "webrip",
    "x264", "x265", "h264", "h265", "hevc", "xvid",
    "1080p", "720p", "480p", "2160p",
    "axxo", "yify", "rarbg",
)

# Patterns that indicate "Season N", "S1-3", etc. — we want to drop these.
_SEASON_RE = re.compile(r"\b(?:s|season|series|seasons)\s*\d+(?:[-–]\d+)?\b",
                         re.IGNORECASE)


def clean_folder_name(name: str) -> Tuple[str, Optional[int]]:
    """Strip torrent cruft, quality tags, season/year markers from a folder
    name. Returns (cleaned_name, year_hint).

    If a year was visible (e.g., "Mr. Show (1995)"), it's returned as a hint.
    Years inside the cleaned name are removed.
    """
    s = name
    # Pull out years first so we can return as hint.
    year_hint: Optional[int] = None
    m = _YEAR_RE.search(s)
    if m:
        try:
            year_hint = int(m.group(1))
        except ValueError:
            pass
    s = _YEAR_RE.sub(" ", s)
    # Strip bracketed tags.
    s = _BRACKET_RE.sub(" ", s)
    # Strip "Season N" / "S1-3" patterns.
    s = _SEASON_RE.sub(" ", s)
    # Replace separators with spaces.
    s = re.sub(r"[._\-]+", " ", s)
    # Normalize whitespace.
    s = re.sub(r"\s+", " ", s).strip()
    # Tokenize and drop tokens that match the stoplist or contain any of
    # the compound-cruft substrings (e.g., "PackDvDrip" -> contains "dvdrip").
    def _is_cruft(tok: str) -> bool:
        low = tok.lower().strip("-.,()")
        if not low:
            return True
        if low in _RELEASE_TOKENS:
            return True
        return any(sub in low for sub in _COMPOUND_CRUFT)
    tokens = [t for t in s.split() if not _is_cruft(t)]
    # Drop pure-digit tokens of >=4 digits (year-leftovers, rip metadata).
    # When the whole input was a year (e.g. clean_folder_name("2002")) the
    # token survives unless we drop unconditionally — which is what we want:
    # a query of "2002" alone is useless and just returns junk results.
    # Short pure-digit tokens (e.g., "30" in "30 Rock") are kept since they
    # can be part of a real title.
    tokens = [t for t in tokens if not (t.isdigit() and len(t) >= 4)]
    # Re-join.
    cleaned = " ".join(tokens).strip()
    return cleaned, year_hint


# =========================================================================
# Episode-filename hint extraction
# =========================================================================

_EPISODE_NUM_RE = re.compile(
    r"\b(?:s\d+e\d+|season\s*\d+|episode\s*\d+|\d+x\d+|\b\d{1,3}\b)",
    re.IGNORECASE,
)


def extract_show_hint_from_filename(fname: str) -> str:
    """Extract a likely show-name prefix from an episode filename.

    Rules:
      - Drop the extension.
      - Cut everything from the first SxxExx / NxN match onward.
      - Cut at the first " - " separator if present.
      - Strip release tokens.
    """
    stem = Path(fname).stem
    # Cut at first SxxExx match.
    m = _EPISODE_NUM_RE.search(stem)
    if m:
        stem = stem[:m.start()]
    # Cut at first " - " or "_-_" separator.
    for sep in (" - ", " — ", "_-_"):
        if sep in stem:
            stem = stem.split(sep, 1)[0]
            break
    # Now run the cleanup heuristic.
    cleaned, _ = clean_folder_name(stem)
    return cleaned


def show_hint_from_folder(source: Path, max_files: int = 5) -> str:
    """Sample a few video files in the folder and extract the most-common
    show-name prefix. Returns "" if no usable files."""
    if not source.is_dir():
        return ""
    counts: Dict[str, int] = {}
    n = 0
    try:
        for child in sorted(source.iterdir()):
            if child.is_file() and child.suffix.lower() in VIDEO_EXTS:
                hint = extract_show_hint_from_filename(child.name)
                if (hint and len(hint) >= 3
                        and not hint.replace(" ", "").isdigit()):
                    counts[hint] = counts.get(hint, 0) + 1
                    n += 1
            elif child.is_dir():
                # One level of recursion for season subfolders.
                try:
                    for grand in sorted(child.iterdir()):
                        if grand.is_file() and grand.suffix.lower() in VIDEO_EXTS:
                            hint = extract_show_hint_from_filename(grand.name)
                            if (hint and len(hint) >= 3
                        and not hint.replace(" ", "").isdigit()):
                                counts[hint] = counts.get(hint, 0) + 1
                                n += 1
                                if n >= max_files:
                                    break
                except OSError:
                    pass
            if n >= max_files:
                break
    except OSError:
        return ""
    if not counts:
        return ""
    return max(counts.items(), key=lambda kv: kv[1])[0]


# =========================================================================
# Confidence scoring
# =========================================================================

@dataclass
class TmdbCandidate:
    tmdb_id: int
    name: str
    first_air_year: Optional[int]
    overview: str
    vote_count: int
    popularity: float
    score: float = 0.0

    def to_csv(self) -> str:
        y = f" ({self.first_air_year})" if self.first_air_year else ""
        return f"{self.tmdb_id}|{self.name}{y}"


def name_similarity(query: str, candidate: str) -> float:
    """0.0 - 1.0 similarity using normalized SequenceMatcher ratio + token
    overlap as a tiebreaker."""
    def _norm(s: str) -> str:
        s = unicodedata.normalize("NFKD", s)
        s = "".join(c for c in s if not unicodedata.combining(c))
        s = re.sub(r"[^a-z0-9 ]+", " ", s.lower())
        return re.sub(r"\s+", " ", s).strip()
    a, b = _norm(query), _norm(candidate)
    if not a or not b:
        return 0.0
    seq = SequenceMatcher(None, a, b).ratio()
    # Token-set jaccard
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        jac = 0.0
    else:
        jac = len(ta & tb) / len(ta | tb)
    return 0.6 * seq + 0.4 * jac


def score_candidates(query: str, results: List[Dict[str, Any]],
                     year_hint: Optional[int]) -> List[TmdbCandidate]:
    out: List[TmdbCandidate] = []
    for r in results:
        first_air = str(r.get("first_air_date") or "")[:4]
        try:
            year = int(first_air) if first_air.isdigit() else None
        except ValueError:
            year = None
        cand = TmdbCandidate(
            tmdb_id=int(r.get("id", 0)),
            name=str(r.get("name", "") or r.get("original_name", "")),
            first_air_year=year,
            overview=str(r.get("overview", "")),
            vote_count=int(r.get("vote_count", 0) or 0),
            popularity=float(r.get("popularity", 0.0) or 0.0),
        )
        # Score:
        #   - 60% name similarity
        #   - 20% year match (if hint provided)
        #   - 20% popularity (log-scaled)
        sim = name_similarity(query, cand.name)
        year_score = 0.0
        if year_hint and cand.first_air_year:
            diff = abs(cand.first_air_year - year_hint)
            year_score = max(0.0, 1.0 - diff / 5.0)  # 5+ years apart => 0
        pop_score = min(1.0, cand.popularity / 50.0)  # popularity > 50 saturates
        cand.score = 0.6 * sim + 0.2 * year_score + 0.2 * pop_score
        out.append(cand)
    out.sort(key=lambda c: -c.score)
    return out


@dataclass
class Verdict:
    confidence: str   # HIGH | MEDIUM | NONE
    candidates: List[TmdbCandidate]
    strategy: str     # which search-strategy produced these
    cleaned_query: str
    year_hint: Optional[int]


def assess(candidates: List[TmdbCandidate]) -> str:
    if not candidates:
        return "NONE"
    top = candidates[0]
    runner = candidates[1].score if len(candidates) > 1 else 0.0
    # HIGH: top score >= 0.85, vote_count > 20, beats runner-up by 0.2
    if top.score >= 0.85 and top.vote_count > 20 and (top.score - runner) >= 0.2:
        return "HIGH"
    # HIGH: only one result, score >= 0.75 (TMDB's own dedup is decent)
    if len(candidates) == 1 and top.score >= 0.75:
        return "HIGH"
    # MEDIUM: top score >= 0.5 OR (top score >= 0.6 even if close)
    if top.score >= 0.5:
        return "MEDIUM"
    return "NONE"


# =========================================================================
# Search orchestration
# =========================================================================

def search_with_strategies(client: TmdbTvClient, source_folder: str,
                            tv_root: Path) -> Verdict:
    """Run search strategies in sequence; return the first non-NONE verdict
    or NONE if all strategies fail."""
    leaf = Path(source_folder).name

    # Strategy 1: cleaned folder name
    cleaned, year_hint = clean_folder_name(leaf)
    if cleaned:
        v = _do_search(client, cleaned, year_hint, "folder_clean")
        if v.confidence != "NONE":
            return v

    # Strategy 2: episode-filename hint
    src_path = Path(source_folder)
    hint = show_hint_from_folder(src_path, max_files=8)
    if hint and hint.lower() != cleaned.lower():
        v = _do_search(client, hint, year_hint, "episode_hint")
        if v.confidence != "NONE":
            return v

    # Strategy 3: cleaned-name without year hint (might have been wrong)
    if cleaned and year_hint:
        v = _do_search(client, cleaned, None, "folder_clean_no_year")
        if v.confidence != "NONE":
            return v

    # Return the best NONE-tier result we got, if any (so the proposal CSV
    # can show what TMDB returned).
    if cleaned:
        return _do_search(client, cleaned, year_hint, "folder_clean")
    return Verdict("NONE", [], "no_query", "", year_hint)


def _do_search(client: TmdbTvClient, query: str,
               year_hint: Optional[int], strategy: str) -> Verdict:
    if not query.strip():
        return Verdict("NONE", [], strategy, query, year_hint)
    try:
        data = client.search_tv(query, first_air_date_year=year_hint)
    except Exception as e:
        log.warning("TMDB search failed for %r: %s", query, e)
        return Verdict("NONE", [], strategy, query, year_hint)
    if is_error(data) or is_not_found(data):
        return Verdict("NONE", [], strategy, query, year_hint)
    results = data.get("results") or []
    cands = score_candidates(query, results, year_hint)
    confidence = assess(cands)
    return Verdict(confidence, cands[:5], strategy, query, year_hint)


# =========================================================================
# CSV I/O
# =========================================================================

def _read_csv_rows(path: Path) -> Tuple[List[Dict[str, str]], List[str]]:
    if not path.is_file():
        raise SystemExit(f"Not found: {path}")
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = raw.decode("utf-16", errors="replace")
    else:
        text = raw.replace(b"\x00", b"").decode("utf-8-sig", errors="replace")
    rdr = csv.DictReader(text.splitlines())
    fieldnames = list(rdr.fieldnames or [])
    rows = list(rdr)
    return rows, fieldnames


PROPOSAL_COLS = [
    "series_id", "source_folder", "old_flags", "old_notes",
    "previous_tmdb_id", "previous_matched_series",
    "confidence", "strategy", "cleaned_query", "year_hint",
    "cand_1", "cand_1_score",
    "cand_2", "cand_2_score",
    "cand_3", "cand_3_score",
    "cand_4", "cand_4_score",
    "cand_5", "cand_5_score",
    "pick", "user_notes",
]


def write_proposal(path: Path,
                   results: List[Tuple[Dict[str, str], Verdict]]) -> None:
    """Write retmdb_proposed.csv. Pick column is auto-filled with '1' for
    HIGH confidence rows that DON'T already have a previous match (i.e.
    fresh no_tmdb_results rows). For multi_result rows that already had
    a guess, pick stays blank to force user review."""
    with open(path, "w", encoding="utf-8-sig", newline="",
              errors="replace") as fh:
        w = csv.DictWriter(fh, fieldnames=PROPOSAL_COLS)
        w.writeheader()
        for plan_row, verdict in results:
            previous_tmdb = (plan_row.get("tmdb_series_id") or "").strip()
            previous_match = (plan_row.get("matched_series") or "").strip()
            # Auto-pick only when no prior guess existed.
            auto_pick = (verdict.confidence == "HIGH" and not previous_tmdb)
            d: Dict[str, str] = {
                "series_id": plan_row.get("series_id", ""),
                "source_folder": plan_row.get("source_folder", ""),
                "old_flags": plan_row.get("flags", ""),
                "old_notes": plan_row.get("notes", ""),
                "previous_tmdb_id": previous_tmdb,
                "previous_matched_series": previous_match,
                "confidence": verdict.confidence,
                "strategy": verdict.strategy,
                "cleaned_query": verdict.cleaned_query,
                "year_hint": str(verdict.year_hint or ""),
                "pick": "1" if auto_pick else "",
                "user_notes": "",
            }
            for i in range(5):
                if i < len(verdict.candidates):
                    c = verdict.candidates[i]
                    d[f"cand_{i+1}"] = c.to_csv()
                    d[f"cand_{i+1}_score"] = f"{c.score:.3f}"
                else:
                    d[f"cand_{i+1}"] = ""
                    d[f"cand_{i+1}_score"] = ""
            w.writerow(d)


def render_target_folder(matched_series: str, year: Optional[int],
                         tmdb_id: int, tv_root: Path) -> str:
    """Build canonical target folder path matching execute_tv's render_series_folder."""
    title = matched_series
    # Mirror sanitize_for_windows lite (just the chars that break paths).
    repl = {":": " - ", "/": " ", "\\": " ", '"': "'", "|": "-",
            "?": "", "*": "", "<": "(", ">": ")"}
    for k, v in repl.items():
        title = title.replace(k, v)
    title = re.sub(r"\s+", " ", title).strip().rstrip(". ")
    y = str(year) if year else ""
    name = f"{title} ({y}) {{tmdb-{tmdb_id}}}"
    return str(tv_root / name)


# =========================================================================
# Phase 1: propose
# =========================================================================

def phase_propose(plan_path: Path, out_path: Path, client: TmdbTvClient,
                  cfg: Dict[str, Any], limit: int = 0,
                  series_filter: Optional[set] = None,
                  include_multi: bool = False) -> int:
    rows, fieldnames = _read_csv_rows(plan_path)
    def _is_eligible(r: Dict[str, str]) -> bool:
        if (r.get("action", "") or "").strip().upper() != "SKIP":
            return False
        flags = r.get("flags", "") or ""
        if "retmdb_attempted" in flags or "disambig_attempted" in flags:
            return False
        if "no_tmdb_results" in flags:
            return True
        if include_multi and "multi_result" in flags:
            return True
        return False
    eligible = [r for r in rows if _is_eligible(r)]
    if series_filter:
        eligible = [r for r in eligible
                    if int(r.get("series_id", "0") or "0") in series_filter]
    if limit:
        eligible = eligible[:limit]
    no_match = sum(1 for r in eligible if "no_tmdb_results" in (r.get("flags") or ""))
    multi = sum(1 for r in eligible if "multi_result" in (r.get("flags") or ""))
    log.info("%d eligible SKIP rows to retry  (no_tmdb_results=%d, multi_result=%d)",
             len(eligible), no_match, multi)

    tv_root = Path(cfg.get("tv_root", "G:\\TV"))
    out: List[Tuple[Dict[str, str], Verdict]] = []
    by_conf: Dict[str, int] = {"HIGH": 0, "MEDIUM": 0, "NONE": 0}
    for i, r in enumerate(eligible, start=1):
        sid = r.get("series_id", "?")
        src = r.get("source_folder", "")
        verdict = search_with_strategies(client, src, tv_root)
        by_conf[verdict.confidence] = by_conf.get(verdict.confidence, 0) + 1
        out.append((r, verdict))
        if i % 25 == 0:
            log.info("Progress: %d/%d (HIGH=%d MEDIUM=%d NONE=%d)",
                     i, len(eligible), by_conf.get("HIGH", 0),
                     by_conf.get("MEDIUM", 0), by_conf.get("NONE", 0))

    write_proposal(out_path, out)
    log.info("=" * 60)
    log.info("Proposals written to: %s", out_path)
    log.info("HIGH (auto-pick=1): %d", by_conf.get("HIGH", 0))
    log.info("MEDIUM (review):    %d", by_conf.get("MEDIUM", 0))
    log.info("NONE (no result):   %d", by_conf.get("NONE", 0))
    log.info("Edit the CSV, set 'pick' column (1-5 for candidate, blank/0 to reject),")
    log.info("then run: retmdb_skips.py --commit %s", out_path)
    return 0


# =========================================================================
# Phase 2: commit
# =========================================================================

def phase_commit(plan_path: Path, proposal_path: Path,
                 cfg: Dict[str, Any]) -> int:
    plan_rows, fieldnames = _read_csv_rows(plan_path)
    by_id: Dict[str, Dict[str, str]] = {
        str(r.get("series_id", "")).strip(): r for r in plan_rows
    }
    proposal_rows, _ = _read_csv_rows(proposal_path)
    tv_root = Path(cfg.get("tv_root", "G:\\TV"))

    accepted = 0
    rejected = 0
    skipped = 0
    errors = 0
    for prop in proposal_rows:
        sid = (prop.get("series_id", "") or "").strip()
        pick = (prop.get("pick", "") or "").strip()
        plan_row = by_id.get(sid)
        if not plan_row:
            log.warning("series_id %s not found in plan; skipping", sid)
            skipped += 1
            continue
        if not pick or pick == "0":
            # User rejected. Mark *_attempted so we don't retry next time.
            # multi_result rows get disambig_attempted (so they don't show up
            # again with --include-multi); plain no_tmdb_results rows get
            # retmdb_attempted.
            existing_flags = plan_row.get("flags", "") or ""
            tokens = [t for t in existing_flags.split(";") if t.strip()]
            attempted_flag = ("disambig_attempted"
                              if any(t.startswith("multi_result") for t in tokens)
                              else "retmdb_attempted")
            if attempted_flag not in tokens:
                tokens.append(attempted_flag)
            plan_row["flags"] = ";".join(tokens)
            rejected += 1
            continue
        try:
            n = int(pick)
        except ValueError:
            log.warning("series_id %s: bad pick %r; skipping", sid, pick)
            errors += 1
            continue
        if n < 1 or n > 5:
            log.warning("series_id %s: pick %d out of range; skipping", sid, n)
            errors += 1
            continue
        cand_field = (prop.get(f"cand_{n}", "") or "").strip()
        if not cand_field:
            log.warning("series_id %s: pick=%d but cand_%d is empty; skipping",
                        sid, n, n)
            errors += 1
            continue
        # cand format: "<tmdb_id>|<name> (<year>)"
        try:
            tmdb_id_str, label = cand_field.split("|", 1)
            tmdb_id = int(tmdb_id_str)
        except ValueError:
            log.warning("series_id %s: malformed cand_%d=%r; skipping",
                        sid, n, cand_field)
            errors += 1
            continue
        m = re.search(r"\((\d{4})\)\s*$", label)
        year = int(m.group(1)) if m else None
        # Strip the year from label for cleanest title.
        title = re.sub(r"\s*\(\d{4}\)\s*$", "", label).strip()
        # Update plan row.
        plan_row["tmdb_series_id"] = str(tmdb_id)
        plan_row["matched_series"] = title
        if year:
            plan_row["matched_year"] = str(year)
        plan_row["target_folder"] = render_target_folder(title, year, tmdb_id, tv_root)
        plan_row["confidence"] = "70"  # human-confirmed
        plan_row["action"] = "AUTO"
        # Update flags: drop no_tmdb_results / multi_result_*, add committed marker.
        existing_flags = plan_row.get("flags", "") or ""
        had_multi = any(t.strip().startswith("multi_result")
                        for t in existing_flags.split(";"))
        tokens = [t for t in existing_flags.split(";")
                  if t.strip()
                  and t.strip() != "no_tmdb_results"
                  and not t.strip().startswith("multi_result")]
        tokens.append("disambig_committed" if had_multi else "retmdb_committed")
        plan_row["flags"] = ";".join(tokens)
        # Notes augmentation
        existing_notes = plan_row.get("notes", "") or ""
        kind = "disambig" if had_multi else "retmdb"
        msg = f"{kind}: matched via {prop.get('strategy','?')} (cand={n})"
        plan_row["notes"] = f"{existing_notes}; {msg}".strip("; ").strip()
        accepted += 1

    # Atomic rewrite of series_plan.csv.
    tmp = plan_path.with_suffix(plan_path.suffix + ".retmdb_tmp")
    with open(tmp, "w", encoding="utf-8-sig", newline="",
              errors="replace") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        for r in plan_rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})
    os.replace(tmp, plan_path)

    log.info("=" * 60)
    log.info("Commit summary:")
    log.info("  accepted (action=AUTO):    %d", accepted)
    log.info("  rejected (retmdb_attempted): %d", rejected)
    log.info("  errors:                     %d", errors)
    log.info("  skipped (sid not in plan):  %d", skipped)
    log.info("Updated: %s", plan_path)
    return 0 if errors == 0 else 3


# =========================================================================
# Main
# =========================================================================

def setup_logging(out_dir: Path, run_id: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / f"retmdb_{run_id}.log"
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt); fh.setLevel(logging.DEBUG)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt); sh.setLevel(logging.INFO)
    log.setLevel(logging.DEBUG)
    if not log.handlers:
        log.addHandler(fh); log.addHandler(sh)
    log.info("retmdb_skips.py v%s starting; log file: %s",
             SCRIPT_VERSION, log_path)
    return log_path


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Re-attempt TMDB lookups for SKIP rows that previously "
                    "returned no results. Two phases: propose (default) and "
                    "commit (--commit <proposal.csv>).")
    ap.add_argument("--config", required=True, help="Path to config.json")
    ap.add_argument("--commit", default=None,
                    help="Phase 2: apply user-reviewed retmdb_proposed.csv "
                         "to series_plan.csv")
    ap.add_argument("--out", default=None,
                    help="Phase 1 output CSV path (default: "
                         "<mediaprep>/retmdb_proposed.csv)")
    ap.add_argument("--limit", type=int, default=0,
                    help="Phase 1: process at most N rows (smoke testing)")
    ap.add_argument("--series", default="",
                    help="Phase 1: comma-separated series_ids to process; "
                         "ignores others")
    ap.add_argument("--include-multi", action="store_true",
                    help="Phase 1: ALSO include rows flagged "
                         "multi_result_* (low-confidence TMDB hits the user "
                         "needs to disambiguate). Default: only no_tmdb_results.")
    return ap.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    cfg_path = Path(args.config)
    if not cfg_path.is_file():
        raise SystemExit(f"Config not found: {cfg_path}")
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    mediaprep_dir = Path(cfg.get("mediaprep_dir", "."))
    plan_path = mediaprep_dir / "series_plan.csv"

    run_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    setup_logging(mediaprep_dir, run_id)

    if args.commit:
        log.info("Mode: COMMIT")
        return phase_commit(plan_path, Path(args.commit), cfg)

    out_path = Path(args.out) if args.out else mediaprep_dir / "retmdb_proposed.csv"
    log.info("Mode: PROPOSE")
    log.info("  series_plan: %s", plan_path)
    log.info("  output:      %s", out_path)
    if args.include_multi:
        log.info("  --include-multi: also processing multi_result_* rows")

    api_key = cfg.get("tmdb_api_key")
    if not api_key or api_key in ("", "REPLACE_ME"):
        raise SystemExit("tmdb_api_key not set in config.json")
    cache_path = mediaprep_dir / "tmdb_tv_cache.sqlite"
    lang = cfg.get("tmdb_tv_language") or cfg.get("language", "en-US")
    client = TmdbTvClient(api_key, cache_path, language=lang)

    series_filter = None
    if args.series:
        series_filter = {int(x.strip()) for x in args.series.split(",")
                         if x.strip()}

    try:
        return phase_propose(plan_path, out_path, client, cfg,
                              limit=args.limit,
                              series_filter=series_filter,
                              include_multi=args.include_multi)
    finally:
        client.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        log.error("Unhandled exception:\n%s", traceback.format_exc())
        sys.exit(2)
