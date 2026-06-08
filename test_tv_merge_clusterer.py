#!/usr/bin/env python3
"""
test_tv_merge_clusterer.py - Unit tests for tv_merge_clusterer.py.

Run:
    py -3.13 test_tv_merge_clusterer.py
    py -3.13 -m unittest test_tv_merge_clusterer
"""

from __future__ import annotations

import unittest

from tv_merge_clusterer import (
    DEFAULT_CONTAINMENT_THRESHOLD,
    DEFAULT_JACCARD_THRESHOLD,
    DEFAULT_MAX_GROUP_SIZE,
    DEFAULT_MIN_GROUP_SIZE,
    DEFAULT_MIN_SHARED_CHARS,
    DEFAULT_MIN_TOKEN_OVERLAP,
    MergeGroup,
    _jaccard,
    _shared_tokens,
    find_merge_candidates,
)


# ---------------------------------------------------------------------------
# Trivial / edge-case inputs
# ---------------------------------------------------------------------------

class TestEmptyAndTrivial(unittest.TestCase):
    def test_empty_input(self):
        self.assertEqual(find_merge_candidates([]), [])

    def test_single_folder(self):
        self.assertEqual(find_merge_candidates(["Mr Bean"]), [])

    def test_two_folders_below_min_group_size(self):
        # Default min_group_size=3, so a perfect 2-folder match still emits nothing.
        groups = find_merge_candidates(["Mr Bean Pilot", "Mr Bean Episode 2"])
        self.assertEqual(groups, [])

    def test_below_min_size_with_relaxed_overlap(self):
        # Even with min_token_overlap=1, a 2-folder cluster is suppressed
        # because min_group_size defaults to 3.
        groups = find_merge_candidates(
            ["Mr Bean Pilot", "Mr Bean Episode 2"],
            min_token_overlap=1,
        )
        self.assertEqual(groups, [])

    def test_below_min_size_with_min_size_two(self):
        # Lower min_group_size to 2 to confirm pair-only clusters can emit.
        groups = find_merge_candidates(
            ["Mr Bean Pilot", "Mr Bean Episode 2"],
            min_token_overlap=1,
            min_group_size=2,
        )
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].member_folder_count, 2)

    def test_validation_jaccard_out_of_range(self):
        with self.assertRaises(ValueError):
            find_merge_candidates(["a", "b", "c"], jaccard_threshold=1.5)

    def test_validation_min_overlap(self):
        with self.assertRaises(ValueError):
            find_merge_candidates(["a", "b", "c"], min_token_overlap=0)

    def test_validation_min_group_size(self):
        with self.assertRaises(ValueError):
            find_merge_candidates(["a", "b", "c"], min_group_size=1)

    def test_skips_empty_strings(self):
        groups = find_merge_candidates(
            ["", "  ", "Mr Bean", "Mr Bean", "Mr Bean"],
            min_token_overlap=1,
        )
        # Three "Mr Bean" entries cluster into one group of three.
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].member_folder_count, 3)

    def test_skips_unnormalizable(self):
        # Names that normalize to zero tokens are dropped before clustering.
        # Pure stopwords -> no tokens -> skipped.
        groups = find_merge_candidates(
            ["The", "And", "Mr Bean", "Mr Bean", "Mr Bean"],
            min_token_overlap=1,
        )
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].member_folder_count, 3)


# ---------------------------------------------------------------------------
# Conservative-default behavior (the §6 design as written)
# ---------------------------------------------------------------------------

