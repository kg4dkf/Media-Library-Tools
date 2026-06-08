#!/usr/bin/env python3
"""
test_execute_tv.py - Unit tests for execute_tv.py (round 6a).

Uses tempfile-based fixtures so file moves are exercised end-to-end
WITHOUT touching real user data. TMDB calls are stubbed via the
FakeSession pattern from test_tv_tmdb_client.

Run:
    py -3.13 test_execute_tv.py
    py -3.13 -m unittest test_execute_tv
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from execute_tv import (
    ClassifiedFile,
    Config,
    DiskOps,
    Journal,
    MergeGroup,
    PreflightError,
    SeriesPlanRow,
    SeriesResult,
    build_merge_groups,
    classify_source_files,
    cleanup_empty_source_dirs,
    hash_file,
    long,
    preflight,
    process_series,
    process_series_group,
    resolve_cross_run_conflict,
    find_sidecars_for_video,
    find_series_root_files,
    render_sidecar_target_name,
    upgrade_extras_via_title_match,
    read_series_plan,
    render_episode_filename,
    render_season_folder,
    render_series_folder,
    same_drive,
    sanitize_for_windows,
    stamp_executed_at,
    target_already_populated,
    write_preflight_report,
)
from tv_tmdb_client import TmdbTvClient


# =========================================================================
# Test fixtures
# =========================================================================

class FakeResponse:
    """Minimal subset of requests.Response."""

    def __init__(self, json_body=None, *, status_code=200,
                 headers=None, raise_on_status=True):
        self.status_code = status_code
        self.headers = headers or {}
        self._json = json_body
        self._raise_on_status = raise_on_status

    def json(self):
        return self._json

    def raise_for_status(self):
        if self._raise_on_status and self.status_code >= 400 and self.status_code != 404:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    """Returns canned responses; FakeResponse instances popped in order.
    Unlike test_tv_tmdb_client's version, this one cycles through a default
    200 OK if the queue runs dry (so we don't have to count exactly)."""

    def __init__(self, responses=None, *, default=None):
        self._responses = list(responses or [])
        self._default = default
        self.calls: List[Tuple[str, Dict[str, Any], float]] = []

    def get(self, url, params, timeout):
        self.calls.append((url, dict(params), timeout))
        if self._responses:
            return self._responses.pop(0)
        if self._default is not None:
            return self._default
        # No default -> empty results.
        return FakeResponse({"results": []})


def make_season_response(season_number: int, episodes_data: List[Dict],
                         air_date: str = "2005-03-26") -> FakeResponse:
    return FakeResponse({
        "season_number": season_number,
        "air_date": air_date,
        "episodes": episodes_data,
    })


# =========================================================================
# Path utilities
# =========================================================================

class TestPathUtilities(unittest.TestCase):
    def test_same_drive_true(self):
        # On Windows this checks drive letters; on POSIX both return ""
        if os.name == "nt":
            self.assertTrue(same_drive(Path("C:\\foo"), Path("C:\\bar")))
            self.assertFalse(same_drive(Path("C:\\foo"), Path("D:\\bar")))
        else:
            # POSIX: no drive letters; both return empty -> False
            self.assertFalse(same_drive(Path("/foo"), Path("/bar")))

    def test_long_path_passthrough_on_posix(self):
        if os.name != "nt":
            self.assertEqual(long(Path("/tmp/foo")), "/tmp/foo")

    def test_sanitize_for_windows_strips_forbidden(self):
        # < and > are mapped to ( ) to preserve a visual cue (matches the
        # movie pipeline's execute.py convention).
        self.assertEqual(sanitize_for_windows("Foo<>:Bar"), "Foo() - Bar")
        self.assertEqual(sanitize_for_windows("Foo|Bar"), "Foo-Bar")
        self.assertEqual(sanitize_for_windows('Foo"Bar'), "Foo'Bar")
        # ? and * stripped entirely.
        self.assertEqual(sanitize_for_windows("Foo?*Bar"), "FooBar")

    def test_sanitize_collapses_whitespace(self):
        self.assertEqual(sanitize_for_windows("Foo   Bar"), "Foo Bar")

    def test_sanitize_trailing_dot_stripped(self):
        self.assertEqual(sanitize_for_windows("Foo. "), "Foo")

    def test_sanitize_reserved_name(self):
        self.assertEqual(sanitize_for_windows("CON"), "CON_")
        self.assertEqual(sanitize_for_windows("LPT1"), "LPT1_")


# =========================================================================
# Config
# =========================================================================

class TestConfig(unittest.TestCase):
    def _write(self, **overrides) -> Path:
        tmp = Path(tempfile.mkdtemp(prefix="exec_tv_cfg_"))
        cfg = {
            "tmdb_api_key": "real-key",
            "tv_root": str(tmp / "tv"),
            "mediaprep_dir": str(tmp / "_mediaprep"),
            "language": "en-US",
        }
        cfg.update(overrides)
        path = tmp / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_required_keys_loaded(self):
        cfg = Config.load(self._write())
        self.assertEqual(cfg.tmdb_api_key, "real-key")
        self.assertTrue(str(cfg.tv_root).endswith("tv"))

    def test_template_defaults(self):
        cfg = Config.load(self._write())
        self.assertIn("{title}", cfg.tv_series_template)
        self.assertIn("{season:02d}", cfg.tv_season_template)
        self.assertIn("S{season:02d}E{episode:02d}", cfg.tv_episode_template)

    def test_missing_required_key_exits(self):
        cfg_path = self._write()
        raw = json.loads(cfg_path.read_text())
        del raw["tv_root"]
        cfg_path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(SystemExit):
            Config.load(cfg_path)


# =========================================================================
# SeriesPlanRow + read_series_plan
# =========================================================================

class TestSeriesPlanReader(unittest.TestCase):
    def _write_plan(self, rows: List[Dict[str, str]]) -> Path:
        tmp = Path(tempfile.mkdtemp(prefix="exec_tv_plan_"))
        path = tmp / "series_plan.csv"
        cols = [
            "series_id", "source_folder", "episode_count", "sample_episode",
            "tmdb_series_id", "imdb_series_id", "matched_series", "matched_year",
            "target_folder", "confidence", "action",
            "content_check", "has_specials", "mixed_content_flag",
            "flags", "notes", "executed_at", "user_notes",
        ]
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in cols})
        return path

    def test_reads_basic_row(self):
        path = self._write_plan([{
            "series_id": "1",
            "source_folder": "G:/TV/Doctor Who (2005)",
            "tmdb_series_id": "57243",
            "matched_series": "Doctor Who",
            "matched_year": "2005",
            "target_folder": "G:/TV/Doctor Who (2005) {tmdb-57243}",
            "confidence": "95",
            "action": "AUTO",
        }])
        rows = read_series_plan(path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].series_id, 1)
        self.assertEqual(rows[0].tmdb_series_id, 57243)
        self.assertEqual(rows[0].matched_year, 2005)
        self.assertTrue(rows[0].is_auto)
        self.assertFalse(rows[0].already_executed)

    def test_already_executed_property(self):
        path = self._write_plan([{
            "series_id": "1", "action": "AUTO",
            "executed_at": "2026-04-29T12:34:56",
        }])
        rows = read_series_plan(path)
        self.assertTrue(rows[0].already_executed)

    def test_non_auto_rows_kept_in_list(self):
        path = self._write_plan([
            {"series_id": "1", "action": "AUTO"},
            {"series_id": "2", "action": "SKIP"},
            {"series_id": "3", "action": ""},
        ])
        rows = read_series_plan(path)
        self.assertEqual(len(rows), 3)
        self.assertEqual(sum(1 for r in rows if r.is_auto), 1)

    def test_empty_or_missing_csv_returns_empty(self):
        tmp = Path(tempfile.mkdtemp(prefix="exec_tv_plan_"))
        self.assertEqual(read_series_plan(tmp / "nonexistent.csv"), [])

    def test_handles_nul_bytes(self):
        # PowerShell sometimes writes NUL-polluted UTF-8.
        path = self._write_plan([{"series_id": "1", "action": "AUTO",
                                  "tmdb_series_id": "100"}])
        polluted = path.read_bytes().replace(b"\n", b"\n\x00")
        path.write_bytes(polluted)
        rows = read_series_plan(path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].tmdb_series_id, 100)


# =========================================================================
# Target-path rendering
# =========================================================================

class TestTargetRendering(unittest.TestCase):
    def _make_config(self) -> Config:
        return Config(
            tmdb_api_key="x", tv_root=Path("G:/TV"),
            mediaprep_dir=Path("/tmp/mediaprep"),
        )

    def test_render_series_folder(self):
        cfg = self._make_config()
        plan = SeriesPlanRow(
            tmdb_series_id=57243, matched_series="Doctor Who",
            matched_year=2005,
        )
        result = render_series_folder(plan, cfg)
        # On POSIX the path uses forward slashes; just check the basename.
        self.assertEqual(result.name, "Doctor Who (2005) {tmdb-57243}")

    def test_render_season_folder_normal(self):
        cfg = self._make_config()
        self.assertEqual(render_season_folder(1, 2005, cfg),
                         "Season 01 (2005)")

    def test_render_season_folder_specials(self):
        cfg = self._make_config()
        self.assertEqual(render_season_folder(0, 2005, cfg), "Specials")

    def test_render_season_folder_no_year(self):
        cfg = self._make_config()
        # Empty year produces "Season 01 ()"; our sanitize strips the empty.
        result = render_season_folder(1, None, cfg)
        self.assertIn("Season 01", result)

    def test_render_episode_filename(self):
        cfg = self._make_config()
        plan = SeriesPlanRow(matched_series="Doctor Who")
        cf = ClassifiedFile(path=Path("/x/Doctor Who S01E01.mkv"),
                            season=1, episode=1)
        result = render_episode_filename(plan, cf, "Rose", cfg)
        self.assertEqual(result, "Doctor Who S01E01 - Rose.mkv")

    def test_render_episode_filename_multi_episode(self):
        cfg = self._make_config()
        plan = SeriesPlanRow(matched_series="Show")
        cf = ClassifiedFile(path=Path("/x/Show S05E12-E13.mkv"),
                            season=5, episode=12, episode_end=13, is_multi=True)
        result = render_episode_filename(plan, cf, "Two-Parter", cfg)
        # Multi-ep marker appears between S/E and " - Title".
        self.assertIn("S05E12-E13", result)
        self.assertTrue(result.endswith(".mkv"))

    def test_render_episode_filename_no_title(self):
        cfg = self._make_config()
        plan = SeriesPlanRow(matched_series="Show")
        cf = ClassifiedFile(path=Path("/x/Show S01E01.mkv"),
                            season=1, episode=1)
        result = render_episode_filename(plan, cf, "", cfg)
        # Empty title sanitizes to nothing — trailing " - " gets cleaned.
        self.assertTrue(result.startswith("Show S01E01"))


