import json
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

import memory_net.leaf.reporter as reporter_module
from memory_net.leaf.cli import (
    _apply_subscribe_config,
    _health_commit_due,
    _write_push_failure_evidence,
)
from memory_net.leaf.health import build_health
from memory_net.leaf.reporter import RedactionBlockedError, report
from memory_net.leaf.subscriber import subscribe
from memory_net.shared.adapters import FileSystemMdAdapter
from memory_net.shared.git_io import GitRepo
from memory_net.shared.redaction import null_redactor
from memory_net.shared.schema import (
    SchemaError,
    validate_health,
    validate_leaf_manifest,
    validate_reported_memory_frontmatter,
)
from memory_net.shared.util.frontmatter import split_frontmatter
from memory_net.shared.util.hashing import short_hash


def test_report_and_subscribe_round_trip(tmp_path: Path) -> None:
    agent_root = tmp_path / "agent"
    shared = agent_root / "memory" / "shared"
    shared.mkdir(parents=True)
    (shared / "note.md").write_text("hello secret@example.com", encoding="utf-8")
    adapter_config = tmp_path / "adapter.yaml"
    adapter_config.write_text(
        """
name: claude-code
roots:
  shared:
    - alias: shared
      base_dir: "${AGENT_ROOT}/memory/shared"
      include: ["**/*.md"]
team_target: "${AGENT_ROOT}/memory/team"
file_pattern: "*.md"
""",
        encoding="utf-8",
    )
    adapter = FileSystemMdAdapter.from_yaml(adapter_config, agent_root=agent_root)
    leaf_repo = tmp_path / "leaf"
    leaf_repo.mkdir()

    result = report(
        repo_path=leaf_repo,
        node_id="alice-01",
        adapter=adapter,
        redactor=null_redactor(),
    )

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert len(manifest["entries"]) == 1
    entry = next(iter(manifest["entries"].values()))
    reported = (leaf_repo / entry["relative_path"]).read_text(encoding="utf-8")
    frontmatter, _body = split_frontmatter(reported)
    validate_reported_memory_frontmatter(frontmatter)

    master = tmp_path / "master"
    product = master / "team" / "common" / "note.md"
    product.parent.mkdir(parents=True)
    product.write_text(
        (
            "---\n"
            "schema_version: memory-net/team-product/v1\n"
            "distillation_id: d1\n"
            "category: common\n"
            "---\n"
            "# Note"
        ),
        encoding="utf-8",
    )
    (master / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "memory-net/master-manifest/v1",
                "compatible_leaf_range": ">=0.1.0,<0.2.0",
                "products_active": {
                    "d1": {
                        "relative_path": "team/common/note.md",
                        "category": "common",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    sub_result = subscribe(
        repo_path=master,
        adapter=adapter,
        config_revision="cfg",
        state_dir=tmp_path / "state",
    )

    assert sub_result.products_written == 1
    assert (agent_root / "memory" / "team" / "INDEX.md").exists()


def test_leaf_manifest_rejects_unsupported_schema_version() -> None:
    with pytest.raises(SchemaError, match="unsupported leaf manifest schema_version"):
        validate_leaf_manifest(
            {
                "schema_version": "memory-net/leaf-manifest/v2",
                "node_id": "alice",
                "entries": {},
            }
        )


def test_reported_memory_rejects_unsupported_schema_version() -> None:
    frontmatter = _reported_memory_frontmatter()
    frontmatter["schema_version"] = "memory-net/reported-memory/v2"

    with pytest.raises(SchemaError, match="unsupported reported memory schema_version"):
        validate_reported_memory_frontmatter(frontmatter)


def test_health_rejects_unsupported_schema_and_bad_scanner_counts() -> None:
    health = build_health(node_id="alice")
    health["schema_version"] = "memory-net/health/v2"
    with pytest.raises(SchemaError, match="unsupported health schema_version"):
        validate_health(health)

    health = build_health(node_id="alice")
    health["redaction_incidents"]["scanner_hits"]["builtin"] = "bad"
    with pytest.raises(SchemaError, match=r"scanner_hits\[builtin\]"):
        validate_health(health)


def test_health_schema_requires_published_fields_and_validates_example() -> None:
    health = build_health(
        node_id="alice",
        config_revision="cfg",
        last_report_at="2026-04-20T09:30:00Z",
        last_pull_at="2026-04-20T09:35:00Z",
        redaction_pattern_version="1",
        extra={"memory_count": 0},
    )

    validate_health(health)
    assert health["role"] == "leaf"
    assert health["published_at"]
    assert health["last_heartbeat_at"]

    del health["published_at"]
    with pytest.raises(SchemaError, match="missing required key: published_at"):
        validate_health(health)


def test_report_rewrites_revision_paths_and_tombstones(
    tmp_path: Path,
) -> None:
    adapter, agent_root = _adapter(tmp_path)
    shared_note = agent_root / "memory" / "shared" / "note.md"
    shared_note.write_text("first version", encoding="utf-8")
    leaf_repo = tmp_path / "leaf"
    leaf_repo.mkdir()

    first = report(
        repo_path=leaf_repo,
        node_id="alice-01",
        adapter=adapter,
        redactor=null_redactor(),
    )
    first_manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    memory_id = next(iter(first_manifest["entries"]))
    first_path = leaf_repo / first_manifest["entries"][memory_id]["relative_path"]
    assert first_path.exists()

    shared_note.write_text("second version", encoding="utf-8")
    second = report(
        repo_path=leaf_repo,
        node_id="alice-01",
        adapter=adapter,
        redactor=null_redactor(),
    )
    second_manifest = json.loads(second.manifest_path.read_text(encoding="utf-8"))
    second_path = leaf_repo / second_manifest["entries"][memory_id]["relative_path"]
    assert second_path.exists()
    assert second_path != first_path
    assert not first_path.exists()

    shared_note.unlink()
    report(
        repo_path=leaf_repo,
        node_id="alice-01",
        adapter=adapter,
        redactor=null_redactor(),
    )
    final_manifest = json.loads(
        (leaf_repo / "manifest.json").read_text(encoding="utf-8")
    )
    assert final_manifest["entries"] == {}
    assert memory_id in final_manifest["tombstones"]
    assert not second_path.exists()


def test_report_rejects_manifest_delete_outside_memories(tmp_path: Path) -> None:
    adapter, agent_root = _adapter(tmp_path)
    shared_note = agent_root / "memory" / "shared" / "note.md"
    shared_note.write_text("first version", encoding="utf-8")
    leaf_repo = tmp_path / "leaf"
    leaf_repo.mkdir()
    (leaf_repo / ".health.json").write_text("{}", encoding="utf-8")
    (leaf_repo / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "memory-net/leaf-manifest/v1",
                "node_id": "alice-01",
                "entries": {
                    "leaf-alice-01-mem-old": {
                        "status": "active",
                        "relative_path": ".health.json",
                        "source_locator": "shared:old.md",
                        "revision_hash": "old",
                        "report_fingerprint": "old",
                    }
                },
                "tombstones": {},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="under memories"):
        report(
            repo_path=leaf_repo,
            node_id="alice-01",
            adapter=adapter,
            redactor=null_redactor(),
        )

    assert (leaf_repo / ".health.json").exists()


def test_report_dry_run_does_not_count_real_writes(tmp_path: Path) -> None:
    adapter, agent_root = _adapter(tmp_path)
    (agent_root / "memory" / "shared" / "note.md").write_text(
        "hello",
        encoding="utf-8",
    )
    leaf_repo = tmp_path / "leaf"
    leaf_repo.mkdir()

    result = report(
        repo_path=leaf_repo,
        node_id="alice-01",
        adapter=adapter,
        redactor=null_redactor(),
        dry_run=True,
    )

    assert result.records_seen == 1
    assert result.records_written == 0
    assert not (leaf_repo / "manifest.json").exists()


def test_report_writes_scanner_audit_and_health_counts(tmp_path: Path) -> None:
    adapter, agent_root = _adapter(tmp_path)
    (agent_root / "memory" / "shared" / "note.md").write_text(
        "api_key=abcdefghijklmnopqrstuvwxyz123456",
        encoding="utf-8",
    )
    leaf_repo = tmp_path / "leaf"
    state_dir = tmp_path / "state"

    result = report(
        repo_path=leaf_repo,
        node_id="alice-01",
        adapter=adapter,
        redactor=null_redactor(),
        scanner_config={"builtin": {"enabled": True}},
        state_dir=state_dir,
    )

    health = json.loads((leaf_repo / ".health.json").read_text(encoding="utf-8"))
    audit_files = list((state_dir / "scanner_audit").glob("*.jsonl"))
    assert result.scanner_hits == {"builtin": 1}
    assert health["redaction_incidents"]["scanner_hits"]["builtin"] == 1
    assert audit_files
    audit = json.loads(audit_files[0].read_text(encoding="utf-8").splitlines()[0])
    assert audit["rule"] == "generic_secret_assignment"
    assert "abcdefghijklmnopqrstuvwxyz" not in json.dumps(audit)


def test_report_blocks_before_write_when_scanner_mode_is_block(tmp_path: Path) -> None:
    adapter, agent_root = _adapter(tmp_path)
    (agent_root / "memory" / "shared" / "note.md").write_text(
        "api_key=abcdefghijklmnopqrstuvwxyz123456",
        encoding="utf-8",
    )
    leaf_repo = tmp_path / "leaf"
    state_dir = tmp_path / "state"

    with pytest.raises(RedactionBlockedError, match="external scanner blocked"):
        report(
            repo_path=leaf_repo,
            node_id="alice-01",
            adapter=adapter,
            redactor=null_redactor(),
            scanner_config={"builtin": {"enabled": True, "mode": "block"}},
            state_dir=state_dir,
        )

    health = json.loads((leaf_repo / ".health.json").read_text(encoding="utf-8"))
    assert not (leaf_repo / "manifest.json").exists()
    assert not (leaf_repo / "memories").exists()
    assert health["status"] == "degraded"
    assert "external scanner blocked" in health["recent_errors"][0]
    assert health["redaction_incidents"]["blocked_reports"] == 1
    assert health["redaction_incidents"]["scanner_hits"]["builtin"] == 1
    assert list((state_dir / "scanner_audit").glob("*.jsonl"))


def test_report_rewrite_keeps_old_payload_when_new_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, agent_root = _adapter(tmp_path)
    note = agent_root / "memory" / "shared" / "note.md"
    note.write_text("first version\n", encoding="utf-8")
    leaf_repo = tmp_path / "leaf"

    report(
        repo_path=leaf_repo,
        node_id="alice-01",
        adapter=adapter,
        redactor=null_redactor(),
    )
    before_manifest = json.loads(
        (leaf_repo / "manifest.json").read_text(encoding="utf-8")
    )
    old_entry = next(iter(before_manifest["entries"].values()))
    old_relative_path = old_entry["relative_path"]

    note.write_text("second version\n", encoding="utf-8")
    monkeypatch.setattr(
        reporter_module,
        "_write_reported_record",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(OSError, match="disk full"):
        report(
            repo_path=leaf_repo,
            node_id="alice-01",
            adapter=adapter,
            redactor=null_redactor(),
        )

    after_manifest = json.loads(
        (leaf_repo / "manifest.json").read_text(encoding="utf-8")
    )
    after_entry = next(iter(after_manifest["entries"].values()))
    assert after_entry["relative_path"] == old_relative_path
    assert (leaf_repo / old_relative_path).exists()


def test_report_marks_health_degraded_when_scanner_mode_is_warn(
    tmp_path: Path,
) -> None:
    adapter, agent_root = _adapter(tmp_path)
    (agent_root / "memory" / "shared" / "note.md").write_text(
        "api_key=abcdefghijklmnopqrstuvwxyz123456",
        encoding="utf-8",
    )
    leaf_repo = tmp_path / "leaf"

    report(
        repo_path=leaf_repo,
        node_id="alice-01",
        adapter=adapter,
        redactor=null_redactor(),
        scanner_config={"builtin": {"enabled": True, "mode": "warn"}},
        state_dir=tmp_path / "state",
    )

    health = json.loads((leaf_repo / ".health.json").read_text(encoding="utf-8"))
    assert health["status"] == "degraded"
    assert "external scanner found" in health["recent_errors"][0]


def test_subscribe_rejects_paths_that_escape_team_target(tmp_path: Path) -> None:
    adapter, agent_root = _adapter(tmp_path)
    master = tmp_path / "master"
    master.mkdir()
    (master / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "memory-net/master-manifest/v1",
                "compatible_leaf_range": ">=0.1.0,<0.2.0",
                "products_active": {
                    "bad": {
                        "relative_path": "team/../../CLAUDE.md",
                        "category": "common",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        subscribe(repo_path=master, adapter=adapter, config_revision="cfg")

    assert not (agent_root / "CLAUDE.md").exists()


def test_subscribe_target_dir_override_must_stay_under_allowed_roots(
    tmp_path: Path,
) -> None:
    adapter, agent_root = _adapter(tmp_path)
    state_dir = tmp_path / "state"
    config_current = tmp_path / "config-current"
    config_current.mkdir()
    (config_current / "subscribe.yaml").write_text(
        "target_dir: /var/tmp/memory-net-team\n",
        encoding="utf-8",
    )
    args = Namespace(
        repo=tmp_path / "repo",
        state_dir=state_dir,
        agent_root=agent_root,
    )

    with pytest.raises(ValueError, match="target_dir"):
        _apply_subscribe_config(args, adapter)


def test_health_commit_not_due_without_prior_state(tmp_path: Path) -> None:
    assert _health_commit_due(tmp_path) is False


@pytest.mark.parametrize(
    "corrupt",
    [
        "not-a-timestamp",
        # Parses in 3.11+ ('Z' is accepted as an arbitrary date/time separator)
        # but yields a *naive* datetime.
        "2026-01-01Z00:00:00",
        "2026-01-01T00:00:00",
        "",
    ],
)
def test_health_commit_survives_a_corrupt_state_timestamp(
    tmp_path: Path, corrupt: str
) -> None:
    """A naive or unparsable stamp must read as "no known time", not crash.

    _health_commit_due subtracts the parsed value from an aware datetime.now,
    so letting a naive datetime through would raise TypeError and take the
    whole daemon tick down over one corrupt state file.
    """
    (tmp_path / "last_health_commit_at").write_text(corrupt + "\n", encoding="utf-8")

    assert _health_commit_due(tmp_path) is False


def test_health_commit_is_due_after_a_stale_aware_timestamp(tmp_path: Path) -> None:
    """Guard the other side: a valid stamp is still parsed and compared."""
    (tmp_path / "last_health_commit_at").write_text(
        "2000-01-01T00:00:00Z\n", encoding="utf-8"
    )

    assert _health_commit_due(tmp_path) is True


def test_subscribe_rejects_product_content_hash_mismatch(tmp_path: Path) -> None:
    adapter, _agent_root = _adapter(tmp_path)
    master = tmp_path / "master"
    product = master / "team" / "common" / "note.md"
    product.parent.mkdir(parents=True)
    content = (
        "---\n"
        "schema_version: memory-net/team-product/v1\n"
        "distillation_id: d1\n"
        "category: common\n"
        "---\n"
        "# Note"
    )
    product.write_text(content, encoding="utf-8")
    (master / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "memory-net/master-manifest/v1",
                "compatible_leaf_range": ">=0.1.0,<0.2.0",
                "products_active": {
                    "d1": {
                        "relative_path": "team/common/note.md",
                        "category": "common",
                        "content_hash": short_hash("different", 16),
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="content_hash mismatch"):
        subscribe(repo_path=master, adapter=adapter, config_revision="cfg")


def test_subscribe_rejects_incompatible_product_schema_before_replace(
    tmp_path: Path,
) -> None:
    adapter, agent_root = _adapter(tmp_path)
    existing = agent_root / "memory" / "team" / "old.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("keep\n", encoding="utf-8")
    master = tmp_path / "master"
    product = master / "team" / "common" / "note.md"
    product.parent.mkdir(parents=True)
    product.write_text(
        (
            "---\n"
            "schema_version: memory-net/team-product/v2\n"
            "distillation_id: d1\n"
            "category: common\n"
            "---\n"
            "# Note"
        ),
        encoding="utf-8",
    )
    (master / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "memory-net/master-manifest/v1",
                "compatible_leaf_range": ">=0.1.0,<0.2.0",
                "products_active": {
                    "d1": {
                        "relative_path": "team/common/note.md",
                        "category": "common",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsupported team product schema_version"):
        subscribe(repo_path=master, adapter=adapter, config_revision="cfg")

    assert existing.read_text(encoding="utf-8") == "keep\n"


def test_subscribe_dry_run_does_not_write_team_or_state(tmp_path: Path) -> None:
    adapter, agent_root = _adapter(tmp_path)
    master = tmp_path / "master"
    product = master / "team" / "common" / "note.md"
    product.parent.mkdir(parents=True)
    product.write_text(
        (
            "---\n"
            "schema_version: memory-net/team-product/v1\n"
            "distillation_id: d1\n"
            "category: common\n"
            "---\n"
            "# Note"
        ),
        encoding="utf-8",
    )
    (master / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "memory-net/master-manifest/v1",
                "compatible_leaf_range": ">=0.1.0,<0.2.0",
                "products_active": {
                    "d1": {
                        "relative_path": "team/common/note.md",
                        "category": "common",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    state_dir = tmp_path / "state"

    result = subscribe(
        repo_path=master,
        adapter=adapter,
        config_revision="cfg",
        state_dir=state_dir,
        dry_run=True,
    )

    assert result.products_written == 0
    assert not (agent_root / "memory" / "team").exists()
    assert not (state_dir / "last_pull.json").exists()


def test_push_failure_evidence_backs_up_and_resets_leaf_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("MEMORY_NET_HOME", str(home))
    monkeypatch.setattr("memory_net.leaf.cli._utc_now_safe", lambda: "20260506T170000Z")
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    other = tmp_path / "other"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(tmp_path, "clone", str(remote), str(repo))
    _configure_git(repo)
    _git(repo, "switch", "--orphan", "leaf/alice")
    (repo / "memories").mkdir()
    (repo / "memories" / "note.md").write_text("base\n", encoding="utf-8")
    (repo / "manifest.json").write_text("{}\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    _git(repo, "push", "origin", "leaf/alice")

    (repo / "memories" / "note.md").write_text("local\n", encoding="utf-8")
    _git(repo, "add", "memories/note.md")
    _git(repo, "commit", "-m", "local report")
    (repo / "untracked.txt").write_text("local untracked\n", encoding="utf-8")

    _git(tmp_path, "clone", str(remote), str(other))
    _configure_git(other)
    _git(other, "switch", "leaf/alice")
    (other / "memories" / "note.md").write_text("remote\n", encoding="utf-8")
    _git(other, "add", "memories/note.md")
    _git(other, "commit", "-m", "remote report")
    _git(other, "push", "origin", "leaf/alice")

    _write_push_failure_evidence(GitRepo(repo), "alice", "non-fast-forward")

    evidence_root = home / "state" / "rejected_commits"
    evidence = next(evidence_root.iterdir())
    metadata = json.loads((evidence / "metadata.json").read_text(encoding="utf-8"))
    health = json.loads((home / "state" / "last_health.json").read_text())
    assert (evidence / "commits.patch").read_text(encoding="utf-8").strip()
    assert (evidence / "worktree.tar.gz").stat().st_size > 0
    assert (evidence / "redacted" / "note.md").exists()
    assert health["status"] == "degraded"
    assert "push rejected" in health["recent_errors"][0]
    assert health["push_failure_evidence"] == str(evidence)
    assert _git(repo, "status", "--porcelain") == ""
    assert _git(repo, "rev-parse", "HEAD") == _git(
        repo,
        "rev-parse",
        "origin/leaf/alice",
    )

    _write_push_failure_evidence(GitRepo(repo), "alice", "non-fast-forward again")

    evidence_dirs = sorted(path.name for path in evidence_root.iterdir())
    backup_branches = [
        line.strip().lstrip("*").strip()
        for line in _git(
            repo, "branch", "--list", "backup/rejected-alice-*"
        ).splitlines()
        if line.strip()
    ]
    assert len(evidence_dirs) == 2
    assert len(backup_branches) == 2
    assert len(set(evidence_dirs)) == 2
    assert len(set(backup_branches)) == 2
    assert _git(repo, "branch", "--list", metadata["backup_branch"])


def _adapter(tmp_path: Path) -> tuple[FileSystemMdAdapter, Path]:
    agent_root = tmp_path / "agent"
    shared = agent_root / "memory" / "shared"
    shared.mkdir(parents=True)
    adapter_config = tmp_path / "adapter.yaml"
    adapter_config.write_text(
        """
name: claude-code
roots:
  shared:
    - alias: shared
      base_dir: "${AGENT_ROOT}/memory/shared"
      include: ["**/*.md"]
team_target: "${AGENT_ROOT}/memory/team"
file_pattern: "*.md"
""",
        encoding="utf-8",
    )
    return (
        FileSystemMdAdapter.from_yaml(adapter_config, agent_root=agent_root),
        agent_root,
    )


def _reported_memory_frontmatter() -> dict[str, object]:
    return {
        "schema_version": "memory-net/reported-memory/v1",
        "memory_id": "leaf-alice-mem-1",
        "source_locator": "shared:note.md",
        "author_node": "alice",
        "author_adapter": "test-adapter",
        "category_hint": "common",
        "revision_hash": "abc12345",
        "report_fingerprint": "rpt-test",
        "redaction_pattern_version": "redact-test",
        "adapter_config_revision": "adapter-test",
        "created_at": "2026-04-25T00:00:00Z",
        "updated_at": "2026-04-25T00:00:00Z",
        "reported_at": "2026-04-25T00:00:00Z",
        "scope": "shared",
        "confidence": "unknown",
        "source_type": "agent_memory",
        "redaction_applied": [],
        "source_frontmatter": {},
    }


def _configure_git(repo: Path) -> None:
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()
