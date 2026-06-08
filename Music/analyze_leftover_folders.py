#!/usr/bin/env python3
"""
Identify folders under E:\\Music that DIDN'T end up in the new tree shape
(everything not under Music\\<Letter>\\, Music\\Compilations\\, etc.) and
characterize their contents for manual review.

Produces leftover_folders.csv with columns:
   rel_path                    : path under music root
   depth                       : nesting depth (1 = top-level under E:\\Music)
   audio_count, audio_size_gb  : audio files directly in this folder
   subtree_audio, subtree_size_gb : same, but for entire subtree
   distinct_artists, top_artist
   companion_count             : .jpg/.nfo/.cue/etc.
   most_likely_category        : SOUNDTRACK | COMPILATION | ARTIST? | EMPTY | UNCLASSIFIED
   sample_filenames            : 3 sample files for eyeballing
"""
import argparse, csv, os, re, time
from collections import Counter, defaultdict
from pathlib import Path

try:
    from mutagen import File as MutagenFile
except ImportError:
    raise SystemExit("ERROR: pip install mutagen")

AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".m4b", ".mp4", ".ogg", ".opus",
              ".wma", ".wav", ".aac", ".aiff", ".aif", ".alac", ".ape"}
COMPANION_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".nfo",
                  ".cue", ".log", ".sfv", ".m3u", ".m3u8", ".pls",
                  ".txt", ".pdf", ".lrc"}

NEW_TREE_TOPS = (
    tuple(chr(c) for c in range(ord("A"), ord("Z") + 1))
    + ("0-9", "_Other", "_Unknown", "_Review", "_QUARANTINE",
       "Compilations", "Soundtracks", "Radio", "Sound Effects",
       "Video Game Soundtracks", "Audiobooks", "Comedy")
)


def long_path(p):
    s = str(p)
    if os.name == "nt" and len(s) > 240 and not s.startswith("\\\\?\\"):
        return "\\\\?\\" + s.replace("/", "\\")
    return s


def is_new_tree(rel_path):
    if not rel_path or rel_path == ".":
        return False
    top = rel_path.replace("/", "\\").split("\\")[0]
    return top in NEW_TREE_TOPS


