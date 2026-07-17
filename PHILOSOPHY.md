# daily-hotspots, Design Philosophy

> One test governs every change: **does it fix the framing, or just patch a symptom?**

## P1, LLM proposes, a deterministic gate disposes

- **Symptom patch:** "the model sometimes pushes junk" → add more prompt scolding.
- **Root cause:** a probabilistic proposer can never be the final authority. So the model only ever
  *proposes* (candidates, per-dimension scores, why-now); a **pure-Python, fail-closed gate**
  (`run.py` + `verify_gate.py`) makes the binding ruling. Guardrails only ever tighten.
- **Decision it produced:** scoring aggregation, classification, dedup, and the schema gate are all
  deterministic functions with a pytest suite (T1 to T9), a skill is *proven*, not *vibed*.

## P2, Signal before noise: ≥2 independent ORIGINs, merge-then-count

- **Symptom patch:** filter spam after the fact.
- **Root cause:** "a media outlet reported a trend" is not a business signal; five reprints of one
  wire are one origin, not five. So the red line is structural: **merge cross-source first, then
  count distinct ORIGIN, then score.** One origin = watch-only, never pushed.
- **Decision it produced:** the funnel collects → de-dups → counts origins → *only then* scores;
  counting before merging is treated as covert signal-faking.

## P3, Own the seam, delegate the engine

- **Symptom patch:** build search/verify/synthesis into the daily tool because it's convenient.
- **Root cause:** that duplicates `market-intel` and fights its one-shot design. daily-hotspots owns
  exactly what nothing else does, **cadence, watchlist, dedup, scoring, delivery**, and delegates
  the deep work behind four fail-closed gates (≤3-5 deep-dives/day).
- **Decision it produced:** Tier-0 discovery never calls a skill; Tier-1 calls `market-intel`
  (`scale=standard`) only for gated survivors, and folds back a light summary, not a raw report.

## P4, 宁缺毋滥 (quality over quota)

- **Symptom patch:** ship N opportunities a day so the channel looks alive.
- **Root cause:** a fixed quota guarantees noise on quiet days. So the system has a **coverage
  floor, not a quota**: an honest empty day says "今日无合格机会" and pushes nothing.
- **Decision it produced:** every push/archive path re-asserts score + origin thresholds; filler is
  mechanically impossible (T6).

## P5, State is durable and idempotent, never re-derived

- **Symptom patch:** keep a loose JSON file and rebuild it each run.
- **Root cause:** loose state drifts and re-pushes. So cross-day memory lives in the frozen
  `schedule-reminder` base (`api_version 1.0.0`), keyed by a **content-pure fingerprint** used as the
  idempotency key, same opportunity UPSERTs, the watermark advances only after a fully successful
  run.
- **Decision it produced:** already-pushed opportunities SUPPRESS or RESURFACE (evolution), never
  blindly re-push; replay is safe.
