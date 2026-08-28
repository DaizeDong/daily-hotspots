# daily-hotspots, Config

`daily-hotspots` is **config-bearing** (Mode B): it reads per-user tuning and per-machine secrets
from a **separate, PRIVATE companion config repo** (`daily-hotspots-config`) that you create.
Secrets never live in this skill repo. This file is the authoritative config contract (config-spec
E1). The skill **never hard-crashes on a missing config**, absent companion repo means it runs on
the built-in `DEFAULT_CONFIG` in `skills/daily-hotspots/scripts/lib.py`.

**The companion repo is a git repo, and it is meant to be.** It is private, and `wrapper.ps1`
`git add`s, commits and pushes its `archive/` after every successful daily run, so the archive has
history, diffs and an off-machine backup, and so the digest's 完整版 link resolves. What stays out
of git is `secrets/*`, which is gitignored. Keeping the only record of your real run history as the
one directory on the machine with no history would not be safety.

There are **three artifacts** in the companion repo that you author:

1. `watchlist.json`, the single user-tunable surface, **deep-merged over** `DEFAULT_CONFIG`.
2. `roster.json`, **the X KOL roster** (the one genuinely-new data asset of the v0.2.0
   source-coverage design). `scripts/init_config.py` **seeds it** with the Appendix A verified-live
   handles (so a clean install is never dark); you then curate it, and the weekly signal-yield engine
   reads and reversibly mutates it. Schema below.
3. `registry.json`, Mode-B audit inventory of the data-source tools this skill talks to (optional;
   shared data sources reuse `companion-config`; there is no net-new secret, so `tools` ships empty).

Everything else under the config dir's `archive/` is **written by the skill**, not authored by you:
the opportunity ledger `opportunities.jsonl`, `dedup-state.json`, the daily digests under
`digests/YYYY/`, the yield denominator `pulls-YYYY-MM.jsonl` plus its `pull-errors-*` and
`collection-*` siblings, the review queue `roster-review.md`, and the monthly
`identity-sweep-YYYY-MM.json`. Which module writes which file, and what each one is for, is a table
in [`reference/push-archive.md`](skills/daily-hotspots/reference/push-archive.md); the roster loop
that consumes them is [`reference/roster-evolution.md`](skills/daily-hotspots/reference/roster-evolution.md).

---

## Discovery convention (how the skill finds your config), E2

`lib.find_config_dir()` resolves the config dir in this order; the first that exists wins:

1. `$DAILY_HOTSPOTS_CONFIG`, environment variable (recommended; location-independent).
2. `~/.daily-hotspots-config/`, dotfile-in-home fallback.
3. `~/.config/daily-hotspots-config/`, XDG-style fallback (Linux/macOS).

If none resolves, `load_config()` returns the built-in defaults. **Reading config is optional and
never fatal.** (The probe order mirrors `market-intel`'s companion convention so the two can share a
config home.)

**Writing is not optional, and it hard-fails.** Every archive write (`archive.py`, `digest.py`,
`yield.py`, `run.py --sources`) resolves its destination through the shared vendored resolver
`tools/datadir.py`, which follows `$DAILY_HOTSPOTS_DATA_DIR`, then `$DAILY_HOTSPOTS_CONFIG`, then a
sibling `<repo parent>/daily-hotspots-config/`, then `~/.daily-hotspots-config/data/`, then
`~/.config/daily-hotspots-config/`, then `~/.daily-hotspots-data/`. If none of those exists,
`archive.resolve_archive_dir` raises `ArchiveDirNotInitialized` with the initialization command,
and it raises `datadir.DataDirInsideOwnRepo` if the destination would land inside this public repo.

There is no fallback, on purpose. The previous behavior returned `~/.daily-hotspots-config/archive`
and then `mkdir(parents=True)`'d it, so an uninitialized machine did not fail: it conjured a
companion config and started an opportunity ledger inside it, at a scattered home path with no
remote, no history and no backup, and "uninitialized" became indistinguishable from "initialized at
the default path". A freshly cloned public skill is SUPPOSED to be uninitialized. Pass
`--archive-dir` for a one-off run.

