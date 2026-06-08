#!/usr/bin/env python3
"""
Trace a single no_match row through the matching pipeline so we can see
exactly where it fails: empty query, FTS5 syntax error, no hits, etc.

Usage:
    python test_search.py                       # picks 3 random no_match rows
    python test_search.py --path "G:\\TV\\..."  # trace a specific file
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, ".")
from identify import to_fts_query, search
import common


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--subs-db", default="subtitles.sqlite")
    ap.add_argument("--state-db", default="identify_state.sqlite")
    ap.add_argument("--media-root", default="G:\\TV")
    ap.add_argument("--path", help="Specific file path to trace (else picks 3 sample no_match rows)")
    args = ap.parse_args()

    subs = sqlite3.connect(args.subs_db)
    state = sqlite3.connect(args.state_db)

    if args.path:
        rows = [state.execute(
            "SELECT path, transcript FROM results WHERE path = ?", (args.path,)
        ).fetchone()]
        if rows == [None]:
            sys.exit(f"No state row for {args.path!r}")
    else:
        rows = state.execute(
            "SELECT path, transcript FROM results WHERE status='no_match' "
            "AND transcript IS NOT NULL AND length(transcript) > 50 LIMIT 3"
        ).fetchall()
        if not rows:
            sys.exit("No no_match rows with transcripts found")

    media_root = Path(args.media_root)

    for path, transcript in rows:
        print("=" * 72)
        print(f"PATH:        {path}")
        print(f"TRANSCRIPT:  {(transcript or '')[:200]!r}")

        # Reconstruct what identify.py would have done.
        try:
            rel = Path(path).relative_to(media_root)
            src_show = rel.parts[0] if len(rel.parts) >= 2 else None
        except ValueError:
            src_show = None
        src_norm = common.normalize_show_name(src_show) if src_show else None
        print(f"SRC SHOW:    {src_show!r}")
        print(f"SRC NORM:    {src_norm!r}")

        # Build FTS query
        query = to_fts_query(transcript or "")
        print(f"QUERY len:   {len(query)} chars, {query.count(' OR ') + 1 if query else 0} terms")
        print(f"QUERY[:200]: {query[:200]!r}")

        if not query:
            print(">>> EMPTY QUERY -- transcript had no usable words. This causes no_match.")
            print()
            continue

        # Constrained search
        try:
            in_hits = search(subs, query, show_norm=src_norm, top_k=3)
            print(f"CONSTRAINED hits (show='{src_norm}'): {len(in_hits)}")
            for h in in_hits:
                print(f"  S{h.season:02d}E{h.episode:02d}  score={h.score:.2f}  show={h.show!r}")
        except Exception as e:
            print(f">>> CONSTRAINED search ERRORED: {type(e).__name__}: {e}")

        # Unconstrained (cross-show) search
        try:
            cross_hits = search(subs, query, show_norm=None, top_k=3)
            print(f"UNCONSTRAINED (cross-show) hits: {len(cross_hits)}")
            for h in cross_hits:
                print(f"  S{h.season:02d}E{h.episode:02d}  score={h.score:.2f}  show={h.show!r}")
        except Exception as e:
            print(f">>> UNCONSTRAINED search ERRORED: {type(e).__name__}: {e}")
        print()

    # Also do one direct sanity check: does FTS5 work at all?
    print("=" * 72)
    print("FTS5 SMOKE TEST: 'space OR earth OR planet' across whole index")
    smoke_hits = subs.execute(
        "SELECT show_norm, season, episode, bm25(episodes_fts) "
        "FROM episodes_fts WHERE episodes_fts MATCH ? "
        "ORDER BY bm25(episodes_fts) ASC LIMIT 5",
        ("space OR earth OR planet",)
    ).fetchall()
    print(f"  hits: {len(smoke_hits)}")
    for h in smoke_hits:
        print(f"    {h[0]:50s}  S{h[1]:02d}E{h[2]:02d}  bm25={h[3]:.2f}")


if __name__ == "__main__":
    main()
