# daily-hotspots, Design Philosophy

## P1, LLM proposes, a deterministic gate disposes

A model can propose candidates, dimension scores and `why_now`, but those judgments remain
uncertain. Pure Python classification, scoring, deduplication and schema checks make admission
reproducible. `run.py` and `verify_gate.py` reject invalid candidates; guardrail changes require
explicit review and regression evidence. The T1 to T9 suite tests these mechanisms, not unmeasured
business outcomes. The four deep-research routing gates are caller obligations without Python
enforcement, as described in [delegation](skills/daily-hotspots/reference/delegation.md).

## P2, Signal before noise: ≥2 independent ORIGINs, merge-then-count

Reprints of one report are one origin. Collect and merge cross-source evidence before counting
independent origins, then score eligible clusters. Ranked cards require at least two independent
origins. Single-origin leads may appear only in the labeled, unverified community pulse. This can
delay early opportunities, but prevents repeated coverage from being treated as corroboration.

## P3, Own the seam, delegate the engine

daily-hotspots owns cadence, watchlist, deduplication, scoring and delivery. Search, verification
and synthesis for selected opportunities use the existing research skills. Tier-0 discovery uses
source tools without a skill call; gated Tier-1 work uses `market-intel` (`scale=standard`) or
`small-cap-deepdive`, with a daily budget of 3 to 5 deep dives. Return a structured summary to the
card and preserve the full report in its PRIVATE artifact location.

## P4, 宁缺毋滥 (quality over quota)

A daily schedule does not require a fixed number of cards. Every push and archive path checks
the applicable score and origin thresholds; T6 covers below-threshold rejection. A completed
collection with no qualifying opportunity reports `今日无合格机会`. Failed or unchecked source
access must remain visible in coverage and cannot establish a quiet day.

## P5, State is durable and idempotent, never re-derived

The `schedule-reminder` interface (`api_version 1.0.0`) stores content-derived opportunity identity and
cross-day state. Repeated observations UPSERT the same opportunity and produce SUPPRESS or
RESURFACE decisions. The watermark advances only after a successful run.

Delivery and source accounting have additional durable owners. Source receipts prevent duplicate
pull counts and cursor advancement. Logical-run finalization and delivery claims block automatic
resend after success or an uncertain result. These rules make recovery dependent on retained
evidence; they do not make a full same-day rerun safe. See [DATA.md](DATA.md).
