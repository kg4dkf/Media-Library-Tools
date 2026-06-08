#!/usr/bin/env python3
"""
Music Collection Snapshot Tool
==============================
Generates a comprehensive CSV snapshot of a music library BEFORE reorganization.
Captures: folder structure, file paths/sizes/dates, MD5 hashes, codec/bitrate/
duration/channels, and ID3/Vorbis/MP4/ASF tags (artist, album, title, track,
disc, date, genre, composer, album_artist, embedded-art presence).

Why a snapshot first? Once you start moving/renaming/retagging 115k files,
this CSV is the only way to:
  - prove what existed where (forensic / undo reference)
  - find duplicates by hash
  - find tag/filename mismatches before fixing them
  - diff "before" vs "after" cleanup

USAGE
-----
    pip install mutagen
    python music_snapshot.py E:\\Music
    python music_snapshot.py E:\\Music --output D:\\snapshots
    python music_snapshot.py E:\\Music --no-hash         (skip MD5 - much faster)
    python music_snapshot.py E:\\Music --hash sha256     (stronger hash, slower)
    python music_snapshot.py E:\\Music --audio-only      (skip non-audio files)
    python music_snapshot.py E:\\Music --resume          (skip files already in
                                                          a previous CSV in the
                                                          output dir)

Hashing 115k MP3s on a spinning disk can take a few hours. On SSD, well under
an hour. Use --no-hash for a quick first pass; rerun later with hashing for the
authoritative snapshot.
"""
import argparse
import csv
import hashlib
import os
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    from mutagen import File as MutagenFile
except ImportError:
    sys.stderr.write(
        "ERROR: the 'mutagen' library is required.\n"
        "Install it with:\n"
        "    pip install mutagen\n"
    )
    sys.exit(2)

# Audio extensions we'll attempt to read tags from. Mutagen handles all of these.
AUDIO_EXTS = {
    ".mp3", ".flac", ".m4a", ".m4b", ".m4p", ".mp4", ".aac",
    ".ogg", ".oga", ".opus", ".spx",
    ".wma", ".asf",
    ".wav", ".wave", ".aif", ".aiff", ".aifc",
    ".ape", ".wv", ".mpc", ".tta", ".tak",
    ".dsf", ".dff",
    ".alac", ".ac3", ".dts",
}

FILE_COLS = [
    "rel_path", "abs_path", "parent_folder", "folder_depth",
    "filename", "ext", "size_bytes", "mtime",
    "is_audio", "audio_codec", "bitrate_kbps", "duration_sec",
    "sample_rate", "channels",
    "tag_artist", "tag_album_artist", "tag_album", "tag_title",
    "tag_track", "tag_disc", "tag_date", "tag_genre", "tag_composer",
    "has_embedded_art", "file_hash", "error",
]

FOLDER_COLS = [
    "rel_path", "abs_path", "parent_folder", "folder_name", "depth",
    "file_count", "audio_file_count", "subfolder_count",
    "total_size_bytes", "distinct_extensions",
]


def long_path(p: Path) -> str:
    """On Windows, prepend \\?\ to bypass MAX_PATH (260 char) limit."""
    s = str(p)
    if os.name == "nt" and len(s) > 240 and not s.startswith("\\\\?\\"):
        if s.startswith("\\\\"):
            return "\\\\?\\UNC\\" + s.lstrip("\\")
        return "\\\\?\\" + s
    return s


def hash_file(path: Path, algo: str = "md5", chunk: int = 1 << 20) -> str:
    """Hash a file's contents. Returns hex digest, or '' on failure."""
    h = hashlib.new(algo)
    try:
        with open(long_path(path), "rb") as f:
            while True:
                block = f.read(chunk)
                if not block:
                    break
                h.update(block)
        return h.hexdigest()
    except Exception:
        return ""


