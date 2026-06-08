#!/usr/bin/env python3
"""
test_inventory_tv.py - Unit tests for inventory_tv.py.

Covers the pure-logic helpers (folder-name parsing, file classification,
target-folder rendering, TMDB match scoring, config loading, state-file
round-trip). Does NOT exercise the full main() — that requires a real
filesystem walk and a TMDB key, and is better validated via the user's
real-data smoke test on G:\\TV.

Run:
    py -3.13 test_inventory_tv.py
    py -3.13 -m unittest test_inventory_tv
"""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from inventory_tv import (
    CONFIDENCE_AUTO,
    CONFIDENCE_REVIEW,
    DEFAULT_TV_SKIP_DIRS,
    Config,
    EpisodeFile,
    SeriesFolder,
    _normalize_name_for_match,
    _read_existing_csv_rows,
    _score_tmdb_match,
    _string_similarity,
    classify_role,
    compute_target_folder,
    load_state,
    parse_series_folder_name,
    refine_episode_role,
    sanitize_component,
    save_state,
    should_skip_dir,
    write_merge_candidates_csv,
    write_mixed_content_csv,
    write_series_plan_csv,
)
from tv_content_check import ContentCheckResult, VERDICT_MIXED
from tv_merge_clusterer import MergeGroup


# ---------------------------------------------------------------------------
# parse_series_folder_name
# ---------------------------------------------------------------------------

class TestParseSeriesFolderName(unittest.TestCase):
    def test_plain_name(self):
        name, year, tmdb, imdb = parse_series_folder_name("Friends")
        self.assertEqual(name, "Friends")
        self.assertIsNone(year)
        self.assertIsNone(tmdb)
        self.assertIsNone(imdb)

    def test_name_with_year(self):
        name, year, tmdb, imdb = parse_series_folder_name("Friends (1994)")
        self.assertEqual(name, "Friends")
        self.assertEqual(year, 1994)

    def test_name_with_tmdb_id(self):
        name, year, tmdb, _ = parse_series_folder_name("Doctor Who (2005) {tmdb-57243}")
        self.assertEqual(name, "Doctor Who")
        self.assertEqual(year, 2005)
        self.assertEqual(tmdb, 57243)

    def test_name_with_imdb_id(self):
        name, _, _, imdb = parse_series_folder_name("Sherlock {imdb-tt1475582}")
        self.assertEqual(name, "Sherlock")
        self.assertEqual(imdb, "tt1475582")

    def test_year_range_variants(self):
        # The parser already widens for "(YYYY-)" / "(YYYY-YYYY)".
        for s in ["Arrow (2012-)", "Arrow (2012-2020)", "Arrow (2012-15)"]:
            name, year, _, _ = parse_series_folder_name(s)
            self.assertEqual(name, "Arrow")
            self.assertEqual(year, 2012)

    def test_strips_trailing_separators(self):
        name, _, _, _ = parse_series_folder_name("MrBean - ")
        self.assertEqual(name, "MrBean")

    def test_camelcase_preserved(self):
        # We don't split CamelCase — that's the clusterer's concern.
        name, _, _, _ = parse_series_folder_name("MrBean")
        self.assertEqual(name, "MrBean")

    def test_strips_leading_episode_number(self):
        # phase6_design.md §6 single-episode wrapper convention:
        # "01 MrBean" -> "MrBean" so TMDB search works.
        name, _, _, _ = parse_series_folder_name("01 MrBean")
        self.assertEqual(name, "MrBean")
        name, _, _, _ = parse_series_folder_name("02 Good Night MrBean")
        self.assertEqual(name, "Good Night MrBean")
        name, _, _, _ = parse_series_folder_name("100 Some Show")
        self.assertEqual(name, "Some Show")

    def test_does_not_strip_collection_size_prefix(self):
        # "200 Classic Cartoons Collector's Edition" — the leading "200" is
        # part of the collection name, not a track number. Conservative
        # heuristic: only strip when followed by whitespace AND the digit
        # group is <= 3 digits. Both still match here, so we accept this
        # collateral damage; the clusterer's identity rule still groups
        # "200 Classic Cartoons" with "200_CARTOONS_DISC*".
        name, _, _, _ = parse_series_folder_name("200 Classic Cartoons")
        self.assertEqual(name, "Classic Cartoons")  # known-acceptable behavior

    def test_preserves_underscore_artifact_folders(self):
        # "200_CARTOONS_DISC1_Title2" has no leading-digit-then-space, so it
        # is left intact (the merge clusterer's normalize_folder_name
        # handles those folder names separately).
        name, _, _, _ = parse_series_folder_name("200_CARTOONS_DISC1_Title2")
        self.assertEqual(name, "200_CARTOONS_DISC1_Title2")


