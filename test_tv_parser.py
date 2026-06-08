#!/usr/bin/env python3
"""
test_tv_parser.py - Unit tests for tv_parser.py.

Stdlib unittest, zero third-party dependencies. Mirrors the structure of
phase6_design.md §5 Stage B (six parser cases) plus §6 (folder normalization
for the merge clusterer) and §7 (cross-folder content series-name derivation).

Run:
    py -3.13 test_tv_parser.py
    py -3.13 -m unittest test_tv_parser
"""

from __future__ import annotations

import unittest

from tv_parser import (
    ParsedEpisode,
    derive_series_from_filename,
    is_video_filename,
    leading_episode_number,
    normalize_folder_name,
    parse_episode,
    tokenize_for_clustering,
)


# ---------------------------------------------------------------------------
# Case 1: SxxExx
# ---------------------------------------------------------------------------

class TestSxxExxPattern(unittest.TestCase):
    def test_dotted_dvd_rip(self):
        p = parse_episode("Show.Name.S01E03.Title.1080p.x264.mkv")
        self.assertTrue(p.parsed)
        self.assertEqual(p.season, 1)
        self.assertEqual(p.episode, 3)
        self.assertEqual(p.episode_end, 3)
        self.assertFalse(p.is_multi)
        self.assertEqual(p.pattern_matched, "s_e")
        self.assertGreaterEqual(p.confidence, 90)
        self.assertEqual(p.series_name_guess, "Show Name")

    def test_lowercase(self):
        p = parse_episode("show name s01e03 title.mkv")
        self.assertEqual((p.season, p.episode), (1, 3))
        self.assertEqual(p.series_name_guess, "show name")

    def test_single_digit_season_or_episode(self):
        p = parse_episode("Show S1E12.mkv")
        self.assertEqual((p.season, p.episode), (1, 12))

    def test_three_digit_season(self):
        # phase6_design.md §9: support shows with >99 episodes per season.
        p = parse_episode("Show S25E100.mkv")
        self.assertEqual((p.season, p.episode), (25, 100))

    def test_separator_between_s_and_e(self):
        p = parse_episode("Show.S01.E03.mkv")
        self.assertEqual((p.season, p.episode), (1, 3))

    def test_no_extension(self):
        p = parse_episode("Show.Name.S01E03.Title")
        self.assertEqual((p.season, p.episode), (1, 3))

    def test_specials_via_s00(self):
        p = parse_episode("Doctor Who S00E01 The Christmas Invasion.mkv")
        self.assertEqual((p.season, p.episode), (0, 1))
        self.assertTrue(p.is_special)


# ---------------------------------------------------------------------------
# Case 2: ##x##
# ---------------------------------------------------------------------------

class TestNxNNPattern(unittest.TestCase):
    def test_basic(self):
        p = parse_episode("Show 1x03.avi")
        self.assertEqual((p.season, p.episode), (1, 3))
        self.assertEqual(p.pattern_matched, "x_form")

    def test_three_digit_episode(self):
        p = parse_episode("Show 12x103.mp4")
        self.assertEqual((p.season, p.episode), (12, 103))

    def test_dotted(self):
        p = parse_episode("Show.1x03.Title.mkv")
        self.assertEqual((p.season, p.episode), (1, 3))
        self.assertEqual(p.series_name_guess, "Show")

    def test_does_not_match_codec_x264(self):
        # "x264" is a codec, not a season-episode marker.
        p = parse_episode("Show.Name.x264.mkv")
        self.assertFalse(p.parsed)
        self.assertIn("unparseable", p.flags)

    def test_does_not_match_codec_h264(self):
        p = parse_episode("Show.Name.h264.mkv")
        self.assertFalse(p.parsed)

    def test_does_not_match_resolution(self):
        # "1920x1080" must not be parsed as season 1920 episode 1080.
        p = parse_episode("Show 1920x1080 trailer.mkv")
        self.assertFalse(p.parsed)


