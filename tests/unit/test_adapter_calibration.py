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
import yaml  # type: ignore[import-untyped]

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


# -- fixture-driven: the shape of a real install, not a hand-built tree ------
#
# The tests above build a tmp tree that happens to look right. These ones
# materialize a redacted snapshot of an actual install, so the thing under test
# is whether the shipped YAML holds up against the *layout that exists* -- with
# its native store, its sibling lock files and its instruction file all present.

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "adapters"


FIXTURE_RENAMES = {"_dotgit": ".git", "_dotagents": ".agents", "_dotcodex": ".codex"}
"""Path components stored under a neutral name and restored on materialization.

A tracked directory named ``.git`` inside this repository would be read as a
nested checkout and its contents could not be committed at all.
"""


def _materialize(fixture: Path, destination: Path) -> Path:
    """Copy a redacted install snapshot; return it as AGENT_ROOT.

    Two fixture conventions are undone here so that what the adapter sees is the
    install rather than the fixture: neutral directory names are restored to
    their real dotted spellings, and a ``.keep`` marker becomes the empty
    directory git could not track.
    """
    for source in sorted(fixture.rglob("*")):
        if not source.is_file():
            continue
        relative = source.relative_to(fixture)
        if relative == Path("README.md"):
            continue  # fixture documentation, not part of the install
        parts = [FIXTURE_RENAMES.get(part, part) for part in relative.parts]
        target = destination.joinpath(*parts)
        if target.name == ".keep":
            target.parent.mkdir(parents=True, exist_ok=True)
            continue  # the directory is the asset; the marker is not
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    return destination


def test_hermes_fixture_reports_nothing_from_the_native_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole install is present, and only the managed root is collected.

    ``memories/MEMORY.md`` and ``memories/USER.md`` are the agent's own notes
    and ``AGENTS.md`` is its instruction file. All three are markdown, all three
    sit inside AGENT_ROOT, and none of them may be reported.
    """
    monkeypatch.setenv("MEMORY_NET_ALLOW_EXPERIMENTAL_ADAPTER", "1")
    agent_root = _materialize(FIXTURES / "hermes", tmp_path / "hermes")
    shared = agent_root / "memory" / "shared" / "auth"
    shared.mkdir(parents=True)
    (shared / "retry.md").write_text("Use bounded backoff.\n", encoding="utf-8")

    records = list(_load("hermes", agent_root).list_shared())

    assert [r.source_locator for r in records] == ["hermes-shared:auth/retry.md"]


def test_hermes_fixture_excludes_a_lock_beside_a_managed_note(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Why the exclude is ``**/*.lock`` and not a directory.

    The fixture shows the real placement: ``MEMORY.md.lock`` is a *sibling* of
    ``MEMORY.md``. So the moment an agent writes inside the managed root, a lock
    appears next to a note there too.
    """
    monkeypatch.setenv("MEMORY_NET_ALLOW_EXPERIMENTAL_ADAPTER", "1")
    agent_root = _materialize(FIXTURES / "hermes", tmp_path / "hermes")
    assert (agent_root / "memories" / "MEMORY.md.lock").is_file()
    shared = agent_root / "memory" / "shared"
    shared.mkdir(parents=True)
    (shared / "note.md").write_text("kept\n", encoding="utf-8")
    (shared / "note.md.lock").write_text("holder\n", encoding="utf-8")
    (shared / "nested").mkdir()
    (shared / "nested" / "deep.md.lock").write_text("holder\n", encoding="utf-8")

    records = list(_load("hermes", agent_root).list_shared())

    assert [r.source_locator for r in records] == ["hermes-shared:note.md"]


