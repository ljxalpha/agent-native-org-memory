"""Contract tests for ``build_health()`` operational identity fields.

All host-like values in this module are neutral placeholders on purpose:
``build_health()`` must never read the real hostname, environment or files.
"""

from __future__ import annotations

import pytest

from memory_net.leaf.health import build_health

OPS_IDENTITY_FIELDS = (
    "adapter",
    "install_id",
    "code_version",
    "host",
    "build_id",
    "config_source_master_sha",
    "started_at",
)

SAMPLE_VALUES = {
    "adapter": "claude-code",
    "install_id": "install-0001",
    "code_version": "0.1.0",
    "host": "leaf-host-01",
    "build_id": "a" * 40,
    "config_source_master_sha": "b" * 40,
    "started_at": "2026-04-20T09:00:00Z",
}


def test_default_output_has_no_ops_identity_keys() -> None:
    """Regression: omitting every new parameter must not change the payload."""
    health = build_health(node_id="alice")
    assert set(health) == {
        "schema_version",
        "node_id",
        "role",
        "status",
        "last_heartbeat_at",
        "recent_errors",
        "redaction_incidents",
        "published_at",
    }
    for field in OPS_IDENTITY_FIELDS:
        assert field not in health


@pytest.mark.parametrize("field", OPS_IDENTITY_FIELDS)
def test_ops_identity_field_absent_when_not_passed(field: str) -> None:
    others = {key: value for key, value in SAMPLE_VALUES.items() if key != field}
    health = build_health(node_id="alice", **others)
    assert field not in health
    for other, value in others.items():
        assert health[other] == value


@pytest.mark.parametrize("field", OPS_IDENTITY_FIELDS)
def test_ops_identity_field_written_when_passed(field: str) -> None:
    health = build_health(node_id="alice", **{field: SAMPLE_VALUES[field]})
    assert health[field] == SAMPLE_VALUES[field]


@pytest.mark.parametrize("field", OPS_IDENTITY_FIELDS)
def test_ops_identity_field_rejects_empty_string(field: str) -> None:
    with pytest.raises(ValueError):
        build_health(node_id="alice", **{field: ""})


@pytest.mark.parametrize("field", OPS_IDENTITY_FIELDS)
def test_ops_identity_field_rejects_non_string(field: str) -> None:
    with pytest.raises(TypeError):
        build_health(node_id="alice", **{field: 42})


def test_all_ops_identity_fields_written_together() -> None:
    health = build_health(node_id="alice", **SAMPLE_VALUES)
    for field, value in SAMPLE_VALUES.items():
        assert health[field] == value


def test_build_health_does_not_read_host_or_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``build_health`` must stay a pure function over its arguments."""
    import platform

    def explode() -> str:  # pragma: no cover - must never be called
        raise AssertionError("build_health must not read platform.node()")

    monkeypatch.setattr(platform, "node", explode)
    health = build_health(node_id="alice", host="leaf-host-01")
    assert health["host"] == "leaf-host-01"
