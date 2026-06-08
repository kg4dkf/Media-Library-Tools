#!/usr/bin/env python3
"""
Intake pipeline for new music.

Takes audio files in an INCOMING folder (any nesting), identifies them via
AcoustID + MusicBrainz, writes canonical tags, downloads art + lyrics +
creates NFO, and moves into your canonical library tree.

Designed for: ripping CDs into a staging folder, dragging downloaded
albums in, etc. Run it nightly and your music gets filed properly.

PIPELINE per audio file
-----------------------
   1. Read existing tags via mutagen
   2. Fingerprint with fpcalc + query AcoustID (if available)
   3. Cross-reference with MusicBrainz (via AcoustID recording_id, or
      via artist+album+year search)
   4. Last.fm lookup -> genre
   5. Cover Art Archive -> folder.jpg + embedded APIC
   6. LRCLIB -> .lrc sidecar
   7. Sanitize and build target path:
      Music\\<Letter>\\<Artist>\\<Year - Album>\\<Track> - <Title>.ext
   8. Write tags via mutagen (folder + MB + Last.fm + Discogs + AcoustID)
   9. Write album.nfo (XML metadata for Kodi/Plex)
  10. Move audio + folder.jpg to target

REQUIREMENTS
------------
   pip install mutagen requests pyacoustid musicbrainzngs
   fpcalc.exe on PATH (Chromaprint)
   env vars: ACOUSTID_API_KEY, LASTFM_API_KEY, DISCOGS_TOKEN (optional)

USAGE
-----
   python intake_new_music.py --incoming "H:\\Music Cleanup\\incoming" --music-root E:\\Music --dry-run
   python intake_new_music.py --incoming "H:\\Music Cleanup\\incoming" --music-root E:\\Music

By default, runs in dry-run mode (preview only). Pass --execute to move.

OPTIONS
-------
   --no-acoustid         skip fingerprint identification (faster, less accurate)
   --no-lyrics           skip LRCLIB download
   --no-art              skip Cover Art Archive
   --no-nfo              don't write album.nfo files
   --keep-source         keep originals in incoming (copy instead of move)
"""
import argparse, csv, hashlib, io, os, re, shutil, subprocess, sys, time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

try:
    from mutagen import File as MutagenFile
    from mutagen.id3 import ID3, APIC, TIT2, TALB, TPE1, TPE2, TRCK, TPOS, TDRC, TCON, TCOM, TXXX
    from mutagen.mp3 import MP3
    from mutagen.flac import FLAC, Picture as FLACPicture
    from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
    from mutagen.oggvorbis import OggVorbis
    from mutagen.oggopus import OggOpus
    from mutagen.asf import ASF, ASFUnicodeAttribute, ASFByteArrayAttribute
    import struct, base64
except ImportError:
    sys.exit("pip install mutagen")
try:
    import requests
except ImportError:
    sys.exit("pip install requests")
try:
    import acoustid
    HAS_ACOUSTID = True
except ImportError:
    HAS_ACOUSTID = False
try:
    import musicbrainzngs as mb
    HAS_MB = True
except ImportError:
    HAS_MB = False


AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".m4b", ".mp4", ".aac",
              ".ogg", ".oga", ".opus", ".wma", ".wav", ".aiff", ".aif"}
LRCLIB_GET = "https://lrclib.net/api/get"
ITUNES_SEARCH = "https://itunes.apple.com/search"
LASTFM_URL = "http://ws.audioscrobbler.com/2.0/"
CAA_BASE = "https://coverartarchive.org/release"
USER_AGENT = "MusicCleanupIntake/1.0"

ILLEGAL = {"<": "(", ">": ")", ":": " -", '"': "'", "/": " ", "\\": " ",
           "|": "-", "?": "", "*": ""}
LEAD_ARTICLES = ("The ", "A ", "An ", "Le ", "La ", "Les ", "El ", "Los ")


def sanitize(s, max_len=180):
    if not s: return ""
    s = str(s)
    for k, v in ILLEGAL.items():
        s = s.replace(k, v)
    s = "".join(c if ord(c) >= 32 else " " for c in s)
    s = re.sub(r"\s+", " ", s).strip().rstrip(". ")
    return s[:max_len].rstrip(". ")


