"""
Shared library for the statusline-usage-updater plugin.

Three responsibilities, kept in one file to keep the plugin self-contained
(no pip install, no package layout, just `python3 -m` from launchd):

  1. OAuth: read Claude Code's ~/.claude/.credentials.json, refresh access
     tokens via Anthropic's /v1/oauth/token endpoint when expired, cache the
     refreshed token in our OWN auth-cache.json (we never write back to
     Claude Code's credentials file — that would race Claude Code itself).

  2. Usage fetch: GET https://api.anthropic.com/api/oauth/usage. Returns the
     parsed seven-day utilisation percentage (0–100).

  3. Token aggregation: walk ~/.claude/projects/**.jsonl, sum
     (input + cache_creation + cache_read + output) tokens per UTC date,
     deduping by API call requestId because one assistant response is split
     across multiple JSONL entries (matches the session-report skill's logic).

The token definition is "every billable token reported by the API across the
day". Cache reads cost less per token than fresh inputs — but the coefficient
absorbs that mix. Don't try to "fix" this by re-weighting cache reads at 0.1;
the whole point of calibrating against realised usage is to let the user's own
mix drive the number.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


# Hard-coded from the Claude Code OAuth integration in the menubar app
# (Sources/ClaudeUsageMenuBar/TokenStore.swift). This is the public client ID
# for the Claude desktop/CLI OAuth flow — not a secret.
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"

TOKEN_URL = "https://api.anthropic.com/v1/oauth/token"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA_HEADER = "oauth-2025-04-20"
USER_AGENT = "statusline-usage-updater/0.1 (+https://github.com/joelpt/claude-plugins)"

HOME = Path.home()
CLAUDE_DIR = HOME / ".claude"
CLAUDE_CREDS = CLAUDE_DIR / ".credentials.json"
STATE_DIR = CLAUDE_DIR / "statusline-usage-updater"
AUTH_CACHE = STATE_DIR / "auth-cache.json"
SAMPLES_FILE = STATE_DIR / "samples.json"
COEFFICIENT_FILE = STATE_DIR / "coefficient.json"
LOG_DIR = STATE_DIR / "log"
LOCK_FILE = STATE_DIR / "update.lock"

# Bootstrap coefficient used until the first daily sample lands. The token
# total includes cache reads — which are 10× cheaper but dominate volume —
# so realised totals are in the billions per week. A heavy Max-20x user
# burns ~50% of weekly quota over ~5B billable tokens → 1e-8 per token,
# expressed as percent (i.e. tokens × 1e-8 ≈ %w). The updater overwrites
# this within ~24h of first install.
BOOTSTRAP_COEFFICIENT = 1.0e-8


# ---------------------------------------------------------------------------
# OAuth: token loading, refresh, caching
# ---------------------------------------------------------------------------


@dataclass
class Credentials:
    access_token: str
    refresh_token: str
    expires_at_ms: int  # epoch milliseconds, matching Claude Code's format

    def is_expired(self, skew_seconds: int = 60) -> bool:
        now_ms = int(time.time() * 1000)
        return self.expires_at_ms - now_ms <= skew_seconds * 1000


def _load_claude_code_credentials() -> Credentials:
    """Read Claude Code's own credentials file. Read-only; never written."""
    if not CLAUDE_CREDS.exists():
        raise RuntimeError(
            f"Claude Code credentials not found at {CLAUDE_CREDS}. "
            "Log in via Claude Code first."
        )
    blob = json.loads(CLAUDE_CREDS.read_text())
    oauth = blob.get("claudeAiOauth") or {}
    try:
        return Credentials(
            access_token=oauth["accessToken"],
            refresh_token=oauth["refreshToken"],
            expires_at_ms=int(oauth["expiresAt"]),
        )
    except KeyError as e:
        raise RuntimeError(f"Malformed credentials file: missing {e!s}")


def _load_cached_credentials() -> Credentials | None:
    if not AUTH_CACHE.exists():
        return None
    try:
        blob = json.loads(AUTH_CACHE.read_text())
        return Credentials(
            access_token=blob["access_token"],
            refresh_token=blob["refresh_token"],
            expires_at_ms=int(blob["expires_at_ms"]),
        )
    except (json.JSONDecodeError, KeyError):
        return None