def test_hermes_fixture_hint_injection_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Injected twice into the real instruction file, unchanged the second time.

    Run repeatedly by every subscribe, so a non-idempotent injection grows the
    file the agent reads on every session until it displaces the instructions.
    """
    monkeypatch.setenv("MEMORY_NET_ALLOW_EXPERIMENTAL_ADAPTER", "1")
    agent_root = _materialize(FIXTURES / "hermes", tmp_path / "hermes")
    adapter = _load("hermes", agent_root)
    target = agent_root / "AGENTS.md"
    original = target.read_text(encoding="utf-8")

    assert adapter.inject_team_hint(adapter.hint_config) is True
    once = target.read_text(encoding="utf-8")
    assert adapter.inject_team_hint(adapter.hint_config) is False
    twice = target.read_text(encoding="utf-8")

    assert once == twice
    assert once.startswith(original.rstrip("\n"))
    assert "@memory/team/INDEX.md" in once
    assert once.count("<!-- memory-net-hint:start -->") == 1


def test_the_hermes_fixture_carries_no_real_content(tmp_path: Path) -> None:
    """A redacted snapshot, checked as one: the shape is the asset, and the
    content must stay neutral sample text."""
    for path in sorted((FIXTURES / "hermes").rglob("*")):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        assert "/home/" not in text
        assert "/Users/" not in text
        assert "@" not in text.replace("@memory/team/INDEX.md", "")


def test_hermes_records_a_positive_run_and_why_it_is_still_experimental() -> None:
    """A paired run on 2026-08-11 returned a positive result under a design
    that isolates the hint. The flag did not move, and the file has to say
    both halves of that.

    `stable` is a claim about what happens when someone else runs this, and the
    evidence is one unreplicated pair, in one order, against a model that
    samples. Pinned because a positive result is exactly what gets rounded up
    into a capability -- it was rounded up once here already, and was wrong.
    """
    raw = (ADAPTERS_DIR / "hermes.yaml").read_text(encoding="utf-8")
    config = yaml.safe_load(raw)
    # Comment text is line-wrapped, so it is joined before matching: an
    # assertion a reflow can break would get "fixed" by deleting it.
    text = " ".join(line.lstrip("# ") for line in raw.splitlines())

    assert config["support_status"] == "experimental"
    # The result, including the control that makes it a measurement.
    assert "the reference alone removed" in text
    assert "sentinel not returned" in text
    assert "does reach the system prompt" in text
    # And why the flag did not move.
    assert "One observation is not a rate" in text
    assert "no retained proof its control answered" in text
    assert "does not follow from the byte delta alone" in text
    # A stated route out, so the status is a step rather than a verdict.
    assert "To promote" in text


def test_the_other_calibrated_adapters_are_still_experimental() -> None:
    """Promoting hermes must not read as promoting the read-path work in
    general: codex and openclaw have calibrated read paths and no consumption
    evidence, which is exactly the distinction the flag exists to carry."""
    for agent in ("codex", "openclaw"):
        config = yaml.safe_load(
            (ADAPTERS_DIR / f"{agent}.yaml").read_text(encoding="utf-8")
        )
        assert config["support_status"] == "experimental", agent


# -- codex: the read path against the layout that exists -----------------


def test_codex_fixture_reports_nothing_from_the_native_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``memories/project-notes.md`` is markdown, inside AGENT_ROOT, and still
    uncollectable.

    Position decides it, before any ``exclude`` is consulted: the reporting root
    is ``${AGENT_ROOT}/memory/shared``, and the native store is not under it.
    """
    monkeypatch.setenv("MEMORY_NET_ALLOW_EXPERIMENTAL_ADAPTER", "1")
    agent_root = _materialize(FIXTURES / "codex", tmp_path / "codex")
    assert (agent_root / "memories" / "project-notes.md").is_file()
    shared = agent_root / "memory" / "shared" / "deploy"
    shared.mkdir(parents=True)
    (shared / "window.md").write_text("Deploy on weekday mornings.\n", encoding="utf-8")

    records = list(_load("codex", agent_root).list_shared())

    assert [r.source_locator for r in records] == ["codex-shared:deploy/window.md"]