class TestTunedDefaults(unittest.TestCase):
    """Verify behavior under the tuned defaults that ship in the module."""

    def test_mr_bean_clusters_under_default(self):
        # Containment criterion: each pair has containment 1.0 on the
        # 'mrbean' token (smaller token-set has size 1, fully contained
        # in the other). Shared chars = 6, meets the min_shared_chars floor.
        members = [
            "01 MrBean",
            "02 Good Night MrBean",
            "03 Mind the Baby MrBean",
        ]
        groups = find_merge_candidates(members)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].member_folder_count, 3)
        self.assertEqual(groups[0].suggested_series_name, "Mrbean")
        self.assertEqual(groups[0].shared_tokens, ["mrbean"])

    def test_three_token_overlap_does_cluster(self):
        # Both legacy criteria satisfied: 4 shared tokens (>=3) and
        # also containment 4/5=0.8 (>=0.5).
        members = [
            "Some Long Show Name Pilot",
            "Some Long Show Name Episode 2",
            "Some Long Show Name Episode 3",
        ]
        groups = find_merge_candidates(members)
        self.assertEqual(len(groups), 1)
        self.assertGreaterEqual(groups[0].shared_token_count, 3)

    def test_default_constants(self):
        # phase6_design.md §14: tv_merge_threshold=0.7, tv_min_merge_size=3.
        self.assertEqual(DEFAULT_JACCARD_THRESHOLD, 0.7)
        self.assertEqual(DEFAULT_MIN_GROUP_SIZE, 3)
        self.assertEqual(DEFAULT_MIN_TOKEN_OVERLAP, 3)
        # Tuned defaults (not in config — algorithm internals).
        self.assertEqual(DEFAULT_CONTAINMENT_THRESHOLD, 0.5)
        self.assertEqual(DEFAULT_MIN_SHARED_CHARS, 6)
        self.assertEqual(DEFAULT_MAX_GROUP_SIZE, 100)


# ---------------------------------------------------------------------------
# Multi-cluster separation
# ---------------------------------------------------------------------------

class TestMultiCluster(unittest.TestCase):
    def test_two_separate_clusters(self):
        members = [
            # Mr Bean cluster
            "Mr Bean Pilot",
            "Mr Bean Diversity Day",
            "Mr Bean Health Care",
            # Office cluster (distinct tokens)
            "The Office Workplace Pilot",
            "The Office Workplace Diversity Day",
            "The Office Workplace Health Care",
        ]
        # Use min_token_overlap=2 so each cluster forms but they don't merge
        # (Mr Bean and Office share "diversity day" / "health care" only by
        # coincidence-of-test-strings; let's tweak input below).
        members = [
            "Mr Bean Pilot Episode",
            "Mr Bean Pilot Aftermath",
            "Mr Bean Pilot Sequel",
            "Doctor Who Christmas Special",
            "Doctor Who Christmas Carol",
            "Doctor Who Christmas Invasion",
        ]
        groups = find_merge_candidates(members, min_token_overlap=2)
        self.assertEqual(len(groups), 2)
        # Groups sorted by smallest-member-name (case-insensitive).
        self.assertTrue(all(g.member_folder_count == 3 for g in groups))
        # First should be 'Doctor Who...' (D < M alphabetically).
        self.assertTrue(groups[0].members[0].lower().startswith("doctor"))
        self.assertTrue(groups[1].members[0].lower().startswith("mr"))
        # Suggested names are derived from shared tokens.
        self.assertIn("Doctor", groups[0].suggested_series_name)
        self.assertIn("Mr", groups[1].suggested_series_name)

    def test_group_ids_assigned_in_sorted_order(self):
        # Disjoint vocabularies between the two clusters so they don't
        # accidentally merge via shared "Show"/"Pilot" tokens.
        members = [
            "Zeta Quux Foobar Plonk",
            "Zeta Quux Wibble Wobble",
            "Zeta Quux Splurge Twiddle",
            "Alpha Bravo Charlie Delta",
            "Alpha Bravo Echo Foxtrot",
            "Alpha Bravo Golf Hotel",
        ]
        groups = find_merge_candidates(members, min_token_overlap=2)
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0].group_id, 1)
        self.assertEqual(groups[1].group_id, 2)
        # Alpha sorts before Zeta -> group_id 1.
        self.assertTrue(groups[0].members[0].lower().startswith("alpha"))


# ---------------------------------------------------------------------------
# Transitive (union-find) clustering: A↔B, B↔C, but NOT A↔C
# ---------------------------------------------------------------------------

