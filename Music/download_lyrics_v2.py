#!/usr/bin/env python3
"""
LRCLIB lyrics downloader v2 — smarter matching for higher hit rate.

Improvements over v1:
  - Title sanitization: strips '(Remastered ...)', '(Live)', '(Mono)',
    '(Single Version)', '(Bonus Track)', '(Acoustic)', '(Demo)', etc.
    before querying.
  - Artist sanitization: strips 'feat. ...', '& others', '(featuring ...)'
    before querying.
  - Three-tier fallback per track:
       1. exact (cleaned artist + cleaned title + album + duration)
       2. drop album
       3. /search endpoint (loose) and pick best result by edit distance
         on title + artist
  - Writes the run log into H:\\Music Cleanup\\ (not under E:\\Music\\)
    so it doesn't get caught by the companion mover.
  - --resume reads BOTH v1 and v2 prior logs.
  - --reprocess-misses re-tries every file that previously missed
    (useful after upgrading the matcher).

USAGE
-----
   pip install mutagen requests
   python download_lyrics_v2.py --music-root E:\\Music --csv-dir "H:\\Music Cleanup"
   python download_lyrics_v2.py --music-root E:\\Music --csv-dir "H:\\Music Cleanup" --resume
   python download_lyrics_v2.py --music-root E:\\Music --csv-dir "H:\\Music Cleanup" --reprocess-misses
"""
import argparse, json, os, re, sys, time
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

try:
    from mutagen import File as MutagenFile
except ImportError:
    sys.exit("ERROR: pip install mutagen")
try:
    import requests
except ImportError:
    sys.exit("ERROR: pip install requests")

LRCLIB_GET = "https://lrclib.net/api/get"
LRCLIB_SEARCH = "https://lrclib.net/api/search"
USER_AGENT = "MusicCleanupLyrics/2.0"
AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".m4b", ".mp4", ".ogg", ".opus",
              ".wma", ".wav", ".aac"}

# ---- title/artist cleanup ----------------------------------------------

# Patterns inside parentheses or brackets that we strip from titles.
TITLE_PAREN_KILL = re.compile(
    r"""\s*
    [\(\[\{]
    \s*
    (?:
       remaster(?:ed)?\s*(?:\d{4})?
       | mono(?:\s*version)?
       | stereo(?:\s*version)?
       | single\s*(?:version|edit)?
       | album\s*version
       | radio\s*(?:edit|version)?
       | extended(?:\s*(?:mix|version))?
       | clean(?:\s*version)?
       | explicit(?:\s*version)?
       | bonus(?:\s*track)?
       | demo
       | acoustic(?:\s*version)?
       | unplugged
       | live(?:\s*at[^)]*|\s*version)?
       | hidden(?:\s*track)?
       | instrumental
       | karaoke
       | reissue
       | deluxe(?:\s*edition)?
       | original(?:\s*motion\s*picture)?(?:\s*recording)?
       | from\s+[^)]+
    )
    [^)\]\}]*
    [\)\]\}]
    \s*""",
    re.IGNORECASE | re.VERBOSE,
)

# Strip trailing " - Single", " - Remastered", " - Live" etc.
TITLE_SUFFIX_KILL = re.compile(
    r"\s*-\s*(remaster(?:ed)?(?:\s*\d{4})?|mono|stereo|single\s*version|"
    r"album\s*version|radio\s*edit|extended\s*mix|live|acoustic|"
    r"unplugged|bonus\s*track|demo|instrumental|karaoke|reissue)\s*$",
    re.IGNORECASE,
)

# Artist cleanup: drop "feat. X", "ft. X", "(featuring X)", "& others"
ARTIST_FEAT_KILL = re.compile(
    r"\s*[\(\[]?\s*(feat\.|ft\.|featuring|with)\s+[^)\]]+[\)\]]?\s*$",
    re.IGNORECASE,
)
ARTIST_OTHERS_KILL = re.compile(r"\s*&\s*others\s*$", re.IGNORECASE)


def clean_title(title):
    if not title:
        return ""
    t = title
    # Repeatedly strip parenthetical noise (might be multiple)
    for _ in range(3):
        new = TITLE_PAREN_KILL.sub("", t).strip()
        if new == t:
            break
        t = new
    t = TITLE_SUFFIX_KILL.sub("", t).strip()
    return re.sub(r"\s+", " ", t).strip()


def clean_artist(artist):
    if not artist:
        return ""
    a = ARTIST_OTHERS_KILL.sub("", artist).strip()
    a = ARTIST_FEAT_KILL.sub("", a).strip()
    # If artist has comma-separated list, keep first one
    if "," in a and len(a) > 50:
        a = a.split(",", 1)[0].strip()
    return re.sub(r"\s+", " ", a).strip()


def long_path(p):
    s = str(p)
    if os.name == "nt":
        s = s.replace("/", "\\")
        if len(s) > 240 and not s.startswith("\\\\?\\"):
            return "\\\\?\\" + s
    return s


