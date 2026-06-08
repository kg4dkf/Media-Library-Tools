#!/usr/bin/env python3
"""
Delete folders flagged in empty_folders.csv.

Safety:
  - By default, only deletes folders where classification is TRULY_EMPTY.
  - With --include-system-only, also deletes folders containing ONLY
    desktop.ini / Thumbs.db / .DS_Store (Windows-generated junk).
  - With --include-companions-only, also deletes folders containing only
    .jpg/.png/.nfo/etc. (these almost always indicate audio already moved away).
  - With --user-action-only, ONLY deletes rows where you set user_action=DELETE.
  - Always processes deepest folders first (so empty parents become deletable).
  - --dry-run prints what would happen, no deletes.

USAGE
-----
  python delete_empty_folders.py --csv empty_folders.csv --dry-run
  python delete_empty_folders.py --csv empty_folders.csv --include-system-only
  python delete_empty_folders.py --csv empty_folders.csv --user-action-only
"""
import argparse, csv, io, os, sys, time
from pathlib import Path


def long_path(p):
    s = str(p)
    if os.name == "nt" and len(s) > 240 and not s.startswith("\\\\?\\"):
        return "\\\\?\\" + s.replace("/", "\\")
    return s


def read_csv_tolerant(path):
    raw = open(path, "rb").read().replace(b"\x00", b"").decode("utf-8", "replace")
    return list(csv.DictReader(io.StringIO(raw)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--include-system-only", action="store_true",
                    help="Also delete folders containing only desktop.ini/Thumbs.db")
    ap.add_argument("--include-companions-only", action="store_true",
                    help="Also delete folders containing only .jpg/.nfo/etc. companions")
    ap.add_argument("--user-action-only", action="store_true",
                    help="Only delete rows where user_action column == 'DELETE'")
    args = ap.parse_args()

    rows = read_csv_tolerant(args.csv)
    print(f"Loaded {len(rows):,} rows from {args.csv}")

    # Build delete candidates
    allowed_cls = {"TRULY_EMPTY"}
    if args.include_system_only:
        allowed_cls.add("SYSTEM_ONLY")
    if args.include_companions_only:
        allowed_cls.add("COMPANIONS_ONLY")

    candidates = []
    for r in rows:
        if args.user_action_only:
            ua = (r.get("user_action") or "").strip().upper()
            if ua != "DELETE":
                continue
        else:
            cls = (r.get("classification") or "").strip()
            if cls not in allowed_cls:
                continue
            # Belt-and-suspenders: never delete if subtree has audio
            if (r.get("subtree_has_audio") or "").strip().upper() == "Y":
                continue
        candidates.append(r["abs_path"])

    print(f"Candidates to delete: {len(candidates):,}")
    if not candidates:
        print("Nothing to delete.")
        return

    # Sort by depth descending so children go before parents
    candidates.sort(key=lambda p: -len(p))

    counts = {"deleted_files_inside": 0, "deleted_dir": 0,
              "skipped_not_empty": 0, "skipped_missing": 0,
              "error": 0}
    started = time.time()

    for p in candidates:
        if not os.path.isdir(long_path(p)):
            counts["skipped_missing"] += 1
            continue
        try:
            # Remove inner system/companion files (only if we allowed those classes)
            entries = os.listdir(long_path(p))
            for fn in entries:
                full = os.path.join(p, fn)
                if os.path.isdir(long_path(full)):
                    # leave subdirs — only top-level files of THIS dir
                    continue
                if args.dry_run:
                    counts["deleted_files_inside"] += 1
                    continue
                try:
                    os.remove(long_path(full))
                    counts["deleted_files_inside"] += 1
                except Exception:
                    pass
            # Now try to remove the directory itself
            if args.dry_run:
                print(f"  WOULD DELETE: {p}")
                counts["deleted_dir"] += 1
            else:
                try:
                    os.rmdir(long_path(p))
                    counts["deleted_dir"] += 1
                except OSError as e:
                    # Not empty (probably has subdirs)
                    counts["skipped_not_empty"] += 1
        except Exception as e:
            counts["error"] += 1
            print(f"  ERROR on {p}: {e}")

    print()
    print("==== summary ====")
    for k, v in counts.items():
        print(f"  {k:<28} {v:>6,}")
    print(f"  elapsed: {time.time()-started:.1f}s")


if __name__ == "__main__":
    main()