class TestTransitiveClustering(unittest.TestCase):
    def test_a_b_c_cluster_via_b(self):
        # Transitive clustering still works for single-episode-wrapper-style
        # folders (leading-num prefix), which the blob filter preserves.
        members = [
            "01 Apple Banana Pilot Sequel",      # A
            "02 Banana Pilot Sequel Cherry",     # B  -- shares 3 tokens with A
            "03 Cherry Date Sequel Plonk",       # C  -- shares 2 with B (cherry, sequel)
        ]
        groups = find_merge_candidates(members, min_token_overlap=1)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].member_folder_count, 3)
        # Strict intersection across all three is empty in this test; shared
        # tokens fall back to majority. Blob filter is bypassed because all
        # three have leading-num prefixes (100% leading-num pct).
        self.assertGreater(len(groups[0].shared_tokens), 0)

    def test_disconnected_pair_doesnt_merge(self):
        # A and B share 3 tokens (cluster), C is unrelated -> only one group of 2,
        # which is suppressed by min_group_size=3 default.
        members = [
            "Apple Banana Cherry Date Pilot",
            "Apple Banana Cherry Date Sequel",
            "Totally Unrelated Folder",
        ]
        groups = find_merge_candidates(members)
        self.assertEqual(groups, [])


# ---------------------------------------------------------------------------
# Suggested-name derivation
# ---------------------------------------------------------------------------

class TestSuggestedNames(unittest.TestCase):
    def test_single_shared_token(self):
        members = [
            "01 MrBean",
            "02 Good Night MrBean",
            "03 Mind the Baby MrBean",
        ]
        groups = find_merge_candidates(members, min_token_overlap=1)
        self.assertEqual(groups[0].suggested_series_name, "Mrbean")

    def test_multi_token_in_longest_order(self):
        # Shared tokens "doctor" and "who" both appear; ordered by their
        # position in the longest member.
        members = [
            "Doctor Who 2005 Pilot",
            "Doctor Who 2005 Christmas Special Episode",
            "Doctor Who 2005 Series Finale",
        ]
        groups = find_merge_candidates(members, min_token_overlap=2)
        self.assertEqual(len(groups), 1)
        self.assertIn("Doctor", groups[0].suggested_series_name)
        self.assertIn("Who", groups[0].suggested_series_name)
        # The order should be Doctor before Who.
        idx_d = groups[0].suggested_series_name.find("Doctor")
        idx_w = groups[0].suggested_series_name.find("Who")
        self.assertLess(idx_d, idx_w)

    def test_majority_fallback_when_strict_intersection_empty(self):
        # Transitive cluster where no token is shared by all members.
        # Use leading-num prefixes so the blob filter keeps it alive
        # (covered by the §6 single-episode-wrapper exemption).
        members = [
            "01 Apple Banana Pilot",       # has apple, banana, pilot
            "02 Banana Cherry Aftermath",  # has banana, cherry, aftermath
            "03 Cherry Date Finale",       # has cherry, date, finale
            "04 Apple Date Special",       # has apple, date, special  -- glues 1<->4
        ]
        groups = find_merge_candidates(members, min_token_overlap=1)
        self.assertEqual(len(groups), 1)
        # Majority fallback picks tokens appearing in >=2 members.
        self.assertNotEqual(groups[0].suggested_series_name, "")


# ---------------------------------------------------------------------------
# Quality / track / paren-year stripping carries through
# ---------------------------------------------------------------------------

class TestNormalizationCarriesThrough(unittest.TestCase):
    def test_quality_tags_stripped_before_clustering(self):
        # phase6_design.md §6 step 2: "Strip quality tags".
        members = [
            "01 MrBean 480p MP3",
            "02 Good Night MrBean BRRiP XviD-NoGRP",
            "03 Mind the Baby MrBean 720p AAC",
        ]
        groups = find_merge_candidates(members, min_token_overlap=1)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].suggested_series_name, "Mrbean")

    def test_paren_year_stripped(self):
        members = [
            "Mr Bean (1990) Pilot Aftermath",
            "Mr Bean (1990) Pilot Sequel",
            "Mr Bean (1990) Pilot Recap",
        ]
        groups = find_merge_candidates(members, min_token_overlap=2)
        self.assertEqual(len(groups), 1)


