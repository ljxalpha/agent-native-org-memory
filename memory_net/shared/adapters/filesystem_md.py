from __future__ import annotations

import fcntl
import fnmatch
import hashlib
import os
import shutil
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from memory_net.shared.adapters.base import MemoryAdapter
from memory_net.shared.models import MemoryRecord, TeamProduct, TeamWritePlan
from memory_net.shared.paths import TEAM_PREFIX
from memory_net.shared.util.frontmatter import split_frontmatter
from memory_net.shared.util.paths import (
    ensure_relative_pattern,
    ensure_within,
    expand_path,
    safe_posix_relative_path,
)

DEFAULT_EXCLUDES = (
    ".git/**",
    ".hg/**",
    ".svn/**",
    "**/.git/**",
    "**/.hg/**",
    "**/.svn/**",
    ".cache/**",
    "**/.cache/**",
    "__pycache__/**",
    "**/__pycache__/**",
    ".pytest_cache/**",
    "**/.pytest_cache/**",
    ".mypy_cache/**",
    "**/.mypy_cache/**",
    ".ruff_cache/**",
    "**/.ruff_cache/**",
    "node_modules/**",
    "**/node_modules/**",
)


def assert_allowed_adapter_path(
    path: Path,
    *,
    agent_root: Path | None,
    label: str,
) -> None:
    resolved = path.expanduser().resolve()
    allowed_roots = [Path.home().resolve()]
    if agent_root is not None:
        allowed_roots.insert(0, agent_root.expanduser().resolve())
    if any(_is_relative_to(resolved, root) for root in allowed_roots):
        return
    roots = ", ".join(str(root) for root in allowed_roots)
    raise ValueError(f"adapter {label} must be under one of: {roots}")


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


@dataclass(frozen=True)
class RootSpec:
    scope: str
    alias: str
    base_dir: Path
    include: tuple[str, ...]
    exclude: tuple[str, ...]


