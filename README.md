# daily-hotspots

A daily business-opportunity workflow for product builders. It collects source evidence, ranks corroborated opportunities, sends a headlines digest to Discord, and preserves the full record in a private archive.

[![Claude Code Skill](https://img.shields.io/badge/Claude%20Code-Skill-orange?style=flat)](https://docs.anthropic.com/en/docs/claude-code)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Languages](https://img.shields.io/badge/Languages-EN%20%2F%20CN-blue?style=flat)](README_CN.md)
[![Roadmap](https://img.shields.io/badge/Roadmap-v0.5.0-purple?style=flat)](ROADMAP.md)

[English](README.md) | [中文版](README_CN.md)

---

## ⭐ Design Philosophy

The model proposes candidates and scores; deterministic Python gates check their structure,
evidence and thresholds. Signals are merged before counting independent origins, so repeated
coverage of one story cannot satisfy the two-origin requirement for a ranked card.

This requirement can exclude early opportunities. Single-origin leads therefore remain in a
labeled, unverified community pulse. A quiet day may contain no qualifying opportunity. Durable
source and delivery receipts support recovery without counting the same pull twice or resending
an uncertain delivery. T1 to T9 tests cover deterministic mechanisms; live access, delivery and
business value require separate evidence. See [PHILOSOPHY.md](PHILOSOPHY.md).

## What it is (and isn't)

daily-hotspots owns the daily cadence, watchlist, cross-day deduplication, scoring, delivery and
private archive. Deep research uses `market-intel` (`scale=standard`) or `small-cap-deepdive`.
The operator applies four routing gates and a daily budget of 3 to 5 deep dives; these delegation
gates are workflow obligations, not Python-enforced admission checks. See the
[delegation reference](skills/daily-hotspots/reference/delegation.md).

## How it works (three-tier funnel)

1. **Tier-0 discovery** (cheap, no skill calls): parallel MCP fan-out (HackerNews, Product Hunt,
   X/twitterapi, arXiv, GitHub, reddit; GDELT in a subagent), an **X KOL roster loop**
   (`get_user_last_tweets` over `roster.json` enabled tier-1 handles, low pre-viral faves floor), the
   **niche-community lanes** (linux.do / V2EX / CN feeds, RSS/JSON parsing with untrusted-input checks), and a **demand
   lane** that mines unmet pain outside tech. Every collected item is untrusted DATA. Normalize
   entities, merge cross-source, **keep only clusters with ≥2 distinct origins**; each evidence item
   carries an `origin_handle` / `origin_source` attribution tag.
2. **Score**: the model proposes five dims (track_fit / timing / feasibility / competition /
   executability) at temperature 0 with anchored samples; `scripts/score.py` aggregates
   deterministically. Supply and demand cards use different weight vectors, and the aggregation uses
   six factors; the exact formula lives in
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
8. **Weekly signal-yield self-evolve**: `run.py --yield --write-review` reports proposals.
   Add `--apply` to prune; the scheduled `yield-wrapper.ps1` adds it unless `-ReportOnly`
   is selected. Proposed additions still require review. See `reference/roster-evolution.md`.

## Install

```
/plugin install github:DaizeDong/daily-hotspots
```

Or clone manually:

```bash
git clone --recurse-submodules https://github.com/DaizeDong/daily-hotspots.git ~/.claude/plugins/daily-hotspots
```

Create or clone a separate **PRIVATE GitHub companion** before running the initializer.
Set `DAILY_HOTSPOTS_CONFIG` to that clone. Initialization and the doctor require Git and
a fresh PRIVATE visibility receipt accepted by the pinned Guards API. The companion must
already have a committed HEAD. Public, unknown or unversioned destinations are rejected.

Local activation: link `skills/daily-hotspots` into the skill installation, initialize and
curate the PRIVATE companion, verify readiness, then register the Windows task if scheduled
operation is authorized. Read-only previews can use built-in defaults; runtime writes require
the initialized companion. No monitored accounts are installed by default.

## Config

`daily-hotspots` is **config-bearing** (Mode B), it reads per-user tuning (`watchlist.json`) and
per-machine secrets from a **separate, private** companion repo (`daily-hotspots-config`). Full
contract: [CONFIG.md](CONFIG.md).

- **Mount:** `DAILY_HOTSPOTS_CONFIG` → `DAILY_HOTSPOTS_CONFIG_DIR` → shared Guards discovery. `DAILY_HOTSPOTS_DATA_DIR` must belong to the same companion. See [CONFIG.md](CONFIG.md#discovery-convention-e2) for the exact layout and fallback order.
- **First time:**
  ```bash
  python scripts/init_config.py        # stamp a conformant skeleton (deterministic)
  export DAILY_HOTSPOTS_CONFIG=~/.daily-hotspots-config   # or pass --out <dir> to init
  python scripts/verify_config.py       # doctor: PASS/FAIL, names what is missing
  ```
- **Switch configs (hot-swap):** point the env var at another config dir, configs are
  self-contained, clear or update the DATA override with it: `export DAILY_HOTSPOTS_CONFIG=~/configs/work` ↔
  `~/configs/personal`.
- **Secrets:** the template defaults to Mode B with out-of-band backup. A selected Mode A
  policy may version credentials in verified PRIVATE Git; restore from that private history.
  Public source never contains credentials. Data-source keys reuse
  `companion-config`; there is no net-new secret, because push egress is the shared Agent Center
  `#hotspots` relay stream (schedule-reminder `relay.py`), not a dedicated bot.

## Dependencies (install-and-use)

Install the sibling skills separately before the doctor: `market-intel`, `self-evolve`,
`schedule-reminder`, and `small-cap-deepdive`. This plugin only installs its own source and
guard/style submodules. Use each sibling repository's installation instructions, then link its
canonical skill directory under the configured skills root (default `~/.claude/skills`).
Use the entrypoint directories below when creating links in your client's skill directory:

| Skill | Directory inside its source checkout |
|---|---|
| market-intel | `skills/market-intel` |
| schedule-reminder | `skills/schedule-reminder` |
| self-evolve | repository root |
| small-cap-deepdive | repository root |

For example, on Windows, after cloning the dependency, create its junction with
`New-Item -ItemType Junction -Path "$HOME/.claude/skills/self-evolve" -Target "$HOME/CodesClaude/self-evolve"`.
Use your actual checkout location and the corresponding client skill directory.
The doctor checks those directories; it does not install dependencies. Per the source-coverage design (spec §4/§12):

| Skill | Role here |
|---|---|
| **market-intel** | (a) Tier-1 deep-dive delegate. (b) Source-definition home for the sources it already carries: the X access routes and the CN feeds live in its reference shards and this skill only references them. **linux.do and V2EX are the deliberate exception**, market-intel does not catalog either, so their definitions are self-contained in [`reference/collect.md`](skills/daily-hotspots/reference/collect.md) §6. (c) Batch tool orchestration for the roster fan-out. Shares `companion-config` data-source keys. |
| **self-evolve** | Methodology frame for the weekly yield engine (methodology constant / signal adaptive / anti-self-deception verify gate). |
| **schedule-reminder** | Base ledger for cross-day dedup + the weekly yield / roster-review reminder item. |
| **small-cap-deepdive** | fintech-crypto track deep-dive branch. |

Install-and-use checklist: (1) sibling skills junctioned + reachable; (2) `companion-config`
data-source keys present (shared); (3) initialize, curate the roster, verify, then run.
The initializer creates an empty runtime roster; add the accounts you choose to monitor.

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

- Runtime rosters start empty. The generated planner sample has 49 synthetic accounts across
  six tracks, including six hardware accounts, and is used only for tests. Hardware coverage
  depends on reviewed accounts and sources; video and vertical hardware forums remain options.
- Source availability is recorded with dated evidence in the
  [collection reference](skills/daily-hotspots/reference/collect.md). Its routes account for
  disabled trend-pulse and `get_trends`, degraded reddit access, quarantined Brightdata and the
  duckduckgo prohibition. Recheck current access before relying on historical probe results.
- The yield engine remains report-only until it has 7 days of real history, and whenever the
  archive numerator cannot be trusted. `run.py --bandit` or `scoring.bandit.enabled` enables the
  optional track bandit and reports its draws; it is off by default, preserving static weights.
- Headline egress applies the bounded structured PII scrub described in
  [push and archive](skills/daily-hotspots/reference/push-archive.md). Its shared Tier1/Tier2
  core is kept byte-synchronized with `demand-mining`; source content is not redacted at ingest.
- Recovery is specific to each operation. Source-batch replay reuses durable pull receipts and
  advances its cursor once; a successful or uncertain delivery claim prevents another send for
  the same logical run. Inspect claims, receipts and lock ownership before retrying.

[DATA.md](DATA.md) defines write admission, the two supported archive layouts, retained replay
files, workspace limits and compaction. [Scheduling](skills/daily-hotspots/reference/cron-setup.md)
defines handoff and completion checks. A digest file alone does not prove delivery.

## Languages

English (`README.md`, authoritative) · 中文 (`README_CN.md`)

## Roadmap · Contributing · License

See [ROADMAP.md](ROADMAP.md) · [CONTRIBUTING.md](CONTRIBUTING.md) · [LICENSE](LICENSE) (MIT).
