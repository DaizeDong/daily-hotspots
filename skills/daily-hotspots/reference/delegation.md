# Step 4, Selective deep-dive (Tier-1 delegation)

Tier-1 ≈ 15× the token cost of Tier-0, so it must be **scarce and gated**. Default = do NOT upgrade.

## Four gates (ALL must pass, fail-closed)

1. **Evidence**: Tier-0 already has distinct ORIGIN ≥ 2.
2. **Score**: in today's Top-N AND `FinalScore ≥ min_score_to_deepdive` (default 80).
3. **Freshness**: not in the ledger's "deep-dived in the last K days" set (no re-grinding).
4. **Budget**: today's deep-dive count < daily cap (≤3-5; overflow → next day).

## Routing + call contract

- Commercial / track / product opportunity → **`market-intel`** (the `Skill` tool, isolated
  subagent).
- Small/micro-cap US-equity angle → **`small-cap-deepdive`** (`ticker <code>` / `theme <topic>`) ,
  reuse its 7-dim card + kill-flags + "hype = casino, find the real beneficiary" separation.
- No commercial-domain match → do not upgrade; the Tier-0 card archives as-is.

Brief rules: pass **only the opportunity-scoped sub-question** (not the whole digest); set
**`scale=standard`** explicitly (reserve `deep` for the rare flagship); require the standard
structured evidence unit back:
`{status, claims:[{claim,source_url,quote,source_tier(L1-L5),date,confidence}], coverage_notes}`.

## Bring the result back without telephone

The deep subagent writes the **full report to an artifact** (market-intel report-template /
small-cap `reports/smallcap/<date>`) and returns only a **light structured summary** that folds into
the card's "deep block" (post-upgrade verdict + risks & counter-evidence + confidence up/down +
report path). Never inline the long report. If fan-out > 5, insert a combiner layer (each combiner
merges 3-4 workers).

## Availability check + graceful degrade

Parse `claude mcp list` three states (only `✓ Connected` is usable) and confirm the
market-intel / small-cap junctions exist. market-intel unavailable → degrade to deep-research / web
and **mark the degrade on the card** (no silent downgrade). Any subagent that returns failed/empty
→ one rewrite + retry; still empty → list it as an explicit gap.

## Budget control (the #1 risk)

≤3-5 deep-dives/day; each inherits market-intel's ~40 tool-calls / ~6 rounds ceiling; `standard`
not `deep` by default; four gates fail-closed; the dedup ledger blocks re-grinding the same
opportunity. Keep this skill's own description lean (library budget ~15k chars / ~4k tokens, over
budget silently truncates the trigger set).
