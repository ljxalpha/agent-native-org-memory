from pathlib import Path

import pytest

from memory_net.shared.adapters import FileSystemMdAdapter

REPO_ROOT = Path(__file__).resolve().parents[2]
ADAPTERS_DIR = REPO_ROOT / "config" / "adapters"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "adapters"

CALIBRATION_MODULE = Path(__file__).with_name("test_adapter_calibration.py")

# Experimental adapters whose paths are pinned to a real on-disk layout by
# tests/unit/test_adapter_calibration.py rather than by a layout fixture dir.
# An entry here is not a free pass: test_calibration_allowlist_is_backed_by_tests
# requires the name to be exercised by that module, so adding a name without
# recording the evidence fails instead of silently widening the exemption.
CALIBRATED_WITHOUT_FIXTURE = frozenset({"codex", "hermes"})


@pytest.mark.parametrize("agent", ["hermes", "openclaw", "opencode", "codex"])
def test_agent_adapter_lists_shared_fixture(
    agent: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEMORY_NET_ALLOW_EXPERIMENTAL_ADAPTER", "1")
    agent_root = tmp_path / agent
    shared = agent_root / "memory" / "shared" / "notes"
    shared.mkdir(parents=True)
    (shared / "tip.md").write_text("Use retries.\n", encoding="utf-8")

    adapter = FileSystemMdAdapter.from_yaml(
        Path("config/adapters") / f"{agent}.yaml",
        agent_root=agent_root,
    )
    records = list(adapter.list_shared())

    assert len(records) == 1
    assert records[0].source_locator == f"{agent}-shared:notes/tip.md"


def test_experimental_adapter_requires_opt_in(tmp_path: Path) -> None:
    config = tmp_path / "adapter.yaml"
    config.write_text(
        """
name: test
support_status: experimental
roots:
  shared:
    - alias: shared
      base_dir: "${AGENT_ROOT}/memory/shared"
      include: ["**/*.md"]
team_target: "${AGENT_ROOT}/memory/team"
""",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="experimental"):
        FileSystemMdAdapter.from_yaml(config, agent_root=tmp_path / "agent")


def test_opencode_managed_root_rejects_symlink_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """opencode reads the convention root only; leaving it is an error.

    The adapter's paths are a convention rather than a discovered layout, so the
    boundary is what keeps an unverified adapter from reaching agent-private state:
    a symlink pointing out of ${AGENT_ROOT}/memory/shared must raise, not be
    silently skipped or followed.
    """
    monkeypatch.setenv("MEMORY_NET_ALLOW_EXPERIMENTAL_ADAPTER", "1")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    agent_root = tmp_path / "agent"
    shared = agent_root / "memory" / "shared"
    shared.mkdir(parents=True)
    (shared / "keep.md").write_text("team-facing note\n", encoding="utf-8")
    private = agent_root / "PRIVATE.md"
    private.write_text("agent-private state\n", encoding="utf-8")
    (shared / "sneaky.md").symlink_to(private)

    adapter = FileSystemMdAdapter.from_yaml(
        ADAPTERS_DIR / "opencode.yaml",
        agent_root=agent_root,
    )

    with pytest.raises(ValueError, match="escapes base_dir"):
        list(adapter.list_shared())


def test_uncalibrated_experimental_adapters_declare_themselves_unverified() -> None:
    """Meta-guard: an experimental adapter with no path evidence must say so.

    An adapter YAML carries no signal about how much its paths can be trusted
    beyond ``support_status``, which only says "opt-in required". Integrators
    additionally need to know whether the paths were observed on a real install or
    merely assumed. The rule: if an adapter is experimental and has neither a
    layout fixture under tests/fixtures/adapters/<name>/ nor an entry in
    CALIBRATED_WITHOUT_FIXTURE, its first comment line must carry an UNVERIFIED
    banner. This makes the asymmetry impossible to reintroduce with the next
    adapter someone adds.
    """
    checked = []
    for config in sorted(ADAPTERS_DIR.glob("*.yaml")):
        text = config.read_text(encoding="utf-8")
        if "support_status: experimental" not in text:
            continue
        name = config.stem
        calibrated = (
            FIXTURES_DIR / name
        ).is_dir() or name in CALIBRATED_WITHOUT_FIXTURE
        if calibrated:
            continue
        checked.append(name)
        first_line = text.splitlines()[0]
        assert "UNVERIFIED" in first_line, (
            f"{config.name}: experimental adapter with no recorded path evidence "
            f"must open with an UNVERIFIED banner, got: {first_line!r}"
        )

    # The rule is only meaningful while at least one such adapter exists.
    assert "opencode" in checked


def test_calibration_allowlist_is_backed_by_tests() -> None:
    """Every allowlist entry must be a checkable claim, not a written promise.

    Without this, the exemption above is a backdoor: the cheapest way to make the
    banner rule pass for a brand-new adapter is to add its name to
    CALIBRATED_WITHOUT_FIXTURE, and nothing would notice that no calibration
    evidence was ever recorded. Tie each name to a config that exists and to a
    calibration test that actually names it.
    """
    calibration = CALIBRATION_MODULE.read_text(encoding="utf-8")
    for name in sorted(CALIBRATED_WITHOUT_FIXTURE):
        assert (ADAPTERS_DIR / f"{name}.yaml").is_file(), (
            f"{name}: allowlisted but config/adapters/{name}.yaml does not exist; "
            "a stale exemption silently disables the UNVERIFIED banner rule"
        )
        assert f'"{name}"' in calibration, (
            f"{name}: allowlisted as calibrated-without-fixture, but "
            f"{CALIBRATION_MODULE.name} never exercises it — either record the "
            "calibration there or ship a layout fixture"
        )


@pytest.mark.parametrize("depth", ["top-level", "nested"])
def test_directory_excludes_apply_at_every_depth(
    depth: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``**/<dir>/**`` exclude must hold at the managed root itself, too.

    ``include`` is matched with :meth:`Path.glob`, where ``**/`` means "zero or
    more segments", while ``exclude`` is matched with ``fnmatch``, where ``**/``
    demands a real separator. Under the raw semantics ``**/draft/**`` dropped
    ``auth/draft/wip.md`` but let ``draft/wip.md`` straight through into the
    distiller input — an exclusion that looks enforced and is not. Both depths
    are pinned so the asymmetry cannot come back.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    agent_root = tmp_path / "agent"
    shared = agent_root / "memory" / "shared"
    parent = shared if depth == "top-level" else shared / "auth"
    (parent / "draft").mkdir(parents=True)
    (parent / "keep.md").write_text("team-facing note\n", encoding="utf-8")
    (parent / "draft" / "wip.md").write_text("not ready\n", encoding="utf-8")

    adapter = FileSystemMdAdapter.from_yaml(
        ADAPTERS_DIR / "claude-code.yaml",
        agent_root=agent_root,
    )
    records = list(adapter.list_shared())

    expected = "keep.md" if depth == "top-level" else "auth/keep.md"
    assert [r.source_locator for r in records] == [f"claude-project-shared:{expected}"]
    assert all("not ready" not in r.content for r in records)
