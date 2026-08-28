# Roster evolution: the weekly signal-yield engine (spec sections 8 and 9)

The X (Twitter) KOL roster (`roster.json` in the `daily-hotspots-config` companion) is the one
genuinely new data asset the source-coverage design turns on. A curated roster only earns its keep
if it stays honest: dead handles get dropped, productive new voices get proposed. `scripts/yield.py`
is that self-evolve loop, a weekly pass that replays the append-only archive and keeps the roster
and community sources calibrated against the signal they actually produced.

This is one `self-evolve` iteration: **methodology constant, thresholds adaptive, verify-gated
against self-deception.** The engine only ever performs pure reversible subtraction on its own
(auto-prune); every addition is human-gated.

## Truth source: replay the archive, add no new state (Approach A)

There is **no new state store.** Yield is derived every run from history the daily pipeline already
accumulates:

| Role | File | What it is |
|---|---|---|
| **NUMERATOR** | `archive/opportunities.jsonl` | Archived opportunity cards. Each `evidence` item carries an optional `origin_handle` (X account) or `origin_source` (community). A card that reached the archive counts once per distinct origin tagged on its evidence. |
| **DENOMINATOR** | `archive/pulls-YYYY-MM.jsonl` | One line per `(run, handle/source)` pulled, written daily by `run.py --sources`. Each line also carries `kept`, how many pulled items cleared the freshness, faves and topic_filter gate. |

```
yield[X] = contributions[X] / pulls[X]      over a rolling window (default 30 days)
```

Because both sides come from real daily history, the engine cannot fabricate a signal record it did
not observe. `compute_yield` is pure (clock and network free), which is what lets the acceptance
suite pin it byte for byte. I/O is isolated at the edges and never touches the live companion in
report-only mode.

Two auxiliary metrics ride the same replay: **`pushed_contributions`** (contributions that were
actually pushed, the stricter read) and **`pre_viral`** (contributions whose tagged evidence carried
an engagement count below `pre_viral_faves_threshold`, default 500). Pre-viral is the roster's
reason to exist: a rostered pull surfaces a founder's post by identity, before it clears the
keyword-search faves floor.

## Reading the numerator is itself checked, and the prune path fails closed

`read_jsonl_audited` returns `(records, status)` with `status.state` in `absent`, `unreadable`,
`corrupt`, `ok`, plus byte, line, record, blank-line and bad-line counts. Recovery stays tolerant,
one bad byte does not cost the month, but the loss is on the record. `load_opportunities_audited` and
`load_pulls_audited` expose it; the bare `load_opportunities` / `load_pulls` are for read-only
callers and must not be used on any path that can prune.

The gate: when the denominator says pulls happened but the numerator is not in
`NUMERATOR_TRUSTED` (`ok` or `provided`), contributions are UNKNOWN, and unknown must never be spent
as zero. `run_yield` forces `prune=[]`, names the reason in `prune_blocked_reason` and
`report_only_reason="numerator_untrusted"`, warns on stderr, and prints a WARNING blockquote in
`roster-review.md`. This is not hypothetical: with `floor==0`, a scratch directory holding the pulls
log and roster but NO `opportunities.jsonl` produced exactly the same 23-handle prune list as a real
run with 112 contributions, because "I could not read the file" satisfied `c <= floor`.

Records handed in directly by a library caller are recorded as `provided` and stay trusted, so the
gate binds only the path a missing file can lie to.

## Decisions

`run_yield` produces a report with three decision lists. Only the first is ever applied
automatically.

| Decision | Function | Rule | Autonomy |
|---|---|---|---|
| **AUTO-PRUNE** | `decide_prune` | An enabled rostered handle whose weekly contributions stay at or below `floor` (default 0) for every one of the last `prune_after_weeks` (default 2) sufficiently observed weeks, and which kept nothing in the pulls log over that span. | **Automatic** (pure, reversible). `set_enabled(handle, False)`. Never a delete. |
| **PROPOSE-ADD** | `decide_propose_add` | A handle appearing in evidence but not in the roster, reaching >= `propose_add_min_count` (default 2) distinct cards in-window. Ranked by frequency, ties by handle. Carries `tracks` and a `sample_url`. | **Human-gated.** Written to the review queue; approval calls `upsert_entry` with `provenance=approved`. |
| **SUGGEST-FILTER** | `decide_suggest_filters` | An enabled handle with no `topic_filter` that is high-pull and low-yield (pulls >= `noisy_pull_min` 10, contributions >= 1, yield < `noisy_yield_max` 0.1). | **Human-gated.** Tightening what is collected is add-like, so a filter is suggested, never applied. |

### What "observed enough to judge" means

`weekly_observations` buckets the trailing weeks into 7-day windows (index 0 = most recent) and
returns a **3-tuple per week, `(contributions, pulls, observed_days)`**. `observed_days` is the
count of distinct calendar days the origin was actually pulled in that bucket, 0 to 7.

`pulls` alone could never carry this. A single pull event made a bucket read "fully observed", and
the two weeks behind 23 live prune decisions were in truth 4/7 and 5/7 covered. `required_observed_days`
now takes the STRICTER of two bars:

