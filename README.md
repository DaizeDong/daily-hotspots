# daily-hotspots

Find frontier business opportunities with real signal behind them, every day; tier-push them to Discord and archive. LLM proposes, a deterministic gate disposes.

[![Claude Code Skill](https://img.shields.io/badge/Claude%20Code-Skill-orange?style=flat)](https://docs.anthropic.com/en/docs/claude-code)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Languages](https://img.shields.io/badge/Languages-EN%20%2F%20CN-blue?style=flat)](#languages)
[![Roadmap](https://img.shields.io/badge/Roadmap-v0.2.0-purple?style=flat)](ROADMAP.md)

[English](README.md) | [中文版](README_CN.md)

---

## ⭐ Read this first — the design philosophy

daily-hotspots exists for one job: surface **business opportunities that have real signal behind
them**, daily, without flooding you with noise. The single governing principle is **LLM proposes, a
deterministic gate disposes** — the model fans out across sources and proposes candidates and
scores, but a pure-Python, fail-closed gate makes the final ruling. From that follow four more:
≥2 independent ORIGINs (merge then count), own-the-seam/delegate-the-engine, 宁缺毋滥
(quality over quota), and durable idempotent state. A skill here is *proven* (T1–T9 pytest), not
*generated*.

📜 **[Read the full design philosophy -> PHILOSOPHY.md](PHILOSOPHY.md)**

---

## What it is (and isn't)

**It is** the daily orchestration product `market-intel` reserved: it owns cadence, a watchlist,
cross-day dedup, a reproducible scoring rubric, and tiered Discord delivery + a private archive.

**It is not** a research engine. It never re-implements search / verification / synthesis — it
delegates the deep work to `market-intel` (`scale=standard`) or `small-cap-deepdive`, behind four
fail-closed gates, ≤3-5 deep-dives/day.

## How it works (three-tier funnel)

1. **Tier-0 discovery** (cheap, no skill calls): parallel MCP fan-out (trend-pulse, HackerNews,
   Product Hunt, X/twitterapi, arXiv, GitHub; GDELT in a subagent), **plus the source-coverage lanes
   (v0.2.0)** — an **X KOL roster loop** (`get_user_last_tweets` over `roster.json` enabled tier-1
   handles, low pre-viral faves floor) and the **niche-community lanes** (linux.do / V2EX / CN feeds,
   RSS/JSON injection-safe). Every collected item is untrusted DATA. Normalize entities, merge
   cross-source, **keep only clusters with ≥2 distinct origins**; each evidence item carries an
   `origin_handle` / `origin_source` attribution tag.
2. **Score**: the model proposes five dims (track_fit / timing / feasibility / competition /
   executability) at temperature 0 with anchored samples; `scripts/score.py` aggregates
   deterministically (`Σwᵢdᵢ × confidence × freshness × track_weight`).
3. **Cross-day dedup + evolution** over the `schedule-reminder` base → NEW / SUPPRESS / RESURFACE.
4. **Selective deep-dive** (four gates) → `market-intel` / `small-cap-deepdive`.
5. **Verify gate → tiered push → archive**: `verify_gate.py` blocks malformed cards; ≥70 push now,
   the rest into the daily digest; `archive.py` appends a quality-gated `opportunities.jsonl` and a
   per-run `pulls-YYYY-MM.jsonl` (the yield denominator).
6. **Dual-track output (v0.2.0)**: ≥2-origin scored signals stay opportunity cards; single-origin
   community rumors render in a separate lightweight `## 社区脉搏` community-pulse section (labeled
   单源未验证, capped, no score / no deep-dive) that auto-upgrades to a card if a second origin
   corroborates the next day.
7. **Daily digest** via the Windows Task Scheduler (08:07) + an idempotent base item.
8. **Weekly signal-yield self-evolve** (`run.py --yield`, report-only until 7 days of history): replays
   the archive to auto-prune dead roster handles (reversible) and propose-add productive new voices
   (human-gated) — the roster stays honest over time. See `reference/roster-evolution.md`.

## Install

```
/plugin install github:DaizeDong/daily-hotspots
```

Or clone manually:

```bash
git clone https://github.com/DaizeDong/daily-hotspots.git ~/.claude/plugins/daily-hotspots
```

Three-step local activation (filesystem-only): (1) junction `skills/daily-hotspots` into
`~/.claude/skills/daily-hotspots`; (2) register the Windows task
(`scripts/register-task.ps1`); (3) optional — clone the private companion config repo and point
`$DAILY_HOTSPOTS_CONFIG` at it. Without the companion repo it runs on a built-in default config.

## Config

`daily-hotspots` is **config-bearing** (Mode B) — it reads per-user tuning (`watchlist.json`) and
per-machine secrets from a **separate, private** companion repo (`daily-hotspots-config`). Full
contract: [CONFIG.md](CONFIG.md).

- **Mount (discovery order):** `$DAILY_HOTSPOTS_CONFIG` → `~/.daily-hotspots-config/` →
  `~/.config/daily-hotspots-config/`. First that exists wins; absent = runs on built-in defaults.
- **First time:**
  ```bash
  python scripts/init_config.py        # stamp a conformant skeleton (deterministic)
  export DAILY_HOTSPOTS_CONFIG=~/.daily-hotspots-config   # or pass --out <dir> to init
  python scripts/verify_config.py       # doctor: PASS/FAIL, names what is missing
  ```
- **Switch configs (hot-swap):** point the env var at another config dir — configs are
  self-contained, no other change needed: `export DAILY_HOTSPOTS_CONFIG=~/configs/work` ↔
  `~/configs/personal`.
- **Secrets:** Mode B. `secrets/*` is gitignored and never enters git. Data-source keys reuse
  `companion-config`; there is no net-new secret, because push egress is the shared Agent Center
  `#hotspots` relay stream (schedule-reminder `relay.py`), not a dedicated bot.

## Dependencies (install-and-use)

daily-hotspots is an orchestration product — it delegates depth to sibling skills, and an install
brings them along (all junctioned + reachable; `verify_config.py` checks this and fails loud on a
missing one). Per the source-coverage design (spec §4/§12):

| Skill | Role here |
|---|---|
| **market-intel** | (a) Tier-1 deep-dive delegate. (b) **Single source-of-truth for source definitions** — linux.do / V2EX / CN feeds / X routes all live in its reference shards; this skill only references them. (c) Batch tool orchestration for the roster fan-out. Shares `companion-config` data-source keys. |
| **self-evolve** | Methodology frame for the weekly yield engine (methodology constant / signal adaptive / anti-self-deception verify gate). |
| **schedule-reminder** | Base ledger for cross-day dedup + the weekly yield / roster-review reminder item. |
| **small-cap-deepdive** | fintech-crypto track deep-dive branch. |

Install-and-use checklist: (1) sibling skills junctioned + reachable; (2) `companion-config`
data-source keys present (shared); (3) `config init → verify → first run` — `config init` **seeds
`roster.json`** with the Appendix A verified-live starter handles (review/curate from there).

## Quick start

```bash
# deterministic tail on prepared candidates (offline preview, no writes / no ledger):
python skills/daily-hotspots/scripts/run.py --in candidates.json --dry-run --no-ledger
# source-coverage self-evolve: write the pulls-log denominator, then the weekly yield pass:
python skills/daily-hotspots/scripts/run.py --sources sources.json        # origin-tag + pulls-log (§6)
python skills/daily-hotspots/scripts/run.py --yield --write-review        # weekly roster self-evolve (§8/§9)
# run the acceptance suite:
cd skills/daily-hotspots && python -m pytest tests/ -q
```

In Claude Code, just say **"跑一下 daily-hotspots"** / **"今天有什么前沿商业机会"** /
**"daily opportunity"**.

## Example output

A Discord card per high-score opportunity (grade + 5 dim scores + why-now + a non-consensus insight
+ an action + N independent sources), plus a daily digest committed to
`archive/digests/YYYY/YYYY-MM-DD.md`. On a quiet day: an honest "今日无合格机会" — no filler.

## Limitations

- The X roster ships **seeded** — `config init` writes the Appendix A verified-live starter handles
  into the companion `roster.json`, so the roster loop produces signal from the first run; review and
  curate it (the weekly yield engine then auto-prunes / proposes additions).
- Reddit uses the reddit-mcp-buddy **login tier** (authenticated 100/min, escapes the anon 403
  IP-block); supply the creds out-of-band. brightdata→old.reddit is a best-effort secondary only.
- twitterapi `get_trends` is broken upstream → uses `search_tweets`; **trend-pulse is marked dead**
  in config after it silently degraded — reconnect + verify non-empty before re-enabling.
- duckduckgo is hard-disabled (hangs). Web fallback order: brightdata > tavily > google-news.
- Push egress is the Agent Center `#hotspots` stream via schedule-reminder's `relay.py` (per-stream
  identity, registry-backed, Big Brother DM fallback if the base is absent). No dedicated bot.
- The signal-yield engine is **report-only until ≥7 days of real history** (cold-start honesty);
  auto-prune activates after week 1.
- **hardware-iot is a genuine roster gap** — no active founder roster found; needs a separate future
  surface (YouTube / vertical hardware forums), not fillable by an X roster alone.

## Languages

English (`README.md`, authoritative) · 中文 (`README_CN.md`)

## Roadmap · Contributing · License

See [ROADMAP.md](ROADMAP.md) · [CONTRIBUTING.md](CONTRIBUTING.md) · [LICENSE](LICENSE) (MIT).
