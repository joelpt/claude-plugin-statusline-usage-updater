"""Shared cost-weighting for the %w statusline figure. ZERO imports — must never
fail to import (both the live helper and the nightly calibrator depend on it,
and they MUST use an identical definition or the %w scale breaks).

Why weight at all: the API's 7-day `seven_day.used_percentage` (what %w is
calibrated against) is cost-weighted — output costs ~5x input, a cache-read
costs 0.1x, a 1h cache-write costs 2x. A flat token count is therefore a poor
proxy: two sessions with equal raw tokens but different input/output/cache mix
consume very different quota. Weighting each usage component by its real price
multiplier (and by the model's base input rate) yields a figure proportional to
actual cost, which tracks the utilisation % far more faithfully across mixes.

The unit is integer micro-USD (`base_$/MTok × weighted_tokens` == $ × 1e6). The
absolute scale is irrelevant — the live numerator and the 7-day calibration
denominator share it, so any constant factor (incl. the ~30% of spend in
auxiliary calls that never reach transcripts) cancels in the coefficient.
"""

# Base input price, $/MTok, per model id prefix. Output is 5x input for every
# current model, so it isn't tabled separately (see weighted_cost_units).
_BASE_USD_PER_MTOK = {
    "claude-opus-4-8": 5.0, "claude-opus-4-7": 5.0, "claude-opus-4-6": 5.0,
    "claude-opus-4-5": 5.0, "claude-opus-4-1": 15.0, "claude-opus-4-0": 15.0,
    "claude-sonnet-4-6": 3.0, "claude-sonnet-4-5": 3.0, "claude-sonnet-4": 3.0,
    "claude-haiku-4-5": 1.0, "claude-haiku-3-5": 0.8,
    "claude-fable-5": 10.0, "claude-mythos-5": 10.0,
}
_DEFAULT_BASE = 5.0  # unknown model → assume Opus-tier (conservative, not zero)


def base_rate(model):
    """Base input $/MTok for a model id (longest-prefix match)."""
    if model:
        best = None
        for k, v in _BASE_USD_PER_MTOK.items():
            if model.startswith(k) and (best is None or len(k) > len(best[0])):
                best = (k, v)
        if best:
            return best[1]
    return _DEFAULT_BASE


def fresh_token_units(usage):
    """"Fresh" tokens for one API call: input + output + cache_creation.

    Deliberately EXCLUDES cache_read_input_tokens. In a long session, every
    turn re-reads the full accumulated context as a cache read — summing that
    across turns balloons into the tens of millions and mostly reflects the
    same content re-processed repeatedly, not new work. Excluding it gives a
    number that tracks actual session output/effort instead of turn count.

    Unlike weighted_cost_units, this is NOT cost-weighted — every token counts
    once regardless of type. Used for the statusline's plain token-count display
    (as opposed to the %w quota-burn figure, which needs the cost weighting and
    DOES include cache reads, since they still cost money).
    """
    if not usage:
        return 0
    cc = usage.get("cache_creation") or {}
    if cc:
        cache_creation = int(cc.get("ephemeral_1h_input_tokens") or 0) + int(
            cc.get("ephemeral_5m_input_tokens") or 0
        )
    else:
        cache_creation = int(usage.get("cache_creation_input_tokens") or 0)
    return (
        int(usage.get("input_tokens") or 0)
        + int(usage.get("output_tokens") or 0)
        + cache_creation
    )


def weighted_cost_units(usage, model):
    """Integer micro-USD for one API call's usage.

    = base_rate(model) × ( input + 0.1·cache_read + 1.25·5m_write
                           + 2·1h_write + 5·output )

    Cache writes are split by ephemeral tier when the transcript records it
    (`usage.cache_creation.ephemeral_{1h,5m}_input_tokens`); otherwise the flat
    `cache_creation_input_tokens` is treated as a 5-minute write.
    """
    if not usage:
        return 0
    cc = usage.get("cache_creation") or {}
    w1h = int(cc.get("ephemeral_1h_input_tokens") or 0)
    w5m = int(cc.get("ephemeral_5m_input_tokens") or 0)
    if not cc:
        w5m = int(usage.get("cache_creation_input_tokens") or 0)
    weighted = (
        int(usage.get("input_tokens") or 0)
        + 0.1 * int(usage.get("cache_read_input_tokens") or 0)
        + 1.25 * w5m
        + 2.0 * w1h
        + 5.0 * int(usage.get("output_tokens") or 0)
    )
    return int(round(base_rate(model) * weighted))
