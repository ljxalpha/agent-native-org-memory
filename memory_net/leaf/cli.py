from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from memory_net import __version__
from memory_net.leaf import daemon, hints
from memory_net.leaf.health import build_health, utc_now, write_health
from memory_net.leaf.identity import (
    OpsIdentityError,
    collect_ops_identity,
    read_host,
    split_ops_identity,
)
from memory_net.leaf.reporter import RedactionBlockedError, report
from memory_net.leaf.scanner import scanner_config_from_report_config
from memory_net.leaf.subscriber import SubscribeResult, subscribe
from memory_net.shared.adapters import FileSystemMdAdapter
from memory_net.shared.adapters.filesystem_md import (
    RootSpec,
    assert_allowed_adapter_path,
)
from memory_net.shared.config import (
    compute_subscribe_contract_revision,
    materialize_generation,
    subscribe_contract_revision,
)
from memory_net.shared.git_io import GitError, GitRepo
from memory_net.shared.observability import setup_logging
from memory_net.shared.redaction import Redactor
from memory_net.shared.schema import (
    SchemaError,
    validate_bootstrap_state,
    validate_health,
)
from memory_net.shared.util.hashing import short_hash
from memory_net.shared.util.paths import expand_path

BUILD_API = 1
HEALTH_COMMIT_INTERVAL_SECONDS = 6 * 60 * 60
#: Captured once per process so every health snapshot a daemon writes shares
#: the same ``started_at``, and so no code path reads the clock twice.
_PROCESS_STARTED_AT = utc_now()
LOGGER = logging.getLogger(__name__)
NON_LOCAL_FILESYSTEM_TYPES = frozenset(
    {"nfs", "nfs4", "smb", "smb2", "smb3", "smbfs", "cifs", "fuse.sshfs"}
)
UNKNOWN_FILESYSTEM_POLICY = "allow"


class SubscribeContractDriftError(RuntimeError):
    pass


