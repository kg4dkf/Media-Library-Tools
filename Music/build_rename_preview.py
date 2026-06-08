"""Phase 4: Build the rename-preview CSV.

For each remaining audio file, compute its proposed new path under E:\\Music
according to:
  - The bucket assigned to its depth-1 folder (FINAL categorization)
  - The user's policies:
      * Tag wins over folder
      * "The/A/An" stripped for Letter selection but kept in Artist name
      * Flat-by-album bucket layout (Soundtracks/Compilations/Radio/Audiobooks)
      * Tag fallbacks via folder/filename, flagged needs_review when shaky
      * No artist-variant merging in this phase
"""
import csv, io, os, re, sqlite3, sys, time
from collections import defaultdict, Counter

DB = "/tmp/music_snapshot.db"
CATEG_CSV = "/sessions/wizardly-determined-sagan/mnt/Music Cleanup/cleanup_depth1_categorization_FINAL.csv"
OUT_CSV = "/sessions/wizardly-determined-sagan/mnt/Music Cleanup/cleanup_rename_preview.csv"
SUMMARY_TXT = "/sessions/wizardly-determined-sagan/mnt/Music Cleanup/cleanup_rename_preview_SUMMARY.txt"

MUSIC_ROOT = "E:\\Music"

# ---- helpers --------------------------------------------------------------

ILLEGAL_CHARS = {
    "<": "(", ">": ")", ":": " -", '"': "'", "/": " ", "\\": " ",
    "|": "-", "?": "", "*": "",
}
WIN_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


def sanitize_component(s, max_len=180):
    """Make a string safe to use as a Windows path component."""
    if not s:
        return ""
    s = str(s)
    for bad, good in ILLEGAL_CHARS.items():
        s = s.replace(bad, good)
    # control chars
    s = "".join(ch if ord(ch) >= 32 else " " for ch in s)
    # collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    # strip trailing dots/spaces
    s = s.rstrip(". ")
    # reserved names
    if s.upper().split(".")[0] in WIN_RESERVED:
        s = "_" + s
    return s[:max_len].rstrip(". ")


_LEAD_ARTICLES = ("The ", "A ", "An ", "Le ", "La ", "Les ", "El ", "Los ")


def letter_for(artist):
    """First letter for the Letter folder, skipping leading articles."""
    if not artist:
        return "_Unknown"
    a = artist.strip()
    for art in _LEAD_ARTICLES:
        if a.lower().startswith(art.lower()):
            a = a[len(art):].strip()
            break
    if not a:
        return "_Unknown"
    c = a[0].upper()
    if c.isalpha():
        return c
    if c.isdigit():
        return "0-9"
    return "_Other"


def parse_year(date_str):
    """Pull a 4-digit year out of any tag_date format."""
    if not date_str:
        return ""
    m = re.search(r"\b(19|20)\d{2}\b", str(date_str))
    return m.group(0) if m else ""


def parse_track(track_str):
    """Track number, zero-padded to 2 digits. Handles '1/12' format."""
    if not track_str:
        return ""
    m = re.match(r"\s*(\d+)", str(track_str))
    if not m:
        return ""
    n = int(m.group(1))
    return f"{n:02d}"


def filename_stem(filename, ext):
    """Strip ext from filename."""
    if ext and filename.lower().endswith(ext.lower()):
        return filename[: -len(ext)]
    return os.path.splitext(filename)[0]


def folder_segments(rel_path):
    """Split rel_path into list of segments using either separator."""
    return [p for p in re.split(r"[\\/]", rel_path) if p]


# ---- main planner ---------------------------------------------------------