def _save_cached_credentials(cred: Credentials) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = AUTH_CACHE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(
            {
                "access_token": cred.access_token,
                "refresh_token": cred.refresh_token,
                "expires_at_ms": cred.expires_at_ms,
            },
            indent=2,
        )
    )
    tmp.replace(AUTH_CACHE)
    os.chmod(AUTH_CACHE, 0o600)


def _refresh_credentials(cred: Credentials) -> Credentials:
    """POST to /v1/oauth/token with grant_type=refresh_token."""
    body = json.dumps(
        {
            "grant_type": "refresh_token",
            "refresh_token": cred.refresh_token,
            "client_id": CLIENT_ID,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 400-class: refresh token rejected. Caller should re-adopt from
        # Claude Code's credentials file and try once more.
        if 400 <= e.code < 500:
            raise InvalidGrantError(f"refresh rejected ({e.code}): {e.read().decode('utf-8', 'replace')[:200]}")
        raise

    issued_at_ms = int(time.time() * 1000)
    return Credentials(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token") or cred.refresh_token,
        expires_at_ms=issued_at_ms + int(data["expires_in"]) * 1000,
    )


class InvalidGrantError(RuntimeError):
    pass


def get_fresh_access_token() -> str:
    """Return a non-expired access token, refreshing or re-adopting as needed."""
    cred = _load_cached_credentials() or _load_claude_code_credentials()
    if cred.is_expired():
        try:
            cred = _refresh_credentials(cred)
        except InvalidGrantError:
            # Our cached refresh token was rejected. Re-read Claude Code's
            # file (which Claude Code itself refreshes during normal use)
            # and try once more.
            cred = _load_claude_code_credentials()
            if cred.is_expired():
                cred = _refresh_credentials(cred)
        _save_cached_credentials(cred)
    return cred.access_token


# ---------------------------------------------------------------------------
# Usage endpoint
# ---------------------------------------------------------------------------


def fetch_usage_snapshot() -> dict:
    """GET /api/oauth/usage. Returns the parsed JSON body."""
    token = get_fresh_access_token()
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "anthropic-beta": OAUTH_BETA_HEADER,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            # Token may have just expired between get_fresh and request.
            # Force a re-adopt and try once more.
            cred = _load_claude_code_credentials()
            cred = _refresh_credentials(cred)
            _save_cached_credentials(cred)
            req.add_header("Authorization", f"Bearer {cred.access_token}")
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        raise


def seven_day_utilization_pct(snapshot: dict | None = None) -> float | None:
    """Return the seven_day.utilization value as a 0–100 percentage, or None."""
    snap = snapshot if snapshot is not None else fetch_usage_snapshot()
    seven = snap.get("seven_day")
    if not seven or "utilization" not in seven:
        return None
    return float(seven["utilization"])


# ---------------------------------------------------------------------------
# Token aggregation from session transcripts
# ---------------------------------------------------------------------------


def _iter_transcript_files(projects_dir: Path) -> Iterator[Path]:
    if not projects_dir.is_dir():
        return
    for p in projects_dir.rglob("*.jsonl"):
        if p.is_file():
            yield p


def _safe_parse_line(line: str) -> dict | None:
    line = line.strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def _timestamp_to_utc_date(ts_iso: str) -> str | None:
    """ISO-8601 → 'YYYY-MM-DD' in UTC. Returns None on parse failure."""
    if not ts_iso:
        return None
    try:
        # Handle 'Z' suffix and fractional seconds
        clean = ts_iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def aggregate_tokens_by_day(
    projects_dir: Path | None = None,
    only_dates: set[str] | None = None,
) -> dict[str, int]:
    """
    Sum `input + cache_creation + cache_read + output` tokens per UTC date.

    Dedupes by message.id (one assistant API response is split across multiple
    JSONL `type:"assistant"` entries — only the LAST carries the final
    output_tokens; earlier blocks have stale counts). This mirrors the
    session-report skill's approach.

    `only_dates`: if given, skip any date not in this set. Faster for incremental
    runs that only need the past N days.
    """
    projects_dir = projects_dir or (CLAUDE_DIR / "projects")
    totals: dict[str, int] = {}
    # Per-message dedupe across the whole walk. A resumed session re-serializes
    # entries with the same message ids; we'd over-count without this.
    # Value is (date, tokens) so we can correctly back out the prior reading
    # from its ORIGINAL date if a later, higher reading shows up.
    seen_messages: dict[str, tuple[str, int]] = {}

    for path in _iter_transcript_files(projects_dir):
        try:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                for raw in f:
                    entry = _safe_parse_line(raw)
                    if not entry or entry.get("type") != "assistant":
                        continue
                    msg = entry.get("message") or {}
                    usage = msg.get("usage") or {}
                    if not usage:
                        continue
                    msg_id = msg.get("id")
                    if not msg_id:
                        continue
                    tokens = (
                        int(usage.get("input_tokens") or 0)
                        + int(usage.get("cache_creation_input_tokens") or 0)
                        + int(usage.get("cache_read_input_tokens") or 0)
                        + int(usage.get("output_tokens") or 0)
                    )
                    date = _timestamp_to_utc_date(entry.get("timestamp") or "")
                    if not date:
                        continue
                    # Dedupe: take MAX across split entries (the final block has
                    # the real output_tokens; earlier ones report a stale lower value).
                    prev = seen_messages.get(msg_id)
                    if prev is not None and tokens <= prev[1]:
                        continue
                    # When updating, subtract from the ORIGINAL date and add to
                    # the current one (handles the unlikely but real case where
                    # a re-serialized entry carries a different timestamp).
                    if prev is not None:
                        if only_dates is None or prev[0] in only_dates:
                            totals[prev[0]] = totals.get(prev[0], 0) - prev[1]
                    if only_dates is None or date in only_dates:
                        totals[date] = totals.get(date, 0) + tokens
                    seen_messages[msg_id] = (date, tokens)
        except (OSError, PermissionError):
            continue

    return totals


def tokens_in_last_n_days(
    n: int = 7,
    today_utc: str | None = None,
    projects_dir: Path | None = None,
) -> tuple[int, dict[str, int]]:
    """
    Returns (total_tokens, per_day_dict) for the rolling N-day window
    ending today_utc (inclusive). Caller controls 'today' for testability.
    """
    if today_utc is None:
        today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    target_dates = _last_n_dates(today_utc, n)
    per_day = aggregate_tokens_by_day(projects_dir, only_dates=set(target_dates))
    total = sum(per_day.get(d, 0) for d in target_dates)
    # Backfill zero for any missing day so downstream knows we measured it.
    for d in target_dates:
        per_day.setdefault(d, 0)
    return total, per_day


def _last_n_dates(today_utc: str, n: int) -> list[str]:
    today = datetime.strptime(today_utc, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return [
        (today - _timedelta_days(i)).strftime("%Y-%m-%d") for i in range(n - 1, -1, -1)
    ]


def _timedelta_days(days: int):
    # Tiny wrapper so tests can monkeypatch without importing datetime.timedelta.
    from datetime import timedelta

    return timedelta(days=days)


# ---------------------------------------------------------------------------
# Diagnostic CLI
# ---------------------------------------------------------------------------


def _cli(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] in {"-h", "--help"}:
        sys.stderr.write(
            "Usage:\n"
            "  python3 lib.py usage         # print raw usage snapshot JSON\n"
            "  python3 lib.py tokens [N]    # sum tokens in last N (default 7) days\n"
            "  python3 lib.py token         # print a fresh access token\n"
        )
        return 1
    cmd = argv[1]
    if cmd == "usage":
        print(json.dumps(fetch_usage_snapshot(), indent=2))
        return 0
    if cmd == "tokens":
        n = int(argv[2]) if len(argv) > 2 else 7
        total, per_day = tokens_in_last_n_days(n)
        out = {"total_tokens": total, "per_day": per_day, "window_days": n}
        print(json.dumps(out, indent=2))
        return 0
    if cmd == "token":
        print(get_fresh_access_token())
        return 0
    sys.stderr.write(f"unknown command: {cmd}\n")
    return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv))