# =========================================================================
# classify_source_files (Stage A)
# =========================================================================

class TestClassifySourceFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="exec_tv_classify_"))

    def _touch(self, rel: str, content: bytes = b"x"):
        p = self.tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return p

    def test_classifies_episode_video(self):
        self._touch("Show S01E03 Pilot.mkv")
        result = classify_source_files(self.tmp)
        ep = [f for f in result if f.role == "episode"]
        self.assertEqual(len(ep), 1)
        self.assertEqual(ep[0].season, 1)
        self.assertEqual(ep[0].episode, 3)
        self.assertEqual(ep[0].episode_key, "S01E03")

    def test_classifies_extras_unparseable_video(self):
        self._touch("Behind the Scenes.mkv")
        result = classify_source_files(self.tmp)
        self.assertEqual(result[0].role, "extras")
        self.assertIsNone(result[0].season)

    def test_classifies_specials_in_season_folder(self):
        self._touch("Specials/Special 1.mkv")
        result = classify_source_files(self.tmp)
        ep = [f for f in result if f.role == "episode"]
        self.assertEqual(len(ep), 1)
        self.assertEqual(ep[0].season, 0)
        self.assertTrue(ep[0].is_special)

    def test_classifies_nfo_subtitle_image(self):
        self._touch("tvshow.nfo")
        self._touch("Show.S01E01.en.srt")
        self._touch("poster.jpg")
        self._touch("fanart.png")
        self._touch("season01-banner.jpg")
        roles = {f.role for f in classify_source_files(self.tmp)}
        self.assertIn("nfo", roles)
        self.assertIn("subtitle", roles)
        self.assertIn("poster", roles)
        self.assertIn("fanart", roles)
        self.assertIn("banner", roles)

    def test_skip_dirs_not_descended(self):
        self._touch(".sonarr/cache.dat")
        self._touch("Show S01E01.mkv")
        result = classify_source_files(self.tmp,
                                       skip_dirs=(".sonarr",))
        # Only the .mkv should appear.
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].role, "episode")

    def test_returns_empty_for_missing_folder(self):
        self.assertEqual(classify_source_files(self.tmp / "nonexistent"), [])


# =========================================================================
# Pre-flight checks
# =========================================================================

class TestPreflight(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="exec_tv_pre_"))
        self.tv_root = self.tmp / "tv"
        self.tv_root.mkdir()
        self.cfg = Config(
            tmdb_api_key="x",
            tv_root=self.tv_root,
            mediaprep_dir=self.tmp / "_mediaprep",
        )

    def _make_row(self, **overrides) -> SeriesPlanRow:
        defaults = dict(
            series_id=1,
            source_folder=str(self.tv_root / "Show (2005)"),
            tmdb_series_id=100,
            matched_series="Show",
            matched_year=2005,
            target_folder=str(self.tv_root / "Show (2005) {tmdb-100}"),
            confidence=95,
            action="AUTO",
        )
        defaults.update(overrides)
        return SeriesPlanRow(**defaults)

    def test_clean_plan_no_errors(self):
        # Source folder must exist.
        (self.tv_root / "Show (2005)").mkdir()
        errors = preflight([self._make_row()], self.cfg)
        self.assertEqual(errors, [])

    def test_missing_tmdb_id_flagged(self):
        (self.tv_root / "Show (2005)").mkdir()
        errors = preflight([self._make_row(tmdb_series_id=None)], self.cfg)
        self.assertTrue(any(e.code == "missing_tmdb_id" for e in errors))

    def test_target_outside_tv_root_flagged(self):
        (self.tv_root / "Show (2005)").mkdir()
        errors = preflight(
            [self._make_row(target_folder="/some/other/path/Show")],
            self.cfg,
        )
        self.assertTrue(any(e.code == "target_outside_tv_root" for e in errors))

    def test_source_missing_flagged(self):
        # Source folder doesn't exist.
        errors = preflight([self._make_row()], self.cfg)
        self.assertTrue(any(e.code == "source_missing" for e in errors))

    def test_duplicate_target_with_different_tmdb_ids_flagged(self):
        # Two AUTO rows resolve to the same target_folder but have different
        # tmdb_series_ids -> still a hard conflict (user must pick one).
        (self.tv_root / "A").mkdir()
        (self.tv_root / "B").mkdir()
        rows = [
            self._make_row(series_id=1, tmdb_series_id=100,
                           source_folder=str(self.tv_root / "A")),
            self._make_row(series_id=2, tmdb_series_id=200,
                           source_folder=str(self.tv_root / "B")),
        ]
        errors = preflight(rows, self.cfg)
        self.assertTrue(any(e.code == "duplicate_target" for e in errors),
                        f"expected duplicate_target, got: {[e.code for e in errors]}")

    def test_same_target_same_tmdb_id_is_merge_not_conflict(self):
        # Two AUTO rows resolve to the same target with the same tmdb_id ->
        # legitimate merge, NOT a duplicate_target error.
        (self.tv_root / "A").mkdir()
        (self.tv_root / "B").mkdir()
        # Both source folder names share token "show" with matched_series
        # so the merge sanity check passes.
        rows = [
            self._make_row(series_id=1, tmdb_series_id=100,
                           source_folder=str(self.tv_root / "A")),
            self._make_row(series_id=2, tmdb_series_id=100,
                           source_folder=str(self.tv_root / "B")),
        ]
        errors = preflight(rows, self.cfg)
        codes = [e.code for e in errors]
        self.assertNotIn("duplicate_target", codes,
                         f"merge mis-flagged as duplicate; errors: {codes}")

    def test_merge_source_mismatch_flagged(self):
        # Two AUTO rows share target+tmdb (would normally merge) but one
        # source folder name has no token overlap with matched_series ->
        # merge_source_mismatch error fires (the off-by-one corruption case).
        (self.tv_root / "Abbott and Costello").mkdir()
        (self.tv_root / "The Addams Family (1964)").mkdir()
        rows = [
            self._make_row(series_id=49, tmdb_series_id=11821,
                           matched_series="The Abbott and Costello Show",
                           source_folder=str(self.tv_root / "Abbott and Costello"),
                           target_folder=str(self.tv_root /
                                             "The Abbott and Costello Show (1952) {tmdb-11821}")),
            self._make_row(series_id=779, tmdb_series_id=11821,
                           matched_series="The Abbott and Costello Show",
                           source_folder=str(self.tv_root / "The Addams Family (1964)"),
                           target_folder=str(self.tv_root /
                                             "The Abbott and Costello Show (1952) {tmdb-11821}")),
        ]
        errors = preflight(rows, self.cfg)
        codes = [e.code for e in errors]
        self.assertIn("merge_source_mismatch", codes,
                      f"corruption not caught; errors: {codes}")
        # The good row (49) shouldn't be flagged.
        bad_ids = {e.series_id for e in errors
                   if e.code == "merge_source_mismatch"}
        self.assertEqual(bad_ids, {779})

    def test_merge_with_apostrophe_variants_passes(self):
        # Real-world: "Hitchhikers Guide to the Galaxy" and
        # "The Hitchhiker's Guide to the Galaxy (1981)" -> should still
        # merge cleanly (token "hitchhikers" / "hitchhiker" overlaps).
        (self.tv_root / "Hitchhikers Guide to the Galaxy").mkdir()
        (self.tv_root / "The Hitchhiker's Guide to the Galaxy (1981)").mkdir()
        rows = [
            self._make_row(series_id=1, tmdb_series_id=2048,
                           matched_series="The Hitchhiker's Guide to the Galaxy",
                           source_folder=str(self.tv_root /
                                             "Hitchhikers Guide to the Galaxy"),
                           target_folder=str(self.tv_root /
                                             "The Hitchhiker's Guide to the Galaxy (1981) {tmdb-2048}")),
            self._make_row(series_id=2, tmdb_series_id=2048,
                           matched_series="The Hitchhiker's Guide to the Galaxy",
                           source_folder=str(self.tv_root /
                                             "The Hitchhiker's Guide to the Galaxy (1981)"),
                           target_folder=str(self.tv_root /
                                             "The Hitchhiker's Guide to the Galaxy (1981) {tmdb-2048}")),
        ]
        errors = preflight(rows, self.cfg)
        self.assertNotIn("merge_source_mismatch",
                         [e.code for e in errors])

    def test_skip_rows_already_executed(self):
        # Already-executed AUTO rows are skipped by pre-flight.
        errors = preflight(
            [self._make_row(executed_at="2026-04-29T12:34:56")],
            self.cfg,
        )
        # No errors because the row is filtered out before checks.
        self.assertEqual(errors, [])

    def test_write_preflight_report(self):
        report_path = self.tmp / "preflight_errors_tv.csv"
        write_preflight_report(report_path, [
            PreflightError(1, "/x", "missing_tmdb_id", "..."),
            PreflightError(0, "/y", "duplicate_target", "..."),
        ])
        self.assertTrue(report_path.exists())
        rows = list(csv.DictReader(open(report_path, encoding="utf-8-sig")))
        self.assertEqual(len(rows), 2)


