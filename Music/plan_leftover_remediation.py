#!/usr/bin/env python3
"""
Plan remediation for leftover audio.

Reads:
  - leftover_files.csv  (output of snapshot_leftover_audio.py)
  - the working DB     (/tmp/music_snapshot.db OR --db path)
  - cleanup_depth1_categorization_FINAL.csv  (for genre-bucket rules)

For each leftover audio file, decides one of:
  HASH_DUPLICATE_OF_NEW   -- exact hash exists in new tree -> quarantine
  HASH_DUPLICATE_INTERNAL -- exact hash exists in another leftover -> winner-pick
  DESTINATION_COLLISION   -- planned new path occupied by an existing new-tree
                             file (likely near-dup) -> quarantine
  NEEDS_RENAME            -- unique, plan a move into the new tree
  NEEDS_REVIEW            -- can't determine artist/album/title

Output:
  leftover_remediation_plan.csv

USAGE
-----
   python plan_leftover_remediation.py \\
       --leftover-csv "H:\\Music Cleanup\\leftover_files.csv" \\
       --db "H:\\Music Cleanup\\music_snapshot.db" \\
       --categorization "H:\\Music Cleanup\\cleanup_depth1_categorization_FINAL.csv" \\
       --out "H:\\Music Cleanup\\leftover_remediation_plan.csv"

Note: if the working DB has been kept in /tmp on the analysis machine, copy
or symlink it next to your other CSVs, or use the patch_db_post_rename.py
output if you have it locally.
"""
import argparse, csv, io, os, re, sqlite3, sys
from collections import Counter, defaultdict
from pathlib import Path

# Reused from build_rename_preview.py
ILLEGAL_CHARS = {"<": "(", ">": ")", ":": " -", '"': "'", "/": " ",
                 "\\": " ", "|": "-", "?": "", "*": ""}
WIN_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
LEAD_ARTICLES = ("The ", "A ", "An ", "Le ", "La ", "Les ", "El ", "Los ")
MUSIC_ROOT = "E:\\Music"


def sanitize(s, max_len=120):
    if not s: return ""
    s = str(s)
    for bad, good in ILLEGAL_CHARS.items():
        s = s.replace(bad, good)
    s = "".join(ch if ord(ch) >= 32 else " " for ch in s)
    s = re.sub(r"\s+", " ", s).strip().rstrip(". ")
    if s.upper().split(".")[0] in WIN_RESERVED:
        s = "_" + s
    return s[:max_len].rstrip(". ")


def letter_for(artist):
    if not artist: return "_Unknown"
    a = artist.strip().lstrip("'\"`")
    for art in LEAD_ARTICLES:
        if a.lower().startswith(art.lower()):
            a = a[len(art):].strip()
            break
    if not a: return "_Unknown"
    c = a[0].upper()
    if c.isalpha() and ord(c) < 128: return c
    if c.isdigit(): return "0-9"
    return "_Other"


def parse_year(date_str):
    if not date_str: return ""
    m = re.search(r"\b(19|20)\d{2}\b", str(date_str))
    return m.group(0) if m else ""


def parse_track(track_str):
    if not track_str: return ""
    m = re.match(r"\s*(\d+)", str(track_str))
    return f"{int(m.group(1)):02d}" if m else ""


def truncate_compilation_artist(artist):
    if not artist: return artist
    if len(artist) > 60 and "," in artist:
        return artist.split(",", 1)[0].strip() + " & others"
    return artist


def folder_segments(rel_path):
    return [p for p in re.split(r"[\\/]", rel_path or "") if p]


def parse_filename_artist_title(stem):
    stem = re.sub(r"^\s*\d{1,3}[\s\-_.]+", "", stem)
    if " - " in stem:
        a, t = stem.split(" - ", 1)
        return a.strip(), t.strip()
    return "", stem.strip()


