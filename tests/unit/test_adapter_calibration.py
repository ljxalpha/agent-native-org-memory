"""Adapter calibration tests (DN-P2).

These tests pin the *shipped* ``config/adapters/*.yaml`` files against the real
on-disk memory layouts of the three agents installed during development
(claude-code, codex, hermes), verified read-only with zero LLM quota:

  * claude-code: global ``~/.claude/CLAUDE.md`` (personal) + per-project memory;
    the memory-net shared face is the convention dir ``${AGENT_ROOT}/memory/shared``.
  * codex: ``~/.codex/memories/`` is a *git repo* (contains ``.git/`` and
    ``memories_1.sqlite``); instruction file is ``AGENTS.md``.
  * hermes: ``~/.hermes/memories/{MEMORY.md,USER.md}`` with sibling ``*.lock``
    files (e.g. ``MEMORY.md.lock``) living right next to the markdown.

The load-bearing invariants encoded here (do not "fix" these — they are by design):

  1. The ``shared`` reporting root is always the memory-net convention
     ``${AGENT_ROOT}/memory/shared`` — never an agent's native global store.
  2. ``MemoryAdapter`` has no ``list_personal``: personal-scope memory is
     *structurally* never collected/reported (privacy by construction).
  3. codex/hermes stay ``support_status: experimental`` (opt-in via env flag)
     until their ``@import``/AGENTS.md honoring is confirmed with a real,
     quota-costing agent run.
"""

from pathlib import Path

import pytest

from memory_net.shared.adapters import FileSystemMdAdapter

REPO_ROOT = Path(__file__).resolve().parents[2]
ADAPTERS_DIR = REPO_ROOT / "config" / "adapters"
CONVENTION_SHARED = ("memory", "shared")


def _load(agent: str, agent_root: Path) -> FileSystemMdAdapter:
    return FileSystemMdAdapter.from_yaml(
        ADAPTERS_DIR / f"{agent}.yaml",
        agent_root=agent_root,
    )


