# Step 5 — Verify gate → tiered push → archive

## Verify gate (fail-closed, final veto)

`scripts/verify_gate.py:validate_card` BLOCKS any card missing: track · 5 score_breakdown dims
(each 0-100) · final_score in [0,100] · ≥2 well-formed evidence{url,source,ts} ·
independent_source_count ≥ min · why_now · action. A blocked card is **never pushed, never archived**
— it returns as an explicit gap, not a silent pass (T9). `gate_batch` then buckets pushable /
archivable / digest_only and flags `empty_day` (T6: only items over the score floor are pushable;
zero filler).

## Daily delivery — one 'headlines' message (2026-07 model; 宁缺毋滥)

The channel gets **one** message per day, not a message per card. `gate_batch` still buckets
pushable / archivable / digest_only and flags `empty_day` (only items over the score floor are
pushable); the top pushable cards (default ≤5, `push.headlines_cap`) are then rendered by
`digest.build_headlines` as a ranked list where each item is: a **bold** headline line
`**N.【领域】标题**` (领域 = the mapped human DOMAIN via `_TRACK_DOMAIN`, e.g. `ai-agents`→`AI`,
`fintech-crypto`→`金融/加密` — NOT the raw tool track), a natural-**prose summary** (≤280 chars,
sentence-boundary trimmed so it never ends mid-sentence), and the primary source **link wrapped in
`<...>`** (clickable, no preview card) followed by `grade score · N源`.
If nothing clears the push bar, the day's best `archivable` cards fill the headlines; a truly empty
day gets an honest "今日无合格机会" line, never filler. Already-pushed (ONGOING) opportunities are
not re-surfaced (cross-day dedup).

**Links without cards + no per-card embeds:** the old model pushed one Discord embed *per card* —
noisy, and every bare url spawned an auto link-preview card. The daily message now includes each
link but wraps it in `<...>` (Discord's suppress-preview syntax), and the relay additionally sets
`SUPPRESS_EMBEDS` (flags=4) as belt-and-suspenders — so links are clickable but no cards render.
Urls are validated to a single clean http(s) token (`_clean_url`); a url with whitespace/newline/
angle-brackets is dropped as junk-or-injection. `scripts/push_card.py:deliver()` sends the single
headline text through the relay; `build_embed` + the Discord hard-limit validators remain for a
future embed-capable bot but are **not** on the daily path.

### Delivery seam (Agent Center egress, zero code change)

`push_card.py:_relay_cmd()` resolves the egress in three tiers: (1) `DAILY_HOTSPOTS_RELAY_CMD` (JSON
list / shell string) if set; else (2) schedule-reminder's `relay.py send --stream hotspots` when the
base is installed, which posts to the Agent Center `#hotspots` channel with per-stream identity from
its own registry; else (3) the Big Brother relay `the standalone relay` so the skill still
works standalone. The relay owns the webhook/token; `push_card.py` never reads or echoes it. There is
no dedicated bot: notifications go to the shared Agent Center `#hotspots` channel, like the other skills.

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