def letter_for(artist):
    if not artist:
        return "_Unknown"
    a = artist.strip()
    for art in LEAD_ARTICLES:
        if a.lower().startswith(art.lower()):
            a = a[len(art):].strip()
            break
    if not a:
        return "_Unknown"
    c = a[0].upper()
    if c.isalpha() and ord(c) < 128:
        return c
    if c.isdigit():
        return "0-9"
    return "_Other"


def long_path(p):
    s = str(p)
    if os.name == "nt" and len(s) > 240 and not s.startswith("\\\\?\\"):
        return "\\\\?\\" + s.replace("/", "\\")
    return s


def read_existing_tags(path):
    out = {"artist": "", "album_artist": "", "album": "", "title": "",
           "track": "", "disc": "", "date": "", "genre": "", "composer": "",
           "duration": 0}
    try:
        f = MutagenFile(long_path(path), easy=True)
        if not f:
            return out
        t = f.tags or {}
        for k_out, k_in in [("artist","artist"), ("album_artist","albumartist"),
                            ("album","album"), ("title","title"),
                            ("track","tracknumber"), ("disc","discnumber"),
                            ("date","date"), ("genre","genre"),
                            ("composer","composer")]:
            v = t.get(k_in)
            if v:
                if isinstance(v, list): v = v[0]
                out[k_out] = str(v).strip()
        out["duration"] = round(getattr(f.info, "length", 0)) if hasattr(f, "info") else 0
    except Exception:
        pass
    return out


def fingerprint_match(path, api_key):
    """Returns dict or None on no match."""
    if not HAS_ACOUSTID or not api_key:
        return None
    try:
        results = list(acoustid.match(api_key, str(path), meta="recordings releases"))
        if not results:
            return None
        score, rec_id, title, artist = results[0]
        if score < 0.85:
            return None
        return {"score": score, "rec_id": rec_id, "title": title, "artist": artist}
    except Exception:
        return None


def query_mb(artist, album, year, contact):
    if not HAS_MB: return None
    try:
        q = f'artist:"{artist}" AND release:"{album}"'
        if year: q += f' AND date:{year}'
        mb.set_useragent("MusicCleanupIntake", "1.0", contact=contact)
        res = mb.search_releases(query=q, limit=1)
        if not res.get("release-list"):
            return None
        top = res["release-list"][0]
        score = int(top.get("ext:score", 0))
        if score < 85:
            return None
        ac = top.get("artist-credit", [{}])
        first = ac[0].get("artist", {}) if ac else {}
        return {
            "score": score,
            "release_id": top.get("id", ""),
            "release_group_id": top.get("release-group", {}).get("id", "") if top.get("release-group") else "",
            "album_canonical": top.get("title", ""),
            "artist_canonical": first.get("name", ""),
            "artist_id": first.get("id", ""),
            "date": top.get("date", ""),
            "country": top.get("country", ""),
        }
    except Exception:
        return None


def lastfm_genre(session, api_key, artist, album):
    """Returns top genre tag or empty string."""
    if not api_key:
        return ""
    for method, extra in (("album.gettoptags", {"album": album}),
                          ("artist.gettoptags", {})):
        try:
            params = {"method": method, "artist": artist, "api_key": api_key,
                      "format": "json", "autocorrect": "1"}
            params.update(extra)
            r = session.get(LASTFM_URL, params=params, timeout=10)
            if r.status_code != 200:
                continue
            data = r.json()
            tags = (data.get("toptags") or {}).get("tag") or []
            if isinstance(tags, dict): tags = [tags]
            for t in tags:
                name = t.get("name", "").strip()
                cnt = int(t.get("count") or 0)
                if name and cnt > 5:
                    return " ".join(w.capitalize() for w in name.split())
        except Exception:
            pass
    return ""


def caa_url(session, release_id):
    if not release_id:
        return ""
    try:
        r = session.get(f"{CAA_BASE}/{release_id}", timeout=10, allow_redirects=True)
        if r.status_code == 200:
            data = r.json()
            for img in data.get("images", []):
                if img.get("front"):
                    t = img.get("thumbnails", {})
                    return t.get("500") or t.get("large") or img.get("image")
    except Exception:
        pass
    return ""


