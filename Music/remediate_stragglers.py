#!/usr/bin/env python3
"""
Phase 5b: Edge-case straggler remediation.

Reads E:\\Music\\ via mutagen, identifies files NOT in the new-tree shape
(everything outside Music\\<Letter>\\, Compilations\\, Soundtracks\\, etc.),
applies extended planning rules:

  - Extended genre/era list (adds Jewish, Opera, Childrens, Hindi, Yiddish,
    plus more catch-all DISSOLVE for any folder that looks genre-y)
  - Aggressive artist truncation for very-long compilation credits
    (anything > 60 chars at the first comma)
  - Loose root files: parse "<Artist> - <Title>.ext" filename pattern when
    tags are missing
  - Better fallbacks (use folder name as album)
  - Recognizes `_Other\\` and CJK character roots as valid new-tree
    destinations (skips them)

Outputs a remediation rename CSV. Then run with --execute to actually move.

USAGE
-----
  pip install mutagen
  python remediate_stragglers.py --music-root E:\\Music              # dry-run, builds CSV only
  python remediate_stragglers.py --music-root E:\\Music --execute    # also moves
"""
import argparse, csv, os, re, sys, time
from datetime import datetime
from pathlib import Path

try:
    from mutagen import File as MutagenFile
except ImportError:
    sys.exit("ERROR: pip install mutagen")

# ---- constants -----------------------------------------------------------

AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".m4b", ".m4p", ".mp4", ".aac",
              ".ogg", ".oga", ".opus", ".wma", ".wav", ".aiff", ".aif",
              ".ape", ".wv", ".alac"}

# Anything starting with one of these is considered "already in new tree" — skip
NEW_TREE_PREFIXES = (
    tuple(chr(c) + os.sep for c in range(ord("A"), ord("Z") + 1))
    + ("0-9" + os.sep, "_Other" + os.sep, "_Unknown" + os.sep,
       "_Review" + os.sep, "_QUARANTINE" + os.sep,
       "Compilations" + os.sep, "Soundtracks" + os.sep,
       "Radio" + os.sep, "Sound Effects" + os.sep,
       "Video Game Soundtracks" + os.sep, "Audiobooks" + os.sep,
       "Comedy" + os.sep)
)

# A single-character new-tree letter folder (e.g. "F\Foo Fighters\...") is depth-2
NEW_TREE_LETTERS = set([chr(c) for c in range(ord("A"), ord("Z") + 1)] + ["0-9"])

# Treat these as DISSOLVE — distribute audio to artist hierarchy by tag
EXTENDED_GENRE_BUCKETS = {
    "90s","80s","70s","60s","50s","00s","10s","oldies",
    "country","pop","rock","classical","jazz","folk","blues","reggae",
    "hip hop","hip-hop","rap","r&b","rnb","soul","disco","metal",
    "punk","alternative","indie","electronic","dance","techno","house",
    "ambient","new age","world","latin","spanish","italian","french",
    "german","japanese","korean","chinese","arabic","yiddish","hindi",
    "irish","gaelic","celtic","christmas","halloween","holiday",
    "musicals","musical","childrens","children","children's",
    "kids","family","jewish","opera","klezmer","wvum",
}

ILLEGAL_CHARS = {"<": "(", ">": ")", ":": " -", '"': "'", "/": " ",
                 "\\": " ", "|": "-", "?": "", "*": ""}
WIN_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
LEAD_ARTICLES = ("The ", "A ", "An ", "Le ", "La ", "Les ", "El ", "Los ")

# ---- helpers -------------------------------------------------------------

def sanitize(s, max_len=120):
    if not s:
        return ""
    s = str(s)
    for bad, good in ILLEGAL_CHARS.items():
        s = s.replace(bad, good)
    s = "".join(ch if ord(ch) >= 32 else " " for ch in s)
    s = re.sub(r"\s+", " ", s).strip().rstrip(". ")
    if s.upper().split(".")[0] in WIN_RESERVED:
        s = "_" + s
    return s[:max_len].rstrip(". ")


