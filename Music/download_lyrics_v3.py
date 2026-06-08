#!/usr/bin/env python3
"""
LRCLIB lyrics downloader v3 — parallel + faster timeouts.

Same matching logic as v2, but uses a thread pool so ~8 requests are in
flight at once. Should be ~8x faster than v2 with the same hit rate.

USAGE
-----
   pip install mutagen requests
   python download_lyrics_v3.py --music-root E:\\Music --csv-dir "H:\\Music Cleanup" --resume
   python download_lyrics_v3.py --music-root E:\\Music --csv-dir "H:\\Music Cleanup" --reprocess-misses
   python download_lyrics_v3.py ... --workers 12          # adjust parallelism
   python download_lyrics_v3.py ... --no-search           # drop /search tier (faster)
"""
import argparse, os, re, sys, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from queue import Queue

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
USER_AGENT = "MusicCleanupLyrics/3.0"
AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".m4b", ".mp4", ".ogg", ".opus",
              ".wma", ".wav", ".aac"}

# ---- title/artist cleanup (same as v2) ---------------------------------

TITLE_PAREN_KILL = re.compile(
    r"""\s*[\(\[\{]\s*(?:
       remaster(?:ed)?\s*(?:\d{4})?
       | mono(?:\s*version)? | stereo(?:\s*version)?
       | single\s*(?:version|edit)? | album\s*version
       | radio\s*(?:edit|version)? | extended(?:\s*(?:mix|version))?
       | clean(?:\s*version)? | explicit(?:\s*version)?
       | bonus(?:\s*track)? | demo | acoustic(?:\s*version)?
       | unplugged | live(?:\s*at[^)]*|\s*version)?
       | hidden(?:\s*track)? | instrumental | karaoke | reissue
       | deluxe(?:\s*edition)?
       | original(?:\s*motion\s*picture)?(?:\s*recording)?
       | from\s+[^)]+
    )[^)\]\}]*[\)\]\}]\s*""",
    re.IGNORECASE | re.VERBOSE,
)
TITLE_SUFFIX_KILL = re.compile(
    r"\s*-\s*(remaster(?:ed)?(?:\s*\d{4})?|mono|stereo|single\s*version|"
    r"album\s*version|radio\s*edit|extended\s*mix|live|acoustic|unplugged|"
    r"bonus\s*track|demo|instrumental|karaoke|reissue)\s*$", re.IGNORECASE)
ARTIST_FEAT_KILL = re.compile(
    r"\s*[\(\[]?\s*(feat\.|ft\.|featuring|with)\s+[^)\]]+[\)\]]?\s*$", re.IGNORECASE)
ARTIST_OTHERS_KILL = re.compile(r"\s*&\s*others\s*$", re.IGNORECASE)


def clean_title(t):
    if not t: return ""
    for _ in range(3):
        new = TITLE_PAREN_KILL.sub("", t).strip()
        if new == t: break
        t = new
    t = TITLE_SUFFIX_KILL.sub("", t).strip()
    return re.sub(r"\s+", " ", t).strip()


def clean_artist(a):
    if not a: return ""
    a = ARTIST_OTHERS_KILL.sub("", a).strip()
    a = ARTIST_FEAT_KILL.sub("", a).strip()
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
        return (
            (t.get("artist", [""]) or [""])[0],
            (t.get("album", [""]) or [""])[0],
            (t.get("title", [""]) or [""])[0],
            round(getattr(f.info, "length", 0)) if hasattr(f, "info") else 0,
        )
    except Exception:
        return ("", "", "", 0)


# ---- LRCLIB API helpers (with per-thread session) ----------------------

_thread_local = threading.local()


def session():
    if not hasattr(_thread_local, "s"):
        s = requests.Session()
        s.headers.update({"User-Agent": USER_AGENT, "Lrclib-Client": USER_AGENT})
        _thread_local.s = s
    return _thread_local.s