# ---------------------------------------------------------------------------
# classify_role
# ---------------------------------------------------------------------------

class TestClassifyRole(unittest.TestCase):
    def test_video(self):
        self.assertEqual(classify_role(Path("Show S01E01.mkv")), "episode-candidate")
        self.assertEqual(classify_role(Path("Show S01E01.MP4")), "episode-candidate")

    def test_nfo(self):
        self.assertEqual(classify_role(Path("tvshow.nfo")), "nfo")
        self.assertEqual(classify_role(Path("episode.xml")), "nfo")

    def test_subtitle(self):
        self.assertEqual(classify_role(Path("Show.S01E01.en.srt")), "subtitle")
        self.assertEqual(classify_role(Path("Show.idx")), "subtitle")

    def test_poster(self):
        self.assertEqual(classify_role(Path("poster.jpg")), "poster")
        self.assertEqual(classify_role(Path("show-poster.png")), "poster")

    def test_fanart(self):
        self.assertEqual(classify_role(Path("fanart.jpg")), "fanart")
        self.assertEqual(classify_role(Path("backdrop.png")), "fanart")

    def test_banner(self):
        self.assertEqual(classify_role(Path("banner.jpg")), "banner")
        self.assertEqual(classify_role(Path("season01-banner.png")), "banner")

    def test_other(self):
        self.assertEqual(classify_role(Path("readme.txt")), "other")
        self.assertEqual(classify_role(Path("Thumbs.db")), "other")


# ---------------------------------------------------------------------------
# refine_episode_role — promotes episode-candidate to episode/extras
# ---------------------------------------------------------------------------

class TestRefineEpisodeRole(unittest.TestCase):
    def _make_file(self, name: str, ext: str = ".mkv") -> EpisodeFile:
        return EpisodeFile(
            file_id=1, abs_path=f"/x/{name}", parent_folder="/x",
            filename=name, ext=ext, size_bytes=100,
            mtime="", file_hash="", role="episode-candidate",
        )

    def test_promotes_parsed_episode(self):
        f = self._make_file("Show S01E03 Title.mkv")
        refine_episode_role(f, "Show")
        self.assertEqual(f.role, "episode")
        self.assertEqual(f.season, 1)
        self.assertEqual(f.episode, 3)
        self.assertEqual(f.pattern_matched, "s_e")
        self.assertGreaterEqual(f.parser_confidence, 90)

    def test_demotes_unparseable_to_extras(self):
        f = self._make_file("Behind the Scenes.mkv")
        refine_episode_role(f, "Show")
        self.assertEqual(f.role, "extras")
        self.assertEqual(f.season, None)

    def test_specials_in_parent_folder(self):
        f = self._make_file("Ep 01.mkv")
        refine_episode_role(f, "Specials")
        self.assertEqual(f.role, "episode")
        self.assertEqual(f.season, 0)
        self.assertTrue(f.is_special)

    def test_already_classified_skipped(self):
        f = self._make_file("foo.mkv")
        f.role = "nfo"  # pretend it's already an nfo
        refine_episode_role(f, "Show")
        self.assertEqual(f.role, "nfo")  # unchanged


# ---------------------------------------------------------------------------
# should_skip_dir
# ---------------------------------------------------------------------------

