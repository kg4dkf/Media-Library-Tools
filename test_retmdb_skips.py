#!/usr/bin/env python3
"""Tests for retmdb_skips.py — offline logic + propose/commit with FakeSession."""

from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).parent))

from retmdb_skips import (
    TmdbCandidate,
    Verdict,
    assess,
    clean_folder_name,
    extract_show_hint_from_filename,
    name_similarity,
    phase_commit,
    phase_propose,
    render_target_folder,
    score_candidates,
    show_hint_from_folder,
)
from tv_tmdb_client import TmdbTvClient


# =========================================================================
# Fake TMDB session reused from test_execute_tv pattern
# =========================================================================

class FakeResponse:
    def __init__(self, body=None, *, status_code=200, headers=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._json = body
    def json(self): return self._json
    def raise_for_status(self):
        if self.status_code >= 400 and self.status_code != 404:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, lookup):
        # lookup: dict mapping query_string -> response_body
        self._lookup = lookup
        self.calls: List[Tuple[str, Dict[str, Any]]] = []

    def get(self, url, params, timeout):
        self.calls.append((url, dict(params)))
        q = params.get("query", "")
        body = self._lookup.get(q, {"results": []})
        return FakeResponse(body)


# =========================================================================
# Folder name cleanup
# =========================================================================

class TestCleanFolderName(unittest.TestCase):
    def test_strip_year(self):
        s, y = clean_folder_name("Mr. Show (1995)")
        self.assertEqual(s, "Mr Show")
        self.assertEqual(y, 1995)

    def test_strip_brackets(self):
        s, _ = clean_folder_name("[ax]_ranma_-_complete_dvd_rip_[a5d9364b]")
        self.assertNotIn("[", s)
        self.assertNotIn("ax", s.lower())  # bracketed tag dropped
        self.assertIn("ranma", s.lower())

    def test_strip_release_quality(self):
        s, _ = clean_folder_name("The X Files Complete Series 1080p BluRay x264")
        self.assertNotIn("complete", s.lower())
        self.assertNotIn("1080p", s.lower())
        self.assertNotIn("bluray", s.lower())
        self.assertIn("X Files", s)

    def test_strip_season_marker(self):
        s, _ = clean_folder_name("Underdog Season 1-3")
        self.assertIn("Underdog", s)
        self.assertNotIn("Season", s)

    def test_strip_year_range(self):
        s, y = clean_folder_name("Some Show (2003-2007)")
        self.assertEqual(y, 2003)
        self.assertIn("Some Show", s)

    def test_no_changes_needed(self):
        s, y = clean_folder_name("Friends")
        self.assertEqual(s, "Friends")
        self.assertIsNone(y)

    def test_year_only_input_returns_empty(self):
        # Real-world bug: clean_folder_name("2002") was returning "2002",
        # which became the TMDB query and returned junk results.
        s, _ = clean_folder_name("2002")
        self.assertEqual(s, "")

    def test_paren_year_only_input_returns_empty(self):
        s, y = clean_folder_name("(2002)")
        self.assertEqual(s, "")
        self.assertEqual(y, 2002)

    def test_short_digit_kept_in_title(self):
        # 1-3 digit numbers are part of legit titles (e.g., "30 Rock").
        s, _ = clean_folder_name("30 Rock")
        self.assertIn("30", s)
        self.assertIn("Rock", s)

    def test_torrent_suffix(self):
        s, _ = clean_folder_name(
            "Teenage Mutant Ninja Turtles PackDvDrip aXXo (2007)")
        self.assertIn("Teenage Mutant Ninja Turtles", s)
        self.assertNotIn("aXXo", s)
        self.assertNotIn("PackDvDrip".lower(), s.lower())