def main(argv: list[str] | None = None) -> int:
    _enforce_supported_platform()
    parser = argparse.ArgumentParser(prog="memory-net-leaf")
    sub = parser.add_subparsers(dest="command", required=True)

    report_parser = sub.add_parser("report")
    _add_adapter_args(report_parser)
    report_parser.add_argument("--repo", required=True, type=Path)
    report_parser.add_argument("--node-id", required=True)
    report_parser.add_argument("--redaction", type=Path)
    report_parser.add_argument("--config-revision", default="local")
    report_parser.add_argument("--dry-run", action="store_true")
    report_parser.add_argument("--no-push", action="store_true")

    subscribe_parser = sub.add_parser("subscribe")
    _add_adapter_args(subscribe_parser)
    subscribe_parser.add_argument("--repo", required=True, type=Path)
    subscribe_parser.add_argument("--config-revision", default="local")
    subscribe_parser.add_argument("--state-dir", type=Path)
    subscribe_parser.add_argument("--dry-run", action="store_true")

    health_parser = sub.add_parser("health")
    health_parser.add_argument("--path", required=True, type=Path)
    health_parser.add_argument("--node-id", required=True)

    hints_parser = sub.add_parser("hints")
    _add_adapter_args(hints_parser)
    hints_parser.add_argument("--state-dir", type=Path)
    hints_parser.add_argument("--config-revision", default="local")
    hints_parser.add_argument("action", choices=["inject", "remove"])

    daemon_parser = sub.add_parser("daemon")
    daemon_parser.add_argument("--node-id", required=True)
    daemon_parser.add_argument("--state-dir", required=True, type=Path)
    daemon_parser.add_argument("--interval-seconds", type=int, default=300)
    daemon_parser.add_argument("--once", action="store_true")
    _add_daemon_task_args(daemon_parser)

    start_parser = sub.add_parser("start")
    start_parser.add_argument("--node-id", required=True)
    start_parser.add_argument("--state-dir", required=True, type=Path)
    start_parser.add_argument("--interval-seconds", type=int, default=300)
    start_parser.add_argument("--once", action="store_true")
    _add_daemon_task_args(start_parser)

    stop_parser = sub.add_parser("stop")
    stop_parser.add_argument("--state-dir", required=True, type=Path)

    trigger_parser = sub.add_parser("trigger")
    trigger_sub = trigger_parser.add_subparsers(dest="target", required=True)
    trigger_report = trigger_sub.add_parser("report")
    _add_adapter_args(trigger_report)
    trigger_report.add_argument("--repo", required=True, type=Path)
    trigger_report.add_argument("--node-id", required=True)
    trigger_report.add_argument("--redaction", type=Path)
    trigger_report.add_argument("--config-revision", default="local")
    trigger_report.add_argument("--dry-run", action="store_true")
    trigger_report.add_argument("--no-push", action="store_true")
    trigger_subscribe = trigger_sub.add_parser("subscribe")
    _add_adapter_args(trigger_subscribe)
    trigger_subscribe.add_argument("--repo", required=True, type=Path)
    trigger_subscribe.add_argument("--config-revision", default="local")
    trigger_subscribe.add_argument("--state-dir", type=Path)
    trigger_subscribe.add_argument("--dry-run", action="store_true")
    trigger_health = trigger_sub.add_parser("health")
    trigger_health.add_argument("--path", required=True, type=Path)
    trigger_health.add_argument("--node-id", required=True)

    status_parser = sub.add_parser("status")
    status_parser.add_argument("--health", required=True, type=Path)
    status_parser.add_argument(
        "--lenient",
        action="store_true",
        help=(
            "accept a health file without operational identity fields; "
            "for installations that predate them"
        ),
    )

    args = parser.parse_args(argv)
    _setup_runtime(args)
    _assert_local_memory_net_home(_state_home(args))
    _validate_bootstrap_for_args(args)
    is_trigger = args.command == "trigger"
    if args.command == "report" or (is_trigger and args.target == "report"):
        lock = (
            nullcontext() if args.dry_run else _repo_lock(args.repo, _state_home(args))
        )
        with lock:
            if not args.dry_run:
                _prepare_leaf_branch(args.repo, args.node_id)
            report_config = _report_config(args)
            adapter = _report_adapter(args)
            redactor = _redactor(args)
            committed = False
            try:
                result = report(
                    repo_path=args.repo,
                    node_id=args.node_id,
                    adapter=adapter,
                    redactor=redactor,
                    config_revision=args.config_revision,
                    scanner_config=scanner_config_from_report_config(report_config),
                    state_dir=_state_home(args) / "state",
                    dry_run=args.dry_run,
                )
            except RedactionBlockedError:
                if not args.dry_run:
                    _commit_health_only(args)
                raise
            if not args.dry_run:
                committed = _commit_and_push_report(
                    args.repo,
                    args.node_id,
                    no_push=args.no_push,
                )
        print(
            json.dumps(
                result.__dict__
                | {
                    "manifest_path": str(result.manifest_path),
                    "committed": committed,
                },
                sort_keys=True,
            )
        )
        return 0

    if args.command == "subscribe" or (is_trigger and args.target == "subscribe"):
        with _repo_lock(args.repo, _state_home(args)):
            snapshot_sha = _origin_master_snapshot(args.repo)
            _ensure_subscribe_contract_current(
                args.repo,
                _state_home(args),
                adapter_name=_adapter_config_name(args),
                snapshot_sha=snapshot_sha,
            )
            merge_strategy, include_index, max_products = _subscribe_options(
                args.repo, _state_home(args)
            )
            subscribe_result = _subscribe_from_master_snapshot(
                args,
                merge_strategy=merge_strategy,
                include_index=include_index,
                max_products=max_products,
                snapshot_sha=snapshot_sha,
            )
        print(
            json.dumps(
                {
                    "products_written": subscribe_result.products_written,
                    "team_target": str(subscribe_result.team_target),
                }
            )
        )
        return 0

    if args.command == "health" or (is_trigger and args.target == "health"):
        identity_kwargs, identity_extra = split_ops_identity(_ops_identity(args))
        write_health(
            args.path,
            build_health(
                node_id=args.node_id,
                extra=identity_extra or None,
                **identity_kwargs,
            ),
        )
        print(str(args.path))
        return 0

    if args.command == "hints":
        adapter = _adapter(args)
        state_path = _hint_state_path(args)
        config_revision = _active_config_revision(args)
        if args.action == "inject":
            changed = hints.inject(
                adapter,
                state_path=state_path,
                config_revision=config_revision,
            )
        else:
            changed = hints.remove(
                adapter,
                state_path=state_path,
                config_revision=config_revision,
            )
        print(json.dumps({"changed": changed}))
        return 0

    if args.command in {"daemon", "start"}:
        report_task, subscribe_task, config_sync_task, health_task = _daemon_tasks(args)
        daemon.run_foreground(
            node_id=args.node_id,
            state_dir=args.state_dir,
            report_task=report_task,
            subscribe_task=subscribe_task,
            config_sync_task=config_sync_task,
            health_task=health_task,
            interval_seconds=args.interval_seconds,
            once=args.once,
        )
        return 0

    if args.command == "stop":
        marker = args.state_dir / "stop-requested"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("stop requested\n", encoding="utf-8")
        print(str(marker))
        return 0

    if args.command == "status":
        return _print_status(args.health, lenient=args.lenient)

    parser.error("unknown command")
    return 2


def _add_adapter_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--adapter", required=True, type=Path)
    parser.add_argument("--agent-root", required=True, type=Path)


def _add_daemon_task_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--adapter", type=Path)
    parser.add_argument("--agent-root", type=Path)
    parser.add_argument("--redaction", type=Path)
    parser.add_argument("--config-revision", default="local")
    parser.add_argument("--no-push", action="store_true")


def _adapter(args: argparse.Namespace) -> FileSystemMdAdapter:
    return FileSystemMdAdapter.from_yaml(args.adapter, agent_root=args.agent_root)


def _adapter_config_name(args: argparse.Namespace) -> str:
    return str(args.adapter.stem)


def _report_adapter(
    args: argparse.Namespace,
    *,
    daemon_mode: bool = False,
) -> FileSystemMdAdapter:
    adapter = _daemon_adapter(args) if daemon_mode else _adapter(args)
    config = _report_config(args)
    include = tuple(str(item) for item in config.get("include") or [])
    exclude = tuple(str(item) for item in config.get("exclude") or [])
    if include or exclude:
        roots = [
            RootSpec(
                scope=root.scope,
                alias=root.alias,
                base_dir=root.base_dir,
                include=include or root.include,
                exclude=(*root.exclude, *exclude),
            )
            for root in adapter.roots
        ]
        adapter.roots = roots
    limits = config.get("limits")
    if isinstance(limits, dict):
        if limits.get("max_files") is not None:
            adapter.max_records = int(limits["max_files"])
        if limits.get("max_file_bytes") is not None:
            adapter.max_file_bytes = int(limits["max_file_bytes"])
    return adapter


