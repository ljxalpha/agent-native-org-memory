[English](README.md) | 中文

<div align="center">

# Agent 原生组织记忆架构

**`aomt`** — 把一支 AI agent 队伍，汇聚成一个不断复利的组织大脑。

[![CI](https://github.com/ljxalpha/agent-native-org-memory/actions/workflows/ci.yml/badge.svg)](https://github.com/ljxalpha/agent-native-org-memory/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

</div>

> **每个 agent 都在习得值得保留的东西——而你的组织对此一无所见。**
> aomt 把散落的 agent 记忆，变成实时的运营情报：共性故障、潜在风险、最佳实践、重复
> 造轮子，都自动浮现；而蒸馏出的经验，又回流给真正需要它的 agent。

你的公司里，正运行着一支不断壮大的 AI agent 队伍，而它们每一个都在**独自**学习。一个
agent 花两小时调试换来的教训，永远到不了明天撞上同一堵墙的另一个 agent；三个团队各自
独立踩中的风险，从未被命名；某位工程师的 agent 摸索出的更优解法，也从未扩散出去。
**aomt 正是那一层缺失的基础设施——它把这些散落、易逝的记忆，同时变成两样东西：一项回流
给每个 agent 的、可沉淀的组织资产，以及一幅集中于一处的实时图景——你的组织在学什么、卡在
哪里、又把什么做对了。**

---

## 问题：每个 agent 的记忆都是一座孤岛

<div align="center">
<img src="docs/images/problem.svg" alt="今天，agent 的记忆被困在孤岛里：每个“人 + agent”单元习得的教训都无法越过高墙；管理者只看得到碎片；agent 以分钟级完成工作，而知识在人与人之间的传递却以小时到天计。" width="900">
</div>

在 AI agent 出现之前，最小的工作单元是**人**，知识在人与人之间流动——很慢。如今，最小
单元变成了**“人 + 其 agent”**，单元*内部*的工作提速了一个数量级，但单元*之间*的通道
却没有升级。三个结构性缺陷随之而来：

- **人类又被拉回了关键路径。** 一个 agent 需要另一个团队的 agent 早已习得的东西，却要靠
  一个人来转述——几分钟的实际工作，要为几小时的等待而停摆。
- **每个 agent 的上限，就是某一个人的知识上限。** 它只知道自己的基座模型，加上与它协作
  的那唯一一个人恰好告诉它的内容。而组织里最有价值的知识——隐性的、跨域的、全局的视角
  ——恰恰从它身边彻底溜走。
- **来之不易的经验在蒸发。** 跨团队的知识只有在一个人写下来、*并且*另一个人主动去翻看时
  才会流动。现实里，这条链条几乎从不闭合。

孤岛化的 agent 记忆，不是一个小小的低效问题。它是新的组织瓶颈——而且你每加一个 agent，
它就更昂贵一分。

---

## 解法：一条回路，两种产出

<div align="center">
<img src="docs/images/architecture.svg" alt="aomt 架构（目标形态）：任何会产生记忆的 agent 都可以成为一个叶子节点；它自动、脱敏、带来源引用地，经由一个带审核/发布关卡的共享 Git 仓库向上汇报到蒸馏根；蒸馏根用可插拔的 LLM 聚类并蒸馏；蒸馏出的团队知识按相关性排序回流给每个 agent；同一份蒸馏还产出组织级洞察——浮现共性问题、提前预警潜在风险、扩散最佳实践、暴露效率瓶颈；整个网络可扩展为团队→部门→组织的树状结构，且 agent 原生、基于 Git、由配置驱动可插拔。" width="960">
</div>

aomt 汇聚你的 agent 本就在产生的工作记忆，用 LLM 将其蒸馏为**带来源引用、已做敏感信息
脱敏的组织知识**。同一条回路，产出两样东西。

**面向管理者 —— 你过去拿不到的组织级洞察。**
这份蒸馏会浮现出任何单个 agent 或个人都无法看到的模式：

| | |
|---|---|
| 🟡 **浮现共性问题** | 同一个故障同时击中三个团队——只需命名一次、修复一次 |
| 🟡 **提前预警潜在风险** | 同一个负面信号在众多 agent 的记忆里反复出现，即成早期预警 |
| 🟡 **扩散最佳实践** | 某位工程师更优的做法，迅速抵达整个组织 |
| 🟡 **暴露效率瓶颈** | 工作在哪里停滞、在哪里被跨团队重复建设，一目了然 |

**面向每个 agent —— 让知识回流。**
每个 agent（一个*叶子*）自动汇报其记忆中可共享的部分；*蒸馏根*将这些汇报聚类、精炼为团队
知识，再按相关性排序回流到每个 agent 的本地记忆中。没有人写文档，没有人抄笔记。
**蒸馏一次，回流所有。**

> **架构目标：任何会产生记忆的 agent 都可以成为叶子** —— 编码、调研、客服、运维，不局限于
> 编码 agent。当前 MVP 先用 CLI 编码 agent 跑通这条回路；随着各自的记忆载体可被接入，新的
> 领域只需编写一份 adapter YAML 即可插入。架构还可扩展为一棵树：一个团队的根，本身可以是
> 部门根的一个叶子，从而让组织的每一层都拥有属于它自身粒度的记忆。

### 为什么是现在，以及为什么会复利

agent 的执行已坍缩到分钟级；而知识在人与人之间的传递，仍需小时到天。你每多部署一个 agent，
这道鸿沟就更宽一分——而它恰恰就是 aomt 要填平的鸿沟。填平它还会复利：更多使用 → 更丰富的
agent 记忆 → 更优质的蒸馏知识 → 更聪明、更少需要人工干预的 agent → 更多使用。这是一个按
复利计息的飞轮：越早转起来，领先优势就越难被追上。

完整的论述——向“人 + agent”单元的转变、为何手工维护的知识库无法替代它、以及飞轮效应的
细节——都在 **[VISION.md](VISION.md)** 中。

---

## 当前已交付的能力

aomt 是一个**早期、可运行的 MVP**：设计已相当完备，实现正按里程碑逐步推进。当下已经在
运行的部分：

- 一条**基于 Git 的流水线**——叶子将各节点报告推送到 `leaf/<node-id>` 分支；distiller
  通过一道审核/发布关卡将团队知识发布到 `master`；叶子再将其订阅回本地记忆。
- 上行途中的**敏感信息脱敏**，以及每份蒸馏产物上的**来源溯源**。
- **面向 CLI 编码 agent 的 adapter。** Claude Code 是经过测试的路径；Codex、Hermes、
  OpenClaw、OpenCode 以 adapter YAML 形式随附。当前的 adapter 读取文件系统中的 Markdown
  记忆，因此接入一个新的“文件型” agent 只需编写一份 **adapter YAML**，无需新增代码；而
  非文件型的记忆载体仍需实现对应的 adapter。
- 一个**可插拔的 LLM** 层（`LLMProvider`）：包含 Anthropic SDK provider、零 token 消耗的
  `mock` provider（用于测试与通过 `--record` / `--replay` 调优 prompt），以及 Hermes-tool
  桥接 provider。

更宏大的愿景——非编码 agent、部门/组织级的根节点、以及一块实时的组织洞察看板——属于
**路线图**，尚非已交付的产品。预期仍有粗糙之处。

---

## 它是如何工作的

1. **汇报（Report）。** 每个叶子扫描其 agent 的共享记忆，对密钥脱敏，并按策略自动将各
   节点报告推送到 `leaf/<node-id>` 分支——无需任何人决定“把它写下来”。
2. **蒸馏（Distill）。** 蒸馏根定期对这些报告聚类，并请求 LLM 将其精炼为团队知识：共性
   知识、共同问题及其解法、最佳实践、早期风险信号——每一条都带来源溯源。
3. **回流（Feed back）。** 蒸馏后的 `team/` 知识按相关性与优先级排序后回流，避免 agent
   被信息淹没。
4. **本地存储（Store）。** 每个叶子将团队知识折叠进本地记忆，并在*私有个人记忆*与*共享
   团队知识*之间保持清晰的分隔。

三条性质让它得以长久：**agent 原生**（从 agent 本就产生的记忆中收割，没有额外的文档负担）、
**基于 Git**（溯源、历史、审核关卡与回滚都免费获得，数据也始终可移植），以及**由配置驱动
的可插拔**（汇报策略、蒸馏算法、产物格式、各 agent 的记忆后端、回流策略全部外置——因此系统
永不绑定任何一种 agent 框架或工具）。

---

## 双仓库模型

本仓库存放**代码、测试、脚本与 CI**。在运行时，各节点与一个**独立的共享 Git 仓库**交互，
该仓库存放实际的数据与运行时配置：

| 内容 | 位置 |
|---|---|
| Python 包、脚本、测试、CI | 本仓库 |
| 运行时 `config/` + `prompts/` | 共享仓库 `master` |
| Leaf 报告 | 共享仓库 `leaf/<node-id>` 分支 |
| 蒸馏产物（`team/`、`manifest.json`） | 共享仓库 `master` |

此处的 `config/` 与 `prompts/` 仅为引导模板与测试夹具；运行时的唯一事实来源始终是共享
仓库的 `master`。

---

## 快速开始

```bash
python3 -m pip install -e ".[dev]"   # Python 3.11+ (Linux/macOS; Windows via WSL2)
pytest
```

最快地看到整条回路跑起来——汇报 → 蒸馏 → 审核/发布 → 订阅——且无网络、无 token 消耗的
方式，是离线回归测试（这一条是真正可复制粘贴运行的）：

```bash
bash tests/integration/local_repo.sh
```

在真实团队里，是同一条回路，只是接到你自己的共享仓库与 agent 上（下面的路径均为占位示例）：

```bash
# 1. 搭起一个共享记忆仓库（每个团队一次）
scripts/bootstrap_repo.sh --repo /tmp/team-memory.git

# 2. 叶子汇报其 agent 中可共享的记忆（上行途中已对敏感信息脱敏）
git clone /tmp/team-memory.git ./leaf-work
python3 -m memory_net.leaf.cli report \
  --repo ./leaf-work --node-id alice-claude-01 --agent-root ~/my-agent \
  --adapter config/adapters/claude-code.yaml \
  --redaction config/redaction_patterns.yaml

# 3. distiller 把团队知识蒸馏到一个 release 分支上——这就是审核/发布关卡
git clone /tmp/team-memory.git ./distill-work
python3 -m memory_net.distiller.cli run --repo ./distill-work --provider mock   # mock = 零 token；换成 `anthropic` 即可上线

# 4. release 经审核（合入 master）后，叶子把它订阅回本地
python3 -m memory_net.leaf.cli subscribe \
  --repo ./distill-work --agent-root ~/my-agent \
  --adapter config/adapters/claude-code.yaml \
  --config-revision test --state-dir ~/.memory-net/state
```

完整命令面（含用于持续运转的 `daemon`）：

```bash
python3 -m memory_net.leaf.cli --help        # report / subscribe / health / hints / daemon / start / stop / trigger / status
python3 -m memory_net.distiller.cli --help    # run [--provider mock|anthropic|hermes_tool]
```

默认配置位于 `config/`，adapter 配置位于 `config/adapters/`，prompt 模板位于
`prompts/`。

---

## 文档

- **`mvp_tech_design.md`** —— 技术设计文档（架构、接口、schema、Git 布局、里程碑）。
  事实来源。
- **`VISION.md`** —— 问题、方法论与飞轮效应。
- **`todo.md`** —— MVP 路线图与任务追踪。
- **`AGENTS.md`** —— 贡献者的工作规则与约定。

## 贡献

参见 [CONTRIBUTING.md](CONTRIBUTING.md)。简而言之：保持测试套件全绿
（`ruff` / `black` / `isort` / `mypy` / `pytest`），提交保持原子化并遵循
`[Type] Scope: Description` 格式，且在改动代码的同时同步更新设计文档。

## 命名

项目与 PyPI 发行包名为 **`aomt`**；而 import 包名仍为 **`memory_net`**，CLI 命令、
`MEMORY_NET_HOME` 环境变量、`~/.memory-net` 主目录，以及 `memory-net/*` 配置 schema
前缀，则保留旧有的 **`memory-net`** 标识（正如 `ripgrep` 发布的命令名为 `rg`）。

## 许可证

[Apache-2.0](LICENSE) © 2026 ljxalpha
