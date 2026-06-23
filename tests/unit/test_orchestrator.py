import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import memory_net.distiller.orchestrator as orchestrator_module
from memory_net.distiller.llm.base import LLMResponse
from memory_net.distiller.llm.mock import MockLLMProvider
from memory_net.distiller.orchestrator import (
    DistillOrchestrator,
    SupersededRunError,
    _new_run_id,
)
from memory_net.distiller.pipeline import DistillationOutput, DistillationPipeline
from memory_net.leaf.health import build_health
from memory_net.shared.config import canonical_generation_hash
from memory_net.shared.models import TeamProduct


def test_orchestrator_raises_superseded_when_master_advanced(tmp_path: Path) -> None:
    repo = _distill_clone(tmp_path)
    orchestrator = _orchestrator(repo)
    old_sha = _git(repo, "rev-parse", "HEAD")
    (repo / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "memory-net/master-manifest/v1",
                "compatible_leaf_range": ">=0.1.0,<0.2.0",
                "last_run_id": "advanced",
                "products_active": {},
            }
        ),
        encoding="utf-8",
    )
    _git(repo, "add", "manifest.json")
    _git(repo, "commit", "-m", "advance master")
    _git(repo, "push", "origin", "master")

    with pytest.raises(SupersededRunError):
        orchestrator._ensure_master_unchanged(old_sha, "run")

    assert (repo / "distill_runs" / "run" / "superseded.json").exists()


def test_orchestrator_marks_tombstone_when_product_disappears(tmp_path: Path) -> None:
    repo = _distill_clone(tmp_path)
    orchestrator = _orchestrator(repo)

    manifest = orchestrator._master_manifest(
        run_id="run",
        snapshot_sha="abc",
        products=[],
        previous_manifest={
            "products_active": {
                "distill-old": {
                    "relative_path": "team/common/old.md",
                    "topic_id": "topic",
                    "category": "common",
                    "snapshot_ref": "distill_runs/old/snapshot.json",
                    "contributors": ["alice"],
                    "citations": ["leaf-a-mem-1@abc"],
                }
            }
        },
    )

    assert "distill-old" in manifest["products_tombstones"]
    assert manifest["products_active"] == {}


def test_new_run_id_is_unique_with_same_timestamp(monkeypatch: Any) -> None:
    class FixedDatetime:
        @classmethod
        def now(cls, tz: Any) -> datetime:
            return datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)

    suffixes = iter(["aaaaaaaa", "bbbbbbbb"])

    class FixedUuid:
        @property
        def hex(self) -> str:
            return next(suffixes)

    monkeypatch.setattr(orchestrator_module, "datetime", FixedDatetime)
    monkeypatch.setattr(orchestrator_module.uuid, "uuid4", FixedUuid)

    assert _new_run_id() == "20260505T120000Z-aaaaaaaa"
    assert _new_run_id() == "20260505T120000Z-bbbbbbbb"


def test_orchestrator_quarantines_memory_id_collision_across_leaves(
    tmp_path: Path,
) -> None:
    repo = _distill_clone(tmp_path)
    _add_leaf(repo, "alice", "leaf-shared-mem-1", "alice text")
    _add_leaf(repo, "bob", "leaf-shared-mem-1", "bob text")
    _git(repo, "fetch", "--all", "--prune")

    result = _orchestrator(repo, no_push=True).run()

    metrics = json.loads(
        (repo / "distill_runs" / result.run_id / "metrics.json").read_text(
            encoding="utf-8"
        )
    )
    assert result.input_records == 1
    assert metrics["id_collisions"] == 1
    health = json.loads((repo / ".health.json").read_text(encoding="utf-8"))
    assert health["status"] == "degraded"
    assert health["id_collisions"] == 1


def test_orchestrator_quarantines_incompatible_leaf_manifest(
    tmp_path: Path,
) -> None:
    repo = _distill_clone(tmp_path)
    _add_leaf(repo, "alice", "leaf-alice-mem-1", "alice text")
    _add_invalid_leaf_manifest(repo, "bad")
    _git(repo, "fetch", "--all", "--prune")

    result = _orchestrator(repo, no_push=True).run()

    metrics = json.loads(
        (repo / "distill_runs" / result.run_id / "metrics.json").read_text(
            encoding="utf-8"
        )
    )
    assert result.input_records == 1
    assert metrics["incompatible_leaves"] == 1
    health = json.loads((repo / ".health.json").read_text(encoding="utf-8"))
    assert health["status"] == "degraded"
    assert health["incompatible_leaves"] == 1


