from __future__ import annotations

import fcntl
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from memory_net.leaf.health import build_health, utc_now, write_health
from memory_net.leaf.identity import split_ops_identity
from memory_net.leaf.scanner import (
    ScannerAuditResult,
    scanner_audit,
    scanner_mode,
    write_scanner_audit,
)
from memory_net.shared.adapters import FileSystemMdAdapter
from memory_net.shared.models import ReportedRecord
from memory_net.shared.redaction import Redactor, null_redactor
from memory_net.shared.schema import validate_reported_memory_frontmatter
from memory_net.shared.util.frontmatter import render_frontmatter, split_frontmatter
from memory_net.shared.util.hashing import revision_hash, short_hash, stable_json

MEMORY_ID_RE = re.compile(r"^leaf-[a-z0-9-]+-mem-[A-Za-z0-9-]+$")

#: The reporter's advisory lock file, kept inside the leaf worktree so that it
#: scopes to the repository being written rather than to one installation home.
LOCK_FILE_NAME = ".reporter.lock"


@dataclass(frozen=True)
class ReportResult:
    node_id: str
    records_seen: int
    records_written: int
    records_skipped: int
    manifest_path: Path
    scanner_hits: dict[str, int] | None = None


class RedactionBlockedError(RuntimeError):
    pass


