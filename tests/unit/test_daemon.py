import builtins
import json
import os
import subprocess
from argparse import Namespace
from contextlib import nullcontext
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import memory_net.leaf.cli as leaf_cli
import memory_net.leaf.daemon as daemon_module
from memory_net.leaf.daemon import run_foreground
from memory_net.shared.config import canonical_generation_hash


def test_run_foreground_once_executes_all_tasks_in_order(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    _force_fallback_scheduler(monkeypatch)
    calls: list[str] = []

    run_foreground(
        node_id="alice",
        state_dir=tmp_path,
        config_sync_task=lambda: calls.append("config"),
        report_task=lambda: calls.append("report"),
        subscribe_task=lambda: calls.append("subscribe"),
        once=True,
    )

    assert calls == ["config", "report", "subscribe"]
    assert (tmp_path / "last_health.json").exists()


def test_run_foreground_stop_marker_breaks_loop(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    _force_fallback_scheduler(monkeypatch)

    def config_sync() -> None:
        (tmp_path / "stop-requested").write_text("stop\n", encoding="utf-8")

    run_foreground(
        node_id="alice",
        state_dir=tmp_path,
        config_sync_task=config_sync,
        interval_seconds=1,
    )

    assert (tmp_path / "stop-requested").exists()


def test_run_foreground_continues_when_one_task_raises(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    _force_fallback_scheduler(monkeypatch)
    calls: list[str] = []

    def config_sync() -> None:
        raise RuntimeError("boom")

    run_foreground(
        node_id="alice",
        state_dir=tmp_path,
        config_sync_task=config_sync,
        report_task=lambda: calls.append("report"),
        subscribe_task=lambda: calls.append("subscribe"),
        once=True,
    )

    health = json.loads((tmp_path / "last_health.json").read_text(encoding="utf-8"))
    assert calls == ["report", "subscribe"]
    assert health["status"] == "degraded"
    assert "config_sync: boom" in health["recent_errors"]


def test_run_foreground_apscheduler_once_executes_tasks_in_order(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    calls: list[str] = []

    class FakeScheduler:
        def __init__(self) -> None:
            self.jobs: list[tuple[str, Any]] = []

        def add_job(self, task: Any, _trigger: str, *, seconds: int, id: str) -> None:
            self.jobs.append((id, task))

        def start(self) -> None:
            calls.append("scheduler-start")

        def shutdown(self) -> None:
            calls.append("scheduler-stop")

    module = ModuleType("apscheduler.schedulers.background")
    module.BackgroundScheduler = FakeScheduler
    monkeypatch.setattr(daemon_module, "import_module", lambda _name: module)

    run_foreground(
        node_id="alice",
        state_dir=tmp_path,
        config_sync_task=lambda: calls.append("config"),
        report_task=lambda: calls.append("report"),
        subscribe_task=lambda: calls.append("subscribe"),
        once=True,
    )

    assert calls == [
        "scheduler-start",
        "config",
        "report",
        "subscribe",
        "scheduler-stop",
    ]


def test_daemon_subscribe_reuses_one_master_snapshot(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    calls: list[tuple[str, str | None]] = []
    args = Namespace(
        repo=tmp_path / "repo",
        adapter=Path("adapter.yaml"),
        agent_root=tmp_path / "agent",
        node_id="alice",
        state_dir=tmp_path / "state",
        config_revision="local",
    )

    monkeypatch.setattr(leaf_cli, "_repo_lock", lambda *_args: nullcontext())
    monkeypatch.setattr(leaf_cli, "_origin_master_snapshot", lambda _repo: "snap-1")
    monkeypatch.setattr(
        leaf_cli,
        "_ensure_subscribe_contract_current",
        lambda _repo, _home, *, adapter_name=None, snapshot_sha=None: calls.append(
            ("contract", f"{adapter_name}:{snapshot_sha}")
        ),
    )
    monkeypatch.setattr(
        leaf_cli,
        "_subscribe_options",
        lambda _repo, _home: ("replace", True, None),
    )
    monkeypatch.setattr(
        leaf_cli,
        "_subscribe_from_master_snapshot",
        lambda _args, **kwargs: calls.append(("subscribe", kwargs["snapshot_sha"])),
    )
    monkeypatch.setattr(leaf_cli, "_prepare_leaf_branch", lambda *_args: None)

    _report_task, subscribe_task, _config_task, _health_task = leaf_cli._daemon_tasks(
        args
    )
    assert subscribe_task is not None
    subscribe_task()

    assert calls == [("contract", "adapter:snap-1"), ("subscribe", "snap-1")]


def test_daemon_config_sync_uses_frozen_snapshot_without_master_switch(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    calls: list[tuple[str, str]] = []
    args = Namespace(
        repo=tmp_path / "repo",
        adapter=Path("claude-code.yaml"),
        agent_root=tmp_path / "agent",
        node_id="alice",
        state_dir=tmp_path / "state",
        config_revision="local",
    )
    generation = tmp_path / "config.generations" / "abc"
    (generation / "adapters").mkdir(parents=True)
    (generation / "adapters" / "claude-code.yaml").write_text(
        "name: claude-code\nteam_target: ${AGENT_ROOT}/memory/team\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(leaf_cli, "_repo_lock", lambda *_args: nullcontext())
    monkeypatch.setattr(
        leaf_cli,
        "_prepare_master_branch",
        lambda _repo: (_ for _ in ()).throw(
            AssertionError("config sync must not switch master")
        ),
    )
    monkeypatch.setattr(leaf_cli, "_origin_master_snapshot", lambda _repo: "snap-1")
    monkeypatch.setattr(
        leaf_cli,
        "_validate_bootstrap_state_from_git_snapshot",
        lambda _repo, snapshot: calls.append(("bootstrap", snapshot)),
    )

    def materialize(
        _repo: Path,
        snapshot: str,
        _generations_root: Path,
        *,
        adapter_name: str,
        source_master_sha: str | None = None,
    ) -> Path:
        calls.append(("materialize", f"{adapter_name}:{snapshot}:{source_master_sha}"))
        return generation

    monkeypatch.setattr(
        leaf_cli,
        "_materialize_generation_from_git_snapshot",
        materialize,
    )
    monkeypatch.setattr(
        leaf_cli,
        "_prepare_leaf_branch",
        lambda _repo, node_id: calls.append(("leaf", node_id)),
    )

    _report_task, _subscribe_task, config_task, _health_task = leaf_cli._daemon_tasks(
        args
    )
    assert config_task is not None
    config_task()

    assert calls == [
        ("bootstrap", "snap-1"),
        ("materialize", "claude-code:snap-1:snap-1"),
        ("leaf", "alice"),
    ]
    assert (tmp_path / "config-current").resolve() == generation


def test_daemon_report_publishes_health_only_on_scanner_block(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    calls: list[tuple[str, str]] = []
    args = Namespace(
        repo=tmp_path / "repo",
        adapter=Path("adapter.yaml"),
        agent_root=tmp_path / "agent",
        node_id="alice",
        state_dir=tmp_path / "state",
        no_push=False,
        config_revision="local",
    )

    def blocked_report(**kwargs: Any) -> None:
        health_path = kwargs["repo_path"] / ".health.json"
        health_path.parent.mkdir(parents=True, exist_ok=True)
        health_path.write_text('{"status":"degraded"}\n', encoding="utf-8")
        raise leaf_cli.RedactionBlockedError("external scanner blocked")

    monkeypatch.setattr(leaf_cli, "_repo_lock", lambda *_args: nullcontext())
    monkeypatch.setattr(leaf_cli, "_prepare_leaf_branch", lambda *_args: None)
    monkeypatch.setattr(leaf_cli, "_daemon_redaction_path", lambda _args: tmp_path)
    monkeypatch.setattr(
        leaf_cli.Redactor,
        "from_yaml",
        staticmethod(lambda _path: object()),
    )
    monkeypatch.setattr(leaf_cli, "_report_config", lambda _args: {})
    monkeypatch.setattr(leaf_cli, "_report_adapter", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(leaf_cli, "report", blocked_report)
    monkeypatch.setattr(
        leaf_cli,
        "_commit_and_push_report",
        lambda _repo, _node, *, no_push, paths=None, message=None: calls.append(
            (",".join(paths or []), str(message))
        )
        or True,
    )

    report_task, _subscribe_task, _config_task, _health_task = leaf_cli._daemon_tasks(
        args
    )
    assert report_task is not None
    with pytest.raises(leaf_cli.RedactionBlockedError):
        report_task()

    assert calls == [(".health.json", "health leaf alice")]
    assert (args.state_dir / "last_health_commit_at").exists()


def _force_fallback_scheduler(monkeypatch: Any) -> None:
    real_import = builtins.__import__

    def fake_import(
        name: str,
        globals: dict[str, object] | None = None,
        locals: dict[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> ModuleType:
        if name == "apscheduler.schedulers.background":
            raise ImportError("forced")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)


OPS_IDENTITY_SAMPLE = {
    "adapter": "claude-code",
    "install_id": "inst-0001",
    "code_version": "0.1.0",
    "host": "leaf-host-01",
    "build_id": "0123456789abcdef0123456789abcdef01234567",
    "config_source_master_sha": "b" * 40,
    "started_at": "2026-04-20T09:00:00Z",
}


def _install_layout(tmp_path: Path, monkeypatch: Any) -> Path:
    """Build a minimal ``MEMORY_NET_HOME`` the way install_leaf.sh lays it out."""
    home = tmp_path / "mn-home"
    (home / "state").mkdir(parents=True)
    (home / "state" / "install_id").write_text("inst-0001\n", encoding="utf-8")
    generation = home / "config.generations" / "gen1"
    (generation / "adapters").mkdir(parents=True)
    (generation / "adapters" / "claude-code.yaml").write_text(
        "name: claude-code\n", encoding="utf-8"
    )
    (generation / "config_source_master_sha").write_text("b" * 40, encoding="utf-8")
    (home / "config-current").symlink_to(generation)
    (home / "local.yaml").write_text(
        'agent: "claude-code"\n'
        'package_ref: "git+ssh://git@example.invalid/org/aomt.git@'
        '0123456789abcdef0123456789abcdef01234567"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MEMORY_NET_HOME", str(home))
    monkeypatch.setattr(leaf_cli, "_PROCESS_STARTED_AT", "2026-04-20T09:00:00Z")
    monkeypatch.setattr(leaf_cli.platform, "node", lambda: "leaf-host-01")
    return home


def test_health_command_stamps_operational_identity(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _install_layout(tmp_path, monkeypatch)
    health_path = tmp_path / ".health.json"

    assert leaf_cli.main(["health", "--path", str(health_path), "--node-id", "a"]) == 0

    health = json.loads(health_path.read_text(encoding="utf-8"))
    for field, value in OPS_IDENTITY_SAMPLE.items():
        assert health[field] == value
    assert health["build_id_source"] == "package_ref"


def test_health_command_degrades_gracefully_without_an_install(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A dev run without an install layout must still produce a health file."""
    monkeypatch.setenv("MEMORY_NET_HOME", str(tmp_path / "absent"))
    health_path = tmp_path / ".health.json"

    assert leaf_cli.main(["health", "--path", str(health_path), "--node-id", "a"]) == 0

    health = json.loads(health_path.read_text(encoding="utf-8"))
    for field in OPS_IDENTITY_SAMPLE:
        assert field not in health


def test_status_accepts_a_complete_health_file(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    _install_layout(tmp_path, monkeypatch)
    health_path = tmp_path / ".health.json"
    leaf_cli.main(["health", "--path", str(health_path), "--node-id", "a"])

    assert leaf_cli.main(["status", "--health", str(health_path)]) == 0
    assert '"host": "leaf-host-01"' in capsys.readouterr().out


def test_status_rejects_a_health_file_without_operational_identity(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    monkeypatch.setenv("MEMORY_NET_HOME", str(tmp_path / "absent"))
    health_path = tmp_path / ".health.json"
    leaf_cli.main(["health", "--path", str(health_path), "--node-id", "a"])

    assert leaf_cli.main(["status", "--health", str(health_path)]) == 1
    err = capsys.readouterr().err
    for field in OPS_IDENTITY_SAMPLE:
        assert field in err


def test_status_lenient_accepts_a_pre_identity_health_file(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    monkeypatch.setenv("MEMORY_NET_HOME", str(tmp_path / "absent"))
    health_path = tmp_path / ".health.json"
    leaf_cli.main(["health", "--path", str(health_path), "--node-id", "a"])

    assert leaf_cli.main(["status", "--health", str(health_path), "--lenient"]) == 0
    assert '"node_id": "a"' in capsys.readouterr().out


def test_status_reports_a_corrupt_health_file_without_a_traceback(
    tmp_path: Path, capsys: Any
) -> None:
    health_path = tmp_path / ".health.json"
    health_path.write_text("{not json", encoding="utf-8")

    assert leaf_cli.main(["status", "--health", str(health_path)]) == 1
    assert "not valid JSON" in capsys.readouterr().err


def test_status_reports_a_missing_health_file_without_a_traceback(
    tmp_path: Path, capsys: Any
) -> None:
    assert leaf_cli.main(["status", "--health", str(tmp_path / "absent.json")]) == 1
    assert "cannot read health file" in capsys.readouterr().err


def test_config_sync_records_the_snapshot_sha_as_generation_provenance(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Config sync repoints ``config-current``; provenance must survive it.

    This drives the real ``materialize_generation()`` over a real git snapshot
    rather than a stub, so it fails if the snapshot sha stops being threaded
    through to the marker.
    """
    repo = tmp_path / "repo"
    (repo / "config" / "adapters").mkdir(parents=True)
    (repo / "bootstrap_state.json").write_text("{}", encoding="utf-8")
    (repo / "config" / "subscribe.yaml").write_text("a: 1\n", encoding="utf-8")
    (repo / "config" / "adapters" / "claude-code.yaml").write_text(
        "name: claude-code\nteam_target: ${AGENT_ROOT}/memory/team\n",
        encoding="utf-8",
    )
    snapshot_sha = _init_repo_with_one_commit(repo)
    args = Namespace(
        repo=repo,
        adapter=Path("claude-code.yaml"),
        agent_root=tmp_path / "agent",
        node_id="alice",
        state_dir=tmp_path / "state",
        config_revision="local",
    )

    monkeypatch.setattr(leaf_cli, "_repo_lock", lambda *_args: nullcontext())
    monkeypatch.setattr(leaf_cli, "_origin_master_snapshot", lambda _repo: snapshot_sha)
    monkeypatch.setattr(
        leaf_cli,
        "_validate_bootstrap_state_from_git_snapshot",
        lambda _repo, _snapshot: None,
    )
    monkeypatch.setattr(leaf_cli, "_prepare_leaf_branch", lambda _repo, _node: None)

    _report, _subscribe, config_task, _health = leaf_cli._daemon_tasks(args)
    assert config_task is not None
    config_task()

    active = (tmp_path / "config-current").resolve()
    assert (active / "config_source_master_sha").read_text(
        encoding="utf-8"
    ) == snapshot_sha
    assert canonical_generation_hash(active) == active.name


def _init_repo_with_one_commit(repo: Path) -> str:
    """Commit the working tree and return the resulting commit sha."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "memory-net",
        "GIT_AUTHOR_EMAIL": "memory-net@example.invalid",
        "GIT_COMMITTER_NAME": "memory-net",
        "GIT_COMMITTER_EMAIL": "memory-net@example.invalid",
    }

    def git(*argv: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(repo), *argv],
            check=True,
            text=True,
            capture_output=True,
            env=env,
        )
        return result.stdout.strip()

    git("init", "-q", "-b", "master")
    git("add", "-A")
    git("commit", "-qm", "seed")
    return git("rev-parse", "HEAD")


def test_report_command_stamps_operational_identity_on_the_leaf_health_file(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The published ``.health.json`` is the operator-facing artifact.

    Asserting on ``report()``'s own parameter proves nothing about the command,
    so this drives ``main(["report", ...])`` and reads the file it leaves in the
    leaf repo. It fails if the CLI stops collecting or forwarding identity.
    """
    home = _install_layout(tmp_path, monkeypatch)
    _writable_adapter(home / "config.generations" / "gen1", tmp_path / "agent")
    (home / "config.generations" / "gen1" / "redaction_patterns.yaml").write_text(
        "version: test\npatterns: []\n", encoding="utf-8"
    )
    repo = tmp_path / "leaf-repo"
    repo.mkdir()
    (repo / "bootstrap_state.json").write_text(
        Path("bootstrap_state.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.setattr(leaf_cli, "_prepare_leaf_branch", lambda *_args: None)
    monkeypatch.setattr(
        leaf_cli, "_commit_and_push_report", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(leaf_cli, "_repo_lock", lambda *_args: nullcontext())

    exit_code = leaf_cli.main(
        [
            "report",
            "--repo",
            str(repo),
            "--node-id",
            "alice",
            "--agent-root",
            str(tmp_path / "agent"),
            "--adapter",
            str(home / "config-current" / "adapters" / "claude-code.yaml"),
        ]
    )

    assert exit_code == 0
    health = json.loads((repo / ".health.json").read_text(encoding="utf-8"))
    for field, value in OPS_IDENTITY_SAMPLE.items():
        assert health[field] == value
    assert health["build_id_source"] == "package_ref"


def test_ops_identity_degrades_when_a_source_is_present_but_unreadable(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """An unreadable source must warn like a missing one, not raise."""
    home = _install_layout(tmp_path, monkeypatch)
    (home / "state" / "install_id").write_bytes(b"\xff\xfe\x00bad")

    args = Namespace(state_dir=home / "state", adapter=None)

    assert leaf_cli._ops_identity(args) is None


def _writable_adapter(generation: Path, agent_root: Path) -> None:
    """Give the generation's adapter YAML a real shared root to scan."""
    (agent_root / "memory" / "shared").mkdir(parents=True)
    (agent_root / "memory" / "shared" / "note.md").write_text(
        "hello\n", encoding="utf-8"
    )
    (generation / "adapters" / "claude-code.yaml").write_text(
        "name: claude-code\n"
        "roots:\n"
        "  shared:\n"
        "    - alias: shared\n"
        '      base_dir: "${AGENT_ROOT}/memory/shared"\n'
        '      include: ["**/*.md"]\n'
        'team_target: "${AGENT_ROOT}/memory/team"\n'
        'file_pattern: "*.md"\n',
        encoding="utf-8",
    )
