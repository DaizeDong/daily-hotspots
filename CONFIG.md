# daily-hotspots — Config

`daily-hotspots` is **config-bearing** (Mode B): it reads per-user tuning and per-machine secrets
from a **separate, private companion config repo** (`daily-hotspots-config`) that you create and keep
out of git. Secrets never live in this skill repo. This file is the authoritative config contract
(config-spec E1). The skill **never hard-crashes on a missing config** — absent companion repo ⇒ it
runs on the built-in `DEFAULT_CONFIG` in `skills/daily-hotspots/scripts/lib.py`.

There are **three artifacts** in the companion repo:

1. `watchlist.json` — the single user-tunable surface, **deep-merged over** `DEFAULT_CONFIG`.
2. `roster.json` — **the X KOL roster** (the one genuinely-new data asset of the v0.2.0
   source-coverage design). `scripts/init_config.py` **seeds it** with the Appendix A verified-live
   handles (so a clean install is never dark); you then curate it, and the weekly signal-yield engine
   reads and reversibly mutates it. Schema below.
3. `registry.json` — Mode-B audit inventory of the data-source tools this skill talks to (optional;
   shared data sources reuse `companion-config`, only the net-new Discord bot token is local).

Two more files are **written by the skill** into the config dir's `archive/` (you do not author
them): `pulls-YYYY-MM.jsonl` (the per-run yield denominator) and `roster-review.md` (the propose-add /
un-prune review queue). See [`reference/roster-evolution.md`](skills/daily-hotspots/reference/roster-evolution.md).

---

## Discovery convention (how the skill finds your config) — E2

`lib.find_config_dir()` resolves the config dir in this order; the first that exists wins:

1. `$DAILY_HOTSPOTS_CONFIG` — environment variable (recommended; location-independent).
2. `~/.daily-hotspots-config/` — dotfile-in-home fallback.
3. `~/.config/daily-hotspots-config/` — XDG-style fallback (Linux/macOS).

