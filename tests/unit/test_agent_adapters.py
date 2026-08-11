from pathlib import Path

import pytest

from memory_net.shared.adapters import FileSystemMdAdapter

REPO_ROOT = Path(__file__).resolve().parents[2]
ADAPTERS_DIR = REPO_ROOT / "config" / "adapters"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "adapters"

# Experimental adapters whose paths are pinned to a real on-disk layout by
# tests/unit/test_adapter_calibration.py rather than by a layout fixture dir.
# Adding a name here is a claim that recorded provenance exists for it.
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
