"""Tests for session_tokens.py — root transcript scanning and subagent rollup.

Covers:
  - Per-file dedup-by-message-id (takes max across split assistant entries).
  - Subagent JSONL files are included in the total.
  - Root and subagent message IDs are disjoint (no cross-file collision).
  - _subagents_fingerprint returns accurate (count, max_mtime).
  - Cache hits when nothing changes; misses when root or subagents change.

Run with:  python3 -m unittest discover tests/
"""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "bin"))

import session_tokens  # noqa: E402  # type: ignore[import-not-found]
import pricing  # noqa: E402  # type: ignore[import-not-found]


def _cost(input_t: int = 0, cache_create: int = 0, cache_read: int = 0,
          output_t: int = 0, model: str | None = None) -> int:
    """Cost-weighted micro-USD for one call (the value the helper now sums)."""
    return pricing.weighted_cost_units({
        "input_tokens": input_t,
        "cache_creation_input_tokens": cache_create,
        "cache_read_input_tokens": cache_read,
        "output_tokens": output_t,
    }, model)


def _entry(msg_id: str, input_t: int = 0, cache_create: int = 0,
           cache_read: int = 0, output_t: int = 0) -> str:
    """Return a single serialised assistant JSONL line.

    Args:
        msg_id: The message.id value.
        input_t: input_tokens.
        cache_create: cache_creation_input_tokens.
        cache_read: cache_read_input_tokens.
        output_t: output_tokens.

    Returns:
        JSON-serialised line (no trailing newline).
    """
    return json.dumps({
        "type": "assistant",
        "message": {
            "id": msg_id,
            "usage": {
                "input_tokens": input_t,
                "cache_creation_input_tokens": cache_create,
                "cache_read_input_tokens": cache_read,
                "output_tokens": output_t,
            },
        },
    })


def _write_jsonl(path: Path, lines: list[str]) -> None:
    """Write lines as a JSONL file.

    Args:
        path: Destination file.
        lines: Serialised JSON lines (newline appended to each).
    """
    path.write_text("\n".join(lines) + "\n")


class ScanJsonlTests(unittest.TestCase):
    """Tests for _scan_jsonl: per-file token extraction and dedup."""

    def setUp(self) -> None:
        """Create a temporary directory for JSONL fixtures."""
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_sums_all_token_types(self) -> None:
        """All four token fields contribute to the total."""
        f = self.root / "a.jsonl"
        _write_jsonl(f, [_entry("msg1", input_t=10, cache_create=20,
                                cache_read=30, output_t=40)])
        seen: dict[str, int] = {}
        session_tokens._scan_jsonl(f, seen)
        self.assertEqual(sum(seen.values()),
                         _cost(input_t=10, cache_create=20, cache_read=30, output_t=40))

    def test_dedup_takes_max_across_split_entries(self) -> None:
        """When the same message.id appears twice, the higher token count wins."""
        f = self.root / "a.jsonl"
        _write_jsonl(f, [
            _entry("msg1", output_t=5),    # stale early block
            _entry("msg1", output_t=200),  # final block — correct value
        ])
        seen: dict[str, int] = {}
        session_tokens._scan_jsonl(f, seen)
        self.assertEqual(seen["msg1"], _cost(output_t=200))
        self.assertEqual(sum(seen.values()), _cost(output_t=200))

    def test_skips_non_assistant_entries(self) -> None:
        """Lines with type != 'assistant' contribute zero tokens."""
        f = self.root / "a.jsonl"
        f.write_text(
            json.dumps({"type": "user", "content": "hello"}) + "\n" +
            _entry("msg1", output_t=50) + "\n"
        )
        seen: dict[str, int] = {}
        session_tokens._scan_jsonl(f, seen)
        self.assertEqual(sum(seen.values()), _cost(output_t=50))

    def test_skips_entries_without_message_id(self) -> None:
        """Entries whose message has no id are silently skipped."""
        f = self.root / "a.jsonl"
        f.write_text(json.dumps({
            "type": "assistant",
            "message": {"usage": {"input_tokens": 99, "output_tokens": 1}},
        }) + "\n")
        seen: dict[str, int] = {}
        session_tokens._scan_jsonl(f, seen)
        self.assertEqual(sum(seen.values()), 0)

    def test_tolerates_missing_file(self) -> None:
        """A non-existent path leaves seen_max unchanged (no exception)."""
        seen: dict[str, int] = {}
        session_tokens._scan_jsonl(self.root / "missing.jsonl", seen)
        self.assertEqual(seen, {})