# ---------------------------------------------------------------------------
# CSV-row export
# ---------------------------------------------------------------------------

class TestCsvRow(unittest.TestCase):
    def test_to_csv_row_keys(self):
        members = [
            "01 MrBean",
            "02 Good Night MrBean",
            "03 Mind the Baby MrBean",
        ]
        groups = find_merge_candidates(members, min_token_overlap=1)
        row = groups[0].to_csv_row()
        # Columns from phase6_design.md §1 merge_candidates.csv.
        for col in (
            "group_id",
            "candidate_series_name",
            "member_folder_count",
            "member_folders",
            "shared_token_count",
            "suggested_match",
            "action",
        ):
            self.assertIn(col, row)
        self.assertEqual(row["group_id"], 1)
        self.assertEqual(row["member_folder_count"], 3)
        self.assertIn("|", row["member_folders"])
        self.assertEqual(row["action"], "REVIEW")  # default per design §2

    def test_to_csv_row_member_separator(self):
        members = ["Mr Bean Pilot Episode One", "Mr Bean Pilot Episode Two", "Mr Bean Pilot Episode Three"]
        groups = find_merge_candidates(members, min_token_overlap=2)
        row = groups[0].to_csv_row(member_separator=";;")
        self.assertIn(";;", row["member_folders"])


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

class TestInternals(unittest.TestCase):
    def test_jaccard_basics(self):
        self.assertEqual(_jaccard(set(), {"a"}), 0.0)
        self.assertEqual(_jaccard({"a"}, set()), 0.0)
        self.assertEqual(_jaccard({"a"}, {"a"}), 1.0)
        self.assertEqual(_jaccard({"a", "b"}, {"a"}), 0.5)
        self.assertAlmostEqual(_jaccard({"a", "b", "c"}, {"b", "c", "d"}), 2 / 4)

    def test_shared_tokens_strict(self):
        token_lists = [
            ["mr", "bean", "pilot"],
            ["mr", "bean", "sequel"],
            ["mr", "bean", "recap"],
        ]
        self.assertEqual(_shared_tokens(token_lists), ["mr", "bean"])

    def test_shared_tokens_majority_fallback(self):
        # No token in all three.
        token_lists = [
            ["alpha", "beta"],
            ["beta", "gamma"],
            ["gamma", "delta"],
        ]
        result = _shared_tokens(token_lists)
        # Tokens appearing in >=2 of 3 -> beta, gamma.
        self.assertEqual(set(result), {"beta", "gamma"})

    def test_shared_tokens_empty_input(self):
        self.assertEqual(_shared_tokens([]), [])


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

class TestDeterminism(unittest.TestCase):
    def test_repeated_runs_produce_identical_output(self):
        import random
        members = [
            "01 MrBean", "02 Good Night MrBean", "03 Mind the Baby MrBean",
            "Doctor Who Christmas Special One",
            "Doctor Who Christmas Carol Two",
            "Doctor Who Christmas Invasion Three",
            "Some Other Show Pilot Episode",
            "Some Other Show Sequel Episode",
            "Some Other Show Finale Episode",
        ]
        a = find_merge_candidates(members, min_token_overlap=1)
        random.Random(42).shuffle(members)
        b = find_merge_candidates(members, min_token_overlap=1)
        # Compare structurally — ids and member ordering should be identical
        # because we sort deterministically.
        self.assertEqual(len(a), len(b))
        for ga, gb in zip(a, b):
            self.assertEqual(ga.group_id, gb.group_id)
            self.assertEqual(ga.members, gb.members)
            self.assertEqual(ga.suggested_series_name, gb.suggested_series_name)


# ---------------------------------------------------------------------------
# Real-data regressions found running the clusterer on the actual G:\TV library
# ---------------------------------------------------------------------------

