import pytest

from memory_net.shared.schema import SchemaError, validate_bootstrap_state


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