def itunes_art_url(session, artist, album):
    """Fallback art source."""
    try:
        r = session.get(ITUNES_SEARCH, params={"term": f"{artist} {album}",
            "media": "music", "entity": "album", "limit": 1}, timeout=10)
        if r.status_code != 200:
            return ""
        data = r.json()
        if not data.get("results"):
            return ""
        url = data["results"][0].get("artworkUrl100", "")
        if url:
            return url.replace("100x100bb", "600x600bb")
    except Exception:
        pass
    return ""


def lrclib_lyrics(session, artist, title, album, duration):
    try:
        params = {"artist_name": artist, "track_name": title}
        if album: params["album_name"] = album
        if duration: params["duration"] = str(duration)
        r = session.get(LRCLIB_GET, params=params, timeout=10)
        if r.status_code == 200:
            data = r.json()
            return (data.get("syncedLyrics") or "").strip(), (data.get("plainLyrics") or "").strip()
    except Exception:
        pass
    return "", ""


def download_bytes(session, url, timeout=20):
    try:
        r = session.get(url, timeout=timeout)
        if r.status_code == 200:
            return r.content
    except Exception:
        pass
    return None


def parse_track_from_filename(filename, ext):
    stem = filename[: -len(ext)] if ext and filename.endswith(ext) else filename
    m = re.match(r"^\s*(\d{1,3})[\s\-_.]+(.+)$", stem)
    if m:
        return f"{int(m.group(1)):02d}", m.group(2).strip()
    return "", stem.strip()


