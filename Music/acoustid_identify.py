#!/usr/bin/env python3
"""
Identify audio files by AcoustID/Chromaprint fingerprint.

Reads each candidate audio file, generates a Chromaprint fingerprint via
fpcalc, queries the AcoustID API, and records the top MusicBrainz
recording matches in a CSV for review.

By default targets only "weakly tagged" tracks (no artist OR no title), so
we use our limited API quota where it matters most. With --all it
fingerprints every audio file in the music root.

Output: acoustid_results.csv with columns:
   abs_path, current_artist, current_album, current_title, duration_sec,
   fp_status, ac_score, ac_recording_id, ac_artist, ac_title, ac_album,
   ac_release_id, ac_year, ac_match_count, notes

The tag fixer (later phase) will read this CSV and propose tag updates.

USAGE
-----
  pip install pyacoustid mutagen requests
  set ACOUSTID_API_KEY env var first (see acoustid_setup_check.py)

  python acoustid_identify.py --music-root E:\\Music --csv-dir "H:\\Music Cleanup"
  python acoustid_identify.py --music-root E:\\Music --csv-dir "H:\\Music Cleanup" --all
  python acoustid_identify.py --music-root E:\\Music --csv-dir "H:\\Music Cleanup" --resume
  python acoustid_identify.py --music-root E:\\Music --csv-dir "H:\\Music Cleanup" --workers 4
"""
import argparse, csv, json, os, sys, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

try:
    import acoustid
except ImportError:
    sys.exit("pip install pyacoustid")
try:
    from mutagen import File as MutagenFile
except ImportError:
    sys.exit("pip install mutagen")

AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".m4b", ".mp4", ".ogg", ".opus",
              ".wma", ".wav", ".aac"}

# AcoustID free tier: ~3 requests/sec sustained. Stay under that.
DEFAULT_RATE_PER_WORKER = 0.4  # seconds between requests per worker


def long_path(p):
    s = str(p)
    if os.name == "nt" and len(s) > 240 and not s.startswith("\\\\?\\"):
        return "\\\\?\\" + s.replace("/", "\\")
    return s


def get_current_tags(path):
    try:
        f = MutagenFile(long_path(path), easy=True)
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


def fingerprint_and_match(path, api_key):
    """Returns dict with status + match data or error."""
    out = {
        "fp_status": "", "ac_score": 0.0,
        "ac_recording_id": "", "ac_artist": "", "ac_title": "",
        "ac_album": "", "ac_release_id": "", "ac_year": "",
        "ac_match_count": 0, "notes": "",
    }
    try:
        # acoustid.match returns iterator of (score, recording_id, title, artist)
        results = list(acoustid.match(
            api_key,
            str(path),
            meta="recordings releases",
        ))
    except acoustid.NoBackendError:
        out["fp_status"] = "no_fpcalc"
        out["notes"] = "fpcalc not found"
        return out
    except acoustid.FingerprintGenerationError as e:
        out["fp_status"] = "fp_error"
        out["notes"] = f"fingerprint failed: {e}"
        return out
    except acoustid.WebServiceError as e:
        out["fp_status"] = "web_error"
        out["notes"] = f"acoustid web error: {e}"
        return out
    except Exception as e:
        out["fp_status"] = "error"
        out["notes"] = type(e).__name__ + ": " + str(e)
        return out

    if not results:
        out["fp_status"] = "no_match"
        return out

    out["ac_match_count"] = len(results)
    # results is already sorted by score descending in pyacoustid
    score, rec_id, title, artist = results[0]
    out["fp_status"] = "match"
    out["ac_score"] = round(score, 3)
    out["ac_recording_id"] = rec_id or ""
    out["ac_artist"] = artist or ""
    out["ac_title"] = title or ""
    # To get album + release_id + year, we need to call lookup with more meta
    try:
        ext_data = acoustid.lookup(api_key, str(path), meta="recordings releases")
        # Extract first release info for the top recording
        results_full = (ext_data.get("results") or [])
        for r in results_full:
            recs = r.get("recordings") or []
            for rec in recs:
                if rec.get("id") == rec_id:
                    releases = rec.get("releases") or []
                    if releases:
                        rel = releases[0]
                        out["ac_release_id"] = rel.get("id", "") or ""
                        out["ac_album"] = rel.get("title", "") or ""
                        date = rel.get("date", "") or ""
                        # extract year
                        import re
                        m = re.search(r"\b(19|20)\d{2}\b", date)
                        if m:
                            out["ac_year"] = m.group(0)
                    break
            if out["ac_album"]:
                break
    except Exception as e:
        out["notes"] = f"meta lookup failed: {e}"
    return out