class TestRealDataRegressions(unittest.TestCase):
    """Each test mirrors a failure mode observed on the user's real library."""

    def test_bridge_folder_does_not_blob(self):
        # "Mickey Donald Goofy - The Three Musketeers" shares one token with
        # the Donald-Duck cluster and one with the Goofy cluster. Without the
        # containment guard, this single bridge merges both into a giant blob.
        members = [
            "Donald Duck - Bee At The Beach",
            "Donald Duck - Bee On Guard",
            "Donald Duck - Donald's Garden",
            "Goofy - How To Sleep",
            "Goofy - How To Dance",
            "Goofy - The Big Wash",
            "Mickey Donald Goofy - The Three Musketeers",
        ]
        groups = find_merge_candidates(members)
        # No group should contain BOTH a Donald-Duck folder and a Goofy folder.
        for g in groups:
            has_donald = any("Donald Duck -" in m for m in g.members)
            has_goofy = any(m.startswith("Goofy -") for m in g.members)
            self.assertFalse(has_donald and has_goofy,
                             f"Group {g.group_id} contains both Donald and Goofy: {g.members}")

    def test_short_token_false_positive_excluded_mr(self):
        members = [
            "Mr Magoo",
            "Mr. Ed",
            "Mr. Robot (2015)",
        ]
        groups = find_merge_candidates(members)
        self.assertEqual(groups, [])

    def test_short_token_false_positive_excluded_real(self):
        members = [
            "Aaahh! Real monsters",
            "The Real Ghostbusters",
            "The Real Ghostbusters (1986)",
        ]
        # Default min_group_size=3 drops the 2-folder Real Ghostbusters
        # identity cluster. Aaahh never links (only 'real' is shared, 4
        # chars, below min_shared_chars=6).
        groups = find_merge_candidates(members)
        self.assertEqual(groups, [])
        # With min_group_size=2, the Real Ghostbusters dupes form a cluster
        # and Aaahh stays out — confirming the false-positive guard.
        groups = find_merge_candidates(members, min_group_size=2)
        self.assertEqual(len(groups), 1)
        self.assertNotIn("Aaahh! Real monsters", groups[0].members)
        self.assertEqual(groups[0].member_folder_count, 2)

    def test_short_token_false_positive_excluded_arrow(self):
        members = [
            "Arrow",
            "Arrow (2012)",
            "Arrow (2012-)",
            "Beauty and the Beast (2012)",
            "Beauty and the Beast (2012-)",
        ]
        # 3 Arrow dupes form an identity cluster (all normalize to {arrow}).
        # 2 Beauty dupes also form one but are dropped at default min_group_size=3.
        # Critical: the two MUST NOT have merged into a single 5-folder cluster
        # via shared "2012" (the bug RE_PAREN_YEAR fix prevented).
        groups = find_merge_candidates(members)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].member_folder_count, 3)
        self.assertTrue(all("Arrow" in m for m in groups[0].members))
        # With min_group_size=2, BOTH clusters emit, separate.
        groups = find_merge_candidates(members, min_group_size=2)
        self.assertEqual(len(groups), 2)
        for g in groups:
            has_arrow = any("Arrow" in m for m in g.members)
            has_beauty = any("Beauty" in m for m in g.members)
            self.assertFalse(has_arrow and has_beauty,
                             "Arrow and Beauty must not cluster")

    def test_year_paren_dash_variants_are_stripped(self):
        from tv_parser import normalize_folder_name
        self.assertEqual(normalize_folder_name("Arrow (2012-)"), "arrow")
        self.assertEqual(normalize_folder_name("Arrow (2012-2015)"), "arrow")
        self.assertEqual(normalize_folder_name("Arrow (2012-15)"), "arrow")
        self.assertEqual(normalize_folder_name("Arrow (2012)"), "arrow")

    def test_max_group_size_drops_runaway_blob(self):
        members = [f"Donald Duck Episode {i:03d}" for i in range(150)]
        groups = find_merge_candidates(members)
        self.assertEqual(groups, [])
        groups = find_merge_candidates(members, max_group_size=200)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].member_folder_count, 150)

    def test_powerpuff_girls_dupes_still_cluster(self):
        members = [
            "Powerpuff Girls, The",
            "The Powerpuff Girls",
            "The Powerpuff Girls (1998)",
        ]
        groups = find_merge_candidates(members)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].member_folder_count, 3)

    def test_twilight_zone_dupes_still_cluster(self):
        members = [
            "Twilight Zone (1960)",
            "The Twilight Zone (1985)",
            "The Twilight Zone (2019)",
        ]
        groups = find_merge_candidates(members)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].member_folder_count, 3)
        self.assertEqual(set(groups[0].shared_tokens), {"twilight", "zone"})

    def test_donald_duck_shorts_cluster_without_eating_others(self):
        members = [
            "Donald Duck - Bee At The Beach",
            "Donald Duck - Bee On Guard",
            "Donald Duck - Donald's Garden",
            "Donald Duck - Donald's Camera",
            "Sherlock",
            "The Simpsons",
        ]
        groups = find_merge_candidates(members)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].member_folder_count, 4)
        self.assertTrue(all("Donald Duck" in m for m in groups[0].members))

    def test_validation_max_group_size_sanity(self):
        with self.assertRaises(ValueError):
            find_merge_candidates(["a b c", "a b c"], max_group_size=1, min_group_size=3)

    def test_validation_containment_threshold_range(self):
        with self.assertRaises(ValueError):
            find_merge_candidates(["a", "b"], containment_threshold=1.5)

    def test_validation_min_shared_chars(self):
        with self.assertRaises(ValueError):
            find_merge_candidates(["a", "b"], min_shared_chars=-1)



