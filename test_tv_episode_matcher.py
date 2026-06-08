#!/usr/bin/env python3
"""Tests for tv_episode_matcher.py — pure-stdlib title-fuzzy-match logic."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Dict, Tuple

sys.path.insert(0, str(Path(__file__).parent))

from tv_episode_matcher import (
    EpisodeMatch,
    episodes_map_from_tmdb_seasons,
    extract_title_hint,
    match_filename_to_episode,
    normalize_for_match,
    title_similarity,
)


class TestNormalize(unittest.TestCase):
    def test_lowercase_and_strip_accents(self):
        self.assertEqual(normalize_for_match("Café Élégance"), "cafe elegance")

    def test_separators_become_spaces(self):
        self.assertEqual(normalize_for_match("foo_bar.baz-qux"), "foo bar baz qux")

    def test_collapse_whitespace(self):
        self.assertEqual(normalize_for_match("  a    b  "), "a b")

    def test_drops_punctuation(self):
        self.assertEqual(normalize_for_match("Tom's Adventure!"), "tom s adventure")


class TestExtractTitleHint(unittest.TestCase):
    def test_basic_filename(self):
        h = extract_title_hint("Underdog - 03 - Go Snow Part 4.avi")
        # Stripping leading "Underdog -" requires series_name; without it
        # we keep the whole thing minus the "03" episode marker.
        self.assertIn("Underdog", h)
        self.assertIn("Go Snow", h)

    def test_with_series_name(self):
        h = extract_title_hint("Underdog - 03 - Go Snow Part 4.avi",
                                series_name="Underdog")
        # Series prefix dropped — note the helper rebuilds in normalized
        # form when stripping series, so only check the meaningful tokens.
        self.assertIn("go snow", h.lower())
        self.assertNotIn("underdog", h.lower())

    def test_strip_sxxexx(self):
        # When series_name strips, we drop case, so check case-insensitively.
        h = extract_title_hint("Show S01E05 The Big One.mkv",
                                series_name="Show")
        self.assertNotIn("S01E05", h)
        self.assertNotIn("s01e05", h.lower())
        self.assertIn("big one", h.lower())

    def test_strip_quality_cruft(self):
        h = extract_title_hint("Show - The Pilot 1080p x264 BluRay.mkv",
                                series_name="Show")
        self.assertNotIn("1080p", h.lower())
        self.assertNotIn("x264", h.lower())
        self.assertNotIn("bluray", h.lower())
        self.assertIn("pilot", h.lower())

    def test_strip_brackets(self):
        h = extract_title_hint("[ax]_Show_-_S01E03_-_Pilot_[a5d9].mkv",
                                series_name="Show")
        self.assertIn("pilot", h.lower())
        self.assertNotIn("[", h)

    def test_strip_leading_disc_number(self):
        h = extract_title_hint("01 - Pilot.mkv")
        self.assertIn("Pilot", h)
        self.assertNotIn("01", h)


class TestTitleSimilarity(unittest.TestCase):
    def test_exact_match(self):
        self.assertGreater(title_similarity("Pilot", "Pilot"), 0.99)

    def test_substring_match(self):
        # Filename has prefix; title is contained.
        s = title_similarity("Show 03 Go Snow Part 4", "Go Snow Part 4")
        self.assertGreater(s, 0.55)

    def test_unrelated_titles(self):
        self.assertLess(title_similarity("The Pilot", "Cat & Mouse"), 0.4)

    def test_empty_inputs(self):
        self.assertEqual(title_similarity("", "x"), 0.0)
        self.assertEqual(title_similarity("x", ""), 0.0)


class TestMatchFilenameToEpisode(unittest.TestCase):
    def setUp(self):
        # Tom and Jerry: a few real episode titles from the original 1940
        # Tom & Jerry shorts.
        self.tj_episodes: Dict[Tuple[int, int], str] = {
            (1, 1): "Puss Gets the Boot",
            (1, 2): "The Midnight Snack",
            (1, 3): "The Night Before Christmas",
            (1, 4): "Fraidy Cat",
            (1, 5): "Dog Trouble",
            (2, 1): "The Mouse Comes to Dinner",
            (2, 2): "The Bowling Alley-Cat",
        }

    def test_high_confidence_substring_match(self):
        m = match_filename_to_episode(
            "Tom and Jerry - Puss Gets the Boot.avi",
            self.tj_episodes, series_name="Tom and Jerry")
        self.assertIsNotNone(m)
        self.assertEqual((m.season, m.episode), (1, 1))
        self.assertEqual(m.confidence, "HIGH")

    def test_high_confidence_with_episode_num_marker(self):
        m = match_filename_to_episode(
            "Tom and Jerry - 04 - Fraidy Cat.avi",
            self.tj_episodes, series_name="Tom and Jerry")
        self.assertIsNotNone(m)
        self.assertEqual((m.season, m.episode), (1, 4))

    def test_medium_confidence_partial_match(self):
        # Slightly garbled title — close but not exact.
        m = match_filename_to_episode(
            "Tom and Jerry - Bowling Alley.avi",  # missing "Cat"
            self.tj_episodes, series_name="Tom and Jerry")
        self.assertIsNotNone(m)
        self.assertEqual((m.season, m.episode), (2, 2))

    def test_no_match_for_unrelated_filename(self):
        m = match_filename_to_episode(
            "Tom and Jerry - Random Garbage Word.avi",
            self.tj_episodes, series_name="Tom and Jerry")
        self.assertIsNone(m)

    def test_empty_episodes_map_returns_none(self):
        m = match_filename_to_episode("Pilot.avi", {})
        self.assertIsNone(m)

    def test_too_short_hint_returns_none(self):
        # Filename has nothing left after stripping cruft.
        m = match_filename_to_episode("S01E01.mkv", self.tj_episodes,
                                       series_name="Tom and Jerry")
        self.assertIsNone(m)


class TestEpisodesMapFromTmdb(unittest.TestCase):
    def test_basic(self):
        seasons = [
            {"season_number": 1, "episodes": [
                {"episode_number": 1, "name": "Pilot"},
                {"episode_number": 2, "name": "Two"},
            ]},
            {"season_number": 2, "episodes": [
                {"episode_number": 1, "name": "Return"},
            ]},
        ]
        m = episodes_map_from_tmdb_seasons(seasons)
        self.assertEqual(m, {
            (1, 1): "Pilot",
            (1, 2): "Two",
            (2, 1): "Return",
        })

    def test_missing_titles_skipped(self):
        seasons = [
            {"season_number": 1, "episodes": [
                {"episode_number": 1, "name": ""},
                {"episode_number": 2, "name": "Has Title"},
            ]},
        ]
        m = episodes_map_from_tmdb_seasons(seasons)
        self.assertEqual(m, {(1, 2): "Has Title"})


class TestRealisticScenarios(unittest.TestCase):
    """Scenarios reflecting the actual stuck rows in the user's library."""

    def test_underdog_safe_waif(self):
        # Underdog has 4 actual canonical episodes the user already
        # got — we want to match the rest similarly.
        episodes = {
            (1, 1): "Safe Waif",
            (1, 2): "March of the Monsters",
            (1, 3): "The Witch of Pickyoon",
            (1, 4): "From Hopeless to Helpless",
            (1, 5): "Whistler's Mother",
            (1, 6): "Go Snow Part 1",
            (1, 7): "Go Snow Part 4",
            (1, 8): "Zot - Part 1",
        }
        # Filename that user's library has, no SxxExx.
        m = match_filename_to_episode(
            "Underdog Safe Waif.avi", episodes, series_name="Underdog")
        self.assertIsNotNone(m)
        self.assertEqual((m.season, m.episode), (1, 1))

    def test_underdog_with_part_marker(self):
        episodes = {
            (1, 6): "Go Snow Part 1",
            (1, 7): "Go Snow Part 4",
        }
        m = match_filename_to_episode(
            "Underdog - Go Snow Part 4.avi", episodes,
            series_name="Underdog")
        self.assertIsNotNone(m)
        self.assertEqual((m.season, m.episode), (1, 7))

    def test_low_confidence_rejected(self):
        episodes = {
            (1, 1): "The Big One",
            (1, 2): "Another Episode",
        }
        m = match_filename_to_episode(
            "Show - cat dog tree.avi", episodes, series_name="Show")
        # No real match — should be None.
        self.assertIsNone(m)


if __name__ == "__main__":
    unittest.main(verbosity=2)