def _report_config(args: argparse.Namespace) -> dict[str, Any]:
    return _load_yaml_mapping(
        _state_home(args) / "config-current" / "report.yaml",
        args.repo / "config" / "report.yaml",
        Path("config/report.yaml"),
    )


def _redactor(args: argparse.Namespace) -> Redactor:
    redaction_path = args.redaction or _configured_redaction_path(_memory_net_home())
    if not redaction_path.exists():
        raise SystemExit(
            f"redaction config is required and was not found: {redaction_path}"
        )
    return Redactor.from_yaml(redaction_path)


def _daemon_tasks(
    args: argparse.Namespace,
) -> tuple[
    Callable[[], object] | None,
    Callable[[], object] | None,
    Callable[[], object] | None,
    Callable[[], object] | None,
]:
    missing = [
        name
        for name, value in [
            ("--repo", args.repo),
            ("--adapter", args.adapter),
            ("--agent-root", args.agent_root),
        ]
        if value is None
    ]
    if missing:
        raise SystemExit(f"daemon requires: {', '.join(missing)}")

    def report_task() -> None:
        with _repo_lock(args.repo, _state_home(args)):
            _prepare_leaf_branch(args.repo, args.node_id)
            redactor = Redactor.from_yaml(_daemon_redaction_path(args))
            report_config = _report_config(args)
            try:
                report(
                    repo_path=args.repo,
                    node_id=args.node_id,
                    adapter=_report_adapter(args, daemon_mode=True),
                    redactor=redactor,
                    config_revision=_active_config_revision(args),
                    scanner_config=scanner_config_from_report_config(report_config),
                    state_dir=args.state_dir,
                )
            except RedactionBlockedError:
                if _commit_health_only(args):
                    _write_state_time(args.state_dir / "last_health_commit_at")
                raise
            committed = _commit_and_push_report(
                args.repo, args.node_id, no_push=args.no_push
            )
            if committed:
                _write_state_time(args.state_dir / "last_report_commit_at")

    def subscribe_task() -> None:
        with _repo_lock(args.repo, _state_home(args)):
            try:
                snapshot_sha = _origin_master_snapshot(args.repo)
                _ensure_subscribe_contract_current(
                    args.repo,
                    _state_home(args),
                    adapter_name=_adapter_config_name(args),
                    snapshot_sha=snapshot_sha,
                )
                merge_strategy, include_index, max_products = _subscribe_options(
                    args.repo, _state_home(args)
                )
                _subscribe_from_master_snapshot(
                    args,
                    merge_strategy=merge_strategy,
                    include_index=include_index,
                    max_products=max_products,
                    snapshot_sha=snapshot_sha,
                )
            finally:
                _prepare_leaf_branch(args.repo, args.node_id)

    def config_sync_task() -> None:
        with _repo_lock(args.repo, _state_home(args)):
            old_adapter = _daemon_adapter_or_none(args)
            snapshot_sha = _origin_master_snapshot(args.repo)
            if snapshot_sha is None:
                _validate_bootstrap_state_from_repo(args.repo)
                generation = materialize_generation(
                    args.repo,
                    args.state_dir.parent / "config.generations",
                    adapter_name=_adapter_config_name(args),
                )
            else:
                _validate_bootstrap_state_from_git_snapshot(args.repo, snapshot_sha)
                generation = _materialize_generation_from_git_snapshot(
                    args.repo,
                    snapshot_sha,
                    args.state_dir.parent / "config.generations",
                    adapter_name=_adapter_config_name(args),
                    source_master_sha=snapshot_sha,
                )
            _replace_config_current(
                args.state_dir.parent / "config-current", generation
            )
            _sync_hints_after_config_sync(args, old_adapter)
            _prepare_leaf_branch(args.repo, args.node_id)

    def health_task() -> None:
        health_path = args.state_dir / "last_health.json"
        with _health_lock(args.state_dir):
            recent_errors = _read_recent_errors(health_path)
            identity_kwargs, identity_extra = split_ops_identity(_ops_identity(args))
            health = build_health(
                node_id=args.node_id,
                status="degraded" if recent_errors else "healthy",
                config_revision=_active_config_revision(args),
                recent_errors=recent_errors,
                published=False,
                extra=identity_extra or None,
                **identity_kwargs,
            )
            write_health(health_path, health)
        if _health_commit_due(args.state_dir, args.repo):
            with _repo_lock(args.repo, _state_home(args)):
                published = dict(health)
                published["published_at"] = _utc_now()
                _prepare_leaf_branch(args.repo, args.node_id)
                write_health(args.repo / ".health.json", published)
                committed = _commit_and_push_report(
                    args.repo,
                    args.node_id,
                    no_push=args.no_push,
                    paths=[".health.json"],
                    message=f"health leaf {args.node_id}",
                )
                if committed:
                    _write_state_time(args.state_dir / "last_health_commit_at")

    return report_task, subscribe_task, config_sync_task, health_task


def _commit_health_only(args: argparse.Namespace) -> bool:
    return _commit_and_push_report(
        args.repo,
        args.node_id,
        no_push=args.no_push,
        paths=[".health.json"],
        message=f"health leaf {args.node_id}",
    )


