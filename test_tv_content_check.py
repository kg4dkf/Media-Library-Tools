#!/usr/bin/env python3
"""
test_tv_content_check.py - Unit tests for tv_content_check.py.

Run:
    py -3.13 test_tv_content_check.py
    py -3.13 -m unittest test_tv_content_check
"""

from __future__ import annotations

import unittest

from tv_content_check import (
    DEFAULT_MIN_EPISODES_FOR_CHECK,
    DEFAULT_SIMILARITY_THRESHOLD,
    VERDICT_EMPTY_FOLDER,
    VERDICT_MIXED,
    VERDICT_NO_EPISODES,
    VERDICT_NO_PARSES,
    VERDICT_OK,
    VERDICT_TOO_FEW,
    ContentCheckResult,
    _bidirectional_containment,
    check_folder_content,
)


# ---------------------------------------------------------------------------
# Clean matches (folder == dominant)
# ---------------------------------------------------------------------------

class TestCleanMatch(unittest.TestCase):
    def test_doctor_who_all_match(self):
        result = check_folder_content(
            "Doctor Who (2005)",
            [
                "Doctor Who S01E01 Rose.mkv",
                "Doctor Who S01E02 The End of the World.mkv",
                "Doctor Who S01E03 The Unquiet Dead.mkv",
            ],
        )
        self.assertEqual(result.verdict, VERDICT_OK)
        self.assertFalse(result.flag_as_mixed)
        self.assertEqual(result.dominant_count, 3)
        self.assertEqual(result.other_count, 0)
        self.assertEqual(result.similarity, 1.0)
        self.assertEqual(result.dominant_filename_series, "Doctor Who")

    def test_friends_with_year_in_folder(self):
        # Folder has a year qualifier; filenames don't. They should still match.
        result = check_folder_content(
            "Friends (1994)",
            [
                "Friends S01E01 The One Where Monica Gets a Roommate.mkv",
                "Friends S01E02 The One With the Sonogram.mkv",
                "Friends S01E03.mkv",
            ],
        )
        self.assertEqual(result.verdict, VERDICT_OK)
        self.assertFalse(result.flag_as_mixed)

    def test_dotted_dvd_rip(self):
        result = check_folder_content(
            "The Office",
            [
                "The.Office.S01E01.Pilot.mkv",
                "The.Office.S01E02.Diversity.Day.mkv",
                "The.Office.S01E03.Health.Care.mkv",
            ],
        )
        self.assertEqual(result.verdict, VERDICT_OK)
        self.assertFalse(result.flag_as_mixed)

    def test_x_form_episodes(self):
        result = check_folder_content(
            "Star Trek",
            [
                "Star Trek 1x01 The Cage.mkv",
                "Star Trek 1x02 Where No Man.mkv",
                "Star Trek 1x03 The Corbomite Maneuver.mkv",
            ],
        )
        self.assertEqual(result.verdict, VERDICT_OK)


# ---------------------------------------------------------------------------
# Clear mismatches (entire folder is the wrong show)
# ---------------------------------------------------------------------------

class TestClearMismatch(unittest.TestCase):
    def test_friends_folder_with_office_episodes(self):
        # Misplaced rip — folder says Friends but every episode is The Office.
        result = check_folder_content(
            "Friends (1994)",
            [
                "The Office S01E01 Pilot.mkv",
                "The Office S01E02 Diversity Day.mkv",
                "The Office S01E03 Health Care.mkv",
            ],
        )
        self.assertEqual(result.verdict, VERDICT_MIXED)
        self.assertTrue(result.flag_as_mixed)
        self.assertEqual(result.dominant_count, 3)
        self.assertEqual(result.other_count, 0)
        self.assertLess(result.similarity, DEFAULT_SIMILARITY_THRESHOLD)
        self.assertEqual(result.dominant_filename_series, "The Office")

    def test_completely_disjoint_tokens(self):
        result = check_folder_content(
            "Doctor Who",
            [
                "Sherlock S01E01 A Study in Pink.mkv",
                "Sherlock S01E02 The Blind Banker.mkv",
                "Sherlock S01E03 The Great Game.mkv",
            ],
        )
        self.assertEqual(result.verdict, VERDICT_MIXED)
        self.assertTrue(result.flag_as_mixed)
        self.assertEqual(result.similarity, 0.0)