def test_orchestrator_counts_missing_leaf_manifest_as_incompatible(
    tmp_path: Path,
) -> None:
    repo = _distill_clone(tmp_path)
    _add_leaf(repo, "alice", "leaf-alice-mem-1", "alice text")
    _add_leaf_without_manifest(repo, "bad")
    _git(repo, "fetch", "--all", "--prune")

    result = _orchestrator(repo, no_push=True).run()

    metrics = json.loads(
        (repo / "distill_runs" / result.run_id / "metrics.json").read_text(
            encoding="utf-8"
        )
    )
    assert result.input_records == 1
    assert metrics["incompatible_leaves"] == 1
    health = json.loads((repo / ".health.json").read_text(encoding="utf-8"))
    assert health["status"] == "degraded"


def test_orchestrator_counts_invalid_leaf_health_as_incompatible(
    tmp_path: Path,
) -> None:
    repo = _distill_clone(tmp_path)
    _add_leaf(repo, "alice", "leaf-alice-mem-1", "alice text")
    _add_leaf(repo, "bad", "leaf-bad-mem-1", "bad text")
    _replace_leaf_health(
        repo,
        "bad",
        {
            "schema_version": "memory-net/health/v2",
            "node_id": "bad",
            "status": "healthy",
        },
    )
    _git(repo, "fetch", "--all", "--prune")

    result = _orchestrator(repo, no_push=True).run()

    metrics = json.loads(
        (repo / "distill_runs" / result.run_id / "metrics.json").read_text(
            encoding="utf-8"
        )
    )
    assert result.input_records == 1
    assert metrics["incompatible_leaves"] == 1
    assert metrics["scanner_audit_summary"] == {}


def test_orchestrator_quarantines_malformed_scanner_health(
    tmp_path: Path,
) -> None:
    repo = _distill_clone(tmp_path)
    _add_leaf(repo, "alice", "leaf-alice-mem-1", "alice text")
    _add_leaf(repo, "bad", "leaf-bad-mem-1", "bad text")
    _replace_leaf_health(
        repo,
        "bad",
        {
            "schema_version": "memory-net/health/v1",
            "node_id": "bad",
            "status": "healthy",
            "redaction_incidents": {
                "blocked_reports": 0,
                "scanner_hits": {"builtin": "not-int"},
            },
        },
    )
    _git(repo, "fetch", "--all", "--prune")

    result = _orchestrator(repo, no_push=True).run()

    metrics = json.loads(
        (repo / "distill_runs" / result.run_id / "metrics.json").read_text(
            encoding="utf-8"
        )
    )
    assert result.input_records == 1
    assert metrics["incompatible_leaves"] == 1
    assert metrics["scanner_audit_summary"] == {}


def test_orchestrator_quarantines_bad_record_without_dropping_leaf(
    tmp_path: Path,
) -> None:
    repo = _distill_clone(tmp_path)
    _add_leaf(repo, "alice", "leaf-alice-mem-1", "alice text")
    _add_missing_leaf_record(repo, "alice", "leaf-alice-mem-missing")
    _git(repo, "fetch", "--all", "--prune")

    result = _orchestrator(repo, no_push=True).run()

    metrics = json.loads(
        (repo / "distill_runs" / result.run_id / "metrics.json").read_text(
            encoding="utf-8"
        )
    )
    assert result.input_records == 1
    assert metrics["quarantined_records"] == 1
    health = json.loads((repo / ".health.json").read_text(encoding="utf-8"))
    assert health["status"] == "degraded"
    assert health["quarantined_records"] == 1