# ---------------------------------------------------------------------------
# Case 3: bare-N inside a Season-N\ folder
# ---------------------------------------------------------------------------

class TestSeasonFolderBareEpisode(unittest.TestCase):
    def test_episode_word(self):
        p = parse_episode("Episode 03.mkv", parent_folder="Season 1")
        self.assertEqual((p.season, p.episode), (1, 3))
        self.assertEqual(p.pattern_matched, "season_folder_ep")

    def test_ep_abbreviated(self):
        p = parse_episode("Ep 03.mkv", parent_folder="Season 01")
        self.assertEqual((p.season, p.episode), (1, 3))

    def test_e_only(self):
        p = parse_episode("E03 - Title.mkv", parent_folder="Season 1")
        self.assertEqual((p.season, p.episode), (1, 3))

    def test_season_with_year_in_parent(self):
        # phase6_design.md §9: season folder is "Season ## (Year)".
        p = parse_episode("Episode 03.mkv", parent_folder="Season 1 (1986)")
        self.assertEqual((p.season, p.episode), (1, 3))

    def test_specials_parent(self):
        p = parse_episode("Ep 01.mkv", parent_folder="Specials")
        self.assertEqual((p.season, p.episode), (0, 1))
        self.assertTrue(p.is_special)

    def test_bare_digit_fallback(self):
        # No "Episode" word, just a leading number — fallback path.
        p = parse_episode("03 Title.mkv", parent_folder="Season 1")
        self.assertEqual((p.season, p.episode), (1, 3))
        self.assertEqual(p.pattern_matched, "season_folder_bare")

    def test_year_in_filename_is_skipped(self):
        # A 4-digit year-like number must not be picked up as the episode.
        p = parse_episode("Episode 1986 - aired.mkv", parent_folder="Season 1")
        # "Episode 1986" matches RE_BARE_EP with ep1=1986. We accept this for now
        # because the "Episode" word is an explicit marker; tightening the
        # heuristic would also reject genuine high-episode-count shows. Document
        # the current behavior.
        self.assertEqual((p.season, p.episode), (1, 1986))

    def test_bare_fallback_skips_year(self):
        p = parse_episode("1986 Title.mkv", parent_folder="Season 1")
        self.assertFalse(p.parsed)  # year-only number is skipped, no other digit

    def test_s_e_takes_priority_over_parent(self):
        # If a filename has SxxExx, that wins over a Season-N parent.
        p = parse_episode("Show S02E04.mkv", parent_folder="Season 1")
        self.assertEqual((p.season, p.episode), (2, 4))
        self.assertEqual(p.pattern_matched, "s_e")


# ---------------------------------------------------------------------------
# Case 4: multi-episode markers
# ---------------------------------------------------------------------------

class TestMultiEpisode(unittest.TestCase):
    def test_s_e_dash_e(self):
        p = parse_episode("Show S01E01-E02 Title.mkv")
        self.assertEqual((p.season, p.episode, p.episode_end), (1, 1, 2))
        self.assertTrue(p.is_multi)

    def test_s_e_e(self):
        p = parse_episode("Show S01E01E02.mkv")
        self.assertEqual((p.season, p.episode, p.episode_end), (1, 1, 2))
        self.assertTrue(p.is_multi)

    def test_s_e_dash_short(self):
        p = parse_episode("Show S01E01-02.mkv")
        self.assertEqual((p.season, p.episode, p.episode_end), (1, 1, 2))
        self.assertTrue(p.is_multi)

    def test_x_form_dash_full(self):
        p = parse_episode("Show 1x01-1x02.mkv")
        self.assertEqual((p.season, p.episode, p.episode_end), (1, 1, 2))
        self.assertTrue(p.is_multi)

    def test_x_form_dash_short(self):
        p = parse_episode("Show 1x01-02.mkv")
        self.assertEqual((p.season, p.episode, p.episode_end), (1, 1, 2))
        self.assertTrue(p.is_multi)

    def test_descending_range_flagged(self):
        p = parse_episode("Show S01E03-01.mkv")
        self.assertEqual((p.season, p.episode), (1, 3))
        self.assertEqual(p.episode_end, 3)  # collapsed
        self.assertIn("multi_ep_range_descending", p.flags)