One inconsistency to know about: `roster.json` is still located by `lib.find_config_dir()` with a
`~/.daily-hotspots-config/roster.json` fallback, so the roster does not yet share the resolver the
archive writers use.

---

## Schema, `watchlist.json` (E1)

All top-level keys are **optional**; anything you omit keeps its `DEFAULT_CONFIG` value. Lists you
supply **replace** the default list (except `exclude`, which is UNION, see Guardrails).

The **authoritative default set is the code**, `lib.DEFAULT_CONFIG`, and you can print it rather
than trust this page:

```bash
python -c "import sys,json;sys.path.insert(0,'skills/daily-hotspots/scripts');import lib;print(json.dumps(lib.DEFAULT_CONFIG,ensure_ascii=False,indent=2))"
```

What follows is the SHAPE of the tunable surface with the shipped values at the time of writing. A
few knobs are read by a module with its own fallback and are not in `DEFAULT_CONFIG` yet; those are
marked `(not in DEFAULT_CONFIG)`. Semantics of the scoring block live in
[`reference/scoring.md`](skills/daily-hotspots/reference/scoring.md), the source lanes in
[`reference/collect.md`](skills/daily-hotspots/reference/collect.md).

```jsonc
{
  "schema_version": 1,                         // int, schema marker (1)

  "tracks": [                                  // array, opportunity tracks (REPLACES default set)
    {
      "id": "ai-agents",                       // str , stable id
      "label": "AI agents / dev tooling",      // str , human label
      "weight": 1.3,                           // float, track multiplier
      "keywords": ["agent", "mcp", "llm"],     // [str], match terms
      "enabled": true                          // bool, include this track
    }
    // The shipped set ends with a keyword-less catch-all track, `unclassified` (weight 1.0). It can
    // never win the keyword contest, and it exists so a candidate that matched nothing has a
    // truthful home instead of being filed under whichever track sits first. If you REPLACE the
    // track list, keep an equivalent and keep it LAST.
  ],

  "focus_topics": ["solo-founder-doable"],     // [str], themes that lift score
  "exclude":      ["memecoin", "nsfw"],        // [str], hard excludes (UNION with built-ins)
  "machine_types": ["tool-saas", "service"],   // [str], allowed business-model tags

  "scoring": {
    "weights": {                               // floats, ~Σ1, SUPPLY score mix
      "track_fit": 0.20, "timing": 0.25, "feasibility": 0.20,
      "competition": 0.15, "executability": 0.20
    },
    "demand_weights": {                        // floats, ~Σ1, DEMAND mix (pain-first)
      "track_fit": 0.10, "timing": 0.10, "feasibility": 0.25,
      "competition": 0.30, "executability": 0.25
    },
    "crowdedness_penalty": 0.7,                // float, demand-only haircut at crowdedness 100
    "demand_freshness_floor": 0.6,             // float, demand freshness never decays below this
    "min_score_to_surface_demand": 60,         // int, the higher demand bar; clamped reachable
    "demand_floor_premium": 5,                 // int, reported: the bar minus min_score_to_archive
    "max_demand_floor_premium": 10,            // int, how far above archive the bar may be pushed
    "crowdedness_mode": "dimension",           // str, INERT: score.py does not read it yet
    "crowdedness_blend": 0.5,                  // float, INERT, only used by "dimension"
    "demand_freshness_mode": "neutral",        // str, INERT: score.py does not read it yet
    "min_score_to_archive": 55,                // int, floored to default (guardrail)
    "min_score_to_push":    70,                // int, floored to default (guardrail)
    "min_score_to_deepdive": 80,               // int
    "min_independent_sources": 2,              // int, floored, >= 2 (guardrail)
    "freshness_half_life_h": 72,               // int, hours to half-decay
    "freshness_gravity": 1.8,                  // float, high-frequency tilt
    "lifecycle_weights": {                     // floats, window-closed downweight
      "emerging": 1.0, "peak": 0.9, "declining": 0.75, "fading": 0.55
    },
    "weight_regression": {                     // floats, re-weighting regression gate
      "max_tau": 0.25, "max_push_churn_frac": 0.20,
      "catastrophic_tau": 0.6, "catastrophic_churn_frac": 0.5
    },
    "bandit": {                                // track explore/exploit bandit (R6)
      "enabled": false,                        // bool (not in DEFAULT_CONFIG), the ONLY switch;
                                               //   literal true required. run.py must still pass
                                               //   process(persist_bandit=bandit.bandit_enabled(cfg))
      "prior_alpha": 1.0, "prior_beta": 1.0,
      "explore_weight_lo": 0.5, "explore_weight_hi": 1.5,
      "reward_pushed": 1.0, "reward_archived": 0.6, "reward_blocked": 0.0
    },
    "dedup_cosine_threshold": 0.83,            // float, token Jaccard cutoff (rung A)
    "dedup_simhash_hamming": 3,                // int, SimHash Hamming cutoff (rung B)
    "dedup_char_ngram_threshold": 0.10,        // float (not in DEFAULT_CONFIG), CJK rung D cutoff
    "dedup_char_ngram_n": 3,                   // int (not in DEFAULT_CONFIG), n for rung D
    "lookback_days": 7,                        // int, compare-window outer bound (ENFORCED)
    "resurface_score_jump": 15,                // int, re-surface delta vs the last SURFACED score
    "samples_cap": 30,                         // int, ext samples ring-buffer cap
    "fading_quiet_days": 5                     // int, quiet days => fading + leaves the compare set
    // scoring.guardrail_notes is OUTPUT, not input: _clamp_guardrails writes every clamp that bit.
  },

  "sources": {                                 // per-source enable + tuning; recipes in collect.md
    "twitterapi": { "enabled": true, "roster_ref": "roster.json",  // str, companion roster file
                    "min_faves_rostered": 25,  // int, LOW faves floor for rostered handles;
                                               //   CAPPED at the 500 keyword floor it undercuts
                    "max_handles_per_run": 40 },// int?, per-run pull CAP; absent = no cap. When it
                                               //   bites, the plan rotates through roster.json's
                                               //   rotation_cursor and NAMES every dropped handle
    "linux.do": { "enabled": true, "fetch": "brightdata",          // brightdata scrape_as_markdown
                  "routes": ["/latest.rss", "/top.rss?period=daily"],  // ONLY these (RSS is injection-free; robots)
                  // two-layer content filter: category tags alone were wrong in BOTH directions
                  "keep_categories": ["前沿快讯", "开发调优", "资源荟萃"],
                  "keep_keywords": ["AI", "agent", "LLM", "模型", "开源", "MCP"],
                  "drop_keywords": ["抽奖", "红包", "羊毛", "求职"],
                  "keep_rule": "KEEP if (category in keep_categories OR title/body matches a keep_keyword) AND NOT any drop_keyword" },
    "v2ex":     { "enabled": true, "fetch": "webfetch",            // direct WebFetch (brightdata empty for V2EX)
                  "routes": ["/api/topics/hot.json", "/api/topics/latest.json"],
                  "keep_nodes": ["create", "programmer", "cloud", "geek",
                                 "claude", "openai", "claudecode", "vibecoding", "ai", "chatgpt"],
                  "drop_nodes": ["jobs", "all4all", "flamewar"] },
    "cn-feeds": { "enabled": true, "fetch": "webfetch",
                  "feeds": [{ "source": "qbitai", "url": "https://www.qbitai.com/feed", "label": "量子位" }] },
    "reddit":   { "enabled": true, "fetch": "arctic-shift",        // reddit-mcp-buddy is DEAD here
                  "api": "https://arctic-shift.photon-reddit.com/api/posts/search",
                  "subreddits": [{ "sub": "SaaS", "weight": 1.0 }],  // [{sub,weight}], weight by yield
                  "window_age_hours": [3, 30],  // [int,int], pull a SETTLED window, not the edge
                  "limit_per_sub": 25,
                  "drop_homoglyph": true, "drop_removed": true, "dedup_by": "author+title" },
    "trend-pulse": { "enabled": false }                            // marked dead (silently degraded)
  },

  "community_pulse": {                          // dual-track Track 2 (single-origin rumors)
    "enabled": true,
    "max_per_day": 8,                           // int, daily cap on the 社区脉搏 section
    "label": "⚠️ 单源未验证 · 社区小道消息",     // str, section label
    "community_sources": ["linux.do", "v2ex", "qbitai"],  // [str], which origins are pulse-eligible
    "max_age_hours": 72,                        // float, freshness bound for pulse eligibility
    "dedup_retention_days": 14                  // int, how long a seen pulse item stays suppressed
  },

  "yield": {                                    // signal-yield engine thresholds (tunable)
    "window_days": 30,                          // int, rolling yield window
    "floor": 0,                                 // int, contributions at/below => prune candidate
    "prune_after_weeks": 2,                     // int, consecutive below-floor weeks before prune
    "min_observed_days_per_week": 0,            // int 0..7, ABSOLUTE coverage bar a week must meet
                                                //   before it may count toward a prune. 0 = rely on
                                                //   the relative bar (this handle's own best-covered
                                                //   week). Capped at 7; above that nothing prunes
    "min_history_days": 7,                      // int, report-only until this much real history
    "propose_add_min_count": 2,                 // int, min evidence count to propose a non-roster handle
    "pre_viral_faves_threshold": 500,           // int, the keyword floor a rostered pull undercuts
    "noisy_pull_min": 10,                       // int, high-pull cutoff for a topic_filter suggestion
    "noisy_yield_max": 0.1                      // float, low-yield cutoff for the same
  },

  "push": { "channel": "discord-relay", "max_per_day": 5 },   // str + int

  "delegation": {                              // sub-skill delegation
    "market-intel": { "enabled": true, "scale": "standard", "daily_cap": 4 }
  }
}
```