def test_orchestrator_dry_run_writes_isolated_outputs(tmp_path: Path) -> None:
    repo = _distill_clone(tmp_path)
    _add_leaf(repo, "alice", "leaf-alice-mem-1", "alice text")
    _git(repo, "fetch", "--all", "--prune")

    result = _orchestrator(repo, dry_run=True).run()

    assert result.input_records == 1
    assert not (repo / "team").exists()
    assert result.manifest_path == repo / "_dryrun" / result.run_id / "manifest.json"
    assert result.manifest_path.exists()
    assert list((repo / "_dryrun" / result.run_id / "team").rglob("*.md"))
    assert (repo / "_dryrun" / result.run_id / "metrics.json").exists()
    assert (repo / "_dryrun" / result.run_id / "snapshot.json").exists()


def test_orchestrator_no_push_writes_outputs_without_remote_commit(
    tmp_path: Path,
) -> None:
    repo = _distill_clone(tmp_path)
    _add_leaf(repo, "alice", "leaf-alice-mem-1", "alice text")
    _git(repo, "fetch", "--all", "--prune")
    before = _git(repo, "rev-parse", "origin/master")

    result = _orchestrator(repo, no_push=True).run()

    assert result.products == 1
    assert (repo / "manifest.json").exists()
    assert list((repo / "distill_runs" / result.run_id / "team").rglob("*.md"))
    assert _git(repo, "rev-parse", "origin/master") == before


def test_orchestrator_fails_when_snapshot_prompt_is_missing(tmp_path: Path) -> None:
    repo = _distill_clone(tmp_path)
    (repo / "prompts" / "distill_user.md").unlink()
    _git(repo, "add", "prompts/distill_user.md")
    _git(repo, "commit", "-m", "remove user prompt")
    _git(repo, "push", "origin", "master")
    _add_leaf(repo, "alice", "leaf-alice-mem-1", "alice text")
    _git(repo, "fetch", "--all", "--prune")

    with pytest.raises(RuntimeError, match="prompt missing from master snapshot"):
        _orchestrator(repo, no_push=True).run()


def test_orchestrator_review_gate_pushes_release_branch_not_master(
    tmp_path: Path,
) -> None:
    repo = _distill_clone(tmp_path)
    _add_leaf(repo, "alice", "leaf-alice-mem-1", "alice text")
    _git(repo, "fetch", "--all", "--prune")
    before = _git(repo, "rev-parse", "origin/master")

    result = _orchestrator(
        repo,
        publish_branch_template="release/<run_id>",
    ).run()

    assert _git(repo, "rev-parse", "origin/master") == before
    assert _git(repo, "rev-parse", "master") == before
    release_ref = _git(
        repo,
        "ls-remote",
        "--heads",
        "origin",
        f"release/{result.run_id}",
    )
    assert f"refs/heads/release/{result.run_id}" in release_ref
    assert _git(repo, "branch", "--show-current") == f"release/{result.run_id}"
    metrics = json.loads(
        (repo / "distill_runs" / result.run_id / "metrics.json").read_text(
            encoding="utf-8"
        )
    )
    assert metrics["review_gate_status"] == "pending"
    assert metrics["publish_branch"] == f"release/{result.run_id}"
    assert metrics["target_branch"] == "master"
    assert (repo / "manifest.json").exists()


def test_orchestrator_frozen_snapshot_keeps_current_branch_and_master_ref(
    tmp_path: Path,
) -> None:
    repo = _distill_clone(tmp_path)
    _add_leaf(repo, "alice", "leaf-alice-mem-1", "alice text")
    _git(repo, "fetch", "--all", "--prune")
    snapshot_sha = _git(repo, "rev-parse", "origin/master")
    _git(repo, "switch", "-c", "work")

    result = _orchestrator(
        repo,
        publish_branch_template="release/<run_id>",
        frozen_master_snapshot_sha=snapshot_sha,
    ).run()

    assert _git(repo, "branch", "--show-current") == "work"
    assert _git(repo, "rev-parse", "master") == snapshot_sha
    release_ref = _git(
        repo,
        "ls-remote",
        "--heads",
        "origin",
        f"release/{result.run_id}",
    )
    assert f"refs/heads/release/{result.run_id}" in release_ref


