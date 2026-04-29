#!/usr/bin/env python3
"""
plex_scan.py — Trigger a Plex library scan via the Plex HTTP API.

Useful at the end of phase 5 (after Radarr+Bazarr) to make Plex pick up the
reorganized H:\\Movies tree. Can also be used standalone any time.

Reads three keys from config.json:
    plex_url           e.g. "http://localhost:32400"
    plex_token         your X-Plex-Token (Settings → Account → Authorized
                       Devices → ... → Show Token, or find via the Plex
                       web URL when logged in)
    plex_movies_section section name (e.g. "Movies") OR numeric section id

Usage:
    py plex_scan.py --config config.json
    py plex_scan.py --config config.json --force      # full re-scan, slow
    py plex_scan.py --config config.json --list       # list sections, exit
    py plex_scan.py --config config.json --wait       # poll until done
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import requests  # type: ignore
except ImportError:
    print("ERROR: 'requests' is not installed. Run: pip install requests", file=sys.stderr)
    sys.exit(3)


VERSION = "1.0.0"


def load_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_sections(base: str, token: str, timeout: int = 15) -> List[Dict[str, str]]:
    """Return all library sections as a list of dicts: id, type, title, paths."""
    url = f"{base.rstrip('/')}/library/sections"
    r = requests.get(url, params={"X-Plex-Token": token}, timeout=timeout)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    sections: List[Dict[str, str]] = []
    for d in root.findall("Directory"):
        paths = [loc.get("path", "") for loc in d.findall("Location")]
        sections.append({
            "id": d.get("key", ""),
            "type": d.get("type", ""),
            "title": d.get("title", ""),
            "paths": " | ".join(paths),
        })
    return sections


def find_section(sections: List[Dict[str, str]], wanted: str) -> Optional[Dict[str, str]]:
    """Find a section by numeric ID or by title (case-insensitive)."""
    if not wanted:
        return None
    if wanted.isdigit():
        for s in sections:
            if s["id"] == wanted:
                return s
        return None
    wl = wanted.strip().lower()
    for s in sections:
        if s["title"].lower() == wl:
            return s
    return None


def trigger_scan(base: str, token: str, section_id: str,
                 force: bool = False, timeout: int = 15) -> int:
    """Kick off a refresh on the given section.

    `force=False` (default): incremental scan — fast, picks up changes since
    last scan. `force=True`: full re-analyze of every file — slow but
    thorough.
    """
    url = f"{base.rstrip('/')}/library/sections/{section_id}/refresh"
    params: Dict[str, Any] = {"X-Plex-Token": token}
    if force:
        params["force"] = "1"
    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.status_code


def is_section_scanning(base: str, token: str, section_id: str,
                        timeout: int = 15) -> bool:
    """Check whether a section currently has a scan in progress."""
    url = f"{base.rstrip('/')}/library/sections/{section_id}"
    r = requests.get(url, params={"X-Plex-Token": token}, timeout=timeout)
    if r.status_code != 200:
        return False
    root = ET.fromstring(r.content)
    refreshing = root.get("refreshing")
    return str(refreshing).lower() == "1"


def wait_for_idle(base: str, token: str, section_id: str,
                  poll_seconds: int = 10, max_minutes: int = 120) -> bool:
    """Poll the section until refreshing=0. Returns True on completion."""
    deadline = time.time() + max_minutes * 60
    last_print = 0.0
    while time.time() < deadline:
        try:
            scanning = is_section_scanning(base, token, section_id)
        except requests.RequestException as e:
            print(f"  poll error: {e}", file=sys.stderr)
            time.sleep(poll_seconds)
            continue
        if not scanning:
            return True
        now = time.time()
        if now - last_print > 30:
            print(f"  ... still scanning ({int((now - (deadline - max_minutes*60))/60)}m elapsed)")
            last_print = now
        time.sleep(poll_seconds)
    return False


def cmd_list(cfg: Dict[str, Any]) -> int:
    base = cfg["plex_url"]
    token = cfg["plex_token"]
    try:
        sections = get_sections(base, token)
    except requests.RequestException as e:
        print(f"ERROR: could not reach Plex at {base}: {e}", file=sys.stderr)
        return 4
    print(f"{'ID':>4}  {'TYPE':<8}  {'TITLE':<40}  PATHS")
    print("-" * 100)
    for s in sections:
        print(f"{s['id']:>4}  {s['type']:<8}  {s['title']:<40}  {s['paths']}")
    return 0


def cmd_scan(cfg: Dict[str, Any], args: argparse.Namespace) -> int:
    base = cfg["plex_url"]
    token = cfg["plex_token"]
    wanted = str(cfg.get("plex_movies_section", "Movies"))

    try:
        sections = get_sections(base, token)
    except requests.RequestException as e:
        print(f"ERROR: could not reach Plex at {base}: {e}", file=sys.stderr)
        print("Check that the plex_url is correct and Plex is reachable from this machine.",
              file=sys.stderr)
        return 4

    section = find_section(sections, wanted)
    if section is None:
        print(f"ERROR: no section matching {wanted!r}. Available sections:",
              file=sys.stderr)
        for s in sections:
            print(f"  id={s['id']}  type={s['type']}  title={s['title']!r}",
                  file=sys.stderr)
        return 5

    print(f"Triggering {'FULL' if args.force else 'incremental'} scan on "
          f"section id={section['id']} title={section['title']!r}")
    try:
        code = trigger_scan(base, token, section["id"], force=args.force)
    except requests.RequestException as e:
        print(f"ERROR: scan trigger failed: {e}", file=sys.stderr)
        return 4
    print(f"Scan accepted (HTTP {code}).")

    if args.wait:
        print("Waiting for scan to complete (poll every 10s, max 2h) ...")
        ok = wait_for_idle(base, token, section["id"])
        if ok:
            print("Scan complete.")
            return 0
        print("Timed out waiting for scan to finish (kept running in Plex).",
              file=sys.stderr)
        return 6

    print("Scan running in background. Use --wait next time, or check Plex UI.")
    return 0


def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="plex_scan.py",
        description="Trigger a Plex library scan via HTTP API.",
    )
    p.add_argument("--config", default="config.json",
                   help="path to config.json containing plex_url/plex_token/plex_movies_section")
    p.add_argument("--list", action="store_true",
                   help="list all library sections and exit")
    p.add_argument("--force", action="store_true",
                   help="force a full re-analyze (slow); without this, runs an incremental scan")
    p.add_argument("--wait", action="store_true",
                   help="poll until the scan finishes (otherwise return immediately)")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        cfg = load_config(Path(args.config))
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 3
    for required in ("plex_url", "plex_token"):
        if not cfg.get(required):
            print(f"Config missing required key: {required}", file=sys.stderr)
            print('Add to config.json:', file=sys.stderr)
            print('  "plex_url": "http://localhost:32400",', file=sys.stderr)
            print('  "plex_token": "YOUR_TOKEN_HERE",', file=sys.stderr)
            print('  "plex_movies_section": "Movies"', file=sys.stderr)
            return 3
    if args.list:
        return cmd_list(cfg)
    return cmd_scan(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