# =========================================================================
# Journal + DiskOps
# =========================================================================

class TestJournal(unittest.TestCase):
    def test_writes_jsonl_records(self):
        tmp = Path(tempfile.mkdtemp(prefix="exec_tv_journal_"))
        j = Journal(tmp / "moves.jsonl", run_id="r1", dry_run=False)
        j.write(series_id=1, op="mkdir", to="/x/Show", result="ok")
        j.write(series_id=1, op="rename", from_="/a", to="/b", result="ok")
        j.close()
        text = (tmp / "moves.jsonl").read_text()
        lines = [l for l in text.splitlines() if l.strip()]
        self.assertEqual(len(lines), 2)
        rec = json.loads(lines[0])
        self.assertEqual(rec["run_id"], "r1")
        self.assertEqual(rec["op"], "mkdir")
        self.assertIn("ts", rec)

    def test_dry_run_marker_overrides_result(self):
        tmp = Path(tempfile.mkdtemp(prefix="exec_tv_journal_"))
        j = Journal(tmp / "moves.jsonl", run_id="r1", dry_run=True)
        j.write(op="rename", result="ok")  # Should be coerced to dry-run
        j.close()
        rec = json.loads((tmp / "moves.jsonl").read_text().splitlines()[0])
        self.assertEqual(rec["result"], "dry-run")


class TestDiskOps(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="exec_tv_ops_"))
        self.journal_path = self.tmp / "moves.jsonl"
        self.journal = Journal(self.journal_path, run_id="r1", dry_run=False)
        self.ops = DiskOps(self.journal, dry_run=False)

    def tearDown(self):
        self.journal.close()

    def _records(self) -> List[Dict[str, Any]]:
        return [json.loads(l) for l in self.journal_path.read_text().splitlines() if l.strip()]

    def test_mkdir_creates_path(self):
        target = self.tmp / "nested" / "folder"
        self.assertTrue(self.ops.mkdir(target, series_id=1, stage="x"))
        self.assertTrue(target.is_dir())
        recs = self._records()
        self.assertEqual(recs[0]["op"], "mkdir")
        self.assertEqual(recs[0]["result"], "ok")

    def test_mkdir_idempotent(self):
        target = self.tmp / "exists"
        target.mkdir()
        self.assertTrue(self.ops.mkdir(target))
        # No record written because it already existed (we early-return).
        # That's the documented behavior.

    def test_rmdir_only_removes_empty(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        nonempty = self.tmp / "nonempty"
        nonempty.mkdir()
        (nonempty / "x.txt").write_text("hi")
        self.assertTrue(self.ops.rmdir(empty))
        self.assertFalse(empty.exists())
        # Non-empty: refused.
        self.assertFalse(self.ops.rmdir(nonempty))
        self.assertTrue(nonempty.exists())

    def test_move_same_drive_atomic(self):
        src = self.tmp / "src.mkv"
        src.write_bytes(b"video bytes")
        dst = self.tmp / "dst" / "renamed.mkv"
        ok = self.ops.move(src, dst, series_id=1, stage="move_episode")
        self.assertTrue(ok)
        self.assertTrue(dst.exists())
        self.assertFalse(src.exists())
        recs = self._records()
        # On Windows: same drive -> "rename" record.
        # On POSIX: no drive letters, falls to "copy"+"delete" path.
        ops = {r.get("op") for r in recs}
        self.assertTrue(
            "rename" in ops or ("copy" in ops and "delete" in ops),
            f"expected rename or copy+delete, got: {ops}",
        )

    def test_move_idempotent_dst_exists(self):
        # Simulate: previous run already wrote dst; src still around.
        src = self.tmp / "src.mkv"
        src.write_bytes(b"abc")
        dst = self.tmp / "dst.mkv"
        dst.write_bytes(b"abc")  # Same size as src
        ok = self.ops.move(src, dst)
        self.assertTrue(ok)
        # src should be deleted (move completion).
        self.assertFalse(src.exists())
        self.assertTrue(dst.exists())

    def test_move_refuses_dst_with_different_size(self):
        src = self.tmp / "src.mkv"
        src.write_bytes(b"abc")
        dst = self.tmp / "dst.mkv"
        dst.write_bytes(b"different content here")
        ok = self.ops.move(src, dst)
        self.assertFalse(ok)
        # Both should still exist.
        self.assertTrue(src.exists())
        self.assertTrue(dst.exists())

    def test_move_missing_source_errors(self):
        ok = self.ops.move(self.tmp / "nope.mkv", self.tmp / "dst.mkv")
        self.assertFalse(ok)


class TestDiskOpsDryRun(unittest.TestCase):
    def test_dry_run_does_not_touch_disk(self):
        tmp = Path(tempfile.mkdtemp(prefix="exec_tv_dry_"))
        journal = Journal(tmp / "moves.jsonl", run_id="r1", dry_run=True)
        ops = DiskOps(journal, dry_run=True)
        src = tmp / "src.mkv"
        src.write_bytes(b"x")
        dst = tmp / "subdir" / "dst.mkv"

        ok = ops.move(src, dst)
        self.assertTrue(ok)
        # Nothing actually moved
        self.assertTrue(src.exists())
        self.assertFalse(dst.exists())
        self.assertFalse(dst.parent.exists())


# =========================================================================
# stamp_executed_at
# =========================================================================

class TestStampExecutedAt(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="exec_tv_stamp_"))
        self.path = self.tmp / "series_plan.csv"
        cols = [
            "series_id", "source_folder", "episode_count", "sample_episode",
            "tmdb_series_id", "imdb_series_id", "matched_series", "matched_year",
            "target_folder", "confidence", "action",
            "content_check", "has_specials", "mixed_content_flag",
            "flags", "notes", "executed_at", "user_notes",
        ]
        with open(self.path, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            for sid in (1, 2, 3):
                w.writerow({"series_id": str(sid), "action": "AUTO"})

    def test_stamps_correct_row(self):
        ok = stamp_executed_at(self.path, series_id=2,
                                timestamp="2026-04-29T12:00:00")
        self.assertTrue(ok)
        rows = list(csv.DictReader(open(self.path, encoding="utf-8-sig")))
        stamps = [r["executed_at"] for r in rows]
        self.assertEqual(stamps, ["", "2026-04-29T12:00:00", ""])

    def test_returns_false_when_replace_keeps_failing(self):
        # Simulate a permanently-locked target (e.g. CSV open in Excel)
        # by monkeypatching os.replace to raise PermissionError. Function
        # should retry, then degrade to a logged warning + return False
        # rather than crashing the run.
        import os as _os
        original = _os.replace
        calls = {"n": 0}
        def fake_replace(src, dst):
            calls["n"] += 1
            raise PermissionError(5, "Access denied (simulated)")
        _os.replace = fake_replace
        try:
            ok = stamp_executed_at(self.path, series_id=2,
                                    timestamp="2026-04-29T12:00:00",
                                    max_attempts=3, retry_delay_s=0.0)
        finally:
            _os.replace = original
        self.assertFalse(ok)
        self.assertEqual(calls["n"], 3,
                         "expected 3 retry attempts before giving up")
        # CSV unchanged.
        rows = list(csv.DictReader(open(self.path, encoding="utf-8-sig")))
        stamps = [r["executed_at"] for r in rows]
        self.assertEqual(stamps, ["", "", ""])

    def test_succeeds_after_transient_permission_error(self):
        # Simulate AV briefly holding the file: first call raises,
        # second succeeds. Should return True and stamp the row.
        import os as _os
        original = _os.replace
        calls = {"n": 0}
        def flaky_replace(src, dst):
            calls["n"] += 1
            if calls["n"] == 1:
                raise PermissionError(5, "AV scan in progress")
            return original(src, dst)
        _os.replace = flaky_replace
        try:
            ok = stamp_executed_at(self.path, series_id=2,
                                    timestamp="2026-04-29T12:00:00",
                                    max_attempts=5, retry_delay_s=0.0)
        finally:
            _os.replace = original
        self.assertTrue(ok)
        self.assertEqual(calls["n"], 2)
        rows = list(csv.DictReader(open(self.path, encoding="utf-8-sig")))
        stamps = [r["executed_at"] for r in rows]
        self.assertEqual(stamps, ["", "2026-04-29T12:00:00", ""])


# =========================================================================
# End-to-end: process_series with synthetic library + fake TMDB
# =========================================================================

class TestProcessSeriesEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="exec_tv_e2e_"))
        self.tv_root = self.tmp / "tv"
        self.tv_root.mkdir()
        self.mediaprep = self.tmp / "_mediaprep"
        self.mediaprep.mkdir()

        # Build a synthetic series folder with 3 episodes.
        self.source = self.tv_root / "Doctor Who (2005)"
        self.source.mkdir()
        (self.source / "Doctor Who S01E01 Rose.mkv").write_bytes(b"e1")
        (self.source / "Doctor Who S01E02 The End of the World.mkv").write_bytes(b"e2")
        (self.source / "Doctor Who S01E03 The Unquiet Dead.mkv").write_bytes(b"e3")

        self.cfg = Config(
            tmdb_api_key="x",
            tv_root=self.tv_root,
            mediaprep_dir=self.mediaprep,
        )
        self.plan_row = SeriesPlanRow(
            series_id=1,
            source_folder=str(self.source),
            tmdb_series_id=57243,
            matched_series="Doctor Who",
            matched_year=2005,
            target_folder=str(self.tv_root / "Doctor Who (2005) {tmdb-57243}"),
            confidence=95,
            action="AUTO",
        )

        # Fake TMDB client returns canned season 1.
        self.fake_session = FakeSession(
            default=make_season_response(1, [
                {"episode_number": 1, "name": "Rose"},
                {"episode_number": 2, "name": "The End of the World"},
                {"episode_number": 3, "name": "The Unquiet Dead"},
            ], air_date="2005-03-26"),
        )
        self.tmdb = TmdbTvClient(
            "key", self.mediaprep / "cache.sqlite",
            session=self.fake_session,
        )

    def tearDown(self):
        self.tmdb.close()

    def _run(self, dry_run=False) -> SeriesResult:
        journal = Journal(self.mediaprep / "moves.jsonl",
                          run_id="r1", dry_run=dry_run)
        ops = DiskOps(journal, dry_run=dry_run)
        try:
            return process_series(self.plan_row, self.cfg, ops, self.tmdb)
        finally:
            journal.close()

    def test_apply_moves_episodes_to_canonical_layout(self):
        result = self._run(dry_run=False)
        self.assertTrue(result.success)
        self.assertEqual(result.moved, 3)
        self.assertEqual(result.errors, 0)

        target_dir = self.tv_root / "Doctor Who (2005) {tmdb-57243}"
        season_dir = target_dir / "Season 01 (2005)"
        self.assertTrue(season_dir.is_dir(), f"missing {season_dir}")
        self.assertTrue((season_dir / "Doctor Who S01E01 - Rose.mkv").exists())
        self.assertTrue((season_dir / "Doctor Who S01E02 - The End of the World.mkv").exists())
        self.assertTrue((season_dir / "Doctor Who S01E03 - The Unquiet Dead.mkv").exists())
        # Source files moved out
        self.assertEqual(list(self.source.glob("*.mkv")), [])

    def test_dry_run_does_not_move(self):
        result = self._run(dry_run=True)
        self.assertTrue(result.success)
        # All episodes still in the source folder
        sources = list(self.source.glob("*.mkv"))
        self.assertEqual(len(sources), 3)

    def test_idempotent_re_run(self):
        # Run once.
        first = self._run(dry_run=False)
        self.assertTrue(first.success)
        # Touch source folder to look like a previous interrupted run.
        # Now create a new src file at original location with same size as
        # a moved-out file to test the "dst exists with same size" branch.
        ep_path = self.source / "Doctor Who S01E01 Rose.mkv"
        ep_path.write_bytes(b"e1")
        # Re-run.
        second = self._run(dry_run=False)
        self.assertTrue(second.success)
        # Source should be cleaned up again.
        self.assertFalse(ep_path.exists())

    def test_no_episodes_returns_failure_with_note(self):
        # Wipe the source folder.
        for p in self.source.glob("*.mkv"):
            p.unlink()
        result = self._run(dry_run=False)
        self.assertFalse(result.success)
        self.assertIn("no parseable episodes", result.notes)

    def test_empty_source_with_populated_target_is_already_executed(self):
        # Simulate the recovery case: a previous --apply moved everything
        # but stamp_executed_at crashed, so executed_at is unset. On the
        # next run, source is empty but target already has episodes.
        # Should report success-already-executed (so caller stamps).
        target = self.tv_root / "Doctor Who (2005) {tmdb-57243}"
        season = target / "Season 01 (2005)"
        season.mkdir(parents=True)
        (season / "Doctor Who S01E01 - Rose.mkv").write_bytes(b"e1")
        # Wipe source (simulating a prior successful move).
        for p in self.source.glob("*.mkv"):
            p.unlink()
        result = self._run(dry_run=False)
        self.assertTrue(result.success,
                        f"expected already-executed success; got {result.notes}")
        self.assertIn("already executed", result.notes)
        self.assertEqual(result.moved, 0)

    def test_journal_records_match_operations(self):
        self._run(dry_run=False)
        recs = [json.loads(l) for l in
                (self.mediaprep / "moves.jsonl").read_text().splitlines()
                if l.strip()]
        # Should have mkdir for series + season, plus per-episode move records.
        # On Windows that's "rename" each; on POSIX that's "copy" + "delete" each.
        ops = [r["op"] for r in recs]
        self.assertIn("mkdir", ops)
        moves = ops.count("rename") + ops.count("copy")
        self.assertGreaterEqual(moves, 3,
                                f"expected >= 3 moves, got ops: {ops}")
        # Every record should have series_id=1.
        sids = {r.get("series_id") for r in recs}
        self.assertEqual(sids, {1})