def first_str(value) -> str:
    """Mutagen tag values are usually lists; pull the first as a clean string."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        if not value:
            return ""
        value = value[0]
    return str(value).strip()


def read_audio(path: Path) -> dict:
    """
    Returns codec/bitrate/duration/tag fields for an audio file.
    Returns {'error': ...} on failure, {} if mutagen can't identify the file.
    """
    out = {}
    try:
        # easy=True normalizes tag keys across formats (artist, album, title,
        # tracknumber, discnumber, date, genre, composer, albumartist).
        easy = MutagenFile(long_path(path), easy=True)
        if easy is None:
            # Not recognized by mutagen even though extension looked audio.
            return {"error": "mutagen: unrecognized format"}

        info = getattr(easy, "info", None)
        if info is not None:
            out["audio_codec"] = type(easy).__name__
            br = getattr(info, "bitrate", None)
            if br:
                out["bitrate_kbps"] = int(br / 1000)
            length = getattr(info, "length", None)
            if length is not None:
                out["duration_sec"] = round(float(length), 2)
            sr = getattr(info, "sample_rate", None)
            if sr:
                out["sample_rate"] = sr
            ch = getattr(info, "channels", None)
            if ch:
                out["channels"] = ch

        tags = easy.tags
        if tags:
            out["tag_artist"] = first_str(tags.get("artist"))
            out["tag_album_artist"] = first_str(tags.get("albumartist"))
            out["tag_album"] = first_str(tags.get("album"))
            out["tag_title"] = first_str(tags.get("title"))
            out["tag_track"] = first_str(tags.get("tracknumber"))
            out["tag_disc"] = first_str(tags.get("discnumber"))
            out["tag_date"] = first_str(tags.get("date")) or first_str(tags.get("year"))
            out["tag_genre"] = first_str(tags.get("genre"))
            out["tag_composer"] = first_str(tags.get("composer"))

        # Embedded art detection requires the non-easy view (raw frames).
        try:
            raw = MutagenFile(long_path(path))
            has_art = False
            if raw is not None:
                if getattr(raw, "pictures", None):  # FLAC
                    has_art = bool(raw.pictures)
                elif raw.tags is not None:
                    keys = list(raw.tags.keys()) if hasattr(raw.tags, "keys") else []
                    for k in keys:
                        ks = str(k)
                        if ks.startswith("APIC") or ks == "covr" or ks == "WM/Picture":
                            has_art = True
                            break
            out["has_embedded_art"] = has_art
        except Exception:
            out["has_embedded_art"] = ""
        return out
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def load_resume_set(out_dir: Path):
    """Collect rel_paths from any prior music_snapshot_files_*.csv in out_dir."""
    seen = set()
    prior = sorted(out_dir.glob("music_snapshot_files_*.csv"))
    for p in prior:
        try:
            with open(p, "r", encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    rp = row.get("rel_path", "").strip()
                    if rp:
                        seen.add(rp)
        except Exception:
            pass
    return seen


def main():
    ap = argparse.ArgumentParser(
        description="Snapshot a music collection to CSV before reorganization.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("root", help="Root directory to scan (e.g., E:\\Music)")
    ap.add_argument("--output", "-o", default=".",
                    help="Output directory for CSVs (default: current dir)")
    ap.add_argument("--no-hash", action="store_true",
                    help="Skip file hashing (much faster)")
    ap.add_argument("--hash", default="md5",
                    choices=["md5", "sha1", "sha256"],
                    help="Hash algorithm (default: md5)")
    ap.add_argument("--audio-only", action="store_true",
                    help="Only catalog audio files (skip .jpg, .nfo, .m3u, etc.)")
    ap.add_argument("--resume", action="store_true",
                    help="Skip files already present in any prior snapshot CSV "
                         "in the output directory")
    ap.add_argument("--follow-symlinks", action="store_true",
                    help="Follow symbolic links during walk (default: don't)")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    if not root.is_dir():
        sys.stderr.write(f"ERROR: not a directory: {root}\n")
        sys.exit(1)

    out_dir = Path(args.output).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    resume_seen = load_resume_set(out_dir) if args.resume else set()

    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    files_csv = out_dir / f"music_snapshot_files_{ts}.csv"
    folders_csv = out_dir / f"music_snapshot_folders_{ts}.csv"
    summary_txt = out_dir / f"music_snapshot_summary_{ts}.txt"

    print(f"Scanning : {root}")
    print(f"Output   : {out_dir}")
    print(f"Hashing  : {'OFF' if args.no_hash else args.hash}")
    print(f"Audio-only: {args.audio_only}")
    if args.resume:
        print(f"Resume   : skipping {len(resume_seen):,} files already snapshotted")
    print()

    folder_stats = {}
    total_files = total_audio = total_errors = 0
    total_size = 0
    skipped_resume = 0
    start = time.time()
    last_print = start

    with open(files_csv, "w", newline="", encoding="utf-8", errors="replace") as ff:
        writer = csv.DictWriter(ff, fieldnames=FILE_COLS, extrasaction="ignore")
        writer.writeheader()
        ff.flush()

        for dirpath, dirnames, filenames in os.walk(
            root, followlinks=args.follow_symlinks
        ):
            dp = Path(dirpath)
            try:
                rel_dir = dp.relative_to(root)
            except ValueError:
                rel_dir = Path(".")
            depth = 0 if str(rel_dir) == "." else len(rel_dir.parts)

            stat = {
                "rel_path": str(rel_dir),
                "abs_path": str(dp),
                "parent_folder": str(rel_dir.parent) if str(rel_dir) != "." else "",
                "folder_name": dp.name,
                "depth": depth,
                "file_count": 0,
                "audio_file_count": 0,
                "subfolder_count": len(dirnames),
                "total_size_bytes": 0,
                "_exts": set(),
            }

            for fn in filenames:
                fp = dp / fn
                rel = str(fp.relative_to(root))
                if args.resume and rel in resume_seen:
                    skipped_resume += 1
                    continue
                try:
                    st = fp.stat()
                except Exception as e:
                    total_errors += 1
                    writer.writerow({
                        "rel_path": rel, "abs_path": str(fp),
                        "parent_folder": str(rel_dir),
                        "folder_depth": depth, "filename": fn,
                        "ext": fp.suffix.lower(), "is_audio": False,
                        "error": f"stat: {type(e).__name__}: {e}",
                    })
                    continue

                ext = fp.suffix.lower()
                is_audio = ext in AUDIO_EXTS
                if args.audio_only and not is_audio:
                    continue

                stat["file_count"] += 1
                stat["total_size_bytes"] += st.st_size
                stat["_exts"].add(ext)
                if is_audio:
                    stat["audio_file_count"] += 1

                row = {
                    "rel_path": rel,
                    "abs_path": str(fp),
                    "parent_folder": str(rel_dir),
                    "folder_depth": depth,
                    "filename": fn,
                    "ext": ext,
                    "size_bytes": st.st_size,
                    "mtime": datetime.fromtimestamp(
                        st.st_mtime
                    ).isoformat(timespec="seconds"),
                    "is_audio": is_audio,
                }

                if is_audio:
                    meta = read_audio(fp)
                    if "error" in meta:
                        total_errors += 1
                        row["error"] = meta["error"]
                    row.update({k: v for k, v in meta.items() if k != "error"})

                if not args.no_hash:
                    row["file_hash"] = hash_file(fp, args.hash)

                try:
                    writer.writerow(row)
                except Exception as e:
                    # As last resort, scrub unencodable chars and retry.
                    row = {k: (str(v).encode("utf-8", "replace").decode("utf-8")
                               if isinstance(v, str) else v)
                           for k, v in row.items()}
                    row["error"] = (row.get("error", "") + f" | csv: {e}").strip(" |")
                    writer.writerow(row)

                total_files += 1
                total_size += st.st_size
                if is_audio:
                    total_audio += 1

                now = time.time()
                if now - last_print >= 2.0:
                    rate = total_files / max(now - start, 0.001)
                    short = dp.name[:60]
                    sys.stdout.write(
                        f"  {total_files:>9,} files | {total_audio:>9,} audio | "
                        f"{total_size/(1024**3):>7.2f} GB | "
                        f"{rate:>5.0f} f/s | in {short}\n"
                    )
                    sys.stdout.flush()
                    ff.flush()
                    last_print = now

            stat["distinct_extensions"] = ",".join(sorted(stat["_exts"]))
            stat.pop("_exts", None)
            folder_stats[str(rel_dir)] = stat

    with open(folders_csv, "w", newline="", encoding="utf-8", errors="replace") as ff:
        w = csv.DictWriter(ff, fieldnames=FOLDER_COLS, extrasaction="ignore")
        w.writeheader()
        for s in folder_stats.values():
            w.writerow(s)

    elapsed = time.time() - start
    summary = (
        "Music Collection Snapshot Summary\n"
        "=================================\n"
        f"Generated:        {datetime.now().isoformat(timespec='seconds')}\n"
        f"Root scanned:     {root}\n"
        f"Total files:      {total_files:,}\n"
        f"Audio files:      {total_audio:,}\n"
        f"Total folders:    {len(folder_stats):,}\n"
        f"Total size:       {total_size/(1024**3):.2f} GB ({total_size:,} bytes)\n"
        f"Read errors:      {total_errors:,}\n"
        f"Skipped (resume): {skipped_resume:,}\n"
        f"Elapsed:          {elapsed:.1f}s ({elapsed/60:.1f} min)\n"
        f"Hash algorithm:   {'(skipped)' if args.no_hash else args.hash}\n"
        "\n"
        "Output files:\n"
        f"  {files_csv}\n"
        f"  {folders_csv}\n"
        f"  {summary_txt}\n"
    )
    print()
    print(summary)
    summary_txt.write_text(summary, encoding="utf-8")


if __name__ == "__main__":
    main()
