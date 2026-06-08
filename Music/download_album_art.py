#!/usr/bin/env python3
"""
Album-art downloader. For every album folder under E:\\Music that doesn't
already have a folder.jpg / cover.jpg, query:

  1. iTunes Search API (fast, free, no key, decent 600x600)
  2. Cover Art Archive via MusicBrainz (fallback, often higher quality)

On success, writes folder.jpg next to the audio tracks. Does NOT embed into
ID3 frames in this script (we'll do that in a separate tag-fixup phase).

Designed to run concurrently with the lyrics downloader — touches different
files entirely.

USAGE
-----
   pip install mutagen requests
   python download_album_art.py --music-root E:\\Music --csv-dir "H:\\Music Cleanup"
   python download_album_art.py --music-root E:\\Music --csv-dir "H:\\Music Cleanup" --resume
   python download_album_art.py ... --workers 8
   python download_album_art.py ... --no-coverart    (skip CAA fallback, iTunes only)
"""
import argparse, json, os, re, sys, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

try:
    from mutagen import File as MutagenFile
except ImportError:
    sys.exit("pip install mutagen")
try:
    import requests
except ImportError:
    sys.exit("pip install requests")

ITUNES_SEARCH = "https://itunes.apple.com/search"
MB_API = "https://musicbrainz.org/ws/2"
CAA_BASE = "https://coverartarchive.org/release"
USER_AGENT = "MusicCleanupArt/1.0 ()"

AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".m4b", ".mp4", ".ogg", ".opus",
              ".wma", ".wav", ".aac"}
ART_FILENAMES = {"folder.jpg", "cover.jpg", "albumart.jpg", "album.jpg",
                 "folder.jpeg", "cover.jpeg", "front.jpg",
                 "albumartsmall.jpg"}


_local = threading.local()


def session():
    if not hasattr(_local, "s"):
        s = requests.Session()
        s.headers.update({"User-Agent": USER_AGENT})
        _local.s = s
    return _local.s


def long_path(p):
    s = str(p)
    if os.name == "nt" and len(s) > 240 and not s.startswith("\\\\?\\"):
        return "\\\\?\\" + s.replace("/", "\\")
    return s


def folder_has_art(folder):
    try:
        entries = os.listdir(long_path(folder))
    except Exception:
        return False
    for fn in entries:
        if fn.lower() in ART_FILENAMES:
            return True
    return False


def folder_path_fallback(folder):
    """Infer (artist, album, year) from the new-tree folder structure:
       ...\\Music\\<Letter>\\<Artist>\\<Year - Album>\\<file>.ext
       or  ...\\Music\\Soundtracks\\<Year - Album>\\...
       or  ...\\Music\\Compilations\\<Year - Album>\\...
    Returns ('', '', '') if not in a recognizable shape."""
    parts = folder.replace("/", "\\").split("\\")
    if len(parts) < 2:
        return ("", "", "")
    # Album folder is the immediate parent; artist is the grandparent
    album_seg = parts[-1]
    parent_seg = parts[-2] if len(parts) >= 2 else ""
    grand_seg = parts[-3] if len(parts) >= 3 else ""
    # Parse "YYYY - <album>" prefix off the album_seg
    year, album = "", album_seg
    m = re.match(r"^(19|20)\d{2}\s*-\s*(.+)$", album_seg)
    if m:
        year = album_seg[:4]
        album = m.group(2).strip()
    # Determine artist
    artist = ""
    if grand_seg in ("Soundtracks", "Compilations", "Radio", "Audiobooks", "Comedy"):
        # No artist for these buckets — return album-only
        return ("", album, year)
    # If the parent looks like a single-letter Letter folder, artist is the
    # current parent_seg
    if len(parent_seg) == 1 and parent_seg.isupper() and parent_seg.isalpha():
        # We're in: <Letter>\<Artist>\<Year - Album> ?
        # Actually that's grand=<Letter>, parent=<Artist>, album=<Year - Album>
        pass
    if grand_seg and len(grand_seg) == 1 and grand_seg.isupper() and grand_seg.isalpha():
        # path: ...\<Letter>\<Artist>\<Year - Album>
        artist = parent_seg
    elif grand_seg == "0-9" or grand_seg == "_Other":
        artist = parent_seg
    return (artist, album, year)


