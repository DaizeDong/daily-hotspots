# Step 2, Classification + scoring (reproducible rubric)

This shard is the authoritative description of what `scripts/classify.py` and `scripts/score.py`
actually compute. Knob names, types and defaults live once in [CONFIG.md](../../../CONFIG.md);
this file explains what each one does to a card.

**Invariants, true under every tuning.** The LLM only ever proposes the five per-dimension scores;
the aggregation is a pure function. More independent origins never lowers a score. Staler never
raises one. Effort is never a denominator. A guardrail clamp can only tighten a floor, and any clamp
that bites is recorded in `scoring.guardrail_notes` rather than applied silently.

## Two-axis classification (frozen enums, anti-drift)

`scripts/classify.py` is deterministic (T1): the same input always yields the same label. Never let
the model invent a category; the enum lives in `watchlist.json` and a new one needs a
`schema_version` bump, which is what keeps cross-day ranking comparable.

**Exclude mute runs first.** `check_excluded` returns the first matching `exclude` term and the
candidate is hard-dropped before any scoring. That test is a bare substring on purpose (`memecoin`
mutes `memecoins`): over-matching is the safe direction for a safety veto. The roster lane calls it
directly, because a preset track carries the track but is never a licence to skip the mute list.

**Axis 1, `track` (single).** Each enabled track scores one point per matched keyword. Keyword
matching is token-bounded when the keyword's edge is an ASCII word character (so `ci` no longer
fires inside *decision* and `api` no longer fires inside *capital*) and a plain substring for CJK,
which has no spaces to bound. The winner maximizes `(hits, track weight, earlier config order)`, so
**a tie on hit count is broken by the higher track weight first, and only then by config order**.
A candidate that matches nothing lands in the real enum member `unclassified` (weight 1.0, no
keywords) with `track_matched: false`; it is never filed under whichever track sits first in config.

**Axis 2, `machine_type` (multi)** is keyword rules intersected with the configured
`machine_types`; an empty result falls back to `["tool-saas"]`. `focus_tags` are the configured
`focus_topics` whose words appear in the text.

## Five-dimension rubric, proposed at temperature 0

You propose each dim 0 to 100 with a one-line `because` plus a bound evidence URL and ts, using the
anchored samples below. `scripts/score.py` aggregates (T2); never hand-compute it.

| dim | meaning | anchors (put in the judge prompt) |
|---|---|---|
| track_fit | watchlist track hit + TAM/demand size | 5=direct hit & large; 1=off |
| timing (why-now) | a **specific recent inflection** (platform shift / cost curve / regulation / behavior / new capability) | 5=>=2 triggers, narrow window; 3=single weak; 1=none |
| feasibility | small team, weeks to MVP with public tools | 5=weeks; 1=heavy assets/license/long R&D |
| competition (reverse-scored) | bluer ocean = higher | 5=blank or only clumsy substitutes (Excel/manual); 1=red ocean w/ strong incumbents |
| executability | path / first channel / ICP clear; solo-dev schlep | 5=clear; 1=murky |

## Aggregation, six factors (not four)

```
FinalScore = raw x confidence x freshness x track_weight x lifecycle_weight x crowdedness_mult
```

clamped into [0,100] and rounded to 4 places. Reading each factor as "what can this do to a card":

| factor | source | range | effect |
|---|---|---|---|
| `raw` | `Σ wᵢ·dᵢ` over the five dims, weights normalized to sum 1 | 0 to 100 | the whole judged content of the card |
| `confidence` | `lib.confidence(independent_source_count)` | 0.5 / 0.8 / 1.0 for 1 / 2 / 3+ origins | the >=2 red line is a HARD gate upstream; this is only the multiplier, and 1 origin is culled before it is reached |
| `freshness` | `lib.freshness(age_h, half_life, gravity)` = `0.8*half-life + 0.2*HN-gravity` | (0,1] | monotone non-increasing in age. `velocity` then scales it by `1 + 0.15*v`, so a still-heating trend resists decay and a **cooling** one is penalized |
| `track_weight` | the config track weight, clamped to [0.5,1.5] then folded at HALF strength (`1.3` becomes `1.15`) | 0.75 to 1.25 | a watchlist preference nudges ranking; it can never dominate the evidence |
| `lifecycle_weight` | `scoring.lifecycle_weights[stage]`, clamped to [0.3,1.0] | emerging 1.0, peak 0.9, declining 0.75, **fading 0.55** | a closed window stops topping the feed. Unknown or absent stage is neutral 1.0 |
| `crowdedness_mult` | demand cards only, `max(0.1, 1 - crowdedness_penalty * crowdedness/100)` | down to 0.3 at the shipped 0.7 penalty | a red ocean is not an opportunity |

The last two are the ones that surprise people: **either can cut a card roughly in half on its own**,
and both are invisible in `raw`. A demand card at `crowdedness` 100 in a `fading` window keeps under
17 percent of its judged content. Persist the full `score_breakdown`, not just the total, so any
weight change can re-rank history without re-scoring it.

## Two weight vectors, one function

`side` picks the vector. `supply` (the default, and what a card without a `side` gets) is
hotness-first breadth. `demand` is pain-first: a durable unmet need does not have to be trending
today, and blue ocean plus can-you-actually-build-it carry the weight.

| dim | `weights` (supply) | `demand_weights` |
|---|---|---|
| track_fit | 0.20 | 0.10 |
| timing | **0.25** | 0.10 |
| feasibility | 0.20 | 0.25 |
| competition | 0.15 | **0.30** |
| executability | 0.20 | 0.25 |

Demand additionally floors freshness at `demand_freshness_floor` (0.6) so recency cannot bury a
months-old complaint thread, and pays the crowdedness multiplier above. It also clears a **higher
surfacing bar**, `min_score_to_surface_demand`, which `lib._clamp_guardrails` pins into
`[min_score_to_archive, min_score_to_archive + max_demand_floor_premium]`: a bar the lane cannot
reach is a silent outage, not a stricter filter, so the clamp reports itself in `guardrail_notes`.

> Live gap worth knowing before you tune: `lib.DEFAULT_CONFIG` also ships `crowdedness_mode`,
> `crowdedness_blend` and `demand_freshness_mode`, documented there as folding crowdedness into the
> competition dimension instead of multiplying it. `score.py` does not read those three keys yet, so
> today they are inert and the multiplier above is what runs.

## Anti-drift (reproducibility red line)

temperature 0, forced JSON, reason-before-score CoT, a counter-prompt against verbosity and
confidence bias, 1/3/5 anchors per dim. Keep a golden set of 10 to 15 frozen historical
opportunities, re-score it before each run, and pause the push if any dim drifts more than one band.
A proposed weight retune goes through `score.weight_regression_gate`, which re-ranks the golden set
under both vectors and returns `auto_pass` / `needs_review` / `block` from Kendall tau plus
push-floor churn against the `scoring.weight_regression` budget.

## L3 pairwise de-bias (top-N)

Min-max normalize FinalScore to 0 to 100; for the top-N run pairwise comparisons in **both
orderings** (permutation de-bias); finalize only when pairwise agrees with pointwise, else flag
"needs human review".
