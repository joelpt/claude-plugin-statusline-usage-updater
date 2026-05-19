# statusline-usage-updater

Auto-calibrates the `#.##%w` (weekly-quota burn) figure in your Claude Code statusline against your **realised** rate-limit utilisation and token volume — no plan-specific guesswork, no static coefficient pulled from internet anecdotes.

Once a day, a `launchd` job:

1. Reads Anthropic's `/api/oauth/usage` endpoint (reusing your Claude Code OAuth credentials — no separate auth setup).
2. Sums every billable token in `~/.claude/projects/**.jsonl` over the last 7 days.
3. Divides: `seven_day_utilisation% ÷ tokens_7d = percent-of-weekly-quota per token`.
4. Adds today's reading to a rolling 7-sample log.
5. Averages the samples and regex-replaces the `STATUSLINE_USAGE_PCT_PER_TOKEN=…` line in your statusline script.

The plugin **does not need to know what plan you're on** — Max 5x, Max 20x, Pro, Team — and it self-corrects to your personal usage mix (peak-hour heavy, Opus-heavy, etc.). After ~7 days of normal use it converges to within a few percent of the live `7d:` figure your bar already shows.

## What you get in your statusline

```
opus[1m] 4.7 high | ctx:120k/1m 1h:24% 1w:54% 0.62%w | ~/code/foo  main
                                          ^^^^^^
                                          this — auto-calibrated daily
```

When Anthropic re-introduces peak-hour throttling and you set `STATUSLINE_PEAK_HOURS_PT="5-11"`, you'll see a `⚠` prefix during weekday 5–11 AM PT:

```
… 1w:54% ⚠0.62%w | …
```

## Install

```bash
# 1. Enable the plugin
claude plugin install statusline-usage-updater@joelpt-claude-plugins

# 2. From inside Claude Code, run the setup skill
/statusline-setup
```

The `/statusline-setup` skill is interactive — it locates your statusline, shows you the proposed edits, and asks before each change. It also offers a menu of optional features (peak warning, context meter, rate-limit timers, etc.).

To install manually instead:

```bash
PLUGIN_ROOT="$HOME/code/claude-plugins/plugins/statusline-usage-updater"

# Install the launchd job (daily 03:17 local time)
"$PLUGIN_ROOT/launchd/install.sh"

# Tell the updater which statusline file(s) to patch
echo "$HOME/.claude/statusline-command.sh" >> ~/.claude/statusline-usage-updater/targets.txt

# Trigger the first run
launchctl start com.joelpt.statusline-usage-updater

# Read back the result
cat ~/.claude/statusline-usage-updater/coefficient.json
```

You still need to add the three anchor lines to your statusline by hand if you skip `/statusline-setup` — see the skill body for the exact text.

## Authentication

