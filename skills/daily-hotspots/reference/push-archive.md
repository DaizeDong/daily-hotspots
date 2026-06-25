# Step 5 — Verify gate → tiered push → archive

## Verify gate (fail-closed, final veto)

`scripts/verify_gate.py:validate_card` BLOCKS any card missing: track · 5 score_breakdown dims
(each 0-100) · final_score in [0,100] · ≥2 well-formed evidence{url,source,ts} ·
independent_source_count ≥ min · why_now · action. A blocked card is **never pushed, never archived**
— it returns as an explicit gap, not a silent pass (T9). `gate_batch` then buckets pushable /
archivable / digest_only and flags `empty_day` (T6: only items over the score floor are pushable;
zero filler).

## Tiered push (anti-spam; 宁缺毋滥)

- **Immediate channel**: single card with FinalScore ≥ `min_score_to_push` (default 70; flagship
  ≥80) AND distinct ORIGIN ≥2 → one embed now. Pre-push dedup: an ONGOING (already-pushed) one is
  not re-pushed; it gets a one-liner in the digest only.
- **Digest channel**: the rest over the archive floor wait for the cron and aggregate into one
  multi-embed message (≤10 cards/msg; overflow → split or attach the md).
- **Daily immediate cap** ≤5; overflow demotes into the digest.

`scripts/push_card.py` builds both a Discord embed (color by grade: ≥80 red/orange, else blue/grey;
5 inline dim fields; NEW-vs-UPDATE tag) and a plain-text rendering, and **validates Discord hard
limits before sending** (embed ≤6000 / ≤25 fields / field.value ≤1024 / ≤10 embeds / content ≤2000;
over-limit → split or degrade to a markdown attachment).

### Delivery seam (clean bot switch, zero code change)

`DAILY_HOTSPOTS_RELAY_CMD` (JSON list / shell string) takes the message on argv/stdin; else fallback
to `the standalone relay`. The relay owns the token — `push_card.py` never reads or
echoes it. ⚠️ The shared relay's `config.json` bot_token is plaintext & flagged leaked: before using
a dedicated hotspots bot, **Reset Token** in the Discord dev portal and put the new token only in the
companion repo's gitignored `secrets/discord-hotspots.env`, then point `DAILY_HOTSPOTS_RELAY_CMD`
at a sender that reads it.

## Archive (private companion repo opportunity store)

`scripts/archive.py:archive_card` re-asserts the quality gate (distinct ORIGIN ≥2 AND score ≥
`min_score_to_archive`, default 55) and only then appends — a low-quality card is mechanically
**refused** (宁缺毋滥). It writes:
- `archive/opportunities.jsonl` — append-only canonical store (git history = backup).
- `archive/dedup-state.json` — fingerprint → {first_seen, last_seen, push_count, cluster_id}.
- `archive/digests/YYYY/YYYY-MM-DD.md` — the human digest (written by `digest.py`; same artifact is
  both pushed and committed; top line carries the coverage stats).

jsonl record fields: opportunity_id, canonical_key, cluster_id, first/last_seen, status, title,
summary, track, focus_tags, machine_type, score, grade, score_breakdown, why_now,
contrarian_insight, action, evidence[], independent_source_count, pushed, push_count,
delegated_deepdive, lifecycle_stage, run_id, schema_version.