# ---------------------------------------------------------------------------
# Partial mismatch — folder matches dominant, but contamination present
# ---------------------------------------------------------------------------

class TestPartialMismatch(unittest.TestCase):
    def test_dominant_matches_folder_with_outliers(self):
        # 4 of 6 are the right show, 2 are misplaced. Folder matches the
        # dominant, so flag_as_mixed is False — but other_count surfaces
        # the contamination for the user's review.
        result = check_folder_content(
            "Doctor Who",
            [
                "Doctor Who S01E01.mkv",
                "Doctor Who S01E02.mkv",
                "Doctor Who S01E03.mkv",
                "Doctor Who S01E04.mkv",
                "Sherlock S01E01.mkv",
                "Sherlock S01E02.mkv",
            ],
        )
        self.assertEqual(result.verdict, VERDICT_OK)
        self.assertFalse(result.flag_as_mixed)
        self.assertEqual(result.dominant_count, 4)
        self.assertEqual(result.other_count, 2)
        self.assertIn("different derived series-name", result.notes)

    def test_minority_dominant(self):
        # 3 Sherlock + 2 Doctor Who in a "Doctor Who" folder. Dominant is
        # Sherlock (3 of 5). Folder doesn't match dominant -> flagged.
        result = check_folder_content(
            "Doctor Who",
            [
                "Sherlock S01E01.mkv",
                "Sherlock S01E02.mkv",
                "Sherlock S01E03.mkv",
                "Doctor Who S01E01.mkv",
                "Doctor Who S01E02.mkv",
            ],
        )
        self.assertEqual(result.verdict, VERDICT_MIXED)
        self.assertEqual(result.dominant_filename_series, "Sherlock")
        self.assertEqual(result.dominant_count, 3)
        self.assertEqual(result.other_count, 2)


# ---------------------------------------------------------------------------
# Inconclusive cases
# ---------------------------------------------------------------------------

class TestInconclusive(unittest.TestCase):
    def test_no_episode_filenames(self):
        result = check_folder_content("Doctor Who", [])
        self.assertEqual(result.verdict, VERDICT_NO_EPISODES)
        self.assertFalse(result.flag_as_mixed)
        self.assertTrue(result.is_inconclusive)

    def test_unparseable_filenames(self):
        # Filenames produce no series-name derivation (just S##E## with no
        # head text). The check is inconclusive — we can't compare.
        result = check_folder_content(
            "Doctor Who",
            [
                "S01E01.mkv",
                "S01E02.mkv",
                "S01E03.mkv",
            ],
        )
        self.assertEqual(result.verdict, VERDICT_NO_PARSES)
        self.assertFalse(result.flag_as_mixed)
        self.assertEqual(result.unparseable_count, 3)

    def test_too_few_parseable_episodes(self):
        # Only one parseable episode; below default min_episodes_for_check=2.
        result = check_folder_content(
            "Doctor Who",
            ["Doctor Who S01E01.mkv"],
        )
        self.assertEqual(result.verdict, VERDICT_TOO_FEW)
        self.assertFalse(result.flag_as_mixed)

    def test_empty_folder_name(self):
        result = check_folder_content(
            "",
            ["Doctor Who S01E01.mkv", "Doctor Who S01E02.mkv"],
        )
        self.assertEqual(result.verdict, VERDICT_EMPTY_FOLDER)

    def test_folder_name_normalizes_to_empty(self):
        # "The And Of" all stopwords -> empty token set after normalization.
        result = check_folder_content(
            "The And Of",
            ["Doctor Who S01E01.mkv", "Doctor Who S01E02.mkv"],
        )
        self.assertEqual(result.verdict, VERDICT_EMPTY_FOLDER)

    def test_blank_filenames_skipped(self):
        # Empty-string filenames in the iterable shouldn't crash; they're
        # silently skipped.
        result = check_folder_content(
            "Doctor Who",
            ["", "Doctor Who S01E01.mkv", "  ", "Doctor Who S01E02.mkv"],
        )
        self.assertEqual(result.verdict, VERDICT_OK)
        self.assertEqual(result.dominant_count, 2)