A **safe minimal** `watchlist.json` is just `{ "schema_version": 1 }`, a no-op that inherits every
default. `init_config.py` stamps exactly that; edit it to tune.

### Guardrails (rails only TIGHTEN, never loosen)

`lib._clamp_guardrails()` re-imposes the built-in defaults as a **floor** after the merge:
`min_independent_sources`, `min_score_to_archive`, `min_score_to_push` are clamped to `max(user,
default)`, and `exclude` is the **UNION** of your list with the built-ins. You can make a rail
stricter; you can never weaken it below the shipped baseline.

---

## Schema, `roster.json` (v0.2.0 X KOL roster)

The one genuinely-new **data asset** the source-coverage design turns on. `scripts/init_config.py`
seeds it (Appendix A verified-live handles) so a fresh install ships it populated, not dark; you
curate from there, and the weekly signal-yield engine (`run.py --yield`) reversibly mutates it
(auto-prune sets `enabled=false`, never deletes) and proposes additions into
`archive/roster-review.md` for your approval. Referenced by `watchlist.json`
`sources.twitterapi.roster_ref`.

```jsonc
{
  "schema_version": 1,                          // int, schema marker
  "rotation_cursor": 0,                         // int?, written by roster.advance_rotation; where
                                                //   the next capped pull window starts. Absent = 0
  "entries": [                                  // array, one per tracked handle
    {
      "handle": "karpathy",                     // str , X handle, no @ (canonical form)
      "track": "ai-agents",                     // str , MUST match a watchlist track id (carries the track)
      "tier": 1,                                // int , 1 = pulled every run; 2 = reserve
      "enabled": true,                          // bool, auto-prune flips this to false (reversible)
      "topic_filter": "(AI OR coding OR ship)", // str?, optional; narrows a broad/noisy account
      "added_at": "2026-07-13T00:00:00Z",       // str , ISO8601 UTC
      "provenance": "seed",                     // str , seed | approved (approved = came via review queue)
      "notes": "audit-verified 2026-07-13"      // str?, optional freeform
    }
  ]
}
```

