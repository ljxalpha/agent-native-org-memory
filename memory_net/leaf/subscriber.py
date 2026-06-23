from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from memory_net import __version__
from memory_net.leaf.health import utc_now
from memory_net.shared.adapters import FileSystemMdAdapter
from memory_net.shared.models import TeamProduct, TeamWritePlan
from memory_net.shared.schema import (
    iter_master_products,
    validate_master_manifest,
    validate_team_product_frontmatter,
)
from memory_net.shared.util.frontmatter import split_frontmatter
from memory_net.shared.util.hashing import short_hash
from memory_net.shared.util.paths import ensure_within, safe_posix_relative_path


@dataclass(frozen=True)
class SubscribeResult:
    products_written: int
    team_target: Path


class SubscribeError(RuntimeError):
    pass


def subscribe(
    *,
    repo_path: Path,
    adapter: FileSystemMdAdapter,
    config_revision: str,
    state_dir: Path | None = None,
    merge_strategy: str = "replace",
    include_index: bool = True,
    dry_run: bool = False,
    max_products: int | None = None,
    manifest: dict[str, object] | None = None,
    product_reader: Callable[[Path], str] | None = None,
) -> SubscribeResult:
    manifest_path = repo_path / "manifest.json"
    if manifest is None and not manifest_path.exists():
        raise SubscribeError("no manifest.json on master; not bootstrapped?")
    if manifest is None:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    validate_master_manifest(manifest, build_version=__version__)
    products: list[TeamProduct] = []
    for distillation_id, item in iter_master_products(manifest):
        if max_products is not None and len(products) >= max_products:
            break
        rel = safe_posix_relative_path(
            str(item["relative_path"]),
            required_prefix="team",
        )
        if product_reader is None:
            source = repo_path / rel
            ensure_within(source, repo_path)
            content = source.read_text(encoding="utf-8")
        else:
            content = product_reader(rel)
        expected_hash = item.get("content_hash")
        if expected_hash and short_hash(content, 16) != str(expected_hash):
            raise ValueError(f"product content_hash mismatch: {rel.as_posix()}")
        frontmatter, _body = split_frontmatter(content)
        validate_team_product_frontmatter(frontmatter)
        if str(frontmatter.get("distillation_id")) != distillation_id:
            raise ValueError(f"product distillation_id mismatch: {rel.as_posix()}")
        products.append(
            TeamProduct(
                relative_path=rel,
                content=content,
                frontmatter=frontmatter,
                category=str(
                    frontmatter.get("category") or item.get("category") or "common"
                ),
                distillation_id=distillation_id,
            )
        )
    if not dry_run:
        adapter.write_team(
            TeamWritePlan(
                products=products,
                config_revision=config_revision,
                merge_strategy=merge_strategy,
                include_index=include_index,
            )
        )
        marker_root = state_dir if state_dir is not None else _default_state_dir()
        marker_root.mkdir(parents=True, exist_ok=True)
        marker = marker_root / "last_pull.json"
        marker.write_text(
            json.dumps(
                {
                    "pulled_at": utc_now(),
                    "config_revision": config_revision,
                    "products": len(products),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    return SubscribeResult(
        products_written=0 if dry_run else len(products),
        team_target=adapter.team_target,
    )


def _default_state_dir() -> Path:
    home = os.environ.get("MEMORY_NET_HOME", str(Path.home() / ".memory-net"))
    return Path(home) / "state"
