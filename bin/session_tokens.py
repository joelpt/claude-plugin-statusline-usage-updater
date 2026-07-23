#!/usr/bin/env python3
"""Statusline helper: print one session's cost-weighted usage, active time,
AND raw token usage.

Called once per statusline refresh, so it has to be fast even for multi-MB
transcripts. Caches a result keyed by (transcript mtime+size, sidecar dir
fingerprint). Returns the cached values instantly when nothing has changed.

Output (single line, three space-separated integers):
  "<cost_units> <active_seconds> <total_tokens>"
  - cost_units: cost-weighted micro-USD across the session + all subagents AND
    workflows (see pricing.py — output 5x input, cache-read 0.1x, 1h-write 2x).
    Matches lib.aggregate_tokens_by_day's definition, so the %w coefficient
    (util% ÷ 7-day cost units) maps it accurately. Deduped by message.id (one
    assistant API response spans multiple JSONL entries; only the last carries
    the final output_tokens — take the max).
  - active_seconds: SUM of "not idle" time across the root transcript and every
    subagent/workflow file — inter-entry gaps >5min (HITL waits, idle) excluded.
    This is total Claude *compute* time (parallel subagents add up), not
    wall-clock.
  - total_tokens: "fresh" (non-cost-weighted) input + output + cache_creation
    tokens across the session + all subagents AND workflows — deliberately
    EXCLUDES cache_read_input_tokens, which re-counts the same accumulated
    context on every turn and would otherwise balloon into the tens of
    millions without reflecting new work (see pricing.py:fresh_token_units).
    Same message.id dedup as cost_units.

Sidecar transcripts live under <session-id>/ — subagents/*.jsonl,
subagents/workflows/wf_*/agent-*.jsonl, etc. We recurse the whole sidecar dir so
workflow-agent burn isn't missed.

On any error the script prints nothing and exits 0 — the statusline must
degrade gracefully (showing "" for %w) rather than crash.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pricing import (  # noqa: E402  # pyright: ignore[reportMissingImports]
    fresh_token_units,
    weighted_cost_units,
)

_IDLE_GAP_S = 5 * 60  # inter-entry gaps longer than this are idle, not work

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
    # Recurse the entire sidecar dir (subagents/ AND subagents/workflows/...) so
    # the cache invalidates when any agent or workflow transcript changes — _scan
    # reads them all, so the fingerprint must cover them all.
    sidecar = transcript.parent / transcript.stem
    if not sidecar.is_dir():
        return (0, 0)
    count = 0
    max_mtime_ns = 0
    try:
        for entry in sidecar.rglob("*.jsonl"):
            if entry.is_file():
                count += 1
                mtime_ns = entry.stat().st_mtime_ns
                if mtime_ns > max_mtime_ns:
                    max_mtime_ns = mtime_ns
    except OSError:
        return (-1, -1)
    return (count, max_mtime_ns)


def _parse_ts(ts: str) -> float | None:
    """ISO-8601 → epoch seconds. Returns None on failure."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _scan_jsonl(
    path: Path, seen_max: dict[str, int], seen_tokens: dict[str, int]
) -> float:
    """Scan one JSONL file: merge cost/token units into seen_max/seen_tokens.

    Cost and token dedup-by-message-ID invariant: one assistant API response is
    serialised across multiple entries sharing the same message.id; only the
    final entry carries the accurate output_tokens (hence the highest cost and
    token count). Taking the max across entries with the same ID gives the
    correct final value for each.

    Active seconds: sum of gaps between consecutive entry timestamps in this file
    that are shorter than the idle threshold — i.e. time the agent was actually
    working, not waiting on a human or sitting idle.

    Args:
        path: JSONL file to scan (root transcript or a subagent/workflow file).
        seen_max: Mutable dict mapping message ID -> highest observed cost units.
        seen_tokens: Mutable dict mapping message ID -> highest observed raw
            token count.

    Returns:
        Active (non-idle) seconds within this file.
    """
    times: list[float] = []
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
                t = _parse_ts(entry.get("timestamp") or "")
                if t is not None:
                    times.append(t)
                if entry.get("type") != "assistant":
                    continue
                msg = entry.get("message") or {}
                usage = msg.get("usage") or {}
                mid = msg.get("id")
                if not usage or not mid:
                    continue
                cost = weighted_cost_units(usage, msg.get("model"))
                if cost > seen_max.get(mid, 0):
                    seen_max[mid] = cost
                tokens = fresh_token_units(usage)
                if tokens > seen_tokens.get(mid, 0):
                    seen_tokens[mid] = tokens
    except OSError:
        return 0.0
    times.sort()
    active = 0.0
    for a, b in zip(times, times[1:]):
        gap = b - a
        if 0 < gap < _IDLE_GAP_S:
            active += gap
    return active


def _scan(transcript: Path) -> tuple[int, int, int]:
    """Scan the root transcript + entire sidecar tree (subagents AND workflows).

    Root and sidecar message IDs are disjoint in practice (each is an independent
    API request with a unique id), so the shared seen_max/seen_tokens dicts
    accumulate all without collision. Active time is summed per file — parallel
    subagents add up to total compute time, which is the intent.

    Returns:
        (total_cost_units, total_active_seconds, total_tokens) across the whole
        session.
    """
    seen_max: dict[str, int] = {}
    seen_tokens: dict[str, int] = {}
    active = _scan_jsonl(transcript, seen_max, seen_tokens)
    sidecar = transcript.parent / transcript.stem
    if sidecar.is_dir():
        try:
            for sub in sidecar.rglob("*.jsonl"):
                if sub.is_file():
                    active += _scan_jsonl(sub, seen_max, seen_tokens)
        except OSError:
            pass
    return sum(seen_max.values()), int(round(active)), sum(seen_tokens.values())


def main(argv: list[str]) -> int:
    """Print "<cost_units> <active_seconds> <total_tokens>" for transcript_path.

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
        # "cost"/"active_s"/"tokens" present only in the current cache schema;
        # an older cache (missing any of these) is treated as a miss and rescanned.
        if (
            cached.get("cost") is not None
            and cached.get("active_s") is not None
            and cached.get("tokens") is not None
            and cached.get("mtime_ns") == st.st_mtime_ns
            and cached.get("size") == st.st_size
            and cached.get("sub_count") == sub_count
            and cached.get("sub_max_mtime_ns") == sub_max_mtime_ns
        ):
            print(f"{cached['cost']} {cached['active_s']} {cached['tokens']}")
            return 0

    cost, active_s, tokens = _scan(transcript)
    _save_cache(
        cache_path,
        {
            "mtime_ns": st.st_mtime_ns,
            "size": st.st_size,
            "sub_count": sub_count,
            "sub_max_mtime_ns": sub_max_mtime_ns,
            "cost": cost,
            "active_s": active_s,
            "tokens": tokens,
        },
    )
    print(f"{cost} {active_s} {tokens}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception:
        # A statusline that vanishes is worse than one missing %w.
        sys.exit(0)
