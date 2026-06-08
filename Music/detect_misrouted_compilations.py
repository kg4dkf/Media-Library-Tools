#!/usr/bin/env python3
"""
Phase 6 helper: detect compilation tracks that got misrouted into artist
folders during Phase 5 rename.

Signals (weighted):
  - Album name contains compilation-publisher tokens (Time Life, K-Tel,
    Rhino Hits, Now That's What I Call, Rolling Stone Magazine, etc.)
  - Album name contains 'Various Artists', 'VA', '(Various)'
  - Album name is just a year ('2013', '2010')
  - Album name is 'Unknown' or '(Unknown)'
  - Album name contains 'Best of', 'Sounds Of The', 'Hits Of The',
    'Greatest Songs', 'Top 100', etc.
  - SAME album name appears under 3+ different artists (strong signal)
  - Artist name was the original folder name (e.g., 'Sesame Street Songs',
    '90s Rock Hits') — usually means the folder dissolved badly

Score >= 0.6 -> flagged as likely-misrouted compilation.

Output: misrouted_compilations.csv with suggested action for each.
"""
import argparse, csv, re
from collections import Counter, defaultdict


# Tokens that strongly suggest a compilation
COMP_PUBLISHERS = [
    "time life", "k-tel", "ktel", "rhino", "now that's what i call",
    "rolling stone magazine", "rolling stone", "vh1", "vh-1",
    "mtv unplugged", "mtv", "billboard top", "billboard hot",
    "absolute music", "kuschelrock", "bravo hits",
]

COMP_THEMES = [
    "best of", "sounds of the", "songs of the", "hits of the",
    "greatest songs", "greatest hits of", "essential opera",
    "top 100", "top 50", "top 40", "top hits",
    "ultimate 70s", "ultimate 80s", "ultimate 90s",
    "classic country", "classic rock", "malt shop memories",
    "the best", "fm rock", "guitar power",
    "compilation", "various artists", "(various)",
    "party", "nostalgia pop", "country romance",
]

# Filename patterns that suggest compilation
COMP_PATTERNS = [
    re.compile(r"^\s*\(?(19|20)\d{2}\)?\s*$"),  # just a year
    re.compile(r"^\s*\(?unknown\)?\s*$", re.I),
    re.compile(r"^\s*\(?various.*artists?\)?", re.I),
    re.compile(r"^\s*va\s*-\s*", re.I),
    re.compile(r"\bdisc\s*\d+\b", re.I),  # often comps with multiple discs
]


def score_album(album_name):
    """Return (score, reasons) where score is 0..1, reasons is a list of strings."""
    if not album_name:
        return (0, [])
    a_lower = album_name.lower()
    score = 0.0
    reasons = []

    for pub in COMP_PUBLISHERS:
        if pub in a_lower:
            score += 0.6
            reasons.append(f"publisher: '{pub}'")
            break

    for theme in COMP_THEMES:
        if theme in a_lower:
            score += 0.4
            reasons.append(f"theme: '{theme}'")
            break

    for pat in COMP_PATTERNS:
        if pat.search(album_name):
            score += 0.5
            reasons.append(f"pattern: {pat.pattern}")
            break

    return (min(score, 1.0), reasons)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", default="misrouted_compilations.csv")
    ap.add_argument("--threshold", type=float, default=0.5)
    args = ap.parse_args()

    rows = list(csv.DictReader(open(args.inp, encoding="utf-8")))
    print(f"Loaded {len(rows):,} albums")

    # Pass 1: text-pattern scoring
    scored = []
    for r in rows:
        if r["bucket"] != "ARTIST":
            continue
        score, reasons = score_album(r["album"])
        scored.append({"row": r, "score": score, "reasons": reasons})

    # Pass 2: cross-artist signal — same album under many artists
    by_album = defaultdict(set)
    for s in scored:
        r = s["row"]
        # Normalize album name slightly for matching
        key = (r["album"].lower().strip(), r["year"])
        by_album[key].add(r["artist"])

    # If 3+ distinct artists share an album, that's a strong compilation signal
    for s in scored:
        r = s["row"]
        key = (r["album"].lower().strip(), r["year"])
        n_artists = len(by_album[key])
        if n_artists >= 3:
            s["score"] = min(1.0, s["score"] + 0.5)
            s["reasons"].append(f"shared_by_{n_artists}_artists")

    # Filter by threshold
    flagged = [s for s in scored if s["score"] >= args.threshold]
    print(f"Flagged as likely-misrouted compilation (>= {args.threshold}): {len(flagged):,}")

    # Reasons distribution
    reason_counts = Counter()
    for s in flagged:
        for rea in s["reasons"]:
            reason_counts[rea.split(":")[0]] += 1
    print(f"\nTop reason categories:")
    for k, n in reason_counts.most_common(10):
        print(f"  {k:<22} {n:>5}")

    # Output CSV
    out_rows = []
    for s in sorted(flagged, key=lambda x: -x["score"]):
        r = s["row"]
        out_rows.append({
            "abs_folder_path": r["abs_folder_path"],
            "artist": r["artist"],
            "album": r["album"],
            "year": r["year"],
            "file_count": r["file_count"],
            "score": round(s["score"], 2),
            "reasons": "; ".join(s["reasons"]),
            "proposed_action": "MOVE_TO_COMPILATIONS",
            "user_override": "",
        })
    cols = list(out_rows[0].keys()) if out_rows else ["abs_folder_path"]
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(out_rows)
    print(f"\nWrote: {args.out}")
    print(f"\nReview the CSV. Mark user_override = 'KEEP' for any false positives.")
    print(f"To execute: run a separate remediation that re-routes flagged albums to Music\\Compilations\\")


if __name__ == "__main__":
    main()
