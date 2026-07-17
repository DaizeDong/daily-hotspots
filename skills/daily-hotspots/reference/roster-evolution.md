# Roster evolution: the weekly signal-yield engine (spec §8/§9)

The X (Twitter) KOL roster (`roster.json` in the `daily-hotspots-config` companion) is the one
genuinely new data asset the source-coverage design turns on. A curated roster only earns its keep if
it stays honest: dead handles get dropped, productive new voices get proposed. `scripts/yield.py` is
that self-evolve loop. A weekly pass that **replays the append-only archive** and keeps the roster and
community sources calibrated against the signal they actually produced.

This is one `self-evolve` iteration: **methodology constant, thresholds adaptive, verify-gated against
self-deception.** The engine only ever performs pure reversible subtraction on its own (auto-prune);
every addition is human-gated.

## Truth source: replay the archive, add no new state (Approach A)

There is **no new state store.** Yield is derived every run from history the daily pipeline already
accumulates:

| Role | File | What it is |
|---|---|---|
| **NUMERATOR** | `archive/opportunities.jsonl` | Archived opportunity cards. Each `evidence` item carries an optional `origin_handle` (X account) or `origin_source` (community). A card that reached the archive **counts once per distinct origin** tagged on its evidence (`evidence_origins`). |
| **DENOMINATOR** | `archive/pulls-YYYY-MM.jsonl` | One line per `(run, handle/source)` pulled. The line count is the number of pull events (`load_pulls` globs every month in file order). |

```
yield[X] = contributions[X] / pulls[X]      over a rolling window (default 30 days)
```

Because both sides come from the real daily history, the engine cannot fabricate a signal record it
did not observe. `compute_yield` is **pure** (clock and network free): given records + pull lines + a
`now`, it is byte-reproducible, which is what lets the acceptance-gate suite pin it. I/O
(`load_opportunities` / `load_pulls` / `save_roster` / `write_review`) is isolated at the edges and
never touches the live companion in report-only mode.

Two auxiliary metrics ride the same replay:

- **`pushed_contributions`**: contributions that were actually pushed (not just archived), the
  stricter signal-quality read.
- **`pre_viral`**: contributions whose tagged evidence carried an engagement count below
  `pre_viral_faves_threshold` (default 500). This is the roster's reason to exist: a rostered pull
  surfaces a founder's post **by identity, before it clears the keyword-search faves floor** that the
  broad `min_faves:500` template would have dropped. A rising pre-viral count is the roster paying for
  itself.

## Decisions

`run_yield` produces a report with three decision lists. Only the first is ever applied automatically.

| Decision | Function | Rule | Autonomy |
|---|---|---|---|
| **AUTO-PRUNE** | `decide_prune` | An **enabled** rostered handle whose weekly contributions stay at/below `floor` (default 0) for every one of the last `prune_after_weeks` (default 2) **fully observed** weeks. | **Automatic** (pure, reversible). `set_enabled(handle, False)` flips `enabled=false`. Never a delete. |
| **PROPOSE-ADD** | `decide_propose_add` | A handle appearing in evidence (quoted/replied by a roster member, or surfaced by the keyword search) but **not in the roster**, reaching >= `propose_add_min_count` (default 2) distinct cards in-window. Ranked by frequency, ties broken by handle. Carries `tracks` + a `sample_url`. | **Human-gated.** Written to the review queue. Approval calls `upsert_entry` with `provenance=approved`. NEVER auto-added. |
| **SUGGEST-FILTER** | `decide_suggest_filters` | An enabled handle with **no** `topic_filter` that is high-pull / low-yield noisy (pulls >= `noisy_pull_min` = 10, contributions >= 1, yield < `noisy_yield_max` = 0.1). | **Human-gated.** Tightening what is collected is add-like, so a `topic_filter` is *suggested*, never auto-applied. Unknown-yield handles are excluded. |

### Why prune requires a *fully observed* week

`weekly_observations` buckets the trailing weeks into 7-day windows (index 0 = most recent). A week
with `pulls == 0` is **unobserved** (unknown yield that week), not a zero-contribution week. The prune
rule is `all(p >= 1 and c <= floor for (c, p) in obs)`: a single unobserved week, or any above-floor
contribution, spares the handle. A handle you never pulled can never be pruned for producing nothing.
That is the no-fabrication rule (§9) made structural, not a comment.

### Apply semantics