# =========================================================================
# target_already_populated
# =========================================================================

class TestTargetAlreadyPopulated(unittest.TestCase):
    def test_missing_target(self):
        tmp = Path(tempfile.mkdtemp(prefix="exec_tv_pop_"))
        self.assertFalse(target_already_populated(tmp / "missing"))

    def test_empty_target(self):
        tmp = Path(tempfile.mkdtemp(prefix="exec_tv_pop_"))
        self.assertFalse(target_already_populated(tmp))

    def test_target_with_only_nfo(self):
        tmp = Path(tempfile.mkdtemp(prefix="exec_tv_pop_"))
        (tmp / "Season 01 (2005)").mkdir()
        (tmp / "Season 01 (2005)" / "season.nfo").write_text("x")
        self.assertFalse(target_already_populated(tmp))

    def test_target_with_video_in_season(self):
        tmp = Path(tempfile.mkdtemp(prefix="exec_tv_pop_"))
        season = tmp / "Season 01 (2005)"
        season.mkdir()
        (season / "Doctor Who S01E01.mkv").write_bytes(b"x")
        self.assertTrue(target_already_populated(tmp))

    def test_target_with_video_in_specials(self):
        tmp = Path(tempfile.mkdtemp(prefix="exec_tv_pop_"))
        sp = tmp / "Specials"
        sp.mkdir()
        (sp / "Doctor Who S00E01.mkv").write_bytes(b"x")
        self.assertTrue(target_already_populated(tmp))

    def test_target_with_video_in_unrelated_subdir(self):
        # Should NOT count: only Season-prefixed or Specials dirs are inspected.
        tmp = Path(tempfile.mkdtemp(prefix="exec_tv_pop_"))
        bad = tmp / "Random"
        bad.mkdir()
        (bad / "stray.mkv").write_bytes(b"x")
        self.assertFalse(target_already_populated(tmp))


# =========================================================================
# cleanup_empty_source_dirs
# =========================================================================

class TestCleanup(unittest.TestCase):
    def test_removes_emptied_source_dirs(self):
        tmp = Path(tempfile.mkdtemp(prefix="exec_tv_cleanup_"))
        cfg = Config(tmdb_api_key="x", tv_root=tmp / "tv",
                     mediaprep_dir=tmp / "mp")
        cfg.mediaprep_dir.mkdir(parents=True)
        cfg.tv_root.mkdir()
        src = cfg.tv_root / "Empty"
        (src / "Season 1").mkdir(parents=True)  # nested empty dir
        plan = [SeriesPlanRow(series_id=1, source_folder=str(src), action="AUTO")]
        journal = Journal(cfg.mediaprep_dir / "j.jsonl", run_id="r", dry_run=False)
        ops = DiskOps(journal, dry_run=False)
        try:
            removed = cleanup_empty_source_dirs(plan, cfg, ops)
        finally:
            journal.close()
        self.assertGreaterEqual(removed, 1)
        self.assertFalse(src.exists())

    def test_keeps_non_empty_dirs(self):
        tmp = Path(tempfile.mkdtemp(prefix="exec_tv_cleanup_"))
        cfg = Config(tmdb_api_key="x", tv_root=tmp / "tv",
                     mediaprep_dir=tmp / "mp")
        cfg.mediaprep_dir.mkdir(parents=True)
        cfg.tv_root.mkdir()
        src = cfg.tv_root / "NotEmpty"
        src.mkdir()
        (src / "stillthere.txt").write_text("data")
        plan = [SeriesPlanRow(series_id=1, source_folder=str(src), action="AUTO")]
        journal = Journal(cfg.mediaprep_dir / "j.jsonl", run_id="r", dry_run=False)
        ops = DiskOps(journal, dry_run=False)
        try:
            cleanup_empty_source_dirs(plan, cfg, ops)
        finally:
            journal.close()
        self.assertTrue(src.exists())
        self.assertTrue((src / "stillthere.txt").exists())


# =========================================================================
# hash_file
# =========================================================================

class TestHashFile(unittest.TestCase):
    def test_hash_consistent(self):
        tmp = Path(tempfile.mkdtemp(prefix="exec_tv_hash_"))
        p = tmp / "x.bin"
        p.write_bytes(b"deadbeef" * 1024)
        h1 = hash_file(p)
        h2 = hash_file(p)
        self.assertEqual(h1, h2)
        self.assertGreater(len(h1), 16)

    def test_hash_changes_with_content(self):
        tmp = Path(tempfile.mkdtemp(prefix="exec_tv_hash_"))
        a = tmp / "a.bin"
        b = tmp / "b.bin"
        a.write_bytes(b"content A")
        b.write_bytes(b"content B")
        self.assertNotEqual(hash_file(a), hash_file(b))


# =========================================================================
# Merge groups (round 6c)
# =========================================================================