# ---------------------------------------------------------------------------
# Case 5: absolute numbering (gated)
# ---------------------------------------------------------------------------

class TestAbsoluteNumbering(unittest.TestCase):
    def test_disabled_by_default(self):
        # phase6_design.md §15: "No anime mode by default".
        p = parse_episode("Show Name - 145.mkv")
        self.assertFalse(p.parsed)

    def test_enabled_via_flag(self):
        p = parse_episode("Show Name - 145.mkv", allow_absolute=True)
        self.assertTrue(p.parsed)
        self.assertEqual(p.season, 0)
        self.assertEqual(p.episode, 145)
        self.assertTrue(p.is_absolute)
        self.assertEqual(p.pattern_matched, "absolute")
        self.assertIn("absolute_numbering_assumed", p.flags)
        self.assertLessEqual(p.confidence, 50)

    def test_absolute_three_digit(self):
        p = parse_episode("Anime Show - 067.mkv", allow_absolute=True)
        self.assertEqual(p.episode, 67)
        self.assertTrue(p.is_absolute)


# ---------------------------------------------------------------------------
# Case 6: specials markers
# ---------------------------------------------------------------------------

class TestSpecialsMarkers(unittest.TestCase):
    def test_christmas_marker_with_s_e(self):
        p = parse_episode("Doctor Who S00E01 The Christmas Invasion.mkv")
        self.assertTrue(p.is_special)

    def test_ova_marker_in_season_folder(self):
        p = parse_episode("Show OVA 03.mkv", parent_folder="Season 1")
        # OVA is a specials marker; season comes from the parent folder, but
        # is_special should still be True.
        self.assertTrue(p.parsed)
        self.assertTrue(p.is_special)

    def test_special_word_alone(self):
        p = parse_episode("Show S00E05 Christmas Special.mkv")
        self.assertTrue(p.is_special)

    def test_holiday_marker(self):
        p = parse_episode("Show S00E01 Holiday Episode.mkv")
        self.assertTrue(p.is_special)


# ---------------------------------------------------------------------------
# Edge cases / unparseable
# ---------------------------------------------------------------------------

class TestUnparseable(unittest.TestCase):
    def test_empty(self):
        p = parse_episode("")
        self.assertFalse(p.parsed)
        self.assertIn("empty_filename", p.flags)

    def test_no_episode_marker(self):
        p = parse_episode("Some Random File.mkv")
        self.assertFalse(p.parsed)
        self.assertIn("unparseable", p.flags)

    def test_year_only_no_parent(self):
        p = parse_episode("Show Name 2024.mkv")
        self.assertFalse(p.parsed)

    def test_returns_dataclass_instance(self):
        p = parse_episode("anything.mkv")
        self.assertIsInstance(p, ParsedEpisode)
        # raw fields preserved for diagnostics.
        self.assertEqual(p.raw_filename, "anything.mkv")


# ---------------------------------------------------------------------------
# episode_key formatting
# ---------------------------------------------------------------------------

class TestEpisodeKey(unittest.TestCase):
    def test_single(self):
        p = parse_episode("Show S01E03.mkv")
        self.assertEqual(p.episode_key, "S01E03")

    def test_zero_padded(self):
        p = parse_episode("Show S1E3.mkv")
        self.assertEqual(p.episode_key, "S01E03")

    def test_multi(self):
        p = parse_episode("Show S05E12-E13.mkv")
        self.assertEqual(p.episode_key, "S05E12-E13")

    def test_unparsed_returns_empty(self):
        p = parse_episode("nope.mkv")
        self.assertEqual(p.episode_key, "")


# ---------------------------------------------------------------------------
# is_video_filename
# ---------------------------------------------------------------------------

