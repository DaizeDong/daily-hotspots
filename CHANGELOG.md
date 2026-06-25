# Changelog

All notable changes to this project are documented here (Keep a Changelog style).

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