def get_album_tags_from_folder(folder):
    """Try ID3 tags first; fall back to folder-name parsing for the new tree.
    Returns (artist, album, year) or empty strings."""
    # Tier 1: ID3 tags from the first audio file
    try:
        entries = os.listdir(long_path(folder))
    except Exception:
        entries = []
    for fn in entries:
        ext = os.path.splitext(fn)[1].lower()
        if ext not in AUDIO_EXTS:
            continue
        try:
            f = MutagenFile(long_path(os.path.join(folder, fn)), easy=True)
            if not f or not f.tags:
                continue
            t = f.tags
            artist = (t.get("albumartist", [""]) or t.get("artist", [""]) or [""])[0]
            album = (t.get("album", [""]) or [""])[0]
            year = (t.get("date", [""]) or [""])[0]
            year_match = re.search(r"\b(19|20)\d{2}\b", year) if year else None
            year = year_match.group(0) if year_match else ""
            if artist and album:
                return (artist, album, year)
            # Save partial for possible fallback merge
            tag_partial = (artist, album, year)
            break
        except Exception:
            continue
    else:
        tag_partial = ("", "", "")

    # Tier 2: parse folder structure
    f_artist, f_album, f_year = folder_path_fallback(folder)
    artist = tag_partial[0] or f_artist
    album = tag_partial[1] or f_album
    year = tag_partial[2] or f_year
    return (artist, album, year)


def itunes_query(artist, album, timeout=10):
    """Query iTunes for an album's cover art URL (600x600)."""
    try:
        r = session().get(ITUNES_SEARCH, params={
            "term": f"{artist} {album}",
            "media": "music", "entity": "album", "limit": 5
        }, timeout=timeout)
        if r.status_code != 200:
            return None
        data = r.json()
        if not data.get("results"):
            return None
        # Pick best by string match
        from difflib import SequenceMatcher
        def score(item):
            a = SequenceMatcher(None,
                (item.get("artistName") or "").lower(), artist.lower()).ratio()
            b = SequenceMatcher(None,
                (item.get("collectionName") or "").lower(), album.lower()).ratio()
            return (a + b) / 2
        best = max(data["results"], key=score)
        if score(best) < 0.6:
            return None
        url = best.get("artworkUrl100")
        if not url:
            return None
        # Upgrade resolution
        url = url.replace("100x100bb", "600x600bb")
        return url
    except Exception:
        return None


def musicbrainz_release_id(artist, album, year="", timeout=15):
    """Look up a MusicBrainz release MBID for the album."""
    try:
        q_parts = [f'artist:"{artist}"', f'release:"{album}"']
        if year:
            q_parts.append(f'date:{year}')
        params = {"query": " AND ".join(q_parts), "fmt": "json", "limit": 5}
        r = session().get(f"{MB_API}/release", params=params, timeout=timeout)
        if r.status_code != 200:
            return None
        data = r.json()
        releases = data.get("releases") or []
        if not releases:
            return None
        return releases[0].get("id")
    except Exception:
        return None


def coverart_archive_url(mbid, timeout=15):
    """Get the front cover URL for a MusicBrainz release."""
    try:
        r = session().get(f"{CAA_BASE}/{mbid}", timeout=timeout, allow_redirects=True)
        if r.status_code != 200:
            return None
        data = r.json()
        for img in data.get("images", []):
            if img.get("front"):
                # Prefer 500px thumbnail; fall back to original
                t = img.get("thumbnails", {})
                return t.get("500") or t.get("large") or img.get("image")
        return None
    except Exception:
        return None


def download_image(url, dest_path, timeout=30):
    try:
        r = session().get(url, timeout=timeout, stream=True)
        if r.status_code != 200:
            return False
        with open(long_path(dest_path), "wb") as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)
        return True
    except Exception:
        return False


def process_folder(folder, use_caa=True):
    """Returns (folder, status, source, msg, size)."""
    if folder_has_art(folder):
        return (folder, "skipped_has_art", "n/a", "", 0)
    artist, album, year = get_album_tags_from_folder(folder)
    if not artist or not album:
        return (folder, "no_tags", "n/a", "missing artist or album in tags", 0)

    # Tier 1: iTunes
    url = itunes_query(artist, album)
    if url:
        dest = os.path.join(folder, "folder.jpg")
        if download_image(url, dest):
            try:
                sz = os.path.getsize(long_path(dest))
            except Exception:
                sz = 0
            return (folder, "ok", "itunes", url, sz)

    # Tier 2: MusicBrainz + CAA
    if use_caa:
        mbid = musicbrainz_release_id(artist, album, year)
        if mbid:
            caa_url = coverart_archive_url(mbid)
            if caa_url:
                dest = os.path.join(folder, "folder.jpg")
                if download_image(caa_url, dest):
                    try:
                        sz = os.path.getsize(long_path(dest))
                    except Exception:
                        sz = 0
                    return (folder, "ok", "coverart_archive", caa_url, sz)

    return (folder, "miss", "n/a", f"{artist} - {album}", 0)