def fetch_get(artist, title, album="", duration=0, timeout=20):
    params = {"artist_name": artist, "track_name": title}
    if album: params["album_name"] = album
    if duration: params["duration"] = str(duration)
    try:
        r = session().get(LRCLIB_GET, params=params, timeout=timeout)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def fetch_search(artist, title, timeout=15):
    try:
        r = session().get(LRCLIB_SEARCH,
                          params={"q": f"{artist} {title}"}, timeout=timeout)
        if r.status_code != 200: return None
        results = r.json()
        if not results: return None
        def score(item):
            a_sim = SequenceMatcher(None,
                (item.get("artistName") or "").lower(), artist.lower()).ratio()
            t_sim = SequenceMatcher(None,
                (item.get("trackName") or "").lower(), title.lower()).ratio()
            has = bool(item.get("syncedLyrics") or item.get("plainLyrics"))
            return (a_sim + t_sim) / 2 * (1.0 if has else 0.3)
        best = max(results[:20], key=score)
        return best if score(best) >= 0.65 else None
    except Exception:
        return None


def find_lyrics(raw_artist, raw_album, raw_title, duration, use_search=True):
    ca = clean_artist(raw_artist)
    ct = clean_title(raw_title)
    if not ca or not ct:
        return (None, "", "no_query")
    # Tier 1: exact
    d = fetch_get(ca, ct, raw_album, duration)
    if d:
        if d.get("syncedLyrics"): return ("synced", d["syncedLyrics"], "exact")
        if d.get("plainLyrics"):  return ("plain",  d["plainLyrics"], "exact")
    # Tier 2: no album
    d = fetch_get(ca, ct, "", duration)
    if d:
        if d.get("syncedLyrics"): return ("synced", d["syncedLyrics"], "no_album")
        if d.get("plainLyrics"):  return ("plain",  d["plainLyrics"], "no_album")
    # Tier 3: no duration
    if duration:
        d = fetch_get(ca, ct, "", 0)
        if d:
            if d.get("syncedLyrics"): return ("synced", d["syncedLyrics"], "no_dur")
            if d.get("plainLyrics"):  return ("plain",  d["plainLyrics"], "no_dur")
    # Tier 4: search
    if use_search:
        s = fetch_search(ca, ct)
        if s:
            if s.get("syncedLyrics"): return ("synced", s["syncedLyrics"], "search")
            if s.get("plainLyrics"):  return ("plain",  s["plainLyrics"], "search")
    return (None, "", "miss")


# ---- worker -------------------------------------------------------------

def process_one(path, use_search, prefer_plain):
    """Return (path, status, tier, text_or_None, error)."""
    try:
        artist, album, title, dur = get_tags(path)
    except Exception as e:
        return (path, "error_tags", "n/a", None, str(e))
    if not artist or not title:
        return (path, "skipped", "no_tags", None, None)
    kind, text, tier = find_lyrics(artist, album, title, dur, use_search=use_search)
    if kind == "synced":
        try:
            with open(long_path(path.with_suffix(".lrc")), "w", encoding="utf-8") as f:
                f.write(text)
            return (path, "synced", tier, len(text), None)
        except Exception as e:
            return (path, "error_write", tier, None, str(e))
    if kind == "plain" and prefer_plain:
        wrapped = "[00:00.00] " + text.replace("\n", "\n[00:00.00] ")
        try:
            with open(long_path(path.with_suffix(".lrc")), "w", encoding="utf-8") as f:
                f.write(wrapped)
            return (path, "plain", tier, len(wrapped), None)
        except Exception as e:
            return (path, "error_write", tier, None, str(e))
    return (path, "miss", tier, None, None)