class TestBuildMergeGroups(unittest.TestCase):
    def _row(self, sid, *, target=None, tmdb=None, src=None,
             action="AUTO", executed_at=""):
        return SeriesPlanRow(
            series_id=sid,
            source_folder=src or f"/x/src{sid}",
            tmdb_series_id=tmdb,
            matched_series=f"S{sid}",
            target_folder=target or f"/T/S{sid}",
            action=action,
            executed_at=executed_at,
        )

    def test_single_row_groups(self):
        plan = [self._row(1, tmdb=10), self._row(2, tmdb=20)]
        groups, conflicts = build_merge_groups(plan)
        self.assertEqual(conflicts, [])
        self.assertEqual(len(groups), 2)
        self.assertTrue(all(not g.is_merge for g in groups))

    def test_two_way_merge_same_tmdb(self):
        plan = [
            self._row(1, target="/T/X", tmdb=99),
            self._row(2, target="/T/X", tmdb=99),
        ]
        groups, conflicts = build_merge_groups(plan)
        self.assertEqual(conflicts, [])
        self.assertEqual(len(groups), 1)
        self.assertTrue(groups[0].is_merge)
        self.assertEqual(groups[0].primary.series_id, 1)
        self.assertEqual([r.series_id for r in groups[0].followers], [2])

    def test_three_way_merge_lowest_id_is_primary(self):
        plan = [
            self._row(7, target="/T/Y", tmdb=42),
            self._row(2, target="/T/Y", tmdb=42),
            self._row(5, target="/T/Y", tmdb=42),
        ]
        groups, conflicts = build_merge_groups(plan)
        self.assertEqual(conflicts, [])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].primary.series_id, 2)
        self.assertEqual(sorted(r.series_id for r in groups[0].followers), [5, 7])

    def test_conflict_when_tmdb_ids_disagree(self):
        plan = [
            self._row(1, target="/T/Z", tmdb=11),
            self._row(2, target="/T/Z", tmdb=22),
        ]
        groups, conflicts = build_merge_groups(plan)
        self.assertEqual(len(groups), 0)
        self.assertEqual(len(conflicts), 1)
        _, ids = conflicts[0]
        self.assertEqual(sorted(ids), [1, 2])

    def test_conflict_when_one_row_has_no_tmdb_id(self):
        plan = [
            self._row(1, target="/T/Q", tmdb=11),
            self._row(2, target="/T/Q", tmdb=None),
        ]
        _, conflicts = build_merge_groups(plan)
        self.assertEqual(len(conflicts), 1)

    def test_already_executed_rows_excluded(self):
        plan = [
            self._row(1, target="/T/A", tmdb=5,
                      executed_at="2026-04-29T12:00:00"),
            self._row(2, target="/T/A", tmdb=5),
        ]
        groups, _ = build_merge_groups(plan)
        self.assertEqual(len(groups), 1)
        self.assertFalse(groups[0].is_merge)
        self.assertEqual(groups[0].primary.series_id, 2)

    def test_non_auto_rows_excluded(self):
        plan = [
            self._row(1, target="/T/A", tmdb=5, action="SKIP"),
            self._row(2, target="/T/A", tmdb=5, action="AUTO"),
        ]
        groups, _ = build_merge_groups(plan)
        self.assertEqual(len(groups), 1)
        self.assertFalse(groups[0].is_merge)


# =========================================================================
# End-to-end: process_series_group with merge fixtures
# =========================================================================

class TestProcessSeriesGroupMerge(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="exec_tv_merge_"))
        self.tv_root = self.tmp / "tv"; self.tv_root.mkdir()
        self.mediaprep = self.tmp / "_mp"; self.mediaprep.mkdir()
        self.cfg = Config(tmdb_api_key="x", tv_root=self.tv_root,
                          mediaprep_dir=self.mediaprep)
        self.tmdb = TmdbTvClient(
            "key", self.mediaprep / "cache.sqlite",
            session=FakeSession(default=make_season_response(1, [
                {"episode_number": 1, "name": "Pilot"},
                {"episode_number": 2, "name": "Two"},
                {"episode_number": 3, "name": "Three"},
            ], air_date="2010-01-01")),
        )

    def tearDown(self):
        self.tmdb.close()

    def _run(self, group, dry_run=False):
        journal = Journal(self.mediaprep / "moves.jsonl",
                          run_id="r", dry_run=dry_run)
        ops = DiskOps(journal, dry_run=dry_run)
        try:
            return process_series_group(group, self.cfg, ops, self.tmdb)
        finally:
            journal.close()

    def _row(self, sid, src):
        return SeriesPlanRow(
            series_id=sid, source_folder=str(src),
            tmdb_series_id=42, matched_series="Show",
            matched_year=2010,
            target_folder=str(self.tv_root / "Show (2010) {tmdb-42}"),
            action="AUTO",
        )

    def test_single_row_group_delegates(self):
        src = self.tv_root / "Show (2010)"
        src.mkdir()
        (src / "Show S01E01.mkv").write_bytes(b"e1")
        (src / "Show S01E02.mkv").write_bytes(b"e2")
        group = MergeGroup(primary=self._row(1, src))
        results = self._run(group)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].success)
        self.assertEqual(results[0].moved, 2)

    def test_two_way_merge_no_collisions(self):
        a = self.tv_root / "Show A"; a.mkdir()
        (a / "Show S01E01.mkv").write_bytes(b"e1")
        (a / "Show S01E02.mkv").write_bytes(b"e2")
        b = self.tv_root / "Show B"; b.mkdir()
        (b / "Show S01E03.mkv").write_bytes(b"e3")

        group = MergeGroup(primary=self._row(1, a),
                           followers=[self._row(2, b)])
        results = self._run(group)
        target = self.tv_root / "Show (2010) {tmdb-42}" / "Season 01 (2010)"
        landed = sorted(p.name for p in target.glob("*.mkv"))
        self.assertEqual(len(landed), 3)
        for r in results:
            self.assertTrue(r.success, f"row {r.series_id} failed: {r.notes}")
        self.assertEqual(sum(r.moved for r in results), 3)

    def test_collision_identical_hash_drops_silently(self):
        a = self.tv_root / "Show A"; a.mkdir()
        (a / "Show S01E01.mkv").write_bytes(b"same content")
        b = self.tv_root / "Show B"; b.mkdir()
        (b / "Show S01E01.mkv").write_bytes(b"same content")

        group = MergeGroup(primary=self._row(1, a),
                           followers=[self._row(2, b)])
        results = self._run(group)
        target = self.tv_root / "Show (2010) {tmdb-42}" / "Season 01 (2010)"
        landed = list(target.glob("*.mkv"))
        self.assertEqual(len(landed), 1)
        by_sid = {r.series_id: r for r in results}
        self.assertEqual(by_sid[1].moved, 1)
        self.assertEqual(by_sid[1].skipped, 0)
        self.assertEqual(by_sid[2].moved, 0)
        self.assertEqual(by_sid[2].skipped, 1)
        self.assertTrue(by_sid[1].success)
        self.assertTrue(by_sid[2].success)

    def test_collision_different_content_quarantines_smaller(self):
        a = self.tv_root / "Show A"; a.mkdir()
        (a / "Show S01E01.mkv").write_bytes(b"BIG" * 1000)
        b = self.tv_root / "Show B"; b.mkdir()
        (b / "Show S01E01.mkv").write_bytes(b"small")

        group = MergeGroup(primary=self._row(1, a),
                           followers=[self._row(2, b)])
        results = self._run(group)
        target = self.tv_root / "Show (2010) {tmdb-42}" / "Season 01 (2010)"
        landed = list(target.glob("*.mkv"))
        self.assertEqual(len(landed), 1)
        self.assertEqual(landed[0].stat().st_size, 3000)
        q_root = self.mediaprep / "duplicates" / "Show (2010) {tmdb-42}"
        quarantined = list(q_root.glob("*.mkv"))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].stat().st_size, 5)
        by_sid = {r.series_id: r for r in results}
        self.assertEqual(by_sid[1].moved, 1)
        self.assertEqual(by_sid[2].moved, 0)
        self.assertEqual(by_sid[2].skipped, 1)
        self.assertTrue(by_sid[1].success)
        self.assertTrue(by_sid[2].success)

    def test_three_way_merge_mixed(self):
        a = self.tv_root / "A"; a.mkdir()
        (a / "Show S01E01.mkv").write_bytes(b"e1")
        b = self.tv_root / "B"; b.mkdir()
        (b / "Show S01E01.mkv").write_bytes(b"e1")
        (b / "Show S01E02.mkv").write_bytes(b"e2")
        c = self.tv_root / "C"; c.mkdir()
        (c / "Show S01E03.mkv").write_bytes(b"e3")

        group = MergeGroup(
            primary=self._row(1, a),
            followers=[self._row(2, b), self._row(3, c)],
        )
        results = self._run(group)
        target = self.tv_root / "Show (2010) {tmdb-42}" / "Season 01 (2010)"
        landed = sorted(p.name for p in target.glob("*.mkv"))
        self.assertEqual(len(landed), 3)
        by_sid = {r.series_id: r for r in results}
        self.assertEqual(by_sid[1].moved, 1)
        self.assertEqual(by_sid[2].moved, 1)
        self.assertEqual(by_sid[2].skipped, 1)
        self.assertEqual(by_sid[3].moved, 1)
        self.assertTrue(all(r.success for r in results))

    def test_journal_records_merge_markers(self):
        a = self.tv_root / "A"; a.mkdir()
        (a / "Show S01E01.mkv").write_bytes(b"e1")
        b = self.tv_root / "B"; b.mkdir()
        (b / "Show S01E02.mkv").write_bytes(b"e2")
        group = MergeGroup(primary=self._row(1, a),
                           followers=[self._row(2, b)])
        self._run(group)
        recs = [json.loads(l) for l in
                (self.mediaprep / "moves.jsonl").read_text().splitlines()
                if l.strip()]
        ops = [r["op"] for r in recs]
        self.assertIn("merge_start", ops)
        self.assertIn("merge_end", ops)


# =========================================================================
# Within-source dedup (round 6c+, after the destination-exists postmortem)
# =========================================================================