def test_orchestrator_frozen_no_push_keeps_outputs_in_original_worktree(
    tmp_path: Path,
) -> None:
    repo = _distill_clone(tmp_path)
    _add_leaf(repo, "alice", "leaf-alice-mem-1", "alice text")
    _git(repo, "fetch", "--all", "--prune")
    snapshot_sha = _git(repo, "rev-parse", "origin/master")
    _git(repo, "switch", "-c", "work")

    result = _orchestrator(
        repo,
        no_push=True,
        frozen_master_snapshot_sha=snapshot_sha,
    ).run()

    assert result.manifest_path == repo / "manifest.json"
    assert result.manifest_path.exists()
    assert (repo / "distill_runs" / result.run_id / "metrics.json").exists()
    assert list((repo / "team").rglob("*.md"))
    assert _git(repo, "branch", "--show-current") == "work"


def test_orchestrator_frozen_failure_keeps_artifacts_in_original_worktree(
    tmp_path: Path,
) -> None:
    repo = _distill_clone(tmp_path)
    _add_leaf(repo, "alice", "leaf-alice-mem-1", "alice text")
    _git(repo, "fetch", "--all", "--prune")
    snapshot_sha = _git(repo, "rev-parse", "origin/master")
    _git(repo, "switch", "-c", "work")

    with pytest.raises(RuntimeError, match="pipeline boom"):
        _orchestrator(
            repo,
            frozen_master_snapshot_sha=snapshot_sha,
            pipeline=FailingPipeline(),
        ).run()

    health = json.loads((repo / ".health.json").read_text(encoding="utf-8"))
    metrics = json.loads(
        (repo / "distill_runs" / "latest" / "metrics.json").read_text(encoding="utf-8")
    )
    assert health["status"] == "degraded"
    assert metrics["status"] == "failed"
    assert _git(repo, "branch", "--show-current") == "work"


def test_orchestrator_writes_empty_run_product_snapshot(tmp_path: Path) -> None:
    repo = _distill_clone(tmp_path)

    _orchestrator(repo)._write_run_product_snapshot("empty", [])

    assert (repo / "distill_runs" / "empty" / "team").is_dir()
    assert (repo / "distill_runs" / "empty" / "team" / ".gitkeep").exists()