# ---- main --------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--music-root", required=True)
    ap.add_argument("--csv-dir", required=True)
    ap.add_argument("--workers", type=int, default=8,
                    help="Number of parallel workers (default 8)")
    ap.add_argument("--no-search", action="store_true",
                    help="Skip the /search fallback tier (faster, slightly lower hit rate)")
    ap.add_argument("--prefer-plain", action="store_true", default=True)
    ap.add_argument("--synced-only", action="store_true")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--reprocess-misses", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    if args.synced_only:
        args.prefer_plain = False

    music_root = Path(args.music_root).resolve()
    csv_dir = Path(args.csv_dir).resolve()

    # Read prior logs
    prior_attempted = set()
    prior_misses = set()
    for log_dir in (csv_dir, music_root):
        for f in log_dir.glob("lyrics_run_*.log"):
            try:
                with open(f, "r", encoding="utf-8") as lf:
                    for ln in lf:
                        parts = ln.split("\t")
                        if len(parts) < 2: continue
                        status, path = parts[0].strip(), parts[1].strip()
                        prior_attempted.add(path)
                        if status.startswith("miss"):
                            prior_misses.add(path)
            except Exception:
                pass
    print(f"Prior attempts: {len(prior_attempted):,} | prior misses: {len(prior_misses):,}")

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
            if ext not in AUDIO_EXTS: continue
            stem = fn[: -len(ext)]
            if stem.lower() in existing_lrc: continue
            candidates.append(Path(dirpath) / fn)
    print(f"  candidates without .lrc: {len(candidates):,} ({time.time()-t0:.1f}s)")

    final = []
    for p in candidates:
        sp = str(p)
        if args.reprocess_misses:
            if sp in prior_misses or sp not in prior_attempted:
                final.append(p)
        elif args.resume:
            if sp not in prior_attempted: final.append(p)
        else:
            final.append(p)
    print(f"  to process: {len(final):,} (workers={args.workers})")
    if args.limit: final = final[: args.limit]

    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    log_path = csv_dir / f"lyrics_run_{ts}.log"
    print(f"  log -> {log_path}\n")

    counts = {"synced": 0, "plain": 0, "miss": 0,
              "skipped": 0, "error_tags": 0, "error_write": 0}
    by_tier = {}
    started = time.time()
    last_print = started
    log_lock = threading.Lock()

    with open(log_path, "w", encoding="utf-8") as logf:
        logf.write(f"# lyrics v3 {ts}\n# music_root={music_root}\n"
                   f"# total={len(final)} workers={args.workers}\n\n")
        logf.flush()

        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = [ex.submit(process_one, p, not args.no_search,
                                 args.prefer_plain) for p in final]
            for i, fut in enumerate(as_completed(futures), 1):
                try:
                    path, status, tier, data, err = fut.result()
                except Exception as e:
                    status, tier, err = "error_tags", "n/a", str(e)
                    path = "<unknown>"
                counts[status] = counts.get(status, 0) + 1
                by_tier[tier] = by_tier.get(tier, 0) + 1
                with log_lock:
                    if status in ("synced", "plain"):
                        logf.write(f"ok-{status}-{tier}\t{path}\t{data}\n")
                    elif status == "miss":
                        logf.write(f"miss\t{path}\t{tier}\n")
                    elif status == "skipped":
                        logf.write(f"skipped\t{path}\tno tags\n")
                    else:
                        logf.write(f"{status}\t{path}\t{err}\n")

                now = time.time()
                if now - last_print >= 2.0 or i == len(final):
                    rate = i / max(now - started, 0.001)
                    hits = counts["synced"] + counts["plain"]
                    den = max(i - counts["skipped"], 1)
                    hit_pct = 100 * hits / den
                    eta = (len(final) - i) / max(rate, 0.001) / 60
                    sys.stdout.write(
                        f"\r  {i:>6,}/{len(final):,}  synced={counts['synced']:,}  "
                        f"plain={counts['plain']:,}  miss={counts['miss']:,}  "
                        f"hit={hit_pct:.1f}%  {rate:.1f} q/s  ETA {eta:.0f} min  "
                    )
                    sys.stdout.flush()
                    last_print = now

    sys.stdout.write("\n")
    print()
    print("==== v3 summary ====")
    for k, v in counts.items():
        print(f"  {k:<14} {v:>6,}")
    print()
    print("By tier:")
    for k, v in sorted(by_tier.items(), key=lambda x: -x[1]):
        print(f"  {k:<14} {v:>6,}")
    print(f"\nElapsed: {(time.time()-started)/60:.1f} min")


if __name__ == "__main__":
    main()
