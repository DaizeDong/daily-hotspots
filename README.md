# daily-hotspots

Find frontier business opportunities with real signal behind them, every day; tier-push them to Discord and archive. LLM proposes, a deterministic gate disposes.

[![Claude Code Skill](https://img.shields.io/badge/Claude%20Code-Skill-orange?style=flat)](https://docs.anthropic.com/en/docs/claude-code)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Languages](https://img.shields.io/badge/Languages-EN%20%2F%20CN-blue?style=flat)](#languages)
[![Roadmap](https://img.shields.io/badge/Roadmap-v0.1.3-purple?style=flat)](ROADMAP.md)

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
   Product Hunt, X/twitterapi, arXiv, GitHub; GDELT in a subagent), normalize entities, merge
   cross-source, **keep only clusters with ≥2 distinct origins**.
2. **Score**: the model proposes five dims (track_fit / timing / feasibility / competition /
   executability) at temperature 0 with anchored samples; `scripts/score.py` aggregates
   deterministically (`Σwᵢdᵢ × confidence × freshness × track_weight`).
3. **Cross-day dedup + evolution** over the `schedule-reminder` base → NEW / SUPPRESS / RESURFACE.
4. **Selective deep-dive** (four gates) → `market-intel` / `small-cap-deepdive`.
5. **Verify gate → tiered push → archive**: `verify_gate.py` blocks malformed cards; ≥70 push now,
   the rest into the daily digest; `archive.py` appends a quality-gated `opportunities.jsonl`.
6. **Daily digest** via the Windows Task Scheduler (08:07) + an idempotent base item.

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
- **Secrets:** Mode B — `secrets/*` is gitignored and never enters git; shared data-source keys
  reuse `companion-config`, only the net-new Discord bot token lives locally. Back up out-of-band.

## Quick start

```bash
# deterministic tail on prepared candidates (offline preview, no writes / no ledger):
python skills/daily-hotspots/scripts/run.py --in candidates.json --dry-run --no-ledger
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

- Reddit is IP-blocked locally → degraded (HN/finnhub/brightdata stand-ins).
- twitterapi `get_trends` is broken upstream → uses `search_tweets`.
- duckduckgo is hard-disabled (hangs). Web fallback order: brightdata > tavily > google-news.
- A dedicated Discord bot token is optional; until set it uses the existing relay.

## Languages

English (`README.md`, authoritative) · 中文 (`README_CN.md`)

## Roadmap · Contributing · License

See [ROADMAP.md](ROADMAP.md) · [CONTRIBUTING.md](CONTRIBUTING.md) · [LICENSE](LICENSE) (MIT).
