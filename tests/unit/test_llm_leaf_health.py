"""What of a leaf's health snapshot may reach the model (mvp_tech_design.md § 8.7.2).

Every record sent to the model carries the health snapshot of the leaf it came
from. The snapshot also carries the leaf's operational identity -- its hostname,
install id and build id -- which is machine identity material and has no role in
distillation. These tests pin the allowlist that narrows the prompt copy, and
pin it from both sides: nothing that identifies a machine gets in, and nothing
the recorded golden prompts already contain falls out (a key that fell out would
change the prompt bytes and turn every strict replay into a miss).
"""

from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from memory_net.distiller.orchestrator import (
    LLM_LEAF_HEALTH_FIELDS,
    _llm_record,
    _prompt_leaf_health,
)
from memory_net.leaf.health import OPS_IDENTITY_FIELDS, build_health
from memory_net.shared.schema.validation import (
    HEALTH_OPS_IDENTITY_FIELDS,
    validate_health,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_BUILDER = REPO_ROOT / "tests" / "fixtures" / "golden_repo" / "build_repo.py"

HOST = "ops-host-sentinel-7f3a"


def _full_health() -> dict[str, Any]:
    """A snapshot shaped like the one a real, fully wired leaf publishes."""
    return build_health(
        node_id="alice-claude-01",
        config_revision="rev-0001",
        last_report_at="2026-01-01T00:00:00Z",
        last_pull_at="2026-01-01T00:00:00Z",
        redaction_pattern_version="rules-0001",
        recent_errors=["report_task: TimeoutError"],
        redaction_incidents={"blocked_reports": 1, "scanner_hits": {"email": 2}},
        published_at="2026-01-01T00:00:00Z",
        adapter="claude-code",
        install_id="install-sentinel-0001",
        code_version="0.1.0",
        host=HOST,
        build_id="b" * 40,
        config_source_master_sha="c" * 40,
        started_at="2026-01-01T00:00:00Z",
        extra={"build_id_source": "package_ref", "memory_count": 3},
    )


def _load_golden_builder() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "llm_leaf_health_build_repo", GOLDEN_BUILDER
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True  # no machine-specific .pyc beside a fixture
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def test_the_projection_keeps_the_reporting_state_and_drops_the_machine() -> None:
    health = _full_health()
    validate_health(health, require_ops_identity=True)
    before = copy.deepcopy(health)

    projected = _prompt_leaf_health(health)

    assert set(projected) == set(health) & LLM_LEAF_HEALTH_FIELDS
    assert set(projected) == {
        "schema_version",
        "node_id",
        "role",
        "status",
        "last_heartbeat_at",
        "published_at",
        "last_report_at",
        "last_pull_at",
        "config_revision",
        "redaction_pattern_version",
        "recent_errors",
        "redaction_incidents",
    }
    for key, value in projected.items():
        assert value == health[key]
    for key in (*OPS_IDENTITY_FIELDS, "build_id_source", "memory_count"):
        assert key not in projected
    assert HOST not in repr(projected)
    # The source snapshot is untouched: the distiller's own roll-up reads it.
    assert health == before
    projected["status"] = "degraded"
    assert health["status"] == before["status"]


def test_the_projection_of_a_minimal_snapshot_adds_nothing() -> None:
    health = build_health(node_id="bob", published=False)
    assert _prompt_leaf_health(health) == health


def test_the_allowlist_holds_no_operational_identity_field() -> None:
    assert LLM_LEAF_HEALTH_FIELDS.isdisjoint(HEALTH_OPS_IDENTITY_FIELDS)
    assert LLM_LEAF_HEALTH_FIELDS.isdisjoint(OPS_IDENTITY_FIELDS)


def test_every_protocol_field_build_health_writes_is_allowed() -> None:
    """Without identity or extras, a snapshot is pure reporting state."""
    protocol_only = build_health(
        node_id="carol",
        config_revision="rev",
        last_report_at="2026-01-01T00:00:00Z",
        last_pull_at="2026-01-01T00:00:00Z",
        redaction_pattern_version="rules",
        recent_errors=["e"],
        redaction_incidents={"blocked_reports": 0, "scanner_hits": {}},
    )
    assert set(protocol_only) <= LLM_LEAF_HEALTH_FIELDS


def test_the_golden_fixture_snapshot_passes_through_unchanged() -> None:
    """Keeps the recorded golden prompts byte-identical under the projection."""
    builder = _load_golden_builder()
    health = builder._leaf_health(
        {"node_id": "alice-claude-01", "reported_at": "2026-01-01T00:00:00Z"}
    )
    assert set(health) <= LLM_LEAF_HEALTH_FIELDS
    assert _prompt_leaf_health(health) == health


def test_a_record_built_for_the_model_carries_only_the_projection() -> None:
    health = _full_health()
    record = _llm_record(
        memory_id="mem-1",
        entry={
            "revision_hash": "abc12345",
            "report_fingerprint": "rpt-test",
            "source_locator": "alice-claude-01:note.md",
        },
        frontmatter={},
        body="body",
        content="content",
        leaf_branch="origin/leaf/alice-claude-01",
        branch_sha="d" * 40,
        node_id="alice-claude-01",
        relative_path="memories/common/note.md",
        leaf_health=health,
    )
    assert record["leaf_health"] == _prompt_leaf_health(health)
    assert HOST not in repr(record)
    assert "install-sentinel-0001" not in repr(record)