def get_tags(path):
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


def fetch_get(session, artist, title, album="", duration=0, timeout=10):
    """Try /api/get (exact match). Returns lyrics dict or None on 404 or error."""
    params = {"artist_name": artist, "track_name": title}
    if album:
        params["album_name"] = album
    if duration:
        params["duration"] = str(duration)
    try:
        r = session.get(LRCLIB_GET, params=params, timeout=timeout,
                        headers={"User-Agent": USER_AGENT, "Lrclib-Client": USER_AGENT})
        if r.status_code == 200:
            return r.json()
        return None
    except Exception:
        return None


def fetch_search(session, artist, title, timeout=10):
    """Try /api/search (loose). Returns best-scored result or None."""
    params = {"q": f"{artist} {title}"}
    try:
        r = session.get(LRCLIB_SEARCH, params=params, timeout=timeout,
                        headers={"User-Agent": USER_AGENT, "Lrclib-Client": USER_AGENT})
        if r.status_code != 200:
            return None
        results = r.json()
        if not results:
            return None
        # Score results by combined similarity of artist + title
        def score(item):
            a_sim = SequenceMatcher(None, (item.get("artistName") or "").lower(),
                                    artist.lower()).ratio()
            t_sim = SequenceMatcher(None, (item.get("trackName") or "").lower(),
                                    title.lower()).ratio()
            # only count entries with at least one lyrics field
            has = bool(item.get("syncedLyrics") or item.get("plainLyrics"))
            return (a_sim + t_sim) / 2 * (1.0 if has else 0.3)
        best = max(results[:20], key=score)
        if score(best) < 0.65:
            return None
        return best
    except Exception:
        return None