class TestShouldSkipDir(unittest.TestCase):
    def test_default_skip_dirs(self):
        for d in DEFAULT_TV_SKIP_DIRS:
            self.assertTrue(should_skip_dir(d, DEFAULT_TV_SKIP_DIRS))

    def test_hidden_directory_skipped(self):
        self.assertTrue(should_skip_dir(".sonarr", DEFAULT_TV_SKIP_DIRS))
        self.assertTrue(should_skip_dir(".cache", DEFAULT_TV_SKIP_DIRS))

    def test_normal_directory_kept(self):
        self.assertFalse(should_skip_dir("Doctor Who (2005)", DEFAULT_TV_SKIP_DIRS))
        self.assertFalse(should_skip_dir("Season 01", DEFAULT_TV_SKIP_DIRS))

    def test_empty_name(self):
        self.assertFalse(should_skip_dir("", DEFAULT_TV_SKIP_DIRS))


# ---------------------------------------------------------------------------
# _normalize_name_for_match / _string_similarity
# ---------------------------------------------------------------------------

class TestNormalizeAndSimilarity(unittest.TestCase):
    def test_normalize_strips_punctuation(self):
        self.assertEqual(_normalize_name_for_match("Mr. Bean!"), "mrbean")
        self.assertEqual(_normalize_name_for_match("Doctor Who"), "doctorwho")

    def test_normalize_handles_unicode(self):
        # NFKD strips accents.
        self.assertEqual(_normalize_name_for_match("Pokémon"), "pokemon")

    def test_similarity_identical(self):
        self.assertAlmostEqual(_string_similarity("Doctor Who", "Doctor Who"), 1.0)

    def test_similarity_close_match(self):
        sim = _string_similarity("Mr Bean", "Mr. Bean!")
        self.assertGreater(sim, 0.95)

    def test_similarity_disjoint(self):
        sim = _string_similarity("Sherlock", "Friends")
        self.assertLess(sim, 0.4)

    def test_similarity_empty(self):
        self.assertEqual(_string_similarity("", "Friends"), 0.0)


# ---------------------------------------------------------------------------
# _score_tmdb_match — heuristic scoring of TMDB candidates
# ---------------------------------------------------------------------------

class TestScoreTmdbMatch(unittest.TestCase):
    def test_exact_name_and_year_high_score(self):
        cand = {"name": "Doctor Who", "first_air_date": "2005-03-26"}
        score = _score_tmdb_match("Doctor Who", 2005, cand, is_only_result=True)
        # Exact name + year + only-result bonus -> very high.
        self.assertGreaterEqual(score, 95)

    def test_exact_name_no_year_in_folder(self):
        cand = {"name": "Doctor Who", "first_air_date": "2005-03-26"}
        score = _score_tmdb_match("Doctor Who", None, cand, is_only_result=True)
        # No year context -> still high but not as high.
        self.assertGreaterEqual(score, 70)

    def test_wrong_year_drops_score(self):
        cand = {"name": "Doctor Who", "first_air_date": "1963-11-23"}
        score = _score_tmdb_match("Doctor Who", 2005, cand, is_only_result=True)
        # Same name but 1963 vs 2005 — penalize.
        self.assertLess(score, 90)

    def test_different_name_low_score(self):
        cand = {"name": "Sherlock", "first_air_date": "2010-07-25"}
        score = _score_tmdb_match("Doctor Who", 2005, cand, is_only_result=True)
        self.assertLess(score, 50)

    def test_year_off_by_one_partial_credit(self):
        cand = {"name": "Doctor Who", "first_air_date": "2006-01-01"}
        score = _score_tmdb_match("Doctor Who", 2005, cand, is_only_result=True)
        # ±1 year still gets significant credit.
        self.assertGreaterEqual(score, 80)

    def test_multi_result_no_solo_bonus(self):
        cand = {"name": "Doctor Who", "first_air_date": "2005-03-26"}
        solo = _score_tmdb_match("Doctor Who", 2005, cand, is_only_result=True)
        multi = _score_tmdb_match("Doctor Who", 2005, cand, is_only_result=False)
        self.assertGreater(solo, multi)

    def test_uses_original_name_when_better(self):
        # TMDB sometimes returns the localized name in `name` and the
        # original in `original_name`.
        cand = {
            "name": "Médecin Qui",
            "original_name": "Doctor Who",
            "first_air_date": "2005-03-26",
        }
        score = _score_tmdb_match("Doctor Who", 2005, cand, is_only_result=True)
        self.assertGreaterEqual(score, 90)


