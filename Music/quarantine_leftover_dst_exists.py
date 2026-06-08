#!/usr/bin/env python3
"""
Quarantine the leftover NEEDS_RENAME files that hit dst_exists.

After running run_leftover_remediation.py --only NEEDS_RENAME, some files
will have failed with dst_exists (destination on disk already had a file
that the DB didn't know about). This script reads the run log produced by
that earlier run and moves those files to _QUARANTINE\\leftover_dst_collision\\.

USAGE
-----
  # Pass the specific run log produced by the stage 1 rename
  python quarantine_leftover_dst_exists.py --music-root E:\\Music ^
      --run-log "E:\\Music\\_QUARANTINE\\leftover_remediation_run_2026-05-15_120710.log"

  # Or scan all run logs in _QUARANTINE and quarantine any dst_exists rows
  python quarantine_leftover_dst_exists.py --music-root E:\\Music --scan-all
"""
import argparse, os, sys, time
from datetime import datetime
from pathlib import Path


def long_path(p):
    s = str(p)
    if os.name == "nt":
        s = s.replace("/", "\\")
        if len(s) > 240 and not s.startswith("\\\\?\\"):
            return "\\\\?\\" + s
    return s


def find_companion_lrc(src):
    src_p = Path(src)
    cand = src_p.with_suffix(".lrc")
    return str(cand) if cand.exists() else None


def attempt_move(src, dst):
    if not os.path.exists(long_path(src)):
        if os.path.exists(long_path(dst)):
            return "skipped_already_at_dst", "destination has the file"
        return "src_missing", "source not found"
    if os.path.exists(long_path(dst)):
        return "dst_exists", "destination already exists; refusing"
    try:
        os.makedirs(long_path(os.path.dirname(dst)), exist_ok=True)
        os.rename(long_path(src), long_path(dst))
        return "moved", ""
    except OSError:
        try:
            import shutil
            shutil.move(long_path(src), long_path(dst))
            return "moved", "shutil"
        except Exception as e:
            return "error", str(e)


def collect_dst_exists_from_log(log_path):
    paths = []
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        for ln in f:
            if not ln.startswith("dst_exists\t"):
                continue
            # format: dst_exists\t<decision>\t<src>\t<msg>
            parts = ln.rstrip("\n").split("\t")
            if len(parts) >= 3:
                # NEW format from run_leftover_remediation: dst_exists\tdecision\tsrc\tmsg
                # OLD format from run_rename: dst_exists\tsrc\tmsg
                if parts[1] in ("NEEDS_RENAME", "NEEDS_REVIEW"):
                    paths.append(parts[2])
                # Don't grab quarantine-side dst_exists; only rename-side
    return paths


def rel_under_root(abs_path, root):
    s = abs_path.replace("/", "\\")
    r = str(root).rstrip("\\")
    if s.lower().startswith(r.lower() + "\\"):
        return s[len(r) + 1:]
    return os.path.basename(s)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--music-root", required=True)
    ap.add_argument("--run-log", help="Specific run log to read")
    ap.add_argument("--scan-all", action="store_true",
                    help="Scan all leftover_remediation_run_*.log in _QUARANTINE")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    music_root = Path(args.music_root).resolve()
    quar_root = music_root / "_QUARANTINE"

    # Find log(s)
    logs = []
    if args.run_log:
        logs = [Path(args.run_log)]
    elif args.scan_all:
        logs = sorted(quar_root.glob("leftover_remediation_run_*.log"))
    else:
        sys.exit("Specify --run-log or --scan-all")

    # Collect unique dst_exists source paths from logs
    paths = []
    seen = set()
    for lp in logs:
        if not lp.exists():
            print(f"WARNING: {lp} not found, skipping")
            continue
        these = collect_dst_exists_from_log(lp)
        for p in these:
            if p not in seen:
                seen.add(p)
                paths.append(p)
        print(f"  {lp.name}: collected {len(these)} dst_exists paths")

    print(f"\nTotal unique dst_exists paths to quarantine: {len(paths):,}")
    if args.dry_run:
        print("(dry-run — first 10:)")
        for p in paths[:10]:
            print(f"  {p}")
        return

    if not paths:
        print("Nothing to do.")
        return

    rev_log = quar_root / "reversal_log.tsv"
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    run_log = quar_root / f"leftover_dst_collision_run_{ts}.log"

    counts = {"moved": 0, "src_missing": 0, "dst_exists": 0, "error": 0,
              "lrc_moved": 0, "lrc_failed": 0}
    started = time.time()
    with open(rev_log, "a", encoding="utf-8") as rf, \
         open(run_log, "w", encoding="utf-8") as lf:
        for src in paths:
            rel = rel_under_root(src, music_root)
            dst = str(music_root / "_QUARANTINE" / "leftover_rename_dst_collision" / rel)
            status, msg = attempt_move(src, dst)
            counts[status] = counts.get(status, 0) + 1
            if status == "moved":
                rf.write(f"{dst}\t{src}\tleftover_rename_dst_collision\n")
                rf.flush()
                lrc_src = find_companion_lrc(src)
                if lrc_src:
                    lrc_dst = str(Path(dst).with_suffix(".lrc"))
                    ls, _ = attempt_move(lrc_src, lrc_dst)
                    if ls == "moved":
                        counts["lrc_moved"] += 1
                        rf.write(f"{lrc_dst}\t{lrc_src}\tleftover_rename_dst_collision_lrc\n")
                        rf.flush()
                    else:
                        counts["lrc_failed"] += 1
            else:
                lf.write(f"{status}\t{src}\t{msg}\n")

    print("\n==== summary ====")
    for k, v in counts.items():
        print(f"  {k:<22} {v:>5,}")
    print(f"  elapsed:               {(time.time()-started):.1f}s")
    print(f"  run log:               {run_log}")


if __name__ == "__main__":
    main()
