# Repository Guidelines

## Project Structure & Progressive Disclosure

This repo is still implementation-light and document-first. Use `AGENTS.md` for working rules only; keep detailed architecture, schemas, and milestone scope in `mvp_tech_design.md` and `todo.md`. This progressive-disclosure split is intentional: keep high-frequency contributor guidance short, and push deep design details into the design docs to save context and reduce drift.

When work grows large enough to threaten context quality, prefer splitting it into focused subtasks and delegating those subtasks to sub-agents when available. Keep delegated scopes concrete and bounded, then integrate their results through the main thread.

Planned source layout is `memory_net/shared/`, `memory_net/leaf/`, and `memory_net/distiller/`; tests belong in `tests/unit/` and `tests/integration/`.

## Build, Test, and Dev Commands

- `pip install -e ".[dev]"`: editable install once `pyproject.toml` exists.
- `pytest`: run the full test suite.
- `pytest tests/unit/test_redaction.py -v`: run one focused unit test file.
- `bash tests/integration/local_repo.sh`: offline end-to-end regression for the local Git flow.
- `black . && isort . && ruff check . && mypy .`: format, lint, and type-check before commit.

## Coding Style & Module Design

Target Python 3.11+. Follow PEP 8, 4-space indentation, and max line length 88. Require type annotations on all function signatures. Use `snake_case` for modules/functions, `PascalCase` for classes, and prefer frozen dataclasses or other immutable shapes for read-only data.

Keep functions small, single-purpose, and easy to test. Prefer early returns over deep nesting. Do not hardcode agent-specific paths, prompts, or redaction behavior; keep those in YAML or Markdown config. Use specific exception handling only; never use bare `except`, and never use mutable default arguments.

Prefer relative paths in code, config, documentation, and examples. Use absolute paths only when an external tool or platform explicitly requires them.

## Testing Guidelines

Use `pytest`. Practice TDD: write a failing test first, make it pass with the smallest change, then refactor safely. Cover user-visible behavior, edge cases, and failure modes. Keep fixtures minimal.

Use mocks only at external boundaries such as network, filesystem, Git, or LLM calls. Do not use mocks to hide defects in pure logic or core domain code; core transformations should be tested with real inputs and deterministic fixtures.

## Commit & PR Guidelines

Keep changes small and shippable. Prefer splitting work into narrow modules and small commits rather than mixing scaffolding, behavior, and refactors in one patch. Commit subjects follow `[Type] Scope: Description` (Type ∈ Feat/Fix/Docs/Refactor/Test/Chore), for example `[Fix] DistillPipeline: handle empty cluster`.

Commit frequency is strict: complete one atomic feature, then create one commit immediately. The maximum commit scope must not exceed one `todo.md` item.

For any incremental adjustment to the technical design, update all three places in the same change set:
- Update the technical design content itself in `mvp_tech_design.md`.
- Append a change record at the end of `mvp_tech_design.md` that includes change summary, change reason, and change time.
- Append the corresponding follow-up task in `todo.md`.

PRs should summarize scope, link the relevant task in `todo.md`, list tests run, and note any required updates to `mvp_tech_design.md` when behavior, schema, or CLI semantics change.
