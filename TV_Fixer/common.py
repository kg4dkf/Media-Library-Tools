"""
Shared utilities: ffmpeg/ffprobe wrappers, file walking, db helpers.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

VIDEO_EXTS = {
    ".mkv", ".mp4", ".avi", ".mov", ".m4v", ".ts", ".m2ts",
    ".mpg", ".mpeg", ".wmv", ".flv", ".webm", ".vob", ".divx",
}

# Plex / Jellyfin / Sonarr extras subdirectories that contain trailers,
# featurettes, behind-the-scenes etc. -- NOT real episodes. iter_videos
# prunes these so prepare.py / identify.py / dedupe.py don't waste cycles
# on 30-second YouTube clips. NOTE: 'Specials' is intentionally NOT in this
# list -- Plex uses it for Season 0 episodes (Christmas specials, etc.),
# which ARE legitimate episodes.
EXTRAS_DIRS = {
    "Trailers", "Featurettes", "Behind The Scenes", "Deleted Scenes",
    "Clips", "Other", "Bloopers", "Interviews", "Scenes", "Shorts",
    "Extras",
}


def require_tool(name: str) -> str:
    """Resolve a tool name to an absolute path, exiting with a clear error if missing."""
    path = shutil.which(name)
    if not path:
        sys.stderr.write(
            f"ERROR: required external tool '{name}' not found on PATH.\n"
            f"Install it and ensure it resolves before re-running.\n"
        )
        sys.exit(2)
    return path


def iter_videos(root: Path, skip_extras: bool = True) -> Iterator[Path]:
    """Yield video files under root, sorted for determinism.

    With skip_extras=True (default), skips Plex-style extras subdirectories
    (Trailers, Featurettes, Clips, etc.) so callers don't accidentally
    process trailer files as if they were episodes.
    """
    for dirpath, dirnames, filenames in os.walk(root):
        if skip_extras:
            # Mutate dirnames in place to prune the os.walk traversal.
            dirnames[:] = [d for d in dirnames if d not in EXTRAS_DIRS]
        for name in sorted(filenames):
            p = Path(dirpath) / name
            if p.suffix.lower() in VIDEO_EXTS:
                yield p


@dataclass
class MediaInfo:
    duration: float       # seconds
    width: int
    height: int
    vbitrate: int         # bits/sec, 0 if unknown
    size: int             # bytes


def probe(path: Path, ffprobe: str = "ffprobe") -> Optional[MediaInfo]:
    """Run ffprobe and return MediaInfo, or None on failure.

    Captures stdout as bytes and decodes UTF-8 with error replacement so
    non-ASCII bytes in filenames or metadata tags don't trigger a cp1252
    decode failure inside subprocess's reader thread on Windows.
    """
    try:
        res = subprocess.run(
            [
                ffprobe, "-v", "error", "-print_format", "json",
                "-show_format", "-show_streams", str(path),
            ],
            capture_output=True, timeout=60, check=True,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    raw = res.stdout
    if not raw:
        return None
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None

    fmt = data.get("format", {}) or {}
    duration = float(fmt.get("duration", 0) or 0)
    size = int(fmt.get("size", 0) or 0)

    width = height = 0
    vbitrate = 0
    for s in data.get("streams", []) or []:
        if s.get("codec_type") == "video":
            width = int(s.get("width", 0) or 0)
            height = int(s.get("height", 0) or 0)
            br = s.get("bit_rate") or fmt.get("bit_rate") or 0
            try:
                vbitrate = int(br)
            except (TypeError, ValueError):
                vbitrate = 0
            break

    return MediaInfo(duration=duration, width=width, height=height,
                     vbitrate=vbitrate, size=size)


def extract_audio_segment(
    path: Path,
    offset: float,
    length: float,
    sample_rate: int = 16000,
    mono: bool = True,
    ffmpeg: str = "ffmpeg",
    out_path: Optional[Path] = None,
) -> Optional[bytes]:
    """
    Extract a PCM-WAV segment from path starting at `offset` for `length` seconds.

    If out_path is given, writes to that file and returns None.
    Otherwise returns the WAV bytes (no header issues — ffmpeg writes a real WAV).
    """
    cmd = [
        ffmpeg, "-v", "error", "-nostdin", "-y",
        "-ss", f"{offset:.3f}",
        "-i", str(path),
        "-t", f"{length:.3f}",
        "-vn",
        "-ac", "1" if mono else "2",
        "-ar", str(sample_rate),
        "-f", "wav",
    ]
    if out_path is not None:
        cmd.append(str(out_path))
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=300)
            return None
        except (subprocess.SubprocessError, OSError):
            return None
    else:
        cmd.append("pipe:1")
        try:
            res = subprocess.run(cmd, check=True, capture_output=True, timeout=300)
            return res.stdout
        except (subprocess.SubprocessError, OSError):
            return None


def compute_offset(duration: float, pct: float, length: float, min_skip: float = 30.0) -> float:
    """
    Pick a sensible starting offset for an audio sample.

    Skips `pct` fraction of the file (at least min_skip seconds to clear cold opens
    and intros) and ensures the sample window fits inside the file.
    """
    if duration <= 0:
        return 0.0
    skip = max(min_skip, duration * pct)
    # Keep at least `length` seconds left in the file after the offset.
    max_offset = max(0.0, duration - length - 1.0)
    return min(skip, max_offset)


def normalize_show_name(name: str) -> str:
    """Cheap normalization for show-folder lookup keys."""
    out = []
    for ch in name.lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in " -_:":
            out.append(" ")
    return " ".join("".join(out).split())