class FileSystemMdAdapter(MemoryAdapter):
    def __init__(
        self,
        *,
        name: str,
        roots: list[RootSpec],
        team_target: Path,
        file_pattern: str = "*.md",
        frontmatter_required: bool = False,
        hint_config: dict[str, Any] | None = None,
        max_records: int | None = None,
        max_file_bytes: int | None = None,
        agent_root: Path | None = None,
    ) -> None:
        self.name = name
        self.roots = roots
        self.team_target = team_target
        self.file_pattern = file_pattern
        self.frontmatter_required = frontmatter_required
        self.hint_config = hint_config or {}
        self.max_records = max_records
        self.max_file_bytes = max_file_bytes
        self.agent_root = agent_root

    @classmethod
    def from_yaml(
        cls,
        path: Path,
        *,
        agent_root: Path | None = None,
    ) -> FileSystemMdAdapter:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        support_status = str(data.get("support_status") or "stable")
        if (
            support_status == "experimental"
            and os.environ.get("MEMORY_NET_ALLOW_EXPERIMENTAL_ADAPTER") != "1"
        ):
            raise RuntimeError(
                f"adapter is experimental; explicit opt-in required: {path}"
            )
        roots: list[RootSpec] = []
        for scope, specs in (data.get("roots") or {}).items():
            for spec in specs or []:
                include = tuple(spec.get("include") or ["**/*.md"])
                exclude = tuple(spec.get("exclude") or [])
                for pattern in include + exclude:
                    ensure_relative_pattern(pattern)
                base_dir = expand_path(
                    str(spec["base_dir"]),
                    agent_root=agent_root,
                )
                assert_allowed_adapter_path(
                    base_dir,
                    agent_root=agent_root,
                    label="base_dir",
                )
                roots.append(
                    RootSpec(
                        scope=scope,
                        alias=str(spec["alias"]),
                        base_dir=base_dir,
                        include=include,
                        exclude=exclude,
                    )
                )

        team_target = expand_path(str(data["team_target"]), agent_root=agent_root)
        assert_allowed_adapter_path(
            team_target,
            agent_root=agent_root,
            label="team_target",
        )
        return cls(
            name=str(data["name"]),
            roots=roots,
            team_target=team_target,
            file_pattern=str(data.get("file_pattern", "*.md")),
            frontmatter_required=bool(data.get("frontmatter_required", False)),
            hint_config=dict(data.get("team_hint_injection") or {}),
            agent_root=agent_root,
        )

    def list_shared(self) -> Iterator[MemoryRecord]:
        emitted = 0
        for root in self.roots:
            if root.scope != "shared" or not root.base_dir.exists():
                continue
            for path in self._iter_matching_files(root):
                if self.max_records is not None and emitted >= self.max_records:
                    return
                if (
                    self.max_file_bytes is not None
                    and path.stat().st_size > self.max_file_bytes
                ):
                    continue
                content = path.read_text(encoding="utf-8")
                frontmatter, _body = split_frontmatter(content)
                if self.frontmatter_required and not frontmatter:
                    continue
                relative_path = path.relative_to(root.base_dir)
                emitted += 1
                yield MemoryRecord(
                    memory_id=frontmatter.get("memory_id"),
                    source_locator=f"{root.alias}:{relative_path.as_posix()}",
                    relative_path=relative_path,
                    content=content,
                    frontmatter=frontmatter,
                    scope=root.scope,
                )

    def write_team(self, plan: TeamWritePlan) -> None:
        if plan.merge_strategy != "replace":
            raise ValueError("MVP-core only supports merge_strategy=replace")

        parent = self.team_target.parent
        parent.mkdir(parents=True, exist_ok=True)
        lock_path = parent / ".team.lock"
        with lock_path.open("a", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                self._write_team_locked(plan, parent)
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _write_team_locked(self, plan: TeamWritePlan, parent: Path) -> None:
        unique = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        tmp = parent / f".team.tmp-{plan.config_revision}-{unique}"
        backup = parent / f".team.prev-{plan.config_revision}-{unique}"
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(backup, ignore_errors=True)
        tmp.mkdir(parents=True)
        try:
            products = list(plan.products)
            if plan.include_index:
                products.append(self._index_product(products))

            for product in products:
                rel = self._strip_team_prefix(product.relative_path)
                target = tmp / rel
                ensure_within(target, tmp)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(product.content, encoding="utf-8")

            moved_existing = False
            if self.team_target.exists():
                os.replace(self.team_target, backup)
                moved_existing = True
            try:
                os.replace(tmp, self.team_target)
            except OSError:
                if moved_existing and backup.exists() and not self.team_target.exists():
                    os.replace(backup, self.team_target)
                raise
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            shutil.rmtree(backup, ignore_errors=True)

    def inject_team_hint(self, hint_spec: dict[str, object]) -> bool:
        if str(hint_spec.get("mode") or "manual") == "never":
            return False
        target = self._hint_target(hint_spec)
        if target is None:
            return False
        start = str(hint_spec.get("anchor_start") or "<!-- memory-net-hint:start -->")
        end = str(hint_spec.get("anchor_end") or "<!-- memory-net-hint:end -->")
        body = str(hint_spec.get("content_template") or "")
        block = f"{start}\n{body.rstrip()}\n{end}\n"
        content = target.read_text(encoding="utf-8") if target.exists() else ""
        new_content = _replace_anchor_block(content, start, end, block)
        if new_content == content:
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_if_unchanged(
            target,
            _content_hash(content),
            lambda current: _replace_anchor_block(current, start, end, block),
        )
        return True

    def remove_team_hint(self, hint_spec: dict[str, object]) -> bool:
        target = self._hint_target(hint_spec)
        if target is None or not target.exists():
            return False
        start = str(hint_spec.get("anchor_start") or "<!-- memory-net-hint:start -->")
        end = str(hint_spec.get("anchor_end") or "<!-- memory-net-hint:end -->")
        content = target.read_text(encoding="utf-8")
        new_content = _remove_anchor_block(content, start, end)
        if new_content == content:
            return False
        _write_if_unchanged(
            target,
            _content_hash(content),
            lambda current: _remove_anchor_block(current, start, end),
        )
        return True

    def _iter_matching_files(self, root: RootSpec) -> Iterator[Path]:
        seen: set[Path] = set()
        for pattern in root.include:
            for path in sorted(root.base_dir.glob(pattern)):
                if not path.is_file() or path in seen:
                    continue
                if not fnmatch.fnmatch(path.name, self.file_pattern):
                    continue
                if not _is_relative_to(path.resolve(), root.base_dir.resolve()):
                    raise ValueError(f"shared file escapes base_dir: {path}")
                rel = path.relative_to(root.base_dir).as_posix()
                if _is_hidden_path(path.relative_to(root.base_dir)):
                    continue
                if any(
                    fnmatch.fnmatch(rel, pat)
                    for pat in (*DEFAULT_EXCLUDES, *root.exclude)
                ):
                    continue
                seen.add(path)
                yield path

    def _hint_target(self, hint_spec: dict[str, object]) -> Path | None:
        target = hint_spec.get("target")
        raw_candidates = hint_spec.get("fallback_targets")
        candidates = (
            [str(item) for item in raw_candidates]
            if isinstance(raw_candidates, list)
            else []
        )
        if isinstance(target, str):
            candidates.insert(0, target)
        if not candidates:
            return None
        for raw in candidates:
            path = self._allowed_hint_path(str(raw))
            if path.exists():
                return path
        install_default = hint_spec.get("install_once_default")
        if isinstance(install_default, str):
            return self._allowed_hint_path(install_default)
        return self._allowed_hint_path(str(candidates[0]))

    def _allowed_hint_path(self, raw: str) -> Path:
        path = expand_path(raw, agent_root=self.agent_root)
        assert_allowed_adapter_path(
            path,
            agent_root=self.agent_root,
            label="hint target",
        )
        return path

    @staticmethod
    def _strip_team_prefix(path: Path) -> Path:
        safe = safe_posix_relative_path(path)
        parts = safe.parts
        if parts and parts[0] == TEAM_PREFIX:
            if len(parts) == 1:
                raise ValueError("team product path must name a file under team/")
            return Path(*parts[1:])
        return safe

    @staticmethod
    def _index_product(products: list[TeamProduct]) -> TeamProduct:
        lines = ["# Team Memory Index", ""]
        for product in sorted(
            products,
            key=lambda item: (item.category, item.relative_path.as_posix()),
        ):
            rel = FileSystemMdAdapter._strip_team_prefix(product.relative_path)
            title = str(product.frontmatter.get("title") or rel.stem.replace("-", " "))
            lines.append(f"- [{title}]({rel.as_posix()})")
        lines.append("")
        return TeamProduct(
            relative_path=Path("INDEX.md"),
            content="\n".join(lines),
            frontmatter={"schema_version": "memory-net/team-index/v1"},
            category="index",
            distillation_id="index",
        )


def _replace_anchor_block(content: str, start: str, end: str, block: str) -> str:
    start_idx = content.find(start)
    if start_idx == -1:
        prefix = "" if content.endswith("\n") or not content else "\n"
        return f"{content}{prefix}{block}"
    end_idx = content.find(end, start_idx)
    if end_idx == -1:
        return f"{content.rstrip()}\n{block}"
    end_idx += len(end)
    if end_idx < len(content) and content[end_idx] == "\n":
        end_idx += 1
    return f"{content[:start_idx]}{block}{content[end_idx:]}"


def _remove_anchor_block(content: str, start: str, end: str) -> str:
    start_idx = content.find(start)
    if start_idx == -1:
        return content
    end_idx = content.find(end, start_idx)
    if end_idx == -1:
        return content
    end_idx += len(end)
    if end_idx < len(content) and content[end_idx] == "\n":
        end_idx += 1
    return content[:start_idx] + content[end_idx:]


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _is_hidden_path(path: Path) -> bool:
    return any(part.startswith(".") for part in path.parts)


def _write_if_unchanged(
    path: Path,
    base_hash: str,
    transform: Callable[[str], str],
) -> None:
    expected_hash = base_hash
    for attempt in range(2):
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if _content_hash(current) != expected_hash:
            if attempt == 0:
                expected_hash = _content_hash(current)
                time.sleep(0.05)
                continue
            raise RuntimeError(f"hint target changed during patch: {path}")
        tmp = path.with_name(f".{path.name}.memory-net.tmp")
        tmp.write_text(transform(current), encoding="utf-8")
        tmp.replace(path)
        return