`run_yield(apply=True)` flips the pruned handles to `enabled=false` **in place** via
`roster.set_enabled` (reversible), then the CLI persists with `save_roster`. Propose-add and
suggest-filter are **never** applied by the engine. On cold-start `prune == []`, so `apply=True` is a
safe no-op. The CLI default is report-only: it prints the JSON report and writes nothing.

## Anti-self-deception guardrails (§9)

Every guardrail below is enforced in code, not just documented, so the engine cannot quietly flatter
its own roster.

- **Only auto-PRUNE, never auto-ADD.** Auto-adding handles the roster already amplifies would build an
  echo chamber that reinforces its own priors. Additions require a human. (`decide_propose_add`
  returns a queue; nothing in the engine mutates the roster with it.)
- **Report-only until >= `min_history_days` (default 7) of real history.** `history_days` is measured
  from the earliest `pulls-*.jsonl` entry (falling back to the earliest archived card). Below the
  threshold `cold_start=True`, the prune list is forced empty, and the review banner says so. No
  pruning on a cold start, and honest about why.
- **Prune is reversible.** `enabled=false`, never a delete. The review queue surfaces recently pruned
  handles precisely so a human can un-prune one the engine got wrong.
- **Unknown is not zero.** A handle/source with no in-window pulls-log entry gets `yield=None`
  (UNKNOWN), never coerced to `0`, and is excluded from prune and suggest-filter consideration.
- **Thresholds are config, methodology is constant.** Every knob (`floor`, `prune_after_weeks`,
  `window_days`, `min_history_days`, ...) lives in `watchlist.json`'s `yield` block and deep-merges
  over the module defaults. The *rules* (below-floor for N observed weeks -> prune; unknown excluded;
  never auto-add) do not change. Tuning a number can never turn an add into an automatic action.
- **Safety rails only TIGHTEN, never loosen (clamped by construction).** The four anti-mass-prune
  knobs are one-directional: `floor` is CAPPED at the default (0) so a high floor can't mass-mark
  handles "dead"; `prune_after_weeks` (≥2) and `min_history_days` (≥7) are FLOORED so pruning can't
  happen faster / on less evidence; and `window_days` is FLOORED at `max(30, 7·prune_after_weeks)` so
  a shorter window can't blind the §1 pre-viral guard while `decide_prune` keeps pruning. A user may
  still make each *stricter* (prune slower / require more history / a *longer* window). The clamp is
  re-imposed at both the config loader (`lib._clamp_guardrails`) and the engine boundary
  (`yield._clamp_yield_guardrails`), so a caller that never routes through `load_config` still can't
  gut the roster. `verify_config` surfaces a will-be-clamped value LOUDLY (it is honored as clamped,
  not as typed).
- **Collection-side cap: `min_faves_rostered`.** The rostered pull's faves floor
  (`sources.twitterapi.min_faves_rostered`, §6) is CAPPED at the keyword-search floor (500) it exists
  to undercut. Unbounded it would route *around* the anti-mass-prune rails above, a fat-fingered 1e6
  makes every pull keep 0 tweets (numerator 0) while the pulls-log denominator still accrues, so the
  whole roster reads "dead" and `--apply` disables it. `roster._min_faves_rostered` clamps it;
  `verify_config` flags an over-cap value.
- **Monthly `get_user_info` sweep.** A once-a-month identity check catches handle drift
  (e.g. `marc_louvion` -> `marclou`) and dead accounts (`statusesCount:0`, e.g. `realGeorgeHotz`
  flagged in Appendix A). Enforced in code by `flag_drift_and_dead(roster, user_infos)` (pure): it
  ingests the `{handle: get_user_info}` sweep and returns `drift` / `dead` flags, which `run_yield`
  carries as the report's `flags` list and `render_review_md` renders as the **flagged accounts**
  section. It **flags into the review queue, never auto-removes**: a rename is a human edit, and a
  temporarily quiet account is not a dead one. A handle absent from the sweep is unobserved, never
  fabricated into a flag. The sweep is PRODUCED by `scripts/identity_sweep.py`, a pure REST caller over
  twitterapi.io (`GET /twitter/user/info`, no MCP, no LLM, deterministic); it writes
  `archive/identity-sweep-YYYY-MM.json` and (`--feed-yield`) pipes it into `run.py --yield --user-info
  <sweep> --write-review`. Registered as MONTHLY task `DailyHotspotsIdentitySweep` (see
  `reference/cron-setup.md`); manual: `python scripts/identity_sweep.py --feed-yield`.