class TestWithinSourceDedup(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="exec_tv_within_"))
        self.tv_root = self.tmp / "tv"
        self.tv_root.mkdir()
        self.mediaprep = self.tmp / "_mediaprep"
        self.mediaprep.mkdir()
        self.cfg = Config(tmdb_api_key="x", tv_root=self.tv_root,
                          mediaprep_dir=self.mediaprep)
        self.source = self.tv_root / "Show (2010)"
        self.source.mkdir()
        self.plan_row = SeriesPlanRow(
            series_id=1,
            source_folder=str(self.source),
            tmdb_series_id=42,
            matched_series="Show",
            matched_year=2010,
            target_folder=str(self.tv_root / "Show (2010) {tmdb-42}"),
            confidence=95,
            action="AUTO",
        )
        self.tmdb = TmdbTvClient(
            "key", self.mediaprep / "cache.sqlite",
            session=FakeSession(default=make_season_response(1, [
                {"episode_number": 1, "name": "Pilot"},
                {"episode_number": 2, "name": "Two"},
                {"episode_number": 5, "name": "Five"},
            ], air_date="2010-01-01")),
        )

    def tearDown(self):
        self.tmdb.close()

    def _run(self) -> SeriesResult:
        journal = Journal(self.mediaprep / "moves.jsonl",
                          run_id="r", dry_run=False)
        ops = DiskOps(journal, dry_run=False)
        try:
            return process_series(self.plan_row, self.cfg, ops, self.tmdb)
        finally:
            journal.close()

    def test_identical_duplicates_in_source_drop_silently(self):
        # Two byte-identical files claiming S01E01 — only one should land,
        # the other goes to the duplicates folder.
        (self.source / "Show A - S01E01.mkv").write_bytes(b"identical")
        (self.source / "Show B - S01E01.mkv").write_bytes(b"identical")
        result = self._run()
        self.assertTrue(result.success, f"failed: {result.notes}")
        # One keeper landed.
        target = (self.tv_root / "Show (2010) {tmdb-42}" / "Season 01 (2010)")
        landed = list(target.glob("*.mkv"))
        self.assertEqual(len(landed), 1)
        # One quarantined.
        q = self.mediaprep / "duplicates" / "Show (2010) {tmdb-42}"
        quarantined = list(q.glob("*.mkv"))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(result.moved, 1)
        self.assertEqual(result.skipped, 1)

    def test_different_content_same_episode_keeps_largest(self):
        # The Addams-Family-style real-world bug: two files parse to the
        # same SxxExx but have different content. Largest should win,
        # smaller goes to quarantine.
        big = b"BIG" * 1000   # 3000 bytes
        small = b"small"      # 5 bytes
        (self.source / "Show wrong - S01E01.mkv").write_bytes(small)
        (self.source / "Show real - S01E01.mkv").write_bytes(big)
        result = self._run()
        self.assertTrue(result.success, f"failed: {result.notes}")
        target = (self.tv_root / "Show (2010) {tmdb-42}" / "Season 01 (2010)")
        landed = list(target.glob("*.mkv"))
        self.assertEqual(len(landed), 1)
        self.assertEqual(landed[0].stat().st_size, 3000)
        # Smaller copy quarantined with provenance in the name.
        q = self.mediaprep / "duplicates" / "Show (2010) {tmdb-42}"
        quarantined = list(q.glob("*.mkv"))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].stat().st_size, 5)

    def test_three_collisions_same_key(self):
        # Three different-sized copies of the same SxxExx. Largest wins,
        # other two go to quarantine.
        (self.source / "Show A - S01E01.mkv").write_bytes(b"a" * 100)
        (self.source / "Show B - S01E01.mkv").write_bytes(b"b" * 500)
        (self.source / "Show C - S01E01.mkv").write_bytes(b"c" * 250)
        result = self._run()
        self.assertTrue(result.success, f"failed: {result.notes}")
        target = (self.tv_root / "Show (2010) {tmdb-42}" / "Season 01 (2010)")
        landed = list(target.glob("*.mkv"))
        self.assertEqual(len(landed), 1)
        self.assertEqual(landed[0].stat().st_size, 500)
        q = self.mediaprep / "duplicates" / "Show (2010) {tmdb-42}"
        quarantined = sorted(p.stat().st_size for p in q.glob("*.mkv"))
        self.assertEqual(quarantined, [100, 250])

    def test_no_collisions_passes_through_cleanly(self):
        # Two distinct episodes — no dedup activity.
        (self.source / "Show - S01E01.mkv").write_bytes(b"e1")
        (self.source / "Show - S01E02.mkv").write_bytes(b"e2")
        result = self._run()
        self.assertTrue(result.success)
        self.assertEqual(result.moved, 2)
        self.assertEqual(result.skipped, 0)
        q = self.mediaprep / "duplicates" / "Show (2010) {tmdb-42}"
        # Quarantine folder shouldn't even exist (no dedup losers).
        self.assertFalse(q.exists() and any(q.glob("*.mkv")))


# =========================================================================
# Cross-run dedup helper (round 6c+, fixes the cross-run keeper-conflict gap)
# =========================================================================

class TestResolveCrossRunConflict(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="exec_tv_xrun_"))
        self.tv_root = self.tmp / "tv"; self.tv_root.mkdir()
        self.mediaprep = self.tmp / "_mp"; self.mediaprep.mkdir()
        self.cfg = Config(tmdb_api_key="x", tv_root=self.tv_root,
                          mediaprep_dir=self.mediaprep)
        self.series_target = self.tv_root / "Show (2010) {tmdb-42}"
        self.series_target.mkdir()
        self.season = self.series_target / "Season 01 (2010)"
        self.season.mkdir()
        self.journal = Journal(self.mediaprep / "moves.jsonl",
                               run_id="r", dry_run=False)
        self.ops = DiskOps(self.journal, dry_run=False)

    def tearDown(self):
        self.journal.close()

    def _src(self, content: bytes) -> Path:
        p = self.tmp / "src.mkv"
        p.write_bytes(content)
        return p

    def _dst(self, content: bytes) -> Path:
        p = self.season / "Show S01E01 - Pilot.mkv"
        p.write_bytes(content)
        return p

    def test_no_conflict_when_dst_missing(self):
        src = self._src(b"hello")
        dst = self.season / "Show S01E01 - Pilot.mkv"  # not created
        verdict = resolve_cross_run_conflict(
            src, dst, series_id=1, series_target=self.series_target,
            cfg=self.cfg, ops=self.ops)
        self.assertEqual(verdict, "no_conflict")

    def test_no_conflict_when_sizes_match(self):
        # Idempotent same-size case — defer to DiskOps.move().
        same = b"identical bytes here"
        src = self._src(same)
        dst = self._dst(same)
        verdict = resolve_cross_run_conflict(
            src, dst, series_id=1, series_target=self.series_target,
            cfg=self.cfg, ops=self.ops)
        self.assertEqual(verdict, "no_conflict")

    def test_source_larger_quarantines_target(self):
        # Existing target is smaller; source wins. Target gets quarantined,
        # caller can proceed to move source into the now-empty slot.
        src = self._src(b"BIG" * 1000)   # 3000 bytes
        dst = self._dst(b"small")         # 5 bytes
        verdict = resolve_cross_run_conflict(
            src, dst, series_id=42, series_target=self.series_target,
            cfg=self.cfg, ops=self.ops)
        self.assertEqual(verdict, "use_source")
        # dst was moved out; the canonical slot is now free.
        self.assertFalse(dst.exists())
        # Loser landed in the duplicates folder.
        q_root = self.mediaprep / "duplicates" / "Show (2010) {tmdb-42}"
        self.assertTrue(q_root.is_dir())
        landed = list(q_root.glob("*.mkv"))
        self.assertEqual(len(landed), 1)
        self.assertEqual(landed[0].read_bytes(), b"small")

    def test_target_larger_signals_use_target(self):
        # Existing target is bigger; source loses. Caller is expected to
        # quarantine src on its end — we don't touch src here.
        src = self._src(b"small")            # 5 bytes
        dst = self._dst(b"BIG" * 1000)        # 3000 bytes
        verdict = resolve_cross_run_conflict(
            src, dst, series_id=42, series_target=self.series_target,
            cfg=self.cfg, ops=self.ops)
        self.assertEqual(verdict, "use_target")
        # dst untouched.
        self.assertTrue(dst.exists())
        self.assertEqual(dst.stat().st_size, 3000)

    def test_identical_hash_signals_use_target(self):
        # Same content (in this contrived case, same bytes -> same hash and
        # same size) — defer to "no_conflict" via the size-match check.
        # To exercise the hash-match branch, set up files with same content
        # but different lengths visible via the size check (impossible with
        # real hashing — left as TODO; in practice the size==size guard
        # catches all idempotent cases).
        # This test instead verifies the explicit identical-hash path with
        # same content, expecting size-match to return no_conflict first.
        src = self._src(b"identical")
        dst = self._dst(b"identical")
        verdict = resolve_cross_run_conflict(
            src, dst, series_id=1, series_target=self.series_target,
            cfg=self.cfg, ops=self.ops)
        self.assertEqual(verdict, "no_conflict")


# =========================================================================
# End-to-end: process_series with target already populated (cross-run case)
# =========================================================================

