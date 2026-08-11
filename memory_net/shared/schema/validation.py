from __future__ import annotations

import re
from typing import Any

from memory_net.shared.paths import MEMORIES_PREFIX, TEAM_PREFIX
from memory_net.shared.util.paths import (
    safe_posix_relative_path,
)

LEAF_MANIFEST_SCHEMA_VERSION = "memory-net/leaf-manifest/v1"
REPORTED_MEMORY_SCHEMA_VERSION = "memory-net/reported-memory/v1"
HEALTH_SCHEMA_VERSION = "memory-net/health/v1"
#: Operational identity keys of ``.health.json`` (mvp_tech_design.md § 8.7.1).
#: They are additive and never required by the schema version itself, so the
#: constant above must NOT be bumped when they are added or extended.
HEALTH_OPS_IDENTITY_FIELDS: tuple[str, ...] = (
    "adapter",
    "install_id",
    "code_version",
    "host",
    "build_id",
    "config_source_master_sha",
    "started_at",
)
BOOTSTRAP_PROTOCOL_SCHEMA_VERSIONS = {
    "manifest_schema_version": 1,
    "record_frontmatter_schema_version": 1,
    "product_frontmatter_schema_version": 1,
    "health_schema_version": 1,
    "owner_schema_version": 1,
}


class SchemaError(ValueError):
    pass


def validate_leaf_manifest(value: dict[str, Any]) -> None:
    entries = validate_leaf_manifest_header(value)
    for memory_id, entry in entries.items():
        validate_leaf_manifest_entry(str(memory_id), entry)


def validate_leaf_manifest_header(value: dict[str, Any]) -> dict[Any, Any]:
    _require_mapping(value, "leaf manifest")
    schema_version = _require(value, "schema_version", str)
    if schema_version != LEAF_MANIFEST_SCHEMA_VERSION:
        raise SchemaError(f"unsupported leaf manifest schema_version={schema_version}")
    _require(value, "node_id", str)
    entries: dict[Any, Any] = _require(value, "entries", dict)
    return entries


def validate_leaf_manifest_entry(memory_id: str, entry: Any) -> None:
    _require_mapping(entry, f"entry {memory_id}")
    _require(entry, "status", str)
    _require(entry, "relative_path", str)
    safe_posix_relative_path(
        str(entry["relative_path"]),
        required_prefix=MEMORIES_PREFIX,
    )
    _require(entry, "source_locator", str)
    _require(entry, "revision_hash", str)
    _require(entry, "report_fingerprint", str)


def validate_master_manifest(
    value: dict[str, Any],
    *,
    build_version: str | None = None,
) -> None:
    _require_mapping(value, "master manifest")
    schema_version = _require(value, "schema_version", str)
    if schema_version != "memory-net/master-manifest/v1":
        raise SchemaError(f"unsupported master schema_version={schema_version}")
    compatible_leaf_range = _require(value, "compatible_leaf_range", str)
    if build_version is not None:
        _require_in_range(build_version, compatible_leaf_range, "compatible_leaf_range")
    for _distillation_id, product in iter_master_products(value):
        _require_mapping(product, "product")
        _require(product, "relative_path", str)
        _require(product, "distillation_id", str)
        _validate_team_relative_path(str(product["relative_path"]))


