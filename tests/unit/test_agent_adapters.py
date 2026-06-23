from pathlib import Path

import pytest

from memory_net.shared.adapters import FileSystemMdAdapter


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
