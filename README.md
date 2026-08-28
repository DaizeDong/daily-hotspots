# daily-hotspots

Find frontier business opportunities with real signal behind them, every day; deliver one ranked headlines message to Discord and archive the rest. LLM proposes, a deterministic gate disposes.

[![Claude Code Skill](https://img.shields.io/badge/Claude%20Code-Skill-orange?style=flat)](https://docs.anthropic.com/en/docs/claude-code)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Languages](https://img.shields.io/badge/Languages-EN%20%2F%20CN-blue?style=flat)](#languages)
[![Roadmap](https://img.shields.io/badge/Roadmap-v0.5.0-purple?style=flat)](ROADMAP.md)

[English](README.md) | [中文版](README_CN.md)

---

## ⭐ Read this first, the design philosophy

daily-hotspots exists for one job: surface **business opportunities that have real signal behind
them**, daily, without flooding you with noise. The single governing principle is **LLM proposes, a
deterministic gate disposes**, the model fans out across sources and proposes candidates and
scores, but a pure-Python, fail-closed gate makes the final ruling. From that follow four more:
≥2 independent ORIGINs (merge then count), own-the-seam/delegate-the-engine, 宁缺毋滥
(quality over quota), and durable idempotent state. A skill here is *proven* (T1 to T9 pytest), not
*generated*.

📜 **[Read the full design philosophy -> PHILOSOPHY.md](PHILOSOPHY.md)**

---

## What it is (and isn't)

**It is** the daily orchestration product `market-intel` reserved: it owns cadence, a watchlist,
cross-day dedup, a reproducible scoring rubric, one daily Discord message, and a private archive.

**It is not** a research engine. It never re-implements search / verification / synthesis, it
delegates the deep work to `market-intel` (`scale=standard`) or `small-cap-deepdive`, behind four
fail-closed gates, ≤3-5 deep-dives/day.

## How it works (three-tier funnel)

1. **Tier-0 discovery** (cheap, no skill calls): parallel MCP fan-out (HackerNews, Product Hunt,
   X/twitterapi, arXiv, GitHub, reddit; GDELT in a subagent), an **X KOL roster loop**
   (`get_user_last_tweets` over `roster.json` enabled tier-1 handles, low pre-viral faves floor), the
   **niche-community lanes** (linux.do / V2EX / CN feeds, RSS/JSON injection-safe), and a **demand
   lane** that mines unmet pain outside tech. Every collected item is untrusted DATA. Normalize
   entities, merge cross-source, **keep only clusters with ≥2 distinct origins**; each evidence item
   carries an `origin_handle` / `origin_source` attribution tag.
2. **Score**: the model proposes five dims (track_fit / timing / feasibility / competition /
   executability) at temperature 0 with anchored samples; `scripts/score.py` aggregates
   deterministically. Supply and demand cards use different weight vectors, and the aggregation is
   six factors, not four; the exact formula lives in
   [`reference/scoring.md`](skills/daily-hotspots/reference/scoring.md).
3. **Cross-day dedup + evolution** over the `schedule-reminder` base → NEW / SUPPRESS / RESURFACE.
4. **Selective deep-dive** (four gates) → `market-intel` / `small-cap-deepdive`.
5. **Verify gate → one daily headlines message → archive**: `verify_gate.py` blocks malformed cards,
   then the day's qualifying cards go out as a single ranked two-column message (the cap applies per
   column) and `archive.py` appends a quality-gated `opportunities.jsonl`. The per-run
   `pulls-YYYY-MM.jsonl` yield denominator is written by `run.py --sources`, not by `archive.py`.
6. **Dual-track output**: ≥2-origin scored signals stay opportunity cards; single-origin community
   rumors render in a separate lightweight `## 社区脉搏` community-pulse section (labeled 单源未验证,
   capped, no score, no deep-dive) that auto-upgrades to a card if a second origin corroborates the
   next day.
7. **Daily digest** via the Windows Task Scheduler (08:07) plus an idempotent base item. The digest
   write is atomic and refuses to overwrite a real digest with an empty-day one.
8. **Weekly signal-yield self-evolve** (`run.py --yield`): replays the archive against the pulls-log
   to auto-prune dead roster handles (reversible) and propose-add productive new voices
   (human-gated). See `reference/roster-evolution.md`.

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
(`scripts/register-task.ps1`); (3) optional, clone the private companion config repo and point
`$DAILY_HOTSPOTS_CONFIG` at it. Step 3 is only optional for a read-only preview: without a
companion repo the skill still *loads* built-in default config, but every archive write hard-fails
with an initialization message rather than inventing a home for your ledger.

## Config

`daily-hotspots` is **config-bearing** (Mode B), it reads per-user tuning (`watchlist.json`) and
per-machine secrets from a **separate, private** companion repo (`daily-hotspots-config`). Full
contract: [CONFIG.md](CONFIG.md).

- **Mount (discovery order):** `$DAILY_HOTSPOTS_CONFIG` → `~/.daily-hotspots-config/` →
  `~/.config/daily-hotspots-config/`. First that exists wins; absent = built-in defaults for
  READS. Writes resolve separately through `tools/datadir.py` and raise when nothing resolves.
- **First time:**
  ```bash
  python scripts/init_config.py        # stamp a conformant skeleton (deterministic)
  export DAILY_HOTSPOTS_CONFIG=~/.daily-hotspots-config   # or pass --out <dir> to init
  python scripts/verify_config.py       # doctor: PASS/FAIL, names what is missing
  ```
- **Switch configs (hot-swap):** point the env var at another config dir, configs are
  self-contained, no other change needed: `export DAILY_HOTSPOTS_CONFIG=~/configs/work` ↔
  `~/configs/personal`.
- **Secrets:** Mode B. `secrets/*` is gitignored and never enters git. Data-source keys reuse
  `companion-config`; there is no net-new secret, because push egress is the shared Agent Center
  `#hotspots` relay stream (schedule-reminder `relay.py`), not a dedicated bot.

## Dependencies (install-and-use)

daily-hotspots is an orchestration product, it delegates depth to sibling skills, and an install
brings them along (all junctioned + reachable; `verify_config.py` checks this and fails loud on a
missing one). Per the source-coverage design (spec §4/§12):

| Skill | Role here |
|---|---|
| **market-intel** | (a) Tier-1 deep-dive delegate. (b) Source-definition home for the sources it already carries: the X access routes and the CN feeds live in its reference shards and this skill only references them. **linux.do and V2EX are the deliberate exception**, market-intel does not catalog either, so their definitions are self-contained in [`reference/collect.md`](skills/daily-hotspots/reference/collect.md) §6. (c) Batch tool orchestration for the roster fan-out. Shares `companion-config` data-source keys. |
| **self-evolve** | Methodology frame for the weekly yield engine (methodology constant / signal adaptive / anti-self-deception verify gate). |
| **schedule-reminder** | Base ledger for cross-day dedup + the weekly yield / roster-review reminder item. |
| **small-cap-deepdive** | fintech-crypto track deep-dive branch. |

Install-and-use checklist: (1) sibling skills junctioned + reachable; (2) `companion-config`
data-source keys present (shared); (3) `config init → verify → first run`, `config init` **seeds
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

One ranked headlines message per day in a **two-column** layout: 🎯 **需求机会** leads (the quality
column, non-consensus opportunities mined from demand sources, review complaints, job postings, niche
forums, each with a pain quote + evidence link + a crowdedness score), then a compact 📈 **供给热点**
tail (basic hotspots for breadth). The full digest (every field + all evidence) is committed to
`archive/digests/YYYY/YYYY-MM-DD.md`. Demand scoring de-emphasizes timing, rewards durable pain, and
penalizes crowdedness; a demand card clears a higher bar, so a thin demand day is honestly empty, no
filler. On a fully quiet day: "今日无合格机会".

## Limitations

- The X roster ships **seeded** (49 handles across all six tracks), so the roster loop produces
  signal from the first run; review and curate it, and the weekly yield engine then auto-prunes and
  proposes additions.
- **Dead and degraded sources are config, not code.** trend-pulse is marked dead after it silently
  degraded, twitterapi `get_trends` is broken upstream so the lane uses `search_tweets`, reddit runs
  on the keyless arctic-shift archive because reddit-mcp-buddy is network-blocked and anon-only, and
  duckduckgo is hard-disabled because it hangs. Per-source status, routes and gotchas are one table
  in [`reference/collect.md`](skills/daily-hotspots/reference/collect.md); this list will drift, that
  one will not.
- Push egress is the Agent Center `#hotspots` stream via schedule-reminder's `relay.py`. No dedicated
  bot. An egress PII scrub runs on the headline text just before the relay (see
  [`reference/push-archive.md`](skills/daily-hotspots/reference/push-archive.md)); its vendored
  Tier1/Tier2 core stays byte-synced with `demand-mining`.
- The signal-yield engine is **report-only until 7 days of real history**, and also whenever it
  cannot trust the numerator it would prune on.
- **hardware-iot is the thinnest track, not an empty one.** The installer seeds six hardware-iot
  handles. Reaching that world properly still needs a surface an X roster cannot provide (YouTube,
  vertical hardware forums).
- The R6 track bandit now has an entry point (`run.py --bandit`, or `scoring.bandit.enabled` for
  good), and it reports every draw it makes. It stays OFF by default, so a default run is still
  byte-identical to the static track weight.
- The roster pull-cap rotation cursor is still not switched on by any entry point: `run.py` never
  advances it, so a capped roster re-plans the same window every run.

## Languages

English (`README.md`, authoritative) · 中文 (`README_CN.md`)

## Roadmap · Contributing · License

See [ROADMAP.md](ROADMAP.md) · [CONTRIBUTING.md](CONTRIBUTING.md) · [LICENSE](LICENSE) (MIT).
