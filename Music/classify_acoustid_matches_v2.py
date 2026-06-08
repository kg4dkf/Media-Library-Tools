#!/usr/bin/env python3
"""
classify_acoustid_matches_v2.py — improved classifier.

Fixes over-flagging in v1 by adding:
  - Filename noise stripping (Sesame Street, From Frozen, Sing-Along,
    Official Video, Tribute To, etc.)
  - Word-overlap scoring (if ac_artist and ac_title words appear inside
    filename, count as match even if string-similarity is low)
  - Substring search on full filename, not just parsed hint

USAGE
-----
  python classify_acoustid_matches_v2.py --in acoustid_results.csv \\
       --out acoustid_classified.csv
"""
import argparse, csv, os, re
from collections import Counter
from difflib import SequenceMatcher

# Noise to strip from filename before comparison
NOISE_TOKENS = [
    # Sesame Street-style attribution prefixes
    "sesame street:", "sesame street",
    "sing-along with", "sing along with", "sing-along", "sing along",
    "tribute to", "in tribute to",
    "cover of", "cover by",
    # Movie / show attribution suffixes/prefixes
    "from frozen 2", "from frozen ii", "from frozen",
    "from tangled", "from moana", "from encanto",
    "from the movie", "from the film",
    "feat.", "ft.", "featuring",
    # Video markers
    "official music video", "official video", "music video",
    "lyric video", "lyrics video", "audio",
    "hd", "hq", "live performance", "(live)",
    "mv", "official mv",
    # Other
    "soundtrack version", "ost version",
]

WORD_RE = re.compile(r"[a-z0-9']+")


def clean(s):
    if not s:
        return ""
    s = s.lower()
    s = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", s)
    s = re.sub(r"[^\w\s\-']+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def strip_noise(s):
    if not s:
        return ""
    out = s.lower()
    # repeatedly remove noise tokens
    for _ in range(3):
        changed = False
        for tok in NOISE_TOKENS:
            if tok in out:
                out = out.replace(tok, " ")
                changed = True
        if not changed:
            break
    out = re.sub(r"[^\w\s\-']+", " ", out)
    out = re.sub(r"\s+", " ", out).strip()
    return out


def words(s):
    return set(WORD_RE.findall(clean(s)))


def word_overlap(short, long_):
    """Fraction of short's words present in long_."""
    sw = words(short)
    lw = words(long_)
    if not sw:
        return 0
    return len(sw & lw) / len(sw)


def parse_filename_hint(abs_path):
    base = os.path.basename(abs_path)
    stem = os.path.splitext(base)[0]
    stem = re.sub(r"^\s*\d{1,3}[\s\-_.]+", "", stem)
    if " - " in stem:
        a, t = stem.split(" - ", 1)
        return a.strip(), t.strip()
    return "", stem.strip()


def parent_folder_hint(abs_path):
    parts = abs_path.replace("/", "\\").split("\\")
    if "Music" in parts:
        idx = parts.index("Music")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    if len(parts) >= 2:
        return parts[-2]
    return ""


def sim(a, b):
    return SequenceMatcher(None, clean(a), clean(b)).ratio()


def classify(row):
    status = (row.get("fp_status") or "").strip()
    if status != "match":
        return (f"SKIP_{status}", "no acoustid match")
    try:
        score = float(row.get("ac_score") or 0)
    except ValueError:
        score = 0

    ac_artist = row.get("ac_artist") or ""
    ac_title = row.get("ac_title") or ""
    abs_path = row.get("abs_path") or ""

    if not ac_artist and not ac_title:
        return ("SKIP_empty_match", f"score={score:.2f} but acoustid returned empty metadata")

    full_filename = os.path.basename(abs_path)
    fn_artist, fn_title = parse_filename_hint(abs_path)
    folder_artist = parent_folder_hint(abs_path)

    # Strip noise from filename title for fairer comparison
    fn_title_clean = strip_noise(fn_title) if fn_title else strip_noise(os.path.splitext(full_filename)[0])

    # Several measures of agreement:
    title_seq = sim(ac_title, fn_title_clean) if fn_title_clean else 0
    artist_seq = sim(ac_artist, fn_artist) if fn_artist else 0
    folder_artist_seq = sim(ac_artist, folder_artist) if folder_artist else 0

    # Word overlap: how much of ac_title's words appear anywhere in filename
    title_word_in_filename = word_overlap(ac_title, full_filename)
    # And ac_artist's words in filename
    artist_word_in_filename = word_overlap(ac_artist, full_filename)

    # Best signals
    best_title = max(title_seq, title_word_in_filename)
    best_artist = max(artist_seq, folder_artist_seq, artist_word_in_filename)

    has_hint = bool(fn_artist or fn_title or folder_artist)

    # NEW classification thresholds
    # Strong agreement: both title & artist words clearly inside filename
    if score >= 0.9 and title_word_in_filename >= 0.7 and artist_word_in_filename >= 0.5:
        return ("AUTO_APPLY", f"score={score:.2f} title_words_in_fn={title_word_in_filename:.2f} artist_words_in_fn={artist_word_in_filename:.2f}")

    # Title strongly present, artist weak (common for compilation-style filenames)
    if score >= 0.9 and title_word_in_filename >= 0.7:
        return ("AUTO_APPLY_OK", f"score={score:.2f} title words match (artist={artist_word_in_filename:.2f})")

    # Artist strongly present, title decent
    if score >= 0.9 and artist_word_in_filename >= 0.7 and best_title >= 0.4:
        return ("AUTO_APPLY_OK", f"score={score:.2f} artist={artist_word_in_filename:.2f} title={best_title:.2f}")

    # Falls back to v1-style thresholds
    if score >= 0.95 and best_title >= 0.7 and best_artist >= 0.5:
        return ("AUTO_APPLY", f"score={score:.2f} title_sim={best_title:.2f} artist_sim={best_artist:.2f}")
    if score >= 0.95 and best_title >= 0.6:
        return ("AUTO_APPLY_OK", f"score={score:.2f} title_sim={best_title:.2f}")
    if score >= 0.95 and not has_hint:
        return ("REVIEW_NO_HINT", f"score={score:.2f} but no filename/folder hint")
    if score >= 0.9 and (best_title >= 0.4 or best_artist >= 0.5):
        return ("REVIEW_PARTIAL", f"score={score:.2f} title={best_title:.2f} artist={best_artist:.2f}")
    if score >= 0.9:
        return ("REVIEW_HIGH", f"score={score:.2f} title={best_title:.2f} artist={best_artist:.2f} (filename disagrees)")
    return ("REVIEW_LOW", f"score={score:.2f}")


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
        fn_a, fn_t = parse_filename_hint(r.get("abs_path", ""))
        out = dict(r)
        out["filename_artist"] = fn_a
        out["filename_title"] = fn_t
        out["folder_top"] = parent_folder_hint(r.get("abs_path", ""))
        out["bucket"] = bucket
        out["reason"] = reason
        out["user_override"] = r.get("user_override", "")  # preserve if present
        out_rows.append(out)

    cols = list(out_rows[0].keys())
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(out_rows)

    print("\n==== bucket counts (v2) ====")
    for k, n in counts.most_common():
        print(f"  {k:<22} {n:>6}")
    print(f"\nWrote: {args.out}")


if __name__ == "__main__":
    main()