def guess_category(folder_name, audio_count, distinct_artists, dom_pct, companion_count):
    nl = folder_name.lower()
    if audio_count == 0:
        return "NO_AUDIO_DIRECT"
    if "compilation" in nl or "various" in nl or " va " in nl or nl.startswith("va "):
        return "COMPILATION?"
    if "soundtrack" in nl or "ost" in nl or "score" in nl or "motion picture" in nl:
        return "SOUNDTRACK?"
    if "discography" in nl or "complete" in nl or "discogr" in nl:
        return "ARTIST_DISCOGRAPHY?"
    if any(g in nl for g in ("playlist", "mix-", "best of", "top 100", "top 50")):
        return "PLAYLIST?"
    if distinct_artists <= 2 and dom_pct >= 0.7 and audio_count >= 3:
        return "ARTIST?"
    if distinct_artists >= 5 and dom_pct < 0.3 and audio_count >= 5:
        return "COMPILATION?"
    return "UNCLASSIFIED"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--music-root", required=True)
    ap.add_argument("--out", default="leftover_folders.csv")
    ap.add_argument("--min-audio", type=int, default=0,
                    help="Only include folders with at least this many audio files in subtree (default 0)")
    args = ap.parse_args()

    root = Path(args.music_root).resolve()
    print(f"Walking {root} for leftover folders...")

    # First pass: collect every folder and per-folder direct stats
    folder_stats = {}  # rel -> dict
    t0 = time.time()
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        n += 1
        if "_QUARANTINE" in dirpath:
            continue
        rel = os.path.relpath(dirpath, root)
        # Skip if this dir is under a new-tree top
        if is_new_tree(rel):
            continue
        # Per-folder direct file analysis
        audio_files = []
        comp_count = 0
        size = 0
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            full = os.path.join(dirpath, fn)
            try:
                fsz = os.path.getsize(long_path(full))
                size += fsz
            except Exception:
                fsz = 0
            if ext in AUDIO_EXTS:
                audio_files.append((full, fn))
            elif ext in COMPANION_EXTS:
                comp_count += 1
        folder_stats[rel] = {
            "abs_path": dirpath,
            "audio_files": audio_files,
            "audio_count": len(audio_files),
            "comp_count": comp_count,
            "size": size,
            "subfolder_count": sum(1 for d in dirnames if d != "_QUARANTINE"),
        }
        if n % 500 == 0:
            print(f"  scanned {n:,} dirs in {time.time()-t0:.1f}s...")
    print(f"  found {len(folder_stats):,} leftover folders ({time.time()-t0:.1f}s)")

    # Second pass: read audio tags from a sample (up to 5 files per folder)
    print("\nReading sample tags...")
    t0 = time.time()
    rows = []
    for rel, st in folder_stats.items():
        depth = 0 if rel == "." else (rel.replace("/", "\\").count("\\") + 1)
        # Read up to 5 audio file tags for artist analysis
        artist_counts = Counter()
        sample_titles = []
        files_to_sample = st["audio_files"][:5]
        for full, fn in files_to_sample:
            try:
                f = MutagenFile(long_path(full), easy=True)
                if f and f.tags:
                    a = (f.tags.get("artist", [""]) or [""])[0]
                    t = (f.tags.get("title", [""]) or [""])[0]
                    if a:
                        artist_counts[a] += 1
                    if t:
                        sample_titles.append(t)
            except Exception:
                pass
        distinct_a = len(artist_counts)
        if artist_counts:
            top_artist, top_n = artist_counts.most_common(1)[0]
            dom_pct = top_n / sum(artist_counts.values())
        else:
            top_artist, dom_pct = "", 0
        # Subtree size — sum all descendants
        subtree_audio = 0
        subtree_size = 0
        for other_rel, other_st in folder_stats.items():
            if other_rel == rel or other_rel.startswith(rel + os.sep) or other_rel.startswith(rel + "\\"):
                subtree_audio += other_st["audio_count"]
                subtree_size += other_st["size"]
        # If filtering by min-audio, skip
        if subtree_audio < args.min_audio:
            continue
        folder_name = rel.split("\\")[-1] if "\\" in rel else (rel.split("/")[-1] if "/" in rel else rel)
        category = guess_category(folder_name, st["audio_count"], distinct_a, dom_pct, st["comp_count"])
        sample_files = "; ".join(fn for _, fn in st["audio_files"][:3])
        rows.append({
            "rel_path": rel,
            "depth": depth,
            "audio_count": st["audio_count"],
            "audio_size_mb": round(st["size"] / (1024**2), 1),
            "subtree_audio": subtree_audio,
            "subtree_size_gb": round(subtree_size / (1024**3), 2),
            "subfolders": st["subfolder_count"],
            "companion_files": st["comp_count"],
            "distinct_artists_sampled": distinct_a,
            "top_artist_sampled": top_artist,
            "dominant_pct": round(dom_pct * 100, 1),
            "most_likely_category": category,
            "sample_filenames": sample_files,
            "user_action": "",  # blank for user to fill in
        })
    print(f"  tag sampling done in {time.time()-t0:.1f}s")

    # Sort by depth then by audio count
    rows.sort(key=lambda r: (r["depth"], -r["subtree_audio"], r["rel_path"]))

    # Summary
    by_cat = Counter(r["most_likely_category"] for r in rows)
    print(f"\nLeftover folders: {len(rows):,}")
    for cat, n in by_cat.most_common():
        print(f"  {cat:<22} {n:>5}")

    out_path = Path(args.out).resolve()
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"\nWrote: {out_path}")
    print(f"\nUser-action column suggestions:")
    print(f"  RETRY_RENAME       - try to plan a rename for this folder")
    print(f"  MOVE_TO_SOUNDTRACKS / MOVE_TO_COMPILATIONS - manual reclassify")
    print(f"  DELETE             - empty/junk, safe to delete")
    print(f"  IGNORE             - leave as-is")
    print(f"  REVIEW             - need to eyeball more")


if __name__ == "__main__":
    main()
