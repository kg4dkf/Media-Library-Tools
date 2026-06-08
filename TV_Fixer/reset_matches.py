#!/usr/bin/env python3
"""
Reset all 'matched' rows in identify_state.sqlite back to 'prepared' status
so the next identify.py run can re-do them with updated logic / thresholds.

Keeps transcripts intact (so we don't have to re-Whisper anything).
Keeps 'error' rows intact too (those won't be reattempted by default).

Usage:
    python reset_matches.py                          # dry run
    python reset_matches.py --commit                 # actually reset
    python reset_matches.py --commit --include-errors  # ALSO retry errors
"""
import argparse
import sqlite3
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state-db", default="identify_state.sqlite")
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--include-errors", action="store_true",
                    help="Also reset 'error' rows -- useful after fixing the underlying bug")
    args = ap.parse_args()

    if not Path(args.state_db).exists():
        print(f"State DB not found: {args.state_db}")
        return

    target_statuses = ["ok", "low_confidence", "no_match", "cross_show_match"]
    if args.include_errors:
        target_statuses.append("error")

    placeholders = ",".join("?" for _ in target_statuses)
    c = sqlite3.connect(args.state_db)

    # Count what we'd touch
    print("Rows that would be reset back to 'prepared':")
    for s in target_statuses:
        n = c.execute("SELECT COUNT(*) FROM results WHERE status = ?", (s,)).fetchone()[0]
        print(f"  {s:18s}  {n:,}")
    total = c.execute(
        f"SELECT COUNT(*) FROM results WHERE status IN ({placeholders})",
        target_statuses,
    ).fetchone()[0]
    print(f"  TOTAL              {total:,}")

    if not args.commit:
        print("\nDRY RUN. Re-run with --commit to actually reset.")
        return

    # Reset
    n = c.execute(
        f"UPDATE results "
        f"SET status='prepared', show=NULL, season=NULL, episode=NULL, "
        f"    score=NULL, margin=NULL, detail=NULL "
        f"WHERE status IN ({placeholders}) AND transcript IS NOT NULL",
        target_statuses,
    ).rowcount

    # For rows with NULL transcript (error rows, mostly), wipe entirely so they
    # get reprocessed from scratch on the next run.
    n2 = 0
    if args.include_errors:
        n2 = c.execute(
            f"DELETE FROM results WHERE status IN ({placeholders}) AND transcript IS NULL",
            target_statuses,
        ).rowcount

    c.commit()
    print(f"\nDone. Reset {n} rows to 'prepared'. Deleted {n2} transcript-less rows.")
    print(f"Now re-run: python identify.py \"G:\\TV\" --subs-db subtitles.sqlite "
          f"--state-db {args.state_db} --report identify_report.csv")


if __name__ == "__main__":
    main()
