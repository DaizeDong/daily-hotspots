# Step 5, Verify gate, headlines push, archive

## Verify gate (fail-closed, final veto)

`scripts/verify_gate.py:validate_card` BLOCKS any card missing: track, 5 `score_breakdown` dims
(each 0 to 100), `final_score` in [0,100], >=2 well-formed `evidence{url,source,ts}`,
`independent_source_count` >= min, `why_now`, `action`. A blocked card is never pushed and never
archived; it returns as an explicit gap, not a silent pass (T9).

`gate_batch` then buckets the survivors: `pushable` (over `min_score_to_push`, itself capped at
`push.max_per_day`), `archivable` (over the card's own side floor: `min_score_to_archive` for
supply, the higher `min_score_to_surface_demand` for demand), `digest_only` for the difference, and
`empty_day` when nothing is archivable. Only items over their floor can be pushed or archived, so
filler is mechanically impossible (T6).

`gate_batch` returns `below_floor`, one entry per card that passed schema validation and then missed
its side's score floor, and `over_push_cap` for cards that qualified but did not fit the daily cap.
Both reach the coverage line, so the digest can say `未达门槛 4` rather than nothing at all.

That reporting is load-bearing rather than cosmetic. `blocked` only ever held schema failures, so a
card that validated and fell under its floor used to vanish with every counter reading zero, and
that silence is precisely how the demand lane stayed dead for 45 days while looking like a run of
quiet days. When the count genuinely is not known (the gate did not report it, for instance on an
older result being replayed), the field renders as `未统计`, which is the honest answer and
deliberately not a zero: `nobody counted` and `there were none` must never look the same.

## Daily delivery, one 'headlines' message (宁缺毋滥)

The channel gets one message per day, not a message per card, built by `digest.build_headlines`
from the `archivable` bucket in a two-column layout.

**The cap is PER COLUMN.** `push.max_per_day` (default 5) is applied separately to the demand list
and to the supply list, both ranked by score, so a full day ships up to **twice** that many items
(5 demand plus 5 supply). Whatever the cap left behind is counted into the `另有 N 条见完整版`
footer, never dropped in silence.

Layout: a header carrying the date, the two column counts and the coverage line; then
🎯 **需求机会** (the quality column: numbered `**N.【领域】标题**`, a prose summary trimmed at a
sentence boundary to <=280 chars, the link, grade, score, origin count and 拥挤度); then a compact
📈 **供给热点** tail of terse one-liners. 领域 is the mapped human domain via `_TRACK_DOMAIN`
(`ai-agents` renders as `AI`), never the raw tool track. Each column prints its own honest-empty
line, and a fully quiet day says 今日无合格机会.

**完整版 link.** A footer links the day's full digest on GitHub (`digest.digest_github_url` derives
the blob URL from the archive repo's `origin` remote, read-only, no network). For that link to
resolve, `wrapper.ps1` commits `archive/` and pushes the companion repo after each successful run.

### One link chooser, so the three surfaces cannot disagree

`digest.choose_card_links(card, pool)` is THE single place a card's link is decided, and
`digest.build_markdown`, `digest.build_headlines` and `push_card.build_embed` all call it against
one batch-wide `digest.url_pool(cards)`. Reading `evidence[0]["url"]` directly is what let the
archived markdown, the pushed headline and the embed pick three different urls for one card.

It is pure, offline and deterministic. Candidates are ranked by shape first (a site root is heavily
penalized, an article-shaped path rewarded), then an aggregator penalty (HN item pages, Techmeme,
reddit comment permalinks, lobste.rs) and a smaller social penalty, then token overlap between the
title and the url slug plus the signal text; ties fall back to original evidence order. The
aggregator link is not thrown away, it comes back as `discussion` and renders as a secondary 讨论
link.

`validate_url` refuses a candidate for one of four reasons, each reported to the reader in Chinese:
`malformed` (not a single clean http(s) token), `site_root` (a bare homepage that does not point at
this event), `truncated` (a sibling url in the same batch extends this path without a `/` at the
cut), and `fabricated_id` (a status-shaped social url whose numeric segment is >=12 digits ending in
>=5 zeros). Truncation is pool-relative by design: with no sibling to compare against it cannot
fire, because asserting a cut with no evidence would be a fabrication of its own.

Rejections are never silent. The archive digest prints `⚠️ 拒收链接: <url> (<原因>)` per rejection and
says `全部候选被拒收` when a card loses every link; the pushed message tags the affected item and
adds a run-level `⚠️ 本次有 N 条证据链接被拒收` line. A malformed url is reported with an empty url so
an injection payload is never echoed back into the digest.

### Coverage line, "clean" and "did not check" are different strings