# ---------------------------------------------------------------------------
# Round 2 regressions — false positives observed on real ~888-folder library
# ---------------------------------------------------------------------------

class TestRoundTwoRegressions(unittest.TestCase):
    """Each test is one false-positive cluster the round-1 algorithm produced."""

    def test_japanese_word_does_not_subset_cluster(self):
        # "Japanese 1" / "Japanese Lessons" / "101 Easy Japanese Recipes" all
        # share only "japanese" but none has a leading episode-number prefix
        # (the "1" in "Japanese 1" is a content suffix, not a track number).
        # Subset rule must not fire.
        members = [
            "Japanese 1",
            "Japanese Lessons",
            "101 Easy Japanese Recipes",
        ]
        groups = find_merge_candidates(members)
        self.assertEqual(groups, [])

    def test_goofy_howto_vs_how_to_play(self):
        # {how, play} = 7 chars; below the strong-rule chars floor of 8 so
        # these don't bridge.
        members = [
            "Goofy - How To Be A Detective",
            "Goofy - How To Dance",
            "Goofy - How To Play Golf",
            "Goofy - How To Sleep",
            "How to Play Baseball (1942)",
            "How to Play Football (1944)",
        ]
        groups = find_merge_candidates(members)
        # Goofy howto folders share {goofy, how} = 8 chars and 2 tokens, so
        # they cluster among themselves. "How to Play X" folders share {how,
        # play} = 7 chars with each other, below threshold. So we expect
        # exactly one Goofy cluster, no How-To-Play cluster.
        self.assertEqual(len(groups), 1)
        self.assertTrue(all(m.startswith("Goofy") for m in groups[0].members))
        self.assertNotIn("How to Play Baseball (1942)", groups[0].members)

    def test_danger_word_does_not_bridge(self):
        # {danger} = 6 chars, single token. Strong rule needs 2+. Subset
        # fails (no leading-num gate, neither side is just "Danger").
        members = [
            "Danger Mouse",
            "Danger Mouse (2015)",
            "Henry Danger",
            "Henry Danger (2014)",
        ]
        groups = find_merge_candidates(members)
        # Each pair of dupes should cluster, but the two shows must not merge.
        # Default min_group_size=3 drops both 2-folder identity clusters.
        self.assertEqual(groups, [])
        # With min_group_size=2 we get two distinct clusters.
        groups = find_merge_candidates(members, min_group_size=2)
        self.assertEqual(len(groups), 2)
        for g in groups:
            has_henry = any("Henry" in m for m in g.members)
            has_mouse = any("Mouse" in m for m in g.members)
            self.assertFalse(has_henry and has_mouse,
                             "Henry Danger and Danger Mouse must not merge")

    def test_science_word_does_not_bridge(self):
        members = [
            "Misfits Of Science",
            "Misfits of Science (1985)",
            "Sid The Science Kid",
            "Sid the Science Kid (2008)",
            "Wicked Science",
            "Wicked Science (2004)",
        ]
        # Each pair of dupes is identity (2 folders each, dropped at min_size=3).
        groups = find_merge_candidates(members)
        self.assertEqual(groups, [])
        groups = find_merge_candidates(members, min_group_size=2)
        self.assertEqual(len(groups), 3)
        # Verify NO group contains two different shows.
        for g in groups:
            has_misfits = any("Misfits" in m for m in g.members)
            has_sid = any("Sid" in m for m in g.members)
            has_wicked = any("Wicked" in m for m in g.members)
            self.assertEqual(sum([has_misfits, has_sid, has_wicked]), 1,
                             f"Group has multiple shows: {g.members}")

    def test_dexter_apostrophe_does_not_bridge(self):
        # "Dexter's" -> "dexter s" -> tokens after length-1 drop -> {dexter}.
        # Wait — actually "dexter" is preserved AND "s" dropped, leaving just
        # {dexter}. The apostrophe-s no longer creates a phantom token.
        members = [
            "Dexter",
            "Dexter (2006)",
            "Dexter's Lab",
            "Dexter's Laboratory",
            "Dexter's Laboratory (1996)",
        ]
        groups = find_merge_candidates(members)
        # Dexter (2 folders, identity) and Dexter's Laboratory (3 folders,
        # identity) — only Laboratory cluster meets default min_group_size=3.
        # Dexter the show and Dexter's Lab must NOT merge under defaults.
        for g in groups:
            has_show = any(m in {"Dexter", "Dexter (2006)"} for m in g.members)
            has_lab = any("Lab" in m for m in g.members)
            self.assertFalse(has_show and has_lab,
                             "Dexter (the show) and Dexter's Lab must not merge")

    def test_title_n_dvd_artifact_stripped(self):
        # 200_CARTOONS_DISC1_Title2 should normalize to drop the Title2 suffix
        # so it doesn't count as a unique token.
        from tv_parser import normalize_folder_name
        n1 = normalize_folder_name("200_CARTOONS_DISC1_Title2")
        n2 = normalize_folder_name("200_CARTOONS_DISC1_Title3")
        # After title-N strip, both should be the same.
        self.assertEqual(n1, n2)

    def test_collection_volume_stopwords(self):
        # phase6_design.md round-2 fix: "edition", "collection", "volume" no
        # longer leak into clustering tokens.
        from tv_parser import normalize_folder_name
        # Identity check: these should normalize to the same form.
        self.assertEqual(
            normalize_folder_name("Looney Tunes Golden Collection"),
            normalize_folder_name("Looney Tunes Golden Collection Volume 5"),
        )
        # And both should reduce to {looney, tunes, golden}.
        self.assertEqual(
            set(normalize_folder_name("Looney Tunes Golden Collection").split()),
            {"looney", "tunes", "golden"},
        )

    def test_seasons_word_no_bridge(self):
        # "Bluey Seasons 1, 2, and 3 All In One" + "Sheep Big City Seasons 1-2"
        # should NOT cluster on the shared "seasons" + bare digits.
        members = [
            "Bluey Seasons 1, 2, and 3 All In One",
            "Sheep in the Big City - Seasons 1-2",
            "Sheep in the Big City Seasons 1 2",
        ]
        # The two Sheep folders cluster (dupes), but Bluey doesn't link.
        groups = find_merge_candidates(members, min_group_size=2)
        # Exactly one group, containing only the Sheep folders.
        self.assertEqual(len(groups), 1)
        self.assertTrue(all("Sheep" in m for m in groups[0].members))




