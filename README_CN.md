# daily-hotspots

面向产品开发者的每日商业机会分析工具：采集来源证据，筛选并排序得到独立来源印证的机会，向 Discord 发送头条摘要，在私有仓库保留完整记录。

[![Claude Code Skill](https://img.shields.io/badge/Claude%20Code-Skill-orange?style=flat)](https://docs.anthropic.com/en/docs/claude-code)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Languages](https://img.shields.io/badge/Languages-EN%20%2F%20CN-blue?style=flat)](README.md)
[![Roadmap](https://img.shields.io/badge/Roadmap-v0.5.0-purple?style=flat)](ROADMAP.md)

[English](README.md) | [中文版](README_CN.md)

---

## ⭐ 设计哲学

模型提出候选和分数，确定性 Python 检查负责核对结构、证据与门槛。先归并信号，再计算独立来源，
同一报道的转载不能满足排名卡所需的两个独立来源。

这一要求可能遗漏尚未得到印证的早期机会，因此单源线索保留在标明未验证的社区脉搏中。
没有合格机会时允许输出空日。持久化的来源和交付回执支持中断恢复，避免重复计算拉取记录或重发
结果不明的消息。T1 至 T9 测试覆盖确定性机制；实时访问、送达和商业价值需要另行验证。
详见 [PHILOSOPHY.md](PHILOSOPHY.md)。

## 它是什么(不是什么)

daily-hotspots 负责每日调度、关注清单、跨日去重、评分、交付和私有归档。深入调研使用
`market-intel`（`scale=standard`）或 `small-cap-deepdive`。调用方须执行四项委托检查，
每天深挖预算为 3 至 5 次；这些是工作流要求，尚无 Python 自动执行的准入检查。
详见[委托说明](skills/daily-hotspots/reference/delegation.md)。

## 工作原理(三层漏斗)

1. **Tier-0 发现**(廉价、不调 skill)：并行 MCP 扇出(HackerNews / Product Hunt / X·twitterapi /
   arXiv / GitHub / reddit；GDELT 丢子代理)，X KOL **名单循环**(`get_user_last_tweets` 遍历
   `roster.json` 已启用 tier-1 handle,低 pre-viral faves 门槛)、**小众社区车道**(linux.do / V2EX /
   CN feeds,按不可信输入处理 RSS/JSON),外加挖真实未满足痛点的**需求车道**。每条采集物皆为不可信 DATA。实体
   归一化,跨源归并,**只留 ≥2 独立源的 cluster**;每条 evidence 带 `origin_handle` / `origin_source`
   归因标签。
2. **评分**：模型 temperature 0 + 锚定样例提出五维(赛道/时机/可行性/竞争/可执行性)；
   `scripts/score.py` 确定性聚合。供给卡与需求卡用**不同权重向量**,聚合使用**六个因子**,
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
- **密钥：** 模板默认使用 Mode B，`secrets/*` 被 gitignore 忽略，需要单独备份。明确选择 Mode A 后，可在已验证为 PRIVATE 的 Git 仓中对凭据进行版本管理，并从该私有历史恢复。公开源码仓不得包含凭据；数据源密钥复用 `companion-config`。
  本仓无 net-new 密钥:推送出口是共享的 Agent Center `#hotspots` relay 流(schedule-reminder `relay.py`),不用专用 bot。

## 依赖 skill(即插即用)

先分别安装 `market-intel`、`self-evolve`、`schedule-reminder` 和 `small-cap-deepdive`。
本插件只提供自身源码及 guard/style 子模块。按各仓的安装说明，把其规范 skill 目录链接到
技能根目录（默认 `~/.claude/skills`）；doctor 只检查目录是否可达，不代为安装。

| Skill | 源码仓内的技能目录 |
|---|---|
| market-intel | `skills/market-intel` |
| schedule-reminder | `skills/schedule-reminder` |
| self-evolve | 仓库根目录 |
| small-cap-deepdive | 仓库根目录 |

Windows 下克隆依赖后，可用
`New-Item -ItemType Junction -Path "$HOME/.claude/skills/self-evolve" -Target "$HOME/CodesClaude/self-evolve"`
创建链接；将路径改为实际源码位置和客户端技能目录。信源覆盖设计（spec §4/§12）规定：

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

- 运行名单初始为空。生成器提供的测试名单包含六条赛道的 49 个合成账号，其中六个属于硬件赛道，
  仅用于测试。硬件覆盖取决于经过审核的账号与信源，可按需补充视频或垂直硬件论坛。
- [采集说明](skills/daily-hotspots/reference/collect.md) 按日期记录信源证据和路由，涵盖停用的
  trend-pulse、`get_trends`，访问降级的 reddit，待复验的 Brightdata，以及禁用的 duckduckgo。
  历史探针结果不能代替当前访问检查。
- yield 引擎在真实历史未满 7 天或归档分子不可信时只生成报告。`run.py --bandit` 或
  `scoring.bandit.enabled` 可启用赛道 bandit，并记录每次采样；默认关闭，继续使用静态权重。
- 头条发送前执行[推送与归档说明](skills/daily-hotspots/reference/push-archive.md)中的结构化 PII
  检查。其 Tier1/Tier2 核心与 `demand-mining` 保持逐字同步；本工具不在采集时脱敏公开来源内容。
- 恢复规则取决于操作类型。来源批次重放复用拉取回执，游标只推进一次；成功或结果不明的交付预约
  会阻止相同逻辑运行再次发送。重试前先核对预约、回执和锁的归属。

[DATA.md](DATA.md) 规定写入准入、两种归档布局、重放文件、工作区大小限制与清理条件。
[调度说明](skills/daily-hotspots/reference/cron-setup.md) 规定交接和完成检查；日报文件存在本身不能证明送达。

## 语言

中文 (`README_CN.md`) · English (`README.md`, 权威版)

## Roadmap · 贡献 · 许可

见 [ROADMAP.md](ROADMAP.md) · [CONTRIBUTING.md](CONTRIBUTING.md) · [LICENSE](LICENSE)(MIT)。