class ScanSubagentsTests(unittest.TestCase):
    """Tests for _scan: root + subagents directory rollup."""

    def setUp(self) -> None:
        """Create a temp project dir with a root transcript."""
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        proj = Path(self.tmp.name) / "proj"
        proj.mkdir()
        self.transcript = proj / "abc123.jsonl"
        _write_jsonl(self.transcript, [_entry("root_msg", output_t=1000)])

    def test_root_only_when_no_subagents_dir(self) -> None:
        """Returns root tokens when no subagents directory exists."""
        self.assertEqual(session_tokens._scan(self.transcript),
                         (_cost(output_t=1000), 0))

    def test_subagent_tokens_added_to_root(self) -> None:
        """Tokens from subagents/*.jsonl are summed with root tokens."""
        sub_dir = self.transcript.parent / "abc123" / "subagents"
        sub_dir.mkdir(parents=True)
        _write_jsonl(sub_dir / "agent-001.jsonl", [_entry("sub_msg", output_t=500)])
        self.assertEqual(session_tokens._scan(self.transcript),
                         (_cost(output_t=1000) + _cost(output_t=500), 0))

    def test_multiple_subagents_all_counted(self) -> None:
        """All subagent files contribute to the total."""
        sub_dir = self.transcript.parent / "abc123" / "subagents"
        sub_dir.mkdir(parents=True)
        _write_jsonl(sub_dir / "agent-001.jsonl", [_entry("sub1", output_t=100)])
        _write_jsonl(sub_dir / "agent-002.jsonl", [_entry("sub2", output_t=200)])
        _write_jsonl(sub_dir / "agent-003.jsonl", [_entry("sub3", output_t=300)])
        self.assertEqual(session_tokens._scan(self.transcript),
                         (_cost(output_t=1000) + _cost(output_t=100)
                          + _cost(output_t=200) + _cost(output_t=300), 0))

    def test_disjoint_ids_no_cross_file_collision(self) -> None:
        """Root and subagent message IDs are independent — no over-dedup."""
        sub_dir = self.transcript.parent / "abc123" / "subagents"
        sub_dir.mkdir(parents=True)
        # Use a completely different ID in the subagent — should not dedup with root.
        _write_jsonl(sub_dir / "agent-001.jsonl", [_entry("different_id", output_t=500)])
        self.assertEqual(session_tokens._scan(self.transcript),
                         (_cost(output_t=1000) + _cost(output_t=500), 0))

    def test_meta_json_files_ignored(self) -> None:
        """Non-.jsonl files (e.g. .meta.json) in subagents/ are skipped."""
        sub_dir = self.transcript.parent / "abc123" / "subagents"
        sub_dir.mkdir(parents=True)
        (sub_dir / "agent-001.meta.json").write_text('{"agentType":"general-purpose"}')
        self.assertEqual(session_tokens._scan(self.transcript),
                         (_cost(output_t=1000), 0))

    def test_same_id_across_root_and_subagent_takes_max(self) -> None:
        """If IDs collide across files, max-dedup undercounts slightly but doesn't crash.

        In practice root and subagent IDs are disjoint (independent API calls),
        but this documents the degenerate behavior rather than leaving it implicit.
        """
        sub_dir = self.transcript.parent / "abc123" / "subagents"
        sub_dir.mkdir(parents=True)
        # Both root and subagent use "root_msg"; subagent has higher tokens.
        _write_jsonl(sub_dir / "agent-001.jsonl", [_entry("root_msg", output_t=2000)])
        # Root has root_msg cost(1000), subagent has root_msg cost(2000).
        # Max-dedup takes the higher and ignores the other → cost(2000), not their sum.
        self.assertEqual(session_tokens._scan(self.transcript),
                         (_cost(output_t=2000), 0))


class FingerprintTests(unittest.TestCase):
    """Tests for _subagents_fingerprint."""

    def setUp(self) -> None:
        """Create a temp directory with a stub transcript."""
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        proj = Path(self.tmp.name) / "proj"
        proj.mkdir()
        self.transcript = proj / "sess1.jsonl"
        self.transcript.write_text("")

    def test_missing_dir_returns_zeros(self) -> None:
        """Returns (0, 0) when the subagents directory does not exist."""
        self.assertEqual(session_tokens._subagents_fingerprint(self.transcript), (0, 0))

    def test_empty_dir_returns_zeros(self) -> None:
        """Returns (0, 0) when the subagents directory exists but is empty."""
        sub_dir = self.transcript.parent / "sess1" / "subagents"
        sub_dir.mkdir(parents=True)
        self.assertEqual(session_tokens._subagents_fingerprint(self.transcript), (0, 0))

    def test_counts_only_jsonl_files(self) -> None:
        """Only .jsonl files count; meta JSON and other files are excluded."""
        sub_dir = self.transcript.parent / "sess1" / "subagents"
        sub_dir.mkdir(parents=True)
        (sub_dir / "agent-001.jsonl").write_text("")
        (sub_dir / "agent-001.meta.json").write_text("{}")
        count, _ = session_tokens._subagents_fingerprint(self.transcript)
        self.assertEqual(count, 1)

    def test_max_mtime_ns_reflects_newest_file(self) -> None:
        """max_mtime_ns is the highest st_mtime_ns among .jsonl subagent files."""
        sub_dir = self.transcript.parent / "sess1" / "subagents"
        sub_dir.mkdir(parents=True)
        f1 = sub_dir / "agent-001.jsonl"
        f2 = sub_dir / "agent-002.jsonl"
        f1.write_text("")
        f2.write_text("")
        os.utime(f1, (1000.0, 1000.0))
        os.utime(f2, (2000.0, 2000.0))
        count, max_mtime_ns = session_tokens._subagents_fingerprint(self.transcript)
        self.assertEqual(count, 2)
        # st_mtime_ns for 2000.0s epoch = 2_000_000_000_000 ns (approximately)
        self.assertEqual(max_mtime_ns, f2.stat().st_mtime_ns)

    def test_oserror_during_scan_returns_sentinel(self) -> None:
        """OSError mid-scan returns (-1, -1) so the caller busts the cache."""
        sub_dir = self.transcript.parent / "sess1" / "subagents"
        sub_dir.mkdir(parents=True)
        (sub_dir / "agent-001.jsonl").write_text("")
        with patch("pathlib.Path.rglob", side_effect=OSError("permission denied")):
            result = session_tokens._subagents_fingerprint(self.transcript)
        self.assertEqual(result, (-1, -1))