def test_claude_code_shared_real_layout_lists_and_excludes_draft(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """claude-code shared root reads nested topic md and honors the draft exclude."""
    # Isolate ~ so resolving the configured personal root never touches real ~/.claude.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    agent_root = tmp_path / "agent"
    shared = agent_root / "memory" / "shared" / "auth"
    (shared / "draft").mkdir(parents=True)
    (shared / "oauth.md").write_text(
        "---\ntags: [auth]\n---\nUse bounded backoff on 429.\n", encoding="utf-8"
    )
    (shared / "draft" / "wip.md").write_text("not ready\n", encoding="utf-8")

    records = list(_load("claude-code", agent_root).list_shared())

    assert [r.source_locator for r in records] == [
        "claude-project-shared:auth/oauth.md"
    ]
    assert all(r.scope == "shared" for r in records)
    assert all("not ready" not in r.content for r in records)


def test_claude_code_personal_scope_is_never_listed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Privacy regression: a configured personal root is NEVER yielded by list_shared.

    claude-code.yaml does declare a personal root (~/.claude/CLAUDE.md). Even with a
    juicy secret sitting in it, list_shared must only ever return shared-scope records.
    """
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    secret = "SUPER-SECRET-PERSONAL-TOKEN-do-not-report"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "CLAUDE.md").write_text(
        f"# personal\n{secret}\n", encoding="utf-8"
    )
    agent_root = tmp_path / "agent"
    shared = agent_root / "memory" / "shared" / "auth"
    shared.mkdir(parents=True)
    (shared / "note.md").write_text("shared and safe\n", encoding="utf-8")

    adapter = _load("claude-code", agent_root)
    # The test is only meaningful if a personal root is actually configured.
    assert any(r.scope == "personal" for r in adapter.roots)
    # ...yet there is no interface to read it.
    assert not hasattr(adapter, "list_personal")

    records = list(adapter.list_shared())
    assert [r.source_locator for r in records] == ["claude-project-shared:auth/note.md"]
    assert all(r.scope == "shared" for r in records)
    assert all(secret not in r.content for r in records)


def test_codex_collects_only_md_outside_the_git_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex shared root must not leak the git repo, sqlite, or lock files.

    Mirrors that ~/.codex/memories/ is itself a git repo with a sqlite sidecar.

    What actually rejects each of these is worth stating, because the config
    reads as if ``exclude`` did all of it: the sqlite and ``.lock`` siblings are
    dropped by ``include: ["**/*.md"]`` before any exclusion runs, and the file
    under ``.git/`` is dropped by the hidden-path skip. The ``exclude`` entries
    are declared cover for a future config that widens ``include``, so this test
    pins the collection boundary, not the exclusion machinery.
    """
    monkeypatch.setenv("MEMORY_NET_ALLOW_EXPERIMENTAL_ADAPTER", "1")
    agent_root = tmp_path / "agent"
    shared = agent_root / "memory" / "shared"
    (shared / ".git" / "hooks").mkdir(parents=True)
    shared.joinpath("good.md").write_text("real codex memory\n", encoding="utf-8")
    shared.joinpath(".git", "config").write_text("[core]\n", encoding="utf-8")
    shared.joinpath(".git", "hooks", "note.md").write_text(
        "leaked?\n", encoding="utf-8"
    )
    shared.joinpath("memories_1.sqlite").write_text("BINARY", encoding="utf-8")
    shared.joinpath("good.md.lock").write_text("", encoding="utf-8")

    records = list(_load("codex", agent_root).list_shared())

    assert [r.source_locator for r in records] == ["codex-shared:good.md"]
    assert all("leaked?" not in r.content for r in records)


def test_hermes_collects_md_and_leaves_sibling_lock_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hermes shared root reads md and leaves the sibling *.lock files behind.

    Mirrors that ~/.hermes/memories/ holds MEMORY.md alongside MEMORY.md.lock.
    The ``.lock`` files are excluded from the collection surface by
    ``include: ["**/*.md"]``, not by the yaml's ``**/*.lock`` entry, which never
    executes while ``include`` stays markdown-only.
    """
    monkeypatch.setenv("MEMORY_NET_ALLOW_EXPERIMENTAL_ADAPTER", "1")
    agent_root = tmp_path / "agent"
    shared = agent_root / "memory" / "shared"
    shared.mkdir(parents=True)
    shared.joinpath("MEMORY.md").write_text("hermes team note\n", encoding="utf-8")
    shared.joinpath("MEMORY.md.lock").write_text("", encoding="utf-8")
    shared.joinpath("USER.md.lock").write_text("", encoding="utf-8")

    records = list(_load("hermes", agent_root).list_shared())

    assert [r.source_locator for r in records] == ["hermes-shared:MEMORY.md"]


def test_shipped_adapters_use_convention_shared_root_and_no_list_personal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invariant guard across all shipped adapters.

    Every shared root must resolve to ${AGENT_ROOT}/memory/shared (the memory-net
    convention), never an agent's native store. And no adapter may expose a way to
    enumerate personal memory. A future edit pointing a shared root at, say,
    ~/.hermes/memories would trip this test.
    """
    monkeypatch.setenv("MEMORY_NET_ALLOW_EXPERIMENTAL_ADAPTER", "1")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    agent_root = tmp_path / "agent"
    expected = agent_root.joinpath(*CONVENTION_SHARED)

    for agent in ("claude-code", "codex", "hermes"):
        adapter = _load(agent, agent_root)
        shared_roots = [r for r in adapter.roots if r.scope == "shared"]
        assert shared_roots, f"{agent}: expected at least one shared root"
        for root in shared_roots:
            assert root.base_dir == expected, (
                f"{agent}: shared root {root.alias} resolved to {root.base_dir}, "
                f"expected the convention dir {expected}"
            )
        assert not hasattr(adapter, "list_personal"), agent
