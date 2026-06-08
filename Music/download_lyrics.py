#!/usr/bin/env python3
"""
Bulk lyrics downloader using LRCLIB (https://lrclib.net) — free, open-source,
no API key required.

For every audio file in --music-root that doesn't already have an .lrc
sibling, queries LRCLIB by artist + title (+ optional album + duration) and
writes a sidecar .lrc with synced lyrics. Falls back to plain (.txt-style
unsynced) lyrics in the .lrc file if no synced version is available.

USAGE
-----
  pip install mutagen requests
  python download_lyrics.py --music-root E:\\Music
  python download_lyrics.py --music-root E:\\Music --dry-run    # just count
  python download_lyrics.py --music-root E:\\Music --resume     # skip files
                                                                  # already in
                                                                  # progress log
  python download_lyrics.py --music-root E:\\Music --rate 0.25   # 4 req/sec
                                                                  # (default 5)

Behavior
--------
- Skips _QUARANTINE entirely.
- Skips files that already have a sidecar .lrc.
- Prefers synced lyrics; if none, writes plain lyrics with a single LRC
  timestamp [00:00.00] so MP3 players still pick it up as text lyrics.
- Logs every query to lyrics_run_<ts>.log so you can resume after Ctrl-C.
- Be polite — default rate limit is 5 requests/second.

LRCLIB API
----------
GET https://lrclib.net/api/get?
   artist_name=...&track_name=...&album_name=...&duration=...
Returns JSON {plainLyrics, syncedLyrics, ...} on hit, 404 on miss.
"""
import argparse, json, os, sys, time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

try:
    from mutagen import File as MutagenFile
except ImportError:
    sys.exit("ERROR: pip install mutagen")
try:
    import requests
except ImportError:
    sys.exit("ERROR: pip install requests")

LRCLIB_URL = "https://lrclib.net/api/get"
USER_AGENT = "MusicCleanupLyrics/1.0 (https://github.com/example)"
AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".m4b", ".mp4", ".ogg", ".opus",
              ".wma", ".wav", ".aac"}


def long_path(p):
    s = str(p)
    if os.name == "nt":
        s = s.replace("/", "\\")
        if len(s) > 240 and not s.startswith("\\\\?\\"):
            return "\\\\?\\" + s
    return s


def get_tags_and_duration(path):
    """Return (artist, album, title, duration_sec) from audio file."""
    try:
        f = MutagenFile(str(path), easy=True)
        if not f:
            return ("", "", "", 0)
        t = f.tags or {}
        artist = (t.get("artist", [""]) or [""])[0]
        album = (t.get("album", [""]) or [""])[0]
        title = (t.get("title", [""]) or [""])[0]
        dur = round(getattr(f.info, "length", 0)) if hasattr(f, "info") else 0
        return (artist, album, title, dur)
    except Exception:
        return ("", "", "", 0)