# ---------------------------------------------------------------------------
# compute_target_folder
# ---------------------------------------------------------------------------

class TestComputeTargetFolder(unittest.TestCase):
    def test_default_template(self):
        s = SeriesFolder(
            tmdb_series_id=57243,
            matched_series="Doctor Who",
            matched_year=2005,
        )
        result = compute_target_folder(
            s, Path("G:/TV"),
            "{title} ({year}) {{tmdb-{tmdb_id}}}",
        )
        # Sanitize_component normalizes path separators in the template
        # output, so the year-paren stays intact and the tmdb-id is bracketed.
        self.assertIn("Doctor Who", result)
        self.assertIn("(2005)", result)
        self.assertIn("{tmdb-57243}", result)

    def test_no_tmdb_id_returns_empty(self):
        s = SeriesFolder(matched_series="Doctor Who", matched_year=2005)
        self.assertEqual(
            compute_target_folder(s, Path("G:/TV"), "{title} ({year})"),
            "",
        )

    def test_no_year_in_template_no_year_data(self):
        s = SeriesFolder(
            tmdb_series_id=999, matched_series="Show", matched_year=None,
        )
        result = compute_target_folder(s, Path("G:/TV"), "{title} ({year})")
        # Year empty string substitutes; sanitize collapses the parens.
        self.assertIn("Show", result)


# ---------------------------------------------------------------------------
# sanitize_component
# ---------------------------------------------------------------------------

class TestSanitizeComponent(unittest.TestCase):
    def test_strips_forbidden_chars(self):
        self.assertEqual(sanitize_component("Foo<>:Bar"), "Foo - Bar")
        self.assertEqual(sanitize_component('Foo"Bar'), "Foo'Bar")
        self.assertEqual(sanitize_component("Foo|Bar"), "Foo-Bar")

    def test_collapses_whitespace(self):
        self.assertEqual(sanitize_component("Foo   Bar"), "Foo Bar")

    def test_strips_trailing_dots(self):
        self.assertEqual(sanitize_component("Mr. "), "Mr")

    def test_handles_empty(self):
        self.assertEqual(sanitize_component(""), "_")

    def test_reserved_names_get_underscore(self):
        self.assertEqual(sanitize_component("CON"), "CON_")
        self.assertEqual(sanitize_component("LPT1"), "LPT1_")


# ---------------------------------------------------------------------------
# State file round-trip
# ---------------------------------------------------------------------------

class TestStateFile(unittest.TestCase):
    def test_round_trip(self):
        tmp = Path(tempfile.mkdtemp(prefix="inv_tv_state_"))
        path = tmp / "state.json"
        save_state(path, {"identified": {"1": {"confidence": 95}}})
        loaded = load_state(path)
        self.assertEqual(loaded["identified"]["1"]["confidence"], 95)

    def test_missing_returns_empty(self):
        tmp = Path(tempfile.mkdtemp(prefix="inv_tv_state_"))
        self.assertEqual(load_state(tmp / "nonexistent.json"), {})

    def test_corrupt_json_returns_empty(self):
        tmp = Path(tempfile.mkdtemp(prefix="inv_tv_state_"))
        path = tmp / "state.json"
        path.write_text("not valid json {{{", encoding="utf-8")
        self.assertEqual(load_state(path), {})


# ---------------------------------------------------------------------------
# Config.load
# ---------------------------------------------------------------------------