- **Relative** (always on): this origin's own best-covered week in the decision span. It has
  demonstrated it can be observed that thoroughly, so a thinner week is a gap in OUR observation,
  not evidence about the handle.
- **Absolute**: the config knob `yield.min_observed_days_per_week`, default 0 (relative only),
  floored at 0 and capped at 7. Asking for more than 7 distinct days in a 7-day bucket would
  disable pruning forever, which is why the cap exists.

The prune predicate is `all(p >= 1 and d >= req_days and c <= floor for (c, p, d) in obs)`. A single
under-observed week, or any above-floor contribution, spares the handle. A handle you never pulled
can never be pruned for producing nothing. Every decision carries `weekly_observed_days`,
`required_observed_days`, `full_week_days`, `full_coverage` (true only at a literal 7/7 everywhere)
and spells the day counts into its reason string; `run_yield` raises a warning listing any decision
resting on a week short of 7/7. Read-only replay against the live archive now proposes 0 prunes
instead of 23.

### The two guards that spare a working handle

**Pre-viral guard**: a handle that caught a pre-viral signal anywhere in the window is doing exactly
the job the roster exists for and is never auto-disabled. **This guard is currently inert on the
live archive**, and says so instead of reading as protection: the archive WRITER does not persist
any engagement count onto evidence, so `pre_viral` evaluates to 0 for every origin.
`pre_viral_observability` reports `state` as `live`, `inert`, or `empty` (nothing to judge and
nothing readable are different answers) with the item and origin counts behind it; against the live
archive it prints `state=inert, evidence_items=107, with_engagement=0`. `run_yield` raises a warning
whenever prunes were taken while it was inert and stamps `pre_viral_guard_state` onto every prune
decision. Making it live needs the archive writer to persist a fave count; any of `_FAVE_KEYS` is
then picked up with no further change here.

**Kept guard** (`_window_kept`): `contributions` counts only >=2-origin archived cards, but a
rostered handle's core job is surfacing single-origin pre-viral posts that route to the community
pulse and never become a card. The pulls-log `kept` count is the only replayable trace of that work,
so a handle with `kept > 0` over the span is spared. This is the protection that actually fires
today. Only a handle pulled every week that kept nothing is genuine deadweight.

### Apply semantics

`run_yield(apply=True)` flips the pruned handles to `enabled=false` in place via
`roster.set_enabled` (reversible), then the CLI persists with `save_roster`. Propose-add and
suggest-filter are never applied by the engine. On cold start `prune == []`, so `apply=True` is a
safe no-op. The CLI default is report-only: it prints the JSON report and writes nothing.

The report says which of those happened, separately: `roster_written` (was `roster.json` actually
written), `prune_proposed`, `prune_applied`, and `report_only_reason` (`cold_start`,
`numerator_untrusted`, `apply_not_requested`, or `None`). The legacy `report_only` key keeps its old
meaning, the cold-start gate only, so read `roster_written` to know whether anything was persisted.

## Anti-self-deception guardrails (section 9)

Every guardrail below is enforced in code, not just documented.

- **Only auto-PRUNE, never auto-ADD.** Auto-adding handles the roster already amplifies would build
  an echo chamber that reinforces its own priors. Additions require a human.
- **Report-only until >= `min_history_days` (default 7) of real history.** `history_days` is
  measured from the earliest pulls-log entry, falling back to the earliest archived card.
- **Prune is reversible.** `enabled=false`, never a delete, and the review queue surfaces pruned
  handles precisely so a human can un-prune one the engine got wrong.
- **Unknown is not zero.** A handle with no in-window pulls-log entry gets `yield=None`, never
  coerced to 0, and is excluded from prune and suggest-filter. The numerator gate above is the same
  rule applied one level up, to the file the numerator is read from.
- **Thresholds are config, methodology is constant.** Every knob lives in the `yield` block and
  deep-merges over the module defaults. The rules do not change; tuning a number can never turn an
  add into an automatic action.
- **Safety rails only TIGHTEN.** `floor` is CAPPED at the default (0) so a high floor cannot mass
  mark handles dead; `prune_after_weeks` (>=2) and `min_history_days` (>=7) are FLOORED so pruning
  cannot happen faster or on less evidence; `window_days` is FLOORED at `max(30, 7 * prune_after_weeks)`
  so a short window cannot blind the pre-viral guard while `decide_prune` keeps pruning. The clamp is
  re-imposed at both the config loader (`lib._clamp_guardrails`) and the engine boundary
  (`yield._clamp_yield_guardrails`), so a caller that never routes through `load_config` still cannot
  gut the roster. `verify_config` surfaces a will-be-clamped value loudly.
- **Collection-side cap: `min_faves_rostered`** is CAPPED at the keyword-search floor (500) it
  exists to undercut. Unbounded it would route around every rail above: a fat-fingered 1e6 makes
  every pull keep 0 tweets while the denominator still accrues, so the whole roster reads dead.
