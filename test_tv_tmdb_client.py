#!/usr/bin/env python3
"""
test_tv_tmdb_client.py - Unit tests for tv_tmdb_client.py.

Uses a FakeSession to avoid real HTTP. Tests cover:
    * each endpoint's URL construction
    * cache hits on repeated calls
    * 404 caching (sentinel)
    * 429 retry honoring Retry-After
    * exception fallback ({"__error__": ...})
    * language parameter injection
    * api_key omission from cache key (so key rotation doesn't break cache)
    * episode_from_season helper
    * is_error / is_not_found helpers

Run:
    py -3.13 test_tv_tmdb_client.py
    py -3.13 -m unittest test_tv_tmdb_client
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from tv_tmdb_client import (
    ERROR_KEY,
    STATUS_NOT_FOUND,
    TMDB_BASE,
    TmdbTvClient,
    episode_from_season,
    is_error,
    is_not_found,
)


# ---------------------------------------------------------------------------
# Fake HTTP session
# ---------------------------------------------------------------------------

class FakeResponse:
    """Minimal subset of requests.Response that TmdbTvClient touches."""

    def __init__(
        self,
        json_body: Optional[Dict[str, Any]] = None,
        *,
        status_code: int = 200,
        headers: Optional[Dict[str, str]] = None,
        raise_on_status: bool = True,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self._json = json_body
        self._raise_on_status = raise_on_status

    def json(self) -> Dict[str, Any]:
        if self._json is None:
            raise ValueError("FakeResponse has no JSON body")
        return self._json

    def raise_for_status(self) -> None:
        if self._raise_on_status and self.status_code >= 400 and self.status_code != 404:
            # 404 is handled before raise_for_status in the client.
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    """Records each call and returns canned responses in order.

    Pass `responses` as a list; each .get() pops the head. If the list runs
    dry, raises AssertionError so tests fail loudly on unexpected extra
    requests.
    """

    def __init__(self, responses: List[FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: List[Tuple[str, Dict[str, Any], Optional[float]]] = []

    def get(self, url: str, params: Dict[str, Any], timeout: float) -> FakeResponse:
        self.calls.append((url, dict(params), timeout))
        if not self._responses:
            raise AssertionError(
                f"FakeSession ran out of responses on call {len(self.calls)}: "
                f"url={url!r}, params={params!r}"
            )
        return self._responses.pop(0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client(
    responses: List[FakeResponse],
    *,
    api_key: str = "test-key",
    language: str = "en-US",
) -> Tuple[TmdbTvClient, FakeSession, Path]:
    """Build a TmdbTvClient backed by a temp sqlite cache and a FakeSession.

    Returns (client, fake_session, cache_path) so tests can introspect both.
    """
    tmp = Path(tempfile.mkdtemp(prefix="tv_tmdb_test_"))
    cache = tmp / "cache.sqlite"
    fake = FakeSession(responses)
    client = TmdbTvClient(api_key, cache, language=language, session=fake)
    return client, fake, cache


# ---------------------------------------------------------------------------
# Construction / validation
# ---------------------------------------------------------------------------

class TestConstruction(unittest.TestCase):
    def test_rejects_empty_api_key(self):
        with self.assertRaises(ValueError):
            TmdbTvClient("", Path("/tmp/x.sqlite"), session=FakeSession([]))

    def test_rejects_placeholder_api_key(self):
        with self.assertRaises(ValueError):
            TmdbTvClient("REPLACE_ME", Path("/tmp/x.sqlite"), session=FakeSession([]))

    def test_creates_cache_table(self):
        client, _, cache_path = _make_client([])
        self.assertTrue(cache_path.exists())
        rows = client.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        self.assertIn(("cache",), rows)

    def test_close_is_idempotent(self):
        client, _, _ = _make_client([])
        client.close()
        client.close()  # must not raise


# ---------------------------------------------------------------------------
# Endpoint URL construction
# ---------------------------------------------------------------------------

class TestEndpoints(unittest.TestCase):
    def test_search_tv(self):
        client, fake, _ = _make_client([
            FakeResponse({"page": 1, "results": [{"id": 57243, "name": "Doctor Who"}]}),
        ])
        result = client.search_tv("Doctor Who", first_air_date_year=2005)
        self.assertEqual(result["results"][0]["name"], "Doctor Who")
        url, params, _ = fake.calls[0]
        self.assertEqual(url, TMDB_BASE + "/search/tv")
        self.assertEqual(params["query"], "Doctor Who")
        self.assertEqual(params["first_air_date_year"], 2005)
        self.assertEqual(params["language"], "en-US")
        self.assertEqual(params["api_key"], "test-key")

    def test_search_tv_without_year(self):
        client, fake, _ = _make_client([FakeResponse({"results": []})])
        client.search_tv("Foo")
        _, params, _ = fake.calls[0]
        self.assertNotIn("first_air_date_year", params)

    def test_tv_details_appends_external_ids(self):
        client, fake, _ = _make_client([
            FakeResponse({"id": 57243, "name": "Doctor Who",
                          "external_ids": {"imdb_id": "tt0436992"}}),
        ])
        data = client.tv_details(57243)
        self.assertEqual(data["external_ids"]["imdb_id"], "tt0436992")
        url, params, _ = fake.calls[0]
        self.assertEqual(url, TMDB_BASE + "/tv/57243")
        self.assertEqual(params["append_to_response"], "external_ids")

    def test_tv_details_can_disable_append(self):
        client, fake, _ = _make_client([FakeResponse({"id": 1})])
        client.tv_details(1, append_to_response=None)
        _, params, _ = fake.calls[0]
        self.assertNotIn("append_to_response", params)

    def test_tv_season(self):
        client, fake, _ = _make_client([
            FakeResponse({
                "season_number": 1,
                "episodes": [{"episode_number": 1, "name": "Rose"}],
            }),
        ])
        data = client.tv_season(57243, 1)
        self.assertEqual(data["episodes"][0]["name"], "Rose")
        url, _, _ = fake.calls[0]
        self.assertEqual(url, TMDB_BASE + "/tv/57243/season/1")

    def test_find_by_imdb(self):
        client, fake, _ = _make_client([
            FakeResponse({"tv_results": [{"id": 57243}], "movie_results": []}),
        ])
        data = client.find_by_imdb("tt0436992")
        self.assertEqual(data["tv_results"][0]["id"], 57243)
        url, params, _ = fake.calls[0]
        self.assertEqual(url, TMDB_BASE + "/find/tt0436992")
        self.assertEqual(params["external_source"], "imdb_id")


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------

class TestCaching(unittest.TestCase):
    def test_repeated_call_hits_cache(self):
        client, fake, _ = _make_client([
            FakeResponse({"results": [{"id": 1}]}),
        ])
        client.search_tv("Foo")
        self.assertFalse(client.last_response_was_cached)
        # Second call must NOT hit the network — FakeSession would raise.
        client.search_tv("Foo")
        self.assertTrue(client.last_response_was_cached)
        self.assertEqual(len(fake.calls), 1)

    def test_different_params_miss_cache(self):
        client, fake, _ = _make_client([
            FakeResponse({"results": []}),
            FakeResponse({"results": []}),
        ])
        client.search_tv("Foo")
        client.search_tv("Bar")
        self.assertEqual(len(fake.calls), 2)
        self.assertFalse(client.last_response_was_cached)

    def test_api_key_not_in_cache_key(self):
        # Cache must survive an API key rotation. We seed with one key and
        # then construct a new client pointed at the same cache file with a
        # different key — the cached row must still be returned.
        client1, _, cache_path = _make_client([FakeResponse({"results": [42]})])
        client1.search_tv("Foo")
        client1.close()

        # New client, new key, same cache file, NO new responses queued.
        # The cache hit must satisfy the request.
        empty_session = FakeSession([])
        client2 = TmdbTvClient("rotated-key", cache_path, session=empty_session)
        result = client2.search_tv("Foo")
        self.assertEqual(result["results"], [42])
        self.assertTrue(client2.last_response_was_cached)
        self.assertEqual(len(empty_session.calls), 0)
        client2.close()

    def test_language_in_cache_key(self):
        # en-US and en-GB responses must NOT collide in cache.
        client_us, _, cache_path = _make_client([
            FakeResponse({"results": ["us"]}),
        ], language="en-US")
        client_us.search_tv("Foo")
        client_us.close()

        # Different language => cache miss => needs a fresh response.
        gb_session = FakeSession([FakeResponse({"results": ["gb"]})])
        client_gb = TmdbTvClient("test-key", cache_path, language="en-GB",
                                 session=gb_session)
        result = client_gb.search_tv("Foo")
        self.assertEqual(result["results"], ["gb"])
        self.assertEqual(len(gb_session.calls), 1)
        client_gb.close()


# ---------------------------------------------------------------------------
# 404 / error / 429 handling
# ---------------------------------------------------------------------------

class TestErrorHandling(unittest.TestCase):
    def test_404_returns_sentinel(self):
        client, fake, _ = _make_client([
            FakeResponse(status_code=404, raise_on_status=False),
        ])
        result = client.tv_details(999_999_999)
        self.assertTrue(is_not_found(result))
        self.assertEqual(result[STATUS_NOT_FOUND], 404)
        self.assertEqual(len(fake.calls), 1)

    def test_404_is_cached(self):
        # A 404 must not re-query on the second call.
        client, fake, _ = _make_client([
            FakeResponse(status_code=404, raise_on_status=False),
        ])
        client.tv_details(999_999_999)
        # Second call: FakeSession has no more responses; cache hit must satisfy.
        result2 = client.tv_details(999_999_999)
        self.assertTrue(is_not_found(result2))
        self.assertTrue(client.last_response_was_cached)
        self.assertEqual(len(fake.calls), 1)

    def test_429_retry_with_retry_after(self):
        # First response 429, second 200. The client should sleep + retry.
        # Patch time.sleep so the test doesn't actually wait.
        import tv_tmdb_client as mod
        sleeps: List[float] = []
        original_sleep = mod.time.sleep
        mod.time.sleep = lambda s: sleeps.append(s)
        try:
            client, fake, _ = _make_client([
                FakeResponse(status_code=429, headers={"Retry-After": "2"},
                             raise_on_status=False),
                FakeResponse({"results": [{"id": 1}]}),
            ])
            data = client.search_tv("Foo")
            self.assertEqual(data["results"][0]["id"], 1)
            self.assertEqual(len(fake.calls), 2)
            # Should have slept ~3s (Retry-After + 1 buffer).
            self.assertTrue(any(s >= 2.0 for s in sleeps), f"sleeps={sleeps}")
        finally:
            mod.time.sleep = original_sleep

    def test_exception_returns_error_sentinel(self):
        # Simulate a network failure by having the FakeSession raise.
        class ExplodingSession:
            def __init__(self): self.calls = 0
            def get(self, *args, **kwargs):
                self.calls += 1
                raise RuntimeError("connection refused")

        tmp = Path(tempfile.mkdtemp(prefix="tv_tmdb_err_"))
        client = TmdbTvClient("k", tmp / "c.sqlite", session=ExplodingSession())
        result = client.search_tv("Foo")
        self.assertTrue(is_error(result))
        self.assertIn("connection refused", result[ERROR_KEY])

    def test_error_is_NOT_cached(self):
        # Errors should be transient — re-running should retry.
        class CountingSession:
            def __init__(self):
                self.calls = 0
            def get(self, *args, **kwargs):
                self.calls += 1
                raise RuntimeError("net down")

        tmp = Path(tempfile.mkdtemp(prefix="tv_tmdb_err_"))
        sess = CountingSession()
        client = TmdbTvClient("k", tmp / "c.sqlite", session=sess)
        client.search_tv("Foo")
        client.search_tv("Foo")
        self.assertEqual(sess.calls, 2)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

class TestEpisodeFromSeason(unittest.TestCase):
    def test_finds_episode(self):
        season = {
            "episodes": [
                {"episode_number": 1, "name": "Rose"},
                {"episode_number": 2, "name": "The End of the World"},
                {"episode_number": 3, "name": "The Unquiet Dead"},
            ],
        }
        ep = episode_from_season(season, 2)
        self.assertEqual(ep["name"], "The End of the World")

    def test_missing_episode(self):
        season = {"episodes": [{"episode_number": 1, "name": "Rose"}]}
        self.assertIsNone(episode_from_season(season, 99))

    def test_empty_season(self):
        self.assertIsNone(episode_from_season({"episodes": []}, 1))

    def test_malformed(self):
        self.assertIsNone(episode_from_season({}, 1))
        self.assertIsNone(episode_from_season({"episodes": "not a list"}, 1))
        self.assertIsNone(episode_from_season(None, 1))


class TestSentinelHelpers(unittest.TestCase):
    def test_is_error(self):
        self.assertTrue(is_error({ERROR_KEY: "boom"}))
        self.assertFalse(is_error({"results": []}))
        self.assertFalse(is_error({}))
        self.assertFalse(is_error(None))

    def test_is_not_found(self):
        self.assertTrue(is_not_found({STATUS_NOT_FOUND: 404}))
        self.assertFalse(is_not_found({"id": 1}))
        self.assertFalse(is_not_found({}))



# ---------------------------------------------------------------------------
# from_config: load credentials from a movie-pipeline-style config.json
# ---------------------------------------------------------------------------

class TestFromConfig(unittest.TestCase):
    def _write_config(self, **overrides) -> Path:
        """Write a temp config.json with the given keys; return its path."""
        tmp = Path(tempfile.mkdtemp(prefix="tv_tmdb_cfg_"))
        cfg = {
            "tmdb_api_key": "real-key-32-hex",
            "language": "en-US",
            "mediaprep_dir": str(tmp / "_mediaprep"),
            "movies_root": "H:/Movies",
        }
        cfg.update(overrides)
        path = tmp / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_loads_api_key_and_language(self):
        cfg_path = self._write_config(language="en-GB")
        client = TmdbTvClient.from_config(cfg_path, session=FakeSession([]))
        self.assertEqual(client.api_key, "real-key-32-hex")
        self.assertEqual(client.language, "en-GB")
        client.close()

    def test_derives_cache_path_from_mediaprep_dir(self):
        cfg_path = self._write_config()
        client = TmdbTvClient.from_config(cfg_path, session=FakeSession([]))
        # Cache file should live under mediaprep_dir as tmdb_tv_cache.sqlite.
        cfg = json.loads(cfg_path.read_text())
        expected = Path(cfg["mediaprep_dir"]) / "tmdb_tv_cache.sqlite"
        self.assertTrue(expected.exists(),
                        f"expected cache file at {expected}")
        client.close()

    def test_explicit_cache_path_override(self):
        cfg_path = self._write_config()
        override = cfg_path.parent / "custom_cache.sqlite"
        client = TmdbTvClient.from_config(
            cfg_path, cache_path=override, session=FakeSession([])
        )
        self.assertTrue(override.exists())
        client.close()

    def test_custom_cache_filename(self):
        cfg_path = self._write_config()
        client = TmdbTvClient.from_config(
            cfg_path, session=FakeSession([]), cache_filename="my_cache.sqlite"
        )
        cfg = json.loads(cfg_path.read_text())
        expected = Path(cfg["mediaprep_dir"]) / "my_cache.sqlite"
        self.assertTrue(expected.exists())
        client.close()

    def test_default_language_when_missing_from_config(self):
        cfg_path = self._write_config()
        # Rewrite without the language key
        raw = json.loads(cfg_path.read_text())
        del raw["language"]
        cfg_path.write_text(json.dumps(raw), encoding="utf-8")

        client = TmdbTvClient.from_config(cfg_path, session=FakeSession([]))
        self.assertEqual(client.language, "en-US")
        client.close()

    def test_rejects_placeholder_api_key(self):
        cfg_path = self._write_config(tmdb_api_key="REPLACE_ME")
        with self.assertRaises(ValueError) as ctx:
            TmdbTvClient.from_config(cfg_path, session=FakeSession([]))
        self.assertIn("tmdb_api_key", str(ctx.exception))

    def test_rejects_empty_api_key(self):
        cfg_path = self._write_config(tmdb_api_key="")
        with self.assertRaises(ValueError):
            TmdbTvClient.from_config(cfg_path, session=FakeSession([]))

    def test_strips_api_key_whitespace(self):
        cfg_path = self._write_config(tmdb_api_key="  spaced-key  ")
        client = TmdbTvClient.from_config(cfg_path, session=FakeSession([]))
        self.assertEqual(client.api_key, "spaced-key")
        client.close()

    def test_missing_mediaprep_dir_without_override_raises(self):
        cfg_path = self._write_config()
        # Strip mediaprep_dir.
        raw = json.loads(cfg_path.read_text())
        del raw["mediaprep_dir"]
        cfg_path.write_text(json.dumps(raw), encoding="utf-8")

        with self.assertRaises(ValueError) as ctx:
            TmdbTvClient.from_config(cfg_path, session=FakeSession([]))
        self.assertIn("mediaprep_dir", str(ctx.exception))

    def test_missing_mediaprep_dir_with_explicit_cache_works(self):
        # mediaprep_dir absent BUT cache_path explicit — should succeed.
        cfg_path = self._write_config()
        raw = json.loads(cfg_path.read_text())
        del raw["mediaprep_dir"]
        cfg_path.write_text(json.dumps(raw), encoding="utf-8")

        cache = cfg_path.parent / "explicit_cache.sqlite"
        client = TmdbTvClient.from_config(
            cfg_path, cache_path=cache, session=FakeSession([])
        )
        self.assertTrue(cache.exists())
        client.close()

    def test_nonexistent_config_path(self):
        with self.assertRaises(FileNotFoundError):
            TmdbTvClient.from_config(
                Path("/tmp/does-not-exist-12345.json"),
                session=FakeSession([]),
            )

    def test_malformed_config_json(self):
        tmp = Path(tempfile.mkdtemp(prefix="tv_tmdb_bad_"))
        bad = tmp / "config.json"
        bad.write_text("this is not json", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            TmdbTvClient.from_config(bad, session=FakeSession([]))



if __name__ == "__main__":
    unittest.main(verbosity=2)