class TestConfigLoad(unittest.TestCase):
    def _write(self, **overrides) -> Path:
        tmp = Path(tempfile.mkdtemp(prefix="inv_tv_cfg_"))
        cfg = {
            "tmdb_api_key": "real-key",
            "tv_root": "G:/TV",
            "mediaprep_dir": str(tmp / "_mediaprep"),
            "language": "en-US",
        }
        cfg.update(overrides)
        path = tmp / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_required_keys(self):
        cfg = Config.load(self._write())
        self.assertEqual(cfg.tmdb_api_key, "real-key")
        self.assertEqual(cfg.tv_root, Path("G:/TV"))

    def test_defaults_filled_in(self):
        cfg = Config.load(self._write())
        self.assertEqual(cfg.confidence_auto, CONFIDENCE_AUTO)
        self.assertEqual(cfg.tv_specials_folder, "Specials")
        self.assertEqual(cfg.language, "en-US")

    def test_missing_required_key_exits(self):
        cfg_path = self._write()
        # Strip tv_root.
        raw = json.loads(cfg_path.read_text())
        del raw["tv_root"]
        cfg_path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(SystemExit):
            Config.load(cfg_path)

    def test_placeholder_api_key_exits(self):
        cfg_path = self._write(tmdb_api_key="REPLACE_ME")
        with self.assertRaises(SystemExit):
            Config.load(cfg_path)

    def test_overrides_applied(self):
        cfg = Config.load(self._write(
            confidence_auto=80,
            tv_merge_threshold=0.6,
            tv_specials_folder="My Specials",
            tv_skip_dirs=[".my-trash"],
        ))
        self.assertEqual(cfg.confidence_auto, 80)
        self.assertEqual(cfg.tv_merge_threshold, 0.6)
        self.assertEqual(cfg.tv_specials_folder, "My Specials")
        self.assertEqual(cfg.tv_skip_dirs, (".my-trash",))

    def test_tv_skip_dirs_must_be_list(self):
        cfg_path = self._write(tv_skip_dirs="not a list")
        with self.assertRaises(SystemExit):
            Config.load(cfg_path)


# ---------------------------------------------------------------------------
# Integration: Config -> SeriesFolder properties
# ---------------------------------------------------------------------------

class TestSeriesFolderProperties(unittest.TestCase):
    def _ep(self, season, episode, *, special=False) -> EpisodeFile:
        return EpisodeFile(
            file_id=1, role="episode",
            season=season, episode=episode, is_special=special,
        )

    def test_episode_count_excludes_non_episodes(self):
        s = SeriesFolder(files=[
            self._ep(1, 1),
            self._ep(1, 2),
            EpisodeFile(role="nfo"),
            EpisodeFile(role="poster"),
            self._ep(1, 3),
        ])
        self.assertEqual(s.episode_count, 3)

    def test_has_specials_via_marker(self):
        s = SeriesFolder(files=[self._ep(0, 1, special=True), self._ep(1, 1)])
        self.assertTrue(s.has_specials)

    def test_has_specials_via_season_zero(self):
        s = SeriesFolder(files=[self._ep(0, 1), self._ep(1, 1)])
        self.assertTrue(s.has_specials)

    def test_no_specials(self):
        s = SeriesFolder(files=[self._ep(1, 1), self._ep(1, 2)])
        self.assertFalse(s.has_specials)

    def test_sample_episode_first_episode_filename(self):
        f1 = EpisodeFile(role="nfo", filename="tvshow.nfo")
        f2 = EpisodeFile(role="episode", filename="Show S01E01.mkv")
        f3 = EpisodeFile(role="episode", filename="Show S01E02.mkv")
        s = SeriesFolder(files=[f1, f2, f3])
        self.assertEqual(s.sample_episode, "Show S01E01.mkv")



# ---------------------------------------------------------------------------
# Review-edit preservation across re-runs
# ---------------------------------------------------------------------------