def report(
    *,
    repo_path: Path,
    node_id: str,
    adapter: FileSystemMdAdapter,
    redactor: Redactor | None = None,
    config_revision: str = "local",
    adapter_config_revision: str = "adapter-local",
    scanner_config: dict[str, Any] | None = None,
    state_dir: Path | None = None,
    dry_run: bool = False,
    ops_identity: dict[str, str] | None = None,
) -> ReportResult:
    """Report shared agent memories into the leaf repo.

    :param ops_identity: optional operational identity from
        :func:`memory_net.leaf.identity.collect_ops_identity`, stamped onto every
        ``.health.json`` this call writes (including the scanner-block degraded
        path). ``None`` leaves the health payload byte-identical to before this
        parameter existed.
    """
    repo_path.mkdir(parents=True, exist_ok=True)
    lock_path = repo_path / LOCK_FILE_NAME
    with lock_path.open("a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            # Under the lock: two reporters racing on the same clone would
            # otherwise both find the entry absent and both append it.
            _exclude_lock_from_status(repo_path)
            return _report_unlocked(
                repo_path=repo_path,
                node_id=node_id,
                adapter=adapter,
                redactor=redactor,
                config_revision=config_revision,
                adapter_config_revision=adapter_config_revision,
                scanner_config=scanner_config,
                state_dir=state_dir,
                dry_run=dry_run,
                ops_identity=ops_identity,
            )
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _exclude_lock_from_status(repo_path: Path) -> None:
    """Keep the lock file from making the leaf worktree dirty.

    Branch preparation refuses to switch branches on a dirty worktree, and the
    lock is untracked, so without this every report after the first one would
    abort with "cannot switch branches from a dirty worktree" — the daemon would
    report exactly once per clone. The rule goes into the clone-local
    ``.git/info/exclude`` rather than into a committed ``.gitignore`` because
    that also repairs leaf branches created before this call site existed, and
    because a lock file is a property of the working copy, not of the branch.

    A linked worktree and a submodule keep ``.git`` as a *file* pointing at the
    real git directory, so the location is resolved through it rather than
    assumed; those shapes hit the same dirty-worktree abort as a plain clone and
    need the same repair. For a linked worktree the rule has to land in the
    *common* git directory, because that is the only ``info/exclude`` git reads.

    Best effort by design: a repository this cannot resolve, or an unwritable
    one, keeps the previous behaviour instead of failing the report here.
    """
    git_dir = _resolve_git_dir(repo_path)
    if git_dir is None:
        return
    exclude_path = git_dir / "info" / "exclude"
    if not exclude_path.parent.is_dir():
        try:
            exclude_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
    entry = f"/{LOCK_FILE_NAME}"
    try:
        existing = (
            exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
        )
        if entry in existing.splitlines():
            return
        separator = "" if not existing or existing.endswith("\n") else "\n"
        with exclude_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{separator}{entry}\n")
    except OSError:
        return


def _resolve_git_dir(repo_path: Path) -> Path | None:
    """Locate the git directory whose ``info/exclude`` applies to ``repo_path``.

    A plain clone keeps a ``.git`` directory. A linked worktree or a submodule
    keeps a ``.git`` *file* holding ``gitdir: <path>``, possibly relative to the
    worktree. A linked worktree's own git directory additionally holds a
    ``commondir`` pointer, and only the common directory's ``info/exclude`` is
    consulted by git, so that is the one this returns.

    :returns: the git directory, or ``None`` when the repository shape cannot be
        resolved (no ``.git``, an unreadable pointer, a dangling target).
    """
    git_path = repo_path / ".git"
    if git_path.is_dir():
        return _common_git_dir(git_path)
    if not git_path.is_file():
        return None
    try:
        pointer = git_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    prefix = "gitdir:"
    if not pointer.startswith(prefix):
        return None
    target = Path(pointer[len(prefix) :].strip())
    resolved = target if target.is_absolute() else repo_path / target
    return _common_git_dir(resolved) if resolved.is_dir() else None


def _common_git_dir(git_dir: Path) -> Path | None:
    """Follow a ``commondir`` pointer, if this git directory has one."""
    commondir = git_dir / "commondir"
    if not commondir.is_file():
        return git_dir
    try:
        target = Path(commondir.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeDecodeError):
        return git_dir
    resolved = target if target.is_absolute() else git_dir / target
    return resolved if resolved.is_dir() else git_dir


def _report_unlocked(
    *,
    repo_path: Path,
    node_id: str,
    adapter: FileSystemMdAdapter,
    redactor: Redactor | None = None,
    config_revision: str = "local",
    adapter_config_revision: str = "adapter-local",
    scanner_config: dict[str, Any] | None = None,
    state_dir: Path | None = None,
    dry_run: bool = False,
    ops_identity: dict[str, str] | None = None,
) -> ReportResult:
    redactor = redactor or null_redactor()
    identity_kwargs, identity_extra = split_ops_identity(ops_identity)
    manifest = _load_manifest(repo_path, node_id)
    previous_entries: dict[str, Any] = dict(manifest.get("entries") or {})
    tombstones = dict(manifest.get("tombstones") or {})
    locator_to_id = {
        str(entry.get("source_locator")): memory_id
        for memory_id, entry in previous_entries.items()
        if isinstance(entry, dict) and entry.get("source_locator")
    }
    locator_to_id.update(
        {
            str(entry.get("source_locator")): memory_id
            for memory_id, entry in tombstones.items()
            if isinstance(entry, dict) and entry.get("source_locator")
        }
    )
    now = utc_now()
    next_entries: dict[str, Any] = {}
    delete_after_manifest: list[Path] = []
    records_seen = 0
    records_written = 0
    records_skipped = 0
    seen_memory_ids: dict[str, str] = {}
    shared_records = list(adapter.list_shared())
    scanner_records = [
        (record.source_locator, record.content)
        for record in shared_records
        if record.source_locator is not None
    ]
    scanner_result = ScannerAuditResult(findings=[])
    active_scanner_mode = "audit"
    if scanner_config is not None:
        active_scanner_mode = scanner_mode(scanner_config)
        scanner_result = scanner_audit(scanner_records, scanner_config)
        if scanner_result.findings and state_dir is not None and not dry_run:
            write_scanner_audit(state_dir, node_id=node_id, result=scanner_result)
        if scanner_result.findings and active_scanner_mode == "block":
            if not dry_run:
                _write_scanner_block_health(
                    repo_path=repo_path,
                    node_id=node_id,
                    config_revision=config_revision,
                    redaction_pattern_version=redactor.version,
                    reported_at=now,
                    scanner_result=scanner_result,
                    memory_count=len(previous_entries),
                    ops_identity=ops_identity,
                )
            raise RedactionBlockedError(
                f"external scanner blocked {len(scanner_result.findings)} findings"
            )

    for record in shared_records:
        records_seen += 1
        if record.source_locator is None:
            continue
        memory_id = _resolve_memory_id(
            record.memory_id,
            record.source_locator,
            locator_to_id,
            node_id,
        )
        previous_locator = seen_memory_ids.get(memory_id)
        if previous_locator is not None and previous_locator != record.source_locator:
            raise ValueError(
                f"memory_id collision in report: {memory_id} maps to both "
                f"{previous_locator} and {record.source_locator}"
            )
        seen_memory_ids[memory_id] = record.source_locator
        redacted = redactor.redact(record.content)
        if redacted.blocked:
            raise RedactionBlockedError(
                f"redaction blocked {record.source_locator}: "
                f"{','.join(redacted.blocked_rules)}"
            )

        category = _category_hint(record.relative_path, record.frontmatter)
        rev = revision_hash(redacted.content)
        fingerprint = _report_fingerprint(
            revision=rev,
            source_locator=record.source_locator,
            category=category,
            redaction_pattern_version=redactor.version,
            adapter_config_revision=adapter_config_revision,
            frontmatter=record.frontmatter,
        )
        previous = previous_entries.get(memory_id) or {}
        tombstones.pop(memory_id, None)
        relative_path = Path(_record_path(category, record.relative_path, rev))
        next_entries[memory_id] = {
            "status": "active",
            "relative_path": relative_path.as_posix(),
            "source_locator": record.source_locator,
            "revision_hash": rev,
            "category_hint": category,
            "redaction_pattern_version": redactor.version,
            "adapter_config_revision": adapter_config_revision,
            "report_fingerprint": fingerprint,
            "first_seen_at": previous.get("first_seen_at") or now,
            "last_seen_at": now,
            "reported_at": now,
        }
        previous_relative_path = Path(str(previous.get("relative_path") or ""))
        if (
            previous.get("report_fingerprint") == fingerprint
            and (repo_path / relative_path).exists()
        ):
            records_skipped += 1
            continue

        if not dry_run:
            if (
                previous_relative_path != Path(".")
                and previous_relative_path != relative_path
            ):
                delete_after_manifest.append(previous_relative_path)
            _write_reported_record(
                repo_path / relative_path,
                ReportedRecord(
                    memory_id=memory_id,
                    relative_path=relative_path,
                    source_locator=record.source_locator,
                    revision_hash=rev,
                    report_fingerprint=fingerprint,
                    category_hint=category,
                    content=redacted.content,
                    frontmatter=record.frontmatter,
                ),
                node_id=node_id,
                author_adapter=adapter.name,
                created_at=str(previous.get("first_seen_at") or now),
                reported_at=now,
                redaction_pattern_version=redactor.version,
                adapter_config_revision=adapter_config_revision,
                redaction_applied=sorted(redacted.stats),
            )
            records_written += 1

    active_ids = set(next_entries)
    for memory_id, entry in previous_entries.items():
        if memory_id in active_ids:
            continue
        tombstones[memory_id] = {
            "deleted_at": now,
            "last_revision_hash": entry.get("revision_hash"),
            "last_relative_path": entry.get("relative_path"),
            "source_locator": entry.get("source_locator"),
        }
        if not dry_run and entry.get("relative_path"):
            delete_after_manifest.append(Path(str(entry["relative_path"])))

    manifest = {
        "schema_version": "memory-net/leaf-manifest/v1",
        "node_id": node_id,
        "config_revision": config_revision,
        "updated_at": now,
        "entries": next_entries,
        "tombstones": tombstones,
    }
    manifest_path = repo_path / "manifest.json"
    if not dry_run:
        recent_errors: list[str] = []
        health_status = "healthy"
        if scanner_result.findings and active_scanner_mode == "warn":
            health_status = "degraded"
            recent_errors.append(
                f"external scanner found {len(scanner_result.findings)} findings"
            )
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        write_health(
            repo_path / ".health.json",
            build_health(
                node_id=node_id,
                status=health_status,
                config_revision=config_revision,
                last_report_at=now,
                redaction_pattern_version=redactor.version,
                recent_errors=recent_errors,
                extra={"memory_count": len(next_entries)} | identity_extra,
                redaction_incidents={"scanner_hits": scanner_result.hit_counts},
                **identity_kwargs,
            ),
        )
        active_paths = {
            Path(str(entry.get("relative_path")))
            for entry in next_entries.values()
            if isinstance(entry, dict) and entry.get("relative_path")
        }
        for path in sorted(set(delete_after_manifest)):
            if path not in active_paths:
                _delete_reported_file(repo_path, path)
    return ReportResult(
        node_id=node_id,
        records_seen=records_seen,
        records_written=records_written,
        records_skipped=records_skipped,
        manifest_path=manifest_path,
        scanner_hits=scanner_result.hit_counts,
    )


def _write_scanner_block_health(
    *,
    repo_path: Path,
    node_id: str,
    config_revision: str,
    redaction_pattern_version: str,
    reported_at: str,
    scanner_result: ScannerAuditResult,
    memory_count: int,
    ops_identity: dict[str, str] | None = None,
) -> None:
    identity_kwargs, identity_extra = split_ops_identity(ops_identity)
    write_health(
        repo_path / ".health.json",
        build_health(
            node_id=node_id,
            status="degraded",
            config_revision=config_revision,
            last_report_at=reported_at,
            redaction_pattern_version=redaction_pattern_version,
            recent_errors=[
                f"external scanner blocked {len(scanner_result.findings)} findings"
            ],
            extra={"memory_count": memory_count} | identity_extra,
            redaction_incidents={
                "blocked_reports": 1,
                "scanner_hits": scanner_result.hit_counts,
            },
            **identity_kwargs,
        ),
    )


def _load_manifest(repo_path: Path, node_id: str) -> dict[str, Any]:
    path = repo_path / "manifest.json"
    if not path.exists():
        return {
            "schema_version": "memory-net/leaf-manifest/v1",
            "node_id": node_id,
            "entries": {},
            "tombstones": {},
        }
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def _resolve_memory_id(
    current: object,
    source_locator: str,
    locator_to_id: dict[str, str],
    node_id: str,
) -> str:
    if isinstance(current, str):
        expected_prefix = f"leaf-{node_id}-mem-"
        if MEMORY_ID_RE.match(current) and current.startswith(expected_prefix):
            return current
        raise ValueError(f"invalid memory_id for node {node_id}: {current}")
    if source_locator in locator_to_id:
        return locator_to_id[source_locator]
    return f"leaf-{node_id}-mem-{short_hash(source_locator, 16)}"


def _category_hint(relative_path: Path, frontmatter: dict[str, Any]) -> str:
    raw = frontmatter.get("category")
    if isinstance(raw, str) and raw.strip():
        return _slug(raw)
    tags = frontmatter.get("tags")
    if isinstance(tags, list) and tags:
        return _slug(str(tags[0]))
    if len(relative_path.parts) > 1:
        return _slug(relative_path.parts[0])
    return "common_knowledge"


def _record_path(category: str, relative_path: Path, rev: str) -> str:
    return f"memories/{category}/{_slug(relative_path.stem)}-{rev}.md"


def _report_fingerprint(
    *,
    revision: str,
    source_locator: str,
    category: str,
    redaction_pattern_version: str,
    adapter_config_revision: str,
    frontmatter: dict[str, Any],
) -> str:
    subset = {
        key: frontmatter[key]
        for key in ["title", "tags", "category", "memory_id"]
        if key in frontmatter
    }
    payload = stable_json(
        {
            "revision_hash": revision,
            "source_locator": source_locator,
            "category": category,
            "redaction_pattern_version": redaction_pattern_version,
            "adapter_config_revision": adapter_config_revision,
            "frontmatter": subset,
        }
    )
    return f"rpt-{short_hash(payload, 12)}"


def _write_reported_record(
    path: Path,
    record: ReportedRecord,
    *,
    node_id: str,
    author_adapter: str,
    created_at: str,
    reported_at: str,
    redaction_pattern_version: str,
    adapter_config_revision: str,
    redaction_applied: list[str],
) -> None:
    source_fm, body = split_frontmatter(record.content)
    frontmatter = {
        "schema_version": "memory-net/reported-memory/v1",
        "memory_id": record.memory_id,
        "source_locator": record.source_locator,
        "author_node": node_id,
        "author_adapter": author_adapter,
        "category_hint": record.category_hint,
        "revision_hash": record.revision_hash,
        "report_fingerprint": record.report_fingerprint,
        "redaction_pattern_version": redaction_pattern_version,
        "adapter_config_revision": adapter_config_revision,
        "created_at": created_at,
        "updated_at": reported_at,
        "reported_at": reported_at,
        "scope": "shared",
        "confidence": source_fm.get("confidence") or "unknown",
        "source_type": source_fm.get("source_type") or "agent_memory",
        "redaction_applied": redaction_applied,
        "source_frontmatter": source_fm,
    }
    validate_reported_memory_frontmatter(frontmatter)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_frontmatter(frontmatter, body), encoding="utf-8")


def _delete_reported_file(repo_path: Path, relative_path: Path) -> None:
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValueError(f"unsafe reported path: {relative_path}")
    if not relative_path.parts or relative_path.parts[0] != "memories":
        raise ValueError(f"reported path must be under memories/: {relative_path}")
    target = repo_path / relative_path
    if target.exists():
        target.unlink()


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "common"