def write_nfo(folder, artist, album, year, genre, tracks):
    """Write a simple album.nfo for Kodi/Plex/Jellyfin."""
    p = Path(folder) / "album.nfo"
    lines = ["<album>"]
    lines.append(f"  <title>{album}</title>")
    lines.append(f"  <artist>{artist}</artist>")
    if year:  lines.append(f"  <year>{year}</year>")
    if genre: lines.append(f"  <genre>{genre}</genre>")
    for t in tracks:
        lines.append(f"  <track>")
        lines.append(f"    <position>{t.get('track','')}</position>")
        lines.append(f"    <title>{t.get('title','')}</title>")
        lines.append(f"  </track>")
    lines.append("</album>")
    try:
        with open(long_path(p), "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return True
    except Exception:
        return False


def write_tags_simple(path, tags, art_bytes=None):
    """Use a stripped-down tag writer that supports MP3/FLAC/MP4/OGG/WMA."""
    # Delegate to tag_apply's write_tags
    from importlib.util import spec_from_file_location, module_from_spec
    here = os.path.dirname(os.path.abspath(__file__))
    apply_path = os.path.join(here, "tag_apply.py")
    if os.path.exists(apply_path):
        spec = spec_from_file_location("tag_apply", apply_path)
        mod = module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.write_tags(path, tags, art_bytes)
    return (False, "tag_apply.py not found")


def process_album(album_folder, music_root, args, session, contact):
    """Process one folder of audio files as a single album."""
    audio = []
    for fn in sorted(os.listdir(album_folder)):
        ext = os.path.splitext(fn)[1].lower()
        if ext in AUDIO_EXTS:
            audio.append((os.path.join(album_folder, fn), fn, ext))
    if not audio:
        return {"folder": album_folder, "status": "no_audio"}

    # Per-album identification: use first track for album-level lookups
    first_path = audio[0][0]
    first_tags = read_existing_tags(first_path)
    print(f"\n--- {album_folder} ---")
    print(f"  {len(audio)} audio files")
    print(f"  current tags: artist='{first_tags['artist']}' album='{first_tags['album']}' year='{first_tags['date']}'")

    # AcoustID fingerprint
    ac = None
    if not args.no_acoustid and HAS_ACOUSTID and args.acoustid_key:
        ac = fingerprint_match(first_path, args.acoustid_key)
        if ac:
            print(f"  AcoustID match: {ac['artist']} / {ac['title']} (score={ac['score']:.2f})")

    # Determine canonical artist/album/year
    artist = (ac["artist"] if ac else "") or first_tags["artist"] or ""
    album = first_tags["album"] or ""
    year = first_tags["date"][:4] if first_tags["date"] else ""

    # MB lookup
    mb_data = query_mb(artist, album, year, contact) if (artist and album) else None
    if mb_data:
        print(f"  MB match (score {mb_data['score']}): {mb_data['artist_canonical']} / {mb_data['album_canonical']}")
        artist = mb_data["artist_canonical"] or artist
        album = mb_data["album_canonical"] or album
        year = (mb_data["date"][:4] if mb_data["date"] else "") or year

    if not (artist and album):
        return {"folder": album_folder, "status": "no_identification"}

    # Last.fm genre
    genre = ""
    if args.lastfm_key:
        genre = lastfm_genre(session, args.lastfm_key, artist, album)
    genre = genre or first_tags["genre"]

    # Cover art
    art_bytes = None
    if not args.no_art:
        if mb_data and mb_data.get("release_id"):
            url = caa_url(session, mb_data["release_id"])
            if url:
                art_bytes = download_bytes(session, url)
                print(f"  art: CAA ({len(art_bytes)} bytes)" if art_bytes else "  art: CAA url found but download failed")
        if not art_bytes:
            url = itunes_art_url(session, artist, album)
            if url:
                art_bytes = download_bytes(session, url)
                print(f"  art: iTunes ({len(art_bytes)} bytes)" if art_bytes else "  art: iTunes url found but download failed")

    # Build target folder
    letter = letter_for(artist)
    artist_sanitized = sanitize(artist)
    album_folder_name = sanitize(f"{year} - {album}" if year else album)
    target_folder = music_root / letter / artist_sanitized / album_folder_name

    if args.dry_run:
        print(f"  TARGET: {target_folder}")

    # Build track-level plan
    track_plan = []
    for src, fn, ext in audio:
        existing = read_existing_tags(src)
        # Try AcoustID per track
        track_ac = None
        if not args.no_acoustid and HAS_ACOUSTID and args.acoustid_key:
            track_ac = fingerprint_match(src, args.acoustid_key)
        title = (track_ac["title"] if track_ac else "") or existing["title"] or ""
        track = existing["track"]
        if not title or not track:
            t_track, t_title = parse_track_from_filename(fn, ext)
            track = track or t_track
            title = title or t_title

        track_clean = re.sub(r"/.*", "", track).zfill(2) if track else ""
        title_clean = sanitize(title) if title else os.path.splitext(fn)[0]
        new_name = (f"{track_clean} - {title_clean}{ext}" if track_clean
                    else f"{title_clean}{ext}")
        new_path = target_folder / new_name
        track_plan.append({
            "src": src, "fn": fn, "ext": ext,
            "title": title, "track": track,
            "ac_rec_id": track_ac["rec_id"] if track_ac else "",
            "duration": existing["duration"],
            "new_path": str(new_path),
        })
        if args.dry_run:
            print(f"    {fn:<60} -> {new_name}")

    if args.dry_run:
        return {"folder": album_folder, "status": "dry_run", "n": len(audio)}

    # Execute: create target folder, write art, move + tag each file
    os.makedirs(long_path(str(target_folder)), exist_ok=True)
    if art_bytes:
        try:
            with open(long_path(str(target_folder / "folder.jpg")), "wb") as f:
                f.write(art_bytes)
        except Exception as e:
            print(f"  could not write folder.jpg: {e}")

    moved = 0
    for tp in track_plan:
        src = tp["src"]
        dst = tp["new_path"]
        # Move (or copy if keep-source)
        try:
            if args.keep_source:
                shutil.copy2(long_path(src), long_path(dst))
            else:
                if os.path.exists(long_path(dst)):
                    print(f"    dst exists, skipping: {dst}")
                    continue
                os.rename(long_path(src), long_path(dst))
        except OSError:
            try:
                shutil.move(long_path(src), long_path(dst))
            except Exception as e:
                print(f"    move failed: {e}")
                continue

        # Tag at destination
        tags_to_write = {
            "artist": artist,
            "album_artist": artist,
            "album": album,
            "title": tp["title"],
            "track": tp["track"],
            "date": year,
            "genre": genre,
            "compilation": "0",
        }
        if mb_data:
            tags_to_write["mb_releaseid"] = mb_data.get("release_id", "")
            tags_to_write["mb_releasegroupid"] = mb_data.get("release_group_id", "")
            tags_to_write["mb_artistid"] = mb_data.get("artist_id", "")
        if tp["ac_rec_id"]:
            tags_to_write["mb_trackid"] = tp["ac_rec_id"]
            tags_to_write["acoustid_id"] = tp["ac_rec_id"]

        ok, msg = write_tags_simple(dst, tags_to_write, art_bytes)
        if not ok:
            print(f"    tag write failed: {msg}")

        # LRCLIB lyrics
        if not args.no_lyrics and tp["title"]:
            synced, plain = lrclib_lyrics(session, artist, tp["title"], album, tp["duration"])
            if synced:
                lrc_path = os.path.splitext(dst)[0] + ".lrc"
                try:
                    with open(long_path(lrc_path), "w", encoding="utf-8") as f:
                        f.write(synced)
                except Exception:
                    pass

        moved += 1

    # NFO
    if not args.no_nfo:
        write_nfo(str(target_folder), artist, album, year, genre,
                  [{"track": tp["track"], "title": tp["title"]} for tp in track_plan])

    print(f"  -> moved {moved}/{len(audio)} to {target_folder}")
    return {"folder": album_folder, "status": "moved", "n": moved,
            "target": str(target_folder)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--incoming", required=True)
    ap.add_argument("--music-root", required=True)
    ap.add_argument("--dry-run", action="store_true", default=True)
    ap.add_argument("--execute", dest="dry_run", action="store_false")
    ap.add_argument("--no-acoustid", action="store_true")
    ap.add_argument("--no-lyrics", action="store_true")
    ap.add_argument("--no-art", action="store_true")
    ap.add_argument("--no-nfo", action="store_true")
    ap.add_argument("--keep-source", action="store_true",
                    help="Copy instead of move (originals stay in --incoming)")
    ap.add_argument("--contact", default="")
    args = ap.parse_args()

    args.acoustid_key = os.environ.get("ACOUSTID_API_KEY", "").strip()
    args.lastfm_key = os.environ.get("LASTFM_API_KEY", "").strip()

    incoming = Path(args.incoming).resolve()
    music_root = Path(args.music_root).resolve()
    if not incoming.is_dir():
        sys.exit(f"--incoming not a directory: {incoming}")
    if not music_root.is_dir():
        sys.exit(f"--music-root not a directory: {music_root}")

    print(f"Incoming:   {incoming}")
    print(f"Music root: {music_root}")
    print(f"Dry-run:    {args.dry_run}")
    print(f"AcoustID:   {bool(args.acoustid_key) and HAS_ACOUSTID and not args.no_acoustid}")
    print(f"Last.fm:    {bool(args.lastfm_key) and not args.no_lyrics}")
    print(f"Lyrics:     {not args.no_lyrics}")
    print(f"Art:        {not args.no_art}")
    print(f"NFO:        {not args.no_nfo}")

    # Find every folder under incoming that has audio files (album-level grouping)
    album_folders = []
    for dp, dn, fn in os.walk(long_path(str(incoming))):
        if any(os.path.splitext(f)[1].lower() in AUDIO_EXTS for f in fn):
            clean = dp[4:] if dp.startswith("\\\\?\\") else dp
            album_folders.append(clean)
    print(f"\nAlbum folders detected: {len(album_folders)}")

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    results = []
    for af in album_folders:
        try:
            r = process_album(af, music_root, args, session, args.contact)
            results.append(r)
        except KeyboardInterrupt:
            print("\nInterrupted")
            break
        except Exception as e:
            print(f"\nERROR processing {af}: {e}")
            results.append({"folder": af, "status": "error", "msg": str(e)})

    print()
    print("==== summary ====")
    from collections import Counter
    by_status = Counter(r["status"] for r in results)
    for k, n in by_status.most_common():
        print(f"  {k:<22} {n:>5}")


if __name__ == "__main__":
    main()
