#!/usr/bin/env python3
"""
Build a SQLite FTS5 index over a directory of subtitle (.srt) files.

Expected layout (flexible):
    <root>/<show>/SxxEyy.srt
    <root>/<show>/Season 01/SxxEyy.srt
    <root>/<show>/<anything>SxxEyy<anything>.srt

The show name is taken from the first directory under <root>. Season and
episode are extracted from the filename using a permissive SxxEyy regex.

Usage:
    python index_subs.py subs/ --db subtitles.sqlite

The identifier script (identify.py) queries this DB.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path
from typing import Iterable, Optional, Tuple

from tqdm import tqdm

import common

# Try a few permissive patterns. Most-specific first.
SE_PATTERNS = [
    re.compile(r"[Ss](\d{1,2})[\s._-]*[Ee](\d{1,3})"),
    re.compile(r"(?<!\d)(\d{1,2})x(\d{1,3})(?!\d)"),
    re.compile(r"[Ss]eason[\s._-]*(\d{1,2})[\s._-]*[Ee]pisode[\s._-]*(\d{1,3})", re.I),
]


def parse_se(name: str) -> Optional[Tuple[int, int]]:
    for pat in SE_PATTERNS:
        m = pat.search(name)
        if m:
            return int(m.group(1)), int(m.group(2))
    return None


def srt_text(path: Path) -> str:
    """Return cleaned dialogue from an SRT, or empty string on failure."""
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    # Tolerate BOM and various encodings.
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            text = None
    if text is None:
        return ""

    try:
        import srt as srt_lib
    except Exception:
        sys.exit("pip install srt")

    try:
        subs = list(srt_lib.parse(text))
    except Exception:
        # Fall back to a crude cleanup if parser chokes.
        subs = None

    if subs is None:
        lines = []
        for line in text.splitlines():
            ls = line.strip()
            if not ls:
                continue
            if "-->" in ls:
                continue
            if ls.isdigit():
                continue
            lines.append(ls)
        cleaned = "\n".join(lines)
    else:
        cleaned = "\n".join(sub.content for sub in subs)

    # Strip HTML/SSA-ish tags, music cues, speaker labels.
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = re.sub(r"\{[^}]*\}", " ", cleaned)
    cleaned = re.sub(r"\[[^\]]*\]", " ", cleaned)
    cleaned = re.sub(r"\([^)]*\)", " ", cleaned)
    cleaned = re.sub(r"^[A-Z][A-Z0-9 .\-']{1,20}:", " ", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


DDL = """
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY,
    show TEXT NOT NULL,
    show_norm TEXT NOT NULL,
    season INTEGER NOT NULL,
    episode INTEGER NOT NULL,
    srt_path TEXT NOT NULL,
    UNIQUE(show_norm, season, episode)
);
CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(
    show_norm UNINDEXED,
    season UNINDEXED,
    episode UNINDEXED,
    content,
    tokenize = 'porter unicode61'
);
"""


def opendb(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.executescript(DDL)
    conn.commit()
    return conn


def iter_srts(root: Path) -> Iterable[Tuple[str, Path]]:
    """Yield (show_dirname, srt_path) for every .srt under root."""
    for p in root.rglob("*.srt"):
        # show = first directory below root
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        if len(rel.parts) < 2:
            continue
        show = rel.parts[0]
        yield show, p


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("root", type=Path, help="Directory of subtitle files")
    ap.add_argument("--db", type=Path, default=Path("subtitles.sqlite"))
    ap.add_argument("--rebuild", action="store_true",
                    help="Drop existing FTS rows for matching shows before reindexing")
    args = ap.parse_args()

    if not args.root.is_dir():
        sys.exit(f"Not a directory: {args.root}")

    conn = opendb(args.db)
    cur = conn.cursor()

    items = list(iter_srts(args.root))
    print(f"Found {len(items)} .srt files under {args.root}")

    if args.rebuild:
        shows = sorted({common.normalize_show_name(s) for s, _ in items})
        for sn in shows:
            cur.execute("DELETE FROM episodes WHERE show_norm = ?", (sn,))
            cur.execute("DELETE FROM episodes_fts WHERE show_norm = ?", (sn,))
        conn.commit()

    indexed = 0
    skipped = 0
    for show, path in tqdm(items, desc="indexing", unit="srt"):
        se = parse_se(path.name)
        if se is None:
            # Try the path stem stripped of extra suffix
            se = parse_se(path.stem)
        if se is None:
            skipped += 1
            continue
        season, episode = se
        show_norm = common.normalize_show_name(show)

        # Upsert metadata row.
        cur.execute(
            "INSERT OR IGNORE INTO episodes (show, show_norm, season, episode, srt_path) "
            "VALUES (?, ?, ?, ?, ?)",
            (show, show_norm, season, episode, str(path)),
        )
        # If it already existed and we're rebuilding, the DELETE above cleared it.
        # If it existed otherwise, skip re-indexing FTS.
        if cur.rowcount == 0:
            continue

        content = srt_text(path)
        if not content:
            skipped += 1
            continue

        cur.execute(
            "INSERT INTO episodes_fts (show_norm, season, episode, content) "
            "VALUES (?, ?, ?, ?)",
            (show_norm, season, episode, content),
        )
        indexed += 1

    conn.commit()
    print(f"Indexed {indexed} episodes. Skipped {skipped} files (no S/E parsed, "
          f"or empty content).")
    print(f"DB: {args.db}")


if __name__ == "__main__":
    main()
