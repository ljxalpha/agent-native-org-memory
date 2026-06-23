[English](README.md) | 中文

# Agent-Native Organizational Memory Architecture

**Agent 原生组织记忆架构** — *aomt*

[![CI](https://github.com/ljxalpha/agent-native-org-memory/actions/workflows/ci.yml/badge.svg)](https://github.com/ljxalpha/agent-native-org-memory/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

> **将每个 agent 散落的记忆蒸馏为共享的组织知识，再回流给每一个 agent。**

一个基于 Git 的**组织记忆架构**，面向由 CLI 编码 agent（Claude Code、Codex、Hermes
等）组成的团队。每位开发者的 agent 都保有各自的本地记忆；网络会将其中*可共享*的部分
蒸馏为经过精炼的**团队知识**，再回流给所有人——无需任何人手动抄录笔记。

> 状态：早期 MVP（`MVP-core`）。设计文档已相当完备；实现正按里程碑逐步推进。预期仍有
> 粗糙之处。

## Vision

单个 agent 正变得越来越快，但它们的记忆却被困在各自的孤岛中——而这种孤岛化的记忆，
如今正是组织真正的瓶颈。当一个 agent 需要另一个团队的 agent 早已习得的知识时，人类被
迫被拉上关键路径；每个 agent 的知识上限取决于与它协作的那一个人；而那些来之不易的跨
团队经验，几乎不会自行流动。

本项目给出的答案是：收割 agent 本就在产生的工作记忆，用 LLM 将其蒸馏为带来源引用、
已脱敏的**组织知识**，再把这些知识自动回流给每一个 agent——通过一条基于 Git 的流水线，
无需额外的文档撰写负担。蒸馏一次，回流所有，让这个循环不断复利。

完整的问题陈述、方法论以及它所驱动的飞轮效应，请参阅 **[VISION.md](VISION.md)**。

## How it works

```
 leaf agents                         distiller                    leaf agents
 (report)                            (cluster + LLM)              (subscribe)
 memory/shared/*.md  ──push──▶  leaf/<node-id>  ──▶  cluster ──▶ distill ──▶
                                                                  team/*.md
        ▲                                                            │
        └──────────────── pull team/ into memory/team/ ◀────────────┘
```

1. **Leaf 节点**扫描每个 agent 的共享记忆，对密钥进行脱敏，并将各节点的报告推送到
   `leaf/<node-id>` 分支。
2. **Distiller** 定期对这些报告进行聚类，请求 LLM 将其蒸馏为团队知识，并通过一道
   审核/发布关卡，将结果发布到 `master`。
3. **Leaf 节点**将蒸馏后的 `team/` 知识拉回到各自本地的 `memory/team/` 目录。

LLM 是可插拔的（`LLMProvider`）：包括一个 Anthropic SDK provider、一个 `mock`
provider（零 token 消耗，用于测试以及通过 `--record`/`--replay` 调优 prompt），以及一个
Hermes-tool 桥接 provider。新增 agent 只需编写一份 **adapter YAML**，无需新增代码。

## Two-repository model

本仓库存放**代码、测试、脚本与 CI**。在运行时，各节点与一个**独立的共享 Git 仓库**
交互，该仓库存放实际的数据与运行时配置：

| 内容 | 位置 |
|---|---|
| Python 包、脚本、测试、CI | 本仓库 |
| 运行时 `config/` + `prompts/` | 共享仓库 `master` |
| Leaf 报告 | 共享仓库 `leaf/<node-id>` 分支 |
| 蒸馏产物（`team/`、`manifest.json`） | 共享仓库 `master` |

此处的 `config/` 与 `prompts/` 仅为引导模板与测试夹具；运行时的唯一事实来源始终是共享
仓库的 `master`。

## Local setup

```bash
python3 -m pip install -e ".[dev]"   # Python 3.11+ (Linux/macOS; Windows via WSL2)
pytest
```

引导一个共享记忆仓库：

```bash
scripts/bootstrap_repo.sh --repo /tmp/team-memory.git
```

运行离线端到端回归测试（无网络、无 token 消耗）：

```bash
bash tests/integration/local_repo.sh
```

## Main commands

```bash
python3 -m memory_net.leaf.cli --help        # leaf: report / subscribe / health / daemon
python3 -m memory_net.distiller.cli --help    # distiller: run [--provider mock|anthropic|hermes_tool]
```

默认配置位于 `config/`，adapter 配置位于 `config/adapters/`，prompt 模板位于
`prompts/`。

> **关于命名。** 项目与 Python 包名为 **`aomt`**。而 CLI 命令（`memory-net-leaf`、
> `memory-net-distiller`）、`MEMORY_NET_HOME` 环境变量、`~/.memory-net` 主目录，以及
> `memory-net/*` 配置 schema 前缀，则保留旧有的 **`memory-net`** 标识（正如 `ripgrep`
> 发布的命令名为 `rg`）。

## Documentation

- **`mvp_tech_design.md`** —— 技术设计文档（架构、接口、schema、Git 布局、里程碑）。
  事实来源。
- **`todo.md`** —— MVP 任务追踪。
- **`AGENTS.md`** —— 贡献者的工作规则与约定。

## Contributing

参见 [CONTRIBUTING.md](CONTRIBUTING.md)。简而言之：保持测试套件全绿
（`ruff` / `black` / `isort` / `mypy` / `pytest`），提交保持原子化并遵循
`[Type] Scope: Description` 格式，且在改动代码的同时同步更新设计文档。

## License

[Apache-2.0](LICENSE) © 2026 ljxalpha
