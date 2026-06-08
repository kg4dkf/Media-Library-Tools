#!/usr/bin/env python3
"""
Reclassify tag_changes_preview.csv to shrink the REVIEW pile.

Strategies (all enabled by default; turn off with --no-<strategy>):

  trust-folder-on-tag-disagree
     artist_tag_disagrees / album_tag_disagrees -> drop reason. User
     policy says folder wins, so per-file ID3 disagreement isn't a
     review trigger. If this was the ONLY reason, file becomes
     IMPROVEMENT (or AUTO_APPLY if low n_changes).

  whitelist-short-artists
     artist_too_short -> drop reason if the artist is alphanumeric
     and looks like a real short name (U2, AC, JJ, M83, etc.).

  keep-old-single-letter-artist  (default ON)
     If artist is a SINGLE letter/digit (folder routing accident
     from bad ID3), reclassify the file as KEEP_OLD.

  recover-artist-from-filename  (opt-in flag)
     For single-letter-artist files, try to parse "Artist - Title.ext"
     out of the filename. If successful, update artist_new in the
     output and drop the review reason. Better than KEEP_OLD when the
     filename has a recoverable artist.

  keep-old-for-misrouted
     album_looks_like_compilation / album_is_just_a_year /
     album_unknown -> reclassify the FILE as KEEP_OLD (don't change
     the tags). These reflect bad folder data; existing tags are
     probably the correct truth.

  trust-many-changes
     many_changes alone -> drop reason. If 6+ fields are improving
     and no OTHER review trigger, that's signal-rich, not suspicious.

After reasons are pruned, re-classify each row:
  - any remaining review reasons -> still REVIEW
  - keep-old triggered -> KEEP_OLD
  - n_changes == 0 -> KEEP_OLD
  - n_changes >= 3 -> IMPROVEMENT
  - else -> AUTO_APPLY

USAGE
-----
  python reclassify_preview.py --in tag_changes_preview.csv --out tag_changes_preview_v3.csv
  python reclassify_preview.py --in ... --out ... --recover-artist-from-filename
"""
import argparse, csv, io, os, re, sys
from collections import Counter


def is_alphanumeric_artist(name):
    """Plausible real short artist (U2, AC, JJ, M83, KT, etc.)."""
    if not name:
        return False
    n = name.strip()
    if len(n) < 2:
        return False
    return all(c.isalnum() or c in "&-+" for c in n)


def is_single_letter_artist(name):
    """Is this artist a single letter/digit (folder routing accident)?"""
    if not name:
        return True
    return len(name.strip()) == 1


def recover_artist_from_filename(rel_path):
    """Extract artist from 'Artist - Title.ext' or 'Artist - Album - 01 - Title.ext'."""
    if not rel_path:
        return ""
    filename = os.path.basename(rel_path.replace("/", "\\").rstrip("\\"))
    stem = os.path.splitext(filename)[0]
    stem = re.sub(r"^\s*\d{1,3}[\s\-_.]+", "", stem)
    if " - " in stem:
        candidate = stem.split(" - ")[0].strip()
        if len(candidate) >= 2 and not candidate.lower().startswith(("the album", "track")):
            return candidate
    return ""