def test_orchestrator_restores_team_backup_when_final_rename_fails(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    repo = _distill_clone(tmp_path)
    existing = repo / "team" / "old.md"
    existing.parent.mkdir()
    existing.write_text("old\n", encoding="utf-8")
    real_replace = os.replace
    calls = 0

    def failing_replace(src: str | Path, dst: str | Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected final rename failure")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", failing_replace)

    with pytest.raises(OSError):
        _orchestrator(repo)._write_team_products([])

    assert existing.read_text(encoding="utf-8") == "old\n"


def test_orchestrator_restores_push_failure_artifacts_after_reset(
    tmp_path: Path,
) -> None:
    repo = _distill_clone(tmp_path)
    orchestrator = _orchestrator(repo)
    snapshot_sha = _git(repo, "rev-parse", "HEAD")
    run_dir = repo / "distill_runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "metrics.json").write_text("{}\n", encoding="utf-8")
    (repo / "team").mkdir(exist_ok=True)
    (repo / "team" / "note.md").write_text("note\n", encoding="utf-8")
    _git(repo, "add", "distill_runs", "team")
    _git(repo, "commit", "-m", "distill run run-1")

    evidence_root = orchestrator._snapshot_push_failure_evidence(
        "run-1",
        snapshot_sha,
        "push failed",
    )
    _git(repo, "reset", "--hard", snapshot_sha)
    orchestrator._restore_push_failure_evidence("run-1", evidence_root)

    assert (run_dir / "metrics.json").exists()
    assert (run_dir / "push_failed" / "error.txt").read_text(
        encoding="utf-8"
    ) == "push failed\n"


def test_orchestrator_metrics_include_scanner_audit_summary(tmp_path: Path) -> None:
    repo = _distill_clone(tmp_path)
    _add_leaf(
        repo, "alice", "leaf-alice-mem-1", "alice text", scanner_hits={"builtin": 2}
    )
    _add_leaf_record(repo, "alice", "leaf-alice-mem-2", "alice second text")
    _add_empty_leaf(repo, "empty", scanner_hits={"builtin": 3})
    _git(repo, "fetch", "--all", "--prune")

    result = _orchestrator(repo, no_push=True).run()

    metrics = json.loads(
        (repo / "distill_runs" / result.run_id / "metrics.json").read_text(
            encoding="utf-8"
        )
    )
    assert metrics["scanner_audit_summary"] == {"builtin": 5}
    assert metrics["pre_publish_duration_seconds"] >= 0
    assert metrics["review_gate_status"] == "disabled"
    assert metrics["config_revision"] == canonical_generation_hash(repo)


def test_orchestrator_metrics_include_tuning_fields(tmp_path: Path) -> None:
    repo = _distill_clone(tmp_path)
    _add_leaf(repo, "alice", "leaf-alice-mem-1", "alice text")
    _add_leaf_record(repo, "alice", "leaf-alice-mem-2", "alice second text")
    _git(repo, "fetch", "--all", "--prune")

    result = _orchestrator(repo, no_push=True).run()

    metrics = json.loads(
        (repo / "distill_runs" / result.run_id / "metrics.json").read_text(
            encoding="utf-8"
        )
    )
    assert metrics["clusters_attempted"] >= 1
    assert metrics["parse_failure_rate"] == 0.0
    assert metrics["citations_emitted"] >= 1
    assert metrics["citations_resolved"] == metrics["citations_emitted"]
    assert metrics["citation_resolution_rate"] == 1.0
    assert metrics["fallback_selected"] == 0
    assert metrics["fallback_accepted"] == 0
    assert metrics["evidence_rule_rejections"] == 0
    # The active products partition exactly across the category distribution.
    assert sum(metrics["category_distribution"].values()) == metrics["products"]
    assert metrics["products"] >= 1
    # On a live run nothing is replayed; every call hit a live provider.
    assert metrics["replay_mode"] == "live"
    assert metrics["replay_hits"] == 0
    assert metrics["replay_misses"] == 0
    assert metrics["replay_live_calls"] == metrics["clusters_attempted"]


def test_orchestrator_writes_record_bundle(tmp_path: Path) -> None:
    repo = _distill_clone(tmp_path)
    _add_leaf(repo, "alice", "leaf-alice-mem-1", "alice text")
    _add_leaf_record(repo, "alice", "leaf-alice-mem-2", "alice second text")
    _git(repo, "fetch", "--all", "--prune")
    bundle = tmp_path / "record_runs" / "bundle"

    result = _orchestrator(repo, no_push=True, record_bundle_dir=bundle).run()

    cluster_assignment = json.loads(
        (bundle / "cluster_assignment.json").read_text(encoding="utf-8")
    )
    snapshot = json.loads((bundle / "snapshot.json").read_text(encoding="utf-8"))
    record_metrics = json.loads(
        (bundle / "record_metrics.json").read_text(encoding="utf-8")
    )
    assert cluster_assignment["run_id"] == result.run_id
    assert cluster_assignment["clusters"]
    assert snapshot["run_id"] == result.run_id
    assert snapshot["leaf_refs"]
    assert record_metrics["replay_mode"] == "live"
    assert record_metrics["replay_hits"] == 0
    assert record_metrics["replay_live_calls"] == record_metrics["calls"]
    # The bundle lives outside the repo and is never staged onto master.
    assert bundle not in (repo / "distill_runs").glob("*")


def test_orchestrator_writes_degraded_health_when_pipeline_fails(
    tmp_path: Path,
) -> None:
    repo = _distill_clone(tmp_path)
    _add_leaf(repo, "alice", "leaf-alice-mem-1", "alice text")
    _git(repo, "fetch", "--all", "--prune")

    with pytest.raises(RuntimeError, match="pipeline boom"):
        _orchestrator(repo, pipeline=FailingPipeline()).run()

    health = json.loads((repo / ".health.json").read_text(encoding="utf-8"))
    metrics = json.loads(
        (repo / "distill_runs" / "latest" / "metrics.json").read_text(encoding="utf-8")
    )
    assert health["status"] == "degraded"
    assert "pipeline boom" in health["recent_errors"]
    assert metrics["status"] == "failed"
    assert metrics["recent_errors"] == ["pipeline boom"]


def test_orchestrator_does_not_publish_when_quality_gate_fails(
    tmp_path: Path,
) -> None:
    repo = _distill_clone(tmp_path)
    _add_leaf(repo, "alice", "leaf-alice-mem-1", "alice text")
    _git(repo, "fetch", "--all", "--prune")
    before = _git(repo, "rev-parse", "origin/master")

    with pytest.raises(RuntimeError, match="distill quality gate failed"):
        _orchestrator(repo, pipeline=GateFailingPipeline()).run()

    _git(repo, "fetch", "origin", "master")
    assert _git(repo, "rev-parse", "origin/master") == before
    metrics = json.loads(
        (repo / "distill_runs" / "latest" / "metrics.json").read_text(encoding="utf-8")
    )
    assert metrics["status"] == "failed"
    assert metrics["parse_failures"] == 1
    assert metrics["citation_failures"] == 1
    # Tuning fields are present on the failure path too (defaults when the
    # synthetic pipeline does not report counts).
    assert metrics["evidence_rule_rejections"] == 0
    assert metrics["fallback_selected"] == 0
    assert metrics["citation_resolution_rate"] == 1.0
    assert metrics["parse_failure_rate"] == 0.0
    assert metrics["category_distribution"] == {}
    assert not _git(repo, "ls-remote", "--heads", "origin", "release/*", check=False)


def _distill_clone(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    repo = tmp_path / "distill"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(tmp_path, "clone", str(remote), str(seed))
    _configure(seed)
    (seed / "prompts").mkdir()
    (seed / "prompts" / "distill_system.md").write_text("system", encoding="utf-8")
    (seed / "prompts" / "distill_user.md").write_text(
        "records {{ records_json }}",
        encoding="utf-8",
    )
    (seed / "bootstrap_state.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "compatible_code_range": ">=0.1.0,<0.2.0",
                "compatible_build_api_range": ">=1,<2",
                "manifest_schema_version": 1,
                "record_frontmatter_schema_version": 1,
                "product_frontmatter_schema_version": 1,
                "health_schema_version": 1,
                "owner_schema_version": 1,
            }
        ),
        encoding="utf-8",
    )
    (seed / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "memory-net/master-manifest/v1",
                "compatible_leaf_range": ">=0.1.0,<0.2.0",
                "products_active": {},
            }
        ),
        encoding="utf-8",
    )
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "bootstrap")
    _git(seed, "push", "origin", "master")
    _git(tmp_path, "clone", str(remote), str(repo))
    _configure(repo)
    return repo


