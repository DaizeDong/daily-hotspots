# Roadmap

Current: **v0.1.2**

## v0.1.2 (current)
- Three-tier funnel, deterministic engines, schedule-reminder integration, Discord tiered push +
  archive, daily cron, T1–T9 acceptance suite.

## Planned (self-evolve headroom — R1–R6 from ARCHITECTURE §9)
- **R1** classify: multilingual / paraphrase fixtures (CN-EN mixed-title normalization robustness).
- **R2** score: weight-retune backtest — re-rank the golden set after a weight change, gated A/B.
- **R3** dedup: adversarial fixtures (timestamp/tracking-only = same; word-overlap-different-event =
  distinct); tune cosine/Hamming margins.
- **R4** anti-filler: down-weight closed-window (peak/declining) opportunities; a weak-signal
  watchlist tier (must persist ≥2 days to upgrade) so the floor doesn't bury early signals.
- **R5** base: oversleep catch-up (at-least-once + dedupe) assertions.
- **R6** multi-armed bandit / Thompson sampling for track explore-exploit balance.
- Embed-capable dedicated Discord bot (drop the content-only relay).