class TestExtractShowHint(unittest.TestCase):
    def test_sxxexx_prefix(self):
        h = extract_show_hint_from_filename(
            "Tom and Jerry - S01E03 - The Mouse Trouble.avi")
        self.assertEqual(h, "Tom and Jerry")

    def test_dash_separator(self):
        h = extract_show_hint_from_filename("Underdog - Episode 5.avi")
        self.assertIn("Underdog", h)

    def test_with_season_word(self):
        h = extract_show_hint_from_filename(
            "Spider-Man TAS - S1E05 - The Menace.avi")
        self.assertIn("Spider", h)

    def test_underscore_filename(self):
        h = extract_show_hint_from_filename(
            "[ax]_ranma_-_season_01_-_02_-_school_is.mkv")
        self.assertIn("ranma", h.lower())

    def test_show_hint_from_folder(self):
        tmp = Path(tempfile.mkdtemp())
        # Create some episode files inside.
        for fn in ("Tom and Jerry - S01E01 - Puss Gets the Boot.avi",
                   "Tom and Jerry - S01E02 - The Midnight Snack.avi",
                   "Tom and Jerry - S01E03 - The Mouse Trouble.avi"):
            (tmp / fn).write_bytes(b"x")
        h = show_hint_from_folder(tmp, max_files=3)
        self.assertEqual(h, "Tom and Jerry")


# =========================================================================
# Candidate scoring
# =========================================================================

class TestNameSimilarity(unittest.TestCase):
    def test_exact(self):
        self.assertGreaterEqual(name_similarity("Friends", "Friends"), 0.99)

    def test_apostrophe_diff(self):
        self.assertGreater(name_similarity("Mr Bean", "Mr. Bean"), 0.85)

    def test_unrelated_low(self):
        self.assertLess(name_similarity("Friends", "Breaking Bad"), 0.4)


class TestScoreCandidates(unittest.TestCase):
    def test_high_confidence_single_strong_match(self):
        results = [{
            "id": 1668, "name": "Friends",
            "first_air_date": "1994-09-22",
            "vote_count": 8000, "popularity": 100.0,
        }]
        cands = score_candidates("Friends", results, year_hint=1994)
        self.assertEqual(len(cands), 1)
        self.assertGreater(cands[0].score, 0.85)
        self.assertEqual(assess(cands), "HIGH")

    def test_medium_when_mixed_results(self):
        results = [
            {"id": 1, "name": "Friends",
             "first_air_date": "1994-09-22",
             "vote_count": 8000, "popularity": 100.0},
            {"id": 2, "name": "Friends Forever",
             "first_air_date": "2010-01-01",
             "vote_count": 5000, "popularity": 50.0},
        ]
        cands = score_candidates("Friends", results, year_hint=None)
        # Top is exact match; with no year hint and runner being a partial
        # match, top should still beat runner clearly.
        self.assertEqual(cands[0].name, "Friends")

    def test_no_results(self):
        self.assertEqual(assess([]), "NONE")


# =========================================================================
# Path rendering
# =========================================================================

class TestRenderTargetFolder(unittest.TestCase):
    def test_basic(self):
        p = render_target_folder("Friends", 1994, 1668, Path("/tv"))
        self.assertEqual(Path(p).name, "Friends (1994) {tmdb-1668}")

    def test_strip_forbidden(self):
        p = render_target_folder("Cap'n Crunch: The Show", 2000, 1, Path("/tv"))
        # `:` mapped to ` - `, `'` kept.
        self.assertIn("Cap'n Crunch - The Show", p)


# =========================================================================
# End-to-end: propose + commit with FakeSession TMDB
# =========================================================================

