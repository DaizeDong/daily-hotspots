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

Demand carries two treatments supply does not, and both are charged **once**, at the weight this
table declares. That wording is the fix, not decoration. Until 2026-08-27 crowdedness was applied
twice: `competition` already carries 0.30 of the demand vector and IS the crowd signal, and then an
outside multiplier haircut the product again by up to 0.545, so a dimension declared at 0.30 held
0.70 of the authority. In the same period `demand_freshness_floor` read as protection and behaved as
a penalty, because real pain evidence is always older than the news half-life and therefore arrived
from BELOW the floor every single time. The lane paid both, then faced a bar five points higher than
supply's, and archived exactly zero cards in its entire production history while the digest printed
each empty column as an honest quiet day.

- **Crowdedness** (`crowdedness_mode: "dimension"`, the default) folds into the competition dimension:
  `competition_effective = (1 - blend) * competition + blend * (100 - crowdedness)`, tuned by
  `crowdedness_blend` (0.5). Its total authority over the score is therefore bounded by the 0.30
  weight above, which is the point. `crowdedness_mode: "legacy_multiplier"` replays the old
  double-counting product and exists ONLY so `tests/test_demand_parity.py` can calibrate new against
  old on real history; it is not a supported production setting.
- **Freshness** (`demand_freshness_mode: "neutral"`, the default) is not decayed at all for demand.
  Recency has not been deleted, it moved to where it is judged rather than assumed: the `timing`
  dimension, at its declared 0.10. `"floor"` replays the old `max(floor, decay)` for calibration, and
  `demand_freshness_floor` is read only in that mode.

Demand still clears a **higher surfacing bar**, `min_score_to_surface_demand`. It was NOT lowered to
fix the outage; the scale was fixed so the bar is reachable. `lib._clamp_guardrails` pins it into
`[min_score_to_archive, min_score_to_archive + max_demand_floor_premium]`, because a bar the lane
cannot reach is a silent outage rather than a stricter filter, and the clamp reports itself in
`guardrail_notes`.

## Scoring a card built from the probed demand sources (2026-08-27)

The six demand sources in `reference/collect.md` §6D are not interchangeable, and the difference
matters at scoring time: each one fills a DIFFERENT required field. A demand card is strongest when
its fields come from different sources, which is also, not by coincidence, what makes its origins
independent.

| source | the field it fills | what it does to the rubric |
|---|---|---|
| Trustpilot 1 and 2 star (§6D.1) | `pain_evidence`, **directly** | the review IS the quote. Do not paraphrase it into a summary; the card leads with the customer's own words and the review permalink is the evidence URL |
| App Store reviews (§6D.2) | `pain_evidence`, **directly** | same, and the app under review is a NAMED incumbent, which feeds crowdedness below |
| SEC full text (§6D.3) | `pain_evidence`, attributable | a company telling a regulator in writing that a process is manual, or that it has a material weakness, is pain evidence signed by the party that has it |
| Federal Register RULE (§6D.4) | `why_now`, **dated** | this is what `timing` is asking for: a published rule with a publication date and a compliance obligation, mandatory and industry wide |
| USAspending award (§6D.5) | budget | a signed dollar figure. Raises `track_fit` (its TAM half stops being a guess) and `executability` (the awarding agency and the awardee are both named, so the ICP is a specific buyer) |
| The Muse job ad (§6D.6) | the paid workaround, plus the ICP | `executability`: the employer is the first channel, and the job description is a specification of the schlep somebody is already paying salary for |

Four rules follow from that table, and each of them is a way the lane has been got wrong before.

**A complaint is `pain_evidence`. It is NOT a `why_now`.** A 1 star review posted today tells you the
pain is real and current; it does not tell you why the window is open now rather than three years
ago. `timing` still has to be earned, and the honest source for it is a dated structural change
(§6D.4) or a named platform or cost shift. Scoring `timing` high because the review was recent is the
mistake the whole demand vector was retuned to stop, which is why `timing` carries only 0.10 there.

**Freshness does not punish an old complaint, and that is deliberate.** Under the shipped
`demand_freshness_mode: "neutral"`, demand freshness is exactly 1.0. A Trustpilot review from last
quarter and one from this morning score the same, because a durable unmet pain does not expire on a
news half life. Recency is judged, at 0.10, inside `timing`, where somebody has to argue for it.

**One page is ONE origin.** Twenty reviews on one Trustpilot business unit page is twenty pieces of
evidence about a single origin, and the same is true of fifty App Store reviews of one app. The
`>=2 distinct origins` red line is unmoved by review count. Counting reviews as origins is the same
covert signal faking as counting five reprints of one wire story, and `confidence` would then hand a
one origin card the 1.0 multiplier it has not earned.

**A budget is evidence for `track_fit` and `executability`, not for `feasibility`.** That an agency
signed a 79023098.38 USD contract for data entry says the demand is large and the buyer is
identifiable. It says nothing at all about whether a small team can build the thing in weeks, which
is what `feasibility` measures. Keep them apart or the dollar figure leaks into every dimension and
the card scores high on one fact five times.

### Crowdedness when the source names the competitor

A review often says where the reviewer went instead. That sentence is the best crowdedness input this
skill has ever had, better than any saturation estimate, because it is a real user making a real
switch on a dated page rather than a model guessing at a market. Use it, and use it carefully.

**Count DISTINCT named alternatives, not mentions.** Five reviewers naming the same competitor is one
competitor. This is the same distinct-origin discipline the evidence gate applies, for the same
reason.

Revised bands for a review-derived card, refining the Lane D rubric in `reference/collect.md`:

- **0 named alternatives, and the complaint describes a manual or spreadsheet workaround**:
  crowdedness 0 to 20. A manual process is a competitor, and it is a clumsy one, which is exactly the
  `competition` anchor 5 wording ("blank or only clumsy substitutes").
- **1 to 2 distinct named alternatives**: 40 to 60, a few players, still fragmented.
- **3 or more distinct named alternatives, or reviewers routinely naming one well known incumbent as
  their destination**: 80 to 100, a red ocean.

Two failure modes to refuse outright.

**A review-derived card can never honestly score crowdedness 0.** You reached those complaints by
reading the reviews of a product, so at least one company is already addressing the need well enough
to have customers who are angry at it. Score what the reviews show. Zero is a claim that nobody is
there, and you are standing in their review page.

**Absence of a named competitor in a small sample is not evidence of blue ocean, and a page you never
fetched is not evidence of anything.** One Trustpilot page delivers 20 reviews and one App Store page
delivers 50; if you read fewer, say how many you read. If the fetch returned the 153 to 170 byte
"Verifying your connection" interstitial (§6D.1), you have NO crowdedness evidence: leave crowdedness
unestimated and let the card fail its evidence requirement, rather than filling in a flattering low
number. The arithmetic is worth knowing before you guess: crowdedness folds into `competition` at
`crowdedness_blend` 0.5, and `competition` carries 0.30 of the demand vector, so the gap between a
fabricated 0 and a truthful 100 is up to 15 points of raw score. That is more than the 5 point
premium the demand floor charges over the archive floor.

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
