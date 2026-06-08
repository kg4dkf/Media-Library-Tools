#!/usr/bin/env python3
"""
ReplayGain analyzer for E:\\Music using rsgain.

Wraps the rsgain CLI tool, which is the modern de facto standard for
ReplayGain 2.0 analysis (R128 / loudness-normalized) and handles every
format we have: MP3, FLAC, M4A/MP4, OGG/Opus, WAV, WMA.

ReplayGain tags allow players that support them (foobar2000, MusicBee,
VLC, Kodi, mpv, Plex w/ extension, etc.) to normalize playback volume
across all tracks/albums — so a quiet ballad and a loud rock track
play at perceptually similar volumes without manual adjustment.

INSTALL rsgain
--------------
Windows: download from https://github.com/complexlogic/rsgain/releases
Extract rsgain.exe somewhere on PATH (or H:\\Music Cleanup\\)

USAGE
-----
   python add_replaygain.py --music-root E:\\Music
   python add_replaygain.py --music-root E:\\Music --check     # just verify rsgain is installed
   python add_replaygain.py --music-root E:\\Music --dry-run   # show what would be done

rsgain easy mode walks the tree, computes per-track AND per-album gain,
writes tags inline. Resumable — re-running skips files that already have
ReplayGain tags.
"""
import argparse, os, shutil, subprocess, sys, time
from pathlib import Path


def find_rsgain():
    p = shutil.which("rsgain")
    if p:
        return p
    # Check local dir
    for cand in ("rsgain.exe", "rsgain"):
        if os.path.exists(cand):
            return os.path.abspath(cand)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--music-root", required=True)
    ap.add_argument("--check", action="store_true", help="Verify rsgain is installed")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-quarantine", action="store_true", default=True,
                    help="Skip _QUARANTINE folder (default on)")
    args = ap.parse_args()

    rsgain = find_rsgain()
    if not rsgain:
        print("ERROR: rsgain not found on PATH.")
        print()
        print("Install:")
        print("  1. Download rsgain from https://github.com/complexlogic/rsgain/releases")
        print("  2. Extract rsgain.exe to a directory on PATH (e.g., H:\\Music Cleanup\\)")
        print("  3. Re-run this script")
        sys.exit(1)
    print(f"Found rsgain: {rsgain}")
    try:
        r = subprocess.run([rsgain, "--version"], capture_output=True, text=True, timeout=5)
        print(f"  version: {(r.stdout or r.stderr).strip().split(chr(10))[0]}")
    except Exception as e:
        print(f"  could not get version: {e}")

    if args.check:
        return

    music_root = Path(args.music_root).resolve()
    if not music_root.is_dir():
        sys.exit(f"ERROR: not a directory: {music_root}")

    # rsgain easy mode: walks recursively, computes per-track + per-album gain.
    # We can't easily exclude _QUARANTINE via rsgain itself, so:
    #   - If user wants to skip _QUARANTINE, we run on each top-level subfolder
    #     except _QUARANTINE.
    targets = []
    for fn in os.listdir(music_root):
        full = music_root / fn
        if not full.is_dir():
            continue
        if args.skip_quarantine and fn == "_QUARANTINE":
            print(f"  skipping {fn}")
            continue
        targets.append(str(full))
    print(f"Targets ({len(targets)}):")
    for t in targets:
        print(f"  {t}")

    if args.dry_run:
        print("\n(dry-run; would run:)")
        for t in targets:
            print(f"  rsgain easy --multithread=4 \"{t}\"")
        return

    print()
    started = time.time()
    for t in targets:
        print(f"\n=== rsgain easy {t} ===")
        cmd = [rsgain, "easy", "--multithread=4", t]
        try:
            subprocess.run(cmd, check=False)
        except KeyboardInterrupt:
            print("\nInterrupted")
            break

    elapsed = time.time() - started
    print(f"\nTotal elapsed: {elapsed/60:.1f} min")


if __name__ == "__main__":
    main()
