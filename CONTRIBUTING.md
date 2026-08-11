# Contributing

Thanks for your interest in **Agent-Native Organizational Memory Architecture (aomt)**! This project is in an
early (`MVP-core`) phase, so contributions, bug reports, and design feedback are
all welcome.

By submitting a contribution you agree that it is licensed under the project's
[Apache-2.0](LICENSE) license.

## Platform

The runtime supports **Linux and macOS only**. Windows is not supported
natively — use WSL2. Startup deliberately blocks other platforms.

## Development setup

```bash
git clone https://github.com/ljxalpha/agent-native-org-memory.git
cd agent-native-org-memory
python3 -m pip install -e ".[dev]"   # Python 3.11+
```

## Before you open a PR

Run the exact checks CI runs. All of them must pass (green) before committing —
do not commit a red tree:

```bash
ruff check .            # lint
black --check .         # formatting
isort --check-only .    # import order
mypy memory_net         # static types (strict)
pytest                  # unit + functional tests
bash tests/integration/local_repo.sh   # offline end-to-end (no network, no tokens)
```

To auto-fix formatting/imports: `ruff check --fix . && isort . && black .`

## Testing expectations

- Write tests alongside code (TDD). Cover both unit behavior and end-to-end /
  boundary conditions.
- For anything that reads or writes the shared Git repo or caches, **assert the
  stored data** is correct, not just the return value.
- Keep `pytest` green. New behavior needs new tests.

## Commit conventions

Commits are **atomic** (one small feature or fix each) and follow:

```
[Type] Scope: Description
```

Allowed types: `Feat`, `Fix`, `Docs`, `Refactor`, `Test`, `Chore`.
Example: `[Feat] AuthModule: Add JWT validation interface`.

If a single change would touch more than ~3 files, split it into smaller
commits.

## Document-driven development

This repo is documentation-first. The design documents are the source of truth:

- **`mvp_tech_design.md`** — architecture, interfaces, schemas, milestones.
  Update it **before or alongside** any interface/behavior change.
- **`todo.md`** — the MVP task tracker. Keep task status current.
- If a requirement in the design doc is unclear or wrong, ask, then append the
  resolution to the doc with a timestamp.

## Architecture note (two repos)

This repository holds **code, tests, scripts, and CI**. At runtime, nodes talk
to a **separate shared Git repository** that holds the actual data and runtime
config (`config/`, `prompts/` on its `master` branch; leaf reports on
`leaf/<node-id>` branches; distilled products on `master`). The `config/` and
`prompts/` directories in *this* repo are bootstrap templates and test fixtures
only — see `mvp_tech_design.md` and `AGENTS.md` for the full constraints.

New agents are supported by **adding an adapter YAML** under `config/adapters/`,
not by writing new code classes — as long as the agent's memory lives in
files on disk. A memory surface that is not a filesystem tree still needs a new
`MemoryAdapter` implementation; `FileSystemMdAdapter` is currently the only one.

Four of the five shipped adapters are experimental (`support_status:
experimental`): `codex`, `hermes`, `openclaw` and `opencode`. Loading any of
them requires the opt-in env flag `MEMORY_NET_ALLOW_EXPERIMENTAL_ADAPTER=1`.
Only `claude-code` is not — and it is stable by the loader's default, since it
declares no `support_status` at all. An adapter stays experimental until a real,
quota-costing agent run shows the injected hint is actually consumed.

## Reporting bugs / requesting features

Open a GitHub issue with: what you expected, what happened, and the minimal
steps (or a failing test) to reproduce.
