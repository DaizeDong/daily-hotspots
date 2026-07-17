# Design Brief, daily-hotspots

> skill-smith Step 0 (research-first) was performed in the planning phase and is captured in
> `CodesResearch/_skill-builds/04-daily-hotspots/ARCHITECTURE.md` (8-way parallel recon: methodology
> / source coverage / scoring / orchestration / dedup / Discord-archive / config-repo / packaging
> anti-patterns, each conclusion cross-checked ≥2 independent sources). This file is the auditable
> summary the gate + self-evolve key off.

## Best references (match-or-beat)
- **market-intel** (thin-delegation orchestration shape, companion-config-spec, source matrix).
- **small-cap-deepdive** (mechanical de-risk FIRST → deep only survivors → rank; kill-flags).
- **Anthropic "scale effort to query complexity"** (Tier-0 cheap full coverage; Tier-1 scarce).
- HN ranking (gravity/freshness), RICE/ICE (rejected pure-divisor), permutation de-bias for LLM
  pairwise ranking.

## Frontier ideas incorporated
- LLM-proposes / deterministic-gate-disposes split → testable (T1 to T9) instead of vibes.
- Merge-then-count distinct ORIGIN as a structural anti-signal-faking rule.
- Fingerprint = content-pure canonical_key = the base idempotency key (replay-safe).

## Anti-patterns avoided (ARCHITECTURE §10)
- session-only CronCreate as a daily scheduler; fat SKILL.md inlining logic; verbose description
  (silent truncation); LLM-vibes scoring with no deterministic function; fixed daily quota / filler;
  single-source signal; loose dedup file; re-push instead of evolution; one-message-per-card spam;
  secrets committed/echoed/BOM-broken; shipping without eval; rebuilding search in this skill;
  reading the base DB directly / DB on OneDrive; counting sources before merge.

## Proof bar (the eval signal)
- `python -m pytest tests/` green: T1 classify determinism, T2 score purity+monotonicity, T3 dedup
  precision + idempotency, T5 real schedule-reminder round-trip + ext preservation, T6 anti-filler /
  honest empty day, T7 cross-day NEW/SUPPRESS/RESURFACE, T8 zero hardcoded secrets, T9 schema
  fail-closed. 29 cases, all passing at v0.1.0.

## Scope & focus (one job, ≤3 modules)
One job: **daily frontier-opportunity radar**. Modules: (1) discovery+scoring, (2) dedup+state,
(3) delivery+archive. Deep research is delegated, not a module.
