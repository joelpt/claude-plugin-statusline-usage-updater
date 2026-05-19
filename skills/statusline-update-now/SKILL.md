---
name: statusline-update-now
description: Trigger an immediate recomputation of the statusline %w coefficient — useful for testing after install, after the user changes statusline paths, or to debug a stale value. Use when the user says "update the statusline coefficient now" or "/statusline-update".
---

# Statusline Usage Updater — Run Now

Fire the nightly job immediately and report the result.

## Steps

1. **Run the updater:**
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/bin/update_coefficient.py"
   ```

   Or, if launchd is installed:
   ```bash
   launchctl start com.joelpt.statusline-usage-updater
   ```
   (Asynchronous — check the log file for results.)

2. **Inspect the result:**
   ```bash
   cat ~/.claude/statusline-usage-updater/coefficient.json
   ```

   Report to the user:
   - The current coefficient (formatted as `1.15e-08`).
   - Sample count out of 7 (more is more stable).
   - Min/max across samples (wide spread = inconsistent usage pattern, narrow = well-calibrated).

3. **If a target statusline was patched, sanity-check:**
   ```bash
   grep 'STATUSLINE_USAGE_PCT_PER_TOKEN' "$STATUSLINE_PATH"
   ```

   The value should match `coefficient.json`.

4. **If errors:** check the log at `~/.claude/statusline-usage-updater/log/$(date +%Y-%m).log`. Common issues:

   - `Claude Code credentials not found` → user has not logged in via Claude Code, or `~/.claude/.credentials.json` was moved.
   - `refresh rejected` → OAuth refresh token is invalid; deleting `~/.claude/statusline-usage-updater/auth-cache.json` forces re-adoption from Claude Code's file.
   - `no tokens recorded` → `~/.claude/projects/` is empty (fresh user) or the last 7 days had no sessions.
   - `no anchor line in <file>` → the target statusline doesn't contain `STATUSLINE_USAGE_PCT_PER_TOKEN=…` yet; run `/statusline-setup` to inject it.