`init_config.py` seeds it from **Appendix A** of the design spec
(`docs/superpowers/specs/2026-07-13-source-coverage-design.md`): **49 live-verified starter handles**
(twitterapi `get_user_info` sweep 2026-07-13) across all six tracks, so a clean install is never
dark. The counts, which are what people actually want from this paragraph: ai-agents 10, dev-tools
11, saas-niche 8, fintech-crypto 8, consumer-social 6, hardware-iot 6. Hardware-iot is the thinnest
but it is **not empty**; a YouTube or vertical-hardware-forum surface remains the real fix for it
(spec Appendix B item 3), because an X roster alone does not reach that world.

Seed hygiene worth knowing before you edit it: drifted handles were corrected and dead accounts
(`statusesCount:0`) were dropped rather than seeded, and noisy high-follower accounts carry a
`topic_filter`. The seeded content is byte-identical to the parse-only sample at
`skills/daily-hotspots/tests/fixtures/roster.sample.json`, which is GENERATED from the installer
`ROSTER`.

The engine's guardrails over this file (auto-prune only, human-gated additions, unknown is not zero,
cold-start report-only, and the rails that only ever tighten) live in
[`reference/roster-evolution.md`](skills/daily-hotspots/reference/roster-evolution.md).

---

## Schema, `registry.json` (E1, optional audit inventory)