def validate_health(
    value: dict[str, Any],
    *,
    require_ops_identity: bool = False,
) -> None:
    """Validate a ``.health.json`` payload.

    Operational identity fields are optional by default so that payloads written
    before they existed remain valid. Pass ``require_ops_identity=True`` for the
    strict operational lane (post-install self-check, stability soak), where all
    seven fields must be present.
    """
    _require_mapping(value, "health")
    schema_version = _require(value, "schema_version", str)
    if schema_version != HEALTH_SCHEMA_VERSION:
        raise SchemaError(f"unsupported health schema_version={schema_version}")
    _require(value, "node_id", str)
    _require(value, "role", str)
    status = _require(value, "status", str)
    if status not in {"healthy", "degraded"}:
        raise SchemaError(f"unsupported health status={status}")
    _require(value, "last_heartbeat_at", str)
    _require(value, "published_at", str)
    recent_errors = _require(value, "recent_errors", list)
    if not all(isinstance(item, str) for item in recent_errors):
        raise SchemaError("recent_errors must contain strings")
    incidents = _require(value, "redaction_incidents", dict)
    blocked_reports = incidents.get("blocked_reports", 0)
    if (
        isinstance(blocked_reports, bool)
        or not isinstance(blocked_reports, int)
        or blocked_reports < 0
    ):
        raise SchemaError("blocked_reports must be non-negative int")
    scanner_hits = _require(incidents, "scanner_hits", dict)
    if not isinstance(scanner_hits, dict):
        raise SchemaError("scanner_hits must be mapping")
    for scanner, count in scanner_hits.items():
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise SchemaError(f"scanner_hits[{scanner}] must be non-negative int")
    _validate_health_ops_identity(value, require=require_ops_identity)


def _validate_health_ops_identity(value: dict[str, Any], *, require: bool) -> None:
    missing = [field for field in HEALTH_OPS_IDENTITY_FIELDS if field not in value]
    if require and missing:
        raise SchemaError(
            "missing required operational identity fields: " + ", ".join(missing)
        )
    for field in HEALTH_OPS_IDENTITY_FIELDS:
        if field not in value:
            continue
        item = value[field]
        if not isinstance(item, str):
            raise SchemaError(f"{field} must be str")
        if not item:
            raise SchemaError(f"{field} must not be empty")


def validate_owner(value: dict[str, Any]) -> None:
    _require_mapping(value, "owner")
    _require(value, "schema_version", str)
    _require(value, "node_id", str)
    _require(value, "install_id", str)
    _require(value, "owner_fingerprint", str)
    _require(value, "host", str)


def validate_bootstrap_state(
    value: dict[str, Any],
    *,
    build_version: str,
    build_api: int,
    build_id: str | None = None,
) -> None:
    _require_mapping(value, "bootstrap state")
    schema_version = _require(value, "schema_version", int)
    if schema_version != 1:
        raise SchemaError(f"unsupported bootstrap schema_version={schema_version}")
    for key, expected in BOOTSTRAP_PROTOCOL_SCHEMA_VERSIONS.items():
        actual = _require(value, key, int)
        if actual != expected:
            raise SchemaError(f"unsupported bootstrap {key}={actual}")
    code_range = _require(value, "compatible_code_range", str)
    api_range = _require(value, "compatible_build_api_range", str)
    _require_in_range(build_version, code_range, "compatible_code_range")
    _require_in_range(str(build_api), api_range, "compatible_build_api_range")
    approved_build_ids = value.get("approved_build_ids")
    if approved_build_ids is not None:
        if not isinstance(approved_build_ids, list) or not all(
            isinstance(item, str) for item in approved_build_ids
        ):
            raise SchemaError("approved_build_ids must contain strings")
        if build_id is None or build_id not in approved_build_ids:
            raise SchemaError("build_id is not approved by bootstrap_state")


def validate_team_product_frontmatter(value: dict[str, Any]) -> None:
    _require_mapping(value, "team product frontmatter")
    schema_version = _require(value, "schema_version", str)
    if schema_version != "memory-net/team-product/v1":
        raise SchemaError(f"unsupported team product schema_version={schema_version}")
    _require(value, "distillation_id", str)
    _require(value, "category", str)
    citations = value.get("citations")
    if citations is not None and not isinstance(citations, list):
        raise SchemaError("citations must be list")