class CacheTests(unittest.TestCase):
    """Tests for cache hit/miss logic in main()."""

    def setUp(self) -> None:
        """Create a temp project with a transcript and patch CACHE_DIR."""
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        proj = root / "proj"
        proj.mkdir()
        self.transcript = proj / "sess1.jsonl"
        _write_jsonl(self.transcript, [_entry("msg1", output_t=777)])
        self.cache_dir = root / "cache"
        self.patcher = patch.object(session_tokens, "CACHE_DIR", self.cache_dir)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def _run_main(self) -> str:
        """Run main() and capture stdout."""
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            session_tokens.main(["-", str(self.transcript)])
        return buf.getvalue().strip()

    def test_first_run_returns_total(self) -> None:
        """First run computes and prints "<cost> <active_s>" (no timestamps → 0)."""
        self.assertEqual(self._run_main(), f"{_cost(output_t=777)} 0")

    def test_cache_hit_returns_same_value(self) -> None:
        """Second run with unchanged transcript returns cached value."""
        self.assertEqual(self._run_main(), f"{_cost(output_t=777)} 0")
        self.assertEqual(self._run_main(), f"{_cost(output_t=777)} 0")

    def test_cache_miss_when_transcript_grows(self) -> None:
        """Adding to the root transcript busts the cache."""
        self.assertEqual(self._run_main(), f"{_cost(output_t=777)} 0")
        # Append a new message to the transcript.
        with self.transcript.open("a") as f:
            f.write(_entry("msg2", output_t=100) + "\n")
        self.assertEqual(self._run_main(),
                         f"{_cost(output_t=777) + _cost(output_t=100)} 0")

    def test_cache_miss_when_subagent_added(self) -> None:
        """Adding a new subagent file busts the cache (sub_count changes)."""
        self.assertEqual(self._run_main(), f"{_cost(output_t=777)} 0")
        sub_dir = self.transcript.parent / "sess1" / "subagents"
        sub_dir.mkdir(parents=True)
        _write_jsonl(sub_dir / "agent-001.jsonl", [_entry("sub1", output_t=123)])
        self.assertEqual(self._run_main(),
                         f"{_cost(output_t=777) + _cost(output_t=123)} 0")

    def test_cache_miss_when_subagent_grows(self) -> None:
        """Growing an existing subagent file busts the cache (sub_max_mtime changes)."""
        sub_dir = self.transcript.parent / "sess1" / "subagents"
        sub_dir.mkdir(parents=True)
        f = sub_dir / "agent-001.jsonl"
        _write_jsonl(f, [_entry("sub1", output_t=50)])
        os.utime(f, (1000.0, 1000.0))
        self.assertEqual(self._run_main(),
                         f"{_cost(output_t=777) + _cost(output_t=50)} 0")
        # Grow the file, bump its mtime.
        with f.open("a") as fp:
            fp.write(_entry("sub2", output_t=50) + "\n")
        os.utime(f, (2000.0, 2000.0))
        self.assertEqual(self._run_main(),
                         f"{_cost(output_t=777) + 2 * _cost(output_t=50)} 0")


class MainEdgeCaseTests(unittest.TestCase):
    """Tests for main() argument handling and graceful degradation."""

    def test_no_args_returns_0_silently(self) -> None:
        """main([]) prints nothing and returns 0."""
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            result = session_tokens.main([])
        self.assertEqual(result, 0)
        self.assertEqual(buf.getvalue(), "")

    def test_nonexistent_path_returns_0_silently(self) -> None:
        """main() with a missing file prints nothing and returns 0."""
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            result = session_tokens.main(["-", "/nonexistent/path.jsonl"])
        self.assertEqual(result, 0)
        self.assertEqual(buf.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