def _commit_and_push_report(
    repo_path: Path,
    node_id: str,
    *,
    no_push: bool,
    paths: list[str] | None = None,
    message: str | None = None,
) -> bool:
    if not (repo_path / ".git").exists():
        return False
    repo = GitRepo(repo_path, no_push=no_push)
    repo.fetch()
    remote_exists = _remote_leaf_branch_exists(repo, node_id)
    if not remote_exists:
        _write_initial_owner_metadata(repo_path, node_id)
    owner_needs_initial_commit = not remote_exists
    _ensure_remote_owner_matches(repo, node_id)
    commit_paths = paths or [
        path
        for path in ["manifest.json", ".health.json", "memories"]
        if (repo_path / path).exists()
    ]
    if owner_needs_initial_commit and ".owner.json" not in commit_paths:
        commit_paths = [".owner.json", *commit_paths]
    committed = repo.add_commit(
        commit_paths,
        message or f"report leaf {node_id}",
    )
    if committed:
        try:
            repo.push(f"HEAD:leaf/{node_id}")
        except GitError as exc:
            _write_push_failure_evidence(repo, node_id, str(exc))
            raise
    return committed


def _prepare_leaf_branch(repo_path: Path, node_id: str) -> None:
    if not (repo_path / ".git").exists():
        return
    branch = f"leaf/{node_id}"
    repo = GitRepo(repo_path)
    repo.fetch()
    if _remote_leaf_branch_exists(repo, node_id):
        _assert_clean_worktree(repo)
        _assert_not_ahead(repo, branch)
        _ensure_remote_owner_matches(repo, node_id)
        repo.run("switch", "-C", branch, f"origin/{branch}")
        return
    current = repo.run("branch", "--show-current").stdout.strip()
    if current == branch:
        _write_initial_owner_metadata(repo_path, node_id)
        return
    if repo.run("status", "--porcelain", check=False).stdout.strip():
        raise RuntimeError("cannot initialize leaf branch from a dirty worktree")
    repo.run("switch", "--orphan", branch)
    repo.run("rm", "-rf", ".", check=False)
    _write_initial_owner_metadata(repo_path, node_id)


def _prepare_master_branch(repo_path: Path) -> None:
    if not (repo_path / ".git").exists():
        return
    repo = GitRepo(repo_path)
    repo.fetch()
    if (
        repo.run(
            "show-ref",
            "--verify",
            "--quiet",
            "refs/remotes/origin/master",
            check=False,
        ).returncode
        == 0
    ):
        _assert_clean_worktree(repo)
        _assert_not_ahead(repo, "master")
        repo.run("switch", "-C", "master", "origin/master")


def _assert_clean_worktree(repo: GitRepo) -> None:
    if repo.run("status", "--porcelain", check=False).stdout.strip():
        raise RuntimeError("cannot switch branches from a dirty worktree")


def _assert_not_ahead(repo: GitRepo, branch: str) -> None:
    local_ref = f"refs/heads/{branch}"
    remote_ref = f"refs/remotes/origin/{branch}"
    has_local = (
        repo.run("show-ref", "--verify", "--quiet", local_ref, check=False).returncode
        == 0
    )
    has_remote = (
        repo.run("show-ref", "--verify", "--quiet", remote_ref, check=False).returncode
        == 0
    )
    if not has_local or not has_remote:
        return
    result = repo.run(
        "rev-list",
        "--left-right",
        "--count",
        f"origin/{branch}...{branch}",
        check=False,
    )
    parts = result.stdout.split()
    if result.returncode == 0 and len(parts) == 2 and int(parts[1]) > 0:
        raise RuntimeError(f"local {branch} has unpushed commits; refusing reset")


