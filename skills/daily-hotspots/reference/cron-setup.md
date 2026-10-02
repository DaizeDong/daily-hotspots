# Step 6, Daily schedule + digest cron

## Scheduler = OS Task Scheduler, NOT in-session CronCreate

The in-session `CronCreate` tool ends with its session. Use Windows Task Scheduler at an
off-:00 minute (08:07), which starts `scripts/wrapper.ps1`. The wrapper invokes
`llmcall.call(prompt, mode="agent")` once and inherits the installed routing, model, timeout and
fallback policy. It does not launch a second provider CLI or replay an uncertain agent run.

```powershell
powershell -ExecutionPolicy Bypass -File scripts/register-task.ps1
```

Optional parameters are `-ConfigDir`, `-Time` and `-Python`. Use paths to the prepared private
companion and executable.

The wrapper resolves absolute Python and Git paths, rejects the WindowsApps Python alias, and
checks the llmcall import through the same isolated shim used for the run. It sets the working
directory to the verified PRIVATE companion and sets `SCHEDULE_DB_PATH` to a local ledger path.
Log destinations must pass PRIVATE storage proof before opening; failure at that stage leaves
console diagnostics and no fallback log. Abort notifications report their own delivery failure.

### Agent handoff and completion

A successful transport exit code establishes only that the agent returned. The agent writes
`candidates.json` and a `candidate-ready.json` receipt containing the current run ID, nonce and
SHA-256 of the candidate bytes. The parent validates that exact handoff, reserves the logical
run, and invokes the deterministic driver through `finalize_handoff.py`.

A reserved run remains reserved after a crash, uncertain send or failed driver result. A retry
requires inspection; a new nonce does not authorize another delivery for the same logical run.
Collection receipts and fingerprint upserts support their respective accounting and dedup steps,
but neither alone proves that finalization or delivery completed.

The wrapper checks the final digest artifact and uses pull records as a weaker diagnostic:

| rc | diagnostic meaning |
| --- | --- |
| 0 | finalizer and subsequent verification/publication steps returned success |
| 1 | preflight, transport or publication failure |
| 3 | no digest and no collection trace |
| 4 | missing/invalid handoff, or collection present without a completed digest |
| 5 | driver invocation failure or digest-clobber refusal |

Consult the stage-labelled diagnostics because finalizer and wrapper failure codes share values.
An empty day still produces an honest empty-day digest. A digest's presence alone does not prove
delivery; the driver result and retained finalization state also matter.

Archive publication observes every `add`, `commit`, `pull --rebase --autostash` and `push` exit
code. It uses the verified upstream remote and branch. A failed rebase leaves the local commit
for inspection and prevents the push.

## Weekly self-evolve yield pass, `DailyHotspotsYield` (spec §8/§9)

`register-task.ps1` **also registers a WEEKLY task** `DailyHotspotsYield` (default Monday 08:37) →
`scripts/yield-wrapper.ps1`. It is the self-evolve loop that keeps the X KOL roster honest, and
without it the engine is **inert** (yield stays `unknown`, auto-prune never fires):

- **What it does**: replays the archive against the daily pulls-log to keep the roster honest. The
  rules, the guards and the review queue live in `reference/roster-evolution.md`; this file only
  owns the schedule.
- **No LLM**: the yield pass is a pure deterministic replay (`yield.py`), so `yield-wrapper.ps1` calls
  `python scripts/run.py --yield --apply --write-review` DIRECTLY, cheapest and most robust (no agent
  transport at all). Pass `-YieldReportOnly` to `register-task.ps1` to have the weekly pass
  report-only.
- **Weekly registration**: the `daily-hotspots:yield:<week>` item is an idempotent durable trace.
  Registration is not an apply lock; repeated applies still rely on the roster-evolution guards.

## Monthly identity sweep, `DailyHotspotsIdentitySweep` (§9 guardrail 4)

Handle drift (`cygaar_dev`→`0xCygaar`) and dead accounts (`statusesCount:0`, 404/suspended) need a
`get_user_info` lookup over the roster. The producer is **`scripts/identity_sweep.py`**, a pure REST
caller over twitterapi.io (`GET /twitter/user/info`, `X-API-Key` from the companion-config secret),
**no MCP, no LLM, deterministic**. It was the one missing wire: `flag_drift_and_dead` + the
`run.py --yield --user-info` ingest were already built + tested; nothing GENERATED the sweep.

- **What it does**: sweeps every ENABLED handle → writes `archive/identity-sweep-YYYY-MM.json`
  (`{handle: <user data>|null}`), then (`--feed-yield`) pipes it into `run.py --yield --user-info
  <sweep> --write-review` (report-only). What happens to a flag from there is
  `reference/roster-evolution.md`. A transient network error RAISES (fails loud) rather than
  silently marking a live account dead.
- **Cadence**: registered as MONTHLY task `DailyHotspotsIdentitySweep` (day 1 @ 11:00, after the daily
  run's window) → `scripts/identity-sweep-wrapper.ps1`. Registered out-of-band (not by
  `register-task.ps1`) so it never re-touches the daily task's `ExecutionTimeLimit`.

**All three wrappers share `scripts/wrapper-common.ps1`.** They resolve Python, write UTF-8 logs
after destination proof, and report abort-notification failures. The native-call helper captures
the child exit code under `Continue` so incidental stderr text does not replace that result.
PRIVATE log proof can fail before a file exists; consult console diagnostics in that case.
- **Manual run**: `python scripts/identity_sweep.py --feed-yield` (respects `DAILY_HOTSPOTS_CONFIG`).

## Base due/tick integration (A + B)

- **A, digest trigger**: `digest.py:register_digest_item` registers the
  `daily-hotspots:digest:<date>` idempotency key. This deduplicates registration.
  Delivery is governed separately by the retained logical-run finalization claim.
- **B, follow-up todos**: high-score opportunities the user should act on can be added as base
  `task` items (`--ext x_daily_hotspots_*`), optionally `depends-on` a market-intel deep-dive item.

Delivery stays decoupled: the digest is delivered by **this skill's own relay**, NOT by the base's
tick→its-own-Discord-relay (channel + card format differ). The base is only a state store
(due/list read-only).

## Hook into an existing daily summary

This skill exposes a "今日商业机会总结" block. If a daily fixed-time summary routine exists, fold
that block into it (don't stand up a competing channel for it).

## At-least-once + dedupe on oversleep

Catch-up collection can overlap the previous observation window. Fingerprint upserts reconcile
observations, source receipts prevent duplicate consumption, and logical-run finalization claims
prevent automatic delivery replay. Inspect an uncertain prior claim before retrying.
`SCHEDULE_NOW` / `DAILY_HOTSPOTS_NOW` inject a clock for replay and tests.
