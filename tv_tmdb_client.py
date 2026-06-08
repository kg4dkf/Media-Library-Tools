#!/usr/bin/env python3
"""
tv_tmdb_client.py - Phase 6 TMDB client for the TV pipeline.

Wraps the four TMDB endpoints inventory_tv.py needs:
    /search/tv
    /tv/{id}                       (with append_to_response=external_ids)
    /tv/{id}/season/{N}
    /find/{imdb_id}                (external_source=imdb_id)

Reuses the movie pipeline's caching pattern from inventory.py / execute.py:
    * sqlite-backed response cache, keyed by (path + sorted JSON params)
    * sliding-window rate limiter (conservative 35 req / 10 s vs. TMDB's 40/10)
    * 404 cached as a sentinel so re-runs don't re-query missing entries
    * 429 retry honoring the Retry-After header
    * Errors return {"__error__": "..."} rather than raising; callers can
      decide whether to retry or fall through

Per phase6_design.md §11, expected call volume on a fresh ~888-series
library: 888 series × ~10 season calls = ~8,880 API calls worst case, all
cached after the first run. The rate limiter ensures we stay under TMDB's
40-per-10-seconds budget even on cold runs.

Public API:
    TmdbTvClient(api_key, cache_path, language="en-US", *, session=None)
        .search_tv(query, *, first_air_date_year=None) -> Dict
        .tv_details(tmdb_id, *, append_to_response="external_ids") -> Dict
        .tv_season(tmdb_id, season_number) -> Dict
        .find_by_imdb(imdb_id) -> Dict
        .close() -> None
        .last_response_was_cached: bool   # diagnostic for testing/logging

    TmdbTvClient.from_config(config_path, *, cache_path=None, session=None)
        Construct from a movie-pipeline-style config.json. Reads
        tmdb_api_key + language; derives cache_path from mediaprep_dir
        when not overridden. Lets the TV pipeline share credentials with
        the existing movie pipeline without copy-pasting the API key.

    episode_from_season(season_data, episode_number) -> Optional[Dict]
        Module-level helper for picking a specific episode out of a
        /tv/{id}/season/{N} response.

Tests can inject a fake session via the `session=` keyword argument; see
test_tv_tmdb_client.py for a worked example.

CLI smoke test:
    # Preferred: read credentials from config.json
    py tv_tmdb_client.py --config config.json search "Doctor Who"
    py tv_tmdb_client.py --config config.json details 57243

    # Or pass credentials explicitly
    py tv_tmdb_client.py --api-key <KEY> --cache /tmp/cache.sqlite \\
        search "Doctor Who"
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


SCRIPT_VERSION = "1.0.0"

TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_RATE_LIMIT = 35       # requests per window (conservative; TMDB's limit is 40/10s)
TMDB_RATE_WINDOW = 10.0    # seconds
TMDB_TIMEOUT = 20

# Sentinels in cached responses so the consumer can distinguish "TMDB said
# no" from "TMDB returned a real result with empty fields".
STATUS_NOT_FOUND = "__status__"
ERROR_KEY = "__error__"

log = logging.getLogger("tv_tmdb_client")


# ---------------------------------------------------------------------------
# Default transport — wraps requests.Session if available
# ---------------------------------------------------------------------------

def _build_default_session() -> Any:
    """Return a requests.Session with a project-tagged User-Agent.

    Imported lazily so test code that injects its own session can run on
    machines without `requests` installed.
    """
    try:
        import requests
    except ImportError as e:
        raise SystemExit(
            "tv_tmdb_client requires `requests` (pip install requests). "
            "If you're running tests, inject a fake session via session=."
        ) from e
    s = requests.Session()
    s.headers.update({"User-Agent": f"tv-inventory/{SCRIPT_VERSION}"})
    return s


# ---------------------------------------------------------------------------
# Public client
# ---------------------------------------------------------------------------

class TmdbTvClient:
    """SQLite-cached, rate-limited TMDB client for the TV pipeline."""

    def __init__(
        self,
        api_key: str,
        cache_path: Path,
        language: str = "en-US",
        *,
        session: Optional[Any] = None,
    ) -> None:
        if not api_key or api_key in ("REPLACE_ME", "YOUR_KEY_HERE"):
            raise ValueError("tmdb_api_key is not set")
        self.api_key = api_key
        self.language = language
        self.session = session if session is not None else _build_default_session()

        cache_path = Path(cache_path)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.db: sqlite3.Connection = sqlite3.connect(
            str(cache_path), isolation_level=None
        )
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS cache (
                key TEXT PRIMARY KEY,
                response TEXT NOT NULL,
                ts INTEGER NOT NULL
            )
            """
        )
        self._request_times: List[float] = []
        # Diagnostic — set after each _get(); used by tests and verbose logging.
        self.last_response_was_cached: bool = False

    @classmethod
    def from_config(
        cls,
        config_path: Path,
        *,
        cache_path: Optional[Path] = None,
        session: Optional[Any] = None,
        cache_filename: str = "tmdb_tv_cache.sqlite",
    ) -> "TmdbTvClient":
        """Construct a TmdbTvClient from a movie-pipeline-style config.json.

        Reads:
            tmdb_api_key   (required)
            language       (optional, defaults to "en-US")
            mediaprep_dir  (used to derive cache_path when one isn't given)

        Args:
            config_path: Path to config.json (the same file inventory.py uses).
            cache_path: Override the cache path. If None, derived from
                mediaprep_dir + cache_filename.
            session: Inject a fake HTTP session (for testing).
            cache_filename: Override the default cache file basename.

        Raises:
            ValueError: if tmdb_api_key is unset/placeholder, or the config
                lacks mediaprep_dir and no cache_path was provided.
            FileNotFoundError / json.JSONDecodeError: on bad config file.
        """
        path = Path(config_path)
        raw = json.loads(path.read_text(encoding="utf-8"))

        api_key = str(raw.get("tmdb_api_key", "")).strip()
        if not api_key or api_key in ("REPLACE_ME", "YOUR_KEY_HERE"):
            raise ValueError(
                f"tmdb_api_key is not set in {path}; "
                f"copy config.example.json -> config.json and add your key"
            )

        language = str(raw.get("language", "en-US"))

        if cache_path is None:
            mediaprep = raw.get("mediaprep_dir")
            if not mediaprep:
                raise ValueError(
                    f"cache_path not given and mediaprep_dir is missing from {path}"
                )
            cache_path = Path(mediaprep) / cache_filename

        return cls(api_key, Path(cache_path), language=language, session=session)

    # ------------------------------------------------------------------
    # Public endpoints
    # ------------------------------------------------------------------

    def search_tv(
        self,
        query: str,
        *,
        first_air_date_year: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Search /search/tv. Returns the raw TMDB envelope (results list, etc.)."""
        params: Dict[str, Any] = {"query": query}
        if first_air_date_year:
            params["first_air_date_year"] = int(first_air_date_year)
        return self._get("/search/tv", params)

    def tv_details(
        self,
        tmdb_id: int,
        *,
        append_to_response: Optional[str] = "external_ids",
    ) -> Dict[str, Any]:
        """Fetch /tv/{id}. By default appends external_ids for the IMDB id
        (phase6_design.md §5 Stage C calls for this in a single round trip)."""
        params: Dict[str, Any] = {}
        if append_to_response:
            params["append_to_response"] = append_to_response
        return self._get(f"/tv/{int(tmdb_id)}", params)

    def tv_season(self, tmdb_id: int, season_number: int) -> Dict[str, Any]:
        """Fetch /tv/{id}/season/{N}. Response includes per-episode data for
        every episode in the season."""
        return self._get(
            f"/tv/{int(tmdb_id)}/season/{int(season_number)}",
            {},
        )

    def find_by_imdb(self, imdb_id: str) -> Dict[str, Any]:
        """Fetch /find/{imdb_id}?external_source=imdb_id.

        Caller picks `tv_results` or `movie_results` from the envelope.
        """
        return self._get(f"/find/{imdb_id}", {"external_source": "imdb_id"})

    def close(self) -> None:
        """Close the sqlite cache. Safe to call multiple times."""
        try:
            self.db.close()
        except Exception:  # pragma: no cover — defensive
            pass

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _cache_key(self, path: str, params: Dict[str, Any]) -> str:
        """Stable key for the response cache. Excludes the api_key so cached
        rows survive a key rotation; includes language so en-US and en-GB
        responses don't collide."""
        return f"{path}?{json.dumps(params, sort_keys=True, ensure_ascii=False)}"

    def _cache_get(self, key: str) -> Optional[Dict[str, Any]]:
        row = self.db.execute(
            "SELECT response FROM cache WHERE key = ?", (key,)
        ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return None

    def _cache_put(self, key: str, value: Dict[str, Any]) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO cache (key, response, ts) VALUES (?, ?, ?)",
            (key, json.dumps(value, ensure_ascii=False), int(time.time())),
        )

    def _rate_limit(self) -> None:
        """Sliding-window throttle. Sleeps if the last TMDB_RATE_LIMIT calls
        all happened within TMDB_RATE_WINDOW seconds."""
        now = time.monotonic()
        self._request_times = [
            t for t in self._request_times if now - t < TMDB_RATE_WINDOW
        ]
        if len(self._request_times) >= TMDB_RATE_LIMIT:
            wait = TMDB_RATE_WINDOW - (now - self._request_times[0]) + 0.1
            if wait > 0:
                log.debug("TMDB rate-limit sleep: %.2fs", wait)
                time.sleep(wait)
            self._request_times = []
        self._request_times.append(time.monotonic())

    def _get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Cached, rate-limited GET. Mirrors inventory.TMDBClient._get."""
        # Normalize: always inject language unless caller overrode it.
        p = dict(params)
        p.setdefault("language", self.language)

        key = self._cache_key(path, p)
        cached = self._cache_get(key)
        if cached is not None:
            self.last_response_was_cached = True
            return cached
        self.last_response_was_cached = False

        self._rate_limit()
        # Add api_key only at request time (never store in cache key).
        request_params = dict(p)
        request_params["api_key"] = self.api_key
        url = TMDB_BASE + path

        try:
            r = self.session.get(url, params=request_params, timeout=TMDB_TIMEOUT)
            if getattr(r, "status_code", None) == 429:
                retry_after = int(r.headers.get("Retry-After", "5"))
                log.warning("TMDB 429 on %s, sleeping %ds", path, retry_after)
                time.sleep(retry_after + 1)
                r = self.session.get(url, params=request_params, timeout=TMDB_TIMEOUT)
            if getattr(r, "status_code", None) == 404:
                data = {STATUS_NOT_FOUND: 404}
                self._cache_put(key, data)
                return data
            if getattr(r, "status_code", None) == 401:
                # Auth failure. Often transient: TMDB's WAF sometimes
                # returns 401 instead of 429 when bursting through
                # season-detail lookups. Retry once with a short
                # backoff, like we do for 429 — that clears most
                # transient cases. If it sticks, surface a one-time
                # clear warning and cache the failure so we don't
                # pound TMDB.
                log.debug("TMDB 401 on %s — backing off 5s and retrying once",
                          path)
                time.sleep(5)
                r = self.session.get(url, params=request_params,
                                       timeout=TMDB_TIMEOUT)
                if getattr(r, "status_code", None) == 401:
                    if not getattr(self, "_warned_401", False):
                        log.error(
                            "TMDB returned 401 Unauthorized for %s "
                            "(persisted after 5s backoff). Possible "
                            "causes: tmdb_api_key invalid; v4 read-token "
                            "pasted into v3 slot (v3 keys are ~32 "
                            "alphanumeric chars, no 'eyJ' prefix); app "
                            "suspended on the TMDB side; or sustained "
                            "rate-limit pressure. Verify at "
                            "https://www.themoviedb.org/settings/api . "
                            "Further 401s this run will be silenced and "
                            "cached.",
                            path,
                        )
                        self._warned_401 = True
                    data = {ERROR_KEY: f"401 Unauthorized: {path}"}
                    self._cache_put(key, data)
                    return data
                # Retry succeeded — fall through to normal handling.
            # Mirror requests.Response.raise_for_status() without requiring
            # the requests module to be importable at type-check time.
            if hasattr(r, "raise_for_status"):
                r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.warning("TMDB error on %s: %s", path, e)
            return {ERROR_KEY: str(e)}

        self._cache_put(key, data)
        return data


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def episode_from_season(
    season_data: Dict[str, Any],
    episode_number: int,
) -> Optional[Dict[str, Any]]:
    """Pull a specific episode out of a /tv/{id}/season/{N} response."""
    if not season_data:
        return None
    episodes = season_data.get("episodes")
    if not isinstance(episodes, list):
        return None
    for ep in episodes:
        if isinstance(ep, dict) and ep.get("episode_number") == episode_number:
            return ep
    return None


def is_error(response: Dict[str, Any]) -> bool:
    """True if the response is an error sentinel (network failure)."""
    return ERROR_KEY in (response or {})


def is_not_found(response: Dict[str, Any]) -> bool:
    """True if the response is a 404 sentinel."""
    return (response or {}).get(STATUS_NOT_FOUND) == 404


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="TMDB TV client smoke test (requires a real API key).",
        epilog=(
            "Provide credentials EITHER via --config <path-to-config.json> "
            "(reads tmdb_api_key, language, and derives the cache path from "
            "mediaprep_dir), OR via --api-key + --cache directly."
        ),
    )
    cred = ap.add_mutually_exclusive_group(required=True)
    cred.add_argument("--config", type=Path,
                      help="Path to config.json (reads tmdb_api_key, language, mediaprep_dir)")
    cred.add_argument("--api-key", help="TMDB v3 auth key (when not using --config)")
    ap.add_argument("--cache", type=Path, default=None,
                    help="Override sqlite cache path. Required with --api-key; "
                         "optional with --config (defaults to mediaprep_dir/tmdb_tv_cache.sqlite).")
    ap.add_argument("--language", default=None,
                    help="Override the language. Defaults to config's language, "
                         "or en-US if --api-key without --config.")
    ap.add_argument("--verbose", "-v", action="store_true")

    sub = ap.add_subparsers(dest="cmd", required=True)

    sp_search = sub.add_parser("search", help="search /search/tv")
    sp_search.add_argument("query")
    sp_search.add_argument("--year", type=int, default=None)

    sp_details = sub.add_parser("details", help="fetch /tv/{id}")
    sp_details.add_argument("tmdb_id", type=int)

    sp_season = sub.add_parser("season", help="fetch /tv/{id}/season/{N}")
    sp_season.add_argument("tmdb_id", type=int)
    sp_season.add_argument("season_number", type=int)

    sp_find = sub.add_parser("find-imdb", help="fetch /find/{imdb_id}")
    sp_find.add_argument("imdb_id")

    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    if args.config:
        client = TmdbTvClient.from_config(
            args.config,
            cache_path=args.cache,
            language=args.language,
        )
    else:
        client = TmdbTvClient(
            api_key=args.api_key,
            cache_path=args.cache,
            language=args.language,
        )

    try:
        if args.tmdb_id:
            data = client.tv_details(args.tmdb_id)
        elif args.find_imdb:
            data = client.find_by_imdb(args.find_imdb)
        elif args.query:
            data = client.search_tv(args.query, first_air_date_year=args.year)
        else:
            ap.error("must provide --tmdb-id, --query, or --find-imdb")
            return 2
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