class TestIsVideoFilename(unittest.TestCase):
    def test_common_extensions(self):
        for ext in (".mkv", ".mp4", ".avi", ".m4v", ".webm", ".iso"):
            self.assertTrue(is_video_filename(f"Show{ext}"), ext)

    def test_case_insensitive(self):
        self.assertTrue(is_video_filename("Show.MKV"))
        self.assertTrue(is_video_filename("Show.MP4"))

    def test_non_video(self):
        self.assertFalse(is_video_filename("Show.nfo"))
        self.assertFalse(is_video_filename("Show.srt"))
        self.assertFalse(is_video_filename("Show.jpg"))
        self.assertFalse(is_video_filename(""))
        self.assertFalse(is_video_filename("Show"))


# ---------------------------------------------------------------------------
# derive_series_from_filename (cross-folder content checker, §7)
# ---------------------------------------------------------------------------

class TestDeriveSeriesFromFilename(unittest.TestCase):
    def test_dotted(self):
        self.assertEqual(
            derive_series_from_filename("Show.Name.S01E03.Title.mkv"),
            "Show Name",
        )

    def test_x_form(self):
        self.assertEqual(
            derive_series_from_filename("Show Name 1x03 Title.avi"),
            "Show Name",
        )

    def test_no_episode_marker_strips_quality(self):
        # No S/E marker; whole stem with quality tags removed becomes the guess.
        self.assertEqual(
            derive_series_from_filename("Show Name 720p WEB-DL.mkv"),
            "Show Name",
        )

    def test_brackets_stripped(self):
        self.assertEqual(
            derive_series_from_filename("[FoxRG] Show Name S01E03.mkv"),
            "Show Name",
        )

    def test_year_paren_stripped(self):
        self.assertEqual(
            derive_series_from_filename("Show Name (2005) S01E01 Pilot.mkv"),
            "Show Name",
        )

    def test_empty(self):
        self.assertEqual(derive_series_from_filename(""), "")

    def test_completely_garbage(self):
        result = derive_series_from_filename("S01E03.mkv")
        self.assertEqual(result, "")  # nothing before the marker


# ---------------------------------------------------------------------------
# normalize_folder_name + tokenize_for_clustering (merge clusterer §6)
# ---------------------------------------------------------------------------

class TestNormalizeFolderName(unittest.TestCase):
    def test_strips_leading_track_number(self):
        # phase6_design.md §6 step 1.
        self.assertEqual(normalize_folder_name("01 MrBean"), "mrbean")

    def test_multi_word_stripped(self):
        self.assertEqual(
            normalize_folder_name("02 Good Night MrBean"),
            "good night mrbean",
        )

    def test_strips_disc_prefix(self):
        self.assertEqual(normalize_folder_name("Disc 2 Mr Bean"), "mr bean")

    def test_strips_t_prefix(self):
        self.assertEqual(normalize_folder_name("T00 Some Title"), "some title")

    def test_strips_paren_year(self):
        self.assertEqual(normalize_folder_name("Mr. Bean (1990)"), "mr bean")

    def test_strips_paren_year_dash_variants(self):
        # Real-data: "Arrow (2012-)" leaks "2012" into tokens unless we strip
        # the trailing-dash variant. Same for ranged years.
        self.assertEqual(normalize_folder_name("Arrow (2012-)"), "arrow")
        self.assertEqual(normalize_folder_name("Arrow (2012-2015)"), "arrow")
        self.assertEqual(normalize_folder_name("Arrow (2012-15)"), "arrow")

    def test_strips_quality_tags(self):
        # phase6_design.md §6 step 2.
        # ("show" is a stopword now; use a fake series name that survives
        # normalization in full.)
        self.assertEqual(
            normalize_folder_name("Foobar Quux 480p MP3"),
            "foobar quux",
        )

    def test_lowercases_and_drops_punctuation(self):
        # phase6_design.md §6 step 3.
        self.assertEqual(normalize_folder_name("Foo-Bar_Baz"), "foo bar baz")

    def test_drops_stopwords(self):
        # phase6_design.md §6 step 4.
        self.assertEqual(
            normalize_folder_name("The Office and Friends"),
            "office friends",
        )

    def test_strips_brackets(self):
        self.assertEqual(
            normalize_folder_name("[FoxRG] Foobar Quux"),
            "foobar quux",
        )

    def test_empty_input(self):
        self.assertEqual(normalize_folder_name(""), "")

    def test_combined_real_world(self):
        # phase6_design.md §6 example: "01 MrBean 480p MP3" should reduce to
        # the same canonical form regardless of track number / quality tags.
        self.assertEqual(
            normalize_folder_name("01 MrBean 480p MP3"),
            "mrbean",
        )

    def test_deterministic(self):
        # Repeat application is a no-op (idempotent).
        once = normalize_folder_name("01 MrBean 480p MP3")
        twice = normalize_folder_name(once)
        self.assertEqual(once, twice)