```jsonc
{
  "schema_version": 1,                 // int
  "spec_version": "1.0",               // str, config-spec version this inventory targets
  "companion_of": "daily-hotspots",    // str, owning skill
  "mode": "B",                         // str, secrets storage mode (B = gitignored + out-of-band)
  "tools": []                          // daily-hotspots has NO net-new secret: push egress is the
                                       // shared Agent Center #hotspots relay (schedule-reminder
                                       // relay.py); data-source keys reuse companion-config
}
```

Shared data-source tools (search / news / HN / etc.) are **not** duplicated here; they reuse
`companion-config`. This companion repo has no net-new secret of its own.

---

## Secrets, Mode B (E6)

The companion config repo is **separate and private**. `secrets/*` is **gitignored** (real values
never enter git; back them up out-of-band). This companion repo has **no net-new secret**: push
egress is the shared Agent Center `#hotspots` relay stream (schedule-reminder `relay.py`, which owns
its own webhook), not a dedicated bot.

Neither this skill repo nor the companion repo ever echoes a secret value.

**Shared data-source secrets are NOT duplicated here.** The twitterapi and brightdata keys reuse
`companion-config` (or env / `~/.claude.json`), and the reddit lane needs no credential at all now
that it reads the keyless arctic-shift archive. If a tool ever
does need a repo-local secret, create `secrets/<slug>.env` (UTF-8, no BOM) with the `KEY=VALUE` pairs
from its registry `env_vars` list.

---

## First-time setup (E3), succeeds on the first try

```bash
# 1. Stamp a conformant, empty companion config skeleton (deterministic, E4):
python scripts/init_config.py            # -> ~/.daily-hotspots-config/  (or pass --out <dir>)

# 2. Point the skill at it (skip if you used the default path):
export DAILY_HOTSPOTS_CONFIG=~/.daily-hotspots-config

# 3. Tune watchlist.json + add secrets, then confirm it is ready:
python scripts/verify_config.py          # doctor: PASS/FAIL per check, names what is missing
```

For the v0.2.0 source-coverage lanes: `init_config.py` already **seeded `roster.json`** (Appendix A
starter handles, review/curate it, schema above); add the `sources.*` / `community_pulse` / `yield`
blocks to `watchlist.json`. `verify_config.py` validates the roster schema and probes dependency
reachability (sibling skills + MCPs), a missing dependency fails loud rather than silently degrading.

---

## Switching between two configs (hot-swap), E5

A config dir is **self-contained** (no hardcoded absolute paths). Keep as many as you like and switch
by repointing the env var, no other change:

```bash
export DAILY_HOTSPOTS_CONFIG=~/configs/work       # config A
export DAILY_HOTSPOTS_CONFIG=~/configs/personal   # config B, same skill, different state
```

Verify the swap: `python scripts/init_config.py --out ~/configs/work` and
`--out ~/configs/personal`, run `verify_config.py --config-dir <each>`, then flip
`$DAILY_HOTSPOTS_CONFIG` between them, both must verify READY.
