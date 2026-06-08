"""Patch DB by rebuilding the files table from scratch in Python.
Faster than UPDATE-in-place when changing many rows.
"""
import sqlite3, time

DB = "/tmp/music_snapshot.db"
LOG = "/sessions/wizardly-determined-sagan/mnt/Music Cleanup/reversal_log.tsv"
MUSIC_ROOT = "E:\\Music"


def norm(p):
    s = p.replace("/", "\\")
    while "\\\\" in s and not s.startswith("\\\\?\\"):
        s = s.replace("\\\\", "\\")
    return s


def rel_under_root(abs_path, root):
    s = norm(abs_path)
    r = norm(root).rstrip("\\")
    if s.lower().startswith(r.lower() + "\\"):
        return s[len(r) + 1:]
    return s


def main():
    t0 = time.time()
    rename_map = {}
    drop_set = set()
    counts = {}
    print("reading log...")
    with open(LOG, "r", encoding="utf-8", errors="replace") as f:
        for ln in f:
            parts = ln.rstrip("\n").split("\t")
            if len(parts) < 3:
                continue
            new_p, orig_p, reason = parts[0], parts[1], parts[2]
            counts[reason] = counts.get(reason, 0) + 1
            o = norm(orig_p)
            if reason in ("rename", "rename_lrc"):
                rename_map[o] = norm(new_p)
            else:
                drop_set.add(o)
    print(f"  log read in {time.time()-t0:.1f}s. {sum(counts.values()):,} entries")

    con = sqlite3.connect(DB)
    cur = con.cursor()

    # Read all rows into Python
    print("loading files table...")
    cols = [d[1] for d in cur.execute("PRAGMA table_info(files)")]
    abs_idx = cols.index("abs_path")
    relp_idx = cols.index("rel_path")
    pf_idx = cols.index("parent_folder")
    fn_idx = cols.index("filename")
    rows = list(cur.execute(f"SELECT {','.join(cols)} FROM files"))
    print(f"  loaded {len(rows):,} rows in {time.time()-t0:.1f}s")

    # Transform in Python
    new_rows = []
    n_drop = n_renamed = 0
    for row in rows:
        ap = row[abs_idx]
        if ap in drop_set:
            n_drop += 1
            continue
        if ap in rename_map:
            new_abs = rename_map[ap]
            rel = rel_under_root(new_abs, MUSIC_ROOT)
            parent = "."
            fn = rel
            if "\\" in rel:
                parent, fn = rel.rsplit("\\", 1)
            row = list(row)
            row[abs_idx] = new_abs
            row[relp_idx] = rel
            row[pf_idx] = parent
            row[fn_idx] = fn
            new_rows.append(row)
            n_renamed += 1
        else:
            new_rows.append(row)
    print(f"  drops: {n_drop:,}  renames: {n_renamed:,}  surviving: {len(new_rows):,}")
    print(f"  transform in {time.time()-t0:.1f}s")

    # Drop and recreate the files table
    print("rebuilding files table...")
    cur.execute("DROP TABLE IF EXISTS files_new")
    cur.execute("""CREATE TABLE files_new (
      rel_path TEXT, abs_path TEXT, parent_folder TEXT, folder_depth INT,
      filename TEXT, ext TEXT, size_bytes INT, mtime TEXT,
      is_audio TEXT, audio_codec TEXT, bitrate_kbps INT, duration_sec REAL,
      sample_rate INT, channels INT,
      tag_artist TEXT, tag_album_artist TEXT, tag_album TEXT, tag_title TEXT,
      tag_track TEXT, tag_disc TEXT, tag_date TEXT, tag_genre TEXT, tag_composer TEXT,
      has_embedded_art TEXT, file_hash TEXT, error TEXT)""")
    placeholders = ",".join("?" * len(cols))
    # files_new has same column order
    cur.executemany(f"INSERT INTO files_new VALUES ({placeholders})", new_rows)
    cur.execute("DROP TABLE files")
    cur.execute("ALTER TABLE files_new RENAME TO files")
    cur.execute("CREATE INDEX idx_files_hash ON files(file_hash)")
    cur.execute("CREATE INDEX idx_files_parent ON files(parent_folder)")
    cur.execute("CREATE INDEX idx_files_artist ON files(tag_artist)")
    cur.execute("CREATE INDEX idx_files_album ON files(tag_album)")
    cur.execute("CREATE INDEX idx_files_audio ON files(is_audio)")
    con.commit()
    print(f"  rebuild done in {time.time()-t0:.1f}s")

    post_audio = cur.execute("SELECT COUNT(*) FROM files WHERE is_audio='True'").fetchone()[0]
    print(f"\nAfter: files={len(new_rows):,} audio={post_audio:,}")

    # Straggler profile
    LETTER = [chr(c) + "\\" for c in range(ord("A"), ord("Z") + 1)] + ["0-9\\"]
    BUCKET = LETTER + [
        "Compilations\\", "Soundtracks\\", "Radio\\", "Sound Effects\\",
        "Video Game Soundtracks\\", "Audiobooks\\", "Comedy\\",
        "_Unknown", "_Review", "_QUARANTINE\\",
    ]
    in_new = 0
    stragglers = 0
    by_top = {}
    for r in cur.execute("SELECT rel_path FROM files WHERE is_audio='True'"):
        rp = r[0] or ""
        if any(rp.startswith(b) for b in BUCKET):
            in_new += 1
        else:
            stragglers += 1
            top1 = rp.split("\\", 1)[0] if "\\" in rp else rp
            by_top[top1] = by_top.get(top1, 0) + 1
    print(f"\naudio in new tree : {in_new:,}")
    print(f"stragglers        : {stragglers:,}")
    if by_top:
        print("top straggler folders:")
        for k, v in sorted(by_top.items(), key=lambda x: -x[1])[:15]:
            print(f"  {v:>5}  {k[:80]}")
    con.close()
    print(f"\ntotal: {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