def query_lrclib(session, artist, title, album="", duration=0, timeout=10):
    """Returns dict from LRCLIB or None on miss."""
    params = {"artist_name": artist, "track_name": title}
    if album:
        params["album_name"] = album
    if duration:
        params["duration"] = str(duration)
    try:
        resp = session.get(LRCLIB_URL, params=params, timeout=timeout,
                           headers={"User-Agent": USER_AGENT, "Lrclib-Client": USER_AGENT})
        if resp.status_code == 200:
            return resp.json()
        return None
    except Exception as e:
        return {"_error": str(e)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--music-root", required=True)
    ap.add_argument("--dry-run", action="store_true", help="Only count what's missing, don't query")
    ap.add_argument("--rate", type=float, default=0.2,
                    help="Min seconds between requests (default 0.2 = 5 req/s)")
    ap.add_argument("--resume", action="store_true",
                    help="Skip files already in any prior lyrics_run_*.log")
    ap.add_argument("--limit", type=int, default=0,
                    help="Cap total queries (testing)")
    ap.add_argument("--prefer-plain", action="store_true",
                    help="Also write plain lyrics if no synced version found")
    args = ap.parse_args()

    music_root = Path(args.music_root).resolve()
    if not music_root.is_dir():
        sys.exit(f"ERROR: {music_root} not a directory")

    # Find candidate audio files
    print("Scanning for audio without .lrc sibling...")
    t0 = time.time()
    candidates = []
    for dirpath, dirnames, filenames in os.walk(music_root):
        # Skip _QUARANTINE
        dirnames[:] = [d for d in dirnames if d != "_QUARANTINE"]
        existing_stems = set()
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext == ".lrc":
                existing_stems.add(fn[:-4].lower())
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in AUDIO_EXTS:
                continue
            stem = fn[: -len(ext)]
            if stem.lower() in existing_stems:
                continue
            candidates.append(Path(dirpath) / fn)
    print(f"Found {len(candidates):,} audio files without .lrc in {time.time()-t0:.1f}s")

    if args.dry_run:
        # Estimate runtime
        est = len(candidates) * max(args.rate, 0.05)
        print(f"\n(dry-run) estimated runtime at {1/args.rate:.1f} req/s: "
              f"{est/60:.1f} min ({est/3600:.1f} h)")
        return

    # Resume handling — read prior log files
    seen_paths = set()
    if args.resume:
        for f in music_root.glob("lyrics_run_*.log"):
            try:
                with open(f, "r", encoding="utf-8") as lf:
                    for ln in lf:
                        if "\t" in ln:
                            parts = ln.split("\t")
                            if len(parts) >= 2:
                                seen_paths.add(parts[1].strip())
            except Exception:
                pass
        print(f"Resume mode: {len(seen_paths):,} files already attempted")

    if args.limit:
        candidates = candidates[: args.limit]

    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    log_path = music_root / f"lyrics_run_{ts}.log"
    print(f"Run log: {log_path}\n")

    counts = {"queried": 0, "found_synced": 0, "found_plain": 0,
              "miss": 0, "skipped_no_artist_title": 0,
              "skipped_resume": 0, "error": 0}
    bytes_written = 0
    started = time.time()
    last_print = started

    session = requests.Session()
    last_req = 0.0

    with open(log_path, "w", encoding="utf-8") as logf:
        logf.write(f"# lyrics run {ts}\n# music_root={music_root}\n# total candidates={len(candidates)}\n\n")
        for i, path in enumerate(candidates, 1):
            spath = str(path)
            if spath in seen_paths:
                counts["skipped_resume"] += 1
                continue

            artist, album, title, duration = get_tags_and_duration(path)
            if not artist or not title:
                counts["skipped_no_artist_title"] += 1
                logf.write(f"skipped\t{spath}\tno artist or title in tags\n")
                continue

            # Throttle
            now = time.time()
            wait = args.rate - (now - last_req)
            if wait > 0:
                time.sleep(wait)
            last_req = time.time()

            counts["queried"] += 1
            data = query_lrclib(session, artist, title, album, duration)
            if data is None:
                counts["miss"] += 1
                logf.write(f"miss\t{spath}\t{artist} - {title}\n")
            elif "_error" in data:
                counts["error"] += 1
                logf.write(f"error\t{spath}\t{data['_error']}\n")
            else:
                synced = (data.get("syncedLyrics") or "").strip()
                plain = (data.get("plainLyrics") or "").strip()
                lrc_text = None
                kind = ""
                if synced:
                    lrc_text = synced
                    kind = "synced"
                    counts["found_synced"] += 1
                elif plain and args.prefer_plain:
                    # Wrap plain lyrics with a single timestamp
                    lrc_text = "[00:00.00] " + plain.replace("\n", "\n[00:00.00] ")
                    kind = "plain"
                    counts["found_plain"] += 1
                else:
                    counts["miss"] += 1
                    logf.write(f"miss-no-synced\t{spath}\t{artist} - {title}\n")
                if lrc_text:
                    lrc_path = path.with_suffix(".lrc")
                    try:
                        with open(long_path(lrc_path), "w", encoding="utf-8") as f:
                            f.write(lrc_text)
                        bytes_written += len(lrc_text.encode("utf-8"))
                        logf.write(f"ok-{kind}\t{spath}\t{len(lrc_text)} chars\n")
                    except Exception as e:
                        counts["error"] += 1
                        logf.write(f"write-error\t{spath}\t{e}\n")

            now = time.time()
            if now - last_print >= 2.0 or i == len(candidates):
                rate = counts["queried"] / max(now - started, 0.001)
                pct = 100.0 * i / len(candidates)
                eta = (len(candidates) - i) * args.rate / 60
                sys.stdout.write(
                    f"\r  {i:>6,}/{len(candidates):,} ({pct:5.1f}%)  "
                    f"hits={counts['found_synced']+counts['found_plain']:,}  "
                    f"miss={counts['miss']:,}  errs={counts['error']:,}  "
                    f"{rate:.1f} q/s  ETA {eta:.0f} min"
                )
                sys.stdout.flush()
                last_print = now

    sys.stdout.write("\n")
    elapsed = time.time() - started
    print()
    print("==== summary ====")
    for k, v in counts.items():
        print(f"  {k:<24} {v:>6,}")
    print(f"  bytes_written          {bytes_written/1024:.1f} KB")
    print(f"  elapsed                {elapsed/60:.1f} min")
    print(f"  log:                   {log_path}")


if __name__ == "__main__":
    main()
