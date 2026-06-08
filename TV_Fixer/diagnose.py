#!/usr/bin/env python3
"""
Pipeline diagnostics: snapshot the state of subtitles.sqlite and
identify_state.sqlite without fighting PowerShell quoting.

Usage:
    python diagnose.py                            # overview
    python diagnose.py --show "3rd Rock"          # focus on a show (substring)
    python diagnose.py --status ok                # sample rows of a given status
    python diagnose.py --status error --limit 20  # show 20 error rows
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--subs-db", default="subtitles.sqlite")
    ap.add_argument("--state-db", default="identify_state.sqlite")
    ap.add_argument("--show", help="Focus on shows whose name contains this substring")
    ap.add_argument("--status",
                    choices=["ok", "low_confidence", "no_match",
                             "cross_show_match", "error", "prepared"],
                    help="Show sample rows of this status (with path + match details)")
    ap.add_argument("--limit", type=int, default=10)
    args = ap.parse_args()

    print(f"=== {args.subs_db} (FTS subtitle index) ===")
    if not Path(args.subs_db).exists():
        print("  FILE DOES NOT EXIST -- run index_subs.py first")
    else:
        s = sqlite3.connect(args.subs_db)
        total = s.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
        shows = s.execute("SELECT COUNT(DISTINCT show) FROM episodes").fetchone()[0]
        print(f"  total episodes indexed: {total:,}")
        print(f"  distinct shows:         {shows}")
        print(f"  sample shows (first {args.limit}):")
        for r in s.execute(
            "SELECT show, COUNT(*) FROM episodes GROUP BY show ORDER BY show LIMIT ?",
            (args.limit,),
        ):
            print(f"    {r[1]:>4}  {r[0]}")
        if args.show:
            print(f"\n  shows matching {args.show!r}:")
            for r in s.execute(
                "SELECT show, COUNT(*) FROM episodes WHERE show LIKE ? GROUP BY show",
                (f"%{args.show}%",),
            ):
                print(f"    {r[1]:>4}  {r[0]}")
        s.close()

    print(f"\n=== {args.state_db} (transcript/match state) ===")
    if not Path(args.state_db).exists():
        print("  FILE DOES NOT EXIST")
        return
    t = sqlite3.connect(args.state_db)
    print(f"  status breakdown:")
    for r in t.execute("SELECT status, COUNT(*) FROM results GROUP BY status ORDER BY 1"):
        print(f"    {r[0]:14s}  {r[1]:>6,}")
    print(f"\n  error reason breakdown (top 10):")
    for r in t.execute(
        "SELECT substr(detail,1,50) AS d, COUNT(*) FROM results "
        "WHERE status='error' GROUP BY d ORDER BY 2 DESC LIMIT 10"
    ):
        print(f"    {r[1]:>5}  {r[0]}")

    if args.show:
        print(f"\n  sample transcripts for {args.show!r}:")
        for r in t.execute(
            "SELECT path, status, substr(transcript,1,150) FROM results "
            "WHERE path LIKE ? ORDER BY status LIMIT 5",
            (f"%{args.show}%",),
        ):
            print(f"    [{r[1]}] {r[0]}")
            print(f"      {(r[2] or '(empty)')[:150]!r}")

    if args.status:
        print(f"\n  sample rows with status={args.status!r} (first {args.limit}):")
        for r in t.execute(
            "SELECT path, show, season, episode, score, margin, "
            "       substr(detail, 1, 80), substr(transcript, 1, 120) "
            "FROM results WHERE status = ? ORDER BY path LIMIT ?",
            (args.status, args.limit),
        ):
            path, show, season, episode, score, margin, detail, transcript = r
            print(f"    path:       {path}")
            if show:
                print(f"      matched:  S{(season or 0):02d}E{(episode or 0):02d} "
                      f"of {show!r}  score={(score or 0):.2f}  margin={(margin or 0):.2f}")
            if detail:
                print(f"      detail:   {detail}")
            if transcript:
                print(f"      excerpt:  {transcript!r}")
    t.close()


if __name__ == "__main__":
    main()