def validate_reported_memory_frontmatter(value: dict[str, Any]) -> None:
    _require_mapping(value, "reported memory frontmatter")
    schema_version = _require(value, "schema_version", str)
    if schema_version != REPORTED_MEMORY_SCHEMA_VERSION:
        raise SchemaError(
            f"unsupported reported memory schema_version={schema_version}"
        )
    _require(value, "memory_id", str)
    _require(value, "source_locator", str)
    _require(value, "author_node", str)
    _require(value, "author_adapter", str)
    _require(value, "category_hint", str)
    _require(value, "revision_hash", str)
    _require(value, "report_fingerprint", str)
    _require(value, "redaction_pattern_version", str)
    _require(value, "adapter_config_revision", str)
    _require(value, "created_at", str)
    _require(value, "updated_at", str)
    _require(value, "reported_at", str)
    scope = _require(value, "scope", str)
    if scope != "shared":
        raise SchemaError(f"reported memory scope must be shared: {scope}")
    _require(value, "confidence", str)
    _require(value, "source_type", str)
    redaction_applied = value.get("redaction_applied")
    if redaction_applied is not None and not isinstance(redaction_applied, list):
        raise SchemaError("redaction_applied must be list")
    source_frontmatter = value.get("source_frontmatter")
    if source_frontmatter is not None and not isinstance(source_frontmatter, dict):
        raise SchemaError("source_frontmatter must be mapping")


def iter_master_products(value: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    products = value.get("products_active")
    if isinstance(products, dict):
        result: list[tuple[str, dict[str, Any]]] = []
        for distillation_id, product in products.items():
            _require_mapping(product, f"product {distillation_id}")
            copied = dict(product)
            copied.setdefault("distillation_id", str(distillation_id))
            result.append((str(distillation_id), copied))
        return result
    if isinstance(products, list):
        result = []
        for product in products:
            _require_mapping(product, "product")
            distillation_id = _require(product, "distillation_id", str)
            result.append((distillation_id, dict(product)))
        return result
    raise SchemaError("products_active must be a mapping")


def _require_mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SchemaError(f"{label} must be a mapping")
    return value


def _require(value: dict[str, Any], key: str, expected_type: type) -> Any:
    if key not in value:
        raise SchemaError(f"missing required key: {key}")
    item = value[key]
    if not isinstance(item, expected_type):
        raise SchemaError(f"{key} must be {expected_type.__name__}")
    return item


def _validate_team_relative_path(raw: str) -> None:
    try:
        path = safe_posix_relative_path(raw, required_prefix=TEAM_PREFIX)
    except ValueError as exc:
        raise SchemaError(str(exc)) from exc
    if path.suffix != ".md":
        raise SchemaError(f"team product path must be markdown: {raw}")


def _require_in_range(version: str, range_spec: str, label: str) -> None:
    for part in [item.strip() for item in range_spec.split(",") if item.strip()]:
        match = re.fullmatch(r"(<=|>=|<|>|==)\s*([0-9]+(?:\.[0-9]+)*)", part)
        if match is None:
            raise SchemaError(f"unsupported {label} comparator: {part}")
        op, bound = match.groups()
        cmp_result = _compare_versions(version, bound)
        if op == ">=" and cmp_result < 0:
            raise SchemaError(f"{version} does not satisfy {label} {range_spec}")
        if op == ">" and cmp_result <= 0:
            raise SchemaError(f"{version} does not satisfy {label} {range_spec}")
        if op == "<=" and cmp_result > 0:
            raise SchemaError(f"{version} does not satisfy {label} {range_spec}")
        if op == "<" and cmp_result >= 0:
            raise SchemaError(f"{version} does not satisfy {label} {range_spec}")
        if op == "==" and cmp_result != 0:
            raise SchemaError(f"{version} does not satisfy {label} {range_spec}")


def _compare_versions(left: str, right: str) -> int:
    left_parts = _version_parts(left)
    right_parts = _version_parts(right)
    width = max(len(left_parts), len(right_parts))
    padded_left = left_parts + (0,) * (width - len(left_parts))
    padded_right = right_parts + (0,) * (width - len(right_parts))
    if padded_left < padded_right:
        return -1
    if padded_left > padded_right:
        return 1
    return 0


def _version_parts(value: str) -> tuple[int, ...]:
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", value):
        raise SchemaError(f"unsupported version value: {value}")
    return tuple(int(part) for part in value.split("."))