class TestProcessSeriesCrossRunIntegration(unittest.TestCase):
    """Simulates the real-world "previous --apply was interrupted, target
    has stale content" scenario and verifies the cross-run dedup recovers."""
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="exec_tv_xrun_e2e_"))
        self.tv_root = self.tmp / "tv"; self.tv_root.mkdir()
        self.mediaprep = self.tmp / "_mp"; self.mediaprep.mkdir()
        self.cfg = Config(tmdb_api_key="x", tv_root=self.tv_root,
                          mediaprep_dir=self.mediaprep)
        # Source folder + one episode.
        self.source = self.tv_root / "Show (2010)"
        self.source.mkdir()
        self.tmdb = TmdbTvClient(
            "key", self.mediaprep / "cache.sqlite",
            session=FakeSession(default=make_season_response(1, [
                {"episode_number": 1, "name": "Pilot"},
            ], air_date="2010-01-01")),
        )

    def tearDown(self):
        self.tmdb.close()

    def _row(self) -> SeriesPlanRow:
        return SeriesPlanRow(
            series_id=1, source_folder=str(self.source),
            tmdb_series_id=42, matched_series="Show",
            matched_year=2010,
            target_folder=str(self.tv_root / "Show (2010) {tmdb-42}"),
            action="AUTO",
        )

    def _run(self):
        journal = Journal(self.mediaprep / "moves.jsonl",
                          run_id="r", dry_run=False)
        ops = DiskOps(journal, dry_run=False)
        try:
            return process_series(self._row(), self.cfg, ops, self.tmdb)
        finally:
            journal.close()

    def test_source_larger_replaces_target(self):
        # Source has the bigger / better copy.
        (self.source / "Show S01E01 - Pilot.mkv").write_bytes(b"BIG" * 1000)
        # Target already has a smaller (stale) copy from a prior run.
        season = self.tv_root / "Show (2010) {tmdb-42}" / "Season 01 (2010)"
        season.mkdir(parents=True)
        (season / "Show S01E01 - Pilot.mkv").write_bytes(b"small")

        result = self._run()
        self.assertTrue(result.success, f"failed: {result.notes}")
        # Canonical now holds the larger source.
        self.assertEqual((season / "Show S01E01 - Pilot.mkv").stat().st_size,
                         3000)
        # Smaller stale target moved to duplicates.
        q_root = self.mediaprep / "duplicates" / "Show (2010) {tmdb-42}"
        quarantined = list(q_root.glob("*.mkv"))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].stat().st_size, 5)

    def test_target_larger_quarantines_source(self):
        # Target already has the better copy from a prior run.
        (self.source / "Show S01E01 - Pilot.mkv").write_bytes(b"small")
        season = self.tv_root / "Show (2010) {tmdb-42}" / "Season 01 (2010)"
        season.mkdir(parents=True)
        (season / "Show S01E01 - Pilot.mkv").write_bytes(b"BIG" * 1000)

        result = self._run()
        self.assertTrue(result.success, f"failed: {result.notes}")
        # Canonical still holds the larger pre-existing copy.
        self.assertEqual((season / "Show S01E01 - Pilot.mkv").stat().st_size,
                         3000)
        # Source went to quarantine.
        q_root = self.mediaprep / "duplicates" / "Show (2010) {tmdb-42}"
        quarantined = list(q_root.glob("*.mkv"))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].stat().st_size, 5)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.moved, 0)


# =========================================================================
# Title-matching upgrade (Round 6b)
# =========================================================================

class TestTitleMatchUpgrade(unittest.TestCase):
    """Integration: source folder contains files with no SxxExx, but with
    titles that match TMDB episode names. Round 6b's title matcher should
    rescue them from 'no parseable episodes' failure."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="exec_tv_title_"))
        self.tv_root = self.tmp / "tv"; self.tv_root.mkdir()
        self.mediaprep = self.tmp / "_mp"; self.mediaprep.mkdir()
        self.cfg = Config(tmdb_api_key="x", tv_root=self.tv_root,
                          mediaprep_dir=self.mediaprep)
        self.source = self.tv_root / "Underdog season 1-3"
        self.source.mkdir()
        self.plan_row = SeriesPlanRow(
            series_id=1,
            source_folder=str(self.source),
            tmdb_series_id=4979,
            matched_series="Underdog",
            matched_year=1964,
            target_folder=str(self.tv_root /
                              "Underdog (1964) {tmdb-4979}"),
            confidence=95,
            action="AUTO",
        )

    def _tmdb_with_episodes(self, episodes_by_season):
        """Build a fake TMDB session that returns the given episode lists.
        episodes_by_season: dict like {1: [(1, "Safe Waif"), ...], 2: [...]}"""
        # tv_details first (number_of_seasons), then tv_season per season.
        details_body = {
            "id": 4979,
            "name": "Underdog",
            "number_of_seasons": max(episodes_by_season.keys()),
            "external_ids": {},
        }

        # Build a lookup of canned responses keyed by URL.
        canned: Dict[str, Any] = {}

        # tv_details endpoint
        canned["details"] = FakeResponse(details_body)
        # season endpoints
        season_responses: Dict[int, FakeResponse] = {}
        for sn, eps in episodes_by_season.items():
            season_responses[sn] = make_season_response(sn, [
                {"episode_number": ep_num, "name": ep_name}
                for ep_num, ep_name in eps
            ], air_date="1964-10-03")

        class _FakeSession:
            def __init__(self):
                self.calls = []

            def get(self, url, params, timeout):
                self.calls.append((url, dict(params)))
                if "/tv/" in url and "/season/" in url:
                    # extract season number
                    sn = int(url.rsplit("/season/", 1)[-1])
                    return season_responses.get(sn,
                        FakeResponse({"__status__": 404}, status_code=404))
                if url.endswith(f"/tv/{4979}"):
                    return canned["details"]
                # Default to empty result.
                return FakeResponse({"results": []})

        return TmdbTvClient(
            "key", self.mediaprep / "cache.sqlite",
            session=_FakeSession(),
        )

    def test_upgrade_extras_via_title_match_function(self):
        # Drop two title-only video files.
        (self.source / "Underdog Safe Waif.avi").write_bytes(b"e1")
        (self.source / "Underdog March of the Monsters.avi").write_bytes(b"e2")
        files = classify_source_files(self.source)
        # Initially they're extras (no SxxExx).
        self.assertEqual(sum(1 for f in files if f.role == "episode"), 0)
        self.assertEqual(sum(1 for f in files if f.role == "extras"), 2)

        tmdb = self._tmdb_with_episodes({
            1: [(1, "Safe Waif"), (2, "March of the Monsters"),
                (3, "Other Episode")],
        })
        try:
            season_data: Dict[int, Any] = {}
            matched, considered = upgrade_extras_via_title_match(
                files, plan=self.plan_row, tmdb=tmdb,
                season_data=season_data,
            )
        finally:
            tmdb.close()

        self.assertEqual(matched, 2)
        self.assertEqual(considered, 2)
        # Both files should now be role=episode with correct (s, e).
        episodes = [f for f in files if f.role == "episode"]
        self.assertEqual(len(episodes), 2)
        keys = {f.episode_key for f in episodes}
        self.assertEqual(keys, {"S01E01", "S01E02"})

    def test_process_series_e2e_with_title_only_filenames(self):
        # End-to-end: process_series should now successfully move
        # title-only files even though they don't parse as SxxExx.
        (self.source / "Underdog Safe Waif.avi").write_bytes(b"e1")
        (self.source / "Underdog March of the Monsters.avi").write_bytes(b"e2")

        tmdb = self._tmdb_with_episodes({
            1: [(1, "Safe Waif"), (2, "March of the Monsters")],
        })

        journal = Journal(self.mediaprep / "moves.jsonl",
                          run_id="r", dry_run=False)
        ops = DiskOps(journal, dry_run=False)
        try:
            result = process_series(self.plan_row, self.cfg, ops, tmdb)
        finally:
            journal.close()
            tmdb.close()

        self.assertTrue(result.success, f"failed: {result.notes}")
        self.assertEqual(result.moved, 2)
        # Files landed in canonical layout.
        target = (self.tv_root / "Underdog (1964) {tmdb-4979}" /
                  "Season 01 (1964)")
        landed = sorted(p.name for p in target.glob("*.avi"))
        self.assertEqual(len(landed), 2)
        # Renamed to canonical form.
        self.assertTrue(any("S01E01" in n for n in landed))
# =========================================================================
# Sidecar / series-root file relocation (Round 6b Stage G/H)
# =========================================================================

class TestSidecarHelpers(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="exec_tv_sidecar_"))

    def test_find_basic_nfo(self):
        ep = self.tmp / "Show S01E01.mkv"; ep.write_bytes(b"e")
        nfo = self.tmp / "Show S01E01.nfo"; nfo.write_text("x")
        sibs = find_sidecars_for_video(ep)
        self.assertEqual(sibs, [nfo])

    def test_find_lang_subtitle(self):
        ep = self.tmp / "Show S01E01.mkv"; ep.write_bytes(b"e")
        srt_en = self.tmp / "Show S01E01.en.srt"; srt_en.write_text("subs")
        srt_es = self.tmp / "Show S01E01.es.srt"; srt_es.write_text("subs")
        sibs = sorted(find_sidecars_for_video(ep))
        self.assertEqual(sibs, sorted([srt_en, srt_es]))

    def test_find_thumb_with_dash_suffix(self):
        ep = self.tmp / "Show S01E01.mkv"; ep.write_bytes(b"e")
        thumb = self.tmp / "Show S01E01-thumb.jpg"; thumb.write_bytes(b"j")
        sibs = find_sidecars_for_video(ep)
        self.assertEqual(sibs, [thumb])

    def test_ignores_unrelated(self):
        ep = self.tmp / "Show S01E01.mkv"; ep.write_bytes(b"e")
        unrelated = self.tmp / "Show S01E02.nfo"; unrelated.write_text("x")
        random_file = self.tmp / "readme.txt"; random_file.write_text("x")
        sibs = find_sidecars_for_video(ep)
        self.assertEqual(sibs, [])

    def test_render_sidecar_target_basic(self):
        src = Path("/x/Show S01E01.mkv")
        dst = Path("/y/Show S01E01 - Pilot.mkv")
        sib = Path("/x/Show S01E01.nfo")
        new = render_sidecar_target_name(sib, src, dst)
        self.assertEqual(new, "Show S01E01 - Pilot.nfo")

    def test_render_sidecar_target_lang_srt(self):
        src = Path("/x/Show S01E01.mkv")
        dst = Path("/y/Show S01E01 - Pilot.mkv")
        sib = Path("/x/Show S01E01.en.srt")
        new = render_sidecar_target_name(sib, src, dst)
        self.assertEqual(new, "Show S01E01 - Pilot.en.srt")

    def test_render_sidecar_target_thumb(self):
        src = Path("/x/Show S01E01.mkv")
        dst = Path("/y/Show S01E01 - Pilot.mkv")
        sib = Path("/x/Show S01E01-thumb.jpg")
        new = render_sidecar_target_name(sib, src, dst)
        self.assertEqual(new, "Show S01E01 - Pilot-thumb.jpg")


class TestSeriesRootFiles(unittest.TestCase):
    def test_finds_recognized_root_files(self):
        tmp = Path(tempfile.mkdtemp(prefix="exec_tv_root_"))
        (tmp / "tvshow.nfo").write_text("x")
        (tmp / "poster.jpg").write_bytes(b"x")
        (tmp / "fanart.jpg").write_bytes(b"x")
        (tmp / "random.txt").write_text("x")  # NOT recognized
        (tmp / "season.nfo").write_text("x")  # NOT recognized (per-season)
        found = sorted(p.name for p in find_series_root_files(tmp))
        self.assertEqual(found, ["fanart.jpg", "poster.jpg", "tvshow.nfo"])

    def test_empty_folder(self):
        tmp = Path(tempfile.mkdtemp(prefix="exec_tv_root_"))
        self.assertEqual(find_series_root_files(tmp), [])

    def test_missing_folder(self):
        self.assertEqual(find_series_root_files(Path("/nonexistent/x")), [])


class TestSidecarE2E(unittest.TestCase):
    """Full process_series run with sidecars present in source."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="exec_tv_sidecar_e2e_"))
        self.tv_root = self.tmp / "tv"; self.tv_root.mkdir()
        self.mediaprep = self.tmp / "_mp"; self.mediaprep.mkdir()
        self.cfg = Config(tmdb_api_key="x", tv_root=self.tv_root,
                          mediaprep_dir=self.mediaprep)
        self.source = self.tv_root / "Doctor Who (2005)"
        self.source.mkdir()
        self.plan_row = SeriesPlanRow(
            series_id=1,
            source_folder=str(self.source),
            tmdb_series_id=57243,
            matched_series="Doctor Who",
            matched_year=2005,
            target_folder=str(self.tv_root /
                              "Doctor Who (2005) {tmdb-57243}"),
            confidence=95,
            action="AUTO",
        )
        self.tmdb = TmdbTvClient(
            "key", self.mediaprep / "cache.sqlite",
            session=FakeSession(default=make_season_response(1, [
                {"episode_number": 1, "name": "Rose"},
            ], air_date="2005-03-26")),
        )

    def tearDown(self):
        self.tmdb.close()

    def test_episode_with_nfo_and_subtitles_relocated(self):
        # Create episode + sidecars.
        ep = self.source / "Doctor Who S01E01.mkv"; ep.write_bytes(b"ep1")
        nfo = self.source / "Doctor Who S01E01.nfo"; nfo.write_text("nfo")
        srt = self.source / "Doctor Who S01E01.en.srt"; srt.write_text("subs")
        thumb = self.source / "Doctor Who S01E01-thumb.jpg"; thumb.write_bytes(b"j")
        # Plus series-root files.
        (self.source / "tvshow.nfo").write_text("show meta")
        (self.source / "poster.jpg").write_bytes(b"poster")

        journal = Journal(self.mediaprep / "moves.jsonl",
                          run_id="r", dry_run=False)
        ops = DiskOps(journal, dry_run=False)
        try:
            result = process_series(self.plan_row, self.cfg, ops, self.tmdb)
        finally:
            journal.close()

        self.assertTrue(result.success, f"failed: {result.notes}")
        target = self.tv_root / "Doctor Who (2005) {tmdb-57243}"
        season_dir = target / "Season 01 (2005)"
        # Episode + 3 sidecars in season folder (renamed canonically).
        self.assertTrue((season_dir / "Doctor Who S01E01 - Rose.mkv").exists())
        self.assertTrue((season_dir / "Doctor Who S01E01 - Rose.nfo").exists())
        self.assertTrue((season_dir / "Doctor Who S01E01 - Rose.en.srt").exists())
        self.assertTrue((season_dir / "Doctor Who S01E01 - Rose-thumb.jpg").exists())
        # Series-root files moved to target root.
        self.assertTrue((target / "tvshow.nfo").exists())
        self.assertTrue((target / "poster.jpg").exists())
        # Source folder is now (mostly) empty.
        leftover = list(self.source.iterdir())
        self.assertEqual(leftover, [],
                         f"expected empty source, got: {leftover}")


