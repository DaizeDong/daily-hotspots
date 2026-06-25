# Step 6 — Daily schedule + digest cron

## Scheduler = OS Task Scheduler, NOT in-session CronCreate

The in-session `CronCreate` tool dies when the session ends — wrong primitive. Use the **Windows
Task Scheduler** at an off-:00 minute (08:07) → the headless wrapper `scripts/wrapper.ps1` →
`claude -p "<run daily-hotspots>" --dangerously-skip-permissions`.

```
powershell -ExecutionPolicy Bypass -File scripts/register-task.ps1 \
  [-ConfigDir C:\path\to\daily-hotspots-config] [-Time 08:07] [-Python C:\path\python.exe]
```

The wrapper mirrors `refresh-market-intel`: **absolute python/claude paths** (Task Scheduler PATH is
minimal — a bare `python`/`git` half-runs and silently fails), fail-fast preflight, and
notify-on-abort (a Discord alert via the relay on any non-zero exit). It sets `SCHEDULE_DB_PATH` to a
local-NTFS ledger path (never OneDrive/network).

## Base due/tick integration (A + B)

- **A — digest trigger**: the digest is an idempotent `schedule-reminder` item
  (`idempotency_key=daily-hotspots:digest:<date>`, `digest.py:register_digest_item`); a re-run /
  catch-up never double-sends.
- **B — follow-up todos**: high-score opportunities the user should act on can be added as base
  `task` items (`--ext x_daily_hotspots_*`), optionally `depends-on` a market-intel deep-dive item.

Delivery stays decoupled: the digest is delivered by **this skill's own relay**, NOT by the base's
tick→its-own-Discord-relay (channel + card format differ). The base is only a state store
(due/list read-only).

## Hook into an existing daily summary

This skill exposes a "今日商业机会总结" block. If a daily fixed-time summary routine exists, fold
that block into it (don't stand up a competing channel for it).

## At-least-once + dedupe on oversleep

If the machine was asleep, the next run uses `since = last_run_at - 5min` and the fingerprint UPSERT
makes catch-up safe (no double-push). `SCHEDULE_NOW` / `DAILY_HOTSPOTS_NOW` inject a clock for
catch-up replay and tests.
