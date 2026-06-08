#!/usr/bin/env python3
"""
Print a random sample of recent rename actions from apply_renames.log so
you can spot-check whether the matches are right.

Each printed entry shows:
  - the ORIGINAL filename (what we had to guess from)
  - the NEW filename (what we renamed it to)
  - the proposed episode title from TMDB
  - the path you can open to verify

Open each NEW path in VLC and confirm the title card / dialogue matches.
Record how many are wrong; from that you can decide whether to revert
everything (revert_renames.py), revert just one show, or accept and
patch as you go.

Usage:
    python sample_matches.py                       # 20 random samples
    python sample_matches.py --n 50                # more
    python sample_matches.py --show "Aaahh"        # one show only
    python sample_matches.py --score-min 50        # only highly-scored matches
"""
from __future__ import annotations

import argparse
import random
import sqlite3
import sys
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state-db", default="identify_state.sqlite")
    ap.add_argument("--n", type=int, default=20, help="Number of samples to print")
    ap.add_argument("--show", help="Filter to paths containing this substring")
    ap.add_argument("--score-min", type=float, default=0,
                    help="Only sample matches with score >= this")
    ap.add_argument("--status", default="ok",
                    help="Which status to sample from (default: ok)")
    args = ap.parse_args()

    if not Path(args.state_db).exists():
        sys.exit(f"State DB not found: {args.state_db}")

    c = sqlite3.connect(args.state_db)
    q = (
        "SELECT path, show, season, episode, score, margin "
        "FROM results WHERE status = ? AND score >= ?"
    )
    params = [args.status, args.score_min]
    if args.show:
        q += " AND path LIKE ?"
        params.append(f"%{args.show}%")
    rows = c.execute(q, params).fetchall()

    if not rows:
        print(f"No rows match the filter (status={args.status}, "
              f"score-min={args.score_min}, show={args.show!r})")
        return

    sample = random.sample(rows, min(args.n, len(rows)))
    print(f"Sampled {len(sample)} of {len(rows)} matching rows:\n")
    for i, r in enumerate(sample, 1):
        path, show, season, episode, score, margin = r
        p = Path(path)
        # The path in the DB is the ORIGINAL pre-rename path. The new path
        # was constructed by apply_renames using show/season/episode. But
        # apply_renames included the episode title -- which we don't have
        # in this DB. So we just show "the file likely lives here now".
        print(f"--- {i} ---")
        print(f"  NEW path (verify by opening this):")
        print(f"    look under: G:\\TV\\<{show}>\\Season {(season or 0):02d}\\")
        print(f"      filename starts with 'S{(season or 0):02d}E{(episode or 0):02d}' or contains it")
        print(f"  ORIGINAL filename: {p.name}")
        print(f"  Matched to:        S{(season or 0):02d}E{(episode or 0):02d} of {show!r}")
        print(f"  score={score:.2f}  margin={margin:.2f}")
        print()

    print("How to verify each one:")
    print("  1. Open the renamed file in VLC.")
    print("  2. Watch the first 30 seconds. Confirm title card / dialogue matches the")
    print("     proposed S/E and episode title.")
    print("  3. Tally how many are wrong. If error rate > 5%, consider revert_renames.py.")


if __name__ == "__main__":
    main()