The plugin reads `~/.claude/.credentials.json` (Claude Code's own credentials file) **read-only** and caches refreshed access tokens under `~/.claude/statusline-usage-updater/auth-cache.json` to avoid racing Claude Code itself. If the OAuth refresh token in our cache ever gets rejected, we re-adopt from Claude Code's file and refresh once more. You should never need to touch tokens manually.

If you're not logged into Claude Code, the updater errors with `Claude Code credentials not found at /Users/.../.claude/.credentials.json`. Run `claude` once to log in.

## How the coefficient is computed (math)

Per day:

```
util  = GET /api/oauth/usage  → seven_day.utilization (0–100%)
tokens = Σ over the last 7 days, dedup by message.id:
         input + cache_creation + cache_read + output
coef   = util / tokens          # percent-of-weekly-quota per token
```

We append today's `coef` to a 7-entry rolling list and take the mean. Note that consecutive daily readings share 6 days of underlying data — they're **not** seven independent observations. The rolling mean is a smoother that survives a single bad usage-endpoint reading; don't oversell it as a 7× confidence boost.

**Token definition.** The coefficient absorbs whatever mix of cheap (cache-read) and expensive (cache-creation, output) tokens you actually use. Resist the urge to weight cache reads at 0.1 — the whole point of calibrating against realised consumption is letting your own pattern set the rate.

## Files written

```
~/.claude/statusline-usage-updater/
├── auth-cache.json           # refreshed OAuth tokens (mode 600)
├── samples.json              # rolling 7-day samples
├── coefficient.json          # current averaged coefficient + metadata
├── targets.txt               # one statusline path per line
├── update.lock               # prevents overlapping runs
├── cache/                    # session-token sidecar caches (mtime-keyed)
└── log/
    ├── YYYY-MM.log           # appendable log from update_coefficient.py
    └── launchd.{stdout,stderr}.log
```

`~/Library/LaunchAgents/com.joelpt.statusline-usage-updater.plist` — the daily-3:17am-AM job.

## Manual operations

| Task | Command |
|---|---|
| Recompute now | `launchctl start com.joelpt.statusline-usage-updater` |
| Synchronous run (with output) | `python3 $PLUGIN_ROOT/bin/update_coefficient.py` |
| Inspect current coefficient | `cat ~/.claude/statusline-usage-updater/coefficient.json` |
| Inspect raw usage snapshot | `python3 $PLUGIN_ROOT/bin/lib.py usage` |
| Inspect last-7d tokens | `python3 $PLUGIN_ROOT/bin/lib.py tokens 7` |
| Watch the daily log | `tail -f ~/.claude/statusline-usage-updater/log/$(date +%Y-%m).log` |
| Run tests | `cd $PLUGIN_ROOT && python3 -m unittest discover tests/` |
| Uninstall the launchd job | `$PLUGIN_ROOT/launchd/install.sh --uninstall` |

## Peak hours

Anthropic's documented peak window for Claude Code 5-hour limits (when throttling was active, March 2026):

- **Times:** 5–11 AM PT (8 AM–2 PM ET, 12–6 PM GMT)
- **Days:** weekdays only
- **Status as of May 2026:** removed for Pro/Max plans

The setup skill does a WebSearch at install time to find the *current* policy and persists it to `~/.claude/statusline-usage-updater/peak-hours.json` (schema: `{checked_at, enforced, pt_range, weekdays_only, summary, sources[]}`). The `STATUSLINE_PEAK_HOURS_PT` anchor is set from that finding.

If Anthropic announces a change, re-run `/statusline-setup` — the skill repeats the search and rewrites the JSON + anchor. The nightly job intentionally does NOT touch peak hours; that policy changes too rarely to spend an LLM call every day.

Range is `start-end` in Pacific Time (24h, inclusive start, exclusive end). The check converts your local clock to PT via `TZ=America/Los_Angeles`, so it works correctly regardless of your machine's timezone.

## Troubleshooting

- **`%w` shows nothing on the bar.** The session-token helper couldn't read the transcript, the coefficient anchor is missing, or `session_tokens` came back as zero. Run `python3 $PLUGIN_ROOT/bin/session_tokens.py "$(jq -r .transcript_path < ~/.claude/statusline-last-input.json)"` — should print an integer.
- **Coefficient stuck on bootstrap value.** Check `~/.claude/statusline-usage-updater/log/YYYY-MM.log`. Most common cause: launchd job didn't fire. Try `launchctl print gui/$(id -u)/com.joelpt.statusline-usage-updater`.
- **`refresh rejected`.** Delete `~/.claude/statusline-usage-updater/auth-cache.json` and retry — that forces re-adoption from Claude Code's credentials.
- **`%w` drifts wildly between sessions on the same day.** Expected for the first ~7 days while samples accumulate. After that, drift > 30% from `1w:` value suggests a usage-pattern shift (e.g. you switched from Sonnet to Opus); give it another week.

## Design notes

- **Why not just use `.cost.total_cost_usd` × some-budget?** That requires knowing your plan price; misses the realised Opus/Sonnet/Haiku mix that drives Anthropic's actual rate-limit accounting; and gives misleadingly stable answers when usage patterns shift. Token-based calibration moves with your behaviour.
- **Why a separate auth cache?** Claude Code rotates `~/.claude/.credentials.json` continuously. Writing to it would race; reading it and caching our own refresh is what the menubar app does too.
- **Why mtime-cache the session-token reads?** The statusline runs on every prompt refresh; multi-MB transcript reads would lag the bar. The sidecar cache costs ~1 stat per refresh — invisible.
- **Why dedup by message ID?** A single assistant response is serialised across multiple JSONL `type:"assistant"` entries (one per content block), each carrying a fresh `output_tokens` count. Only the last is accurate; earlier ones are stale lower-bounds. Without dedup we'd 3–10× over-count. (See `analyze-sessions.mjs` in the official `session-report` skill — same trap, same fix.)

## License

Private. See repo root.
