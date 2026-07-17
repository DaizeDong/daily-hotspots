# Step 3, Cross-day dedup + evolution (on the schedule-reminder base)

State lives in the `schedule-reminder` base, **frozen `api_version 1.0.0`**. Hard rules:
**subprocess only** (`reminder.py <verb> --json`), never read the `.db`, never build SQL; write
calls carry `--source daily-hotspots --idempotency-key --actor`; the DB must be **local NTFS**
(OneDrive/network = WAL corruption); `list` has no tag filter, so `list --source daily-hotspots
--active` then filter in-process by `ext`; there is no generic KV → the watermark is a singleton
item (`idempotency_key=daily-hotspots:watermark`, value in `ext`). `scripts/dedup.py:LedgerClient`
wraps all of this; locate `reminder.py` via `$DAILY_HOTSPOTS_REMINDER_CMD` or the default probe.

## Fingerprint (content-pure, never a timestamp/tracking param)

- **Hard key** = `canonical_key` (entity set ⊕ track) → used directly as `idempotency_key`, so an
  exact same opportunity UPSERTs (same id, ext merged) = built-in idempotency.
- **Soft match** (`dedup.match_existing`, pure, T3): for hard-key misses, **multi-signal** ,
  SimHash Hamming ≤ 3 OR token Jaccard ≥ cos_thr OR (strong shared-entity set + Jaccard ≥ 0.45),
  over the lookback window (default 7d, brute-force; a few hundred fingerprints need no LSH).
  Single-signal matching is forbidden (pure-semantic → "same words, different event" false merge;
  pure-string → misses rewrites). The entity-overlap guard is what blocks the false merge.

## Three-branch decision (`dedup.decide`, pure)

| branch | condition | action |
|---|---|---|
| **NEW** | no fingerprint match | score → if over floor push; store item (pending→doing) |
| **SUPPRESS** | match + no material change (small score delta + no new origin + same lifecycle) | update last_seen + append a score sample; **do not re-push** |
| **RESURFACE** | match + material change (lifecycle stage jump / score jump ≥ resurface_score_jump / a new origin crossing the ≥2 line) | push an **evolution UPDATE card** (delta + new sources), push_count+1 |

## ext namespace (`x_daily_hotspots_*`, MUST-PRESERVE round-trip)

`build_ext` writes canonical_key, simhash, text(≤400), first_seen, last_seen, last_score,
lifecycle_stage, source_set, push_count, and a `samples` ring buffer (capped, default 30):
`{ts,score,n_sources,velocity,stage}`. For anchorable keywords, fill `stage` from trend-pulse
`get_trend_velocity` + `get_lifecycle_prediction` (emerging/peak/declining/fading); else self-derive
velocity (score delta / days). 5 consecutive quiet days → transition `doing→done` (fading auto
close-out; drop from the lookback compare set).

## Watermark + idempotency

Collect `since = last_run_at - 5min` (clock-skew buffer; over-load + fingerprint dedup beats
missing a late arrival). **Write the watermark only after the whole run succeeds** (atomic); a
mid-run failure leaves it unmoved → next run re-covers, fingerprint UPSERT prevents a double-push.
Dedup IDs are content/source-derived, **never generated at processing time** (else replay mints new
IDs and breaks dedup).
