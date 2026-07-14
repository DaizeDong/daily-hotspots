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
- **Monthly `get_user_info` sweep (operational).** A once-a-month identity check catches handle drift
  (e.g. `marc_louvion` -> `marclou`) and dead accounts (`statusesCount:0`, e.g. `realGeorgeHotz`
  flagged in Appendix A). It **flags into the review queue, never auto-removes**: a rename is a human
  edit, and a temporarily quiet account is not a dead one.

## Weekly cadence

- **When:** weekly. The design wires an idempotent `schedule-reminder` item
  `daily-hotspots:yield:<week>` (spec §8) so the pass runs once per ISO week and cannot double-apply.
- **Entry point today:** standalone `python scripts/yield.py`. Default is **report-only**.
  - `--apply` also disables pruned handles in `roster.json` and saves it (reversible).
  - `--write-review` also writes `archive/roster-review.md`.
  - `--archive-dir` / `--roster` override the config-dir probe so tests and dry runs never touch the
    live companion implicitly.
- **Baseline after week 1.** Ships report-only; pruning activates only after one week of real history
  clears the cold-start gate (spec §13 rollout).

Recommended weekly operator loop:

1. `python scripts/yield.py --write-review` (report-only) and read `archive/roster-review.md`.
2. Sanity-check the pruned list and the propose-add candidates against reality.
3. Re-run with `--apply` to commit the (reversible) prunes.
4. Approve any propose-add / suggest-filter entries by hand (`upsert_entry`, `provenance=approved`).

## The review queue (`archive/roster-review.md`)

`render_review_md` writes a deterministic, sorted queue. It is where the engine's human-gated
decisions live, and the un-prune escape hatch. Three sections:

| Section | Contents |
|---|---|
| **propose-add** | `handle · count · tracks · sample` for non-roster handles above the frequency floor. Labeled *human-gated; NEVER auto-added.* |
| **recently pruned** | `handle · track · reason` for auto-pruned handles. Labeled *reversible: enabled=false, un-prune here.* |
| **suggested topic_filters** | `handle · track · pulls · contributions · yield` for noisy high-pull handles. |

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