def read_tolerant_csv(path):
    raw = open(path, "rb").read().replace(b"\x00", b"").decode("utf-8", "replace")
    return list(csv.DictReader(io.StringIO(raw)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--leftover-csv", required=True)
    ap.add_argument("--db", required=True,
                    help="Working DB with the new-tree files (file_hash column populated)")
    ap.add_argument("--categorization", required=True,
                    help="cleanup_depth1_categorization_FINAL.csv from earlier phases")
    ap.add_argument("--out", default="leftover_remediation_plan.csv")
    args = ap.parse_args()

    # Load categorization (still useful for understanding bucket structure)
    cat_map = {}
    for r in read_tolerant_csv(args.categorization):
        cat_map[r["folder_name"]] = r["auto_category"]
    print(f"Loaded categorization for {len(cat_map):,} depth-1 folders")

    # Load DB: build sets/maps of files actually in the new tree
    # (filter out stragglers — files in DB but at non-new-tree paths)
    NEW_TREE_PREFIXES = (
        tuple(chr(c) + "\\" for c in range(ord("A"), ord("Z") + 1))
        + ("0-9\\", "Compilations\\", "Soundtracks\\", "Radio\\",
           "Sound Effects\\", "Video Game Soundtracks\\",
           "Audiobooks\\", "Comedy\\", "_Other\\", "_Unknown\\")
    )
    def in_new_tree(rel):
        if not rel:
            return False
        s = rel.replace("/", "\\")
        return any(s.startswith(p) for p in NEW_TREE_PREFIXES)

    print(f"Loading new-tree state from {args.db}...")
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    new_tree_hashes = set()
    new_tree_abs_paths = set()
    n_total = n_in_tree = 0
    for r in con.execute(
        "SELECT file_hash, abs_path, rel_path FROM files WHERE is_audio='True'"
    ):
        n_total += 1
        if not in_new_tree(r["rel_path"] or ""):
            continue
        n_in_tree += 1
        h = (r["file_hash"] or "").strip()
        if h:
            new_tree_hashes.add(h)
        new_tree_abs_paths.add(r["abs_path"])
    con.close()
    print(f"  audio rows total: {n_total:,}  in-new-tree: {n_in_tree:,}")
    print(f"  new-tree hashes: {len(new_tree_hashes):,}")
    print(f"  new-tree abs_paths: {len(new_tree_abs_paths):,}")

    # Load leftover snapshot
    print(f"Loading leftover snapshot from {args.leftover_csv}...")
    leftover_rows = read_tolerant_csv(args.leftover_csv)
    print(f"  leftover audio: {len(leftover_rows):,}")

    # Step 1: internal dedup — same hash among leftovers
    by_hash = defaultdict(list)
    for r in leftover_rows:
        h = (r.get("file_hash") or "").strip()
        if h:
            by_hash[h].append(r)

    # For internal duplicates, winner = (lossless > lossy, bitrate, tag completeness, shortest path)
    LOSSLESS = {".flac", ".wav", ".aiff", ".aif", ".alac", ".ape", ".wv", ".tta", ".dsf", ".dff"}
    def score_internal(r):
        ext = (r.get("ext") or "").lower()
        try: br = int(r.get("bitrate_kbps") or 0)
        except: br = 0
        tag_complete = sum(1 for k in ("tag_artist","tag_album","tag_title","tag_track","tag_date","tag_genre")
                           if (r.get(k) or "").strip())
        return (
            -1 if ext in LOSSLESS else 0,
            -br,
            -tag_complete,
            len(r.get("rel_path") or ""),
            (r.get("rel_path") or "").lower(),
        )

    internal_winners = {}  # hash -> abs_path of winner
    internal_losers = set()  # abs_paths that are losers
    for h, group in by_hash.items():
        if len(group) <= 1:
            continue
        scored = sorted(group, key=score_internal)
        winner = scored[0]
        internal_winners[h] = winner["abs_path"]
        for r in scored[1:]:
            internal_losers.add(r["abs_path"])
    print(f"  internal duplicate groups: {sum(1 for v in by_hash.values() if len(v)>1):,}")
    print(f"  internal losers: {len(internal_losers):,}")

    # Step 2: plan each leftover file
    plan = []
    counts = Counter()
    # Build a quick map of planned new_abs_paths to detect collisions WITHIN the leftover plan
    planned_dests = defaultdict(list)

    for r in leftover_rows:
        src = r["abs_path"]
        rel = r["rel_path"] or ""
        ext = (r.get("ext") or os.path.splitext(r.get("filename","") or "")[1]).lower()
        h = (r.get("file_hash") or "").strip()

        # Step 2a: hash duplicate of something in new tree
        if h and h in new_tree_hashes:
            counts["HASH_DUPLICATE_OF_NEW"] += 1
            plan.append({
                **r,
                "decision": "HASH_DUPLICATE_OF_NEW",
                "quarantine_dest": "_QUARANTINE\\leftover_dup_of_new\\" + rel,
                "new_abs_path": "",
                "action": "MOVE",
                "notes": "hash exists in new tree -> quarantine",
            })
            continue

        # Step 2b: internal duplicate (loser)
        if src in internal_losers:
            winner_abs = internal_winners.get(h, "")
            counts["HASH_DUPLICATE_INTERNAL"] += 1
            plan.append({
                **r,
                "decision": "HASH_DUPLICATE_INTERNAL",
                "quarantine_dest": "_QUARANTINE\\leftover_internal_dup\\" + rel,
                "new_abs_path": "",
                "action": "MOVE",
                "notes": f"internal dup of {winner_abs}",
            })
            continue

        # Step 2c: plan a rename. Apply same logic as build_rename_preview, but
        # always treat as DISSOLVE (route by tags) since leftover folder
        # categories were the source of the original missed dissolve.
        artist = (r.get("tag_artist") or "").strip()
        album = (r.get("tag_album") or "").strip()
        title = (r.get("tag_title") or "").strip()
        track = parse_track(r.get("tag_track"))
        year = parse_year(r.get("tag_date"))

        artist_src = "tag"
        if not artist:
            # try filename
            stem = (r.get("filename") or "")
            stem = stem[: -len(ext)] if ext and stem.endswith(ext) else stem
            f_artist, f_title = parse_filename_artist_title(stem)
            if f_artist:
                artist = f_artist
                artist_src = "filename"
            if not title and f_title:
                title = f_title
            # fall back to depth-2 of original path (the artist subfolder under the genre bucket)
            if not artist:
                segs = folder_segments(rel)
                if len(segs) >= 2:
                    artist = segs[1]
                    artist_src = "folder_depth2"

        artist = truncate_compilation_artist(artist)

        # album fallback
        if not album:
            segs = folder_segments(rel)
            if segs:
                cand = segs[-1] if len(segs) >= 1 else ""
                # actually we want the immediate parent folder of the file:
                pf = "\\".join(segs[:-1]) if len(segs) > 1 else ""
                # last folder
                if pf:
                    last = pf.split("\\")[-1]
                    if last and not re.fullmatch(r"(?i)cd\s*\d+|disc\s*\d+", last.strip()):
                        album = last

        if not artist or not title:
            counts["NEEDS_REVIEW"] += 1
            new_rel = "_Review\\" + rel
            plan.append({
                **r,
                "decision": "NEEDS_REVIEW",
                "quarantine_dest": "",
                "new_abs_path": MUSIC_ROOT + "\\" + new_rel,
                "action": "REVIEW",
                "notes": f"missing artist or title (artist_src={artist_src})",
            })
            continue

        # Build path
        title_s = sanitize(title)
        artist_s = sanitize(artist)
        album_clean = re.sub(r"^(19|20)\d{2}\s*[-_]\s*", "", album) if album else ""
        album_s = sanitize(year + " - " + album_clean) if year and album_clean else sanitize(album_clean or "_Singles")
        if track:
            base = sanitize(track) + " - " + title_s
        else:
            base = title_s
        new_filename = base + ext
        new_rel = os.path.join(letter_for(artist), artist_s, album_s, new_filename)
        new_rel = new_rel.replace("/", "\\")
        new_abs = MUSIC_ROOT + "\\" + new_rel

        # Step 2d: destination collides with an existing new-tree file
        if new_abs in new_tree_abs_paths:
            counts["DESTINATION_COLLISION"] += 1
            plan.append({
                **r,
                "decision": "DESTINATION_COLLISION",
                "quarantine_dest": "_QUARANTINE\\leftover_dest_collision\\" + rel,
                "new_abs_path": new_abs,
                "action": "MOVE",
                "notes": "dest already has a file in new tree (likely near-dup)",
            })
            continue

        # Also collision WITHIN the leftover plan?
        planned_dests[new_abs].append(src)

        counts["NEEDS_RENAME"] += 1
        plan.append({
            **r,
            "decision": "NEEDS_RENAME",
            "quarantine_dest": "",
            "new_abs_path": new_abs,
            "action": "RENAME",
            "notes": f"artist_src={artist_src}",
        })

    # Step 3: resolve internal-rename-collisions (two leftover files want same dest)
    n_internal_dest_collisions = 0
    for dest, srcs in planned_dests.items():
        if len(srcs) <= 1:
            continue
        n_internal_dest_collisions += 1
        # Pick a winner by score on the rows
        rows = [r for r in plan if r["abs_path"] in srcs and r["decision"] == "NEEDS_RENAME"]
        rows.sort(key=score_internal)
        winner = rows[0]
        for r in rows[1:]:
            r["decision"] = "NEEDS_RENAME_LOSER"
            r["action"] = "MOVE"
            r["quarantine_dest"] = "_QUARANTINE\\leftover_rename_collision\\" + r["rel_path"]
            counts["NEEDS_RENAME_LOSER"] = counts.get("NEEDS_RENAME_LOSER", 0) + 1
            counts["NEEDS_RENAME"] -= 1

    print(f"\n  decisions:")
    for k, n in counts.most_common():
        print(f"    {k:<28} {n:>6,}")

    # Write
    out_cols = list(plan[0].keys())
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=out_cols)
        w.writeheader()
        w.writerows(plan)
    print(f"\nWrote: {args.out}")
    print("\nNext step: review the CSV, then we'll write run_leftover_remediation.py to execute.")


if __name__ == "__main__":
    main()