def main():
    t0 = time.time()
    # Load categorization
    cat_map = {}  # depth-1 folder name -> category
    with open(CATEG_CSV, "rb") as f:
        raw = f.read().replace(b"\x00", b"").decode("utf-8", "replace")
    for r in csv.DictReader(io.StringIO(raw)):
        cat_map[r["folder_name"]] = r["auto_category"]
    print(f"Loaded categorization for {len(cat_map):,} depth-1 folders  ({time.time()-t0:.1f}s)")

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    # Pull every active audio file
    rows = list(con.execute(
        "SELECT rel_path, abs_path, parent_folder, filename, ext, "
        "tag_artist, tag_album_artist, tag_album, tag_title, tag_track, "
        "tag_disc, tag_date, tag_genre, audio_codec, error "
        "FROM files WHERE is_audio='True'"
    ))
    print(f"Audio rows to plan: {len(rows):,}  ({time.time()-t0:.1f}s)")

    # Track destinations to detect conflicts
    dest_to_sources = defaultdict(list)

    out_rows = []
    counters = Counter()

    for r in rows:
        segs = folder_segments(r["rel_path"])
        top1 = segs[0] if segs else ""
        bucket = cat_map.get(top1, "ARTIST")  # default ARTIST if missing
        counters["bucket_" + bucket] += 1

        if bucket == "IGNORE":
            # Don't propose a move
            out_rows.append({
                "abs_path": r["abs_path"],
                "rel_path": r["rel_path"],
                "ext": r["ext"],
                "bucket": bucket,
                "artist_used": "", "artist_source": "",
                "album_used": "", "album_source": "",
                "year_used": "", "year_source": "",
                "track_used": "", "track_source": "",
                "title_used": "", "title_source": "",
                "letter": "",
                "new_rel_path": r["rel_path"],  # unchanged
                "new_abs_path": r["abs_path"],
                "needs_review": "",
                "review_reasons": "",
                "conflict_with": "",
            })
            continue

        review_reasons = []

        # ---- artist resolution -------------------------------------------
        artist = (r["tag_artist"] or "").strip()
        artist_src = "tag"
        if not artist:
            # Bucket-specific fallbacks
            if bucket == "ARTIST" and len(segs) >= 1:
                artist = segs[0]
                artist_src = "depth1_folder"
            elif bucket in ("DISSOLVE",):
                # Genre buckets: use the second segment if it looks like an artist
                if len(segs) >= 2:
                    artist = segs[1]
                    artist_src = "depth2_folder"
                else:
                    review_reasons.append("artist_missing_in_genre_bucket")
                    artist = "_Unknown Artist"
                    artist_src = "fallback"
            else:
                # SOUNDTRACK / COMPILATION / RADIO / AUDIOBOOK / etc don't need artist for path
                # but for COMPILATION track-name we'll still use it (or "Various")
                if bucket == "COMPILATION":
                    artist = "Various Artists"
                    artist_src = "fallback"
                else:
                    artist = ""
                    artist_src = ""
        if not artist and bucket not in ("SOUNDTRACK", "COMPILATION", "RADIO",
                                          "AUDIOBOOK", "SOUNDEFFECTS", "VIDEOGAME"):
            review_reasons.append("artist_missing")

        # ---- album resolution --------------------------------------------
        album = (r["tag_album"] or "").strip()
        album_src = "tag"
        if not album:
            # Walk up: if the file's parent_folder has a useful name, use it
            # Skip generic-looking names ("CD1", "Disc 1")
            if len(segs) >= 2:
                # Last segment of parent_folder should be the album folder
                pf_segs = folder_segments(r["parent_folder"])
                if pf_segs:
                    candidate = pf_segs[-1]
                    if candidate and not re.fullmatch(r"(?i)cd\s*\d+|disc\s*\d+", candidate.strip()):
                        album = candidate
                        album_src = "parent_folder"
                    elif len(pf_segs) >= 2:
                        album = pf_segs[-2]
                        album_src = "grandparent_folder"
            if not album:
                album = "_Unknown Album"
                album_src = "fallback"
                review_reasons.append("album_missing")

        # Strip leading "YYYY - " from album if it duplicates the year we'd add
        m_year_in_album = re.match(r"^(19|20)\d{2}\s*[-_]\s*(.+)$", album)
        album_inner_year = m_year_in_album.group(0).split(" - ", 1)[0] if m_year_in_album else ""
        album_clean = m_year_in_album.group(2) if m_year_in_album else album

        # ---- year resolution ---------------------------------------------
        year = parse_year(r["tag_date"])
        year_src = "tag" if year else ""
        if not year and album_inner_year:
            year = album_inner_year
            year_src = "album_folder"
        if not year:
            year_src = "missing"

        # ---- track resolution --------------------------------------------
        track = parse_track(r["tag_track"])
        track_src = "tag" if track else ""
        if not track:
            # Try to pull a leading "01 -" or "01_" or "01." from filename
            stem = filename_stem(r["filename"], r["ext"])
            m = re.match(r"^\s*(\d{1,3})[\s\-_.]+", stem)
            if m:
                n = int(m.group(1))
                if 1 <= n <= 999:
                    track = f"{n:02d}"
                    track_src = "filename"
        if not track:
            track_src = "missing"

        # ---- title resolution --------------------------------------------
        title = (r["tag_title"] or "").strip()
        title_src = "tag"
        if not title:
            stem = filename_stem(r["filename"], r["ext"])
            # Strip leading track-number-ish prefix
            stem = re.sub(r"^\s*\d{1,3}\s*[-_.]\s*", "", stem)
            # Strip trailing ".mp3" if doubled up
            title = stem.strip()
            title_src = "filename"
            if not title:
                title = "Unknown Title"
                title_src = "fallback"
                review_reasons.append("title_missing")

        # ---- compose path -----------------------------------------------
        ext = r["ext"]

        # Track filename component
        if bucket == "COMPILATION":
            # <Track> - <Artist> - <Title>.ext
            parts = []
            if track:
                parts.append(track)
            parts.append(artist or "Various Artists")
            parts.append(title)
            base = " - ".join(sanitize_component(p) for p in parts if p)
        else:
            # <Track - Title>.ext  (or just Title if no track)
            if track:
                base = sanitize_component(track) + " - " + sanitize_component(title)
            else:
                base = sanitize_component(title)
        new_filename = base + ext

        # Album folder (year-prefixed if available)
        if year:
            album_folder = sanitize_component(year + " - " + album_clean)
        else:
            album_folder = sanitize_component(album_clean)

        # Build full new path based on bucket
        if bucket == "ARTIST":
            new_rel = os.path.join(letter_for(artist),
                                   sanitize_component(artist),
                                   album_folder, new_filename)
        elif bucket == "DISSOLVE":
            new_rel = os.path.join(letter_for(artist),
                                   sanitize_component(artist),
                                   album_folder, new_filename)
        elif bucket == "SOUNDTRACK":
            new_rel = os.path.join("Soundtracks", album_folder, new_filename)
        elif bucket == "COMPILATION":
            new_rel = os.path.join("Compilations", album_folder, new_filename)
        elif bucket == "RADIO":
            # Music\Radio\<Artist or Show>\<Year - Album>\<Track - Title>
            show_or_artist = artist or (segs[1] if len(segs) >= 2 else "_Unknown")
            new_rel = os.path.join("Radio", sanitize_component(show_or_artist),
                                   album_folder, new_filename)
        elif bucket == "AUDIOBOOK":
            author = artist or "_Unknown Author"
            new_rel = os.path.join("Audiobooks", sanitize_component(author),
                                   album_folder, new_filename)
        elif bucket == "COMEDY":
            new_rel = os.path.join("Comedy", letter_for(artist),
                                   sanitize_component(artist), album_folder, new_filename)
        elif bucket == "SOUNDEFFECTS":
            # Preserve internal structure under Sound Effects
            tail_segs = segs[1:] if len(segs) > 1 else [r["filename"]]
            tail_segs[-1] = new_filename
            new_rel = os.path.join("Sound Effects", *[sanitize_component(s) for s in tail_segs])
        elif bucket == "VIDEOGAME":
            game = segs[1] if len(segs) >= 2 else "_Unknown Game"
            new_rel = os.path.join("Video Game Soundtracks", sanitize_component(game),
                                   new_filename)
        else:
            review_reasons.append(f"unknown_bucket:{bucket}")
            new_rel = os.path.join("_Review", r["rel_path"])

        # Normalize all separators to backslash for Windows.
        # Linux os.path.join uses '/', but the actual target is Windows
        # where mixed separators break \\?\ long-path support and confuse
        # case-insensitive collision detection. Force backslashes.
        new_rel = new_rel.replace("/", "\\")
        new_abs = MUSIC_ROOT + "\\" + new_rel
        dest_to_sources[new_abs].append(r["abs_path"])

        # If we landed in _Unknown anywhere, mark for review
        if "_Unknown" in new_rel or "_Review" in new_rel:
            review_reasons.append("unknown_destination_segment")

        out_rows.append({
            "abs_path": r["abs_path"],
            "rel_path": r["rel_path"],
            "ext": ext,
            "bucket": bucket,
            "artist_used": artist,
            "artist_source": artist_src,
            "album_used": album_clean,
            "album_source": album_src,
            "year_used": year,
            "year_source": year_src,
            "track_used": track,
            "track_source": track_src,
            "title_used": title,
            "title_source": title_src,
            "letter": letter_for(artist) if bucket in ("ARTIST", "DISSOLVE", "COMEDY") else "",
            "new_rel_path": new_rel,
            "new_abs_path": new_abs,
            "needs_review": "Y" if review_reasons else "",
            "review_reasons": ";".join(review_reasons),
            "conflict_with": "",  # filled below
        })

        if review_reasons:
            counters["needs_review"] += 1
        else:
            counters["clean"] += 1

    # Detect conflicts: same destination, multiple sources
    conflict_count = 0
    for dest, sources in dest_to_sources.items():
        if len(sources) > 1:
            conflict_count += 1
    print(f"Destinations with conflicts: {conflict_count:,}  ({time.time()-t0:.1f}s)")

    # Annotate conflict_with
    src_to_row = {r["abs_path"]: r for r in out_rows}
    for dest, sources in dest_to_sources.items():
        if len(sources) > 1:
            for src in sources:
                others = [s for s in sources if s != src]
                src_to_row[src]["conflict_with"] = " | ".join(others[:3])
                if len(others) > 3:
                    src_to_row[src]["conflict_with"] += f" | (+{len(others)-3} more)"

    # Write CSV
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
        w.writeheader()
        w.writerows(out_rows)

    # Summary
    by_bucket = Counter(r["bucket"] for r in out_rows)
    by_review = sum(1 for r in out_rows if r["needs_review"])
    by_conflict = sum(1 for r in out_rows if r["conflict_with"])
    review_reason_counts = Counter()
    for r in out_rows:
        for rs in r["review_reasons"].split(";"):
            if rs:
                review_reason_counts[rs] += 1

    summary = []
    summary.append("Rename Preview Summary")
    summary.append("=" * 50)
    summary.append(f"Total audio files planned : {len(out_rows):,}")
    summary.append(f"Needs review              : {by_review:,}")
    summary.append(f"Conflicts (collisions)    : {by_conflict:,}")
    summary.append("")
    summary.append("By bucket:")
    for b, n in by_bucket.most_common():
        summary.append(f"  {b:<14} {n:>7,}")
    summary.append("")
    summary.append("Top review reasons:")
    for r, n in review_reason_counts.most_common(15):
        summary.append(f"  {n:>5,}  {r}")
    summary.append("")
    summary.append(f"Preview CSV: {OUT_CSV}")
    summary.append(f"Total time : {time.time()-t0:.1f}s")
    out_text = "\n".join(summary)
    print()
    print(out_text)
    open(SUMMARY_TXT, "w", encoding="utf-8").write(out_text)
    con.close()


if __name__ == "__main__":
    main()
")
    summary.append("")
    summary.append(f"Preview CSV: {OUT_CSV}")
    summary.append(f"Total time : {time.time()-t0:.1f}s")
    out_text = "\n".join(summary)
    print()
    print(out_text)
    open(SUMMARY_TXT, "w", encoding="utf-8").write(out_text)
    con.close()


if __name__ == "__main__":
    main()
