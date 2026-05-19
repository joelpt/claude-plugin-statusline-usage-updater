#!/usr/bin/env python3
"""
Nightly job: recompute the statusline %w coefficient from realised usage.

What it does, in order:
  1. Take a single coincident reading: (seven_day utilisation%, sum of tokens
     across all sessions in the last 7 days). Their ratio is the
     pct-per-token coefficient that exactly reproduces today's bar.
  2. Append it to a rolling 7-sample log under ~/.claude/statusline-usage-updater/.
  3. Average those samples (mean) → working coefficient. Note: consecutive
     daily readings share 6 days of underlying data, so the 7 samples are
     correlated — this is a smoother, not 7 independent measurements.
  4. Write coefficient.json (machine-readable).
  5. For every target statusline file we've been told to update, do a regex
     replace on the anchor line.

Designed to be safe to run repeatedly: same day overwrites that day's sample;
no anchor line in a target file → that file is skipped silently with a log line.

Run modes:
  --once          single run (default; what launchd calls)
  --dry-run       compute and log, but skip writing samples/coefficient/targets
  --print         compute and print only; no side effects
  --targets-from  path to a file listing one statusline path per line (defaults
                  to STATE_DIR/targets.txt)

Exit codes: 0 success, 1 transient error (will retry tomorrow), 2 config error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

# Allow running as a script (launchd will exec this directly).
sys.path.insert(0, str(Path(__file__).parent))
import lib  # noqa: E402


ROLLING_WINDOW_DAYS = 7
MAX_SAMPLES = 7
COEFFICIENT_ANCHOR_RE = re.compile(
    r"^(\s*STATUSLINE_USAGE_PCT_PER_TOKEN\s*=\s*).+?(\s*(?:#.*)?)$",
    re.MULTILINE,
)


def _log(msg: str, log_path: Path | None = None) -> None:
    line = f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}Z] {msg}"
    print(line, file=sys.stderr)
    if log_path is not None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def _take_lock() -> bool:
    lib.STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        # O_EXCL → fails atomically if lock exists.
        fd = os.open(lib.LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(fd, f"{os.getpid()} {time.time()}\n".encode())
        os.close(fd)
        return True
    except FileExistsError:
        # Stale lock (>1h old) → take it over.
        try:
            mtime = lib.LOCK_FILE.stat().st_mtime
            if time.time() - mtime > 3600:
                lib.LOCK_FILE.unlink()
                return _take_lock()
        except OSError:
            pass
        return False


def _release_lock() -> None:
    try:
        lib.LOCK_FILE.unlink()
    except FileNotFoundError:
        pass


def _load_samples() -> list[dict]:
    if not lib.SAMPLES_FILE.exists():
        return []
    try:
        blob = json.loads(lib.SAMPLES_FILE.read_text())
        return blob.get("samples", [])
    except (json.JSONDecodeError, OSError):
        return []


def _save_samples(samples: list[dict]) -> None:
    lib.STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = lib.SAMPLES_FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"samples": samples}, indent=2, sort_keys=True)
    )
    tmp.replace(lib.SAMPLES_FILE)


def _save_coefficient(coef: float, samples: list[dict]) -> None:
    lib.STATE_DIR.mkdir(parents=True, exist_ok=True)
    coefs = [s["coefficient"] for s in samples if s.get("coefficient")]
    blob = {
        "coefficient": coef,
        "computed_at": datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z",
        "sample_count": len(coefs),
        "samples_min": min(coefs) if coefs else None,
        "samples_max": max(coefs) if coefs else None,
    }
    tmp = lib.COEFFICIENT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(blob, indent=2, sort_keys=True))
    tmp.replace(lib.COEFFICIENT_FILE)


def _load_targets(override_path: Path | None) -> list[Path]:
    path = override_path or (lib.STATE_DIR / "targets.txt")
    if not path.exists():
        return []
    targets: list[Path] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Expand ~ and env vars.
        targets.append(Path(os.path.expandvars(os.path.expanduser(line))))
    return targets


def _patch_target(target: Path, coefficient: float, log_path: Path) -> bool:
    """Regex-replace the anchor line. Returns True if patched, False if anchor missing."""
    if not target.exists():
        _log(f"target missing: {target}", log_path)
        return False
    try:
        original = target.read_text()
    except OSError as e:
        _log(f"can't read {target}: {e}", log_path)
        return False

    new_value = f"{coefficient:.6e}"
    replacement = rf"\g<1>{new_value}\g<2>"
    patched, n = COEFFICIENT_ANCHOR_RE.subn(replacement, original)
    if n == 0:
        _log(f"no anchor line in {target} (looking for STATUSLINE_USAGE_PCT_PER_TOKEN=…)", log_path)
        return False
    if patched == original:
        _log(f"coefficient already {new_value} in {target}", log_path)
        return True

    tmp = target.with_suffix(target.suffix + ".usage-updater.tmp")
    tmp.write_text(patched)
    # Preserve executable bit etc.
    try:
        st = target.stat()
        os.chmod(tmp, st.st_mode)
    except OSError:
        pass
    tmp.replace(target)
    _log(f"updated {target} → coefficient={new_value}", log_path)
    return True


def compute_today_sample(today_utc: str | None = None) -> dict:
    """Take one coincident (utilisation, tokens) reading and return a sample dict."""
    if today_utc is None:
        today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    snapshot = lib.fetch_usage_snapshot()
    util = lib.seven_day_utilization_pct(snapshot)
    if util is None:
        raise RuntimeError("seven_day utilisation missing from usage response")
    total_tokens, per_day = lib.tokens_in_last_n_days(
        n=ROLLING_WINDOW_DAYS, today_utc=today_utc
    )
    if total_tokens <= 0:
        raise RuntimeError(
            f"no tokens recorded in last {ROLLING_WINDOW_DAYS} days — "
            "is ~/.claude/projects/ empty?"
        )
    coef = util / total_tokens  # percent per token
    return {
        "date": today_utc,
        "utilization_pct": util,
        "tokens_7d": total_tokens,
        "coefficient": coef,
        "per_day_tokens": per_day,
    }


def merge_sample(samples: list[dict], new_sample: dict) -> list[dict]:
    """Append/overwrite by date; trim to MAX_SAMPLES (most recent)."""
    by_date = {s["date"]: s for s in samples}
    by_date[new_sample["date"]] = new_sample
    ordered = sorted(by_date.values(), key=lambda s: s["date"])
    return ordered[-MAX_SAMPLES:]


def average_coefficient(samples: list[dict]) -> float:
    """Mean of per-sample coefficients. Returns BOOTSTRAP if no samples."""
    coefs = [s["coefficient"] for s in samples if s.get("coefficient")]
    if not coefs:
        return lib.BOOTSTRAP_COEFFICIENT
    return sum(coefs) / len(coefs)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--once", action="store_true", help="single run (default)")
    ap.add_argument("--dry-run", action="store_true", help="compute but don't persist or patch")
    ap.add_argument("--print", action="store_true", dest="just_print", help="print result and exit")
    ap.add_argument("--targets-from", type=Path, default=None)
    args = ap.parse_args(argv[1:])

    log_path = lib.LOG_DIR / f"{datetime.now(timezone.utc).strftime('%Y-%m')}.log"

    if not _take_lock():
        _log("another updater run is in progress; skipping", log_path)
        return 1
    try:
        try:
            sample = compute_today_sample()
        except Exception as e:
            _log(f"compute_today_sample failed: {e!r}", log_path)
            _log(traceback.format_exc(), log_path)
            return 1
        _log(
            f"sample date={sample['date']} util={sample['utilization_pct']:.2f}% "
            f"tokens_7d={sample['tokens_7d']:,} coef={sample['coefficient']:.6e}",
            log_path,
        )

        samples = _load_samples()
        new_samples = merge_sample(samples, sample)
        averaged = average_coefficient(new_samples)
        _log(
            f"averaged coefficient over {len(new_samples)} sample(s): {averaged:.6e}",
            log_path,
        )

        if args.just_print:
            print(json.dumps({"sample": sample, "coefficient": averaged}, indent=2, default=str))
            return 0
        if args.dry_run:
            _log("--dry-run: skipping persistence and target patching", log_path)
            return 0

        _save_samples(new_samples)
        _save_coefficient(averaged, new_samples)

        targets = _load_targets(args.targets_from)
        if not targets:
            _log("no targets configured; coefficient saved but no statusline patched", log_path)
        for t in targets:
            _patch_target(t, averaged, log_path)
        return 0
    finally:
        _release_lock()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