def find_album_folders(music_root):
    """Return list of paths to leaf folders that contain audio."""
    out = []
    for dirpath, dirnames, filenames in os.walk(music_root):
        if "_QUARANTINE" in dirpath:
            dirnames[:] = [d for d in dirnames if d != "_QUARANTINE"]
            continue
        # If this folder has audio, treat as an album folder
        has_audio = any(os.path.splitext(fn)[1].lower() in AUDIO_EXTS for fn in filenames)
        if has_audio:
            out.append(dirpath)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--music-root", required=True)
    ap.add_argument("--csv-dir", required=True)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--no-coverart", action="store_true",
                    help="Skip Cover Art Archive fallback (iTunes only)")
    ap.add_argument("--resume", action="store_true",
                    help="Skip folders already in any prior album_art_run_*.log")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    music_root = Path(args.music_root).resolve()
    csv_dir = Path(args.csv_dir).resolve()

    # Resume — find prior logs
    prior_attempted = set()
    if args.resume:
        for f in csv_dir.glob("album_art_run_*.log"):
            try:
                with open(f, "r", encoding="utf-8") as lf:
                    for ln in lf:
                        parts = ln.split("\t")
                        if len(parts) >= 2:
                            prior_attempted.add(parts[1].strip())
            except Exception:
                pass
    print(f"Prior attempts: {len(prior_attempted):,}")

    print("Scanning for album folders...")
    t0 = time.time()
    folders = find_album_folders(music_root)
    print(f"  found {len(folders):,} album-bearing folders in {time.time()-t0:.1f}s")

    # Filter: skip ones with art and ones already attempted
    candidates = []
    for f in folders:
        if f in prior_attempted:
            continue
        if folder_has_art(f):
            continue
        candidates.append(f)
    print(f"  candidates without art and not previously tried: {len(candidates):,}")

    if args.limit:
        candidates = candidates[: args.limit]

    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    log_path = csv_dir / f"album_art_run_{ts}.log"

    counts = {"ok_itunes": 0, "ok_coverart_archive": 0,
              "miss": 0, "skipped_has_art": 0, "no_tags": 0}
    started = time.time()
    last_print = started
    log_lock = threading.Lock()
    bytes_dl = 0

    with open(log_path, "w", encoding="utf-8") as logf:
        logf.write(f"# album-art {ts}\n# total={len(candidates)} workers={args.workers}\n\n")
        logf.flush()
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = {ex.submit(process_folder, c, not args.no_coverart): c for c in candidates}
            for i, fut in enumerate(as_completed(futures), 1):
                folder, status, source, msg, size = fut.result()
                key = f"{status}_{source}" if status == "ok" else status
                counts[key] = counts.get(key, 0) + 1
                if status == "ok":
                    bytes_dl += size
                with log_lock:
                    logf.write(f"{status}\t{folder}\t{source}\t{msg}\n")
                now = time.time()
                if now - last_print >= 2.0 or i == len(candidates):
                    rate = i / max(now - started, 0.001)
                    hits = counts.get("ok_itunes", 0) + counts.get("ok_coverart_archive", 0)
                    hit_pct = 100.0 * hits / max(i - counts.get("skipped_has_art", 0) - counts.get("no_tags", 0), 1)
                    eta = (len(candidates) - i) / max(rate, 0.001) / 60
                    sys.stdout.write(
                        f"\r  {i:>5,}/{len(candidates):,}  hits={hits:,}  "
                        f"miss={counts.get('miss',0):,}  hit%={hit_pct:.0f}  "
                        f"{rate:.1f} f/s  ETA {eta:.0f} min  "
                        f"{bytes_dl/1024**2:.1f} MB"
                    )
                    sys.stdout.flush()
                    last_print = now

    sys.stdout.write("\n")
    print()
    print("==== summary ====")
    for k, v in counts.items():
        print(f"  {k:<22} {v:>6,}")
    print(f"  bytes downloaded:   {bytes_dl/1024**2:.1f} MB")
    print(f"  elapsed:            {(time.time()-started)/60:.1f} min")
    print(f"  log:                {log_path}")


if __name__ == "__main__":
    main()
