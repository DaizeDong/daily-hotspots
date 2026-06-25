# Step 2 — Classification + scoring (reproducible rubric)

## Two-axis classification (frozen enums — anti-drift)

`scripts/classify.py` is **deterministic** (T1): keyword-hit counting over the config enum, ties
broken by config order then track weight, so the same input always yields the same label. Never let
the LLM invent a category — the enum lives in `watchlist.json`; a new one needs a `schema_version`
bump (keeps cross-day ranking comparable).

- Axis 1 `track` (single): ai-agents / dev-tools / saas-niche / fintech-crypto / consumer-social /
  hardware-iot (config-editable).
- Axis 2 `machine_type` (multi): tool-saas / marketplace / media / service / hardware / arbitrage /
  oss-monetization.
- `exclude` mutes (memecoin/MLM/NSFW/airdrop…) hard-drop before any scoring.

## Five-dimension rubric — propose at temperature 0, then aggregate deterministically

You (the LLM) propose each dim 0-100 with a one-line `because` + a bound evidence URL+ts, using the
anchored samples below. The **aggregation is `scripts/score.py` — a pure function (T2)**; never
hand-compute it.

| dim | meaning | anchors (put in the judge prompt) | default w |
|---|---|---|---|
| track_fit | watchlist track hit + TAM/demand size | 5=direct hit & large; 1=off | 0.20 |
| timing (why-now) | a **specific recent inflection** (platform shift / cost curve / regulation / behavior / new capability) | 5=≥2 triggers, narrow window; 3=single weak; 1=none | **0.25** |
| feasibility | small team, weeks to MVP with public tools | 5=weeks; 1=heavy assets/license/long R&D | 0.20 |
| competition (reverse-scored) | bluer ocean = higher | 5=blank or only clumsy substitutes (Excel/manual); 1=red ocean w/ strong incumbents | 0.15 |
| executability | path / first channel / ICP clear; solo-dev schlep | 5=clear; 1=murky | 0.20 |

Aggregation (in `score.py`, do not re-derive):
`FinalScore = (Σ wᵢ·dᵢ) × Confidence(n_sources) × Freshness(age,velocity) × track_weight_clamped`.

- **Confidence** maps independent-origin count (**a hard gate, not folded into the dims**):
  1 origin → culled (watch only); 2 → 0.8; 3+ → 1.0. Monotone non-decreasing.
- **Freshness** = HN-gravity ⊕ exponential half-life (default 72h, gravity 1.8), monotone
  non-increasing in age; a still-heating velocity gives a bounded boost so real trends aren't
  decay-killed.
- **Effort is NOT a denominator** (small-divisor score explosions are an anti-pattern);
  executability is the positive proxy instead.

## Anti-drift (reproducibility red line)

temperature 0 · forced JSON · reason-before-score CoT · counter-prompt against verbosity/confidence
bias · 1/3/5 anchors per dim. Keep a **golden set** (10-15 frozen historical opportunities): re-score
before each run; any dim drifting >1 band → pause push, recalibrate. Weights + the rubric live in
`watchlist.json` (versioned) so a re-weight can **re-rank history without re-scoring** (full
`score_breakdown` is persisted, not just the total).

## L3 pairwise de-bias (top-N)

Min-max normalize FinalScore to 0-100; for the top-N run pairwise comparisons **both orderings
(permutation de-bias)**; finalize only when pairwise agrees with pointwise, else flag "needs human
review".
