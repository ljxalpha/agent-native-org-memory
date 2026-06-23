from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class MemoryRecord:
    memory_id: str | None
    source_locator: str | None
    relative_path: Path
    content: str
    frontmatter: dict[str, Any]
    scope: str


@dataclass(frozen=True)
class TeamProduct:
    relative_path: Path
    content: str
    frontmatter: dict[str, Any]
    category: str
    distillation_id: str


@dataclass(frozen=True)
class TeamWritePlan:
    products: list[TeamProduct]
    config_revision: str
    merge_strategy: str
    include_index: bool = True


@dataclass(frozen=True)
class ReportedRecord:
    memory_id: str
    relative_path: Path
    source_locator: str
    revision_hash: str
    report_fingerprint: str
    category_hint: str
    content: str
    frontmatter: dict[str, Any] = field(default_factory=dict)
