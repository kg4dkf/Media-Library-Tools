"""Phase 4b: Resolve rename-destination conflicts via per-group winner picking.

For every group of files mapping to the same proposed new path, pick a winner
using the same priority logic as the hash-based dedupe:
   1. Lossless ext > lossy ext  (FLAC/WAV/ALAC > MP3/M4A/etc.)
   2. Higher bitrate
   3. Has embedded art
   4. More tags filled in
   5. Shortest source path
   6. Alphabetical (deterministic tiebreak)

Output:
   cleanup_post_rename_dedupe_to_quarantine.csv  -- losers, ready for a second
                                                    quarantine pass
   cleanup_rename_preview.csv                     -- rewritten with conflicts gone
"""
import csv, io, os, sqlite3, time
from collections import defaultdict, Counter

DB = "/tmp/music_snapshot.db"
PREVIEW = "/sessions/wizardly-determined-sagan/mnt/Music Cleanup/cleanup_rename_preview.csv"
LOSERS_OUT = "/sessions/wizardly-determined-sagan/mnt/Music Cleanup/cleanup_post_rename_dedupe_to_quarantine.csv"

LOSSLESS_EXTS = {".flac", ".wav", ".aiff", ".aif", ".alac", ".ape", ".wv", ".tta",
                 ".dsf", ".dff"}


def read_tolerant(path):
    raw = open(path, "rb").read().replace(b"\x00", b"").decode("utf-8", "replace")
    return list(csv.DictReader(io.StringIO(raw)))


def main():
    t0 = time.time()
    rows = read_tolerant(PREVIEW)
    print(f"loaded preview rows: {len(rows):,}  ({time.time()-t0:.1f}s)")

    # Pull per-file technical metadata from DB
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    meta = {}
    for r in con.execute(
        "SELECT abs_path, ext, bitrate_kbps, has_embedded_art, "
        "tag_artist, tag_album_artist, tag_album, tag_title, tag_track, "
        "tag_disc, tag_date, tag_genre, tag_composer, audio_codec, file_hash, size_bytes "
        "FROM files WHERE is_audio='True'"
    ):
        meta[r["abs_path"]] = dict(r)
    con.close()
    print(f"loaded DB metadata for {len(meta):,} files  ({time.time()-t0:.1f}s)")

    # Group preview rows by destination
    by_dest = defaultdict(list)
    for r in rows:
        by_dest[r["new_abs_path"]].append(r)

    conflicts = {d: rs for d, rs in by_dest.items() if len(rs) > 1}
    print(f"conflict groups: {len(conflicts):,}")
    print(f"files in conflicts: {sum(len(rs) for rs in conflicts.values()):,}")

    NEW_TREE_TOPS = (
        tuple(chr(c) + "\\" for c in range(ord("A"), ord("Z") + 1))
        + ("0-9\\", "Compilations\\", "Soundtracks\\", "Radio\\",
           "Sound Effects\\", "Video Game Soundtracks\\",
           "Audiobooks\\", "Comedy\\")
    )

    def in_new_tree(rel_path):
        rp = rel_path.replace("/", "\\")
        return any(rp.startswith(t) for t in NEW_TREE_TOPS)

    def score(row):
        m = meta.get(row["abs_path"], {})
        ext = (m.get("ext") or row["ext"] or "").lower()
        is_lossless = ext in LOSSLESS_EXTS
        try:
            br = int(m.get("bitrate_kbps") or 0)
        except (TypeError, ValueError):
            br = 0
        # tag completeness
        tag_complete = sum(1 for k in (
            "tag_artist", "tag_album_artist", "tag_album", "tag_title",
            "tag_track", "tag_disc", "tag_date", "tag_genre", "tag_composer"
        ) if (m.get(k) or "").strip())
        has_art = (m.get("has_embedded_art") or "").lower() == "true"
        path_len = len(row["rel_path"])
        # NEW: strongly prefer files already in the new tree (already-moved
        # canonical copies). This breaks ties between stragglers and already-
        # renamed files in favor of keeping the moved copy.
        already_moved = in_new_tree(row["rel_path"])
        # lower tuple -> better. So negate the "more is better" criteria.
        return (
            0 if already_moved else 1,    # already-moved wins
            -1 if is_lossless else 0,     # lossless first
            -br,                           # higher bitrate first
            -1 if has_art else 0,         # has_art first
            -tag_complete,                # more tags first
            path_len,                     # shorter path first
            row["rel_path"].lower(),      # alphabetical tiebreak
        )

    # Pick winner per conflict; mark losers for quarantine
    losers = []
    winners_kept = 0
    for dest, group in conflicts.items():
        scored = sorted(((score(r), r) for r in group), key=lambda x: x[0])
        winner = scored[0][1]
        winners_kept += 1
        for s, r in scored[1:]:
            losers.append({
                "group_dest": dest,
                "abs_path": r["abs_path"],
                "rel_path": r["rel_path"],
                "ext": r["ext"],
                "bucket": r["bucket"],
                "artist_used": r["artist_used"],
                "album_used": r["album_used"],
                "title_used": r["title_used"],
                "winner_rel_path": winner["rel_path"],
                "winner_abs_path": winner["abs_path"],
                "quarantine_dest": "_QUARANTINE\\post_rename_dedupe\\" + r["rel_path"],
                "action": "MOVE",
            })

    print(f"winners kept: {winners_kept:,}")
    print(f"losers (for additional quarantine): {len(losers):,}")

    # Estimate bytes recoverable
    bytes_loser = 0
    for l in losers:
        bytes_loser += int(meta.get(l["abs_path"], {}).get("size_bytes") or 0)
    print(f"loser bytes: {bytes_loser/1024**3:.2f} GB")

    # Write losers CSV
    if losers:
        with open(LOSERS_OUT, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(losers[0].keys()))
            w.writeheader()
            w.writerows(losers)
        print(f"wrote: {LOSERS_OUT}")

    # Now rewrite preview: keep winners + non-conflicting rows, drop losers
    losers_set = set(l["abs_path"] for l in losers)
    new_rows = [r for r in rows if r["abs_path"] not in losers_set]
    # Clear conflict_with annotations on the survivors
    for r in new_rows:
        r["conflict_with"] = ""

    # Verify no remaining conflicts
    new_by_dest = defaultdict(list)
    for r in new_rows:
        new_by_dest[r["new_abs_path"]].append(r)
    remaining = sum(1 for rs in new_by_dest.values() if len(rs) > 1)
    print(f"remaining conflicts after winner-pick: {remaining}")

    with open(PREVIEW, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(new_rows[0].keys()))
        w.writeheader()
        w.writerows(new_rows)
    print(f"rewrote: {PREVIEW}  ({len(new_rows):,} rows)")

    # Sample a couple of resolutions
    print("\nSample winner-loser groups:")
    for dest, group in list(conflicts.items())[:5]:
        scored = sorted(((score(r), r) for r in group), key=lambda x: x[0])
        print(f"\nDEST: {scored[0][1]['new_rel_path']}")
        for s, r in scored:
            m = meta.get(r["abs_path"], {})
            ext = m.get("ext") or r["ext"]
            br = m.get("bitrate_kbps") or "?"
            tag = "★KEEP" if r is scored[0][1] else " quar"
            print(f"  {tag}  ext={ext:<6} br={br:<5}  src: {r['rel_path'][:90]}")
    print(f"\nTotal time: {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
