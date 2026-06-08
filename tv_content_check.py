#!/usr/bin/env python3
"""
tv_content_check.py - Phase 6 cross-folder content mismatch detector.

Implements phase6_design.md §7: detect folders that contain episodes from a
DIFFERENT show than the folder name suggests (typical cause: a misplaced rip).
Used by inventory_tv.py during phase 1 to populate `mixed_content.csv`;
unit-testable in isolation; reuses tv_parser.derive_series_from_filename
and tv_parser.normalize_folder_name as primitives.

Algorithm (design §7, refined after real-data review):
    1. Take each episode file's stem.
    2. Strip S##E## / quality tags / paren-years to derive a "filename-
       derived series name" (delegated to tv_parser.derive_series_from_filename).
    3. Normalize each derivation the same way folder names get normalized
       (so the comparison is apples-to-apples).
    4. The most-common normalized derivation across the folder's episodes is
       the "dominant content".
    5. Compare the normalized folder name against the normalized dominant
       via BIDIRECTIONAL CONTAINMENT (max of folder-in-dominant ratio and
       dominant-in-folder ratio). If that score < threshold, flag the folder
       as `mixed_content`.

       The bidirectional metric (vs. symmetric Jaccard) correctly accepts
       cases like folder="Family Guy" + filename="Family Guy 101 Death Has
       a Shadow.mkv" — the folder name's tokens are fully contained in the
       filename-derived tokens, even though the dominant has 4 extra tokens.
       Symmetric Jaccard would falsely flag this as mixed content.

Phase 3 doesn't try to outsmart this. Mixed-content folders need human
attention (REVIEW / SKIP / RELOCATE per §7's user resolution options).

Public API:
    ContentCheckResult                dataclass
    check_folder_content(...)         main entry point
    DEFAULT_*                         threshold constants

CLI smoke test:
    py tv_content_check.py "Doctor Who" \\
        "Doctor Who S01E01 Rose.mkv" \\
        "Doctor Who S01E02 The End of the World.mkv"
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Set, Tuple

from tv_parser import (
    derive_series_from_filename,
    normalize_folder_name,
)


SCRIPT_VERSION = "1.0.0"

# Defaults tuned for real-world libraries. The threshold is intentionally
# permissive — the CSV review step is the user's gate, and we'd rather
# present a borderline match for review than silently file a misplaced rip.
DEFAULT_SIMILARITY_THRESHOLD = 0.5
DEFAULT_MIN_EPISODES_FOR_CHECK = 2

log = logging.getLogger("tv_content_check")


# ---------------------------------------------------------------------------
# Verdict constants — typed strings for the result.verdict field
# ---------------------------------------------------------------------------

VERDICT_OK = "ok"
VERDICT_MIXED = "mixed_content"
VERDICT_NO_EPISODES = "inconclusive_no_episodes"
VERDICT_NO_PARSES = "inconclusive_no_parses"
VERDICT_TOO_FEW = "inconclusive_too_few"
VERDICT_EMPTY_FOLDER = "inconclusive_empty_folder_name"


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------

@dataclass
class ContentCheckResult:
    """One row in mixed_content.csv (phase6_design.md §1).

    Maps to the CSV columns:
        folder_name              -> folder_name
        dominant_filename_series -> dominant_filename_series
        episode_count_dominant   -> dominant_count
        episode_count_other      -> other_count
        action                   -> "REVIEW" by default per §7
        manual_resolution        -> blank (filled by user)

    Extra fields (similarity, total_episode_count, verdict, notes) are
    diagnostics for the log and for human review.
    """
    folder_name: str
    folder_normalized: str = ""
    dominant_filename_series: str = ""   # original-case representative
    dominant_normalized: str = ""
    dominant_count: int = 0
    other_count: int = 0                 # episodes whose derived series differs
    unparseable_count: int = 0           # episodes whose name had no derivation
    total_episode_count: int = 0
    similarity: float = 0.0              # Jaccard(folder_tokens, dominant_tokens)
    flag_as_mixed: bool = False
    verdict: str = VERDICT_NO_EPISODES
    notes: str = ""

    @property
    def is_inconclusive(self) -> bool:
        """True when the check produced no actionable verdict."""
        return self.verdict not in (VERDICT_OK, VERDICT_MIXED)

    def to_csv_row(self) -> dict:
        """Return a dict matching mixed_content.csv columns."""
        return {
            "folder_name": self.folder_name,
            "dominant_filename_series": self.dominant_filename_series,
            "episode_count_dominant": self.dominant_count,
            "episode_count_other": self.other_count,
            "action": "REVIEW",
            "manual_resolution": "",
            # diagnostics — useful when investigating a flag
            "similarity": f"{self.similarity:.2f}",
            "total_episode_count": self.total_episode_count,
            "unparseable_count": self.unparseable_count,
            "verdict": self.verdict,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _jaccard(a: Set[str], b: Set[str]) -> float:
    """Jaccard index of two token sets. Returns 0.0 if either side is empty.

    Kept for compatibility but no longer used as the primary metric — see
    _bidirectional_containment below.
    """
    if not a or not b:
        return 0.0
    inter = a & b
    if not inter:
        return 0.0
    return len(inter) / len(a | b)


def _bidirectional_containment(folder: Set[str], dominant: Set[str]) -> float:
    """Containment that's symmetric in the right way for content-check.

    Returns max(|f∩d|/|f|, |f∩d|/|d|). The intuition:
      * folder "Family Guy" with dominant "Family Guy 101 Death Has Shadow"
        -> folder fully contained in dominant -> 1.0 (NORMAL, not flagged).
        Filenames legitimately include episode numbers and titles past the
        show name, and that's not mixed content.
      * folder "Star Trek The Original Series" with dominant "Star Trek"
        -> dominant fully contained in folder -> 1.0 (also normal — abbreviated
        filenames are fine).
      * folder "Friends" with dominant "The Office" -> 0 (genuine mismatch).

    A symmetric Jaccard would have flagged the first case at any threshold
    above ~0.4 because the dominant token set is much larger; this metric
    correctly accepts it.
    """
    if not folder or not dominant:
        return 0.0
    inter = folder & dominant
    if not inter:
        return 0.0
    return max(len(inter) / len(folder), len(inter) / len(dominant))


def _normalize_for_compare(name: str) -> str:
    """Normalize a derived series name the same way folder names normalize.

    Reuses tv_parser.normalize_folder_name so folder-vs-derivation comparisons
    use a consistent token vocabulary (stopwords stripped, length-1 dropped,
    quality tags removed, etc.).
    """
    return normalize_folder_name(name)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_folder_content(
    folder_name: str,
    episode_filenames: Iterable[str],
    *,
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    min_episodes_for_check: int = DEFAULT_MIN_EPISODES_FOR_CHECK,
) -> ContentCheckResult:
    """Compare the folder's stated name against the dominant series-name
    implied by its episode filenames, and flag if they disagree.

    Args:
        folder_name: Just the folder's basename (no path).
        episode_filenames: Basenames of the episode files inside the folder.
            Pass only video filenames; non-video sidecars are noise here.
        similarity_threshold: Folder is flagged mixed_content when the
            Jaccard similarity between its normalized tokens and the
            dominant filename-derived series's normalized tokens falls
            below this value. Default 0.5; tighten for stricter checks.
        min_episodes_for_check: Folders with fewer than this many parseable
            episode names are returned as inconclusive (statistical noise).

    Returns:
        A ContentCheckResult. `flag_as_mixed` is True iff the folder should
        be added to mixed_content.csv. `verdict` distinguishes the OK / mixed
        / inconclusive cases.
    """
    if not (0.0 <= similarity_threshold <= 1.0):
        raise ValueError(
            f"similarity_threshold must be in [0, 1], got {similarity_threshold!r}"
        )
    if min_episodes_for_check < 1:
        raise ValueError(
            f"min_episodes_for_check must be >= 1, got {min_episodes_for_check!r}"
        )

    folder_normalized = _normalize_for_compare(folder_name)
    folder_tokens = set(folder_normalized.split())

    if not folder_tokens:
        return ContentCheckResult(
            folder_name=folder_name,
            folder_normalized="",
            verdict=VERDICT_EMPTY_FOLDER,
            notes="folder name normalizes to no usable tokens",
        )

    # Walk filenames, derive a series-name for each.
    derivations: List[Tuple[str, str]] = []  # (original_derived, normalized_derived)
    unparseable = 0
    total = 0
    for fname in episode_filenames:
        if not fname:
            continue
        total += 1
        derived = derive_series_from_filename(fname)
        normalized = _normalize_for_compare(derived) if derived else ""
        if not normalized:
            unparseable += 1
            continue
        derivations.append((derived, normalized))

    if total == 0:
        return ContentCheckResult(
            folder_name=folder_name,
            folder_normalized=folder_normalized,
            total_episode_count=0,
            verdict=VERDICT_NO_EPISODES,
            notes="no episode filenames provided",
        )

    if not derivations:
        return ContentCheckResult(
            folder_name=folder_name,
            folder_normalized=folder_normalized,
            total_episode_count=total,
            unparseable_count=unparseable,
            verdict=VERDICT_NO_PARSES,
            notes="no filename produced a parseable series-name guess",
        )

    if len(derivations) < min_episodes_for_check:
        return ContentCheckResult(
            folder_name=folder_name,
            folder_normalized=folder_normalized,
            total_episode_count=total,
            unparseable_count=unparseable,
            verdict=VERDICT_TOO_FEW,
            notes=(
                f"only {len(derivations)} parseable episode(s); "
                f"need >= {min_episodes_for_check}"
            ),
        )

    # Find the mode (most common normalized derivation).
    counter: Counter[str] = Counter(n for (_, n) in derivations)
    dominant_normalized, dominant_count = counter.most_common(1)[0]
    other_count = sum(c for n, c in counter.items() if n != dominant_normalized)

    # Pick the first-occurring original-case derivation that maps to the
    # dominant normalized form. That's the human-readable representative.
    representative = next(orig for (orig, n) in derivations if n == dominant_normalized)

    dominant_tokens = set(dominant_normalized.split())
    similarity = _bidirectional_containment(folder_tokens, dominant_tokens)
    flag = similarity < similarity_threshold

    notes_parts: List[str] = []
    if other_count > 0:
        notes_parts.append(
            f"{other_count} of {len(derivations)} parseable episodes have a "
            f"different derived series-name"
        )
    if unparseable:
        notes_parts.append(f"{unparseable} unparseable filename(s)")
    notes = "; ".join(notes_parts)

    return ContentCheckResult(
        folder_name=folder_name,
        folder_normalized=folder_normalized,
        dominant_filename_series=representative,
        dominant_normalized=dominant_normalized,
        dominant_count=dominant_count,
        other_count=other_count,
        unparseable_count=unparseable,
        total_episode_count=total,
        similarity=similarity,
        flag_as_mixed=flag,
        verdict=VERDICT_MIXED if flag else VERDICT_OK,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

def _format_result(r: ContentCheckResult) -> str:
    lines = [
        f"folder_name:              {r.folder_name!r}",
        f"folder_normalized:        {r.folder_normalized!r}",
        f"verdict:                  {r.verdict}",
        f"flag_as_mixed:            {r.flag_as_mixed}",
        f"similarity:               {r.similarity:.3f}",
        f"dominant_filename_series: {r.dominant_filename_series!r}",
        f"dominant_normalized:      {r.dominant_normalized!r}",
        f"dominant_count:           {r.dominant_count}",
        f"other_count:              {r.other_count}",
        f"unparseable_count:        {r.unparseable_count}",
        f"total_episode_count:      {r.total_episode_count}",
        f"notes:                    {r.notes!r}",
    ]
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Detect folders whose episode filenames imply a different show "
            "than the folder name."
        ),
    )
    ap.add_argument("folder", help="Folder name (just the basename)")
    ap.add_argument("filenames", nargs="*",
                    help="Episode filenames inside the folder. If none given, reads one per line from stdin.")
    ap.add_argument("--threshold", type=float, default=DEFAULT_SIMILARITY_THRESHOLD,
                    help=f"Similarity threshold (default: {DEFAULT_SIMILARITY_THRESHOLD})")
    ap.add_argument("--min-episodes", type=int, default=DEFAULT_MIN_EPISODES_FOR_CHECK,
                    help=f"Min parseable episodes to evaluate (default: {DEFAULT_MIN_EPISODES_FOR_CHECK})")
    ap.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    if args.filenames:
        files: Iterable[str] = args.filenames
    else:
        files = [line.rstrip("\r\n") for line in sys.stdin if line.strip()]

    result = check_folder_content(
        args.folder,
        files,
        similarity_threshold=args.threshold,
        min_episodes_for_check=args.min_episodes,
    )
    print(_format_result(result))
    # Exit code conveys flag status: 0 ok, 1 mixed_content, 2 inconclusive.
    if result.flag_as_mixed:
        return 1
    if result.is_inconclusive:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