If none resolves, `load_config()` returns the built-in defaults — config is optional, never fatal.
(The probe order mirrors `market-intel`'s companion convention so the two can share a config home.)

---

## Schema — `watchlist.json` (E1)

All top-level keys are **optional**; anything you omit keeps its `DEFAULT_CONFIG` value. Lists you
supply **replace** the default list (except `exclude`, which is UNION — see Guardrails). Example
showing every field with its type and default:

```jsonc
{
  "schema_version": 1,                         // int — schema marker (1)

  "tracks": [                                  // array — opportunity tracks (REPLACES default set)
    {
      "id": "ai-agents",                       // str  — stable id
      "label": "AI agents / dev tooling",      // str  — human label
      "weight": 1.3,                           // float — track multiplier
      "keywords": ["agent", "mcp", "llm"],     // [str] — match terms
      "enabled": true                          // bool — include this track
    }
  ],

  "focus_topics": ["solo-founder-doable"],     // [str] — themes that lift score
  "exclude":      ["memecoin", "nsfw"],        // [str] — hard excludes (UNION with built-ins)
  "machine_types": ["tool-saas", "service"],   // [str] — allowed business-model tags

  "scoring": {
    "weights": {                               // floats, ~Σ1 — composite score mix
      "track_fit": 0.20, "timing": 0.25, "feasibility": 0.20,
      "competition": 0.15, "executability": 0.20
    },
    "min_score_to_archive": 55,                // int — floored to default (guardrail)
    "min_score_to_push":    70,                // int — floored to default (guardrail)
    "min_score_to_deepdive": 80,               // int
    "min_independent_sources": 2,              // int — floored, >= 2 (guardrail)
    "freshness_half_life_h": 72,               // int — hours to half-decay
    "freshness_gravity": 1.8,                  // float — high-frequency tilt
    "lifecycle_weights": {                     // floats — window-closed downweight
      "emerging": 1.0, "peak": 0.9, "declining": 0.75, "fading": 0.55
    },
    "weight_regression": {                     // floats — re-weighting regression gate
      "max_tau": 0.25, "max_push_churn_frac": 0.20,
      "catastrophic_tau": 0.6, "catastrophic_churn_frac": 0.5
    },
    "bandit": {                                // floats — track explore/exploit bandit
      "prior_alpha": 1.0, "prior_beta": 1.0,
      "explore_weight_lo": 0.5, "explore_weight_hi": 1.5,
      "reward_pushed": 1.0, "reward_archived": 0.6, "reward_blocked": 0.0
    },
    "dedup_cosine_threshold": 0.83,            // float — semantic dedup cutoff
    "dedup_simhash_hamming": 3,                // int — SimHash Hamming cutoff
    "lookback_days": 7,                        // int — ledger lookback window
    "resurface_score_jump": 15,                // int — re-surface delta
    "samples_cap": 30,                         // int — bandit samples cap
    "fading_quiet_days": 5                     // int — days quiet => fading
  },

  "sources": {                                 // v0.2.0 source-coverage — per-source enable + tuning
    "twitterapi": { "enabled": true, "roster_ref": "roster.json",  // str — companion roster file
                    "min_faves_rostered": 25,  // int — LOW faves floor for rostered handles (pre-viral)
                    "max_handles_per_run": 40 },// int? — optional per-run pull CAP (cost/rate guardrail); absent = no cap
    "linux.do": { "enabled": true, "fetch": "brightdata",          // brightdata scrape_as_markdown
                  "routes": ["/latest.rss", "/top.rss?period=daily"],  // ONLY these (RSS is injection-free; robots)
                  "keep_categories": ["前沿快讯", "开发调优"] },
    "v2ex":     { "enabled": true, "fetch": "webfetch",            // direct WebFetch (brightdata empty for V2EX)
                  "routes": ["/api/topics/hot.json", "/api/topics/latest.json"],
                  "keep_nodes": ["create", "programmer", "cloud", "geek"],
                  "drop_nodes": ["jobs", "all4all", "flamewar"] },
    "cn-feeds": { "enabled": true, "fetch": "webfetch",
                  "feeds": [{ "source": "qbitai", "url": "https://www.qbitai.com/feed", "label": "量子位" }] },
    "reddit":   { "enabled": true, "auth_tier": "login" },         // login tier escapes the anon 403
    "trend-pulse": { "enabled": false }                            // marked dead (silently degraded)
  },

  "community_pulse": {                          // v0.2.0 dual-track Track 2 (single-origin rumors)
    "enabled": true,
    "max_per_day": 8,                           // int — daily cap on the 社区脉搏 section
    "label": "⚠️ 单源未验证 · 社区小道消息",     // str — section label
    "community_sources": ["linux.do", "v2ex", "qbitai"],  // [str] — which origins are pulse-eligible
    "rank_by": ["freshness", "community_heat"]  // [str] — ranking signals
  },

  "yield": {                                    // v0.2.0 signal-yield engine thresholds (tunable)
    "window_days": 30,                          // int — rolling yield window
    "floor": 0,                                 // int — contributions at/below => prune candidate
    "prune_after_weeks": 2,                     // int — consecutive below-floor weeks before prune
    "min_history_days": 7,                      // int — report-only until this much real history
    "propose_add_min_count": 2,                 // int — min evidence count to propose a non-roster handle
    "pre_viral_faves_threshold": 500,           // int — the keyword floor a rostered pull undercuts
    "noisy_pull_min": 10,                       // int — high-pull cutoff for a topic_filter suggestion
    "noisy_yield_max": 0.1                      // float — low-yield cutoff for the same
  },

  "push": { "channel": "discord-relay", "max_per_day": 5 },   // str + int

  "delegation": {                              // sub-skill delegation
    "market-intel": { "enabled": true, "scale": "standard", "daily_cap": 4 }
  }
}
```

A **safe minimal** `watchlist.json` is just `{ "schema_version": 1 }` — a no-op that inherits every
default. `init_config.py` stamps exactly that; edit it to tune.

### Guardrails (rails only TIGHTEN, never loosen)

`lib._clamp_guardrails()` re-imposes the built-in defaults as a **floor** after the merge:
`min_independent_sources`, `min_score_to_archive`, `min_score_to_push` are clamped to `max(user,
default)`, and `exclude` is the **UNION** of your list with the built-ins. You can make a rail
stricter; you can never weaken it below the shipped baseline.

---

## Schema — `roster.json` (v0.2.0 X KOL roster)

The one genuinely-new **data asset** the source-coverage design turns on. `scripts/init_config.py`
seeds it (Appendix A verified-live handles) so a fresh install ships it populated, not dark; you
curate from there, and the weekly signal-yield engine (`run.py --yield`) reversibly mutates it
(auto-prune sets `enabled=false`, never deletes) and proposes additions into
`archive/roster-review.md` for your approval. Referenced by `watchlist.json`
`sources.twitterapi.roster_ref`.

```jsonc
{
  "schema_version": 1,                          // int — schema marker
  "entries": [                                  // array — one per tracked handle
    {
      "handle": "karpathy",                     // str  — X handle, no @ (canonical form)
      "track": "ai-agents",                     // str  — MUST match a watchlist track id (carries the track)
      "tier": 1,                                // int  — 1 = pulled every run; 2 = reserve
      "enabled": true,                          // bool — auto-prune flips this to false (reversible)
      "topic_filter": "(AI OR coding OR ship)", // str? — optional; narrows a broad/noisy account
      "added_at": "2026-07-13T00:00:00Z",       // str  — ISO8601 UTC
      "provenance": "seed",                     // str  — seed | approved (approved = came via review queue)
      "notes": "audit-verified 2026-07-13"      // str? — optional freeform
    }
  ]
}
```

`init_config.py` seeds it from **Appendix A** of the design spec
(`docs/superpowers/specs/2026-07-13-source-coverage-design.md`) — verified-live starter handles mapped
to tracks (karpathy / swyx / simonw … for ai-agents; levelsio / garrytan / paulg … for dev-tools;
etc.), so you only need to review and curate. Notes on the seed: use `marclou` **not** `marc_louvion`
(404); `balajis` / `levelsio` carry a `topic_filter` (high-follower, noisy); `realGeorgeHotz` was
purged (`statusesCount:0`) and is left out; **hardware-iot has no active founder roster** and needs a
separate future surface. The seeded content matches the parse-only sample at
`skills/daily-hotspots/tests/fixtures/roster.sample.json`.

**Guardrails (anti-self-deception, spec §9):** the engine only ever **auto-prunes** (pure reversible
subtraction); **every addition is human-gated** (propose-add into the review queue). A handle with a
missing pulls-log entry gets `yield=unknown` (not 0) and is excluded from prune consideration. Nothing
is pruned until ≥ `yield.min_history_days` of real history exists (cold-start = report-only).

---

## Schema — `registry.json` (E1, optional audit inventory)

```jsonc
{
  "schema_version": 1,                 // int
  "spec_version": "1.0",               // str — config-spec version this inventory targets
  "companion_of": "daily-hotspots",    // str — owning skill
  "mode": "B",                         // str — secrets storage mode (B = gitignored + out-of-band)
  "tools": [                           // array (may be empty)
    {
      "slug": "discord-hotspots",      // str  — kebab-case; matches secrets/<slug>.env
      "installed": true,               // bool
      "matrix_origin": "net-new",      // str  — net-new | shared-with(market-intel)
      "domain": "push", "tier": "core",
      "transport": "discord-bot",      // str
      "health_last": null,             // str|null — last health check ISO ts
      "env_vars": ["DISCORD_HOTSPOTS_BOT_TOKEN", "DISCORD_HOTSPOTS_USER_ID"]
    }
  ]
}
```

Shared data-source tools (search / news / HN / etc.) are **not** duplicated here — they reuse
`companion-config`. Only the net-new Discord push bot has secrets local to this companion repo.

---

## Secrets — Mode B (E6)

The companion config repo is **separate and private**. `secrets/*` is **gitignored** — real values
never enter git; back them up out-of-band (cloud sync / encrypted drive). Per tool that needs
credentials, create `secrets/<slug>.env` (UTF-8, no BOM) with the `KEY=VALUE` pairs its
`env_vars` list. The only net-new secret here is the Discord bot:

```
# secrets/discord-hotspots.env   (gitignored)
DISCORD_HOTSPOTS_BOT_TOKEN=...
DISCORD_HOTSPOTS_USER_ID=...
```

Neither this skill repo nor the companion repo ever echoes a secret value.

**Shared data-source secrets are NOT duplicated here.** The v0.2.0 reddit **login-tier** credentials
and the twitterapi / brightdata keys reuse `companion-config` (or env / `~/.claude.json`) — the only
net-new secret local to this companion repo remains the Discord bot token.

---

## First-time setup (E3) — succeeds on the first try

```bash
# 1. Stamp a conformant, empty companion config skeleton (deterministic — E4):
python scripts/init_config.py            # -> ~/.daily-hotspots-config/  (or pass --out <dir>)

# 2. Point the skill at it (skip if you used the default path):
export DAILY_HOTSPOTS_CONFIG=~/.daily-hotspots-config

# 3. Tune watchlist.json + add secrets, then confirm it is ready:
python scripts/verify_config.py          # doctor: PASS/FAIL per check, names what is missing
```

For the v0.2.0 source-coverage lanes: `init_config.py` already **seeded `roster.json`** (Appendix A
starter handles — review/curate it, schema above); add the `sources.*` / `community_pulse` / `yield`
blocks to `watchlist.json`. `verify_config.py` validates the roster schema and probes dependency
reachability (sibling skills + MCPs) — a missing dependency fails loud rather than silently degrading.

---

## Switching between two configs (hot-swap) — E5

A config dir is **self-contained** (no hardcoded absolute paths). Keep as many as you like and switch
by repointing the env var — no other change:

```bash
export DAILY_HOTSPOTS_CONFIG=~/configs/work       # config A
export DAILY_HOTSPOTS_CONFIG=~/configs/personal   # config B — same skill, different state
```

Verify the swap: `python scripts/init_config.py --out ~/configs/work` and
`--out ~/configs/personal`, run `verify_config.py --config-dir <each>`, then flip
`$DAILY_HOTSPOTS_CONFIG` between them — both must verify READY.
