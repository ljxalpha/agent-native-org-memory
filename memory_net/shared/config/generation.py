from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import yaml  # type: ignore[import-untyped]

from memory_net.shared.util.hashing import hash_path_bytes

SUBSCRIBER_ADAPTER_FIELDS = ("team_target",)


def collect_generation_files(source_root: Path) -> list[tuple[Path, bytes]]:
    pairs: list[tuple[Path, bytes]] = []
    bootstrap = source_root / "bootstrap_state.json"
    if bootstrap.exists():
        pairs.append((Path("bootstrap_state.json"), bootstrap.read_bytes()))

    config_root = source_root / "config"
    if config_root.exists():
        for path in sorted(config_root.rglob("*")):
            if path.is_file():
                pairs.append((path.relative_to(config_root), path.read_bytes()))
    else:
        for path in sorted(source_root.glob("*.yaml")):
            pairs.append((path.relative_to(source_root), path.read_bytes()))
        adapters_root = source_root / "adapters"
        if adapters_root.exists():
            for path in sorted(adapters_root.rglob("*")):
                if path.is_file():
                    pairs.append((path.relative_to(source_root), path.read_bytes()))

    prompts_root = source_root / "prompts"
    if prompts_root.exists():
        for path in sorted(prompts_root.rglob("*")):
            if path.is_file():
                pairs.append(
                    (
                        Path("prompts") / path.relative_to(prompts_root),
                        path.read_bytes(),
                    )
                )

    return pairs


def canonical_generation_hash(source_root: Path) -> str:
    return hash_path_bytes(collect_generation_files(source_root))


def subscribe_contract_revision(
    source_root: Path,
    *,
    adapter_name: str | None = None,
) -> str:
    adapters: dict[str, bytes] = {}
    adapters_root = source_root / _config_path(source_root, "adapters")
    if adapter_name is None:
        if adapters_root.exists():
            for path in sorted(adapters_root.glob("*.yaml")):
                adapters[path.stem] = path.read_bytes()
    else:
        adapter_path = adapters_root / f"{adapter_name}.yaml"
        if adapter_path.exists():
            adapters[adapter_name] = adapter_path.read_bytes()
    return compute_subscribe_contract_revision(
        bootstrap_state=_optional_file_bytes(source_root / "bootstrap_state.json"),
        subscribe_yaml=_optional_file_bytes(
            source_root / _config_path(source_root, "subscribe.yaml")
        ),
        adapter_yamls=adapters,
        adapter_name=adapter_name,
    )


def compute_subscribe_contract_revision(
    *,
    bootstrap_state: bytes | None,
    subscribe_yaml: bytes | None,
    adapter_yamls: dict[str, bytes],
    adapter_name: str | None = None,
) -> str:
    pairs: list[tuple[Path, bytes]] = []
    if bootstrap_state is not None:
        pairs.append((Path("bootstrap_state.json"), _normalize_json(bootstrap_state)))
    if subscribe_yaml is not None:
        pairs.append((Path("subscribe.yaml"), _normalize_yaml(subscribe_yaml)))
    selected_adapters = (
        adapter_yamls.items()
        if adapter_name is None
        else (
            ((adapter_name, adapter_yamls[adapter_name]),)
            if adapter_name in adapter_yamls
            else ()
        )
    )
    for name, data in sorted(selected_adapters):
        pairs.append(
            (
                Path("adapters") / f"{name}.yaml",
                _normalize_adapter_subscriber_fields(data),
            )
        )
    return hash_path_bytes(pairs)


def _optional_file_bytes(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def _normalize_json(data: bytes) -> bytes:
    value = json.loads(data.decode("utf-8"))
    return _canonical_json_bytes(value)


def _normalize_yaml(data: bytes) -> bytes:
    value = yaml.safe_load(data.decode("utf-8")) or {}
    return _canonical_json_bytes(value)


def _normalize_adapter_subscriber_fields(data: bytes) -> bytes:
    value = yaml.safe_load(data.decode("utf-8")) or {}
    if not isinstance(value, dict):
        value = {}
    subset = {
        field: value[field] for field in SUBSCRIBER_ADAPTER_FIELDS if field in value
    }
    return _canonical_json_bytes(subset)


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _config_path(source_root: Path, *parts: str) -> Path:
    if (source_root / "config").exists():
        return Path("config", *parts)
    return Path(*parts)


def materialize_generation(
    source_root: Path,
    generations_root: Path,
    *,
    adapter_name: str | None = None,
    source_master_sha: str | None = None,
) -> Path:
    """Materialize a content-addressed config generation.

    :param source_master_sha: provenance of the snapshot the config came from.
        When given it is recorded in the ``config_source_master_sha`` marker.

    Marker files (``config_revision``, ``subscribe_contract_revision`` and
    ``config_source_master_sha``) are written **after** the content hash is
    computed and are never part of :func:`collect_generation_files`. Feeding a
    marker into the hash would make one unchanged config resolve to a different
    generation whenever master moved, which would defeat content addressing.
    """
    revision = canonical_generation_hash(source_root)
    target = generations_root / revision
    if target.exists():
        actual = canonical_generation_hash(target)
        if actual == revision:
            subscribe_marker = target / "subscribe_contract_revision"
            desired_contract = subscribe_contract_revision(
                source_root,
                adapter_name=adapter_name,
            )
            if (
                not subscribe_marker.exists()
                or subscribe_marker.read_text(encoding="utf-8") != desired_contract
            ):
                subscribe_marker.write_text(
                    desired_contract,
                    encoding="utf-8",
                )
            _write_source_master_sha(target, source_master_sha)
            return target
        _quarantine_corrupted_generation(target, actual=actual)

    tmp = generations_root / f".{revision}.tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    for rel, data in collect_generation_files(source_root):
        out = tmp / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
    (tmp / "config_revision").write_text(revision, encoding="utf-8")
    (tmp / "subscribe_contract_revision").write_text(
        subscribe_contract_revision(source_root, adapter_name=adapter_name),
        encoding="utf-8",
    )
    _write_source_master_sha(tmp, source_master_sha)
    tmp.replace(target)
    actual = canonical_generation_hash(target)
    if actual != revision:
        shutil.rmtree(target, ignore_errors=True)
        raise RuntimeError(
            f"materialized generation hash mismatch: expected {revision}, got {actual}"
        )
    return target


def _write_source_master_sha(generation: Path, source_master_sha: str | None) -> None:
    """Record snapshot provenance as a marker file, if the caller supplied it.

    Absent provenance leaves no marker at all, so generations materialized by
    callers that predate this parameter stay byte-identical.
    """
    if source_master_sha is None:
        return
    (generation / "config_source_master_sha").write_text(
        source_master_sha,
        encoding="utf-8",
    )


def _quarantine_corrupted_generation(target: Path, *, actual: str) -> Path:
    quarantine_root = target.parent / "_corrupted"
    quarantine_root.mkdir(parents=True, exist_ok=True)
    destination = quarantine_root / f"{target.name}-actual-{actual}-{uuid.uuid4().hex}"
    shutil.move(str(target), str(destination))
    return destination