def test_codex_fixture_keeps_a_checkout_inside_the_managed_root_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fixture shows the native store is a real checkout, and the same thing
    happens inside the managed root the moment an operator versions it.

    What rejects these is the hidden-path skip, which runs *before* ``exclude``
    is consulted (``filesystem_md.py``: ``_is_hidden_path`` precedes the pattern
    loop) -- so the config's ``.git/**`` and ``**/.git/**`` entries are declared
    cover for a future config that stops skipping hidden paths, and what this
    test pins is the boundary rather than the machinery. Both depths are built
    because a top-level-only rule would be indistinguishable here otherwise.
    """
    monkeypatch.setenv("MEMORY_NET_ALLOW_EXPERIMENTAL_ADAPTER", "1")
    agent_root = _materialize(FIXTURES / "codex", tmp_path / "codex")
    assert (agent_root / "memories" / ".git").is_dir()
    shared = agent_root / "memory" / "shared"
    (shared / ".git" / "info").mkdir(parents=True)
    (shared / ".git" / "info" / "notes.md").write_text("plumbing\n", encoding="utf-8")
    (shared / "nested" / ".git").mkdir(parents=True)
    (shared / "nested" / ".git" / "notes.md").write_text("plumbing\n", encoding="utf-8")
    (shared / "kept.md").write_text("Team-visible note.\n", encoding="utf-8")

    records = list(_load("codex", agent_root).list_shared())

    assert [r.source_locator for r in records] == ["codex-shared:kept.md"]


def test_codex_sqlite_siblings_are_out_of_reach_without_an_exclude_for_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deliberately no ``**/*.sqlite*`` pattern in the shipped config.

    The trio sits beside ``memories/``, one level above the managed root, so a
    relative glob rooted at ``memory/shared`` cannot reach it. A second
    mechanism agreeing with the first would only suggest that a ``.sqlite``
    *inside* the managed root is expected -- it is not.
    """
    monkeypatch.setenv("MEMORY_NET_ALLOW_EXPERIMENTAL_ADAPTER", "1")
    agent_root = _materialize(FIXTURES / "codex", tmp_path / "codex")
    for suffix in ("", "-shm", "-wal"):
        assert (agent_root / f"memories_1.sqlite{suffix}").is_file()
    config = yaml.safe_load((ADAPTERS_DIR / "codex.yaml").read_text(encoding="utf-8"))
    excludes = config["roots"]["shared"][0]["exclude"]
    assert not [pattern for pattern in excludes if "sqlite" in pattern]
    shared = agent_root / "memory" / "shared"
    shared.mkdir(parents=True)
    (shared / "kept.md").write_text("Team-visible note.\n", encoding="utf-8")

    records = list(_load("codex", agent_root).list_shared())

    assert [r.source_locator for r in records] == ["codex-shared:kept.md"]


def test_codex_dot_directories_in_the_native_store_stay_unreported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``.agents/`` and ``.codex/`` are empty in a stock install, so a walk that
    reached them would find nothing and the read path would look correct.

    Filled here with markdown, which is what makes the assertion mean anything:
    they are still not reported, and the reason is the reporting root rather
    than luck about what happens to be in them.
    """
    monkeypatch.setenv("MEMORY_NET_ALLOW_EXPERIMENTAL_ADAPTER", "1")
    agent_root = _materialize(FIXTURES / "codex", tmp_path / "codex")
    for name in (".agents", ".codex"):
        directory = agent_root / "memories" / name
        assert directory.is_dir() and not list(directory.iterdir())
        (directory / "agent-private.md").write_text("private\n", encoding="utf-8")
    shared = agent_root / "memory" / "shared"
    shared.mkdir(parents=True)
    (shared / "kept.md").write_text("Team-visible note.\n", encoding="utf-8")

    records = list(_load("codex", agent_root).list_shared())

    assert [r.source_locator for r in records] == ["codex-shared:kept.md"]


def test_codex_fixture_hint_injection_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Injected twice into the instruction file, unchanged the second time.

    Run by every subscribe, so a non-idempotent injection grows the file the
    agent reads on every session until it displaces the instructions. The
    fixture has no ``AGENTS.md`` -- a stock install does not -- so this also
    covers the install-once path creating one.
    """
    monkeypatch.setenv("MEMORY_NET_ALLOW_EXPERIMENTAL_ADAPTER", "1")
    agent_root = _materialize(FIXTURES / "codex", tmp_path / "codex")
    adapter = _load("codex", agent_root)
    target = agent_root / "AGENTS.md"
    assert not target.exists()

    assert adapter.inject_team_hint(adapter.hint_config) is True
    once = target.read_text(encoding="utf-8")
    assert adapter.inject_team_hint(adapter.hint_config) is False
    twice = target.read_text(encoding="utf-8")

    assert once == twice
    assert "@memory/team/INDEX.md" in once
    assert once.count("<!-- memory-net-hint:start -->") == 1


def test_the_codex_fixture_carries_no_real_content() -> None:
    """A redacted snapshot, checked as one."""
    for path in sorted((FIXTURES / "codex").rglob("*")):
        if not path.is_file() or path.name == "README.md":
            continue
        text = path.read_bytes().decode("utf-8")
        assert "/home/" not in text
        assert "/Users/" not in text
        assert "@" not in text


def test_codex_stays_experimental_until_a_real_run_says_otherwise() -> None:
    """A calibrated read path is not evidence of a consumed hint."""
    config = yaml.safe_load((ADAPTERS_DIR / "codex.yaml").read_text(encoding="utf-8"))

    assert config["support_status"] == "experimental"