`digest.coverage_line(coverage, qualified)` is shared verbatim by the archive header and the pushed
message, and reads exactly these keys from the coverage dict `run.build_coverage` emits:
`signals_collected`, `sources_invoked` / `sources_available`, `sources_failed`, `candidates`,
`suppressed`, `below_floor`, `signals_unaccounted`. `qualified` is passed in by the caller.

Any value that is not a real integer, and any key named in `coverage["unmeasured"]`, renders as
**未统计**, never as `0`. That is the whole point: a placeholder zero reads as an observation, and
for 30 of 31 archived digests this line shipped the literal placeholder `(see SKILL run)`, which
asserted nothing while looking like a value. Under the line, `_render_dropped` LISTS what the run
dropped rather than only counting it: every failed source with its error, and every below-floor card
with its title, side, score and floor.

### Links without preview cards, and the length limit

Every url is wrapped in `<...>` (Discord's suppress-preview syntax) and the relay additionally sets
`SUPPRESS_EMBEDS`, so links stay clickable with no auto-preview card. `push_card.deliver` then
enforces Discord's 2000-char content limit through `split_for_discord`, which splits at message
boundaries (blank-line blocks first, then single lines) and rejoins with the separator it split on.
Only a single line longer than the budget is hard-cut, and the cut is announced inside the delivered
text. Every chunk is stamped 第 i/n 段, the split and any hard cut are reported in `deliver`'s
return detail, and a non-zero relay rc on ANY chunk makes the whole delivery a failure, so a partial
send can never be reported as a success.

### Delivery seam (Agent Center egress, zero code change)

`push_card._relay_cmd()` resolves the egress in three tiers: `DAILY_HOTSPOTS_RELAY_CMD` (JSON list
or shell string) if set; else schedule-reminder's `relay.py send --stream hotspots` when the base is
installed, which posts to the Agent Center `#hotspots` channel with per-stream identity; else the
machine-local adapter `~/.local/relay.py`, which speaks the same convention so the skill still works
standalone. The relay owns the webhook and token; `push_card.py` never reads or echoes it. There is
no dedicated bot.

**Egress PII scrub.** `scripts/redact.py:scrub_egress` runs on the headline text just before the
relay: it redacts dangerous structured types (email, phone, card, secret, ip, discord-id, invite) in
place while leaving evidence URLs and @handles intact, and logs one line when it fires. It never
aborts a delivery, and it is the sole PII guarantee on the push path (ingest is not redacted; the
content is public frontier signal).

## Archive (private companion repo)

`scripts/archive.py:archive_card` re-asserts the quality gate (distinct ORIGIN >=2 AND score >= the
card's floor) and only then appends. Everything the skill writes into the companion repo's
`archive/`, and who writes it:

| path | writer | what it is |
|---|---|---|
| `opportunities.jsonl` | `archive.py` | append-only canonical card store; the yield NUMERATOR |
| `dedup-state.json` | `archive.py` | fingerprint to `{first_seen,last_seen,push_count,cluster_id}` |
| `digests/YYYY/YYYY-MM-DD.md` | `digest.py` | the human digest; same artifact is pushed and committed |
| `pulls-YYYY-MM.jsonl` | `run.py --sources` | one line per pulled handle or source; the yield DENOMINATOR |
| `pull-errors-YYYY-MM.jsonl` | `run.py --sources` | failed pulls, deliberately a separate file so `load_pulls` cannot mistake them for denominator lines |
| `collection-YYYY-MM.jsonl` | `run.py --sources` | what the collection layer reported, replayed by `build_coverage` |
| `roster-review.md` | `yield.py` | the propose-add / un-prune review queue |
| `identity-sweep-YYYY-MM.json` | `identity_sweep.py` | the monthly `get_user_info` sweep |

The pulls-log is written by `run.py --sources`, not by `archive.py`. Skipping that call leaves the
yield engine permanently inert.

**The digest write hard-fails, it never degrades.** `digest.write_digest_file` writes to a temp file
in the same directory and `os.replace`s it onto the final path, so no reader ever sees a
half-written digest. If a digest for that date already exists with real content and the new content
is the empty-day text, it raises `digest.DigestClobberError` and keeps the existing file: on
2026-08-27 a manual re-run that collected nothing overwrote the scheduled digest that had. Every
other failure (permission, disk, encoding, an unreadable existing digest) propagates as well, so a
write that did not happen can never look like one that did. `run.py` currently records that
exception in `errors` rather than aborting the run.

jsonl record fields: opportunity_id, canonical_key, cluster_id, first/last_seen, status, title,
summary, track, focus_tags, machine_type, score, grade, score_breakdown, why_now,
contrarian_insight, action, evidence[], independent_source_count, pushed, push_count,
delegated_deepdive, lifecycle_stage, run_id, schema_version.
