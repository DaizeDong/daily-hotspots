# Step 6, Daily schedule + digest cron

## Scheduler = OS Task Scheduler, NOT in-session CronCreate

The in-session `CronCreate` tool dies when the session ends, wrong primitive. Use the **Windows
Task Scheduler** at an off-:00 minute (08:07) → the headless wrapper `scripts/wrapper.ps1` →
`claude -p "<run daily-hotspots>" --dangerously-skip-permissions`.

```
powershell -ExecutionPolicy Bypass -File scripts/register-task.ps1 \
  [-ConfigDir C:\path\to\daily-hotspots-config] [-Time 08:07] [-Python C:\path\python.exe]
```

The wrapper mirrors `refresh-market-intel`: **absolute python/claude paths** (Task Scheduler PATH is
minimal, a bare `python`/`git` half-runs and silently fails), fail-fast preflight, and
notify-on-abort (a Discord alert via the relay on any non-zero exit). It sets `SCHEDULE_DB_PATH` to a
local-NTFS ledger path (never OneDrive/network). The daily prompt tells the skill to run the roster
loop + community lanes and **write the pulls-log denominator via `run.py --sources`** (spec §6), that
per-run write is what the weekly yield pass below replays.

## Weekly self-evolve yield pass, `DailyHotspotsYield` (spec §8/§9)

`register-task.ps1` **also registers a WEEKLY task** `DailyHotspotsYield` (default Monday 08:37) →
`scripts/yield-wrapper.ps1`. It is the self-evolve loop that keeps the X KOL roster honest, and
without it the engine is **inert** (yield stays `unknown`, auto-prune never fires):

- **What it does**: REPLAYS the archive, numerator = origin-tagged archived cards
  (`opportunities.jsonl`), denominator = the daily pulls-log (`pulls-YYYY-MM.jsonl`), over a rolling
  30-day window, then **auto-prunes** dead handles (reversible `enabled=false`) and writes a
  **propose-add** review queue (`archive/roster-review.md`, human-approved). See
  `reference/roster-evolution.md`.
- **No LLM**: the yield pass is a pure deterministic replay (`yield.py`), so `yield-wrapper.ps1` calls
  `python scripts/run.py --yield --apply --write-review` DIRECTLY, cheapest + most robust (no
  `claude -p`). Auto-prune is safe: `enabled=false` (never a delete, un-prune from the review queue),
  a no-op until 7 days of real history (cold-start), every prune logged with reason + stats. Pass
  `-YieldReportOnly` to `register-task.ps1` to have the weekly pass report-only (no prune).
- **Cadence dedup**: the pass is idempotent per ISO week (spec's `daily-hotspots:yield:<week>` item),
  so a re-run / catch-up cannot double-apply.

## Monthly identity sweep, `DailyHotspotsIdentitySweep` (§9 guardrail 4)

Handle drift (`cygaar_dev`→`0xCygaar`) and dead accounts (`statusesCount:0`, 404/suspended) need a
`get_user_info` lookup over the roster. The producer is **`scripts/identity_sweep.py`**, a pure REST
caller over twitterapi.io (`GET /twitter/user/info`, `X-API-Key` from the companion-config secret),
**no MCP, no LLM, deterministic**. It was the one missing wire: `flag_drift_and_dead` + the
`run.py --yield --user-info` ingest were already built + tested; nothing GENERATED the sweep.

- **What it does**: sweeps every ENABLED handle → writes `archive/identity-sweep-YYYY-MM.json`
  (`{handle: <user data>|null}`), then (`--feed-yield`) runs `run.py --yield --user-info <sweep>
  --write-review` (report-only) so drift/dead land in the **flagged accounts** section of
  `archive/roster-review.md` (flagged only, **never auto-removed**, a rename is a human edit).
  A transient network error RAISES (fails loud) rather than silently marking a live account dead.
- **Cadence**: registered as MONTHLY task `DailyHotspotsIdentitySweep` (day 1 @ 11:00, after the daily
  run's window) → `scripts/identity-sweep-wrapper.ps1` (absolute python path, Discord notify-on-abort,
  native-call under `Continue` per the wrapper.ps1 stderr lesson). Registered out-of-band (not by
  `register-task.ps1`) so it never re-touches the daily task's `ExecutionTimeLimit`.
- **Manual run**: `python scripts/identity_sweep.py --feed-yield` (respects `DAILY_HOTSPOTS_CONFIG`).

## Base due/tick integration (A + B)

- **A, digest trigger**: the digest is an idempotent `schedule-reminder` item
  (`idempotency_key=daily-hotspots:digest:<date>`, `digest.py:register_digest_item`); a re-run /
  catch-up never double-sends.
- **B, follow-up todos**: high-score opportunities the user should act on can be added as base
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
