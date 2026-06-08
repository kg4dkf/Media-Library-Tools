"""Build the dedupe preview CSV with keeper/loser flags."""
import sqlite3, csv, re, time, sys

DB = "/tmp/music_snapshot.db"
OUT = "/sessions/wizardly-determined-sagan/mnt/Music Cleanup/cleanup_dedupe_to_quarantine.csv"

GENRE_BUCKETS = {
    "90s","80s","70s","60s","Oldies","Country","Pop","Rock","Classical",
    "Irish","Gaelic","Alternative","Jazz","Folk","New Age","Christmas",
    "Halloween","Hip Hop","Rap","Blues","Reggae","R&B","Soul","Indie",
    "Electronic","Dance","Metal","Punk","Latin","World","Disco",
    "Sound Effects","Video Game Sounds","WVUM","Spanish","Italian",
    "French","German","Japanese","Korean","Chinese",
}

# Tokens that hint a non-canonical / messy source path
BAD_TOKENS = [
    "torrent", "www.", "rip-",
    "compilation", "various", "playlist",
    "(va)", " va ", "mix-", "mix ",
    " top ", "top 100", "top 50",
    " best of ",
]

def path_score(rel_path):
    """Lower = preferred as keeper."""
    parts = re.split(r"[\\/]", rel_path)
    top = parts[0] if parts else ""
    score = 0
    if top in GENRE_BUCKETS:
        score += 1000
    p_lower = rel_path.lower()
    for tok in BAD_TOKENS:
        if tok in p_lower:
            score += 200
            break  # one penalty is enough
    # ".com\" / ".com/" detection without raw-string headache
    if ".com" + chr(92) in p_lower or ".com/" in p_lower:
        score += 200
    score += len(parts) * 5
    score += len(rel_path) // 20
    return score

def tag_completeness(row):
    n = 0
    for c in ("tag_artist","tag_album_artist","tag_album","tag_title",
              "tag_track","tag_disc","tag_date","tag_genre"):
        if row[c]:
            n += 1
    return n

def main():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    print("loading hash groups...")
    t0 = time.time()
    hashes = [(r["file_hash"], r["c"]) for r in con.execute(
        "SELECT file_hash, COUNT(*) c FROM files "
        "WHERE is_audio='True' AND file_hash<>'' AND size_bytes>0 "
        "GROUP BY file_hash HAVING c>1"
    )]
    print(f"  {len(hashes)} duplicate hash groups in {time.time()-t0:.1f}s")

    n_groups = n_keepers = n_losers = 0
    loser_bytes = 0
    t0 = time.time()
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "group_id","group_size","role",
            "abs_path","rel_path","size_bytes","ext","bitrate_kbps",
            "tag_artist","tag_album","tag_title","tag_track","has_embedded_art",
            "path_score","tag_complete","keeper_rel_path","quarantine_dest","action",
        ])
        Q_PREFIX = "_QUARANTINE" + chr(92) + "duplicate" + chr(92)
        for gi, (h, c) in enumerate(hashes, start=1):
            rows = list(con.execute(
                "SELECT abs_path, rel_path, size_bytes, ext, bitrate_kbps, "
                "tag_artist, tag_album, tag_title, tag_track, tag_album_artist, "
                "tag_disc, tag_date, tag_genre, has_embedded_art "
                "FROM files WHERE file_hash=? ORDER BY rel_path",
                (h,)
            ))
            if not rows:
                continue
            scored = []
            for r in rows:
                scored.append((path_score(r["rel_path"]),
                               -tag_completeness(r),
                               r["rel_path"].lower(),
                               dict(r)))
            scored.sort(key=lambda x: (x[0], x[1], x[2]))
            keeper = scored[0][3]
            n_groups += 1
            for ps, neg_tc, _, r in scored:
                if r is keeper:
                    role = "KEEP"
                    dest = ""
                    action = "KEEP"
                    n_keepers += 1
                else:
                    role = "QUARANTINE"
                    dest = Q_PREFIX + r["rel_path"]
                    action = "MOVE"
                    n_losers += 1
                    loser_bytes += r["size_bytes"]
                w.writerow([
                    gi, c, role,
                    r["abs_path"], r["rel_path"], r["size_bytes"], r["ext"],
                    r["bitrate_kbps"] or "",
                    r["tag_artist"] or "", r["tag_album"] or "",
                    r["tag_title"] or "", r["tag_track"] or "",
                    r["has_embedded_art"] or "",
                    ps, -neg_tc, keeper["rel_path"], dest, action,
                ])
    print(f"groups: {n_groups:,} | keepers: {n_keepers:,} | losers: {n_losers:,} | "
          f"losers GB: {loser_bytes/1024**3:.2f}")
    print(f"build time: {time.time()-t0:.1f}s")
    con.close()

if __name__ == "__main__":
    main()
