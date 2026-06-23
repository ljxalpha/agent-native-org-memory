import json
import os
from pathlib import Path
from typing import Any

from memory_net.leaf import hints
from memory_net.shared.adapters import FileSystemMdAdapter
from memory_net.shared.models import TeamProduct, TeamWritePlan


def test_filesystem_adapter_lists_shared_and_writes_team(tmp_path: Path) -> None:
    agent_root = tmp_path / "agent"
    shared = agent_root / "memory" / "shared" / "auth"
    shared.mkdir(parents=True)
    (shared / "oauth.md").write_text(
        "---\ntags: [auth]\n---\nUse retries.\n", encoding="utf-8"
    )
    config = tmp_path / "adapter.yaml"
    config.write_text(
        """
name: claude-code
roots:
  shared:
    - alias: claude-project-shared
      base_dir: "${AGENT_ROOT}/memory/shared"
      include: ["**/*.md"]
team_target: "${AGENT_ROOT}/memory/team"
file_pattern: "*.md"
frontmatter_required: false
""",
        encoding="utf-8",
    )

    adapter = FileSystemMdAdapter.from_yaml(config, agent_root=agent_root)
    records = list(adapter.list_shared())

    assert len(records) == 1
    assert records[0].source_locator == "claude-project-shared:auth/oauth.md"

    adapter.write_team(
        TeamWritePlan(
            products=[
                TeamProduct(
                    relative_path=Path("team/common/oauth.md"),
                    content="# OAuth\n",
                    frontmatter={"title": "OAuth"},
                    category="common",
                    distillation_id="distill-1",
                )
            ],
            config_revision="cfg",
            merge_strategy="replace",
        )
    )

    assert (agent_root / "memory" / "team" / "common" / "oauth.md").exists()
    assert (agent_root / "memory" / "team" / "INDEX.md").exists()


def test_filesystem_adapter_rejects_base_dir_outside_allowed_roots(
    tmp_path: Path,
) -> None:
    config = tmp_path / "adapter.yaml"
    config.write_text(
        """
name: claude-code
roots:
  shared:
    - alias: outside
      base_dir: "/etc"
      include: ["**/*.md"]
team_target: "${AGENT_ROOT}/memory/team"
""",
        encoding="utf-8",
    )

    try:
        FileSystemMdAdapter.from_yaml(config, agent_root=tmp_path / "agent")
    except ValueError as exc:
        assert "base_dir" in str(exc)
    else:
        raise AssertionError("expected adapter path rejection")


def test_filesystem_adapter_rejects_team_target_outside_allowed_roots(
    tmp_path: Path,
) -> None:
    config = tmp_path / "adapter.yaml"
    config.write_text(
        """
name: claude-code
roots:
  shared:
    - alias: shared
      base_dir: "${AGENT_ROOT}/memory/shared"
      include: ["**/*.md"]
team_target: "/var/tmp/memory-net-team"
""",
        encoding="utf-8",
    )

    try:
        FileSystemMdAdapter.from_yaml(config, agent_root=tmp_path / "agent")
    except ValueError as exc:
        assert "team_target" in str(exc)
    else:
        raise AssertionError("expected adapter path rejection")


