#!/usr/bin/env python3
"""
Statusline helper: print total billable tokens for ONE session transcript.

Called once per statusline refresh, so it has to be fast even for multi-MB
transcripts. Caches a result keyed by (transcript path, mtime, size). If the
transcript hasn't grown since the last run, we return the cached total
instantly without parsing anything.

Token definition matches lib.aggregate_tokens_by_day:
  input_tokens + cache_creation_input_tokens + cache_read_input_tokens + output_tokens
deduped by message.id (one assistant API response spans multiple JSONL
entries; only the last carries the final output_tokens).

Usage:
  python3 session_tokens.py <transcript_path>     # → "<int>\\n" on stdout
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
    # Use a hash so we don't have to deal with path-as-filename escaping.
    h = hashlib.sha1(str(transcript).encode()).hexdigest()[:16]
    return CACHE_DIR / f"sess-{transcript.stem}-{h}.json"


def _load_cache(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _save_cache(path: Path, payload: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, separators=(",", ":")))
        tmp.replace(path)
    except OSError:
        pass


def _scan(transcript: Path) -> int:
    """Full re-scan of the transcript. Returns total tokens."""
    seen_max: dict[str, int] = {}
    try:
        with transcript.open("r", encoding="utf-8", errors="replace") as f:
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
        return 0
    return sum(seen_max.values())


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        return 0
    transcript = Path(argv[1])
    if not transcript.is_file():
        return 0

    try:
        st = transcript.stat()
    except OSError:
        return 0

    cache_path = _cache_path_for(transcript)
    cached = _load_cache(cache_path)
    if (
        cached
        and cached.get("mtime") == st.st_mtime
        and cached.get("size") == st.st_size
    ):
        print(cached["total"])
        return 0

    total = _scan(transcript)
    _save_cache(
        cache_path,
        {"mtime": st.st_mtime, "size": st.st_size, "total": total},
    )
    print(total)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception:
        # Defensive: a statusline that vanishes is worse than one missing %w.
        sys.exit(0)
