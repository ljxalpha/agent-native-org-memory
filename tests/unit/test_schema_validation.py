from typing import Any

import pytest

from memory_net.shared.schema import SchemaError, validate_bootstrap_state
from memory_net.shared.schema.validation import (
    HEALTH_SCHEMA_VERSION,
    validate_health,
)

OPS_IDENTITY_FIELDS = (
    "adapter",
    "install_id",
    "code_version",
    "host",
    "build_id",
    "config_source_master_sha",
    "started_at",
)

OPS_IDENTITY_SAMPLE = {
    "adapter": "claude-code",
    "install_id": "install-0001",
    "code_version": "0.1.0",
    "host": "leaf-host-01",
    "build_id": "a" * 40,
    "config_source_master_sha": "b" * 40,
    "started_at": "2026-04-20T09:00:00Z",
}


def test_bootstrap_state_rejects_unsupported_protocol_schema_version() -> None:
    value = _bootstrap_state()
    value["health_schema_version"] = 999

    with pytest.raises(SchemaError, match="health_schema_version"):
        validate_bootstrap_state(value, build_version="0.1.0", build_api=1)


def test_bootstrap_state_rejects_unapproved_build_id() -> None:
    value = _bootstrap_state()
    value["approved_build_ids"] = ["build-a"]

    with pytest.raises(SchemaError, match="build_id"):
        validate_bootstrap_state(
            value,
            build_version="0.1.0",
            build_api=1,
            build_id="build-b",
        )


def test_bootstrap_state_accepts_current_protocol_versions() -> None:
    validate_bootstrap_state(
        _bootstrap_state(),
        build_version="0.1.0",
        build_api=1,
        build_id="build-a",
    )


def test_health_schema_version_constant_is_unchanged() -> None:
    """Bumping this constant would fail-close every existing installation.

    ``bootstrap_state.json`` pins ``health_schema_version: 1``; operational
    identity is an additive, never-required extension, so the version stays put.
    """
    assert HEALTH_SCHEMA_VERSION == "memory-net/health/v1"


def test_health_lenient_mode_accepts_payload_without_ops_identity() -> None:
    """Regression: pre-existing health payloads stay valid by default."""
    validate_health(_health())


def test_health_lenient_mode_accepts_full_ops_identity() -> None:
    validate_health(_health(**OPS_IDENTITY_SAMPLE))


@pytest.mark.parametrize("field", OPS_IDENTITY_FIELDS)
def test_health_rejects_empty_ops_identity_value(field: str) -> None:
    with pytest.raises(SchemaError, match=field):
        validate_health(_health(**{field: ""}))


@pytest.mark.parametrize("field", OPS_IDENTITY_FIELDS)
def test_health_rejects_non_string_ops_identity_value(field: str) -> None:
    with pytest.raises(SchemaError, match=field):
        validate_health(_health(**{field: 42}))


def test_health_strict_mode_accepts_full_ops_identity() -> None:
    validate_health(_health(**OPS_IDENTITY_SAMPLE), require_ops_identity=True)


@pytest.mark.parametrize("field", OPS_IDENTITY_FIELDS)
def test_health_strict_mode_requires_each_ops_identity_field(field: str) -> None:
    present = {key: value for key, value in OPS_IDENTITY_SAMPLE.items() if key != field}
    with pytest.raises(SchemaError, match=field):
        validate_health(_health(**present), require_ops_identity=True)


def test_health_strict_mode_lists_every_missing_field() -> None:
    with pytest.raises(SchemaError) as excinfo:
        validate_health(_health(), require_ops_identity=True)
    message = str(excinfo.value)
    for field in OPS_IDENTITY_FIELDS:
        assert field in message


def _health(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": HEALTH_SCHEMA_VERSION,
        "node_id": "alice",
        "role": "leaf",
        "status": "healthy",
        "last_heartbeat_at": "2026-04-20T09:45:00Z",
        "published_at": "2026-04-20T09:46:02Z",
        "recent_errors": [],
        "redaction_incidents": {"blocked_reports": 0, "scanner_hits": {}},
    }
    value.update(overrides)
    return value


def _bootstrap_state() -> dict[str, object]:
    return {
        "schema_version": 1,
        "compatible_code_range": ">=0.1.0,<0.2.0",
        "compatible_build_api_range": ">=1,<2",
        "manifest_schema_version": 1,
        "record_frontmatter_schema_version": 1,
        "product_frontmatter_schema_version": 1,
        "health_schema_version": 1,
        "owner_schema_version": 1,
    }