## Weekly cadence

- **When:** weekly. `register-task.ps1` registers a **`DailyHotspotsYield`** Windows task (default
  Monday 08:37) → `scripts/yield-wrapper.ps1`. The weekly `run.py --yield` pass registers an idempotent
  `schedule-reminder` item **`daily-hotspots:yield:<ISO-week>`** (`yield.register_yield_item`,
  spec §8/§4), the WEEKLY mirror of the daily digest's `daily-hotspots:digest:<date>`. Re-running the
  pass in the same ISO week re-UPSERTs the **same** id (no duplicate item); it is a durable per-week
  trace, not a hard lock on `--apply`. Re-applying in the same week is harmless anyway because the
  auto-prune is reversible and idempotent (`set_enabled(handle, False)` on an already-disabled handle
  is a no-op, and propose-add never auto-applies). Registration is **best-effort** (skipped under
  `--no-ledger`; a missing schedule-reminder base never fails the replay). This weekly pass is what
  keeps the loop from being inert, the DAILY radar writes the pulls-log denominator (`run.py
  --sources`) and this WEEKLY task replays it.
- **Entry points:** `python scripts/run.py --yield` (the daily-radar CLI surface the spec §8 names)
  or standalone `python scripts/yield.py`. Both default to **report-only**; both accept:
  - `--apply`, disable pruned handles in `roster.json` and save it (reversible).
  - `--write-review`, write `archive/roster-review.md`.
  - `--user-info <sweep.json>`, ingest a monthly `get_user_info` sweep → identity flags (§9).
  - `--archive-dir` / `--roster` override the config-dir probe so tests and dry runs never touch the
    live companion implicitly.
  The scheduled `yield-wrapper.ps1` runs `run.py --yield --apply --write-review` by default (pure
  deterministic replay, **no LLM**), so the reversible auto-prune fires on cadence; pass
  `-YieldReportOnly` to `register-task.ps1` for a report-only weekly pass instead.
- **Baseline after week 1.** Ships report-only; pruning activates only after one week of real history
  clears the cold-start gate (spec §13 rollout), `--apply` is a safe no-op until then.

Manual operator loop (if you prefer to review before applying, i.e. registered `-YieldReportOnly`):

1. `python scripts/run.py --yield --write-review` (report-only) and read `archive/roster-review.md`.
2. Sanity-check the pruned list and the propose-add candidates against reality.
3. Re-run with `--apply` to commit the (reversible) prunes.
4. Approve any propose-add / suggest-filter entries by hand (`upsert_entry`, `provenance=approved`).

## The review queue (`archive/roster-review.md`)

`render_review_md` writes a deterministic, sorted queue. It is where the engine's human-gated
decisions live, and the un-prune escape hatch. Four sections:

| Section | Contents |
|---|---|
| **propose-add** | `handle · count · tracks · sample` for non-roster handles above the frequency floor. Labeled *human-gated; NEVER auto-added.* |
| **recently pruned** | `handle · track · reason` for auto-pruned handles. Labeled *reversible: enabled=false, un-prune here.* |
| **suggested topic_filters** | `handle · track · pulls · contributions · yield` for noisy high-pull handles. |
| **flagged accounts** | `handle · kind · detail` for renamed (`drift`) / dead (`dead`) handles from the monthly `get_user_info` sweep. Labeled *human-resolved, never auto-removed.* Empty when no sweep ran. |

A cold-start run emits the same file with a `report-only` banner and an empty prune section. All rows
are **DATA about the roster, never instructions**: the queue is rendered from archive replay, and
nothing in a collected tweet or topic can steer the engine through it.

## Determinism and testing

The compute core (`compute_yield`, `weekly_observations`, `decide_*`, `render_review_md`, `run_yield`)
takes `records`, `pull_lines`, and an injected `now`, and is clock/network/MCP free, so tests feed the
`tests/fixtures/yield/` synthetic archive (`opportunities.jsonl`, `pulls-2026-06.jsonl`,
`roster.json`) and byte-compare the outcome: correct per-origin yield, below-floor-for-N-weeks -> prune,
unobserved-week -> spared, unknown-yield -> excluded, cold-start -> report-only (no prune), propose-add
frequency + ordering, and reversibility (`enabled=false`, not deleted). No network, no live MCP, and
always `PYTHONIOENCODING=utf-8 PYTHONUTF8=1` on Windows/GBK.