def truncate_compilation_artist(artist):
    """For long comma-separated compilation credits, keep only first artist."""
    if not artist:
        return artist
    # If artist is suspiciously long (>60 chars) and contains commas, keep the
    # first credit and append ' & others'.
    if len(artist) > 60 and "," in artist:
        first = artist.split(",")[0].strip()
        return first + " & others"
    return artist


def letter_for(artist):
    if not artist:
        return "_Unknown"
    a = artist.strip()
    # Strip leading apostrophes/quotes
    a = a.lstrip("'\"`‘’")
    for art in LEAD_ARTICLES:
        if a.lower().startswith(art.lower()):
            a = a[len(art):].strip()
            break
    if not a:
        return "_Unknown"
    c = a[0].upper()
    if c.isalpha() and ord(c) < 128:
        return c
    if c.isdigit():
        return "0-9"
    # Non-Latin (CJK, accented, emoji, etc.) — group all under _Other
    return "_Other"


def parse_year(date_str):
    if not date_str:
        return ""
    m = re.search(r"\b(19|20)\d{2}\b", str(date_str))
    return m.group(0) if m else ""


def parse_track(track_str):
    if not track_str:
        return ""
    m = re.match(r"\s*(\d+)", str(track_str))
    if not m:
        return ""
    return f"{int(m.group(1)):02d}"


def parse_filename_artist_title(stem):
    """Parse filenames like 'Artist - Title' or 'Title.mp4'."""
    # Strip leading track-number prefix
    stem = re.sub(r"^\s*\d{1,3}[\s\-_.]+", "", stem)
    # Look for ' - ' separator
    if " - " in stem:
        parts = stem.split(" - ", 1)
        artist = parts[0].strip()
        title = parts[1].strip()
        # Clean trailing junk like "(Official Video)", "(Audio)"
        title = re.sub(r"\s*\(\s*(official|audio|video|hd|hq|lyrics?|lyric video|live|remaster(ed)?( \d+)?|mv|music video|official.*?video|original.*?video).*?\)\s*", "", title, flags=re.I).strip()
        return artist, title
    # No separator — treat whole as title
    return "", stem.strip()


def get_easy_tags(path):
    """Returns (artist, album, title, track, date, duration_sec) or all empty."""
    try:
        f = MutagenFile(str(path), easy=True)
        if not f or not f.tags:
            info = getattr(f, "info", None) if f else None
            return ("", "", "", "", "", round(info.length, 2) if info else 0)
        t = f.tags
        return (
            (t.get("artist", [""]) or [""])[0],
            (t.get("album", [""]) or [""])[0],
            (t.get("title", [""]) or [""])[0],
            (t.get("tracknumber", [""]) or [""])[0],
            (t.get("date", [""]) or [""])[0] or (t.get("year", [""]) or [""])[0],
            round(f.info.length, 2) if hasattr(f, "info") else 0,
        )
    except Exception:
        return ("", "", "", "", "", 0)


def is_new_tree_path(rel_path):
    rp = rel_path.replace("/", os.sep)
    parts = rp.split(os.sep)
    if not parts:
        return False
    return parts[0] in NEW_TREE_LETTERS or rp.startswith(NEW_TREE_PREFIXES)


