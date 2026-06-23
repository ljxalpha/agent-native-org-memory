import builtins
import json
from argparse import Namespace
from contextlib import nullcontext
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import memory_net.leaf.cli as leaf_cli
import memory_net.leaf.daemon as daemon_module
from memory_net.leaf.daemon import run_foreground


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
    ) -> Path:
        calls.append(("materialize", f"{adapter_name}:{snapshot}"))
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
        ("materialize", "claude-code:snap-1"),
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
