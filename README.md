English | [中文](README.zh-CN.md)

<div align="center">

# Agent-Native Organizational Memory Architecture

**`aomt`** — turn a fleet of AI agents into one compounding organizational brain.

[![CI](https://github.com/ljxalpha/agent-native-org-memory/actions/workflows/ci.yml/badge.svg)](https://github.com/ljxalpha/agent-native-org-memory/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

</div>

> **Every agent is learning things worth keeping — and your organization can't see any of it.**
> aomt turns scattered agent memory into live operating intelligence: common failures,
> latent risks, best practices, and duplicated work surface automatically — and the
> distilled lessons flow back to the agents that need them.

Your company is already running a growing fleet of AI agents, and every one of them is
learning *alone*. The lesson one agent paid for in a two-hour debugging session never
reaches the agent that hits the same wall tomorrow. The risk three teams independently
stumbled into is never named. The better approach one engineer's agent discovered never
spreads. **aomt is the missing layer that turns that scattered, perishable memory into
two things at once: a durable organizational asset fed back to every agent, and a live
view — in one place — of what your organization is learning, struggling with, and
getting right.**

---

## The problem: every agent's memory is a silo

<div align="center">
<img src="docs/images/problem.svg" alt="Today, agent memory is trapped in silos: each person-and-agent pair learns lessons that never cross the wall; leadership sees only fragments; agents do work in minutes while knowledge between people moves in hours to days." width="900">
</div>

Before AI agents, the smallest unit of work was a **person**, and knowledge moved
person-to-person — slowly. Now the unit is a **person *and* their agent**, and the
work *inside* that unit got an order of magnitude faster. The channels *between*
units did not. Three structural defects follow:

- **Humans are back on the critical path.** An agent needs something another team's
  agent already learned — and a person has to relay it. Minutes of real work stall
  for hours of waiting.
- **Each agent is capped by one human's knowledge.** It knows its base model plus
  whatever its single operator happens to tell it. The organization's most valuable
  knowledge — tacit, cross-domain, the global view — escapes it entirely.
- **Hard-won lessons evaporate.** Cross-team knowledge only flows when one human
  writes it down *and* another goes looking. In practice that chain almost never
  completes.

Siloed agent memory is not a minor inefficiency. It is the new organizational
bottleneck — and it gets more expensive with every agent you add.

---

## The solution: two products from one loop

<div align="center">
<img src="docs/images/architecture.svg" alt="aomt architecture (target): any memory-producing agent can be a leaf node; it reports (auto, redacted, source-cited) up through a shared Git repo with a review/release gate to a distillation root that clusters and distills with a pluggable LLM; distilled team knowledge feeds back down ranked by relevance; the same distillation yields organizational intelligence — common problems surfaced, latent risks flagged early, best practices diffused, efficiency bottlenecks exposed; the network scales into a team-to-department-to-org tree and is agent-native, Git-backed, and pluggable by config." width="960">
</div>

aomt harvests the working memory your agents already produce and distills it with an
LLM into **source-cited, secret-redacted organizational knowledge**. One loop yields
two products.

**For leadership — organizational intelligence you couldn't get before.**
The distillation surfaces patterns no single agent or person can see:

| | |
|---|---|
| 🟡 **Common problems, surfaced** | the same failure hitting three teams — named once, fixed once |
| 🟡 **Latent risks, flagged early** | a negative signal recurring across many agents becomes an early warning |
| 🟡 **Best practices, diffused** | one engineer's better approach reaches the whole org, fast |
| 🟡 **Efficiency bottlenecks, exposed** | where work stalls or gets duplicated across teams, made visible |

**For every agent — knowledge that flows back.**
Each agent (a *leaf*) automatically reports the shareable parts of its memory; the
*distillation root* clusters and refines those reports into team knowledge, then
delivers it back down — ranked by relevance — into each agent's local memory. No one
writes a doc. No one copies a note. **Distill once, feed back to all.**

> **Architecture target: any memory-producing agent can be a leaf** — coding, research,
> support, ops — not just coding agents. Today's MVP proves the loop with CLI coding
> agents; new domains plug in through an adapter YAML as their memory surfaces become
> available. The network also scales into a tree: a team's root can itself be a leaf of
> a department root, giving every layer of the organization a memory at its own
> granularity.

### Why now, and why it compounds

Agent execution collapsed to minutes; moving knowledge between people still takes hours
to days. That gap widens with every agent you deploy — and it is exactly the gap aomt
closes. Closing it also compounds: more usage → richer agent memory → better distilled
knowledge → smarter agents that need less hand-holding → more usage. It is a flywheel
that pays compound interest: the earlier it starts turning, the harder its lead is to
catch.

The full thesis — the shift to person-agent units, why a hand-maintained knowledge
base can't substitute, and the flywheel in detail — is in **[VISION.md](VISION.md)**.

---

## What's shipping today

aomt is an **early, working MVP**: the design is comprehensive, the implementation is
growing milestone by milestone. What runs right now:

- A **Git-backed pipeline** — leaves push per-node reports to `leaf/<node-id>`
  branches; the distiller publishes team knowledge to `master` through a
  review/release gate; leaves subscribe it back into local memory.
- **Secret redaction** on the way up, and **source provenance** on every distilled
  product.
- **Adapters for CLI coding agents.** Claude Code is the tested path; Codex, Hermes,
  OpenClaw, and OpenCode ship as adapter YAMLs. Today's adapter reads filesystem
  Markdown memory, so onboarding a new file-based agent is an **adapter YAML**, not new
  code; memory surfaces that aren't files on disk still need an adapter implementation.
- A **pluggable LLM** layer (`LLMProvider`): an Anthropic SDK provider, a zero-token
  `mock` provider for tests and prompt tuning (`--record` / `--replay`), and a
  Hermes-tool bridge.

The wider vision — non-coding agents, department/org-level roots, and a live
organizational-intelligence dashboard — is the **roadmap**, not yet the shipped
product. Expect rough edges.

---

## How it works

1. **Report.** Each leaf scans its agent's shared memory, redacts secrets, and pushes
   a per-node report up to its `leaf/<node-id>` branch — automatically, on a policy,
   with no human deciding to "write it down".
2. **Distill.** The distillation root periodically clusters those reports and asks an
   LLM to refine them into team knowledge: common knowledge, shared problems and
   their solutions, best practices, and early risk signals — each with source
   provenance.
3. **Feed back.** The distilled `team/` knowledge is delivered back down, ranked by
   relevance and priority so agents are not overloaded.
4. **Store locally.** Each leaf folds team knowledge into its local memory, keeping a
   clean separation between *private personal memory* and *shared team knowledge*.

Three properties keep it durable: **agent-native** (harvested from memory agents
already produce — no extra documentation chore), **Git-backed** (provenance, history,
review gates, and rollback come for free, and the data stays portable), and
**pluggable by configuration** (reporting policy, distillation, product format,
per-agent memory backend, and delivery are all externalized — so the system never
binds to one agent framework or tool).

---

## Two-repository model

This repository holds **code, tests, scripts, and CI**. At runtime, nodes interact
with a **separate shared Git repository** that holds the actual data and runtime
configuration:

| What | Where |
|---|---|
| Python packages, scripts, tests, CI | this repo |
| Runtime `config/` + `prompts/` | shared repo `master` |
| Leaf reports | shared repo `leaf/<node-id>` branches |
| Distilled products (`team/`, `manifest.json`) | shared repo `master` |

The `config/` and `prompts/` here are bootstrap templates and test fixtures; the
runtime source of truth is always the shared repo's `master`.

---

## Get started

```bash
python3 -m pip install -e ".[dev]"   # Python 3.11+ (Linux/macOS; Windows via WSL2)
pytest
```

The fastest way to watch the whole loop run — report → distill → review/release →
subscribe — with no network and no tokens, is the offline regression (this one is
genuinely copy-paste runnable):

```bash
bash tests/integration/local_repo.sh
```

In a real team it is the same loop, wired to your own shared repo and agents (paths
below are placeholders):

```bash
# 1. stand up a shared memory repo (once per team)
scripts/bootstrap_repo.sh --repo /tmp/team-memory.git

# 2. each member's leaf reports its agent's shareable memory (secrets redacted on the way up)
git clone /tmp/team-memory.git ./leaf-work
python3 -m memory_net.leaf.cli report \
  --repo ./leaf-work --node-id alice-claude-01 --agent-root ~/my-agent \
  --adapter config/adapters/claude-code.yaml \
  --redaction config/redaction_patterns.yaml

# 3. the distiller distills team knowledge onto a release branch — the review/release gate
git clone /tmp/team-memory.git ./distill-work
python3 -m memory_net.distiller.cli run --repo ./distill-work --provider mock   # mock = zero tokens; swap for `anthropic` to go live

# 4. once the release is approved (promoted to master), leaves subscribe it back
python3 -m memory_net.leaf.cli subscribe \
  --repo ./distill-work --agent-root ~/my-agent \
  --adapter config/adapters/claude-code.yaml \
  --config-revision test --state-dir ~/.memory-net/state
```

Full command surface (including `daemon` for continuous operation):

```bash
python3 -m memory_net.leaf.cli --help        # report / subscribe / health / hints / daemon / start / stop / trigger / status
python3 -m memory_net.distiller.cli --help    # run [--provider mock|anthropic|hermes_tool]
```

Default config lives in `config/`, adapter config in `config/adapters/`, and prompt
templates in `prompts/`.

---

## Documentation

- **`mvp_tech_design.md`** — the technical design (architecture, interfaces, schemas,
  Git layout, milestones). Source of truth.
- **`VISION.md`** — the problem, the approach, and the flywheel.
- **`todo.md`** — MVP roadmap and task tracker.
- **`AGENTS.md`** — contributor working rules and conventions.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). In short: keep the suite green
(`ruff` / `black` / `isort` / `mypy` / `pytest`), make atomic
`[Type] Scope: Description` commits, and update the design docs alongside code.

## Naming

The project and PyPI distribution are **`aomt`**; the import package is still
**`memory_net`**, and the CLI commands, the `MEMORY_NET_HOME` env var, the
`~/.memory-net` home directory, and the `memory-net/*` config-schema prefixes keep the
legacy **`memory-net`** identifier (much as `ripgrep` ships `rg`).

## License

[Apache-2.0](LICENSE) © 2026 ljxalpha