# ---------------------------------------------------------------------------
# Threshold tuning
# ---------------------------------------------------------------------------

class TestThreshold(unittest.TestCase):
    def test_strict_threshold_partial_overlap(self):
        # Bidirectional containment: folder "Doctor Who" with dominant
        # "Doctor Strange" share {doctor} only. Containment in either
        # direction is 1/2 = 0.5. Threshold 0.7 -> flag; threshold 0.4 -> ok.
        files = [
            "Doctor Strange S01E01.mkv",
            "Doctor Strange S01E02.mkv",
            "Doctor Strange S01E03.mkv",
        ]
        result_loose = check_folder_content(
            "Doctor Who", files, similarity_threshold=0.4,
        )
        self.assertEqual(result_loose.verdict, VERDICT_OK)
        result_strict = check_folder_content(
            "Doctor Who", files, similarity_threshold=0.7,
        )
        self.assertEqual(result_strict.verdict, VERDICT_MIXED)

    def test_folder_subset_of_dominant_not_flagged(self):
        # The real-data fix: "Family Guy" folder with filenames like
        # "Family Guy 101 Death Has A Shadow.mkv" should NOT be flagged.
        # Under the old Jaccard metric this was a false positive.
        files = [
            "Family Guy 101 Death Has A Shadow.mkv",
            "Family Guy 102 I Never Met The Dead Man.mkv",
            "Family Guy 103 Chitty Chitty Death Bang.mkv",
        ]
        result = check_folder_content("Family Guy", files)
        self.assertEqual(result.verdict, VERDICT_OK)
        self.assertEqual(result.similarity, 1.0)

    def test_dominant_subset_of_folder_not_flagged(self):
        # Reverse direction: folder "Star Trek The Original Series" with
        # filenames "Star Trek S01E01.mkv" — abbreviated filenames are fine.
        files = [
            "Star Trek S01E01.mkv",
            "Star Trek S01E02.mkv",
            "Star Trek S01E03.mkv",
        ]
        result = check_folder_content("Star Trek The Original Series", files)
        self.assertEqual(result.verdict, VERDICT_OK)
        self.assertEqual(result.similarity, 1.0)

    def test_validation_threshold_range(self):
        with self.assertRaises(ValueError):
            check_folder_content("X", ["A S01E01.mkv"], similarity_threshold=1.5)

    def test_validation_min_episodes_positive(self):
        with self.assertRaises(ValueError):
            check_folder_content("X", ["A S01E01.mkv"], min_episodes_for_check=0)


# ---------------------------------------------------------------------------
# CSV row export
# ---------------------------------------------------------------------------

class TestCsvRow(unittest.TestCase):
    def test_to_csv_row_keys(self):
        result = check_folder_content(
            "Friends",
            [
                "The Office S01E01.mkv",
                "The Office S01E02.mkv",
            ],
        )
        row = result.to_csv_row()
        # phase6_design.md §1 mixed_content.csv columns
        for col in (
            "folder_name",
            "dominant_filename_series",
            "episode_count_dominant",
            "episode_count_other",
            "action",
            "manual_resolution",
        ):
            self.assertIn(col, row)
        self.assertEqual(row["folder_name"], "Friends")
        self.assertEqual(row["dominant_filename_series"], "The Office")
        self.assertEqual(row["episode_count_dominant"], 2)
        self.assertEqual(row["episode_count_other"], 0)
        self.assertEqual(row["action"], "REVIEW")  # design §7 default
        self.assertEqual(row["manual_resolution"], "")

    def test_to_csv_row_diagnostics(self):
        result = check_folder_content(
            "Doctor Who",
            ["Doctor Who S01E01.mkv", "Doctor Who S01E02.mkv"],
        )
        row = result.to_csv_row()
        self.assertIn("similarity", row)
        self.assertIn("verdict", row)
        self.assertIn("notes", row)


