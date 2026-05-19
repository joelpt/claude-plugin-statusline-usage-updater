"""
Tests for the token aggregator and the anchor-line regex replace.

OAuth and the live usage endpoint are NOT exercised here — they'd need
either a live token or extensive mocking, and both the menubar app and
this plugin's own diagnostic CLI (`python3 bin/lib.py usage`) cover
those paths.

Focus is on the two pieces that would silently corrupt data if wrong:
  1. Per-day token aggregation with split-message dedupe.
  2. The anchor-line regex replace (must not touch any other line).

Run with:  python3 -m unittest discover tests/
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).parent.parent / "bin"))

import lib  # noqa: E402  (sys.path injection — pyright can't see this)
import update_coefficient  # noqa: E402


FIXTURES = Path(__file__).parent / "fixtures"


class TokenAggregationTests(unittest.TestCase):
    def setUp(self):
        # Build a fake projects dir containing the fixture transcript.
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "projects"
        proj = self.root / "-test-project"
        proj.mkdir(parents=True)
        (proj / "session.jsonl").write_bytes(
            (FIXTURES / "transcript_basic.jsonl").read_bytes()
        )

    def test_per_day_totals_dedupe_split_assistant_entries(self):
        # msg_A appears twice with output_tokens 5 then 200 — must take the MAX.
        # Expected per-day:
        #   2026-05-13: 100+50+1000+200 = 1350  (msg_A, final block)
        #   2026-05-14: 10+0+50000+40 = 50050   (msg_B; msg_C has zero usage skipped)
        #   2026-05-15: 1000+2000+3000+4000 = 10000  (msg_D)
        totals = lib.aggregate_tokens_by_day(self.root)
        self.assertEqual(totals.get("2026-05-13"), 1350)
        self.assertEqual(totals.get("2026-05-14"), 50050)
        self.assertEqual(totals.get("2026-05-15"), 10000)

    def test_only_dates_filter_skips_unrequested_days(self):
        totals = lib.aggregate_tokens_by_day(
            self.root, only_dates={"2026-05-14"}
        )
        self.assertEqual(totals, {"2026-05-14": 50050})

    def test_tokens_in_last_n_days_window(self):
        total, per_day = lib.tokens_in_last_n_days(
            n=3, today_utc="2026-05-15", projects_dir=self.root
        )
        # 13 + 14 + 15 covered
        self.assertEqual(total, 1350 + 50050 + 10000)
        self.assertEqual(per_day["2026-05-13"], 1350)
        self.assertEqual(per_day["2026-05-14"], 50050)
        self.assertEqual(per_day["2026-05-15"], 10000)


class AnchorReplaceTests(unittest.TestCase):
    def test_anchor_replace_only_touches_target_line(self):
        before = (
            "#!/usr/bin/env bash\n"
            "set -uo pipefail\n"
            "STATUSLINE_USAGE_PCT_PER_TOKEN=1.0e-09\n"
            "STATUSLINE_PEAK_HOURS_PT=\"5-11\"\n"
            "OTHER_VAR=1.0e-09  # do not touch\n"
            "echo 'usage pct per token line not here'\n"
        )
        new_value = "1.234567e-08"
        replacement = rf"\g<1>{new_value}\g<2>"
        patched, n = update_coefficient.COEFFICIENT_ANCHOR_RE.subn(
            replacement, before
        )
        self.assertEqual(n, 1)
        self.assertIn(f"STATUSLINE_USAGE_PCT_PER_TOKEN={new_value}", patched)
        # Other lines untouched
        self.assertIn("STATUSLINE_PEAK_HOURS_PT=\"5-11\"", patched)
        self.assertIn("OTHER_VAR=1.0e-09  # do not touch", patched)

    def test_anchor_replace_preserves_inline_comment(self):
        before = "STATUSLINE_USAGE_PCT_PER_TOKEN=1.0e-09 # auto-set\n"
        replacement = r"\g<1>9.9e-09\g<2>"
        patched, n = update_coefficient.COEFFICIENT_ANCHOR_RE.subn(
            replacement, before
        )
        self.assertEqual(n, 1)
        self.assertEqual(patched, "STATUSLINE_USAGE_PCT_PER_TOKEN=9.9e-09 # auto-set\n")

    def test_anchor_replace_no_match_reports_zero(self):
        before = "echo hello\nSTATUSLINE_PEAK_HOURS_PT=''\n"
        _, n = update_coefficient.COEFFICIENT_ANCHOR_RE.subn(
            r"\g<1>9.9e-09\g<2>", before
        )
        self.assertEqual(n, 0)


class CoefficientAveragingTests(unittest.TestCase):
    def test_merge_sample_overwrites_same_date(self):
        samples = [
            {"date": "2026-05-10", "coefficient": 1.0e-8},
            {"date": "2026-05-11", "coefficient": 1.1e-8},
        ]
        new = {"date": "2026-05-11", "coefficient": 1.5e-8}  # overwrite
        merged = update_coefficient.merge_sample(samples, new)
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[-1]["coefficient"], 1.5e-8)

    def test_merge_sample_trims_to_max(self):
        samples = [
            {"date": f"2026-05-{d:02d}", "coefficient": 1.0e-8}
            for d in range(1, 9)
        ]
        new = {"date": "2026-05-09", "coefficient": 2.0e-8}
        merged = update_coefficient.merge_sample(samples, new)
        self.assertEqual(len(merged), update_coefficient.MAX_SAMPLES)
        # Oldest dropped
        dates = [s["date"] for s in merged]
        self.assertEqual(dates[0], "2026-05-03")
        self.assertEqual(dates[-1], "2026-05-09")

    def test_average_coefficient_returns_bootstrap_when_empty(self):
        self.assertEqual(
            update_coefficient.average_coefficient([]),
            lib.BOOTSTRAP_COEFFICIENT,
        )

    def test_average_coefficient_mean(self):
        samples = [
            {"date": "x", "coefficient": 1.0e-8},
            {"date": "y", "coefficient": 2.0e-8},
            {"date": "z", "coefficient": 3.0e-8},
        ]
        self.assertAlmostEqual(
            update_coefficient.average_coefficient(samples), 2.0e-8
        )


if __name__ == "__main__":
    unittest.main()