def is_weakly_tagged(artist, title):
    """Return True if the file should be a candidate (missing artist or title)."""
    return not (artist and artist.strip()) or not (title and title.strip())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--music-root", required=True)
    ap.add_argument("--csv-dir", required=True)
    ap.add_argument("--out", default="acoustid_results.csv")
    ap.add_argument("--all", action="store_true",
                    help="Process every audio file (default: only weakly-tagged)")
    ap.add_argument("--workers", type=int, default=2,
                    help="Parallel workers (free AcoustID limit: ~3 req/sec total)")
    ap.add_argument("--rate", type=float, default=DEFAULT_RATE_PER_WORKER,
                    help="Min seconds between requests per worker")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--retry-web-errors", action="store_true",
                    help="With --resume, also re-query rows that previously got web_error status")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--root-subdir", default="",
                    help="Restrict scan to a subdirectory of music-root "
                         "(e.g. 'Irish' to only fingerprint files under E:\\Music\\Irish). "
                         "Repeatable as comma-separated list.")
    ap.add_argument("--from-csv", default="",
                    help="Read candidate file paths from a CSV column instead of "
                         "walking the disk. Use with --csv-column to pick the column.")
    ap.add_argument("--csv-column", default="abs_path",
                    help="Column name to read paths from when using --from-csv (default: abs_path)")
    args = ap.parse_args()

    api_key = os.environ.get("ACOUSTID_API_KEY", "")
    if not api_key:
        sys.exit("ERROR: ACOUSTID_API_KEY env var not set. Run acoustid_setup_check.py first.")

    music_root = Path(args.music_root).resolve()
    csv_dir = Path(args.csv_dir).resolve()
    out_path = csv_dir / args.out

    # Resume — read paths already processed
    prior_done = set()
    rewriting_out = False
    if args.resume and out_path.exists():
        try:
            kept_rows = []
            with open(out_path, "r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                fieldnames = reader.fieldnames
                for r in reader:
                    p = r.get("abs_path", "").strip()
                    status = r.get("fp_status", "").strip()
                    if not p:
                        continue
                    if args.retry_web_errors and status == "web_error":
                        # Drop this row so it'll get re-queried
                        continue
                    prior_done.add(p)
                    kept_rows.append(r)
            print(f"Resume: {len(prior_done):,} previously processed "
                  f"({'will retry web_errors' if args.retry_web_errors else 'including web_errors'})")
            # If we filtered out web_errors, rewrite the CSV without them so
            # we can append fresh attempts.
            if args.retry_web_errors and fieldnames:
                with open(out_path, "w", encoding="utf-8", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=fieldnames)
                    w.writeheader()
                    w.writerows(kept_rows)
                print(f"  rewrote CSV with web_error rows removed ({len(kept_rows):,} kept)")
        except Exception as e:
            print(f"Resume read failed: {e}")

    # Scan candidates — either from a CSV column, a subset of subdirs, or full walk
    t0 = time.time()
    candidates = []
    if args.from_csv:
        print(f"Reading candidates from {args.from_csv} (column={args.csv_column})...")
        with open(args.from_csv, "r", encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                p = (r.get(args.csv_column) or "").strip()
                if p and p not in prior_done:
                    candidates.append(p)
        print(f"  loaded {len(candidates):,} candidates ({time.time()-t0:.1f}s)")
    else:
        roots_to_scan = []
        if args.root_subdir:
            for sub in args.root_subdir.split(","):
                sub = sub.strip()
                if sub:
                    roots_to_scan.append(music_root / sub)
        else:
            roots_to_scan = [music_root]
        print(f"Scanning {len(roots_to_scan)} root(s)...")
        for root_scan in roots_to_scan:
            if not root_scan.exists():
                print(f"  WARNING: {root_scan} does not exist, skipping")
                continue
            for dirpath, dirnames, filenames in os.walk(root_scan):
                if "_QUARANTINE" in dirpath:
                    dirnames[:] = []
                    continue
                for fn in filenames:
                    ext = os.path.splitext(fn)[1].lower()
                    if ext not in AUDIO_EXTS:
                        continue
                    full = os.path.join(dirpath, fn)
                    if full in prior_done:
                        continue
                    candidates.append(full)
        print(f"  found {len(candidates):,} audio files ({time.time()-t0:.1f}s)")

    # Filter: weakly-tagged only by default
    if not args.all:
        print("Filtering to weakly-tagged candidates (missing artist or title)...")
        filtered = []
        for p in candidates:
            a, _, t, _ = get_current_tags(Path(p))
            if is_weakly_tagged(a, t):
                filtered.append(p)
        print(f"  weakly-tagged: {len(filtered):,}")
        candidates = filtered

    if args.limit:
        candidates = candidates[: args.limit]

    print(f"\nWill process {len(candidates):,} files with {args.workers} worker(s)")
    print(f"Expected runtime: {len(candidates) * args.rate / args.workers / 60:.0f} min minimum")
    print(f"Output: {out_path}\n")

    # Open output (append mode if resuming, write+header otherwise)
    cols = ["abs_path", "current_artist", "current_album", "current_title",
            "duration_sec", "fp_status", "ac_score",
            "ac_recording_id", "ac_artist", "ac_title", "ac_album",
            "ac_release_id", "ac_year", "ac_match_count", "notes"]

    is_new = not (args.resume and out_path.exists())
    out_f = open(out_path, "a" if not is_new else "w", encoding="utf-8", newline="")
    writer = csv.DictWriter(out_f, fieldnames=cols)
    if is_new:
        writer.writeheader()
    out_lock = threading.Lock()

    counts = {"match": 0, "no_match": 0, "fp_error": 0, "web_error": 0,
              "error": 0, "no_fpcalc": 0}
    started = time.time()
    last_print = started
    rate_lock = threading.Lock()
    last_req_per_worker = {}

    def process(path):
        tid = threading.get_ident()
        with rate_lock:
            last = last_req_per_worker.get(tid, 0)
            now = time.time()
            wait = args.rate - (now - last)
            if wait > 0:
                time.sleep(wait)
            last_req_per_worker[tid] = time.time()

        artist, album, title, dur = get_current_tags(Path(path))
        result = fingerprint_and_match(path, api_key)
        row = {
            "abs_path": path,
            "current_artist": artist,
            "current_album": album,
            "current_title": title,
            "duration_sec": dur,
        }
        row.update(result)
        with out_lock:
            writer.writerow(row)
            out_f.flush()
        return result["fp_status"]

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = [ex.submit(process, c) for c in candidates]
        for i, fut in enumerate(as_completed(futures), 1):
            try:
                status = fut.result()
            except Exception as e:
                status = "error"
            counts[status] = counts.get(status, 0) + 1
            now = time.time()
            if now - last_print >= 2.0 or i == len(candidates):
                rate = i / max(now - started, 0.001)
                hit_pct = 100.0 * counts.get("match", 0) / max(i, 1)
                eta = (len(candidates) - i) / max(rate, 0.001) / 60
                sys.stdout.write(
                    f"\r  {i:>6,}/{len(candidates):,}  match={counts['match']:,}  "
                    f"no_match={counts['no_match']:,}  errs={counts['error']:,}  "
                    f"hit={hit_pct:.0f}%  {rate:.1f}/s  ETA {eta:.0f} min  "
                )
                sys.stdout.flush()
                last_print = now

    out_f.close()
    sys.stdout.write("\n")
    print()
    print("==== summary ====")
    for k, v in counts.items():
        print(f"  {k:<14} {v:>6,}")
    print(f"  elapsed: {(time.time()-started)/60:.1f} min")
    print(f"  out:     {out_path}")


if __name__ == "__main__":
    main()
