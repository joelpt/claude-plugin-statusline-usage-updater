---
name: statusline-setup
description: Interactive first-time setup for the statusline-usage-updater plugin — analyzes the user's statusline, inserts the auto-calibrated %w anchor, installs the nightly launchd job, and walks through optional features. Use when the user invokes /statusline-setup or asks how to install / configure / wire up the usage-updater plugin.
---

# Statusline Usage Updater — Setup

Walk the user through a clean first-time install. They end with:

1. Three anchor variables added to their statusline script — `STATUSLINE_USAGE_PCT_PER_TOKEN`, `STATUSLINE_PEAK_HOURS_PT`, `STATUSLINE_PEAK_WARN_SYMBOL`.
2. The `#.##%w` display block in their statusline reading `session_tokens × coefficient`, with a peak-hour ⚠ prefix when applicable.
3. A daily `launchd` job that recomputes the coefficient and patches the anchor.
4. (Optional) extra statusline features they liked from the feature menu.

This is an **interactive** skill: ask the user before each edit. Confirm anything before writing.

---

## Step 0 — Research current peak hours (web search)

Anthropic adjusts Claude Code's peak-hour policy from time to time.
Use the **WebSearch** tool to find out what's in effect right now, then persist the finding locally so the rest of the skill (and future re-runs) can rely on it.

Search with a current-year-anchored query:

```text
"Claude Code" peak hours rate limit weekly cap <CURRENT_YEAR>
```

Read 2–3 results.
What you're looking for:

1. **Are peak hours currently enforced?** (As of May 2026 the answer for Pro/Max was *no* — Anthropic removed peak-hour throttling. That can change again.)
2. **If yes: the exact window.** Pacific Time, days of week, start/end hours. The historical spec was `5–11 AM PT, weekdays only`.

Write the finding to `~/.claude/statusline-usage-updater/peak-hours.json`.
Use the Write tool.
Schema:

```json
{
  "checked_at": "YYYY-MM-DDTHH:MM:SSZ",
  "enforced": true,
  "pt_range": "5-11",
  "weekdays_only": true,
  "summary": "one-line human-readable explanation",
  "sources": [
    "https://...",
    "https://..."
  ]
}
```

If `enforced: false`, set `pt_range: ""` — that's the anchor the statusline will pick up, with empty meaning "no warning glyph".

If the search is inconclusive or returns conflicting answers, ask the user which result they trust before writing.
Don't guess.

Show the user the persisted file before continuing — they should see what's about to drive the `STATUSLINE_PEAK_HOURS_PT` anchor.

---

## Step 1 — Find their statusline

Locate the user's statusline command. In settings.json look for `statusLine.command` (top-level or under `experimental`). Common paths:

- `~/.claude/statusline-command.sh` (most common — Joel's setup)
- `~/.claude/statusline.sh`
- A custom path in `settings.json#statusLine.command`

Read the file. Report what you see to the user before changing anything.

If they don't have one yet, propose creating one from the reference at `${CLAUDE_PLUGIN_ROOT}/templates/minimal-statusline.sh` (if absent, fall back to walking them through the smallest viable script — `printf '%s' "$input" | jq -r '.model.display_name'` is enough to start).

---

## Step 2 — Pick an insert point for `#.##%w`

Goal: the bar should read something like
```
opus[1m] 4.7 high | ctx:120k/1m 1h:24% 1w:54% 0.62%w | ~/dir main
```

The `%w` slot belongs alongside the rate-limit info (`1h:`, `1w:`) — it's the same conceptual family (how much of weekly quota is this session burning). Suggest inserting it **after** the 7-day-window display, separated by a space.

If their statusline already shows some form of `%w` (cost-based or otherwise), propose **replacing** that block with the token-based variant rather than adding a second one.

Show the user the proposed diff before editing. Use the Edit tool.

---

## Step 3 — Inject the three anchor variables

Add this block near the top of their statusline script (after any `set -…` lines, before main logic). The exact spelling on the three anchor lines matters — `bin/update_coefficient.py` regex-matches `^STATUSLINE_USAGE_PCT_PER_TOKEN\s*=`.

```bash
# Auto-calibrated by statusline-usage-updater. Do not hand-edit the values.
STATUSLINE_USAGE_PCT_PER_TOKEN=1.0e-08
STATUSLINE_PEAK_HOURS_PT=""
STATUSLINE_PEAK_WARN_SYMBOL="⚠"
STATUSLINE_SESSION_TOKENS_BIN="${CLAUDE_PLUGIN_ROOT}/bin/session_tokens.py"
```

Set `STATUSLINE_PEAK_HOURS_PT` from the `pt_range` field of the `~/.claude/statusline-usage-updater/peak-hours.json` you wrote in Step 0.
If the research said peak hours are not currently enforced, leave it as `""`.

Note the `${CLAUDE_PLUGIN_ROOT}` token expansion — at install time, replace it with the absolute path returned by:

```bash
echo "${CLAUDE_PLUGIN_ROOT:-$HOME/code/claude-plugins/plugins/statusline-usage-updater}"
```

---

## Step 4 — Replace (or insert) the `%w` computation block

Reference implementation (matches Joel's local statusline; copy with confirmation):

```bash
transcript_path=$(printf '%s' "$input" | jq -r '.transcript_path // empty')
session_tokens=""
if [[ -n "$transcript_path" && -x "$STATUSLINE_SESSION_TOKENS_BIN" ]]; then
    session_tokens=$("$STATUSLINE_SESSION_TOKENS_BIN" "$transcript_path" 2>/dev/null)
fi

cost_info=""
if [[ -n "$session_tokens" && "$session_tokens" =~ ^[0-9]+$ && "$session_tokens" -gt 0 ]]; then
    pct=$(awk -v t="$session_tokens" -v c="$STATUSLINE_USAGE_PCT_PER_TOKEN" \
            'BEGIN{printf "%.6f", t*c}')
    pct_fmt=$(awk -v p="$pct" 'BEGIN{
        if      (p < 10)  printf "%.2f", p
        else if (p < 100) printf "%.1f", p
        else              printf "%d",   p
    }')
    cost_color=""
    if   awk -v p="$pct" 'BEGIN{exit !(p >= 50)}'; then cost_color="$ORANGE"
    elif awk -v p="$pct" 'BEGIN{exit !(p >= 25)}'; then cost_color="$YELLOW"
    fi

    peak_prefix=""
    if [[ -n "$STATUSLINE_PEAK_HOURS_PT" ]]; then
        pt_dow=$(TZ=America/Los_Angeles date +%u 2>/dev/null || echo 0)
        pt_hour=$(TZ=America/Los_Angeles date +%H 2>/dev/null || echo "")
        pt_hour="${pt_hour#0}"
        peak_start="${STATUSLINE_PEAK_HOURS_PT%-*}"
        peak_end="${STATUSLINE_PEAK_HOURS_PT#*-}"
        if [[ "$pt_dow" =~ ^[1-5]$ && "$pt_hour" =~ ^[0-9]+$ \
              && "$peak_start" =~ ^[0-9]+$ && "$peak_end" =~ ^[0-9]+$ \
              && pt_hour -ge peak_start && pt_hour -lt peak_end ]]; then
            peak_prefix="${ORANGE}${STATUSLINE_PEAK_WARN_SYMBOL}${RESET}"
        fi
    fi
    if [[ -n "$cost_color" ]]; then
        cost_info=" ${peak_prefix}${cost_color}${pct_fmt}%w${RESET}"
    else
        cost_info=" ${peak_prefix}${pct_fmt}%w"
    fi
fi
```

Adapt variable names if their script uses different conventions (e.g. `BLUE` instead of `ORANGE`). Preserve their existing color-threshold preferences if they have any.

### Audit recent CC session histories for statusline incidents (do this BEFORE editing)

Run a quick recon over `~/.claude/projects/**.jsonl` for statusline-related pain points the user has hit before. Surface the top 3 incident-derived rules to them inline with the static list below as "you've seen this before". Use Bash + grep:

```bash
# Match Claude-noticed statusline failures: traps, vanishing bars, jq nulls, pipefail kills.
grep -lE '(statusline|statusLine|statusline-command)' \
    ~/.claude/projects/*/*.jsonl 2>/dev/null \
  | head -20 \
  | while read -r f; do
        grep -oE '(statusline[^"]*?(fallback|vanish|crash|pipefail|null|undefined|unset|missing|broken)[^"]{0,80})' "$f" \
          | sort -u | head -3
    done
```

Adjust the regex if the user uses different terminology in their bar (`prompt-line`, `status-bar`, etc.). If nothing turns up, skip this — clean history is a fine state.

### Applied learnings — do NOT skip these

From the audit above and from observing other statusline scripts across `~/.claude/projects/`, watch out for:

- **No `set -e`.** A statusline that vanishes is strictly worse than one missing a field. Use `set -uo pipefail` only, and a top-level `trap` that prints a minimal `model | dir` fallback.
- **No `pipefail` on `git log @{upstream}..`** unless you've verified the branch has upstream tracking. Worktrees and `claude-agents` sessions often don't.
- **Always check the helper exists and is executable** before invoking it — `[[ -x "$STATUSLINE_SESSION_TOKENS_BIN" ]]`. Otherwise a missing/moved plugin silently breaks the bar.
- **Don't read the transcript twice.** The session-token helper already caches by mtime; don't add another shell-side jq pass.
- **Never use the cost+budget fallback at the same time.** Two definitions of `%w` coexisting (cost-based + token-based) confuse the reading.

---

## Step 5 — Install the nightly launchd job

Run the install script. Use the Bash tool; confirm with the user first.

```bash
"${CLAUDE_PLUGIN_ROOT}/launchd/install.sh"
```

This renders the plist template, drops it at `~/Library/LaunchAgents/com.joelpt.statusline-usage-updater.plist`, and loads it. Daily run at 03:17 local time.

To trigger an immediate run for verification:

```bash
launchctl start com.joelpt.statusline-usage-updater
tail -n 50 ~/.claude/statusline-usage-updater/log/$(date +%Y-%m).log
```

---

## Step 6 — Wire the user's statusline path into the updater's target list

The updater patches the anchor only in files listed under `~/.claude/statusline-usage-updater/targets.txt`. Append the user's statusline path:

```bash
mkdir -p ~/.claude/statusline-usage-updater
echo "$STATUSLINE_PATH" >> ~/.claude/statusline-usage-updater/targets.txt
```

Confirm by running:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/bin/update_coefficient.py" --dry-run
```

The output should mention "no anchor" or "updated …" referencing their statusline.

---

## Step 7 — Optional feature menu

Offer the user a short menu of statusline ideas drawn from Joel's reference statusline and other commonly-seen patterns. Use `AskUserQuestion` with `multiSelect: true`. Don't over-design — implement what they pick using their existing `jq`-from-input pattern.

**Suggested options:**

- **Context window meter** — `ctx:120k/1m` with yellow at >120k, orange at >512k. Joel's reference shows `.context_window.current_usage` totals.
- **Rate-limit timers** — `1h:24% 1w:54%` with a green-on-near-reset highlight (≤45 min). Joel's reference does this for both 5h and 7d windows.
- **Effort / fast-mode chips** — `.effort.level` and `.fast_mode` from the input.
- **Output-style chip** — `[explanatory]` when `.output_style.name` is non-default.
- **Git ahead/behind glyphs** — `↱`, `↰`, `⇅` for ahead, behind, both, plus `±` for dirty.
- **Worktree-aware path collapse** — `<project>/⋯/<worktree-name>` when cwd is under `.claude/worktrees/`.
- **Peak-hour ⚠** — wired to the WebSearch finding from Step 0 (`~/.claude/statusline-usage-updater/peak-hours.json`). On for current Anthropic-enforced windows, silent otherwise. Re-run `/statusline-setup` to recheck if Anthropic re-introduces or changes the policy.
- **Cost-USD readout** — raw `$1.98` from `.cost.total_cost_usd`, for users who prefer dollars to %w. (Tradeoff: harder to relate to the weekly quota.)

Use the Read tool on Joel's statusline at `$HOME/.claude/statusline-command.sh` for working copies of any block the user picks.

---

## Step 8 — Verify

Show the user a one-line test:

```bash
cat ~/.claude/statusline-last-input.json | bash "$STATUSLINE_PATH"
```

The output should contain a `#.##%w` near the rate-limit info. **Tell the user this explicitly** (otherwise the new value will look like a regression):

- **Per-session `%w` will look ~10–20× smaller than a cost-based formula gave them.** That's correct, not broken. The coefficient is calibrated so the *sum across a week's sessions* equals the real `1w:` figure their bar shows — any single session is a small slice of that.
- The first 7 days the coefficient will be smoothing toward steady state — early readings may be off by 10–30%. After that, it should track within a few percent of the real `1w:` value.
- To force a refresh: `launchctl start com.joelpt.statusline-usage-updater`.
- To inspect the running coefficient: `cat ~/.claude/statusline-usage-updater/coefficient.json`.
- To uninstall: `"${CLAUDE_PLUGIN_ROOT}/launchd/install.sh" --uninstall`.