# ---------------------------------------------------------------------------
# Round 3: transitive-blob filter (no globally-shared token)
# ---------------------------------------------------------------------------

class TestTransitiveBlobFilter(unittest.TestCase):
    """Verify the post-build filter that drops clusters with empty strict
    global intersection AND low leading-num percentage."""

    def test_no_globally_shared_token_dropped(self):
        # Mickey Mouse blob shape: pair-wise edges chain through different
        # 2-token combinations but no single token spans all members. Without
        # leading-num prefixes, the blob filter should drop this cluster.
        members = [
            "Apple Banana Pilot",       # A
            "Banana Cherry Aftermath",  # B  -- shares banana with A
            "Cherry Date Finale",       # C  -- shares cherry with B
            "Apple Date Special",       # D  -- glues A and C via apple+date
        ]
        # Without leading-num prefixes, this transitive blob is dropped.
        groups = find_merge_candidates(members, min_token_overlap=1)
        self.assertEqual(groups, [])

    def test_globally_shared_token_kept(self):
        # All members share {goofy} — strict global intersection non-empty.
        # Even without leading numbers, the cluster survives.
        members = [
            "Goofy - Father's Day Off",
            "Goofy - Father's Lion",
            "Goofy - Father's Weekend",
            "Goofy - Lion Down",
            "Goofy - They're Off",
        ]
        groups = find_merge_candidates(members)
        self.assertEqual(len(groups), 1)

    def test_majority_leading_num_kept(self):
        # Mr. Bean shape: leading-num exemption keeps the cluster even when
        # strict global intersection isn't trivially deducible (here it IS
        # {mrbean}, but the exemption would also save more transitive cases).
        members = [
            "01 MrBean",
            "02 Good Night MrBean",
            "03 Mind the Baby MrBean",
        ]
        groups = find_merge_candidates(members)
        self.assertEqual(len(groups), 1)

    def test_new_adventures_bridge_dropped(self):
        # Real-data case: Mighty Mouse / Lois & Clark / Winnie the Pooh chain
        # through "new adventures" pair-wise but no token spans all.
        members = [
            "Adventures of Superman",
            "Lois and Clark The New Adventures of Superman",
            "Mighty Mouse",
            "Mighty Mouse The New Adventures",
            "The New Adventures of Mighty Mouse and Heckle Jeckle (1979)",
            "The New Adventures of Winnie the Pooh (1988)",
        ]
        groups = find_merge_candidates(members)
        # Mighty Mouse identity dupes ({mighty, mouse}) might still cluster as
        # 2-folder identity group, dropped by default min_group_size=3. The
        # transitive blob spanning all 6 must NOT survive.
        for g in groups:
            has_pooh = any("Pooh" in m for m in g.members)
            has_lois = any("Lois" in m for m in g.members)
            self.assertFalse(has_pooh and has_lois,
                             "Pooh and Lois must not cluster")

    def test_kim_possible_attack_killer_bridge_dropped(self):
        # "Kim Possible - The Secret Files - Attack Of The Killer Bebes..."
        # bridges Kim Possible and Attack of the Killer Tomatoes via shared
        # {attack, killer}. Strict global intersection is empty.
        members = [
            "Attack of the Killer Tomatoes",
            "Attack of the Killer Tomatoes (1990)",
            "Kim Possible",
            "Kim Possible (2002)",
            "Kim Possible - The Secret Files - Attack Of The Killer Bebes, Downhill",
            "Kim Possible - The Villain Files (2004)",
        ]
        groups = find_merge_candidates(members)
        # Pair-only identity dupes (Kim Possible, Attack of Killer Tomatoes
        # each as 2-folder identity) are below default min_group_size=3 and
        # also wouldn't bridge. The 6-member blob must not survive.
        for g in groups:
            has_kim = any("Kim Possible" in m for m in g.members)
            has_tomatoes = any("Tomatoes" in m for m in g.members)
            self.assertFalse(has_kim and has_tomatoes,
                             "Kim Possible and Killer Tomatoes must not cluster")



if __name__ == "__main__":
    unittest.main(verbosity=2)
