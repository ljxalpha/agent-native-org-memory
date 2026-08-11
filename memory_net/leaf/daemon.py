from __future__ import annotations

import fcntl
import json
import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from importlib import import_module
from pathlib import Path
from typing import Any

from memory_net.leaf.health import build_health, write_health
from memory_net.leaf.identity import split_ops_identity

LOGGER = logging.getLogger(__name__)

#: Operational identity, or a callable that re-collects it. A daemon outlives
#: its own configuration: ``config_sync`` repoints ``config-current`` while the
#: process runs, which changes ``config_source_master_sha``. Accepting a
#: callable lets the degraded snapshots read the identity that is true *now*
#: instead of the one captured at startup -- a health snapshot that names the
#: previous config generation is worse than one that names none.
OpsIdentity = dict[str, str] | Callable[[], "dict[str, str] | None"] | None


def _resolve_ops_identity(ops_identity: OpsIdentity) -> dict[str, str] | None:
    return ops_identity() if callable(ops_identity) else ops_identity


def run_foreground(
    *,
    node_id: str,
    state_dir: Path,
    report_task: Callable[[], object] | None = None,
    subscribe_task: Callable[[], object] | None = None,
    config_sync_task: Callable[[], object] | None = None,
    health_task: Callable[[], object] | None = None,
    interval_seconds: int = 300,
    once: bool = False,
    ops_identity: OpsIdentity = None,
) -> None:
    """Run the leaf daemon loop in the foreground.

    :param ops_identity: optional operational identity stamped onto the health
        snapshots this daemon writes, including the degraded snapshots produced
        when a task raises. ``None`` keeps the previous payload byte for byte.
        Pass a callable to have it re-collected at each write, so a snapshot
        taken after a config sync does not carry the pre-sync provenance.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    stop_marker = state_dir / "stop-requested"
    if stop_marker.exists():
        stop_marker.unlink()
    with _exclusive_lock(state_dir / "instance.lock"):
        _run_foreground_locked(
            node_id=node_id,
            state_dir=state_dir,
            stop_marker=stop_marker,
            report_task=report_task,
            subscribe_task=subscribe_task,
            config_sync_task=config_sync_task,
            health_task=health_task,
            interval_seconds=interval_seconds,
            once=once,
            ops_identity=ops_identity,
        )


def _run_foreground_locked(
    *,
    node_id: str,
    state_dir: Path,
    stop_marker: Path,
    report_task: Callable[[], object] | None,
    subscribe_task: Callable[[], object] | None,
    config_sync_task: Callable[[], object] | None,
    health_task: Callable[[], object] | None,
    interval_seconds: int,
    once: bool,
    ops_identity: OpsIdentity = None,
) -> None:
    def default_health_task() -> None:
        identity_kwargs, identity_extra = split_ops_identity(
            _resolve_ops_identity(ops_identity)
        )
        health_path = state_dir / "last_health.json"
        recent_errors = _read_recent_errors(health_path)
        write_health(
            health_path,
            build_health(
                node_id=node_id,
                status="degraded" if recent_errors else "healthy",
                recent_errors=recent_errors,
                published=False,
                extra=identity_extra or None,
                **identity_kwargs,
            ),
        )

    active_health_task = health_task or default_health_task
    tasks = [
        ("config_sync", config_sync_task),
        ("report", report_task),
        ("subscribe", subscribe_task),
        ("health", active_health_task),
    ]
    safe_tasks = [
        (
            name,
            _safe(
                name,
                task,
                state_dir=state_dir,
                node_id=node_id,
                ops_identity=ops_identity,
            ),
        )
        for name, task in tasks
        if task is not None
    ]

    try:
        scheduler_module = import_module("apscheduler.schedulers.background")
        background_scheduler = scheduler_module.BackgroundScheduler
    except ImportError:
        LOGGER.warning("apscheduler is unavailable; falling back to a simple loop")
        while True:
            if stop_marker.exists():
                return
            for _name, task in safe_tasks:
                task()
                if stop_marker.exists():
                    return
            if once:
                return
            deadline = time.monotonic() + interval_seconds
            while time.monotonic() < deadline:
                if stop_marker.exists():
                    return
                time.sleep(min(1.0, deadline - time.monotonic()))

    scheduler = background_scheduler()
    for name, task in safe_tasks:
        seconds = (
            max(30, interval_seconds // 2) if name == "health" else interval_seconds
        )
        scheduler.add_job(task, "interval", seconds=seconds, id=name)
    scheduler.start()
    _safe(
        "health",
        active_health_task,
        state_dir=state_dir,
        node_id=node_id,
        ops_identity=ops_identity,
    )()
    if once:
        for name, task in safe_tasks:
            if name != "health":
                task()
        scheduler.shutdown()
        return
    for name, task in safe_tasks:
        if name == "config_sync":
            task()
            break
    try:
        while True:
            if stop_marker.exists():
                return
            time.sleep(3600)
    finally:
        scheduler.shutdown()


def _safe(
    task_name: str,
    task: Callable[[], object],
    *,
    state_dir: Path,
    node_id: str,
    ops_identity: OpsIdentity = None,
) -> Callable[[], object]:
    def wrapped() -> object | None:
        try:
            return task()
        except Exception as exc:
            LOGGER.exception("leaf daemon task %s failed", task_name)
            _append_recent_error(
                state_dir / "last_health.json",
                node_id=node_id,
                task_name=task_name,
                error=str(exc) or exc.__class__.__name__,
                ops_identity=ops_identity,
            )
            return None

    return wrapped


def _append_recent_error(
    path: Path,
    *,
    node_id: str,
    task_name: str,
    error: str,
    ops_identity: OpsIdentity = None,
) -> None:
    identity_kwargs, identity_extra = split_ops_identity(
        _resolve_ops_identity(ops_identity)
    )

    def degraded() -> dict[str, Any]:
        return build_health(
            node_id=node_id,
            status="degraded",
            extra=identity_extra or None,
            **identity_kwargs,
        )

    if path.exists():
        try:
            health: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            health = degraded()
    else:
        health = degraded()
    health["status"] = "degraded"
    errors = list(health.get("recent_errors") or [])
    errors.append(f"{task_name}: {error}")
    health["recent_errors"] = errors[-10:]
    write_health(path, health)


def _read_recent_errors(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        health = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return [str(item) for item in health.get("recent_errors") or []][-10:]


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
