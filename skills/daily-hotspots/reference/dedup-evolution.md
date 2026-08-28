# Step 3, Cross-day dedup + evolution (on the schedule-reminder base)

State lives in the `schedule-reminder` base, **frozen `api_version 1.0.0`**. Hard rules:
**subprocess only** (`reminder.py <verb> --json`), never read the `.db`, never build SQL; write
calls carry `--source daily-hotspots --idempotency-key --actor`; the DB must be **local NTFS**
(OneDrive or a network share means WAL corruption); `list` has no tag filter, so `list --source
daily-hotspots --active` then filter in-process by `ext`; there is no generic KV, so the watermark
is a singleton item (`idempotency_key=daily-hotspots:watermark`, value in `ext`).
`scripts/dedup.py:LedgerClient` wraps all of this; locate `reminder.py` via
`$DAILY_HOTSPOTS_REMINDER_CMD` or the default probe.

## Fingerprint (content-pure, never a timestamp or tracking param)

- **Hard key** = `canonical_key` (entity set plus track), used directly as `idempotency_key`, so an
  exact same opportunity UPSERTs (same id, ext merged) and idempotency is built in.
- **Soft match** (`dedup.match_existing`, pure, T3) for hard-key misses. Single-signal matching is
  forbidden in both directions: pure semantic overlap gives "same words, different event" false
  merges, pure string matching misses rewrites. The curated shared-entity set is the guard that
  blocks the false merge, and it is required by every rung except the near-identical one.

| rung | fires when | why it exists |
|---|---|---|
| A | token Jaccard >= `dedup_cosine_threshold` (0.83) | a genuine near-duplicate text, regardless of word order; the only rung that needs no second signal |
| B | shared entities AND SimHash Hamming <= `dedup_simhash_hamming` (3) AND subject agreement | a light rewrite |
| C | shared entities AND Jaccard >= 0.45 AND subject agreement | a heavier rewrite of the same story |
| D | shared entities AND character 3-gram similarity >= `dedup_char_ngram_threshold` (0.10) AND subject agreement | CJK. The tokenizer emits whole clauses as single tokens, so two write-ups of ONE story score about 0.12 Jaccard and Hamming 18 to 24, and rungs A to C can never fire. Character n-grams do not depend on word boundaries |

Candidate text is truncated to 400 chars before comparison, matching what `build_ext` stores, so a
row is never compared against an asymmetrically longer candidate. Ranking uses `max(Jaccard,
char_similarity)` so a CJK match is not always ranked last.

**Subject agreement** (`_subject_agree`) reads the KIND of the earliest divergence in a difflib
alignment of the two entity sequences. A `replace` at the first non-equal opcode is a subject
substitution (same template, swapped brand) and vetoes the match wherever in the sentence it sits;
an `insert` or `delete` there is added framing and agrees. It ABSTAINS when the substituted span
holds a token that is not word-like (ascii, or a CJK run of at most 6 chars, because brands are
short and clauses are not): an alignment against a clause token cannot identify a subject, so it
must not pretend to. Comparing only the two LEADING entities, which is what it used to do, both
false-merged on a generic leading token and false-split on a framing prefix.

Two new knobs, `dedup_char_ngram_threshold` (0.10) and `dedup_char_ngram_n` (3), are read from
`cfg["scoring"]` with module-level defaults in `dedup.py`, so a `watchlist.json` override works
today even though they are not yet in `lib.DEFAULT_CONFIG`.

## The compare window actually expires now

`lookback_days` (7) and `fading_quiet_days` (5) shipped in the config and were read by nothing:
`list_active` returned every row ever written (342 permanently active rows on the live ledger), so
the suppression surface grew without bound and a card from months ago could still suppress today's.

`dedup.partition_ledger(rows, cfg, now)` is the pure split. A row expires when days since its
`last_seen` reach EITHER bound, whichever comes first. Singleton bookkeeping rows (any key starting
`daily-hotspots:`, the watermark, bandit arms, pulse-seen) are exempt and never expire; any future
singleton MUST use that prefix. A row with a missing or unparsable `last_seen` is KEPT and named
with a reason, because dropping a row of unknown age would be the silent loss this fixes.

Nothing is dropped silently. The report carries `checked`, `kept`, both bound values AND whether
each came from config or the builtin default, every expired row with its quiet age and which bounds
fired, plus the singleton and undated lists. `LedgerClient.list_active` applies the window, stores
the report on `self.last_window_report`, and prints `dedup.format_window_report` to stderr on every
call, so "checked 10, dropped none" and "never ran" are different lines. Over-long keys are cut with
a visible `...(cut)` and an over-long expired list ends with `+N more expired (not listed)`. Pass
`window=False` for the raw rows.

## Three-branch decision (`dedup.decide`, pure)

| branch | condition | action |
|---|---|---|
| **NEW** | no fingerprint match | score, push if over the floor, store item (pending to doing) |
| **SUPPRESS** | match with no material change | update last_seen, append a score sample, do not re-push |
| **RESURFACE** | match with material change: a lifecycle stage jump, `abs(score_delta) >= resurface_score_jump`, or a new origin crossing the >=2 line | push an evolution UPDATE card (delta plus new sources), push_count+1 |

### The baseline the delta is measured against

`score_delta` is measured against `x_daily_hotspots_baseline_score`, the score at the last time the
opportunity was actually SURFACED, not against yesterday's observation. `build_ext` advances that
anchor ONLY when the candidate carries `pushed=True` or `archived=True`, so a RESURFACE the verify
gate blocked does not consume it.

This is the ratchet defect. The comparison used to run against `last_score`, which `build_ext`
rewrites on EVERY run including a SUPPRESS, so for an opportunity that strengthens a little each day
the baseline climbed in lockstep with the score, every day's delta was the one-day step of two or
three points, `resurface_score_jump` was never reached, and a story that went from 60 to 90 over two
weeks was suppressed on all fourteen days. Legacy rows written before the key existed fall back to
`last_score` and self-heal on the next write; the returned delta carries `baseline_score`,
`baseline_from`, `last_observed_score` and `observed_delta` so which anchor was used is visible.

## ext namespace (`x_daily_hotspots_*`, MUST-PRESERVE round-trip)

`build_ext` writes canonical_key, simhash, text (<=400 chars), first_seen, last_seen, last_score
(today's observation), baseline_score (the last surfaced score), lifecycle_stage, source_set,
push_count, and a `samples` ring buffer (capped, default 30) of `{ts,score,n_sources,velocity,stage}`.
For anchorable keywords fill `stage` from trend-pulse `get_trend_velocity` plus
`get_lifecycle_prediction`; else self-derive velocity as score delta over days. Five consecutive
quiet days transitions `doing` to `done` (fading auto close-out) and, per the window above, takes
the row out of the compare set.

## Watermark + idempotency

Collect `since = last_run_at - 5min` (clock-skew buffer; over-collecting plus fingerprint dedup
beats missing a late arrival). **Write the watermark only after the whole run succeeds** (atomic); a
mid-run failure leaves it unmoved so the next run re-covers, and the fingerprint UPSERT prevents a
double-push. Dedup IDs are content-derived and source-derived, never generated at processing time,
because a replay that mints new IDs breaks dedup.
