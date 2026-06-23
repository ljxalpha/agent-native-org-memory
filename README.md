English | [中文](README.zh-CN.md)

# Agent-Native Organizational Memory Architecture

**Agent 原生组织记忆架构** — *aomt*

[![CI](https://github.com/ljxalpha/agent-native-org-memory/actions/workflows/ci.yml/badge.svg)](https://github.com/ljxalpha/agent-native-org-memory/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

> **Distill each agent's scattered memories into shared organizational
> knowledge, and feed it back to every agent.**

A Git-backed **organizational memory architecture** for teams of CLI coding
agents (Claude Code, Codex, Hermes, …). Each developer's agent keeps its own
local memory; the network distills the *shareable* parts into curated **team
knowledge** that flows back to everyone — without anyone hand-copying notes.

> Status: early MVP (`MVP-core`). The design docs are comprehensive; the
> implementation is growing milestone by milestone. Expect rough edges.

## Vision

Individual agents are getting fast, but their memory is trapped in silos — and
that siloed memory is now the organization's real bottleneck. When an agent needs
something another team's agent already learned, a human is forced onto the
critical path; each agent's knowledge is capped by the one person it works with;
and hard-won cross-team lessons rarely flow on their own.

This project's answer is to harvest the working memory agents already produce,
distill it with an LLM into source-cited, secret-redacted **organizational
knowledge**, and feed that knowledge back to every agent — automatically, over a
Git-backed pipeline, with no separate documentation chore. Distill once, feed
back to all, and let the loop compound.

See **[VISION.md](VISION.md)** for the full problem statement, approach, and the
flywheel it sets in motion.

## How it works

```
 leaf agents                         distiller                    leaf agents
 (report)                            (cluster + LLM)              (subscribe)
 memory/shared/*.md  ──push──▶  leaf/<node-id>  ──▶  cluster ──▶ distill ──▶
                                                                  team/*.md
        ▲                                                            │
        └──────────────── pull team/ into memory/team/ ◀────────────┘
```

1. **Leaf nodes** scan each agent's shared memory, redact secrets, and push
   per-node reports to a `leaf/<node-id>` branch.
2. The **distiller** periodically clusters those reports, asks an LLM to distill
   them into team knowledge, and publishes the result to `master` (through a
   review/release gate).
3. **Leaves** pull the distilled `team/` knowledge back into their local
   `memory/team/` directory.

The LLM is pluggable (`LLMProvider`): an Anthropic SDK provider, a `mock`
provider (zero-token, for tests and prompt tuning via `--record`/`--replay`),
and a Hermes-tool bridge. New agents are added by writing an **adapter YAML**,
not new code.

## Two-repository model

This repository holds **code, tests, scripts, and CI**. At runtime, nodes
interact with a **separate shared Git repository** that holds the actual data
and runtime configuration:

| What | Where |
|---|---|
| Python packages, scripts, tests, CI | this repo |
| Runtime `config/` + `prompts/` | shared repo `master` |
| Leaf reports | shared repo `leaf/<node-id>` branches |
| Distilled products (`team/`, `manifest.json`) | shared repo `master` |

The `config/` and `prompts/` here are bootstrap templates and test fixtures; the
runtime source of truth is always the shared repo's `master`.

## Local setup

```bash
python3 -m pip install -e ".[dev]"   # Python 3.11+ (Linux/macOS; Windows via WSL2)
pytest
```

Bootstrap a shared memory repository:

```bash
scripts/bootstrap_repo.sh --repo /tmp/team-memory.git
```

Run the offline end-to-end regression (no network, no tokens):

```bash
bash tests/integration/local_repo.sh
```

## Main commands

```bash
python3 -m memory_net.leaf.cli --help        # leaf: report / subscribe / health / daemon
python3 -m memory_net.distiller.cli --help    # distiller: run [--provider mock|anthropic|hermes_tool]
```

Default config lives in `config/`, adapter config in `config/adapters/`, and
prompt templates in `prompts/`.

> **Naming.** The project and Python package are **`aomt`**. The CLI commands
> (`memory-net-leaf`, `memory-net-distiller`), the `MEMORY_NET_HOME` env var, the
> `~/.memory-net` home directory, and the `memory-net/*` config-schema prefixes keep
> the legacy **`memory-net`** identifier (much as `ripgrep` ships the `rg` command).

## Documentation

- **`mvp_tech_design.md`** — the technical design (architecture, interfaces,
  schemas, Git layout, milestones). Source of truth.
- **`todo.md`** — MVP task tracker.
- **`AGENTS.md`** — contributor working rules and conventions.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). In short: keep the suite green
(`ruff` / `black` / `isort` / `mypy` / `pytest`), make atomic
`[Type] Scope: Description` commits, and update the design docs alongside code.

## License

[Apache-2.0](LICENSE) © 2026 ljxalpha