# ---- main ----------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--music-root", required=True)
    ap.add_argument("--execute", action="store_true",
                    help="Actually move files (default: just write the CSV)")
    ap.add_argument("--csv-out", default="cleanup_remediation_stragglers.csv")
    args = ap.parse_args()

    music_root = Path(args.music_root).resolve()
    csv_path = music_root.parent / args.csv_out  # write next to where we run
    # Better: write to current directory
    csv_path = Path(args.csv_out).resolve()
    print(f"Music root: {music_root}")
    print(f"Output CSV: {csv_path}")

    # Walk music_root looking for audio files NOT in new-tree shape
    print("\nWalking music root for stragglers...")
    t0 = time.time()
    stragglers = []
    skip_dirs = {"_QUARANTINE"}
    for dirpath, dirnames, filenames in os.walk(music_root):
        # Skip _QUARANTINE entirely
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        try:
            rel_dir = Path(dirpath).relative_to(music_root)
        except ValueError:
            continue
        rel_str = str(rel_dir) if str(rel_dir) != "." else ""
        # If the top-level segment is a new-tree letter or special bucket, skip subtree
        if rel_str:
            top = rel_str.split(os.sep)[0]
            if top in NEW_TREE_LETTERS or any(rel_str.startswith(p.rstrip(os.sep)) for p in NEW_TREE_PREFIXES):
                continue
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in AUDIO_EXTS:
                continue
            stragglers.append((Path(dirpath) / fn, rel_str, fn, ext))
    print(f"Found {len(stragglers):,} straggler audio files in {time.time()-t0:.1f}s")

    # For each straggler, propose a new path using extended rules
    out_rows = []
    counters = {"already_at_dst": 0, "no_artist_no_title": 0, "moved": 0,
                "filename_recovered": 0, "tag_recovered": 0,
                "genre_dissolved": 0}

    for full_path, rel_dir, filename, ext in stragglers:
        # Identify "bucket" from old top-level folder
        old_top = rel_dir.split(os.sep)[0] if rel_dir else ""
        old_top_lower = old_top.lower()
        if old_top_lower in EXTENDED_GENRE_BUCKETS:
            bucket = "DISSOLVE"
        elif "compilation" in old_top_lower or "various" in old_top_lower:
            bucket = "COMPILATION"
        elif "discography" in old_top_lower or "complete" in old_top_lower:
            bucket = "ARTIST"  # treat the discography folder as an artist folder
        else:
            bucket = "ARTIST"

        # Read tags
        artist, album, title, track_str, date, duration = get_easy_tags(full_path)
        artist_src = "tag"

        # Filename fallback for artist/title
        if not artist or not title:
            stem = filename[: -len(ext)]
            f_artist, f_title = parse_filename_artist_title(stem)
            if not artist and f_artist:
                artist = f_artist
                artist_src = "filename"
                counters["filename_recovered"] += 1
            if not title and f_title:
                title = f_title

        # Folder fallback for artist (use depth-1 folder name if it's not a genre)
        if not artist and old_top and old_top_lower not in EXTENDED_GENRE_BUCKETS:
            artist = old_top
            artist_src = "folder"

        # Truncate compilation-style monster artist strings
        artist = truncate_compilation_artist(artist)

        # Album fallback: use parent folder name if not a generic CD/Disc marker
        if not album and rel_dir:
            parts = rel_dir.split(os.sep)
            if parts:
                cand = parts[-1]
                if cand and not re.fullmatch(r"(?i)cd\s*\d+|disc\s*\d+", cand.strip()):
                    album = cand

        if not artist and not title:
            # Truly unidentifiable — quarantine to _Review folder
            counters["no_artist_no_title"] += 1
            new_rel = "_Review" + os.sep + (rel_dir + os.sep if rel_dir else "") + filename
            out_rows.append({
                "src_abs": str(full_path),
                "src_rel": str(Path(rel_dir) / filename) if rel_dir else filename,
                "bucket": "REVIEW",
                "artist": "", "album": "", "year": "", "track": "", "title": "",
                "new_rel": new_rel,
                "new_abs": str(music_root / new_rel),
                "notes": "no artist or title - sent to _Review",
            })
            continue

        # Default fallbacks
        if not title:
            title = filename[: -len(ext)] if ext else filename
        if not album:
            album = "_Singles"  # bucket for loose root tracks
        year = parse_year(date)
        track = parse_track(track_str)

        # Build new relative path
        ext = ext.lower()
        # Sanitize
        artist_s = sanitize(artist) if artist else "_Unknown Artist"
        album_clean = re.sub(r"^(19|20)\d{2}\s*[-_]\s*", "", album)
        album_s = sanitize(year + " - " + album_clean) if year else sanitize(album_clean)
        title_s = sanitize(title)
        if track:
            track_s = sanitize(track)
            base = track_s + " - " + title_s
        else:
            base = title_s
        new_filename = base + ext

        if bucket == "DISSOLVE" or bucket == "ARTIST":
            new_rel = os.path.join(letter_for(artist), artist_s, album_s, new_filename)
            counters["genre_dissolved" if bucket == "DISSOLVE" else "moved"] += 1
        elif bucket == "COMPILATION":
            new_rel = os.path.join("Compilations", album_s, new_filename)
        else:
            new_rel = os.path.join("_Review", filename)

        new_abs = str(music_root / new_rel)

        # If src == dst, skip
        if str(full_path) == new_abs:
            counters["already_at_dst"] += 1
            continue

        out_rows.append({
            "src_abs": str(full_path),
            "src_rel": str(Path(rel_dir) / filename) if rel_dir else filename,
            "bucket": bucket,
            "artist": artist, "album": album, "year": year,
            "track": track, "title": title,
            "new_rel": new_rel,
            "new_abs": new_abs,
            "notes": "artist_src=" + artist_src,
        })

    # Detect collisions in remediation plan
    by_dest = {}
    for r in out_rows:
        by_dest.setdefault(r["new_abs"], []).append(r)
    collisions = sum(1 for v in by_dest.values() if len(v) > 1)
    if collisions:
        print(f"WARNING: {collisions} destination collisions in remediation plan")
        # Mark all colliding rows
        for dest, group in by_dest.items():
            if len(group) > 1:
                # Use first row as winner
                for i, r in enumerate(group):
                    if i > 0:
                        r["new_rel"] = "_Review" + os.sep + r["src_rel"]
                        r["new_abs"] = str(music_root / r["new_rel"])
                        r["notes"] += " | collision_loser_routed_to_review"

    # Write CSV
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
        w.writeheader()
        w.writerows(out_rows)
    print(f"\nWrote {len(out_rows):,} planned moves to: {csv_path}")
    print(f"Counters: {counters}")

    # Execute moves
    if not args.execute:
        print("\n(dry-run; pass --execute to actually move files)")
        return

    print("\nExecuting moves...")
    quar_root = music_root / "_QUARANTINE"
    quar_root.mkdir(exist_ok=True)
    rev_log = quar_root / "reversal_log.tsv"
    n_moved = n_skipped = n_err = 0
    with open(rev_log, "a", encoding="utf-8") as rev_f:
        for r in out_rows:
            src = r["src_abs"]
            dst = r["new_abs"]
            if not os.path.exists(src):
                n_skipped += 1
                continue
            if os.path.exists(dst):
                n_skipped += 1
                continue
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                os.rename(src, dst)
                rev_f.write(f"{dst}\t{src}\tremediation_straggler\n")
                rev_f.flush()
                n_moved += 1
                # LRC sibling
                lrc_src = src[: -len(os.path.splitext(src)[1])] + ".lrc"
                if os.path.exists(lrc_src):
                    lrc_dst = dst[: -len(os.path.splitext(dst)[1])] + ".lrc"
                    try:
                        os.rename(lrc_src, lrc_dst)
                        rev_f.write(f"{lrc_dst}\t{lrc_src}\tremediation_straggler_lrc\n")
                        rev_f.flush()
                    except Exception:
                        pass
            except Exception as e:
                n_err += 1
                print(f"  ERROR: {src}: {e}")

    print(f"\n=== Execute summary ===")
    print(f"  moved   : {n_moved}")
    print(f"  skipped : {n_skipped}")
    print(f"  errors  : {n_err}")


if __name__ == "__main__":
    main()