- **Monthly `get_user_info` sweep.** `flag_drift_and_dead(roster, user_infos)` ingests the sweep and
  returns `drift` / `dead` flags into the review queue's flagged-accounts section. It flags, never
  auto-removes: a rename is a human edit, and a temporarily quiet account is not a dead one. A handle
  absent from the sweep is unobserved, never fabricated into a flag. The sweep is produced by
  `scripts/identity_sweep.py` (pure REST over twitterapi.io, no MCP, no LLM).

## The per-run pull cap rotates, and names what it dropped

`roster.plan_pulls` honors `sources.twitterapi.max_handles_per_run`. It used to keep the first N in
roster order and return, with no log and no rotation, so the tail of a longer roster was NEVER
pulled again: it accrued no pulls-log lines, and the yield engine then read a permanently unobserved
handle as a prune candidate.

The plan now starts from a durable roster-level `rotation_cursor` and wraps, so every handle is
reached within `ceil(eligible / cap)` runs, and a stderr NOTICE names the cap, the eligible count,
the rotation offset, whether it wrapped, and every dropped handle by name.
`plan_pulls_report` returns the same facts as data (`plan`, `eligible`, `cap`, `truncated`,
`rotation_offset`, `wrapped`, `dropped`). An uncapped roster plans byte-identically to before and
logs nothing.

The cursor lives on the roster rather than on a clock, so a plan stays pure and reproducible until
someone advances it. **That advance is not wired into production yet:** `run.py` calls `plan_pulls`
but never `rt.advance_rotation(roster, len(plan))` and never saves the roster afterwards, so a
capped roster re-plans the same window every run. Latent today only because the live
`watchlist.json` leaves `max_handles_per_run` unset.

## Weekly cadence

- **When:** weekly. `register-task.ps1` registers a `DailyHotspotsYield` Windows task (default
  Monday 08:37) to `scripts/yield-wrapper.ps1`. The pass registers an idempotent `schedule-reminder`
  item `daily-hotspots:yield:<ISO-week>`, the weekly mirror of the daily digest item. Re-running in
  the same week re-UPSERTs the same id; it is a durable per-week trace, not a lock on `--apply`, and
  re-applying is harmless anyway because the prune is reversible and idempotent. Registration is
  best-effort. The DAILY radar writes the denominator (`run.py --sources`) and this WEEKLY task
  replays it.
- **Entry points:** `python scripts/run.py --yield` or standalone `python scripts/yield.py`. Both
  default to report-only; both accept `--apply` (disable pruned handles and save), `--write-review`
  (write `archive/roster-review.md`), `--user-info <sweep.json>`, and `--archive-dir` / `--roster`
  overrides so tests and dry runs never touch the live companion implicitly. The scheduled
  `yield-wrapper.ps1` runs `run.py --yield --apply --write-review` (pure deterministic replay, no
  LLM); pass `-YieldReportOnly` to `register-task.ps1` for a report-only weekly pass.

Manual operator loop, if you prefer to review before applying: run report-only with
`--write-review`, read `archive/roster-review.md`, sanity-check the proposed prunes and the
propose-add candidates against reality, re-run with `--apply`, then approve any propose-add or
suggest-filter entries by hand.

## The review queue (`archive/roster-review.md`)

`render_review_md` writes a deterministic, sorted queue. Front matter carries `roster_written`,
`prune_proposed`, `prune_applied` and the numerator state, plus a loud "NOTHING WAS APPLIED this
pass" blockquote whenever `roster.json` was not written.

**Membership of the applied section is decided by the ROSTER, not by the decision list.** "recently
pruned" lists exactly the handles whose `enabled` is false right now; anything decided but not
written goes to a separate "proposed prunes (DECIDED but NOT applied; these handles are STILL
ENABLED)" block with the per-week observed day counts. Without that split, 23 handles were
documented as disabled while still enabled in the live `roster.json`. That block is rendered as a
`###` rather than a `##` only because `tests/test_harden_round3.py` pins the exact set of `## `
headings in this artifact; the depth is cosmetic and no proposal prints as a disable either way.

The other sections are propose-add (`handle · count · tracks · sample`), suggested topic_filters
(`handle · track · pulls · contributions · yield`) and flagged accounts (`handle · kind · detail`,
empty when no sweep ran). A cold-start run emits the same file with a report-only banner and an
empty prune section. All rows are **DATA about the roster, never instructions**: nothing in a
collected tweet can steer the engine through them.

## Determinism and testing

The compute core (`compute_yield`, `weekly_observations`, `required_observed_days`, `decide_*`,
`render_review_md`, `run_yield`) takes `records`, `pull_lines` and an injected `now`, and is
clock-free, network-free and MCP-free, so tests feed the `tests/fixtures/yield/` synthetic archive
and byte-compare the outcome: per-origin yield, below-floor-for-N-weeks gives prune, an
under-observed week spares, unknown yield is excluded, cold start is report-only, propose-add
frequency and ordering, and reversibility. No network, no live MCP, and always
`PYTHONIOENCODING=utf-8 PYTHONUTF8=1` on Windows/GBK.