class TestProposeCommit(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="retmdb_e2e_"))
        self.tv_root = self.tmp / "tv"; self.tv_root.mkdir()
        self.mediaprep = self.tmp / "_mp"; self.mediaprep.mkdir()
        # Build a series_plan.csv with several no-match SKIPs.
        self.plan_path = self.mediaprep / "series_plan.csv"
        cols = [
            "series_id", "source_folder", "episode_count", "sample_episode",
            "tmdb_series_id", "imdb_series_id", "matched_series", "matched_year",
            "target_folder", "confidence", "action",
            "flags", "notes", "user_notes", "executed_at",
        ]
        rows = [
            # HIGH-confidence resolvable: clean folder name "Friends"
            {"series_id": "100", "source_folder": "/tv/Friends.Complete.1080p",
             "action": "SKIP", "flags": "no_tmdb_results"},
            # Won't resolve (no canned response)
            {"series_id": "200", "source_folder": "/tv/200_CARTOONS_DISC1_Title2",
             "action": "SKIP", "flags": "no_tmdb_results"},
            # Already attempted - should be skipped
            {"series_id": "300", "source_folder": "/tv/Some Other",
             "action": "SKIP", "flags": "no_tmdb_results;retmdb_attempted"},
            # Wrong action (AUTO) - should be skipped
            {"series_id": "400", "source_folder": "/tv/Bingo",
             "action": "AUTO", "flags": ""},
        ]
        with open(self.plan_path, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in cols})

        # Config
        self.config_path = self.tmp / "config.json"
        self.config_path.write_text(json.dumps({
            "tmdb_api_key": "fake",
            "tv_root": str(self.tv_root),
            "mediaprep_dir": str(self.mediaprep),
        }))

        # Fake TMDB session — returns Friends for "Friends Complete 1080p"
        # but actually our cleaner reduces it to "Friends".
        self.session = FakeSession({
            "Friends": {
                "results": [{
                    "id": 1668, "name": "Friends",
                    "first_air_date": "1994-09-22",
                    "vote_count": 8000, "popularity": 100.0,
                }]
            },
        })

    def _client(self) -> TmdbTvClient:
        return TmdbTvClient(
            "fake", self.mediaprep / "cache.sqlite",
            session=self.session,
        )

    def test_propose_writes_csv(self):
        client = self._client()
        try:
            cfg = json.loads(self.config_path.read_text())
            out = self.mediaprep / "retmdb_proposed.csv"
            phase_propose(self.plan_path, out, client, cfg)
        finally:
            client.close()

        self.assertTrue(out.exists())
        rows = list(csv.DictReader(open(out, encoding="utf-8-sig")))
        # Should contain rows for 100 and 200 (both eligible).
        sids = [r["series_id"] for r in rows]
        self.assertIn("100", sids)
        self.assertIn("200", sids)
        self.assertNotIn("300", sids)  # already attempted
        self.assertNotIn("400", sids)  # action=AUTO

        # Series 100 should have HIGH confidence and pre-filled pick=1.
        r100 = next(r for r in rows if r["series_id"] == "100")
        self.assertEqual(r100["confidence"], "HIGH")
        self.assertEqual(r100["pick"], "1")
        self.assertIn("1668", r100["cand_1"])

        # Series 200 should have NONE.
        r200 = next(r for r in rows if r["series_id"] == "200")
        self.assertEqual(r200["confidence"], "NONE")

    def test_commit_applies_picks(self):
        # First, propose.
        client = self._client()
        cfg = json.loads(self.config_path.read_text())
        out = self.mediaprep / "retmdb_proposed.csv"
        try:
            phase_propose(self.plan_path, out, client, cfg)
        finally:
            client.close()

        # Now commit (auto-pick for HIGH was already filled in).
        rc = phase_commit(self.plan_path, out, cfg)
        self.assertEqual(rc, 0)

        # Verify series 100 is now AUTO with the right tmdb_id.
        rows = list(csv.DictReader(open(self.plan_path, encoding="utf-8-sig")))
        r100 = next(r for r in rows if r["series_id"] == "100")
        self.assertEqual(r100["action"], "AUTO")
        self.assertEqual(r100["tmdb_series_id"], "1668")
        self.assertEqual(r100["matched_series"], "Friends")
        self.assertEqual(r100["matched_year"], "1994")
        self.assertIn("retmdb_committed", r100["flags"])
        self.assertNotIn("no_tmdb_results", r100["flags"])

        # Series 200 (NONE -> no auto pick) should have stayed SKIP with
        # retmdb_attempted added.
        r200 = next(r for r in rows if r["series_id"] == "200")
        self.assertEqual(r200["action"], "SKIP")
        self.assertIn("retmdb_attempted", r200["flags"])

    def test_propose_excludes_multi_by_default(self):
        # Add a multi_result row.
        rows = list(csv.DictReader(open(self.plan_path, encoding="utf-8-sig")))
        fieldnames = list(rows[0].keys())
        rows.append({k: "" for k in fieldnames})
        rows[-1].update({
            "series_id": "500", "source_folder": "/tv/Diego",
            "action": "SKIP", "flags": "multi_result_19",
            "tmdb_series_id": "221428", "matched_series": "Diego San",
        })
        with open(self.plan_path, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)
        client = self._client()
        cfg = json.loads(self.config_path.read_text())
        out = self.mediaprep / "retmdb_proposed.csv"
        try:
            phase_propose(self.plan_path, out, client, cfg)
        finally:
            client.close()
        prop_rows = list(csv.DictReader(open(out, encoding="utf-8-sig")))
        sids = [r["series_id"] for r in prop_rows]
        # Default: multi_result row 500 NOT included.
        self.assertNotIn("500", sids)

    def test_propose_includes_multi_when_flagged(self):
        # Add a multi_result row.
        rows = list(csv.DictReader(open(self.plan_path, encoding="utf-8-sig")))
        fieldnames = list(rows[0].keys())
        rows.append({k: "" for k in fieldnames})
        rows[-1].update({
            "series_id": "500", "source_folder": "/tv/Friends",
            "action": "SKIP", "flags": "multi_result_19",
            "tmdb_series_id": "999", "matched_series": "Wrong Friends Match",
        })
        with open(self.plan_path, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)
        client = self._client()
        cfg = json.loads(self.config_path.read_text())
        out = self.mediaprep / "retmdb_proposed.csv"
        try:
            phase_propose(self.plan_path, out, client, cfg, include_multi=True)
        finally:
            client.close()
        prop_rows = list(csv.DictReader(open(out, encoding="utf-8-sig")))
        sids = [r["series_id"] for r in prop_rows]
        self.assertIn("500", sids)
        r500 = next(r for r in prop_rows if r["series_id"] == "500")
        self.assertEqual(r500["previous_tmdb_id"], "999")
        self.assertEqual(r500["previous_matched_series"], "Wrong Friends Match")
        # Even if confidence is HIGH, pick is left blank because there's a
        # previous match — force user to confirm.
        self.assertEqual(r500["pick"], "")

    def test_commit_multi_result_rejection_flags_disambig_attempted(self):
        rows = list(csv.DictReader(open(self.plan_path, encoding="utf-8-sig")))
        fieldnames = list(rows[0].keys())
        rows.append({k: "" for k in fieldnames})
        rows[-1].update({
            "series_id": "500", "source_folder": "/tv/Friends",
            "action": "SKIP", "flags": "multi_result_19",
            "tmdb_series_id": "999", "matched_series": "Wrong",
        })
        with open(self.plan_path, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)
        client = self._client()
        cfg = json.loads(self.config_path.read_text())
        out = self.mediaprep / "retmdb_proposed.csv"
        try:
            phase_propose(self.plan_path, out, client, cfg, include_multi=True)
        finally:
            client.close()
        # Don't pick anything (rejection).
        phase_commit(self.plan_path, out, cfg)
        rows = list(csv.DictReader(open(self.plan_path, encoding="utf-8-sig")))
        r500 = next(r for r in rows if r["series_id"] == "500")
        self.assertEqual(r500["action"], "SKIP")
        self.assertIn("disambig_attempted", r500["flags"])
        self.assertNotIn("retmdb_attempted", r500["flags"])

    def test_commit_user_rejection(self):
        # User explicitly clears pick column → row should stay SKIP.
        client = self._client()
        cfg = json.loads(self.config_path.read_text())
        out = self.mediaprep / "retmdb_proposed.csv"
        try:
            phase_propose(self.plan_path, out, client, cfg)
        finally:
            client.close()
        # Edit the proposal to clear pick on series 100.
        prop_rows = list(csv.DictReader(open(out, encoding="utf-8-sig")))
        for r in prop_rows:
            if r["series_id"] == "100":
                r["pick"] = ""
        with open(out, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=prop_rows[0].keys())
            w.writeheader()
            w.writerows(prop_rows)

        phase_commit(self.plan_path, out, cfg)
        rows = list(csv.DictReader(open(self.plan_path, encoding="utf-8-sig")))
        r100 = next(r for r in rows if r["series_id"] == "100")
        # User rejection -> stays SKIP, flag retmdb_attempted set.
        self.assertEqual(r100["action"], "SKIP")
        self.assertIn("retmdb_attempted", r100["flags"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