def test_filesystem_adapter_allows_home_paths(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    config = tmp_path / "adapter.yaml"
    config.write_text(
        """
name: claude-code
roots:
  shared:
    - alias: home-shared
      base_dir: "~/memory/shared"
      include: ["**/*.md"]
team_target: "~/memory/team"
""",
        encoding="utf-8",
    )

    adapter = FileSystemMdAdapter.from_yaml(config, agent_root=tmp_path / "agent")

    assert adapter.team_target == home / "memory" / "team"


def test_hint_inject_and_remove_uses_existing_candidate(tmp_path: Path) -> None:
    adapter = _hint_adapter(tmp_path)
    readme = tmp_path / "agent" / "README.md"
    readme.write_text("# Project\n", encoding="utf-8")

    assert adapter.inject_team_hint(adapter.hint_config) is True
    assert "@memory/team/INDEX.md" in readme.read_text(encoding="utf-8")
    assert adapter.inject_team_hint(adapter.hint_config) is False

    assert adapter.remove_team_hint(adapter.hint_config) is True
    assert "@memory/team/INDEX.md" not in readme.read_text(encoding="utf-8")


def test_write_team_restores_backup_when_final_rename_fails(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    adapter = _hint_adapter(tmp_path)
    existing = adapter.team_target / "old.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("old\n", encoding="utf-8")
    real_replace = os.replace
    calls = 0

    def failing_replace(src: str | Path, dst: str | Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected final rename failure")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", failing_replace)

    try:
        adapter.write_team(
            TeamWritePlan(
                products=[
                    TeamProduct(
                        relative_path=Path("team/new.md"),
                        content="new\n",
                        frontmatter={},
                        category="common",
                        distillation_id="distill-1",
                    )
                ],
                config_revision="cfg",
                merge_strategy="replace",
            )
        )
    except OSError:
        pass

    assert existing.read_text(encoding="utf-8") == "old\n"


def test_hint_inject_uses_install_once_default_when_no_candidate_exists(
    tmp_path: Path,
) -> None:
    adapter = _hint_adapter(tmp_path)
    target = tmp_path / "agent" / "CLAUDE.md"

    assert adapter.inject_team_hint(adapter.hint_config) is True
    assert target.exists()
    assert "@memory/team/INDEX.md" in target.read_text(encoding="utf-8")


def test_hint_sync_reinjects_managed_install_once_on_config_change(
    tmp_path: Path,
) -> None:
    old_adapter = _hint_adapter_with_template(tmp_path, "@memory/team/INDEX.md\n")
    target = tmp_path / "agent" / "CLAUDE.md"
    state_path = tmp_path / "state" / "hints_state.json"
    assert old_adapter.inject_team_hint(old_adapter.hint_config) is True
    _write_hint_state(state_path, old_adapter, managed_state="installed")

    new_adapter = _hint_adapter_with_template(
        tmp_path,
        "@memory/team/INDEX.md\nUpdated\n",
    )

    assert (
        hints.sync_config_change(
            old_adapter,
            new_adapter,
            state_path=state_path,
            config_revision="cfg2",
        )
        is True
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert "Updated" in target.read_text(encoding="utf-8")
    assert state["managed_state"] == "installed"
    assert state["yaml_sig"] == hints.hint_config_signature(new_adapter.hint_config)


def test_hint_sync_removes_managed_block_when_mode_becomes_never(
    tmp_path: Path,
) -> None:
    old_adapter = _hint_adapter_with_template(tmp_path, "@memory/team/INDEX.md\n")
    target = tmp_path / "agent" / "CLAUDE.md"
    state_path = tmp_path / "state" / "hints_state.json"
    assert old_adapter.inject_team_hint(old_adapter.hint_config) is True
    _write_hint_state(state_path, old_adapter, managed_state="installed")
    new_adapter = _hint_adapter_with_template(
        tmp_path,
        "@memory/team/INDEX.md\n",
        mode="never",
    )

    assert (
        hints.sync_config_change(
            old_adapter,
            new_adapter,
            state_path=state_path,
            config_revision="cfg2",
        )
        is True
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert "@memory/team/INDEX.md" not in target.read_text(encoding="utf-8")
    assert state["managed_state"] == "disabled_by_mode"


def test_hint_inject_persists_managed_state(tmp_path: Path) -> None:
    adapter = _hint_adapter(tmp_path)
    state_path = tmp_path / "state" / "hints_state.json"

    assert (
        hints.inject(
            adapter,
            state_path=state_path,
            config_revision="cfg1",
        )
        is True
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["adapter"] == "claude-code"
    assert state["config_revision"] == "cfg1"
    assert state["managed_state"] == "installed"
    assert state["yaml_sig"] == hints.hint_config_signature(adapter.hint_config)


def test_hint_remove_persists_removed_state(tmp_path: Path) -> None:
    adapter = _hint_adapter(tmp_path)
    state_path = tmp_path / "state" / "hints_state.json"
    assert (
        hints.inject(
            adapter,
            state_path=state_path,
            config_revision="cfg1",
        )
        is True
    )

    assert (
        hints.remove(
            adapter,
            state_path=state_path,
            config_revision="cfg2",
        )
        is True
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["config_revision"] == "cfg2"
    assert state["managed_state"] == "removed"


def test_list_shared_skips_hidden_dirs_and_uses_stable_order(tmp_path: Path) -> None:
    agent_root = tmp_path / "agent"
    shared = agent_root / "memory" / "shared"
    (shared / ".git").mkdir(parents=True)
    (shared / "b").mkdir()
    (shared / "a").mkdir()
    (shared / ".git" / "secret.md").write_text("secret", encoding="utf-8")
    (shared / "b" / "note.md").write_text("b", encoding="utf-8")
    (shared / "a" / "note.md").write_text("a", encoding="utf-8")
    config = tmp_path / "adapter.yaml"
    config.write_text(
        """
name: claude-code
roots:
  shared:
    - alias: shared
      base_dir: "${AGENT_ROOT}/memory/shared"
      include: ["**/*.md"]
team_target: "${AGENT_ROOT}/memory/team"
""",
        encoding="utf-8",
    )

    adapter = FileSystemMdAdapter.from_yaml(config, agent_root=agent_root)

    assert [record.source_locator for record in adapter.list_shared()] == [
        "shared:a/note.md",
        "shared:b/note.md",
    ]


def test_list_shared_rejects_symlink_escape(tmp_path: Path) -> None:
    agent_root = tmp_path / "agent"
    shared = agent_root / "memory" / "shared"
    shared.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("secret\n", encoding="utf-8")
    (shared / "escape.md").symlink_to(outside)
    config = tmp_path / "adapter.yaml"
    config.write_text(
        """
name: claude-code
roots:
  shared:
    - alias: shared
      base_dir: "${AGENT_ROOT}/memory/shared"
      include: ["**/*.md"]
team_target: "${AGENT_ROOT}/memory/team"
""",
        encoding="utf-8",
    )

    adapter = FileSystemMdAdapter.from_yaml(config, agent_root=agent_root)

    try:
        list(adapter.list_shared())
    except ValueError as exc:
        assert "escapes base_dir" in str(exc)
    else:
        raise AssertionError("expected symlink escape rejection")


def test_hint_target_outside_allowed_roots_is_rejected(tmp_path: Path) -> None:
    agent_root = tmp_path / "agent"
    (agent_root / "memory" / "shared").mkdir(parents=True, exist_ok=True)
    config = tmp_path / "adapter.yaml"
    config.write_text(
        """
name: claude-code
roots:
  shared:
    - alias: shared
      base_dir: "${AGENT_ROOT}/memory/shared"
      include: ["**/*.md"]
team_target: "${AGENT_ROOT}/memory/team"
team_hint_injection:
  mode: install_once
  target: "/var/tmp/memory-net-hint.md"
  content_template: |
    @memory/team/INDEX.md
""",
        encoding="utf-8",
    )

    adapter = FileSystemMdAdapter.from_yaml(config, agent_root=agent_root)

    try:
        adapter.inject_team_hint(adapter.hint_config)
    except ValueError as exc:
        assert "hint target" in str(exc)
    else:
        raise AssertionError("expected hint target rejection")


def _hint_adapter(tmp_path: Path) -> FileSystemMdAdapter:
    return _hint_adapter_with_template(tmp_path, "@memory/team/INDEX.md\n")


def _hint_adapter_with_template(
    tmp_path: Path,
    template: str,
    *,
    mode: str = "install_once",
) -> FileSystemMdAdapter:
    agent_root = tmp_path / "agent"
    (agent_root / "memory" / "shared").mkdir(parents=True, exist_ok=True)
    config = tmp_path / "adapter.yaml"
    config.write_text(
        """
name: claude-code
roots:
  shared:
    - alias: shared
      base_dir: "${AGENT_ROOT}/memory/shared"
      include: ["**/*.md"]
team_target: "${AGENT_ROOT}/memory/team"
team_hint_injection:
  mode: __MODE__
  target: "${AGENT_ROOT}/CLAUDE.md"
  install_once_default: "${AGENT_ROOT}/CLAUDE.md"
  fallback_targets:
    - "${AGENT_ROOT}/README.md"
  content_template: |
__TEMPLATE__
""".replace("__MODE__", mode).replace(
            "__TEMPLATE__",
            "\n".join(f"    {line}" for line in template.rstrip().splitlines()),
        ),
        encoding="utf-8",
    )
    return FileSystemMdAdapter.from_yaml(config, agent_root=agent_root)


def _write_hint_state(
    path: Path,
    adapter: FileSystemMdAdapter,
    *,
    managed_state: str,
) -> None:
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "managed_state": managed_state,
                "yaml_sig": hints.hint_config_signature(adapter.hint_config),
            }
        )
        + "\n",
        encoding="utf-8",
    )