def _ensure_remote_owner_matches(repo: GitRepo, node_id: str) -> None:
    local_path = repo.path / ".owner.json"
    remote = repo.run(
        "show",
        f"origin/leaf/{node_id}:.owner.json",
        check=False,
    )
    if remote.returncode != 0:
        if _remote_leaf_branch_exists(repo, node_id):
            raise RuntimeError(f"origin/leaf/{node_id} is missing .owner.json")
        return
    try:
        local_owner = (
            json.loads(local_path.read_text(encoding="utf-8"))
            if local_path.exists()
            else _current_owner_identity(node_id)
        )
        remote_owner = json.loads(remote.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("invalid leaf owner metadata") from exc
    for key in ("node_id", "install_id"):
        if local_owner.get(key) != remote_owner.get(key):
            raise RuntimeError(f"origin/leaf/{node_id} owner {key} changed")


def _remote_leaf_branch_exists(repo: GitRepo, node_id: str) -> bool:
    return (
        repo.run(
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/remotes/origin/leaf/{node_id}",
            check=False,
        ).returncode
        == 0
    )


def _write_initial_owner_metadata(repo_path: Path, node_id: str) -> None:
    owner_path = repo_path / ".owner.json"
    if owner_path.exists():
        return
    state_home = _memory_net_home()
    local_config = _load_yaml_mapping(state_home / "local.yaml")
    owner = _current_owner_identity(node_id) | {
        "schema_version": "memory-net/owner/v1",
        "agent": str(local_config.get("agent") or "unknown"),
        "agent_root": str(local_config.get("agent_root") or ""),
        "created_at": _utc_now(),
    }
    owner_path.write_text(
        json.dumps(owner, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _current_owner_identity(node_id: str) -> dict[str, str]:
    state_home = _memory_net_home()
    return {
        "node_id": node_id,
        "install_id": _read_or_create_install_id(state_home),
        "owner_fingerprint": _owner_fingerprint(),
        "host": read_host(),
    }


def _read_or_create_install_id(state_home: Path) -> str:
    install_id_path = state_home / "state" / "install_id"
    if install_id_path.exists():
        return install_id_path.read_text(encoding="utf-8").strip()
    install_id_path.parent.mkdir(parents=True, exist_ok=True)
    install_id = "inst-" + uuid.uuid4().hex
    install_id_path.write_text(install_id + "\n", encoding="utf-8")
    return install_id


def _owner_fingerprint() -> str:
    import hashlib

    raw = f"{read_host()}:{platform.system()}:{platform.machine()}".encode()
    return "ofp-" + hashlib.sha256(raw).hexdigest()[:12]


def _write_push_failure_evidence(repo: GitRepo, node_id: str, error: str) -> None:
    evidence_id = f"{_utc_now_safe()}-{uuid.uuid4().hex[:8]}"
    root = _memory_net_home() / "state" / "rejected_commits" / evidence_id
    root.mkdir(parents=True)
    (root / "error.txt").write_text(error + "\n", encoding="utf-8")
    status = repo.run("status", "--short", check=False)
    (root / "status.txt").write_text(status.stdout + status.stderr, encoding="utf-8")
    base_ref = f"origin/leaf/{node_id}"
    patch = repo.run("format-patch", f"{base_ref}..HEAD", "--stdout", check=False)
    patch_text = patch.stdout + patch.stderr
    if not patch_text.strip():
        patch = repo.run("format-patch", "-1", "--stdout", check=False)
        patch_text = patch.stdout + patch.stderr
    (root / "commits.patch").write_text(patch_text, encoding="utf-8")
    (root / "commit.patch").write_text(patch_text, encoding="utf-8")
    unstaged = repo.run("diff", "--binary", check=False)
    staged = repo.run("diff", "--cached", "--binary", check=False)
    (root / "worktree.patch").write_text(
        unstaged.stdout + unstaged.stderr + staged.stdout + staged.stderr,
        encoding="utf-8",
    )
    _write_untracked_archive(repo, root / "worktree.tar.gz")
    memories = repo.path / "memories"
    if memories.exists():
        shutil.copytree(memories, root / "redacted", dirs_exist_ok=True)
    backup_branch = f"backup/rejected-{node_id}-{evidence_id}"
    repo.run("branch", backup_branch, "HEAD")
    (root / "metadata.json").write_text(
        json.dumps(
            {
                "backup_branch": backup_branch,
                "node_id": node_id,
                "repo": str(repo.path),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    write_health(
        _memory_net_home() / "state" / "last_health.json",
        build_health(
            node_id=node_id,
            status="degraded",
            recent_errors=[f"push rejected: {error}"],
            extra={"push_failure_evidence": str(root)},
            published=False,
        ),
    )
    repo.fetch()
    if repo.run("rev-parse", "--verify", base_ref, check=False).returncode == 0:
        repo.run("reset", "--hard", base_ref)
        repo.run("clean", "-fd")


def _write_untracked_archive(repo: GitRepo, path: Path) -> None:
    result = repo.run("ls-files", "--others", "--exclude-standard", "-z", check=False)
    paths = [item for item in result.stdout.split("\0") if item]
    with tarfile.open(path, "w:gz") as archive:
        for rel in paths:
            candidate = repo.path / rel
            if candidate.exists():
                archive.add(candidate, arcname=rel)


def _replace_config_current(link_path: Path, generation: Path) -> None:
    link_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = link_path.with_name(f".{link_path.name}.tmp")
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    os.symlink(generation, tmp)
    os.replace(tmp, link_path)


def _subscribe_from_master_snapshot(
    args: argparse.Namespace,
    *,
    merge_strategy: str,
    include_index: bool,
    max_products: int | None,
    snapshot_sha: str | None = None,
) -> SubscribeResult:
    adapter = (
        _daemon_adapter(args)
        if getattr(args, "command", None) in {"daemon", "start"}
        else _adapter(args)
    )
    adapter = _apply_subscribe_config(args, adapter)
    config_revision = (
        _active_config_revision(args)
        if getattr(args, "command", None) in {"daemon", "start"}
        else args.config_revision
    )
    if (args.repo / ".git").exists():
        repo = GitRepo(args.repo)
        if snapshot_sha is None:
            snapshot_sha = _origin_master_snapshot(args.repo)
        if snapshot_sha is None:
            raise SubscribeContractDriftError("origin/master is required for subscribe")
        manifest = repo.show_json(f"{snapshot_sha}:manifest.json")
        return subscribe(
            repo_path=args.repo,
            adapter=adapter,
            config_revision=config_revision,
            state_dir=getattr(args, "state_dir", None),
            merge_strategy=merge_strategy,
            include_index=include_index,
            dry_run=bool(getattr(args, "dry_run", False)),
            max_products=max_products,
            manifest=manifest,
            product_reader=lambda rel: repo.show_text(
                f"{snapshot_sha}:{rel.as_posix()}"
            ),
        )
    return subscribe(
        repo_path=args.repo,
        adapter=adapter,
        config_revision=config_revision,
        state_dir=getattr(args, "state_dir", None),
        merge_strategy=merge_strategy,
        include_index=include_index,
        dry_run=bool(getattr(args, "dry_run", False)),
        max_products=max_products,
    )


def _setup_runtime(args: argparse.Namespace) -> None:
    _enforce_supported_platform(_platform_config_path(args))
    setup_logging(config_path=_observability_config_path(args))


def _enforce_supported_platform(config_path: Path | None = None) -> None:
    current = platform.system()
    supported = _supported_platforms(config_path)
    if current not in supported:
        print(
            f"[ABORT] unsupported OS: {current}; supported: {', '.join(supported)}",
            file=sys.stderr,
        )
        raise SystemExit(2)


def _validate_bootstrap_for_args(args: argparse.Namespace) -> None:
    repo = getattr(args, "repo", None)
    if repo is not None:
        _validate_bootstrap_state_from_repo(repo)


def _validate_bootstrap_state_from_repo(repo_path: Path) -> None:
    _validate_bootstrap_state_text(_read_bootstrap_state(repo_path))


def _validate_bootstrap_state_from_git_snapshot(
    repo_path: Path,
    snapshot_sha: str,
) -> None:
    repo = GitRepo(repo_path)
    _validate_bootstrap_state_text(
        repo.show_text(f"{snapshot_sha}:bootstrap_state.json")
    )


def _validate_bootstrap_state_text(text: str) -> None:
    try:
        value = json.loads(text)
        validate_bootstrap_state(
            value,
            build_version=__version__,
            build_api=BUILD_API,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise SystemExit(f"bootstrap_state.json is not compatible: {exc}") from exc


def _read_bootstrap_state(repo_path: Path) -> str:
    if (repo_path / ".git").exists():
        repo = GitRepo(repo_path)
        repo.fetch()
        result = repo.run(
            "show",
            "origin/master:bootstrap_state.json",
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
    path = repo_path / "bootstrap_state.json"
    if path.exists():
        return path.read_text(encoding="utf-8")
    raise SystemExit("bootstrap_state.json is required before running leaf commands")


def _materialize_generation_from_git_snapshot(
    repo_path: Path,
    snapshot_sha: str,
    generations_root: Path,
    *,
    adapter_name: str,
    source_master_sha: str | None = None,
) -> Path:
    """Materialize the config generation frozen in one master snapshot.

    :param source_master_sha: recorded as the generation's provenance marker.
        Config sync replaces ``config-current``, so without it every sync would
        drop the marker the installer wrote and the operational identity of the
        leaf would lose ``config_source_master_sha`` from that point on.
    """
    generations_root.mkdir(parents=True, exist_ok=True)
    source_root = (
        generations_root / f".snapshot-{short_hash(snapshot_sha, 8)}-{uuid.uuid4().hex}"
    )
    repo = GitRepo(repo_path)
    try:
        source_root.mkdir(parents=True)
        result = repo.run(
            "ls-tree",
            "-r",
            "--name-only",
            snapshot_sha,
            "bootstrap_state.json",
            "config",
            "prompts",
        )
        for rel in sorted(line for line in result.stdout.splitlines() if line.strip()):
            out = source_root / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(repo.show_bytes(f"{snapshot_sha}:{rel}"))
        return materialize_generation(
            source_root,
            generations_root,
            adapter_name=adapter_name,
            source_master_sha=source_master_sha,
        )
    finally:
        shutil.rmtree(source_root, ignore_errors=True)


def _print_status(health_path: Path, *, lenient: bool) -> int:
    """Read, validate and print a health file.

    ``status`` is the gate that makes "the install finished" mean "this leaf can
    be located by an operator", so it validates instead of printing blindly.
    Returns a process exit code.
    """
    try:
        raw = health_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"cannot read health file {health_path}: {exc}", file=sys.stderr)
        return 1
    try:
        health = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"health file {health_path} is not valid JSON: {exc}", file=sys.stderr)
        return 1
    try:
        validate_health(health, require_ops_identity=not lenient)
    except SchemaError as exc:
        print(f"health file {health_path} failed validation: {exc}", file=sys.stderr)
        return 1
    print(raw)
    return 0


def _ops_identity(args: argparse.Namespace) -> dict[str, str] | None:
    """Collect operational identity from the installation layout, best effort.

    A development run without a full ``install_leaf.sh`` layout must not break
    reporting, so an incomplete layout logs a warning and yields ``None``; the
    strict gate lives in ``status`` instead.
    """
    state_home = _state_home(args)
    try:
        return collect_ops_identity(
            state_dir=state_home / "state",
            adapter_path=_ops_identity_adapter_path(args, state_home),
            local_yaml_path=state_home / "local.yaml",
            generation_dir=state_home / "config-current",
            started_at=_PROCESS_STARTED_AT,
        )
    except OpsIdentityError as exc:
        LOGGER.warning("operational identity is unavailable: %s", exc)
        return None


def _ops_identity_adapter_path(args: argparse.Namespace, state_home: Path) -> Path:
    """Locate the adapter YAML naming this leaf's adapter.

    Prefers an explicit ``--adapter``; otherwise follows the installed layout
    ``config-current/adapters/<local.yaml agent>.yaml``.
    """
    adapter = getattr(args, "adapter", None)
    if isinstance(adapter, Path):
        active = state_home / "config-current" / "adapters" / adapter.name
        return active if active.exists() else adapter
    agent = _load_yaml_mapping(state_home / "local.yaml").get("agent")
    return state_home / "config-current" / "adapters" / f"{agent}.yaml"


def _memory_net_home() -> Path:
    return Path(os.environ.get("MEMORY_NET_HOME", str(Path.home() / ".memory-net")))


def _state_home(args: argparse.Namespace) -> Path:
    state_dir = getattr(args, "state_dir", None)
    if isinstance(state_dir, Path):
        return state_dir.parent
    return _memory_net_home()


def _config_current(args: argparse.Namespace) -> Path:
    return _state_home(args) / "config-current"


def _daemon_adapter(args: argparse.Namespace) -> FileSystemMdAdapter:
    adapter_path = _config_current(args) / "adapters" / args.adapter.name
    if not adapter_path.exists():
        raise RuntimeError(f"active adapter config is missing: {adapter_path}")
    return FileSystemMdAdapter.from_yaml(adapter_path, agent_root=args.agent_root)


def _daemon_adapter_or_none(args: argparse.Namespace) -> FileSystemMdAdapter | None:
    adapter_path = _config_current(args) / "adapters" / args.adapter.name
    if not adapter_path.exists():
        return None
    return FileSystemMdAdapter.from_yaml(adapter_path, agent_root=args.agent_root)


def _sync_hints_after_config_sync(
    args: argparse.Namespace,
    old_adapter: FileSystemMdAdapter | None,
) -> None:
    hints.sync_config_change(
        old_adapter,
        _daemon_adapter(args),
        state_path=args.state_dir / "hints_state.json",
        config_revision=_active_config_revision(args),
    )


def _hint_state_path(args: argparse.Namespace) -> Path:
    state_dir = getattr(args, "state_dir", None)
    if isinstance(state_dir, Path):
        return state_dir / "hints_state.json"
    return _state_home(args) / "state" / "hints_state.json"


def _daemon_redaction_path(args: argparse.Namespace) -> Path:
    path = _configured_redaction_path(_state_home(args))
    if not path.exists():
        raise RuntimeError(f"active redaction config is missing: {path}")
    return path


def _active_config_revision(args: argparse.Namespace) -> str:
    path = _config_current(args) / "config_revision"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return str(args.config_revision)


def _ensure_subscribe_contract_current(
    repo_path: Path,
    state_home: Path,
    *,
    adapter_name: str | None = None,
    snapshot_sha: str | None = None,
) -> None:
    current_link = state_home / "config-current"
    current_revision = current_link / "subscribe_contract_revision"
    if not current_revision.exists():
        raise SubscribeContractDriftError(
            "subscribe contract is not active; run config_sync before subscribe"
        )
    if (repo_path / ".git").exists():
        repo = GitRepo(repo_path)
        ref = snapshot_sha or _origin_master_snapshot(repo_path)
        if ref is None:
            raise SubscribeContractDriftError("origin/master is required for subscribe")
        remote_revision = _subscribe_contract_revision_from_git(
            repo,
            ref,
            adapter_name=adapter_name,
        )
    else:
        remote_revision = subscribe_contract_revision(
            repo_path,
            adapter_name=adapter_name,
        )
    local_revision = current_revision.read_text(encoding="utf-8").strip()
    if remote_revision != local_revision:
        raise SubscribeContractDriftError(
            "subscribe contract drift; refuse to write team/"
        )


def _origin_master_snapshot(repo_path: Path) -> str | None:
    if not (repo_path / ".git").exists():
        return None
    repo = GitRepo(repo_path)
    repo.fetch()
    return _sha_or_abort(repo, "origin/master", label="origin/master")


def _subscribe_contract_revision_from_git(
    repo: GitRepo,
    ref: str,
    *,
    adapter_name: str | None = None,
) -> str:
    adapter_path = (
        "config/adapters"
        if adapter_name is None
        else f"config/adapters/{adapter_name}.yaml"
    )
    result = repo.run(
        "ls-tree",
        "-r",
        "--name-only",
        ref,
        "bootstrap_state.json",
        "config/subscribe.yaml",
        adapter_path,
        check=False,
    )
    blobs: dict[str, bytes] = {}
    for rel in sorted(line for line in result.stdout.splitlines() if line.strip()):
        blobs[rel] = repo.show_bytes(f"{ref}:{rel}")
    adapter_yamls = {
        Path(rel).stem: data
        for rel, data in blobs.items()
        if rel.startswith("config/adapters/") and rel.endswith(".yaml")
    }
    return compute_subscribe_contract_revision(
        bootstrap_state=blobs.get("bootstrap_state.json"),
        subscribe_yaml=blobs.get("config/subscribe.yaml"),
        adapter_yamls=adapter_yamls,
        adapter_name=adapter_name,
    )


def _subscribe_options(
    repo_path: Path,
    state_home: Path,
) -> tuple[str, bool, int | None]:
    config = _load_yaml_mapping(
        state_home / "config-current" / "subscribe.yaml",
        repo_path / "config" / "subscribe.yaml",
    )
    limits = config.get("limits")
    max_products = None
    if isinstance(limits, dict) and limits.get("max_products") is not None:
        max_products = int(limits["max_products"])
    return (
        str(config.get("merge_strategy") or "replace"),
        bool(config.get("include_index", True)),
        max_products,
    )


def _apply_subscribe_config(
    args: argparse.Namespace,
    adapter: FileSystemMdAdapter,
) -> FileSystemMdAdapter:
    config = _load_yaml_mapping(
        _state_home(args) / "config-current" / "subscribe.yaml",
        args.repo / "config" / "subscribe.yaml",
    )
    raw_target = config.get("target_dir")
    if isinstance(raw_target, str) and raw_target:
        target = expand_path(raw_target, agent_root=args.agent_root)
        if not target.is_absolute():
            target = args.agent_root / target
        assert_allowed_adapter_path(
            target,
            agent_root=args.agent_root,
            label="target_dir",
        )
        adapter.team_target = target
    return adapter


def _configured_redaction_path(state_home: Path) -> Path:
    config_current = state_home / "config-current"
    report_config = _load_yaml_mapping(config_current / "report.yaml")
    raw = report_config.get("redaction_patterns_path")
    if isinstance(raw, str) and raw:
        configured = Path(raw)
        if configured.is_absolute():
            return configured
        return config_current / configured.name
    return config_current / "redaction_patterns.yaml"


def _platform_config_path(args: argparse.Namespace) -> Path | None:
    return _first_existing_runtime_config(args, "platform.yaml")


def _observability_config_path(args: argparse.Namespace) -> Path | None:
    return _first_existing_runtime_config(args, "observability.yaml")


def _first_existing_runtime_config(
    args: argparse.Namespace,
    filename: str,
) -> Path | None:
    candidates = [_state_home(args) / "config-current" / filename]
    repo = getattr(args, "repo", None)
    if isinstance(repo, Path):
        candidates.append(repo / "config" / filename)
    candidates.append(Path("config") / filename)
    for path in candidates:
        if path.exists():
            return path
    return None


def _supported_platforms(config_path: Path | None) -> tuple[str, ...]:
    config = _load_yaml_mapping(config_path) if config_path is not None else {}
    runtime = config.get("runtime")
    raw = runtime.get("supported_os") if isinstance(runtime, dict) else None
    if raw is None:
        raw = config.get("supported_os")
    if isinstance(raw, list) and raw:
        return tuple(str(item) for item in raw)
    return ("Linux", "Darwin")


def _load_yaml_mapping(*paths: Path) -> dict[str, Any]:
    return _load_optional_yaml_mapping(*paths)


def _load_required_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise RuntimeError(f"config file is required: {path}")
    return _load_yaml_text(path.read_text(encoding="utf-8"), str(path))


def _load_optional_yaml_mapping(*paths: Path) -> dict[str, Any]:
    for path in paths:
        if not path.exists():
            continue
        return _load_yaml_text(path.read_text(encoding="utf-8"), str(path))
    LOGGER.info(
        "using default config; no candidate exists: %s",
        ", ".join(map(str, paths)),
    )
    return {}


def _load_yaml_text(text: str, label: str) -> dict[str, Any]:
    value = yaml.safe_load(text) or {}
    if not isinstance(value, dict):
        raise RuntimeError(f"config must be a mapping: {label}")
    return value


def _write_state_time(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_utc_now() + "\n", encoding="utf-8")


def _health_commit_due(state_dir: Path, repo_path: Path | None = None) -> bool:
    if repo_path is not None and (repo_path / ".git").exists():
        latest_commit = _git_last_commit_at(repo_path)
        if latest_commit is not None:
            age = datetime.now(UTC) - latest_commit
            return age.total_seconds() >= HEALTH_COMMIT_INTERVAL_SECONDS
    times = [
        item
        for item in [
            _read_state_time(state_dir / "last_report_commit_at"),
            _read_state_time(state_dir / "last_health_commit_at"),
        ]
        if item is not None
    ]
    if not times:
        return False
    latest = max(times)
    age = datetime.now(UTC) - latest
    return age.total_seconds() >= HEALTH_COMMIT_INTERVAL_SECONDS


def _git_last_commit_at(repo_path: Path) -> datetime | None:
    result = GitRepo(repo_path).run("log", "-1", "--format=%ct", check=False)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return datetime.fromtimestamp(int(result.stdout.strip()), UTC)
    except ValueError:
        return None


def _assert_local_memory_net_home(state_home: Path) -> None:
    fs_type = _filesystem_type(state_home)
    if fs_type is None:
        if UNKNOWN_FILESYSTEM_POLICY == "deny":
            raise SystemExit(
                f"MEMORY_NET_HOME filesystem type is unknown: {state_home}"
            )
        return
    if fs_type is not None and fs_type in NON_LOCAL_FILESYSTEM_TYPES:
        raise SystemExit(
            f"MEMORY_NET_HOME must be on a local filesystem; got {fs_type}: {state_home}"
        )


def _filesystem_type(path: Path) -> str | None:
    target = _nearest_existing_ancestor(path)
    if not target.exists():
        return None
    if sys.platform.startswith("linux"):
        return _stat_filesystem_type(["stat", "-f", "-c", "%T", str(target)])
    if sys.platform == "darwin":
        return _stat_filesystem_type(["stat", "-f", "%T", str(target)])
    return None


def _nearest_existing_ancestor(path: Path) -> Path:
    target = path
    while not target.exists() and target != target.parent:
        target = target.parent
    return target


def _stat_filesystem_type(args: list[str]) -> str | None:
    result = subprocess.run(
        args,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip().lower()
    return None


def _read_state_time(path: Path) -> datetime | None:
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8").strip()
    try:
        # fromisoformat has accepted a trailing 'Z' since 3.11 (our floor).
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # A naive value would blow up on the `datetime.now(UTC) - latest`
        # subtraction in _health_commit_due. State files are always written by
        # _write_state_time, which stamps UTC, so anything naive is corrupt
        # input and is treated the same as an unparsable one: no known time.
        return None
    return parsed


def _read_recent_errors(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        health = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return [str(item) for item in health.get("recent_errors") or []][-10:]


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utc_now_safe() -> str:
    return _utc_now().replace(":", "").replace("-", "")


def _sha_or_abort(repo: GitRepo, ref: str, *, label: str) -> str:
    result = repo.run("rev-parse", ref, check=False)
    sha = result.stdout.strip()
    if result.returncode != 0 or not sha:
        raise RuntimeError(f"cannot resolve {label}")
    return sha


@contextmanager
def _repo_lock(repo_path: Path, state_home: Path) -> Iterator[None]:
    if not (repo_path / ".git").exists():
        yield
        return
    with _lock_file(state_home / "state" / "repo.lock"):
        yield


@contextmanager
def _health_lock(state_dir: Path) -> Iterator[None]:
    with _lock_file(state_dir / "health.lock"):
        yield


@contextmanager
def _lock_file(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


if __name__ == "__main__":
    raise SystemExit(main())