def _add_leaf(
    repo: Path,
    node_id: str,
    memory_id: str,
    body: str,
    *,
    scanner_hits: dict[str, int] | None = None,
) -> None:
    branch = f"leaf/{node_id}"
    _git(repo, "switch", "--orphan", branch)
    _git(repo, "rm", "-rf", ".", check=False)
    memory_path = repo / "memories" / "common" / f"{node_id}.md"
    memory_path.parent.mkdir(parents=True)
    memory_path.write_text(
        "\n".join(
            [
                "---",
                "schema_version: memory-net/reported-memory/v1",
                f"memory_id: {memory_id}",
                f"source_locator: {node_id}:note.md",
                f"author_node: {node_id}",
                "author_adapter: test-adapter",
                "category_hint: common",
                "revision_hash: abc12345",
                "report_fingerprint: rpt-test",
                "redaction_pattern_version: redact-test",
                "adapter_config_revision: adapter-test",
                "created_at: '2026-04-25T00:00:00Z'",
                "updated_at: '2026-04-25T00:00:00Z'",
                "reported_at: '2026-04-25T00:00:00Z'",
                "scope: shared",
                "confidence: unknown",
                "source_type: agent_memory",
                "redaction_applied: []",
                "source_frontmatter: {}",
                "---",
                body,
                "",
            ]
        ),
        encoding="utf-8",
    )
    (repo / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "memory-net/leaf-manifest/v1",
                "node_id": node_id,
                "entries": {
                    memory_id: {
                        "status": "active",
                        "relative_path": memory_path.relative_to(repo).as_posix(),
                        "source_locator": f"{node_id}:note.md",
                        "revision_hash": "abc12345",
                        "report_fingerprint": "rpt-test",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (repo / ".health.json").write_text(
        json.dumps(_leaf_health(node_id, scanner_hits=scanner_hits)),
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", f"report {node_id}")
    _git(repo, "push", "origin", branch)
    _git(repo, "switch", "-C", "master", "origin/master")


def _add_invalid_leaf_manifest(repo: Path, node_id: str) -> None:
    branch = f"leaf/{node_id}"
    _git(repo, "switch", "--orphan", branch)
    _git(repo, "rm", "-rf", ".", check=False)
    (repo / "manifest.json").write_text(
        json.dumps({"schema_version": "invalid"}),
        encoding="utf-8",
    )
    _git(repo, "add", "manifest.json")
    _git(repo, "commit", "-m", f"bad leaf {node_id}")
    _git(repo, "push", "origin", branch)
    _git(repo, "switch", "-C", "master", "origin/master")


def _add_leaf_without_manifest(repo: Path, node_id: str) -> None:
    branch = f"leaf/{node_id}"
    _git(repo, "switch", "--orphan", branch)
    _git(repo, "rm", "-rf", ".", check=False)
    (repo / ".health.json").write_text(
        json.dumps(_leaf_health(node_id)),
        encoding="utf-8",
    )
    _git(repo, "add", ".health.json")
    _git(repo, "commit", "-m", f"manifestless leaf {node_id}")
    _git(repo, "push", "origin", branch)
    _git(repo, "switch", "-C", "master", "origin/master")


def _add_leaf_record(repo: Path, node_id: str, memory_id: str, body: str) -> None:
    branch = f"leaf/{node_id}"
    _git(repo, "switch", branch)
    memory_path = repo / "memories" / "common" / f"{node_id}-second.md"
    _write_memory_file(
        memory_path,
        node_id=node_id,
        memory_id=memory_id,
        body=body,
        source_locator=f"{node_id}:second.md",
    )
    manifest = json.loads((repo / "manifest.json").read_text(encoding="utf-8"))
    manifest["entries"][memory_id] = {
        "status": "active",
        "relative_path": memory_path.relative_to(repo).as_posix(),
        "source_locator": f"{node_id}:second.md",
        "revision_hash": "abc12345",
        "report_fingerprint": "rpt-test",
    }
    (repo / "manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", f"report {node_id} extra")
    _git(repo, "push", "origin", branch)
    _git(repo, "switch", "-C", "master", "origin/master")


def _add_missing_leaf_record(repo: Path, node_id: str, memory_id: str) -> None:
    branch = f"leaf/{node_id}"
    _git(repo, "switch", branch)
    manifest = json.loads((repo / "manifest.json").read_text(encoding="utf-8"))
    manifest["entries"][memory_id] = {
        "status": "active",
        "relative_path": f"memories/common/{node_id}-missing.md",
        "source_locator": f"{node_id}:missing.md",
        "revision_hash": "abc12345",
        "report_fingerprint": "rpt-test",
    }
    (repo / "manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    _git(repo, "add", "manifest.json")
    _git(repo, "commit", "-m", f"report {node_id} missing")
    _git(repo, "push", "origin", branch)
    _git(repo, "switch", "-C", "master", "origin/master")


def _add_empty_leaf(
    repo: Path,
    node_id: str,
    *,
    scanner_hits: dict[str, int] | None = None,
) -> None:
    branch = f"leaf/{node_id}"
    _git(repo, "switch", "--orphan", branch)
    _git(repo, "rm", "-rf", ".", check=False)
    (repo / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "memory-net/leaf-manifest/v1",
                "node_id": node_id,
                "entries": {},
            }
        ),
        encoding="utf-8",
    )
    (repo / ".health.json").write_text(
        json.dumps(_leaf_health(node_id, scanner_hits=scanner_hits)),
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", f"empty leaf {node_id}")
    _git(repo, "push", "origin", branch)
    _git(repo, "switch", "-C", "master", "origin/master")


def _replace_leaf_health(repo: Path, node_id: str, health: dict[str, Any]) -> None:
    branch = f"leaf/{node_id}"
    _git(repo, "switch", branch)
    (repo / ".health.json").write_text(json.dumps(health), encoding="utf-8")
    _git(repo, "add", ".health.json")
    _git(repo, "commit", "-m", f"replace health {node_id}")
    _git(repo, "push", "origin", branch)
    _git(repo, "switch", "-C", "master", "origin/master")


def _leaf_health(
    node_id: str,
    *,
    scanner_hits: dict[str, int] | None = None,
) -> dict[str, Any]:
    return build_health(
        node_id=node_id,
        redaction_incidents={"scanner_hits": scanner_hits or {}},
    )


def _write_memory_file(
    path: Path,
    *,
    node_id: str,
    memory_id: str,
    body: str,
    source_locator: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "---",
                "schema_version: memory-net/reported-memory/v1",
                f"memory_id: {memory_id}",
                f"source_locator: {source_locator}",
                f"author_node: {node_id}",
                "author_adapter: test-adapter",
                "category_hint: common",
                "revision_hash: abc12345",
                "report_fingerprint: rpt-test",
                "redaction_pattern_version: redact-test",
                "adapter_config_revision: adapter-test",
                "created_at: '2026-04-25T00:00:00Z'",
                "updated_at: '2026-04-25T00:00:00Z'",
                "reported_at: '2026-04-25T00:00:00Z'",
                "scope: shared",
                "confidence: unknown",
                "source_type: agent_memory",
                "redaction_applied: []",
                "source_frontmatter: {}",
                "---",
                body,
                "",
            ]
        ),
        encoding="utf-8",
    )


def _orchestrator(
    repo: Path,
    *,
    dry_run: bool = False,
    no_push: bool = False,
    publish_branch_template: str | None = None,
    frozen_master_snapshot_sha: str | None = None,
    pipeline: object | None = None,
    record_bundle_dir: Path | None = None,
    replay_mode: str = "live",
) -> DistillOrchestrator:
    active_pipeline = pipeline or DistillationPipeline(
        provider=MockLLMProvider(),
        system_prompt_path=repo / "prompts" / "distill_system.md",
        user_prompt_path=repo / "prompts" / "distill_user.md",
    )
    return DistillOrchestrator(
        repo_path=repo,
        pipeline=active_pipeline,  # type: ignore[arg-type]
        dry_run=dry_run,
        no_push=no_push,
        publish_branch_template=publish_branch_template,
        frozen_master_snapshot_sha=frozen_master_snapshot_sha,
        record_bundle_dir=record_bundle_dir,
        replay_mode=replay_mode,
    )


class FailingPipeline:
    def __init__(self) -> None:
        self.system_prompt_path = Path("prompts/distill_system.md")
        self.user_prompt_path = Path("prompts/distill_user.md")

    def run(self, records: list[dict[str, Any]], *, run_id: str) -> object:
        raise RuntimeError("pipeline boom")


class GateFailingPipeline:
    def __init__(self) -> None:
        self.system_prompt_path = Path("prompts/distill_system.md")
        self.user_prompt_path = Path("prompts/distill_user.md")

    def run(self, records: list[dict[str, Any]], *, run_id: str) -> DistillationOutput:
        return DistillationOutput(
            products=[
                TeamProduct(
                    relative_path=Path("team/common/gated.md"),
                    content="---\n"
                    "schema_version: memory-net/team-product/v1\n"
                    f"distillation_id: distill-{run_id}-001\n"
                    "category: common\n"
                    "---\n# Gated\n",
                    frontmatter={
                        "schema_version": "memory-net/team-product/v1",
                        "distillation_id": f"distill-{run_id}-001",
                        "category": "common",
                        "citations": [],
                        "contributors": [],
                        "conflicts": [],
                    },
                    category="common",
                    distillation_id=f"distill-{run_id}-001",
                )
            ],
            response=LLMResponse(
                text="{}",
                model="mock",
                usage={"input_tokens": 10, "output_tokens": 5},
            ),
            skipped_clusters=1,
            estimated_cost_usd=0.001,
            cluster_assignments=[{"cluster_id": 0, "memory_ids": [], "size": 1}],
            parse_failures=1,
            citation_failures=1,
        )


def _configure(repo: Path) -> None:
    _git(repo, "config", "user.email", "memory-net@example.invalid")
    _git(repo, "config", "user.name", "memory-net")


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and proc.returncode != 0:
        raise AssertionError(proc.stderr)
    return proc.stdout.strip()
