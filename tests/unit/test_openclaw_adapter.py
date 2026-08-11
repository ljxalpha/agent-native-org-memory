"""OpenClaw adapter read-path regressions (M3-002).

These pin ``config/adapters/openclaw.yaml`` against the real on-disk layout of an
openclaw 2026.3.2 install, whose redacted snapshot lives in
``tests/fixtures/adapters/openclaw/``. The layout facts that matter:

  * the agent working root is ``<openclaw home>/workspace`` — that is AGENT_ROOT;
  * ``AGENTS.md`` at that root is the per-session instruction file (hint target);
  * the native memory store is ``<openclaw home>/memory/main.sqlite``, i.e. one
    level *above* the managed root ``${AGENT_ROOT}/memory/shared``, while native
    daily notes ``${AGENT_ROOT}/memory/YYYY-MM-DD.md`` are its *siblings*;
  * ``.env`` / ``credentials/`` / ``logs/`` / ``agents/`` sit alongside them.

What is deliberately NOT claimed: that openclaw consumes an injected hint block.
No agent run was made, so the adapter stays ``support_status: experimental``.
"""

from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]

from memory_net.shared.adapters import FileSystemMdAdapter

REPO_ROOT = Path(__file__).resolve().parents[2]
ADAPTER_YAML = REPO_ROOT / "config" / "adapters" / "openclaw.yaml"
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "adapters" / "openclaw"

# The fixture cannot ship a file literally named ``.env`` (the repo git-ignores it
# and treats a tracked one as a secret-hygiene failure), so it is stored under a
# neutral name and restored while materializing the tree.
FIXTURE_RENAMES = {"_dotenv": ".env"}


def _materialize_install(destination: Path) -> Path:
    """Copy the redacted openclaw layout into ``destination``; return AGENT_ROOT."""
    for source in sorted(FIXTURE_ROOT.rglob("*")):
        if not source.is_file():
            continue
        relative = source.relative_to(FIXTURE_ROOT)
        if relative.name == "README.md" and relative.parent == Path("."):
            continue  # fixture documentation, not part of the install layout
        relative = relative.with_name(FIXTURE_RENAMES.get(relative.name, relative.name))
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    # Secret-bearing dirs git cannot track empty; recreate them with sample content.
    for directory in ("credentials", "logs", "agents"):
        (destination / directory).mkdir(parents=True, exist_ok=True)
        (destination / directory / "sample.md").write_text(
            "sample sibling content\n", encoding="utf-8"
        )
    return destination / "workspace"


def _load(agent_root: Path) -> FileSystemMdAdapter:
    return FileSystemMdAdapter.from_yaml(ADAPTER_YAML, agent_root=agent_root)


@pytest.fixture(autouse=True)
def _opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORY_NET_ALLOW_EXPERIMENTAL_ADAPTER", "1")


def test_list_shared_only_reads_the_managed_root(tmp_path: Path) -> None:
    """Only ``${AGENT_ROOT}/memory/shared/**/*.md`` is collected.

    The surrounding real install is full of markdown — AGENTS.md, MEMORY.md,
    USER.md and the native daily notes under ``memory/`` — and none of it may be
    reported.
    """
    agent_root = _materialize_install(tmp_path / "openclaw")
    shared = agent_root / "memory" / "shared" / "notes"
    shared.mkdir(parents=True)
    (shared / "tip.md").write_text("Retry with bounded backoff.\n", encoding="utf-8")
    (agent_root / "memory" / "shared" / "top.md").write_text(
        "Top-level team-facing note.\n", encoding="utf-8"
    )

    records = list(_load(agent_root).list_shared())

    assert [r.source_locator for r in records] == [
        "openclaw-shared:notes/tip.md",
        "openclaw-shared:top.md",
    ]
    assert all(r.scope == "shared" for r in records)
    # The agent-private neighbourhood really does exist in this tree...
    assert (agent_root / "MEMORY.md").exists()
    assert (agent_root / "memory" / "2026-01-01.md").exists()
    # ...and none of it made it into a record.
    collected = "\n".join(r.content for r in records)
    assert "Long-term memory" not in collected
    assert "Operator profile" not in collected
    assert "daily note" not in collected


