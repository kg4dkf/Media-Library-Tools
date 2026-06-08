#!/usr/bin/env python3
"""
Classify AcoustID matches into auto-apply / review / skip buckets based on
agreement between AcoustID's returned metadata and the file's existing hints
(filename + parent folder).

The AcoustID 'score' is FINGERPRINT match confidence only — it does NOT mean
the returned artist/title is correct. AcoustID's database is community-sourced
and contains plenty of mislabeled entries. So a 1.0 score can still be a wrong
identification if a bad contributor tagged that fingerprint poorly.

This classifier compares:
   - ac_artist vs derived filename artist
   - ac_title vs derived filename title (with fuzzy matching)
   - ac_artist vs folder hierarchy

And buckets into:
   AUTO_APPLY     score >= 0.9 AND title fuzzy match >= 0.7
   AUTO_APPLY_OK  score >= 0.9 AND ac_title appears as substring of filename
                  (or vice versa) at >= 0.6 ratio
   REVIEW_HIGH    high score but title/artist disagrees with filename
   REVIEW_LOW     score < 0.9
   REVIEW_NO_HINT score >= 0.9 but no filename/folder hint to compare

USAGE
-----
   python classify_acoustid_matches.py --in acoustid_results.csv \\
       --out acoustid_classified.csv
"""
import argparse, csv, os, re, sys
from collections import Counter
from difflib import SequenceMatcher


def clean_for_compare(s):
    """Normalize a string for fuzzy comparison."""
    if not s:
        return ""
    s = s.lower()
    s = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", s)  # strip parentheticals
    s = re.sub(r"[^\w\s\-']+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def parse_filename_hint(abs_path):
    """Try to extract (artist, title) from a music filename."""
    base = os.path.basename(abs_path)
    stem = os.path.splitext(base)[0]
    # Strip leading "01 ", "01-", "1.", etc.
    stem = re.sub(r"^\s*\d{1,3}[\s\-_.]+", "", stem)
    # Pattern: "Artist - Title" anywhere
    if " - " in stem:
        a, t = stem.split(" - ", 1)
        return a.strip(), t.strip()
    return "", stem.strip()


def parent_folder_hint(abs_path):
    """Return the depth-1 folder under the music root, useful as artist hint."""
    parts = abs_path.replace("/", "\\").split("\\")
    # /E:/Music/<Top>/<...>/file.mp3 - take Top
    if "Music" in parts:
        idx = parts.index("Music")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    if len(parts) >= 2:
        return parts[-2]
    return ""


def sim(a, b):
    return SequenceMatcher(None, clean_for_compare(a), clean_for_compare(b)).ratio()


def substring_overlap(a, b):
    """Bidirectional substring containment ratio."""
    a, b = clean_for_compare(a), clean_for_compare(b)
    if not a or not b:
        return 0
    if a in b:
        return len(a) / max(len(b), 1)
    if b in a:
        return len(b) / max(len(a), 1)
    return 0


def classify(row):
    """Return (bucket, reason_string)."""
    status = (row.get("fp_status") or "").strip()
    if status != "match":
        return f"SKIP_{status}", "no acoustid match"
    try:
        score = float(row.get("ac_score") or 0)
    except ValueError:
        score = 0

    ac_artist = row.get("ac_artist") or ""
    ac_title = row.get("ac_title") or ""
    abs_path = row.get("abs_path") or ""

    fn_artist, fn_title = parse_filename_hint(abs_path)
    folder_artist = parent_folder_hint(abs_path)

    # Compute similarities
    title_sim = sim(ac_title, fn_title) if fn_title else 0
    title_overlap = substring_overlap(ac_title, fn_title) if fn_title else 0
    artist_sim = sim(ac_artist, fn_artist) if fn_artist else 0
    artist_folder_sim = sim(ac_artist, folder_artist) if folder_artist else 0

    has_hint = bool(fn_artist or fn_title or folder_artist)
    best_title = max(title_sim, title_overlap)
    best_artist = max(artist_sim, artist_folder_sim)

    if score >= 0.95 and best_title >= 0.7 and best_artist >= 0.5:
        return "AUTO_APPLY", f"score={score:.2f} title_sim={best_title:.2f} artist_sim={best_artist:.2f}"
    if score >= 0.95 and best_title >= 0.6:
        return "AUTO_APPLY_OK", f"score={score:.2f} title_sim={best_title:.2f} (artist_sim weak {best_artist:.2f})"
    if score >= 0.95 and not has_hint:
        return "REVIEW_NO_HINT", f"score={score:.2f} but file has no usable filename/folder hint"
    if score >= 0.9 and (best_title >= 0.4 or best_artist >= 0.6):
        return "REVIEW_PARTIAL", f"score={score:.2f} title_sim={best_title:.2f} artist_sim={best_artist:.2f}"
    if score >= 0.9:
        return "REVIEW_HIGH", f"score={score:.2f} but content disagrees title={best_title:.2f} artist={best_artist:.2f}"
    return "REVIEW_LOW", f"score={score:.2f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", default="acoustid_classified.csv")
    args = ap.parse_args()

    rows = list(csv.DictReader(open(args.inp, encoding="utf-8")))
    print(f"Loaded {len(rows):,} rows")

    out_rows = []
    counts = Counter()
    for r in rows:
        bucket, reason = classify(r)
        counts[bucket] += 1
        # Compute hints for the output
        fn_a, fn_t = parse_filename_hint(r.get("abs_path", ""))
        out = dict(r)
        out["filename_artist"] = fn_a
        out["filename_title"] = fn_t
        out["folder_top"] = parent_folder_hint(r.get("abs_path", ""))
        out["bucket"] = bucket
        out["reason"] = reason
        out["user_override"] = ""
        out_rows.append(out)

    out_path = args.out
    cols = list(out_rows[0].keys())
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(out_rows)

    print("\n==== bucket counts ====")
    for k, n in counts.most_common():
        print(f"  {k:<24} {n:>6}")
    print(f"\nWrote: {out_path}")
    print()
    print("Bucket meanings:")
    print("  AUTO_APPLY      - safe to write to tags (score high AND filename agrees)")
    print("  AUTO_APPLY_OK   - title overlap is good even if artist disagrees")
    print("  REVIEW_NO_HINT  - high score but no hint to verify (probably correct, eyeball anyway)")
    print("  REVIEW_PARTIAL  - one of title/artist agrees, other doesn't")
    print("  REVIEW_HIGH     - high score but file metadata disagrees (likely AcoustID bad data OR file is wrongly named)")
    print("  REVIEW_LOW      - lower score, manual eye needed")
    print("  SKIP_*          - no match or error")


if __name__ == "__main__":
    main()
