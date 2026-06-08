"""Patch the working DB to reflect Phase 3 quarantine moves. Fast version."""
import csv, io, os, sqlite3, time

DB = "/tmp/music_snapshot.db"
CSV_DIR = "/sessions/wizardly-determined-sagan/mnt/Music Cleanup"
CSV_NAMES = [
    "cleanup_zero_byte_to_quarantine.csv",
    "cleanup_read_errors_to_quarantine.csv",
    "cleanup_dedupe_to_quarantine.csv",
]


def read_tolerant(path):
    raw = open(path, "rb").read().replace(b"\x00", b"").decode("utf-8", errors="replace")
    return list(csv.DictReader(io.StringIO(raw)))


def main():
    t0 = time.time()
    moved_abs = set()
    for name in CSV_NAMES:
        rows = read_tolerant(os.path.join(CSV_DIR, name))
        n = 0
        for r in rows:
            if (r.get("action") or "").strip().upper() != "MOVE":
                continue
            ap = (r.get("abs_path") or "").strip()
            if ap:
                moved_abs.add(ap)
                n += 1
        print(f"  {name}: {n} MOVE rows  ({time.time()-t0:.1f}s)")
    print(f"Total unique moved paths: {len(moved_abs):,}  ({time.time()-t0:.1f}s)")

    con = sqlite3.connect(DB)
    cur = con.cursor()

    pre_files = cur.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    pre_audio = cur.execute("SELECT COUNT(*) FROM files WHERE is_audio='True'").fetchone()[0]
    print(f"\nBefore: files={pre_files:,} audio={pre_audio:,}")

    # Step 1: Identify (parent_folder, audio_stem) for moved audio so we can find LRC companions.
    print(f"\nStep 1: collecting moved audio stems...  ({time.time()-t0:.1f}s)")
    audio_stems = set()  # set of (parent_folder, stem_without_ext)
    moved_in_db = 0
    # Stream files table to avoid temp table cost
    for parent, fn, ext, abs_path in cur.execute(
        "SELECT parent_folder, filename, ext, abs_path FROM files WHERE is_audio='True'"
    ):
        if abs_path in moved_abs:
            moved_in_db += 1
            stem = fn[:-len(ext)] if ext and fn.endswith(ext) else fn
            audio_stems.add((parent, stem))
    print(f"  moved audio rows in DB: {moved_in_db:,}  ({time.time()-t0:.1f}s)")
    print(f"  unique (parent, stem) keys: {len(audio_stems):,}  ({time.time()-t0:.1f}s)")

    # Step 2: scan .lrc files, identify those that match
    print(f"\nStep 2: matching .lrc companions...  ({time.time()-t0:.1f}s)")
    lrc_to_drop = []
    for parent, fn, abs_path in cur.execute(
        "SELECT parent_folder, filename, abs_path FROM files WHERE ext='.lrc'"
    ):
        stem = fn[:-4] if fn.endswith(".lrc") else fn
        if (parent, stem) in audio_stems:
            lrc_to_drop.append(abs_path)
    print(f"  .lrc to drop: {len(lrc_to_drop):,}  ({time.time()-t0:.1f}s)")

    # Step 3: delete - chunk the moved set to avoid huge IN-clause issues
    print(f"\nStep 3: deleting...  ({time.time()-t0:.1f}s)")
    cur.execute("BEGIN")
    moved_list = list(moved_abs)
    CHUNK = 500
    deleted_audio = 0
    for i in range(0, len(moved_list), CHUNK):
        chunk = moved_list[i:i+CHUNK]
        placeholders = ",".join("?" * len(chunk))
        cur.execute(f"DELETE FROM files WHERE abs_path IN ({placeholders})", chunk)
        deleted_audio += cur.rowcount
    print(f"  deleted moved audio rows: {deleted_audio:,}  ({time.time()-t0:.1f}s)")

    # Delete LRC
    deleted_lrc = 0
    for i in range(0, len(lrc_to_drop), CHUNK):
        chunk = lrc_to_drop[i:i+CHUNK]
        placeholders = ",".join("?" * len(chunk))
        cur.execute(f"DELETE FROM files WHERE abs_path IN ({placeholders})", chunk)
        deleted_lrc += cur.rowcount
    print(f"  deleted .lrc rows       : {deleted_lrc:,}  ({time.time()-t0:.1f}s)")

    con.commit()

    post_files = cur.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    post_audio = cur.execute("SELECT COUNT(*) FROM files WHERE is_audio='True'").fetchone()[0]
    print(f"\nAfter:  files={post_files:,} audio={post_audio:,}")
    print(f"  total time: {time.time()-t0:.1f}s")
    con.close()


if __name__ == "__main__":
    main()