def test_native_store_and_secret_siblings_are_unreachable(tmp_path: Path) -> None:
    """The sqlite store and ``.env``/``credentials/`` are denied twice over.

    Assertion 1: they live outside the managed root, so the configured glob never
    even enumerates them. Assertion 2: were a copy to appear *inside* the managed
    root, the ``exclude`` list still drops it.
    """
    agent_root = _materialize_install(tmp_path / "openclaw")
    shared = agent_root / "memory" / "shared"
    shared.mkdir(parents=True)
    (shared / "keep.md").write_text("Team-facing note.\n", encoding="utf-8")

    adapter = _load(agent_root)
    shared_root = next(r for r in adapter.roots if r.scope == "shared")

    # --- assertion 1: out of glob range by position -------------------------
    native_store = agent_root.parent / "memory" / "main.sqlite"
    dotenv = agent_root.parent / ".env"
    assert native_store.is_file() and dotenv.is_file()
    reachable = {p.resolve() for p in shared_root.base_dir.rglob("*")}
    assert native_store.resolve() not in reachable
    assert dotenv.resolve() not in reachable
    assert (agent_root.parent / "credentials").resolve() not in reachable

    # --- assertion 2: excluded even when misplaced inside the managed root ---
    (shared / "main.sqlite").write_text("", encoding="utf-8")
    (shared / ".env").write_text("TOKEN=sample\n", encoding="utf-8")
    for directory in ("credentials", "logs", "agents"):
        (shared / directory).mkdir()
        (shared / directory / "leak.md").write_text("leaked?\n", encoding="utf-8")

    records = list(adapter.list_shared())

    assert [r.source_locator for r in records] == ["openclaw-shared:keep.md"]
    assert all("leaked?" not in r.content for r in records)
    excludes = set(shared_root.exclude)
    assert {
        "**/*.sqlite*",
        ".env",
        "credentials/**",
        "agents/**",
        "logs/**",
    } <= excludes


def test_managed_root_boundary_rejects_dotdot_and_symlink_escapes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Escaping the managed root is an error, not a silent skip."""
    # Isolate HOME so "under the user's home" cannot accidentally bless an escape.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    agent_root = _materialize_install(tmp_path / "openclaw")
    shared = agent_root / "memory" / "shared"
    shared.mkdir(parents=True)
    (shared / "keep.md").write_text("Team-facing note.\n", encoding="utf-8")

    base = yaml.safe_load(ADAPTER_YAML.read_text(encoding="utf-8"))

    # (1) a base_dir climbing out of AGENT_ROOT is refused at load time
    escaping_base = tmp_path / "escaping-base.yaml"
    hoisted = dict(base)
    hoisted["roots"] = {
        "shared": [
            {
                "alias": "openclaw-shared",
                "base_dir": "${AGENT_ROOT}/../memory",
                "include": ["**/*.md"],
            }
        ]
    }
    escaping_base.write_text(yaml.safe_dump(hoisted), encoding="utf-8")
    with pytest.raises(ValueError, match="must be under"):
        FileSystemMdAdapter.from_yaml(escaping_base, agent_root=agent_root)

    # (2) a `..` include pattern is refused at load time
    escaping_glob = tmp_path / "escaping-glob.yaml"
    dotdot = dict(base)
    dotdot["roots"] = {
        "shared": [
            {
                "alias": "openclaw-shared",
                "base_dir": "${AGENT_ROOT}/memory/shared",
                "include": ["../*.md"],
            }
        ]
    }
    escaping_glob.write_text(yaml.safe_dump(dotdot), encoding="utf-8")
    with pytest.raises(ValueError, match="relative to base_dir"):
        FileSystemMdAdapter.from_yaml(escaping_glob, agent_root=agent_root)

    # (3) a symlink out of the managed root is refused at read time
    (shared / "private.md").symlink_to(agent_root / "MEMORY.md")
    with pytest.raises(ValueError, match="escapes base_dir"):
        list(_load(agent_root).list_shared())


def test_hint_injection_is_idempotent_across_two_runs(tmp_path: Path) -> None:
    """Injecting twice leaves exactly one anchored block in the real AGENTS.md."""
    agent_root = _materialize_install(tmp_path / "openclaw")
    instructions = agent_root / "AGENTS.md"
    original = instructions.read_text(encoding="utf-8")

    adapter = _load(agent_root)
    hint_spec = adapter.hint_config

    assert adapter.inject_team_hint(hint_spec) is True
    after_first = instructions.read_text(encoding="utf-8")
    assert adapter.inject_team_hint(hint_spec) is False
    after_second = instructions.read_text(encoding="utf-8")

    assert after_second == after_first
    start = str(hint_spec["anchor_start"])
    end = str(hint_spec["anchor_end"])
    assert after_second.count(start) == 1
    assert after_second.count(end) == 1
    assert "@memory/team/INDEX.md" in after_second
    # The pre-existing instruction body is preserved verbatim.
    assert after_second.startswith(original.rstrip("\n"))
