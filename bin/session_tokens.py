#!/usr/bin/env python3
"""Statusline helper: print total billable tokens for one session and its subagents.

Called once per statusline refresh, so it has to be fast even for multi-MB
transcripts. Caches a result keyed by (transcript mtime+size, subagents dir
fingerprint). Returns the cached total instantly when nothing has changed.

Token definition matches lib.aggregate_tokens_by_day:
  input_tokens + cache_creation_input_tokens + cache_read_input_tokens + output_tokens
deduped by message.id (one assistant API response spans multiple JSONL
entries; only the last carries the final output_tokens).

Subagents (Agent tool spawns) write to <session-id>/subagents/*.jsonl alongside
the root transcript. Their tokens are counted here but NOT in the root transcript,
so skipping them understates %w for orchestrator-heavy sessions.

Usage:
  python3 session_tokens.py <transcript_path>     # -> "<int>\\n" on stdout
On any error the script prints nothing and exits 0 — the statusline must
degrade gracefully (showing "" for %w) rather than crash.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

CACHE_DIR = Path.home() / ".claude" / "statusline-usage-updater" / "cache"


def _cache_path_for(transcript: Path) -> Path:
    """Return the sidecar cache file path for a transcript.

    Args:
        transcript: Path to the root session transcript .jsonl.

    Returns:
        Path under CACHE_DIR, derived from a hash of the transcript path.
    """
    h = hashlib.sha1(str(transcript).encode()).hexdigest()[:16]
    return CACHE_DIR / f"sess-{transcript.stem}-{h}.json"


def _load_cache(path: Path) -> dict[str, object] | None:
    """Load the sidecar cache, returning None on any parse or IO error.

    Args:
        path: Cache file path.

    Returns:
        Parsed cache dict, or None if missing or unreadable.
    """
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())  # type: ignore[return-value]
    except (json.JSONDecodeError, OSError):
        return None


def _save_cache(path: Path, payload: dict[str, object]) -> None:
    """Write payload atomically; silently ignores IO errors.

    Args:
        path: Destination cache file.
        payload: Data to serialise as JSON.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, separators=(",", ":")))
        tmp.replace(path)
    except OSError:
        pass


def _subagents_fingerprint(transcript: Path) -> tuple[int, int]:
    """Return a cheap fingerprint for the session's subagents directory.

    Uses nanosecond mtime integers rather than float st_mtime to avoid
    precision loss on filesystems (e.g. APFS) where two distinct writes
    can alias to the same float value.

    On any OSError during directory traversal, returns (-1, -1) as a
    cache-busting sentinel so the caller always rescans rather than
    potentially serving a stale cached total.

    Args:
        transcript: Path to the root session transcript .jsonl.

    Returns:
        Tuple of (number of .jsonl subagent files, highest mtime_ns among
        them), or (0, 0) when the subagents directory does not exist, or
        (-1, -1) when the directory exists but cannot be read.
    """
    subagents_dir = transcript.parent / transcript.stem / "subagents"
    if not subagents_dir.is_dir():
        return (0, 0)
    count = 0
    max_mtime_ns = 0
    try:
        for entry in subagents_dir.iterdir():
            if entry.suffix == ".jsonl" and entry.is_file():
                count += 1
                mtime_ns = entry.stat().st_mtime_ns
                if mtime_ns > max_mtime_ns:
                    max_mtime_ns = mtime_ns
    except OSError:
        return (-1, -1)
    return (count, max_mtime_ns)


def _scan_jsonl(path: Path, seen_max: dict[str, int]) -> None:
    """Scan one JSONL file and merge token counts into seen_max.

    Handles the dedup-by-message-ID invariant: one assistant API response
    is serialised across multiple entries sharing the same message.id;
    only the final entry carries the accurate output_tokens. Taking the max
    across all entries with the same ID gives the correct final count.

    Args:
        path: JSONL file to scan (root transcript or a subagent file).
        seen_max: Mutable dict mapping message ID -> highest observed token
            count. Updated in place.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "assistant":
                    continue
                msg = entry.get("message") or {}
                usage = msg.get("usage") or {}
                if not usage:
                    continue
                mid = msg.get("id")
                if not mid:
                    continue
                tokens = (
                    int(usage.get("input_tokens") or 0)
                    + int(usage.get("cache_creation_input_tokens") or 0)
                    + int(usage.get("cache_read_input_tokens") or 0)
                    + int(usage.get("output_tokens") or 0)
                )
                prev = seen_max.get(mid, 0)
                if tokens > prev:
                    seen_max[mid] = tokens
    except OSError:
        pass


def _scan(transcript: Path) -> int:
    """Scan the root transcript and all subagent files; return total tokens.

    Root and subagent message IDs are disjoint in practice: each is an
    independent API request assigned a unique ID by Anthropic's API.
    The shared seen_max dict therefore accumulates both without collision.
    In the degenerate case where an ID did appear in both files, the max
    dedup would count only the higher of the two token values — a slight
    undercount, not a crash or overcounting.

    Args:
        transcript: Path to the root session transcript .jsonl.

    Returns:
        Total deduplicated token count across the root session and subagents.
    """
    seen_max: dict[str, int] = {}
    _scan_jsonl(transcript, seen_max)
    subagents_dir = transcript.parent / transcript.stem / "subagents"
    if subagents_dir.is_dir():
        try:
            for sub in subagents_dir.iterdir():
                if sub.suffix == ".jsonl" and sub.is_file():
                    _scan_jsonl(sub, seen_max)
        except OSError:
            pass
    return sum(seen_max.values())


def main(argv: list[str]) -> int:
    """Print total tokens for transcript_path to stdout.

    Args:
        argv: sys.argv; expects exactly one positional argument (transcript path).

    Returns:
        Always 0; errors degrade to silence rather than a non-zero exit code.
    """
    if len(argv) != 2:
        return 0
    transcript = Path(argv[1])
    if not transcript.is_file():
        return 0

    try:
        st = transcript.stat()
    except OSError:
        return 0

    sub_count, sub_max_mtime_ns = _subagents_fingerprint(transcript)

    cache_path = _cache_path_for(transcript)
    cached = _load_cache(cache_path)
    if cached is not None:
        cached_total = cached.get("total")
        if (
            cached_total is not None
            and cached.get("mtime_ns") == st.st_mtime_ns
            and cached.get("size") == st.st_size
            and cached.get("sub_count") == sub_count
            and cached.get("sub_max_mtime_ns") == sub_max_mtime_ns
        ):
            print(cached_total)
            return 0

    total = _scan(transcript)
    _save_cache(
        cache_path,
        {
            "mtime_ns": st.st_mtime_ns,
            "size": st.st_size,
            "sub_count": sub_count,
            "sub_max_mtime_ns": sub_max_mtime_ns,
            "total": total,
        },
    )
    print(total)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception:
        # A statusline that vanishes is worse than one missing %w.
        sys.exit(0)