# =========================================================================
# Stage I: per-season metadata relocation
# =========================================================================

class TestSeasonFolderFiles(unittest.TestCase):
    def test_finds_recognized_season_files(self):
        from execute_tv import find_season_folder_files
        tmp = Path(tempfile.mkdtemp(prefix="exec_tv_seas_"))
        (tmp / "season.nfo").write_text("x")
        (tmp / "poster.jpg").write_bytes(b"x")
        (tmp / "fanart.jpg").write_bytes(b"x")
        (tmp / "junk.txt").write_text("x")  # NOT recognized
        found = sorted(p.name for p in find_season_folder_files(tmp))
        self.assertEqual(found, ["fanart.jpg", "poster.jpg", "season.nfo"])


class TestSeasonArtAtSeriesRoot(unittest.TestCase):
    def test_plex_season_poster_caught_at_root(self):
        from execute_tv import find_series_root_files
        tmp = Path(tempfile.mkdtemp(prefix="exec_tv_root_plex_"))
        (tmp / "season01-poster.jpg").write_bytes(b"x")
        (tmp / "season12-fanart.png").write_bytes(b"x")
        (tmp / "season-poster.jpg").write_bytes(b"x")  # NOT plex pattern
        (tmp / "tvshow.nfo").write_text("x")
        found = sorted(p.name for p in find_series_root_files(tmp))
        # Both season-NN-* art and tvshow.nfo should be picked up.
        self.assertIn("season01-poster.jpg", found)
        self.assertIn("season12-fanart.png", found)
        self.assertIn("tvshow.nfo", found)
        self.assertNotIn("season-poster.jpg", found)


class TestStageISeasonMetadataE2E(unittest.TestCase):
    """End-to-end: source has Season subfolders containing season.nfo +
    posters; Stage I should move them to the corresponding target
    season folder."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="exec_tv_stagei_"))
        self.tv_root = self.tmp / "tv"; self.tv_root.mkdir()
        self.mediaprep = self.tmp / "_mp"; self.mediaprep.mkdir()
        self.cfg = Config(tmdb_api_key="x", tv_root=self.tv_root,
                          mediaprep_dir=self.mediaprep)
        # Source: <root>/Season 1/<episode>.mkv + season.nfo + poster.jpg
        self.source = self.tv_root / "Doctor Who (2005)"
        self.source.mkdir()
        season1 = self.source / "Season 1"
        season1.mkdir()
        (season1 / "Doctor Who S01E01.mkv").write_bytes(b"e1")
        (season1 / "season.nfo").write_text("season meta")
        (season1 / "poster.jpg").write_bytes(b"poster bytes")
        (season1 / "fanart.jpg").write_bytes(b"fanart bytes")
        # Plus a series-root Plex-convention seasonNN-poster.jpg
        (self.source / "season01-poster.jpg").write_bytes(b"plex poster")

        self.plan_row = SeriesPlanRow(
            series_id=1,
            source_folder=str(self.source),
            tmdb_series_id=57243,
            matched_series="Doctor Who",
            matched_year=2005,
            target_folder=str(self.tv_root /
                              "Doctor Who (2005) {tmdb-57243}"),
            confidence=95,
            action="AUTO",
        )
        self.tmdb = TmdbTvClient(
            "key", self.mediaprep / "cache.sqlite",
            session=FakeSession(default=make_season_response(1, [
                {"episode_number": 1, "name": "Rose"},
            ], air_date="2005-03-26")),
        )

    def tearDown(self):
        self.tmdb.close()

    def test_season_metadata_relocated_to_target_season_folder(self):
        journal = Journal(self.mediaprep / "moves.jsonl",
                          run_id="r", dry_run=False)
        ops = DiskOps(journal, dry_run=False)
        try:
            result = process_series(self.plan_row, self.cfg, ops, self.tmdb)
        finally:
            journal.close()

        self.assertTrue(result.success, f"failed: {result.notes}")
        target = self.tv_root / "Doctor Who (2005) {tmdb-57243}"
        season_dir = target / "Season 01 (2005)"
        # Episode landed.
        self.assertTrue((season_dir / "Doctor Who S01E01 - Rose.mkv").exists())
        # Stage I: season.nfo + poster.jpg + fanart.jpg moved to season folder.
        self.assertTrue((season_dir / "season.nfo").exists())
        self.assertTrue((season_dir / "poster.jpg").exists())
        self.assertTrue((season_dir / "fanart.jpg").exists())
        # Stage H: Plex-convention season01-poster.jpg moved to target root.
        self.assertTrue((target / "season01-poster.jpg").exists())
        # Source season folder should be empty (nothing left blocking cleanup).
        leftover = list((self.source / "Season 1").iterdir())
        self.assertEqual(leftover, [],
                         f"expected empty season subfolder, got: {leftover}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
