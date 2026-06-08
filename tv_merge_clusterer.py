#!/usr/bin/env python3
"""
tv_merge_clusterer.py - Phase 6 reverse-grouping clusterer (the Mr. Bean fix).

Implements phase6_design.md §6: detect "flat" or single-episode-per-folder
messes and propose them for merging into one virtual series. ~388 of the 888
top-level folders in the target library are this shape.

Pure-stdlib library that takes a list of top-level folder names and returns
clusters where each cluster's members appear to be episodes of the same show.
Used by inventory_tv.py during phase 1 to populate `merge_candidates.csv`;
unit-testable in isolation; reuses tv_parser.normalize_folder_name and
tv_parser.tokenize_for_clustering as primitives.

Algorithm:
    1. Normalize each folder name (drop track numbers, quality tags, lower,
       drop stopwords) -> token list / set.
    2. For every folder pair (i, j) where i < j: link as "merge candidate"
       if ANY of three criteria pass:
         a. CONTAINMENT (primary): |intersection| / min(|a|, |b|) >=
            containment_threshold AND total chars in shared tokens >=
            min_shared_chars. This handles the §6 single-episode-wrapper
            case (Mr. Bean) without collapsing entire libraries through
            "bridge folders" like "Mickey Donald Goofy - Three Musketeers".
         b. TOKEN-COUNT (legacy, conservative): |intersection| >=
            min_token_overlap.
         c. JACCARD (legacy): |intersection| / |union| >= jaccard_threshold.
    3. Union-find on the candidate-edge graph: A↔B and B↔C => {A,B,C}.
    4. Drop components below min_group_size or above max_group_size (the cap
       suppresses runaway transitive blobs).

Defaults are tuned for real-world libraries:
    containment_threshold = 0.5
    min_shared_chars      = 6     # rejects "mr", "real", "arrow" false-positives
    jaccard_threshold     = 0.7
    min_token_overlap     = 3
    min_group_size        = 3
    max_group_size        = 100   # safety cap; warn-and-skip beyond

The phase6_design.md §14 config keys map as:
    tv_merge_threshold  -> jaccard_threshold
    tv_min_merge_size   -> min_group_size
The other knobs are tuning-only and not in config.example.json.

False positives (proposing a merge that shouldn't happen) are worse than false
negatives — the user can always merge manually in series_plan.csv if the
clusterer misses a subtle case.

Public API:
    MergeGroup                   dataclass
    find_merge_candidates(...)   main entry point
    DEFAULT_*                    threshold constants

CLI smoke test:
    py tv_merge_clusterer.py "01 MrBean" "02 Good Night MrBean" \\
        "03 Mind the Baby MrBean" --min-overlap 1
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Set, Tuple

from tv_parser import (
    leading_episode_number,
    normalize_folder_name,
    tokenize_for_clustering,
)


SCRIPT_VERSION = "1.0.0"

# Defaults. Two of these (jaccard_threshold, min_group_size) map to
# config.example.json keys tv_merge_threshold / tv_min_merge_size per
# phase6_design.md §14. The others are tuning knobs introduced after
# observing the algorithm's behavior on a real ~888-folder library.
DEFAULT_JACCARD_THRESHOLD = 0.7
DEFAULT_MIN_TOKEN_OVERLAP = 3
DEFAULT_MIN_GROUP_SIZE = 3
DEFAULT_CONTAINMENT_THRESHOLD = 0.5
DEFAULT_MIN_SHARED_CHARS = 6
DEFAULT_MAX_GROUP_SIZE = 100

log = logging.getLogger("tv_merge_clusterer")


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------

@dataclass
class MergeGroup:
    """One row in merge_candidates.csv (phase6_design.md §1).

    Fields map to the CSV columns:
        group_id              -> group_id
        suggested_series_name -> candidate_series_name
        members               -> member_folders (pipe-joined by inventory_tv.py)
        len(members)          -> member_folder_count
        len(shared_tokens)    -> shared_token_count

    `suggested_match` (the TMDB-side field) is filled in by inventory_tv.py
    after running a TMDB search using suggested_series_name; not the
    clusterer's responsibility.
    """
    group_id: int
    members: List[str] = field(default_factory=list)
    suggested_series_name: str = ""
    shared_tokens: List[str] = field(default_factory=list)

    @property
    def member_folder_count(self) -> int:
        return len(self.members)

    @property
    def shared_token_count(self) -> int:
        return len(self.shared_tokens)

    def to_csv_row(self, *, member_separator: str = " | ") -> dict:
        """Return a dict matching the merge_candidates.csv column names."""
        return {
            "group_id": self.group_id,
            "candidate_series_name": self.suggested_series_name,
            "member_folder_count": self.member_folder_count,
            "member_folders": member_separator.join(self.members),
            "shared_token_count": self.shared_token_count,
            "suggested_match": "",
            "action": "REVIEW",
            "notes": "",
        }


# ---------------------------------------------------------------------------
# Union-find (DSU) — small enough to inline, kept private
# ---------------------------------------------------------------------------

class _UnionFind:
    __slots__ = ("_parent", "_rank")

    def __init__(self, n: int) -> None:
        self._parent: List[int] = list(range(n))
        self._rank: List[int] = [0] * n

    def find(self, x: int) -> int:
        # Path compression.
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, x: int, y: int) -> bool:
        rx, ry = self.find(x), self.find(y)
        if rx == ry:
            return False
        # Union by rank.
        if self._rank[rx] < self._rank[ry]:
            rx, ry = ry, rx
        self._parent[ry] = rx
        if self._rank[rx] == self._rank[ry]:
            self._rank[rx] += 1
        return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_merge_candidates(
    folder_names: Iterable[str],
    *,
    jaccard_threshold: float = DEFAULT_JACCARD_THRESHOLD,
    min_token_overlap: int = DEFAULT_MIN_TOKEN_OVERLAP,
    min_group_size: int = DEFAULT_MIN_GROUP_SIZE,
    containment_threshold: float = DEFAULT_CONTAINMENT_THRESHOLD,
    min_shared_chars: int = DEFAULT_MIN_SHARED_CHARS,
    max_group_size: int = DEFAULT_MAX_GROUP_SIZE,
) -> List[MergeGroup]:
    """Cluster folder names that appear to belong to the same series.

    Args:
        folder_names: Top-level folder names (just basenames). Folders that
            normalize to an empty token list are filtered out.
        jaccard_threshold: Legacy criterion. Pairs with Jaccard(a, b) >= this
            value are linked. 0.0 disables; 1.0 requires identical token sets.
        min_token_overlap: Legacy criterion. Pairs with at least this many
            shared tokens are linked.
        min_group_size: Components smaller than this are dropped.
        containment_threshold: Primary criterion. A pair links if the smaller
            token set is at least this fraction contained in the larger
            (|intersection| / min(|a|, |b|) >= threshold) AND the shared
            tokens have enough char weight (see min_shared_chars). 0.5 is the
            tuned default; 1.0 requires the smaller set to be a strict subset.
        min_shared_chars: Containment links also require sum(len(t) for t in
            shared_tokens) >= this. Filters single-short-token false
            positives like "Mr Robot" + "Mr Magoo" sharing only "mr".
        max_group_size: Components above this are dropped with a log warning.
            Safety net against transitive blobs in dense libraries.

    Returns:
        List of MergeGroup, sorted by smallest-member-name (case-insensitive),
        with group_id assigned 1..N in that order.
    """
    if jaccard_threshold < 0 or jaccard_threshold > 1:
        raise ValueError(f"jaccard_threshold must be in [0, 1], got {jaccard_threshold!r}")
    if not (0 <= containment_threshold <= 1):
        raise ValueError(
            f"containment_threshold must be in [0, 1], got {containment_threshold!r}"
        )
    if min_token_overlap < 1:
        raise ValueError(f"min_token_overlap must be >= 1, got {min_token_overlap!r}")
    if min_group_size < 2:
        raise ValueError(f"min_group_size must be >= 2, got {min_group_size!r}")
    if max_group_size < min_group_size:
        raise ValueError(
            f"max_group_size ({max_group_size}) must be >= min_group_size ({min_group_size})"
        )
    if min_shared_chars < 0:
        raise ValueError(f"min_shared_chars must be >= 0, got {min_shared_chars!r}")

    # Stage 1: normalize and dedupe trivial empties.
    # Each item: (original, tokens_list, tokens_set, has_leading_episode_num).
    # The leading-num flag gates the SUBSET rule below — it's the structural
    # signal that the folder is a single-episode wrapper like "01 MrBean"
    # rather than a content-named folder like "Japanese 1" (a language course).
    items: List[Tuple[str, List[str], Set[str], bool]] = []
    for name in folder_names:
        if name is None:
            continue
        original = str(name).rstrip("\\/").strip()
        if not original:
            continue
        tokens_list = normalize_folder_name(original).split()
        if not tokens_list:
            log.debug("Skipping %r: no tokens after normalization", original)
            continue
        has_num = leading_episode_number(original) is not None
        items.append((original, tokens_list, set(tokens_list), has_num))

    n = len(items)
    if n < min_group_size:
        return []

    # Stage 2: build candidate edges. O(n^2) -- fine for n ~ 1000.
    #
    # Edge rules, in order. First match wins; falls through if none fire.
    #   (0) IDENTITY: tokens_i == tokens_j  -> link unconditionally.
    #       (Arrow / Arrow (2012) / Arrow (2012-) all normalize to {arrow}.)
    #   (1) SUBSET: smaller side's full token-set IS the intersection AND
    #       the smaller side's ORIGINAL folder has a leading episode-number
    #       prefix (the structural signal that it's a "01 MrBean"-style
    #       single-episode wrapper). Char weight check filters single-token
    #       false-positives like Lost vs Lost in Space.
    #   (2) STRONG: |intersection| >= 2 AND containment >= 0.5 AND total
    #       shared chars >= min_shared_chars + 2. The +2 floor distinguishes
    #       legitimate 2-token clusters ({donald, duck} = 10 chars) from
    #       weak short-token bridges ({how, play} = 7 chars).
    #   (3) LEGACY token-count: |intersection| >= min_token_overlap (kept
    #       for backward compat with the §14 design knobs).
    #   (4) LEGACY Jaccard: |intersection| / |union| >= jaccard_threshold.
    uf = _UnionFind(n)
    edges_added = 0
    strong_chars_floor = min_shared_chars + 2
    for i in range(n):
        toks_i = items[i][2]
        if not toks_i:
            continue
        has_num_i = items[i][3]
        for j in range(i + 1, n):
            toks_j = items[j][2]
            if not toks_j:
                continue
            inter = toks_i & toks_j
            if not inter:
                continue
            has_num_j = items[j][3]

            # (0) IDENTITY
            if toks_i == toks_j:
                if uf.union(i, j):
                    edges_added += 1
                continue

            # (1) SUBSET (gated on smaller-side leading-number prefix)
            shared_chars = sum(len(t) for t in inter)
            if inter == toks_i and has_num_i and shared_chars >= min_shared_chars:
                if uf.union(i, j):
                    edges_added += 1
                continue
            if inter == toks_j and has_num_j and shared_chars >= min_shared_chars:
                if uf.union(i, j):
                    edges_added += 1
                continue

            # (2) STRONG (2+ shared tokens, decent containment, decent chars)
            if len(inter) >= 2:
                smaller_size = min(len(toks_i), len(toks_j))
                containment = len(inter) / smaller_size if smaller_size else 0.0
                if (containment >= containment_threshold
                        and shared_chars >= strong_chars_floor):
                    if uf.union(i, j):
                        edges_added += 1
                    continue

            # (3) LEGACY token-count
            if len(inter) >= min_token_overlap:
                if uf.union(i, j):
                    edges_added += 1
                continue

            # (4) LEGACY Jaccard
            union = toks_i | toks_j
            jaccard = len(inter) / len(union) if union else 0.0
            if jaccard >= jaccard_threshold:
                if uf.union(i, j):
                    edges_added += 1

    log.debug("Built %d union-find edges across %d items", edges_added, n)

    # Stage 3: collect components.
    components: dict[int, List[int]] = {}
    for idx in range(n):
        root = uf.find(idx)
        components.setdefault(root, []).append(idx)

    # Stage 4: build MergeGroups for components meeting size bounds.
    raw_groups: List[Tuple[List[str], List[List[str]]]] = []
    for member_indices in components.values():
        size = len(member_indices)
        if size < min_group_size:
            continue
        if size > max_group_size:
            sample = sorted(items[i][0] for i in member_indices)[:5]
            log.warning(
                "Skipping runaway component of %d members (max_group_size=%d). "
                "Likely a transitive blob through a bridge folder. Sample: %s",
                size, max_group_size, sample,
            )
            continue
        members_originals = sorted(
            (items[i][0] for i in member_indices),
            key=lambda s: s.lower(),
        )
        members_tokens_lists = [items[i][1] for i in member_indices]
        raw_groups.append((members_originals, members_tokens_lists))

    # Stage 4.5: Drop transitive blobs.
    #
    # A "transitive blob" is a component where pair-wise edges chain through
    # different 2-token combinations without any single token spanning all
    # members. These almost always trace back to one outlier folder
    # (compilation video, hybrid title card, etc.) that bridges otherwise
    # unrelated shows.
    #
    # Rule: drop a component if its STRICT global intersection (tokens shared
    # by EVERY member) is empty AND fewer than 50% of members have a leading
    # episode-number prefix. The leading-num exemption preserves the §6
    # use case (Mr. Bean folders all share {mrbean}, but a relaxed-threshold
    # cluster could also span via leading-number-only signals).
    filtered_groups: List[Tuple[List[str], List[List[str]]]] = []
    for members, tokens_lists in raw_groups:
        token_sets = [set(t) for t in tokens_lists]
        strict_global = set.intersection(*token_sets) if token_sets else set()
        if not strict_global:
            leading_num_count = sum(
                1 for orig in members
                if leading_episode_number(orig) is not None
            )
            leading_num_pct = leading_num_count / len(members)
            if leading_num_pct < 0.5:
                log.warning(
                    "Skipping %d-member transitive blob (no globally-shared "
                    "token, leading-num pct=%.0f%%). Sample: %s",
                    len(members), leading_num_pct * 100, sorted(members)[:5],
                )
                continue
        filtered_groups.append((members, tokens_lists))

    # Sort groups deterministically: by first member (case-insensitive), then size desc.
    filtered_groups.sort(key=lambda g: (g[0][0].lower(), -len(g[0])))

    output: List[MergeGroup] = []
    for gid, (members, tokens_lists) in enumerate(filtered_groups, start=1):
        shared = _shared_tokens(tokens_lists)
        suggested = _derive_suggested_name(tokens_lists, shared)
        output.append(MergeGroup(
            group_id=gid,
            members=members,
            suggested_series_name=suggested,
            shared_tokens=shared,
        ))

    return output


# ---------------------------------------------------------------------------
# Suggested-name derivation
# ---------------------------------------------------------------------------

def _shared_tokens(token_lists: Sequence[List[str]]) -> List[str]:
    """Tokens common to ALL members, ordered by first appearance in longest member.

    Falls back to majority tokens (>= ceil(N/2)) when the strict intersection
    is empty (can happen with transitive clusters where A∩C is empty).
    """
    if not token_lists:
        return []
    intersection: Set[str] = set(token_lists[0])
    for toks in token_lists[1:]:
        intersection &= set(toks)
    if not intersection:
        # Majority fallback.
        counter: Counter[str] = Counter()
        for toks in token_lists:
            counter.update(set(toks))
        threshold = (len(token_lists) + 1) // 2  # ceil(N/2)
        intersection = {t for t, c in counter.items() if c >= threshold}
        if not intersection:
            return []

    longest = max(token_lists, key=len)
    seen: dict[str, int] = {}
    for i, tok in enumerate(longest):
        if tok in intersection and tok not in seen:
            seen[tok] = i
    # Tokens that appear in `intersection` but not in the longest member fall
    # to the end, alphabetically.
    leftover = sorted(intersection - set(seen))
    ordered_from_longest = [t for t, _ in sorted(seen.items(), key=lambda kv: kv[1])]
    return ordered_from_longest + leftover


def _derive_suggested_name(
    token_lists: Sequence[List[str]],
    shared_tokens: Sequence[str],
) -> str:
    """Title-case the shared tokens into a human-readable series-name suggestion.

    Falls back to the most frequent token across members when shared_tokens
    is empty (defensive — Stage 4 already filters empty groups).
    """
    if shared_tokens:
        return " ".join(t.title() for t in shared_tokens)

    if not token_lists:
        return ""
    counter: Counter[str] = Counter()
    for toks in token_lists:
        counter.update(set(toks))
    if not counter:
        return ""
    most_common, _ = counter.most_common(1)[0]
    return most_common.title()


def _jaccard(a: Set[str], b: Set[str]) -> float:
    """Jaccard index of two token sets. Public-ish helper for tests."""
    if not a or not b:
        return 0.0
    inter = a & b
    if not inter:
        return 0.0
    return len(inter) / len(a | b)


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

def _format_group(g: MergeGroup) -> str:
    lines = [
        f"Group #{g.group_id} -- {g.member_folder_count} members",
        f"  suggested_series_name: {g.suggested_series_name!r}",
        f"  shared_tokens ({g.shared_token_count}): {g.shared_tokens}",
        f"  members:",
    ]
    for m in g.members:
        lines.append(f"    - {m}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Cluster top-level folder names that look like the same TV series.",
    )
    ap.add_argument("folders", nargs="*",
                    help="Folder names. If none given, reads one per line from stdin.")
    ap.add_argument("--containment", type=float, default=DEFAULT_CONTAINMENT_THRESHOLD,
                    help=f"Containment threshold (default: {DEFAULT_CONTAINMENT_THRESHOLD})")
    ap.add_argument("--min-shared-chars", type=int, default=DEFAULT_MIN_SHARED_CHARS,
                    help=f"Minimum chars in shared tokens (default: {DEFAULT_MIN_SHARED_CHARS})")
    ap.add_argument("--jaccard", type=float, default=DEFAULT_JACCARD_THRESHOLD,
                    help=f"Jaccard threshold (default: {DEFAULT_JACCARD_THRESHOLD})")
    ap.add_argument("--min-overlap", type=int, default=DEFAULT_MIN_TOKEN_OVERLAP,
                    help=f"Minimum shared tokens (default: {DEFAULT_MIN_TOKEN_OVERLAP})")
    ap.add_argument("--min-size", type=int, default=DEFAULT_MIN_GROUP_SIZE,
                    help=f"Minimum cluster size to emit (default: {DEFAULT_MIN_GROUP_SIZE})")
    ap.add_argument("--max-size", type=int, default=DEFAULT_MAX_GROUP_SIZE,
                    help=f"Drop components above this size (default: {DEFAULT_MAX_GROUP_SIZE})")
    ap.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    if args.folders:
        folders: Iterable[str] = args.folders
    else:
        folders = [line.rstrip("\r\n") for line in sys.stdin if line.strip()]

    groups = find_merge_candidates(
        folders,
        containment_threshold=args.containment,
        min_shared_chars=args.min_shared_chars,
        jaccard_threshold=args.jaccard,
        min_token_overlap=args.min_overlap,
        min_group_size=args.min_size,
        max_group_size=args.max_size,
    )

    if not groups:
        print("(no merge candidates found)")
        return 0

    for g in groups:
        print(_format_group(g))
        print()
    print(f"Total: {len(groups)} group(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
