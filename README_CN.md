# daily-hotspots

每天发现有真实信号支撑的前沿商业机会，分级推送到 Discord 并归档。LLM 提候选，确定性闸门做终审。

[![Claude Code Skill](https://img.shields.io/badge/Claude%20Code-Skill-orange?style=flat)](https://docs.anthropic.com/en/docs/claude-code)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Languages](https://img.shields.io/badge/Languages-EN%20%2F%20CN-blue?style=flat)](#languages)
[![Roadmap](https://img.shields.io/badge/Roadmap-v0.1.2-purple?style=flat)](ROADMAP.md)

[English](README.md) | [中文版](README_CN.md)

---

## ⭐ 先读这里 — 设计理念

daily-hotspots 只做一件事：每天捞出**有真实信号支撑的商业机会**，且不拿噪音淹你。唯一统领原则是
**LLM 提候选，确定性闸门做终审**——模型多源扇出、提出候选与分数，但最终裁决由纯 Python、
fail-closed 的闸门做。由此派生四条：去重归并后 **≥2 独立 ORIGIN**（先归并再数源）、守接缝/委托引擎、
**宁缺毋滥**、状态持久且幂等。这里的 skill 是被**证明**过的（T1–T9 pytest），不是"生成完就算数"。

📜 **[完整设计理念 -> PHILOSOPHY.md](PHILOSOPHY.md)**

---

## 它是什么(不是什么)

**它是** market-intel 显式预留的每日 orchestration product：自持节奏(cadence)、关注清单(watchlist)、
跨日去重、可复现评分 rubric、Discord 分级推送 + 私有归档。

**它不是** 检索引擎。绝不重造检索/验证/合成——深活委托给 `market-intel`(`scale=standard`) 或
`small-cap-deepdive`，过四道 fail-closed 闸，每日深挖 ≤3-5 次。

## 工作原理(三层漏斗)

1. **Tier-0 发现**(廉价、不调 skill)：并行 MCP 扇出(trend-pulse / HackerNews / Product Hunt /
   X·twitterapi / arXiv / GitHub；GDELT 丢子代理)，实体归一化，跨源归并，**只留 ≥2 独立源的 cluster**。
2. **评分**：模型 temperature 0 + 锚定样例提出五维(赛道/时机/可行性/竞争/可执行性)；
   `scripts/score.py` 确定性聚合(`Σwᵢdᵢ × 置信 × 新鲜度 × 赛道权重`)。
3. **跨日去重 + 演化**(接 schedule-reminder 基座) → NEW / SUPPRESS / RESURFACE。
4. **选择性深挖**(四闸) → `market-intel` / `small-cap-deepdive`。
5. **验证闸 → 分级推送 → 归档**：`verify_gate.py` 拦截残缺卡；≥70 即时单推，其余进每日 digest；
   `archive.py` 质量闸后 append `opportunities.jsonl`。
6. **每日摘要**：Windows 计划任务(08:07) + 幂等基座 item。

## 安装

```
/plugin install github:DaizeDong/daily-hotspots
```

或手动克隆:

```bash
git clone https://github.com/DaizeDong/daily-hotspots.git ~/.claude/plugins/daily-hotspots
```

本地三步激活(纯文件系统)：(1) 把 `skills/daily-hotspots` junction 到
`~/.claude/skills/daily-hotspots`；(2) 注册 Windows 计划任务(`scripts/register-task.ps1`)；
(3) 可选——克隆私有配套 config 仓并把 `$DAILY_HOTSPOTS_CONFIG` 指过去。无配套仓则跑内置默认配置。

## 配置

`daily-hotspots` 是**带 config 的 skill**(Mode B)—— 它从一个**独立、私有**的配套仓
(`daily-hotspots-config`)读取每用户调参(`watchlist.json`)与每机器密钥。完整规范见
[CONFIG.md](CONFIG.md)。

- **挂载(发现顺序):** `$DAILY_HOTSPOTS_CONFIG` → `~/.daily-hotspots-config/` →
  `~/.config/daily-hotspots-config/`。命中第一个即用;都没有则跑内置默认。
- **首次配置:**
  ```bash
  python scripts/init_config.py        # 生成符合规范的骨架(确定性)
  export DAILY_HOTSPOTS_CONFIG=~/.daily-hotspots-config   # 或给 init 传 --out <dir>
  python scripts/verify_config.py       # doctor:逐项 PASS/FAIL,明确报缺什么
  ```
- **切换 config(即插即用):** 把环境变量指向另一个 config 目录即可 —— config 自包含,无需任何别的
  改动:`export DAILY_HOTSPOTS_CONFIG=~/configs/work` ↔ `~/configs/personal`。
- **密钥:** Mode B —— `secrets/*` 已 gitignore,永不入库;共享数据源密钥复用 `companion-config`,
  仅新增的 Discord 机器人 token 落在本地。请用库外备份。

## 快速开始

```bash
# 对准备好的候选跑确定性尾段(离线预览,不写盘/不接 ledger):
python skills/daily-hotspots/scripts/run.py --in candidates.json --dry-run --no-ledger
# 跑验收测试:
cd skills/daily-hotspots && python -m pytest tests/ -q
```

在 Claude Code 里直接说 **"跑一下 daily-hotspots"** / **"今天有什么前沿商业机会"** / **"每日热点"**。

## 示例输出

每条高分机会一张 Discord 卡(评级 + 五维分 + why-now + 一句非共识洞察 + 行动建议 + N 个独立源)，
外加一份每日 digest commit 到 `archive/digests/YYYY/YYYY-MM-DD.md`。安静日：诚实推
"今日无合格机会"——绝不灌水。

## 局限

- Reddit 本机 IP 级封锁 → 降级(HN/finnhub/brightdata 替身)。
- twitterapi `get_trends` 上游已坏 → 用 `search_tweets`。
- 硬禁 duckduckgo(会 hang)。Web 兜底顺序 brightdata > tavily > google-news。
- 独立 Discord bot token 可选；未设前复用现有 relay。

## 语言

中文 (`README_CN.md`) · English (`README.md`, 权威版)

## Roadmap · 贡献 · 许可

见 [ROADMAP.md](ROADMAP.md) · [CONTRIBUTING.md](CONTRIBUTING.md) · [LICENSE](LICENSE)(MIT)。
