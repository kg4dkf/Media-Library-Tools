#!/usr/bin/env python3
"""
Act on dedupe_report.csv: delete (or recycle-bin) duplicate files whose path
matches a filter, but only when the corresponding KEEP file lives outside the
filter and actually exists on disk.

DEFAULTS ARE SAFE:
  - dry-run by default; prints what would be removed.
  - When --commit is passed, files go to the Windows Recycle Bin via send2trash,
    so a mistake is recoverable.
  - Permanent deletion requires BOTH --commit AND --permanent.

Typical usage to clean out the 'Look at Later' duplicates:

  pip install send2trash
  python apply_dedupe.py dedupe_report.csv --folder "Look at Later"
                                                         # dry run

  python apply_dedupe.py dedupe_report.csv --folder "Look at Later" --commit
                                                         # sends to Recycle Bin

  # The script writes a log of removed files to apply_dedupe.log so a re-run
  # skips files it has already handled.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List


def _safe(s) -> str:
    """Round-trip strings through UTF-8 with errors='replace' so unpaired
    Windows surrogates in filenames don't crash log writers."""
    return str(s).encode("utf-8", errors="replace").decode("utf-8")


def fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def load_clusters(csv_path: Path) -> Dict[str, List[dict]]:
    clusters: Dict[str, List[dict]] = defaultdict(list)
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            clusters[row["cluster"]].append(row)
    return clusters


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("report", type=Path, help="dedupe_report.csv produced by dedupe.py")
    ap.add_argument("--folder", required=True,
                    help="Substring (case-insensitive) the dup path must contain "
                         "for deletion to be considered. Example: \"Look at Later\"")
    ap.add_argument("--commit", action="store_true",
                    help="Actually act. Without this flag, only print what WOULD happen.")
    ap.add_argument("--permanent", action="store_true",
                    help="Permanently delete instead of Recycle Bin. Requires --commit.")
    ap.add_argument("--log", type=Path, default=Path("apply_dedupe.log"),
                    help="Append-only log of removed files (so re-runs skip them)")
    ap.add_argument("--limit", type=int, default=0,
                    help="Process at most N files (0 = unlimited)")
    args = ap.parse_args()

    if not args.report.is_file():
        sys.exit(f"Report not found: {args.report}")

    needle = args.folder.lower()
    clusters = load_clusters(args.report)
    print(f"Loaded {len(clusters)} clusters from {args.report}")

    # Load existing log so we don't double-process.
    already_done: set = set()
    if args.log.exists():
        with args.log.open("r", encoding="utf-8") as f:
            for line in f:
                p = line.strip().split("\t", 1)[-1]
                if p:
                    already_done.add(p)
        if already_done:
            print(f"Skipping {len(already_done)} files already in log: {args.log}")

    # Build the work list. For each cluster:
    #   - require at least one KEEP file whose path does NOT contain the needle
    #     and that exists on disk (the verified canonical copy)
    #   - delete every dup whose path contains the needle and exists
    plan = []
    skipped_no_keep_outside = 0
    skipped_keep_missing = 0
    skipped_dup_missing = 0
    for cid, rows in clusters.items():
        keeps_outside = [
            r for r in rows
            if r["keep"] == "KEEP" and needle not in r["path"].lower()
        ]
        if not keeps_outside:
            skipped_no_keep_outside += 1
            continue
        verified_keep = next((r for r in keeps_outside if Path(r["path"]).exists()), None)
        if verified_keep is None:
            skipped_keep_missing += 1
            continue

        for r in rows:
            if r["keep"] == "KEEP":
                continue
            if needle not in r["path"].lower():
                continue
            p = Path(r["path"])
            if r["path"] in already_done:
                continue
            if not p.exists():
                skipped_dup_missing += 1
                continue
            plan.append({
                "dup_path": r["path"],
                "keep_path": verified_keep["path"],
                "size": int(r.get("size") or 0) or (p.stat().st_size if p.exists() else 0),
                "cluster": cid,
            })

    if args.limit and len(plan) > args.limit:
        plan = plan[: args.limit]

    total_bytes = sum(item["size"] for item in plan)
    print(f"\nFilter: dup path contains {args.folder!r}")
    print(f"Files queued for removal:    {len(plan)}")
    print(f"Total space to be freed:     {fmt_bytes(total_bytes)}")
    print(f"Skipped (no KEEP outside):   {skipped_no_keep_outside}")
    print(f"Skipped (KEEP file missing): {skipped_keep_missing}")
    print(f"Skipped (dup already gone):  {skipped_dup_missing}")

    if not plan:
        print("\nNothing to do.")
        return

    # Show a sample.
    print("\nFirst 5 actions:")
    for item in plan[:5]:
        print(f"  - {item['dup_path']}")
        print(f"    (canonical: {item['keep_path']})")
    if len(plan) > 5:
        print(f"  ... and {len(plan) - 5} more")

    if not args.commit:
        print("\nDRY RUN — nothing was changed. Re-run with --commit to act.")
        return

    if args.permanent:
        print("\n** PERMANENT DELETE requested. Files will not be recoverable. **")
        action = "permanent_delete"
        try:
            remover = lambda p: os.unlink(p)
        except Exception as e:
            sys.exit(f"Error setting up deleter: {e}")
    else:
        try:
            from send2trash import send2trash
        except Exception:
            sys.exit("pip install send2trash  (or pass --permanent at your own risk)")
        action = "recycle_bin"
        remover = lambda p: send2trash(p)

    print(f"\nActing on {len(plan)} files ({action})...")
    removed = 0
    failed = 0
    with args.log.open("a", encoding="utf-8") as logf:
        for item in plan:
            try:
                remover(item["dup_path"])
            except Exception as e:
                sys.stderr.write(f"FAIL: {item['dup_path']}: {e}\n")
                failed += 1
                continue
            logf.write(f"{action}\t{_safe(item['dup_path'])}\n")
            logf.flush()
            removed += 1
            if removed % 200 == 0:
                print(f"  ... {removed}/{len(plan)}")

    print(f"\nDone. Removed {removed} files. Failed: {failed}. "
          f"Freed approx {fmt_bytes(sum(i['size'] for i in plan[:removed]))}.")
    print(f"Log: {args.log}")


if __name__ == "__main__":
    main()
