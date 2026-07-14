# Roadmap

Current: **v0.2.0**

## v0.2.0 (current) — source coverage + self-evolve yield engine
- **New capability (source-coverage design, spec `docs/superpowers/specs/2026-07-13-source-coverage-design.md`).**
  Closes the two blind spots a 7-subagent audit found: X tracked zero named KOLs (keyword-only, never
  pre-viral) and the niche-community layer (linux.do / V2EX / CN) sat at 0%.
- **X KOL roster loop** — `roster.json` (the one genuinely-new data asset) drives a
  `get_user_last_tweets` pull over enabled tier-1 handles with a low `min_faves_rostered` floor to
  catch pre-viral posts; the broad keyword search is kept for open discovery.
- **Niche community lanes** — linux.do (`/latest.rss` + `/top.rss` via brightdata, injection-free),
  V2EX (keyless JSON API via direct WebFetch), CN feeds (量子位 `qbitai.com/feed`); source definitions
  reused from market-intel shards, not copied.
- **Dual-track output** — ≥2-origin signals stay opportunity cards; single-origin community rumors
  render in a separate lightweight `## 社区脉搏` community-pulse section (label 单源未验证, capped,
  no score/deep-dive), and auto-upgrade to a card if a second origin corroborates.
- **Self-evolve signal-yield engine** (`scripts/yield.py`, report-only until 7 days of real history) —
  replays the append-only archive (numerator) against the per-run pulls-log (denominator) for a rolling
  30-day per-handle/source yield; **auto-prunes** (reversible `enabled=false`) and **propose-adds**
  (human-gated review queue). Anti-self-deception: never auto-add, never fabricate, thresholds are
  config.
- **Two existing-source fixes** — reddit switched to the login tier (escapes the anon IP-block),
  trend-pulse marked dead in config after it silently degraded.
- Dependency skills declared install-and-use (market-intel / self-evolve / schedule-reminder /
  small-cap-deepdive); `verify_config.py` gains roster schema + dependency-reachability checks.

## v0.1.3
- First real end-to-end run (2026-07-13): `--dry-run` archive-leak fix, cron wrapper permission
  posture reverted to skip-permissions (the allow-list omitted Skill/Agent and no-op'd the run),
  dry-run archive-isolation regression tests. 147 passed.

## v0.1.2
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