def read_tolerant(path):
    raw = open(path, "rb").read().replace(b"\x00", b"").decode("utf-8", "replace")
    return list(csv.DictReader(io.StringIO(raw)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--no-trust-folder", action="store_true",
                    help="Don't auto-drop artist_tag_disagrees / album_tag_disagrees")
    ap.add_argument("--no-whitelist-short", action="store_true",
                    help="Don't auto-drop artist_too_short for plausible short names")
    ap.add_argument("--no-keep-old-for-misrouted", action="store_true",
                    help="Don't reclassify misrouted-comp rows as KEEP_OLD")
    ap.add_argument("--no-trust-many-changes", action="store_true",
                    help="Don't auto-drop lone-many_changes reason")
    ap.add_argument("--keep-old-single-letter-artist", action="store_true", default=True,
                    help="For single-letter artists (broken folder routing), KEEP_OLD (default ON)")
    ap.add_argument("--no-keep-old-single-letter-artist", action="store_false",
                    dest="keep_old_single_letter_artist")
    ap.add_argument("--recover-artist-from-filename", action="store_true",
                    help="For single-letter-artist files, try to extract real artist from filename")
    args = ap.parse_args()

    rows = read_tolerant(args.inp)
    print(f"Loaded {len(rows):,} rows")

    before = Counter(r["classification"] for r in rows)
    print("\nBefore:")
    for k, n in before.most_common():
        print(f"  {k:<14} {n:>7,}")

    REASON_TRUST_FOLDER = {"artist_tag_disagrees", "album_tag_disagrees"}
    REASON_SHORT_ARTIST = {"artist_too_short"}
    REASON_KEEP_OLD = {"album_looks_like_compilation",
                       "album_is_just_a_year", "album_unknown"}
    REASON_TRUST_MANY = {"many_changes"}

    stats = Counter()
    for r in rows:
        if r["classification"] != "REVIEW":
            continue
        notes = r.get("notes", "") or ""
        raw_reasons = [s.strip() for s in notes.split(";") if s.strip()]
        clean_reasons = [re.sub(r"_\(sim=[\d.]+\)", "", x) for x in raw_reasons]

        kept_reasons = []
        keep_old_now = False

        for raw, clean in zip(raw_reasons, clean_reasons):
            # Strategy: trust folder on tag disagree
            if not args.no_trust_folder and clean in REASON_TRUST_FOLDER:
                stats[f"dropped:{clean}"] += 1
                continue

            # Strategy: short artist handling
            if clean in REASON_SHORT_ARTIST:
                artist = r.get("artist_new") or r.get("artist_old") or ""
                # Try filename recovery first if requested
                if args.recover_artist_from_filename and is_single_letter_artist(artist):
                    recovered = recover_artist_from_filename(r.get("rel_path", ""))
                    if recovered and len(recovered) >= 2:
                        r["artist_new"] = recovered
                        r["artist_source"] = "filename_recovered"
                        stats["recovered_artist_from_filename"] += 1
                        continue
                # Otherwise KEEP_OLD for single-letter
                if args.keep_old_single_letter_artist and is_single_letter_artist(artist):
                    keep_old_now = True
                    stats["keep_old_for:single_letter_artist"] += 1
                    continue
                # Whitelist alphanumeric short artists
                if not args.no_whitelist_short and is_alphanumeric_artist(artist):
                    stats["dropped:artist_too_short"] += 1
                    continue
                stats["kept:artist_too_short"] += 1
                kept_reasons.append(raw)
                continue

            # Strategy: keep-old for misrouted comps
            if not args.no_keep_old_for_misrouted and clean in REASON_KEEP_OLD:
                keep_old_now = True
                stats[f"keep_old_for:{clean}"] += 1
                continue

            # Strategy: trust many_changes when it's alone
            if not args.no_trust_many_changes and clean in REASON_TRUST_MANY:
                if len(clean_reasons) == 1:
                    stats[f"dropped:{clean}"] += 1
                    continue
                stats[f"kept:{clean}"] += 1
                kept_reasons.append(raw)
                continue

            kept_reasons.append(raw)

        try:
            n_ch = int(r.get("n_changes") or 0)
        except ValueError:
            n_ch = 0

        if kept_reasons:
            new_cls = "REVIEW"
        elif keep_old_now:
            new_cls = "KEEP_OLD"
        elif n_ch == 0:
            new_cls = "KEEP_OLD"
        elif n_ch >= 3:
            new_cls = "IMPROVEMENT"
        else:
            new_cls = "AUTO_APPLY"

        r["classification"] = new_cls
        r["notes"] = ";".join(kept_reasons)

    after = Counter(r["classification"] for r in rows)
    print("\nAfter:")
    for k, n in after.most_common():
        delta = after[k] - before.get(k, 0)
        sign = "+" if delta >= 0 else ""
        print(f"  {k:<14} {n:>7,}  ({sign}{delta:,})")

    print("\nReason-handling stats:")
    for k, n in stats.most_common():
        print(f"  {n:>5,}  {k}")

    cols = list(rows[0].keys())
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    print(f"\nWrote: {args.out}")


if __name__ == "__main__":
    main()


if __name__ == "__main__":
    main()
