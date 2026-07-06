# Changelog

All notable changes to this project are documented here (Keep a Changelog style).

## [0.1.2] - 2026-07-06
### Fixed
- **R4 lifecycle downweight now reaches live scoring.** `run.build_card` called `score_opportunity`
  without `lifecycle_stage`, so the closed-window (fading) downweight was inert in production (sw
  always 1.0). Now wired: a fading opportunity scores strictly lower than an emerging one.
- **push_card.py standalone CLI** no longer crashes with UnicodeEncodeError on a legacy Windows (GBK)
  console — stdout is forced to UTF-8 (the run.py pipeline path was already unaffected).
### Added
- **R5 catch-up entry** (`run.py --catch-up`): reachable, idempotent backfill of missed daily-digest
  items since the last watermark (the tested `catch_up_digests` was previously invoked by nothing).
  Opt-in; reads no candidate input; for the cron/orchestration layer after an oversleep.
- Regression tests `tests/test_run_wiring.py` (R4 downweight + catch-up reachability). 145 passed.

## [0.1.1] - 2026-06-27
### Changed
- **Discord egress unified through Agent Center relay**: pushes now prefer schedule-reminder's
  `relay.py send --stream hotspots` (per-stream identity in the Agent Center server) when the base
  is installed, and **fall back to the Big Brother relay (send.py) when it is not** — fully
  pluggable, no behaviour change when the base is absent. Existing env/arg overrides still win.

## [0.1.0] - 2026-06-25
### Added
- Initial release. Three-tier funnel: Tier-0 multi-source discovery (trend-pulse / HackerNews /
  Product Hunt / X / arXiv / GitHub / GDELT), in-skill cross-source merge, ≥2-distinct-origin red
  line.
- Deterministic engines (stdlib only): `classify.py` (frozen-enum two-axis classifier),
  `score.py` (pure 5-dim aggregation with confidence/freshness multipliers), `dedup.py`
  (multi-signal fingerprint + NEW/SUPPRESS/RESURFACE over the schedule-reminder base),
  `verify_gate.py` (fail-closed schema + anti-filler), `archive.py`, `push_card.py` (Discord
  embed + hard-limit validation + relay seam), `digest.py`, `run.py` (orchestrator).
- schedule-reminder base integration (frozen api_version 1.0.0): idempotency-key dedup,
  `x_daily_hotspots_*` ext namespace, singleton watermark, idempotent daily digest item.
- Windows Task Scheduler headless wrapper + register script (08:07, off-:00).
- Acceptance suite: 29 pytest cases covering T1–T9 (classify / score / dedup / base round-trip /
  anti-filler / cross-day / secrets / schema), including a real reminder.py round-trip.
- Bilingual philosophy-first README, PHILOSOPHY.md (P1–P5), 6 progressive-loading reference shards.