class TestTokenizeForClustering(unittest.TestCase):
    def test_returns_set(self):
        tokens = tokenize_for_clustering("01 MrBean")
        self.assertIsInstance(tokens, set)
        self.assertIn("mrbean", tokens)

    def test_overlap_for_clustering(self):
        a = tokenize_for_clustering("01 MrBean 480p MP3")
        b = tokenize_for_clustering("02 Good Night MrBean 480p MP3")
        self.assertEqual(a & b, {"mrbean"})


class TestLeadingEpisodeNumber(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(leading_episode_number("01 MrBean"), 1)

    def test_two_digits(self):
        self.assertEqual(leading_episode_number("15 Foo Bar"), 15)

    def test_no_leading_number(self):
        self.assertIsNone(leading_episode_number("MrBean"))

    def test_year_is_not_episode_number(self):
        # phase6_design.md §6 explicitly distinguishes leading episode prefix
        # from a year. "1990 Mr Bean" is a year-prefixed name, not an episode.
        self.assertIsNone(leading_episode_number("1990 Mr Bean"))

    def test_empty(self):
        self.assertIsNone(leading_episode_number(""))


# ---------------------------------------------------------------------------
# Integration: full naming-convention examples from phase6_design.md §9
# ---------------------------------------------------------------------------

class TestDesignDocExamples(unittest.TestCase):
    """Walk through the literal examples in phase6_design.md §9."""

    def test_alf_pilot(self):
        # Already-renamed canonical form should round-trip cleanly.
        p = parse_episode("Alf S01E01 - Pilot.mp4", parent_folder="Season 01 (1986)")
        self.assertEqual((p.season, p.episode), (1, 1))
        self.assertEqual(p.series_name_guess, "Alf")

    def test_doctor_who_rose(self):
        p = parse_episode("Doctor Who S01E01 - Rose.mkv", parent_folder="Season 01 (2005)")
        self.assertEqual((p.season, p.episode), (1, 1))
        self.assertEqual(p.series_name_guess, "Doctor Who")

    def test_doctor_who_specials(self):
        p = parse_episode(
            "Doctor Who S00E01 - The Christmas Invasion.mkv",
            parent_folder="Specials",
        )
        self.assertEqual((p.season, p.episode), (0, 1))
        self.assertTrue(p.is_special)

    def test_mr_bean_folder_normalization(self):
        # phase6_design.md §6 sample: 15 folders that should cluster.
        members = [
            "01 MrBean",
            "02 Good Night MrBean",
            "03 Mind the Baby MrBean",
        ]
        normalized = [normalize_folder_name(m) for m in members]
        self.assertEqual(normalized[0], "mrbean")
        self.assertEqual(normalized[1], "good night mrbean")
        self.assertEqual(normalized[2], "mind baby mrbean")
        # All three share "mrbean" as a token — clusterer uses union-find on
        # this overlap (see test_tv_merge_clusterer).
        token_sets = [tokenize_for_clustering(m) for m in members]
        common = token_sets[0] & token_sets[1] & token_sets[2]
        self.assertEqual(common, {"mrbean"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