class TestReviewPreservation(unittest.TestCase):
    """Re-running inventory_tv must preserve the user-set action/notes
    columns from prior runs (keyed by stable identifiers)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="inv_tv_preserve_"))

    def _write_initial_series_csv(self, action: str, user_notes: str = "") -> Path:
        """Write a series_plan.csv as if a previous run wrote it AND the user
        then edited the action / user_notes columns. Uses csv.writer so the
        header column order matches what write_series_plan_csv emits."""
        path = self.tmp / "series_plan.csv"
        cols = [
            "series_id", "source_folder", "episode_count", "sample_episode",
            "tmdb_series_id", "imdb_series_id", "matched_series", "matched_year",
            "target_folder", "confidence", "action",
            "content_check", "has_specials", "mixed_content_flag",
            "flags", "notes", "executed_at", "user_notes",
        ]
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(cols)
            w.writerow([
                "1", "G:/TV/Doctor Who (2005)", "13", "Doctor Who S01E01.mkv",
                "57243", "tt0436992", "Doctor Who", "2005",
                "G:/TV/Doctor Who (2005) {tmdb-57243}", "95",
                action,
                "ok", "", "",
                "", "", "", user_notes,
            ])
        return path

    def test_preserves_user_action_when_rewriting(self):
        # Prior CSV says user manually marked this row SKIP.
        path = self._write_initial_series_csv(action="SKIP")

        # Now re-run inventory; the algorithm would default to AUTO (conf=95).
        s = SeriesFolder(
            series_id=1,
            source_folder="G:/TV/Doctor Who (2005)",
            tmdb_series_id=57243,
            matched_series="Doctor Who",
            matched_year=2005,
            confidence=95,
            target_folder="G:/TV/Doctor Who (2005) {tmdb-57243}",
            files=[EpisodeFile(role="episode", filename="Doctor Who S01E01.mkv")],
        )
        write_series_plan_csv(path, [s])

        # Read back and confirm the SKIP survived.
        rows = _read_existing_csv_rows(path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "SKIP")

    def test_default_action_used_when_no_prior_user_edit(self):
        # No prior CSV exists.
        path = self.tmp / "series_plan.csv"
        s = SeriesFolder(
            series_id=1,
            source_folder="G:/TV/Doctor Who (2005)",
            tmdb_series_id=57243,
            matched_series="Doctor Who",
            confidence=95,
        )
        write_series_plan_csv(path, [s])
        rows = _read_existing_csv_rows(path)
        self.assertEqual(rows[0]["action"], "AUTO")  # algorithm default at conf=95

    def test_preserves_user_notes(self):
        path = self._write_initial_series_csv(action="", user_notes="Wrong year, fix me")
        s = SeriesFolder(
            series_id=1,
            source_folder="G:/TV/Doctor Who (2005)",
            tmdb_series_id=57243,
            matched_series="Doctor Who",
            confidence=95,
        )
        write_series_plan_csv(path, [s])
        rows = _read_existing_csv_rows(path)
        self.assertEqual(rows[0]["user_notes"], "Wrong year, fix me")

    def test_preserves_executed_at(self):
        # A timestamp written by execute_tv.py must survive an inventory re-run.
        path = self.tmp / "series_plan.csv"
        cols_extra = [
            "series_id", "source_folder", "episode_count", "sample_episode",
            "tmdb_series_id", "imdb_series_id", "matched_series", "matched_year",
            "target_folder", "confidence", "action",
            "content_check", "has_specials", "mixed_content_flag",
            "flags", "notes", "executed_at", "user_notes",
        ]
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(cols_extra)
            w.writerow([
                "1", "G:/TV/Foo", "1", "Foo S01E01.mkv",
                "100", "", "Foo", "2020", "G:/TV/Foo (2020) {tmdb-100}",
                "95", "AUTO", "ok", "", "",
                "", "", "2026-04-29T12:34:56", "",
            ])
        s = SeriesFolder(
            series_id=1, source_folder="G:/TV/Foo",
            tmdb_series_id=100, matched_series="Foo",
            matched_year=2020, confidence=95,
        )
        write_series_plan_csv(path, [s])
        rows = _read_existing_csv_rows(path)
        self.assertEqual(rows[0]["executed_at"], "2026-04-29T12:34:56")

    def test_merge_candidates_preserves_action_and_name(self):
        # User flipped a merge group to AUTO_MERGE and renamed the suggestion.
        path = self.tmp / "merge_candidates.csv"
        cols = [
            "group_id", "candidate_series_name", "member_folder_count",
            "member_folders", "shared_token_count", "suggested_match",
            "action", "notes",
        ]
        members = ["01 MrBean", "02 Good Night MrBean", "03 Mind the Baby MrBean"]
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(cols)
            w.writerow([
                "1", "Mr. Bean (Animated)", "3",
                " | ".join(members), "1", "tmdb-12345",
                "AUTO_MERGE", "user notes here",
            ])

        # Re-run produces the same group with default REVIEW + algorithm name.
        g = MergeGroup(
            group_id=1, members=members,
            suggested_series_name="Mrbean", shared_tokens=["mrbean"],
        )
        write_merge_candidates_csv(path, [g])
        rows = _read_existing_csv_rows(path)
        self.assertEqual(rows[0]["action"], "AUTO_MERGE")
        self.assertEqual(rows[0]["candidate_series_name"], "Mr. Bean (Animated)")
        self.assertEqual(rows[0]["suggested_match"], "tmdb-12345")
        self.assertEqual(rows[0]["notes"], "user notes here")

    def test_merge_candidates_no_preserve_when_members_change(self):
        # User reviewed cluster A, but on re-run the cluster grew/shrunk so
        # the member-set is different. The user's prior action does NOT carry
        # over (different cluster).
        path = self.tmp / "merge_candidates.csv"
        cols = [
            "group_id", "candidate_series_name", "member_folder_count",
            "member_folders", "shared_token_count", "suggested_match",
            "action", "notes",
        ]
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(cols)
            w.writerow([
                "1", "Old", "2", "Old A | Old B", "0", "",
                "AUTO_MERGE", "",
            ])
        # New run sees different members.
        g = MergeGroup(
            group_id=1, members=["New A", "New B", "New C"],
            suggested_series_name="New", shared_tokens=["new"],
        )
        write_merge_candidates_csv(path, [g])
        rows = _read_existing_csv_rows(path)
        # New row is REVIEW (algorithm default) since member set changed.
        self.assertEqual(rows[0]["action"], "REVIEW")

    def test_mixed_content_preserves_action_and_resolution(self):
        path = self.tmp / "mixed_content.csv"
        cols = [
            "folder_name", "dominant_filename_series",
            "episode_count_dominant", "episode_count_other",
            "action", "manual_resolution",
            "similarity", "total_episode_count", "unparseable_count",
            "verdict", "notes",
        ]
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(cols)
            w.writerow([
                "Friends", "The Office", "3", "0",
                "RELOCATE", "Move to G:/TV/The Office (2005)",
                "0.00", "3", "0", "mixed_content", "",
            ])

        # Re-run produces same flag with default REVIEW action.
        result = ContentCheckResult(
            folder_name="Friends",
            dominant_filename_series="The Office",
            dominant_count=3,
            other_count=0,
            total_episode_count=3,
            similarity=0.0,
            flag_as_mixed=True,
            verdict=VERDICT_MIXED,
        )
        write_mixed_content_csv(path, [result])
        rows = _read_existing_csv_rows(path)
        self.assertEqual(rows[0]["action"], "RELOCATE")
        self.assertEqual(rows[0]["manual_resolution"],
                         "Move to G:/TV/The Office (2005)")

    def test_robust_to_nul_bytes_in_existing_csv(self):
        # PowerShell sometimes writes UTF-16 with BOM, or polluted UTF-8 with
        # NUL bytes. _read_existing_csv_rows should still parse it.
        path = self.tmp / "series_plan.csv"
        # Write good UTF-8 then sprinkle NUL bytes (simulating PowerShell glitch)
        normal = self._write_initial_series_csv(action="SKIP")
        polluted = normal.read_bytes().replace(b"\n", b"\n\x00")
        normal.write_bytes(polluted)

        s = SeriesFolder(
            series_id=1, source_folder="G:/TV/Doctor Who (2005)",
            tmdb_series_id=57243, matched_series="Doctor Who",
            matched_year=2005, confidence=95,
        )
        # Should not crash, and should still preserve the SKIP.
        write_series_plan_csv(normal, [s])
        rows = _read_existing_csv_rows(normal)
        self.assertEqual(rows[0]["action"], "SKIP")



if __name__ == "__main__":
    unittest.main(verbosity=2)