# ---------------------------------------------------------------------------
# Default constants
# ---------------------------------------------------------------------------

class TestDefaults(unittest.TestCase):
    def test_default_constants(self):
        self.assertEqual(DEFAULT_SIMILARITY_THRESHOLD, 0.5)
        self.assertEqual(DEFAULT_MIN_EPISODES_FOR_CHECK, 2)


# ---------------------------------------------------------------------------
# Realistic real-world scenarios
# ---------------------------------------------------------------------------

class TestRealWorldScenarios(unittest.TestCase):
    def test_dvd_rip_with_dotted_filenames(self):
        result = check_folder_content(
            "Star Trek",
            [
                "Star.Trek.1x01.The.Cage.1080p.BluRay.x264.mkv",
                "Star.Trek.1x02.Where.No.Man.1080p.BluRay.x264.mkv",
                "Star.Trek.1x03.The.Corbomite.Maneuver.1080p.BluRay.x264.mkv",
            ],
        )
        self.assertEqual(result.verdict, VERDICT_OK)

    def test_release_group_brackets_stripped(self):
        result = check_folder_content(
            "Doctor Who",
            [
                "[FoxRG] Doctor Who S01E01 Rose.mkv",
                "[FoxRG] Doctor Who S01E02 The End of the World.mkv",
                "[FoxRG] Doctor Who S01E03 The Unquiet Dead.mkv",
            ],
        )
        self.assertEqual(result.verdict, VERDICT_OK)

    def test_misplaced_rip_in_anthology_folder(self):
        # Real-world case: someone's dragged an episode of a show into the
        # wrong series folder. Most files are right, one is wrong. Should
        # still resolve to OK (folder matches dominant) but surface other_count.
        result = check_folder_content(
            "Twilight Zone",
            [
                "Twilight Zone S01E01.mkv",
                "Twilight Zone S01E02.mkv",
                "Twilight Zone S01E03.mkv",
                "Twilight Zone S01E04.mkv",
                "Outer Limits S01E01.mkv",
            ],
        )
        self.assertEqual(result.verdict, VERDICT_OK)
        self.assertEqual(result.dominant_count, 4)
        self.assertEqual(result.other_count, 1)



# ---------------------------------------------------------------------------
# Helper: bidirectional containment metric
# ---------------------------------------------------------------------------

class TestBidirectionalContainment(unittest.TestCase):
    def test_full_containment_either_direction(self):
        # Folder is subset of dominant -> 1.0
        self.assertEqual(
            _bidirectional_containment({"family", "guy"},
                                        {"family", "guy", "101", "death"}),
            1.0,
        )
        # Dominant is subset of folder -> 1.0
        self.assertEqual(
            _bidirectional_containment({"star", "trek", "original"},
                                        {"star", "trek"}),
            1.0,
        )

    def test_no_overlap(self):
        self.assertEqual(
            _bidirectional_containment({"friends"}, {"office"}),
            0.0,
        )

    def test_partial_overlap_picks_higher_side(self):
        # {a,b} vs {b,c}: |inter|=1, max(1/2, 1/2) = 0.5
        self.assertEqual(
            _bidirectional_containment({"a", "b"}, {"b", "c"}),
            0.5,
        )

    def test_empty_set_returns_zero(self):
        self.assertEqual(_bidirectional_containment(set(), {"a"}), 0.0)
        self.assertEqual(_bidirectional_containment({"a"}, set()), 0.0)
        self.assertEqual(_bidirectional_containment(set(), set()), 0.0)

    def test_identical_sets(self):
        self.assertEqual(
            _bidirectional_containment({"a", "b", "c"}, {"a", "b", "c"}),
            1.0,
        )



if __name__ == "__main__":
    unittest.main(verbosity=2)