def find_lyrics(session, raw_artist, raw_album, raw_title, duration):
    """Try multi-tier strategy. Returns ('synced'/'plain'/None, text, query_kind)."""
    clean_a = clean_artist(raw_artist)
    clean_t = clean_title(raw_title)
    if not clean_a or not clean_t:
        return (None, "", "no_query")

    # Tier 1: exact with album + duration
    d = fetch_get(session, clean_a, clean_t, raw_album, duration)
    if d:
        if d.get("syncedLyrics"):
            return ("synced", d["syncedLyrics"], "exact")
        if d.get("plainLyrics"):
            return ("plain", d["plainLyrics"], "exact")

    # Tier 2: drop album
    d = fetch_get(session, clean_a, clean_t, "", duration)
    if d:
        if d.get("syncedLyrics"):
            return ("synced", d["syncedLyrics"], "no_album")
        if d.get("plainLyrics"):
            return ("plain", d["plainLyrics"], "no_album")

    # Tier 3: drop duration
    if duration:
        d = fetch_get(session, clean_a, clean_t, "", 0)
        if d:
            if d.get("syncedLyrics"):
                return ("synced", d["syncedLyrics"], "no_duration")
            if d.get("plainLyrics"):
                return ("plain", d["plainLyrics"], "no_duration")

    # Tier 4: /search
    s = fetch_search(session, clean_a, clean_t)
    if s:
        if s.get("syncedLyrics"):
            return ("synced", s["syncedLyrics"], "search")
        if s.get("plainLyrics"):
            return ("plain", s["plainLyrics"], "search")

    return (None, "", "miss")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--music-root", required=True)
    ap.add_argument("--csv-dir", required=True,
                    help="Where to write the run log (e.g. H:\\Music Cleanup)")
    ap.add_argument("--rate", type=float, default=0.2,
                    help="Min seconds between requests (default 0.2 = 5/s)")
    ap.add_argument("--resume", action="store_true",
                    help="Skip files already attempted in any prior lyrics_run_*.log")
    ap.add_argument("--reprocess-misses", action="store_true",
                    help="Re-query files that previously missed (uses v2 matcher)")
    ap.add_argument("--prefer-plain", action="store_true", default=True,
                    help="If no synced, write plain lyrics (default on)")
    ap.add_argument("--synced-only", action="store_true",
                    help="Only write if synced lyrics found")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    if args.synced_only:
        args.prefer_plain = False

    music_root = Path(args.music_root).resolve()
    csv_dir = Path(args.csv_dir).resolve()

    # Build "already attempted" set from prior logs (in BOTH csv_dir and music_root)
    prior_attempted = set()
    prior_misses = set()
    for log_dir in (csv_dir, music_root):
        for f in log_dir.glob("lyrics_run_*.log"):
            try:
                with open(f, "r", encoding="utf-8") as lf:
                    for ln in lf:
                        if "\t" not in ln:
                            continue
                        parts = ln.split("\t")
                        if len(parts) < 2:
                            continue
                        status, path = parts[0].strip(), parts[1].strip()
                        prior_attempted.add(path)
                        if status.startswith("miss") or status == "miss-no-synced":
                            prior_misses.add(path)
            except Exception:
                pass
    print(f"Prior attempts seen: {len(prior_attempted):,} | prior misses: {len(prior_misses):,}")

    # Scan for candidates
    print("Scanning audio files...")
    t0 = time.time()
    candidates = []
    for dirpath, dirnames, filenames in os.walk(music_root):
        dirnames[:] = [d for d in dirnames if d != "_QUARANTINE"]
        existing_lrc = set()
        for fn in filenames:
            if fn.lower().endswith(".lrc"):
                existing_lrc.add(fn[:-4].lower())
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in AUDIO_EXTS:
                continue
            stem = fn[: -len(ext)]
            if stem.lower() in existing_lrc:
                continue
            candidates.append(Path(dirpath) / fn)
    print(f"  candidates without .lrc: {len(candidates):,} ({time.time()-t0:.1f}s)")

    # Filter based on resume/reprocess
    final = []
    for p in candidates:
        sp = str(p)
        if args.reprocess_misses:
            # Only retry prior misses
            if sp in prior_misses:
                final.append(p)
            elif sp not in prior_attempted:
                final.append(p)  # also do new ones
        elif args.resume:
            if sp in prior_attempted:
                continue
            final.append(p)
        else:
            final.append(p)
    print(f"  to process: {len(final):,}")

    if args.limit:
        final = final[: args.limit]

    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    log_path = csv_dir / f"lyrics_run_{ts}.log"
    print(f"  log -> {log_path}\n")

    counts = {"queried": 0, "synced": 0, "plain": 0, "miss": 0,
              "skipped_no_tags": 0, "error": 0}
    by_tier = {"exact": 0, "no_album": 0, "no_duration": 0, "search": 0, "miss": 0, "no_query": 0}
    started = time.time()
    last_print = started
    session = requests.Session()
    last_req = 0.0

    with open(log_path, "w", encoding="utf-8") as logf:
        logf.write(f"# lyrics v2 {ts}\n# music_root={music_root}\n"
                   f"# total={len(final)}\n# synced_only={args.synced_only}\n\n")
        for i, path in enumerate(final, 1):
            artist, album, title, dur = get_tags(path)
            if not artist or not title:
                counts["skipped_no_tags"] += 1
                logf.write(f"skipped\t{path}\tno artist/title\n")
                continue
            # Throttle
            now = time.time()
            wait = args.rate - (now - last_req)
            if wait > 0:
                time.sleep(wait)
            last_req = time.time()
            counts["queried"] += 1

            kind, text, tier = find_lyrics(session, artist, album, title, dur)
            by_tier[tier] = by_tier.get(tier, 0) + 1

            if kind == "synced":
                counts["synced"] += 1
                try:
                    with open(long_path(path.with_suffix(".lrc")), "w", encoding="utf-8") as f:
                        f.write(text)
                    logf.write(f"ok-synced-{tier}\t{path}\t{len(text)} chars\n")
                except Exception as e:
                    counts["error"] += 1
                    logf.write(f"error\t{path}\t{e}\n")
            elif kind == "plain" and args.prefer_plain:
                counts["plain"] += 1
                wrapped = "[00:00.00] " + text.replace("\n", "\n[00:00.00] ")
                try:
                    with open(long_path(path.with_suffix(".lrc")), "w", encoding="utf-8") as f:
                        f.write(wrapped)
                    logf.write(f"ok-plain-{tier}\t{path}\t{len(wrapped)} chars\n")
                except Exception as e:
                    counts["error"] += 1
                    logf.write(f"error\t{path}\t{e}\n")
            else:
                counts["miss"] += 1
                logf.write(f"miss\t{path}\tcleaned: {clean_artist(artist)} - {clean_title(title)}\n")

            now = time.time()
            if now - last_print >= 2.0 or i == len(final):
                rate = counts["queried"] / max(now - started, 0.001)
                hit_pct = 100 * (counts["synced"] + counts["plain"]) / max(counts["queried"], 1)
                eta = (len(final) - i) * args.rate / 60
                sys.stdout.write(
                    f"\r  {i:>6,}/{len(final):,}  synced={counts['synced']:,}  "
                    f"plain={counts['plain']:,}  miss={counts['miss']:,}  "
                    f"hit={hit_pct:.1f}%  {rate:.1f} q/s  ETA {eta:.0f} min  "
                )
                sys.stdout.flush()
                last_print = now

    sys.stdout.write("\n")
    print()
    print("==== v2 summary ====")
    for k, v in counts.items():
        print(f"  {k:<22} {v:>6,}")
    print()
    print("Hit breakdown by tier:")
    for k, v in by_tier.items():
        print(f"  {k:<14} {v:>6,}")
    print(f"\nElapsed: {(time.time()-started)/60:.1f} min")


if __name__ == "__main__":
    main()
