# daily-hotspots

每天发现有真实信号支撑的前沿商业机会，每天一条头条推到 Discord，其余归档。LLM 提候选，确定性闸门做终审。

[![Claude Code Skill](https://img.shields.io/badge/Claude%20Code-Skill-orange?style=flat)](https://docs.anthropic.com/en/docs/claude-code)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Languages](https://img.shields.io/badge/Languages-EN%20%2F%20CN-blue?style=flat)](README.md)
[![Roadmap](https://img.shields.io/badge/Roadmap-v0.5.0-purple?style=flat)](ROADMAP.md)

[English](README.md) | [中文版](README_CN.md)

---

## ⭐ 设计哲学

daily-hotspots 只做一件事：每天捞出**有真实信号支撑的商业机会**，且不拿噪音淹你。唯一统领原则是
**LLM 提候选，确定性闸门做终审**,模型多源扇出、提出候选与分数，但最终裁决由纯 Python、
fail-closed 的闸门做。由此派生四条：去重归并后 **≥2 独立 ORIGIN**（先归并再数源）、守接缝/委托引擎、
**宁缺毋滥**、状态持久且幂等。T1 to T9 测试覆盖确定性机制，不能证明未经测试的部署具备实时信源访问、商业价值或送达能力。

独立信源门槛会漏掉尚未得到印证的早期机会，因此单源线索只进入标明未验证的社区脉搏，
不参与机会卡排名。持久回执增加存储与恢复工作，但避免失败后重跑时重复计数或盲目重发。

📜 **[完整设计理念 -> PHILOSOPHY.md](PHILOSOPHY.md)**

---

## 它是什么(不是什么)

**它是** market-intel 显式预留的每日 orchestration product：自持节奏(cadence)、关注清单(watchlist)、
跨日去重、可复现评分 rubric、每天一条 Discord 消息 + 私有归档。

**它不是** 检索引擎。绝不重造检索/验证/合成,深活委托给 `market-intel`(`scale=standard`) 或
`small-cap-deepdive`，过四道 fail-closed 闸，每日深挖 ≤3-5 次。

## 工作原理(三层漏斗)

1. **Tier-0 发现**(廉价、不调 skill)：并行 MCP 扇出(HackerNews / Product Hunt / X·twitterapi /
   arXiv / GitHub / reddit；GDELT 丢子代理)，X KOL **名单循环**(`get_user_last_tweets` 遍历
   `roster.json` 已启用 tier-1 handle,低 pre-viral faves 门槛)、**小众社区车道**(linux.do / V2EX /
   CN feeds,RSS/JSON 抗注入),外加挖真实未满足痛点的**需求车道**。每条采集物皆为不可信 DATA。实体
   归一化,跨源归并,**只留 ≥2 独立源的 cluster**;每条 evidence 带 `origin_handle` / `origin_source`
   归因标签。
2. **评分**：模型 temperature 0 + 锚定样例提出五维(赛道/时机/可行性/竞争/可执行性)；
   `scripts/score.py` 确定性聚合。供给卡与需求卡用**不同权重向量**,且聚合是**六个因子而非四个**,
   完整公式见 [`reference/scoring.md`](skills/daily-hotspots/reference/scoring.md)。
3. **跨日去重 + 演化**(接 schedule-reminder 基座) → NEW / SUPPRESS / RESURFACE。
4. **选择性深挖**(四闸) → `market-intel` / `small-cap-deepdive`。
5. **验证闸 → 每天一条头条 → 归档**：`verify_gate.py` 拦截残缺卡；当天合格卡合成**一条**排好序的
   双列消息(上限**按列**生效),`archive.py` 质量闸后 append `opportunities.jsonl`。
   yield 分母 `pulls-YYYY-MM.jsonl` 由 `run.py --sources` 写,不是 `archive.py`。
6. **双轨输出**：≥2 源的评分信号仍出机会卡；单源社区小道消息进独立的轻量
   `## 社区脉搏` 段(标 单源未验证,设上限,不评分/不深挖),次日若有第二独立源印证则自动升级为卡。
7. **每日摘要**：Windows 计划任务(08:07) + 幂等基座 item。digest 落盘是原子写,且拒绝用空日文本
   覆盖当天已有的真实 digest。
8. **每周信号产出自演化**：`run.py --yield --write-review` 只生成报告；加 `--apply` 才会停用账号。
   计划任务的 `yield-wrapper.ps1` 默认加 `--apply`，传 `-ReportOnly` 可只报告。新增账号仍需审核。见 `reference/roster-evolution.md`。

## 安装

```
/plugin install github:DaizeDong/daily-hotspots
```

或手动克隆:

```bash
git clone --recurse-submodules https://github.com/DaizeDong/daily-hotspots.git ~/.claude/plugins/daily-hotspots
```

初始化前先创建或克隆独立的 **PRIVATE GitHub 伴生仓**，将 `DAILY_HOTSPOTS_CONFIG` 指向它。
初始化器和 doctor 需要 Git，以及固定版本 Guards 接口认可的新鲜 PRIVATE 可见性凭据。
伴生仓必须已有提交；公开、未知或未纳入版本管理的目标会被拒绝。

本地启用时，先把 `skills/daily-hotspots` 链接到技能安装目录，初始化并维护 PRIVATE 伴生仓，
检查就绪后再按授权注册 Windows 计划任务。只读预览可以使用内置默认配置；运行写入需要
已初始化的伴生仓。初始化不会安装监测账号，先选定账号再启用拉取。

## 配置

`daily-hotspots` 是**带 config 的 skill**(Mode B), 它从一个**独立、私有**的配套仓
(`daily-hotspots-config`)读取每用户调参(`watchlist.json`)与每机器密钥。完整规范见
[CONFIG.md](CONFIG.md)。

- **挂载：** `DAILY_HOTSPOTS_CONFIG` 优先，`DAILY_HOTSPOTS_CONFIG_DIR` 为别名，随后使用 Guards 的统一发现规则。`DAILY_HOTSPOTS_DATA_DIR` 必须属于同一个伴生仓。完整顺序与目录布局见 [CONFIG.md](CONFIG.md#discovery-convention-e2)。
- **首次配置:**
  ```bash
  python scripts/init_config.py        # 生成符合规范的骨架(确定性)
  export DAILY_HOTSPOTS_CONFIG=~/.daily-hotspots-config   # 或给 init 传 --out <dir>
  python scripts/verify_config.py       # doctor:逐项 PASS/FAIL,明确报缺什么
  ```
- **切换 config(即插即用):** 把环境变量指向另一个 config 目录即可, config 自包含,同时清除或更新 DATA 覆盖值：`export DAILY_HOTSPOTS_CONFIG=~/configs/work` ↔ `~/configs/personal`。
- **密钥:** Mode B。`secrets/*` 默认被 gitignore 忽略，可按私有仓库政策入版本管理和备份，不得公开；数据源密钥复用 `companion-config`。
  本仓无 net-new 密钥:推送出口是共享的 Agent Center `#hotspots` relay 流(schedule-reminder `relay.py`),不用专用 bot。

## 依赖 skill(即插即用)

先分别安装 `market-intel`、`self-evolve`、`schedule-reminder` 和 `small-cap-deepdive`。
本插件只提供自身源码及 guard/style 子模块。按各仓的安装说明，把其规范 skill 目录链接到
技能根目录（默认 `~/.claude/skills`）；doctor 只检查目录是否可达，不代为安装。据信源覆盖设计(spec §4/§12):

| Skill | 在此的角色 |
|---|---|
| **market-intel** | (a) Tier-1 深挖委托方。(b) 它已收录的信源以它为准: X 访问路由与 CN feeds 都在它的 reference shard 里,本 skill 只引用不复制。**linux.do 与 V2EX 是刻意的例外**, market-intel 没有收录它们,所以这两个信源的定义自足地写在 [`reference/collect.md`](skills/daily-hotspots/reference/collect.md) §6。(c) 名单扇出的批量工具编排。共享 `companion-config` 数据源密钥。 |
| **self-evolve** | 每周 yield 引擎的方法论框架(方法论恒定 / 信号自适应 / 反自欺 verify 闸)。 |
| **schedule-reminder** | 跨日去重基座 ledger + 每周 yield / 名单复查提醒 item。 |
| **small-cap-deepdive** | fintech-crypto 赛道深挖分支。 |

即插即用清单:(1) 兄弟 skill 已 junction + 可达;(2) 共享 `companion-config` 数据源密钥就位;
(3) `config init → verify → 首跑`。初始化会在私有 DATA 路径创建空的 `roster.json`；
先自行选定账号，再启用账号拉取。生成器提供的合成账号只用于测试。

## 快速开始

```bash
# 对准备好的候选跑确定性尾段(离线预览,不写盘/不接 ledger):
python skills/daily-hotspots/scripts/run.py --in candidates.json --dry-run --no-ledger
# 信源覆盖自演化:先写 pulls-log 分母,再跑每周 yield:
python skills/daily-hotspots/scripts/run.py --sources sources.json        # 打 origin 标签 + 写 pulls-log(§6)
python skills/daily-hotspots/scripts/run.py --yield --write-review        # 每周名单自演化(§8/§9)
# 跑验收测试:
cd skills/daily-hotspots && python -m pytest tests/ -q
```

在 Claude Code 里直接说 **"跑一下 daily-hotspots"** / **"今天有什么前沿商业机会"** / **"每日热点"**。

## 示例输出

每天推一条排好序的**头条**消息(不是每条机会一张卡),**双列**布局:🎯 **需求机会** 打头(质量列,
从需求源、差评、招聘帖、小众论坛里挖出的非共识机会,每条带痛点原话 + 证据链接 + 拥挤度分),
后接一段紧凑的 📈 **供给热点**(基础广度)。完整 digest(全部字段 + 全部证据)commit 到
`archive/digests/YYYY/YYYY-MM-DD.md`,并作为 完整版 链接附在头条末尾。需求侧评分弱化时机、
奖励持久痛点、惩罚拥挤度;需求卡要过更高的门槛,所以需求薄的那天就诚实留空,绝不灌水。
全天无货时:"今日无合格机会"。

## 局限

- X 运行名单初始为空。请先在私有伴生仓中选定要监测的账号，再启用账号拉取。
  测试样本由生成器提供，包含覆盖六条赛道的 49 个合成账号，不会安装为运行名单。
- **信源的死活是配置,不是代码。** trend-pulse 静默降级后已标记 dead;twitterapi `get_trends`
  上游已坏,该车道改用 `search_tweets`;reddit 走免鉴权的 arctic-shift 归档(reddit-mcp-buddy 被网络
  封锁且只有匿名档);duckduckgo 因会 hang 被硬禁。逐源状态、路由与坑集中在
  [`reference/collect.md`](skills/daily-hotspots/reference/collect.md) 一张表里,这份清单会漂,那张表不会。
- 推送出口=Agent Center `#hotspots` 流(经 schedule-reminder `relay.py`),无专用 bot。交给 relay 前
  头条文本先过一道出口 PII 脱敏,细节见
  [`reference/push-archive.md`](skills/daily-hotspots/reference/push-archive.md);vendored 的
  Tier1/Tier2 核心与 `demand-mining` 逐字保持同步。
- 信号产出引擎**满 7 天真实历史前只报告**;分子(归档账本)读不可信时同样只报告、不下线任何 handle。
- hardware-iot 的覆盖取决于私有运行名单和启用的信源。初始化不会安装任何监测账号；
  可按需求补充视频或垂直硬件论坛等来源。
- R6 赛道 bandit 现在有入口了:`run.py --bandit` 跑一次,或者在配置里把 `scoring.bandit.enabled`
  置为 true 长期打开,而且开启后这一轮抽了哪些数都会写进结果里。默认仍然是关的,所以默认运行与
  静态赛道权重逐字一致。
- `run.py --sources` 会冻结本轮账号批次，保存信号和成功拉取回执，并据此推进一次轮转游标。
  重放不会重复消耗同一批次；部分失败保留未完成部分。存储或回执失败需要先核对已落盘状态，
  不能把命令失败直接当成整轮从未执行。

运行写入通过 Git 和固定版本 Guards 接口验证独立 PRIVATE 伴生仓、已提交历史及新鲜可见性凭据。
具体目标必须允许纳入 Git，包括锁和临时文件；被忽略的目标会被拒绝。源计划、游标与回执和
拉取账本一起保留在伴生仓。中断留下的源锁须先核对活动和所有者，不能盲目删除。

每次非预览交付先在归档的 `delivery-claims/` 保留逻辑 run ID。成功和结果不确定的运行
都会保留预约；同 ID 重试会在交付前停止。中断后先检查预约和下游回执，改输入不代表可以重发。
Dry run 不保留交付预约。

收集过程默认使用 PRIVATE 伴生仓的 `archive/workspaces/<run-id>/` 临时工作区。
运行成功后，包装脚本把 `candidates.json` 和 `result.json` 的原样副本保留在
`archive/runs/`，再清理已完成的工作区。交接预约保存在工作区外的 `archive/finalizations/`；
旧版工作区中的预约会先迁移，删除临时文件不会放开同一次运行的重发限制。
证据缺失、冲突、超限或结果不确定时，会保留工作区并报告失败。
候选交接与归档共用 20,000,000 字节上限，结果文件上限为 1 MiB。

包装脚本在收集前验证 PRIVATE 存储，支持 `archive/` 和 `data/archive/` 两种布局。
`DAILY_HOTSPOTS_RUN_ROOT` 可另选 PRIVATE 版本化位置；自动清理要求它与归档位于同一伴生仓。
失败运行的工作区留待检查。产物用途、保留条件和手动清理命令见
[DATA.md](DATA.md) 与 [storage.contract.json](storage.contract.json)。

## 语言

中文 (`README_CN.md`) · English (`README.md`, 权威版)

## Roadmap · 贡献 · 许可

见 [ROADMAP.md](ROADMAP.md) · [CONTRIBUTING.md](CONTRIBUTING.md) · [LICENSE](LICENSE)(MIT)。
