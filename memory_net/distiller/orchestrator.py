from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import uuid
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from memory_net.distiller.metrics import (
    DistillMetrics,
    citation_resolution_rate,
    parse_failure_rate,
)
from memory_net.distiller.pipeline import DistillationOutput, DistillationPipeline
from memory_net.distiller.provenance import RunProvenance
from memory_net.distiller.runctx import RunContext, RunIdentity
from memory_net.shared.git_io import GitError, GitRepo
from memory_net.shared.paths import (
    MASTER_ALLOWED_EXACT_PATHS,
    MASTER_ALLOWED_PREFIXES,
    TEAM_PREFIX,
)
from memory_net.shared.schema import (
    validate_health,
    validate_leaf_manifest_entry,
    validate_leaf_manifest_header,
    validate_reported_memory_frontmatter,
)
from memory_net.shared.util.frontmatter import split_frontmatter
from memory_net.shared.util.hashing import hash_path_bytes, short_hash
from memory_net.shared.util.paths import (
    safe_posix_relative_path,
)


@dataclass(frozen=True)
class OrchestratorResult:
    run_id: str
    input_records: int
    products: int
    manifest_path: Path


class SupersededRunError(RuntimeError):
    pass


class DistillGateError(RuntimeError):
    pass


class DistillOrchestrator:
    def __init__(
        self,
        *,
        repo_path: Path,
        pipeline: DistillationPipeline,
        dry_run: bool = False,
        no_push: bool = False,
        publish_branch_template: str | None = None,
        frozen_master_snapshot_sha: str | None = None,
        record_bundle_dir: Path | None = None,
        replay_mode: str = "live",
        context: RunContext | None = None,
    ) -> None:
        self.repo_path = repo_path
        self.pipeline = pipeline
        self.dry_run = dry_run
        self.no_push = no_push
        self.publish_branch_template = publish_branch_template
        self.frozen_master_snapshot_sha = frozen_master_snapshot_sha
        # External run-scoped record bundle dir (RECORD-001); never under the
        # master path whitelist. replay_mode labels this run for metrics.json.
        self.record_bundle_dir = record_bundle_dir
        self.replay_mode = replay_mode
        # The single injection carrier (§5.5.3). None keeps the pre-injection
        # behaviour: a freshly generated run id and the wall clock.
        self.context = context
        # Resolved lazily, per run: minting one here would burn a run id at
        # construction time, which is not what the pre-injection code did.
        self._identity: RunIdentity | None = None
        self.repo = GitRepo(repo_path, dry_run=dry_run, no_push=no_push)
        self.leaf_refs: dict[str, str] = {}
        self.leaf_health_by_branch: dict[str, dict[str, Any]] = {}
        self.id_collisions = 0
        self.incompatible_leaves = 0
        self.quarantined_records = 0

    def _resolve_identity(self) -> RunIdentity:
        """The run's id and clock: injected if present, wall clock otherwise."""
        injected = self.context.identity if self.context is not None else None
        return injected if injected is not None else RunIdentity.system()

    def _now(self) -> str:
        """This run's clock. Never ``datetime.now`` directly (§5.5.3)."""
        if self._identity is None:
            self._identity = self._resolve_identity()
        return self._identity.now()

    def run(self) -> OrchestratorResult:
        started = time.monotonic()
        self.id_collisions = 0
        self.incompatible_leaves = 0
        self.quarantined_records = 0
        self.leaf_health_by_branch = {}
        self.repo.run("fetch", "--all", "--prune", check=False)
        if self.frozen_master_snapshot_sha:
            if self._local_master_ahead_remote():
                raise RuntimeError(
                    "local master has unpushed commits; refusing to reset"
                )
            snapshot_sha = self.frozen_master_snapshot_sha
            with self._frozen_snapshot_worktree(snapshot_sha):
                result = self._run_at_snapshot(started, snapshot_sha)
            return OrchestratorResult(
                run_id=result.run_id,
                input_records=result.input_records,
                products=result.products,
                manifest_path=self.repo_path / "manifest.json",
            )
        snapshot_sha = self._switch_master()
        return self._run_at_snapshot(started, snapshot_sha)

    def _run_at_snapshot(
        self,
        started: float,
        snapshot_sha: str,
    ) -> OrchestratorResult:
        # Resolved per run, not per instance: without an injected identity each
        # run must still get its own id, exactly as before injection existed.
        self._identity = self._resolve_identity()
        run_id = self._identity.run_id
        records: list[dict[str, Any]] = []
        # Bound before the try so a failure *after* distillation succeeded still
        # reports this run's real provenance. Anything else would contradict the
        # record_metrics.json already written from the same output.
        output: DistillationOutput | None = None
        try:
            records = self._load_leaf_records()
            previous_manifest = self._load_master_manifest()
            with self._snapshot_prompts(snapshot_sha):
                output = self.pipeline.run(records, run_id=run_id)
            self._write_record_bundle(
                run_id=run_id, snapshot_sha=snapshot_sha, output=output
            )
            gate_error = _distill_gate_error(output)
            if gate_error:
                if not self.dry_run:
                    self._write_failure_outputs(
                        run_id=run_id,
                        snapshot_sha=snapshot_sha,
                        records=records,
                        error=gate_error,
                        duration_seconds=time.monotonic() - started,
                        output=output,
                    )
                raise DistillGateError(gate_error)
            if self.dry_run:
                self._write_dry_run_outputs(
                    run_id=run_id,
                    snapshot_sha=snapshot_sha,
                    records=records,
                    output=output,
                    previous_manifest=previous_manifest,
                    duration_seconds=time.monotonic() - started,
                )
            else:
                self._write_distill_outputs(
                    run_id=run_id,
                    snapshot_sha=snapshot_sha,
                    records=records,
                    output=output,
                    previous_manifest=previous_manifest,
                    duration_seconds=time.monotonic() - started,
                )
                if not self.no_push:
                    self._publish_outputs(snapshot_sha=snapshot_sha, run_id=run_id)
        except DistillGateError:
            raise
        except Exception as exc:
            if not self.dry_run:
                self._write_failure_outputs(
                    run_id=run_id,
                    snapshot_sha=snapshot_sha,
                    records=records,
                    error=str(exc) or exc.__class__.__name__,
                    duration_seconds=time.monotonic() - started,
                    output=output,
                )
            raise
        finally:
            # Explicit, and in a finally: a reservation that never reached a
            # terminal state means an attempt's cost was silently dropped from
            # the run's ceiling. __del__ would be too late and too uncertain.
            self._assert_budget_settled()
        return OrchestratorResult(
            run_id=run_id,
            input_records=len(records),
            products=len(output.products),
            manifest_path=(
                self.repo_path / "_dryrun" / run_id / "manifest.json"
                if self.dry_run
                else self.repo_path / "manifest.json"
            ),
        )

    def _snapshot_payload(self, *, run_id: str, snapshot_sha: str) -> str:
        """The run's snapshot.json body, shared by the dry-run and live writers.

        The two used to carry duplicated literals, which is how one of them
        gains a field the other never does.
        """
        payload: dict[str, Any] = {
            "leaf_refs": self.leaf_refs,
            "master_snapshot_sha": snapshot_sha,
            "run_id": run_id,
        }
        payload.update(self._profile_provenance())
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"

    def _run_configuration_fields(self) -> dict[str, Any]:
        """Which profile ran, and whether the vectors were the stand-in.

        Written to metrics on the success *and* the failure path, since a failed
        run is exactly when someone needs to know what it was configured with.
        ``embedding_test_only`` is here because the stand-in leaves no other
        trace: its products validate and its metrics look ordinary, so without
        this field nothing distinguishes them from a real run's.
        """
        fields: dict[str, Any] = dict(self._profile_provenance())
        # Nested getattr: the pipeline is a collaborator, and a test double
        # standing in for it is not obliged to grow an embedder.
        embedder = getattr(self.pipeline, "embedder", None)
        policy = getattr(embedder, "policy", None)
        if policy is not None:
            fields["embedding_test_only"] = bool(policy.test_only)
        return fields

    def _profile_provenance(self) -> dict[str, str]:
        """Which profile bytes this run used, or nothing when none was injected.

        Absent rather than null-valued: a run that predates profile provenance,
        or one with no profile file at all, must produce the same artifact bytes
        it always did rather than three keys describing nothing.

        ``master_snapshot_sha`` is deliberately not taken from here -- the
        provenance carries its own copy and the caller already has the run's,
        and letting the profile's copy win would silently rename what that field
        means depending on whether a profile was injected.
        """
        profile = self.context.profile if self.context is not None else None
        if profile is None:
            return {}
        as_dict = getattr(profile, "as_dict", None)
        if as_dict is None:  # pragma: no cover - defensive, see §5.5.6
            return {}
        fields = dict(as_dict())
        fields.pop("master_snapshot_sha", None)
        return fields

    def _assert_budget_settled(self) -> None:
        """Fail a *succeeding* run that leaked a budget reservation.

        Skipped while another exception is propagating. Raising out of a
        ``finally`` replaces the exception in flight, so on an already-failing
        run this would report a leaked reservation in place of the actual cause
        -- and change the exception's *type*, which callers match on
        (``DistillGateError`` is handled quite differently from a
        ``RuntimeError``). A run that is failing anyway is already loud; a run
        that reports success while an attempt's cost never landed on the ceiling
        is the silent case worth catching.
        """
        budget = self.context.budget if self.context is not None else None
        if budget is None or sys.exc_info()[0] is not None:
            return
        budget.assert_all_settled()

    def _write_dry_run_outputs(
        self,
        *,
        run_id: str,
        snapshot_sha: str,
        records: list[dict[str, Any]],
        output: DistillationOutput,
        previous_manifest: dict[str, Any],
        duration_seconds: float,
    ) -> None:
        root = self.repo_path / "_dryrun" / run_id
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)
        self._write_product_snapshot(root / TEAM_PREFIX, output.products)
        manifest = self._master_manifest(
            run_id=run_id,
            snapshot_sha=snapshot_sha,
            products=output.products,
            previous_manifest=previous_manifest,
        )
        (root / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self._distill_metrics(
            run_id=run_id,
            snapshot_sha=snapshot_sha,
            records=records,
            output=output,
            duration_seconds=duration_seconds,
            review_gate_status="dry_run",
            publish_branch="dry_run",
        ).write(root / "metrics.json")
        (root / "snapshot.json").write_text(
            self._snapshot_payload(run_id=run_id, snapshot_sha=snapshot_sha),
            encoding="utf-8",
        )

    def _write_distill_outputs(
        self,
        *,
        run_id: str,
        snapshot_sha: str,
        records: list[dict[str, Any]],
        output: DistillationOutput,
        previous_manifest: dict[str, Any],
        duration_seconds: float,
    ) -> None:
        self._write_team_products(output.products)
        manifest = self._master_manifest(
            run_id=run_id,
            snapshot_sha=snapshot_sha,
            products=output.products,
            previous_manifest=previous_manifest,
        )
        (self.repo_path / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self._write_health(run_id, len(records), len(output.products))
        metrics = self._distill_metrics(
            run_id=run_id,
            snapshot_sha=snapshot_sha,
            records=records,
            output=output,
            duration_seconds=duration_seconds,
        )
        metrics.write(self.repo_path / "distill_runs" / run_id / "metrics.json")
        metrics.write(self.repo_path / "distill_runs" / "latest" / "metrics.json")
        self._write_run_product_snapshot(run_id, output.products)
        self._write_clusters(run_id, output.cluster_assignments or [])
        (self.repo_path / "distill_runs" / run_id / "snapshot.json").write_text(
            self._snapshot_payload(run_id=run_id, snapshot_sha=snapshot_sha),
            encoding="utf-8",
        )

    def _distill_metrics(
        self,
        *,
        run_id: str,
        snapshot_sha: str,
        records: list[dict[str, Any]],
        output: DistillationOutput,
        duration_seconds: float,
        review_gate_status: str | None = None,
        publish_branch: str | None = None,
    ) -> DistillMetrics:
        return DistillMetrics(
            run_id=run_id,
            input_records=len(records),
            products=len(output.products),
            input_tokens=int(output.response.usage.get("input_tokens", 0)),
            output_tokens=int(output.response.usage.get("output_tokens", 0)),
            skipped_clusters=output.skipped_clusters,
            estimated_cost_usd=output.estimated_cost_usd,
            pre_publish_duration_seconds=round(duration_seconds, 3),
            clusters=len(output.cluster_assignments or []),
            fallback_attempts=output.fallback_attempts,
            parse_failures=output.parse_failures,
            parse_error_details=output.parse_error_details or [],
            citation_failures=output.citation_failures,
            config_revision=self._config_revision(snapshot_sha),
            master_snapshot_sha=snapshot_sha,
            id_collisions=self.id_collisions,
            incompatible_leaves=self.incompatible_leaves,
            quarantined_records=self.quarantined_records,
            scanner_audit_summary=_scanner_audit_summary(
                self.leaf_health_by_branch.values()
            ),
            review_gate_status=review_gate_status or self._review_gate_status(run_id),
            publish_branch=publish_branch or self._publish_branch(run_id),
            target_branch="master",
            clusters_attempted=output.clusters_attempted,
            parse_failure_rate=parse_failure_rate(
                output.parse_failures, output.clusters_attempted
            ),
            citations_emitted=output.citations_emitted,
            citations_resolved=output.citations_resolved,
            citation_resolution_rate=citation_resolution_rate(
                output.citations_resolved, output.citations_emitted
            ),
            fallback_selected=output.fallback_attempts,
            fallback_accepted=output.fallback_accepted,
            category_distribution=output.category_distribution or {},
            evidence_rule_rejections=output.evidence_rule_rejections,
            replay_mode=self.replay_mode,
            replay_hits=output.replay_hits,
            replay_misses=output.replay_misses,
            replay_live_calls=_replay_live_calls(output),
            provider_backends=output.provenance.provider_backends,
            usage_source=output.provenance.usage_source,
            usage_source_counts=output.provenance.usage_source_counts,
            **self._run_configuration_fields(),
        )

    def _write_failure_outputs(
        self,
        *,
        run_id: str,
        snapshot_sha: str,
        records: list[dict[str, Any]],
        error: str,
        duration_seconds: float,
        output: DistillationOutput | None = None,
    ) -> None:
        now = self._now()
        health = {
            "schema_version": "memory-net/health/v1",
            "node_id": "distiller",
            "role": "distiller",
            "status": "degraded",
            "last_heartbeat_at": now,
            "published_at": now,
            "last_distill_at": now,
            "run_id": run_id,
            "input_records": len(records),
            "products": 0,
            "recent_errors": [error],
            "redaction_incidents": {
                "blocked_reports": 0,
                "scanner_hits": {},
            },
        }
        (self.repo_path / ".health.json").write_text(
            json.dumps(health, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        config_revision = self._config_revision(snapshot_sha)
        # A failed run still discloses where whatever calls it did make were
        # accounted; with no output at all this is the zero-call view.
        provenance = output.provenance if output else RunProvenance.empty()
        metrics = DistillMetrics(
            run_id=run_id,
            input_records=len(records),
            products=0,
            input_tokens=(
                int(output.response.usage.get("input_tokens", 0)) if output else 0
            ),
            output_tokens=(
                int(output.response.usage.get("output_tokens", 0)) if output else 0
            ),
            skipped_clusters=output.skipped_clusters if output else 0,
            estimated_cost_usd=output.estimated_cost_usd if output else 0.0,
            pre_publish_duration_seconds=round(duration_seconds, 3),
            clusters=len(output.cluster_assignments or []) if output else 0,
            fallback_attempts=output.fallback_attempts if output else 0,
            parse_failures=output.parse_failures if output else 0,
            parse_error_details=(output.parse_error_details or []) if output else [],
            citation_failures=output.citation_failures if output else 0,
            config_revision=config_revision,
            master_snapshot_sha=snapshot_sha,
            id_collisions=self.id_collisions,
            incompatible_leaves=self.incompatible_leaves,
            quarantined_records=self.quarantined_records,
            scanner_audit_summary=_scanner_audit_summary(
                self.leaf_health_by_branch.values()
            ),
            review_gate_status=self._review_gate_status(run_id),
            publish_branch=self._publish_branch(run_id),
            target_branch="master",
            clusters_attempted=output.clusters_attempted if output else 0,
            parse_failure_rate=parse_failure_rate(
                output.parse_failures if output else 0,
                output.clusters_attempted if output else 0,
            ),
            citations_emitted=output.citations_emitted if output else 0,
            citations_resolved=output.citations_resolved if output else 0,
            citation_resolution_rate=citation_resolution_rate(
                output.citations_resolved if output else 0,
                output.citations_emitted if output else 0,
            ),
            fallback_selected=output.fallback_attempts if output else 0,
            fallback_accepted=output.fallback_accepted if output else 0,
            category_distribution=(
                (output.category_distribution or {}) if output else {}
            ),
            evidence_rule_rejections=output.evidence_rule_rejections if output else 0,
            replay_mode=self.replay_mode,
            replay_hits=output.replay_hits if output else 0,
            replay_misses=output.replay_misses if output else 0,
            replay_live_calls=_replay_live_calls(output) if output else 0,
            provider_backends=provenance.provider_backends,
            usage_source=provenance.usage_source,
            usage_source_counts=provenance.usage_source_counts,
            **self._run_configuration_fields(),
            status="failed",
            recent_errors=[error],
        )
        metrics.write(self.repo_path / "distill_runs" / run_id / "metrics.json")
        metrics.write(self.repo_path / "distill_runs" / "latest" / "metrics.json")

    def _publish_outputs(self, *, snapshot_sha: str, run_id: str) -> None:
        self._ensure_master_unchanged(snapshot_sha, run_id)
        self._enforce_master_path_whitelist(snapshot_sha)
        self.repo.run(
            "add",
            "team",
            "manifest.json",
            ".health.json",
            "distill_runs",
        )
        self._enforce_staged_path_whitelist(snapshot_sha)
        staged = self.repo.run(
            "diff",
            "--cached",
            "--quiet",
            check=False,
        ).returncode
        if staged == 0:
            return
        self.repo.run("commit", "-m", f"distill run {run_id}")
        self._ensure_master_unchanged(snapshot_sha, run_id)
        target_branch = self._publish_branch(run_id)
        current_branch = self.repo.run(
            "symbolic-ref",
            "--quiet",
            "--short",
            "HEAD",
            check=False,
        ).stdout.strip()
        try:
            if target_branch == "master":
                if current_branch == "master":
                    self.repo.run("push", "origin", "master")
                else:
                    self.repo.run("push", "origin", "HEAD:master")
            else:
                self.repo.run("push", "origin", f"HEAD:refs/heads/{target_branch}")
                self.repo.run("switch", "-C", target_branch)
                if current_branch == "master":
                    self.repo.run("branch", "-f", "master", snapshot_sha)
        except GitError as exc:
            evidence_root = self._snapshot_push_failure_evidence(
                run_id,
                snapshot_sha,
                str(exc),
            )
            self.repo.run("reset", "--hard", snapshot_sha, check=False)
            self._restore_push_failure_evidence(run_id, evidence_root)
            if _is_superseded_push_error(str(exc)):
                self._write_superseded(run_id, snapshot_sha, str(exc))
                raise SupersededRunError(
                    "master push failed after snapshot drift"
                ) from exc
            raise RuntimeError("transient master push failed") from exc

    def _publish_branch(self, run_id: str) -> str:
        if not self.publish_branch_template:
            return "master"
        return self.publish_branch_template.replace("<run_id>", run_id)

    def _review_gate_status(self, run_id: str) -> str:
        return "pending" if self._publish_branch(run_id) != "master" else "disabled"

    def _load_leaf_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        seen_ids: dict[str, str] = {}
        branches = self.repo.run("branch", "-r", check=False).stdout.splitlines()
        for raw in branches:
            branch = raw.strip()
            if not branch.startswith("origin/leaf/"):
                continue
            branch_sha = self.repo.current_sha(branch)
            self.leaf_refs[branch] = branch_sha
            try:
                branch_records = self._load_one_leaf_records(
                    branch=branch,
                    branch_sha=branch_sha,
                    seen_ids=seen_ids,
                )
            except (json.JSONDecodeError, KeyError, ValueError, GitError):
                self.incompatible_leaves += 1
                continue
            records.extend(branch_records)
        return records

    def _load_one_leaf_records(
        self,
        *,
        branch: str,
        branch_sha: str,
        seen_ids: dict[str, str],
    ) -> list[dict[str, Any]]:
        manifest_result = self.repo.run(
            "show",
            f"{branch_sha}:manifest.json",
            check=False,
        )
        if manifest_result.returncode != 0:
            raise GitError(f"leaf manifest missing or unreadable for {branch}")
        manifest_text = manifest_result.stdout
        if not manifest_text.strip():
            raise ValueError(f"leaf manifest empty for {branch}")
        manifest = json.loads(manifest_text)
        health = self._load_leaf_health(branch_sha)
        self.leaf_health_by_branch[branch] = health
        entries = validate_leaf_manifest_header(manifest)
        records: list[dict[str, Any]] = []
        for memory_id, entry in entries.items():
            try:
                record = self._load_one_leaf_record(
                    branch=branch,
                    branch_sha=branch_sha,
                    memory_id=str(memory_id),
                    entry=entry,
                    seen_ids=seen_ids,
                    node_id=str(manifest["node_id"]),
                    leaf_health=health,
                )
            except (KeyError, ValueError, RuntimeError, GitError):
                self.quarantined_records += 1
                continue
            if record is not None:
                records.append(record)
        return records

    def _load_one_leaf_record(
        self,
        *,
        branch: str,
        branch_sha: str,
        memory_id: str,
        entry: Any,
        seen_ids: dict[str, str],
        node_id: str,
        leaf_health: dict[str, Any],
    ) -> dict[str, Any] | None:
        validate_leaf_manifest_entry(memory_id, entry)
        if entry.get("status") != "active":
            return None
        rel = str(entry["relative_path"])
        content = self.repo.show_text(f"{branch_sha}:{rel}")
        frontmatter, body = split_frontmatter(content)
        validate_reported_memory_frontmatter(frontmatter)
        _validate_manifest_frontmatter_consistency(
            memory_id=memory_id,
            entry=entry,
            frontmatter=frontmatter,
            relative_path=rel,
        )
        if memory_id in seen_ids and seen_ids[memory_id] != branch:
            self.id_collisions += 1
            return None
        seen_ids[memory_id] = branch
        return _llm_record(
            memory_id=memory_id,
            entry=entry,
            frontmatter=frontmatter,
            body=body,
            content=content,
            leaf_branch=branch,
            branch_sha=branch_sha,
            node_id=node_id,
            relative_path=rel,
            leaf_health=leaf_health,
        )

    def _load_leaf_health(self, branch_sha: str) -> dict[str, Any]:
        health_result = self.repo.run(
            "show",
            f"{branch_sha}:.health.json",
            check=False,
        )
        if health_result.returncode != 0:
            raise GitError("leaf health missing or unreadable")
        health_text = health_result.stdout
        if not health_text.strip():
            raise ValueError("leaf health empty")
        value = json.loads(health_text)
        if not isinstance(value, dict):
            raise ValueError("leaf health must be object")
        validate_health(value)
        return value

    def _load_master_manifest(self) -> dict[str, Any]:
        path = self.repo_path / "manifest.json"
        if not path.exists():
            return {}
        return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))

    def _config_revision(self, snapshot_sha: str) -> str:
        result = self.repo.run(
            "ls-tree",
            "-r",
            "--name-only",
            snapshot_sha,
            "bootstrap_state.json",
            "config",
            "prompts",
            check=False,
        )
        if result.returncode != 0:
            return short_hash(snapshot_sha, 12)
        pairs: list[tuple[Path, bytes]] = []
        for raw in sorted(line for line in result.stdout.splitlines() if line.strip()):
            rel = Path(raw)
            if rel == Path("bootstrap_state.json"):
                canonical = rel
            elif rel.parts and rel.parts[0] == "config":
                canonical = Path(*rel.parts[1:])
            elif rel.parts and rel.parts[0] == "prompts":
                canonical = rel
            else:
                continue
            pairs.append((canonical, self.repo.show_bytes(f"{snapshot_sha}:{raw}")))
        return hash_path_bytes(pairs)

    def _master_manifest(
        self,
        *,
        run_id: str,
        snapshot_sha: str,
        products: list[Any],
        previous_manifest: dict[str, Any],
    ) -> dict[str, Any]:
        products_active = {
            product.distillation_id: self._product_manifest_entry(product, run_id)
            for product in products
        }
        previous_active = _manifest_products_mapping(
            previous_manifest.get("products_active") or {}
        )
        previous_tombstones = _manifest_products_mapping(
            previous_manifest.get("products_tombstones") or {}
        )
        now = self._now()
        products_tombstones = previous_tombstones | {
            distillation_id: {
                "deleted_at": now,
                "last_relative_path": entry.get("relative_path"),
                "last_topic_id": entry.get("topic_id"),
                "last_category": entry.get("category"),
                "snapshot_ref": entry.get("snapshot_ref"),
                "contributors": entry.get("contributors", []),
                "cited_memories": entry.get("citations", []),
            }
            for distillation_id, entry in previous_active.items()
            if distillation_id not in products_active
        }
        products_tombstones = {
            distillation_id: entry
            for distillation_id, entry in products_tombstones.items()
            if distillation_id not in products_active
        }
        return {
            "schema_version": "memory-net/master-manifest/v1",
            "compatible_leaf_range": ">=0.1.0,<0.2.0",
            "last_run_id": run_id,
            "master_snapshot_sha": snapshot_sha,
            "topic_normalization_version": 1,
            "updated_at": now,
            "products_active": products_active,
            "products_tombstones": products_tombstones,
            "citation_backrefs": _citation_backrefs(products_active),
            "subject_index": _subject_index(products_active, products_tombstones),
            "topic_migrations": {},
        }

    def _product_manifest_entry(self, product: Any, run_id: str) -> dict[str, Any]:
        frontmatter = product.frontmatter
        citations = [
            _citation_ref(citation) for citation in frontmatter.get("citations", [])
        ]
        return {
            "relative_path": product.relative_path.as_posix(),
            "category": product.category,
            "topic_id": frontmatter.get("topic_id"),
            "canonical_topic_key": frontmatter.get("canonical_topic_key"),
            "topic_key": frontmatter.get("topic_key"),
            "evidence_hash": frontmatter.get("evidence_hash"),
            "content_hash": short_hash(product.content, 16),
            "run_id": run_id,
            "snapshot_ref": f"distill_runs/{run_id}/snapshot.json",
            "citations": citations,
            "contributors": frontmatter.get("contributors", []),
            "confidence": frontmatter.get("confidence", "unknown"),
            "conflicts_count": len(frontmatter.get("conflicts", [])),
        }

    def _write_health(self, run_id: str, input_records: int, products: int) -> None:
        now = self._now()
        recent_errors: list[str] = []
        if self.incompatible_leaves:
            recent_errors.append(f"incompatible leaves: {self.incompatible_leaves}")
        if self.id_collisions:
            recent_errors.append(f"memory id collisions: {self.id_collisions}")
        if self.quarantined_records:
            recent_errors.append(f"quarantined records: {self.quarantined_records}")
        health = {
            "schema_version": "memory-net/health/v1",
            "node_id": "distiller",
            "role": "distiller",
            "status": "degraded" if recent_errors else "healthy",
            "last_heartbeat_at": now,
            "published_at": now,
            "last_distill_at": now,
            "run_id": run_id,
            "input_records": input_records,
            "products": products,
            "recent_errors": recent_errors,
            "redaction_incidents": {
                "blocked_reports": 0,
                "scanner_hits": {},
            },
            "id_collisions": self.id_collisions,
            "incompatible_leaves": self.incompatible_leaves,
            "quarantined_records": self.quarantined_records,
        }
        (self.repo_path / ".health.json").write_text(
            json.dumps(health, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _write_team_products(self, products: list[Any]) -> None:
        unique = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        team = self.repo_path / "team"
        tmp = self.repo_path / f".team.tmp-{unique}"
        backup = self.repo_path / f".team.prev-{unique}"
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(backup, ignore_errors=True)
        tmp.mkdir(parents=True)
        try:
            for product in products:
                rel = safe_posix_relative_path(
                    product.relative_path,
                    required_prefix=TEAM_PREFIX,
                )
                target = tmp / Path(*rel.parts[1:])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(product.content, encoding="utf-8")
            moved_existing = False
            if team.exists():
                os.replace(team, backup)
                moved_existing = True
            try:
                os.replace(tmp, team)
            except OSError:
                if moved_existing and backup.exists() and not team.exists():
                    os.replace(backup, team)
                raise
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            shutil.rmtree(backup, ignore_errors=True)

    def _write_run_product_snapshot(self, run_id: str, products: list[Any]) -> None:
        root = self.repo_path / "distill_runs" / run_id / TEAM_PREFIX
        self._write_product_snapshot(root, products)

    def _write_product_snapshot(self, root: Path, products: list[Any]) -> None:
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)
        (root / ".gitkeep").write_text("", encoding="utf-8")
        for product in products:
            rel = safe_posix_relative_path(
                product.relative_path,
                required_prefix=TEAM_PREFIX,
            )
            target = root / Path(*rel.parts[1:])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(product.content, encoding="utf-8")

    def _switch_master(self) -> str:
        if (self.repo_path / ".git").exists():
            if self.frozen_master_snapshot_sha:
                return self.frozen_master_snapshot_sha
            has_remote_master = self.repo.run(
                "show-ref",
                "--verify",
                "--quiet",
                "refs/remotes/origin/master",
                check=False,
            ).returncode
            has_local_master = self.repo.run(
                "show-ref",
                "--verify",
                "--quiet",
                "refs/heads/master",
                check=False,
            ).returncode
            if has_remote_master == 0:
                if has_local_master == 0 and self._local_master_ahead_remote():
                    raise RuntimeError(
                        "local master has unpushed commits; refusing to reset"
                    )
                self.repo.run("switch", "-C", "master", "origin/master")
                return self._sha_or_abort("HEAD", label="master snapshot")
            elif has_local_master == 0:
                self.repo.run("switch", "master")
                return self._sha_or_abort("HEAD", label="master snapshot")
        return self._sha_or_abort("HEAD", label="master snapshot")

    def _ensure_master_unchanged(self, snapshot_sha: str, run_id: str) -> None:
        self.repo.run("fetch", "origin", "master", check=False)
        remote_sha = self._sha_or_abort("origin/master", label="origin/master")
        if remote_sha != snapshot_sha:
            self._write_superseded(
                run_id,
                snapshot_sha,
                f"origin/master advanced to {remote_sha}",
            )
            raise SupersededRunError("master changed since distill snapshot")

    def _enforce_master_path_whitelist(self, snapshot_sha: str) -> None:
        diff_paths = self.repo.run(
            "diff",
            "--name-only",
            snapshot_sha,
            check=False,
        ).stdout.splitlines()
        untracked = self.repo.run(
            "ls-files",
            "--others",
            "--exclude-standard",
            check=False,
        ).stdout.splitlines()
        for path in diff_paths + untracked:
            if not _master_path_allowed(path):
                raise RuntimeError(f"distiller diff escapes master whitelist: {path}")

    def _enforce_staged_path_whitelist(self, snapshot_sha: str) -> None:
        staged_paths = self.repo.run(
            "diff",
            "--cached",
            "--name-only",
            snapshot_sha,
            check=False,
        ).stdout.splitlines()
        for path in staged_paths:
            if not _master_path_allowed(path):
                raise RuntimeError(f"distiller staged path escapes whitelist: {path}")

    def _snapshot_push_failure_evidence(
        self,
        run_id: str,
        snapshot_sha: str,
        error: str,
    ) -> Path:
        evidence_root = Path(tempfile.mkdtemp(prefix="memory-net-push-failed-"))
        run_dir = self.repo_path / "distill_runs" / run_id
        artifacts = evidence_root / "artifacts"
        if run_dir.exists():
            shutil.copytree(
                run_dir,
                artifacts,
                ignore=shutil.ignore_patterns("push_failed"),
            )
        path = evidence_root / "push_failed"
        path.mkdir(parents=True)
        (path / "error.txt").write_text(error + "\n", encoding="utf-8")
        patch = self.repo.run("diff", snapshot_sha, "HEAD", check=False)
        (path / "commit.patch").write_text(
            patch.stdout + patch.stderr,
            encoding="utf-8",
        )
        status = self.repo.run("status", "--short", check=False)
        (path / "status.txt").write_text(
            status.stdout + status.stderr,
            encoding="utf-8",
        )
        return evidence_root

    def _restore_push_failure_evidence(self, run_id: str, evidence_root: Path) -> None:
        target = self.repo_path / "distill_runs" / run_id
        target.mkdir(parents=True, exist_ok=True)
        artifacts = evidence_root / "artifacts"
        if artifacts.exists():
            for item in artifacts.iterdir():
                destination = target / item.name
                if item.is_dir():
                    shutil.copytree(item, destination, dirs_exist_ok=True)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, destination)
        shutil.copytree(
            evidence_root / "push_failed",
            target / "push_failed",
            dirs_exist_ok=True,
        )
        shutil.rmtree(evidence_root, ignore_errors=True)

    def _write_clusters(
        self,
        run_id: str,
        clusters: list[dict[str, Any]],
    ) -> None:
        path = self.repo_path / "distill_runs" / run_id / "clusters.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for cluster in clusters:
                handle.write(json.dumps(cluster, sort_keys=True) + "\n")

    def _write_record_bundle(
        self,
        *,
        run_id: str,
        snapshot_sha: str,
        output: DistillationOutput,
    ) -> None:
        if self.record_bundle_dir is None:
            return
        root = self.record_bundle_dir
        root.mkdir(parents=True, exist_ok=True)
        # RecordingProvider creates calls.jsonl lazily on its
        # first call, so a zero-call run (empty input or exhausted budget) would
        # leave the bundle without the consolidated log that ReplayProvider(<dir>)
        # loads eagerly in its constructor. Guarantee an (empty) calls.jsonl so the
        # bundle stays self-consistent and replays as zero calls.
        (root / "calls.jsonl").touch(exist_ok=True)
        _write_json(
            root / "cluster_assignment.json",
            {
                "run_id": run_id,
                "master_snapshot_sha": snapshot_sha,
                "clusters": output.cluster_assignments or [],
            },
        )
        _write_json(
            root / "snapshot.json",
            {
                "leaf_refs": self.leaf_refs,
                "master_snapshot_sha": snapshot_sha,
                "run_id": run_id,
            },
        )
        total_calls = output.clusters_attempted + output.fallback_attempts
        _write_json(
            root / "record_metrics.json",
            {
                "run_id": run_id,
                "calls": total_calls,
                "replay_mode": self.replay_mode,
                "replay_hits": output.replay_hits,
                "replay_misses": output.replay_misses,
                "replay_live_calls": _replay_live_calls(output),
                # Same values as distill_runs/<run_id>/metrics.json: the two
                # sinks are read by different consumers and must never disagree.
                "provider_backends": output.provenance.provider_backends,
                "usage_source": output.provenance.usage_source,
                "usage_source_counts": output.provenance.usage_source_counts,
            },
        )

    @contextmanager
    def _snapshot_prompts(self, snapshot_sha: str) -> Any:
        prompt_paths = [
            ("system_prompt_path", self.pipeline.system_prompt_path),
            ("user_prompt_path", self.pipeline.user_prompt_path),
        ]
        originals = {name: path for name, path in prompt_paths}
        with tempfile.TemporaryDirectory(prefix="memory-net-prompts-") as tmpdir:
            tmp = Path(tmpdir)
            repo_root = self.repo_path.resolve()
            for name, path in prompt_paths:
                candidate = path if path.is_absolute() else self.repo_path / path
                try:
                    rel = candidate.resolve().relative_to(repo_root)
                except ValueError as exc:
                    raise RuntimeError(
                        f"prompt path must be inside repo snapshot: {path}"
                    ) from exc
                result = self.repo.run(
                    "show",
                    f"{snapshot_sha}:{rel.as_posix()}",
                    check=False,
                )
                if result.returncode != 0:
                    raise RuntimeError(
                        f"prompt missing from master snapshot: {rel.as_posix()}"
                    )
                out = tmp / rel.name
                out.write_text(result.stdout, encoding="utf-8")
                setattr(self.pipeline, name, out)
            try:
                yield
            finally:
                for name, path in originals.items():
                    setattr(self.pipeline, name, path)

    def _sha_or_abort(self, ref: str, *, label: str) -> str:
        result = self.repo.run("rev-parse", ref, check=False)
        sha = result.stdout.strip()
        if result.returncode != 0 or not sha:
            raise RuntimeError(f"cannot resolve {label}")
        return sha

    @contextmanager
    def _frozen_snapshot_worktree(self, snapshot_sha: str) -> Any:
        if not (self.repo_path / ".git").exists():
            yield
            return
        original_repo_path = self.repo_path
        original_repo = self.repo
        original_pipeline_paths = {
            "system_prompt_path": self.pipeline.system_prompt_path,
            "user_prompt_path": self.pipeline.user_prompt_path,
        }
        with tempfile.TemporaryDirectory(prefix="memory-net-distill-worktree-") as root:
            worktree = Path(root) / "repo"
            original_repo.run(
                "worktree", "add", "--detach", str(worktree), snapshot_sha
            )
            self.repo_path = worktree
            self.repo = GitRepo(worktree, dry_run=self.dry_run, no_push=self.no_push)
            for name, path in original_pipeline_paths.items():
                if not path.is_absolute():
                    continue
                try:
                    rel = path.resolve().relative_to(original_repo_path.resolve())
                except ValueError:
                    continue
                setattr(self.pipeline, name, worktree / rel)
            try:
                yield
            except Exception:
                _copy_frozen_artifacts(worktree, original_repo_path)
                raise
            finally:
                if self.no_push:
                    _copy_frozen_artifacts(worktree, original_repo_path)
                for name, path in original_pipeline_paths.items():
                    setattr(self.pipeline, name, path)
                self.repo_path = original_repo_path
                self.repo = original_repo
                original_repo.run(
                    "worktree",
                    "remove",
                    "--force",
                    str(worktree),
                    check=False,
                )

    def _local_master_ahead_remote(self) -> bool:
        result = self.repo.run(
            "rev-list",
            "--left-right",
            "--count",
            "origin/master...master",
            check=False,
        )
        if result.returncode != 0:
            return False
        parts = result.stdout.split()
        if len(parts) != 2:
            return False
        return int(parts[1]) > 0

    def _write_superseded(self, run_id: str, snapshot_sha: str, reason: str) -> None:
        path = self.repo_path / "distill_runs" / run_id / "superseded.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "status": "config_drift_superseded",
                    "master_snapshot_sha": snapshot_sha,
                    "reason": reason,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )


def _manifest_products_mapping(raw: Any) -> dict[str, dict[str, Any]]:
    if isinstance(raw, dict):
        return {
            str(distillation_id): dict(entry)
            for distillation_id, entry in raw.items()
            if isinstance(entry, dict)
        }
    if isinstance(raw, list):
        result: dict[str, dict[str, Any]] = {}
        for entry in raw:
            if isinstance(entry, dict) and entry.get("distillation_id"):
                result[str(entry["distillation_id"])] = dict(entry)
        return result
    return {}


def _validate_manifest_frontmatter_consistency(
    *,
    memory_id: str,
    entry: dict[str, Any],
    frontmatter: dict[str, Any],
    relative_path: str,
) -> None:
    checks = {
        "memory_id": memory_id,
        "revision_hash": entry.get("revision_hash"),
        "source_locator": entry.get("source_locator"),
        "report_fingerprint": entry.get("report_fingerprint"),
    }
    for key, expected in checks.items():
        if str(frontmatter.get(key) or "") != str(expected or ""):
            raise RuntimeError(
                f"reported frontmatter {key} mismatch for {relative_path}"
            )


def _llm_record(
    *,
    memory_id: str,
    entry: dict[str, Any],
    frontmatter: dict[str, Any],
    body: str,
    content: str,
    leaf_branch: str,
    branch_sha: str,
    node_id: str,
    relative_path: str,
    leaf_health: dict[str, Any],
) -> dict[str, Any]:
    return {
        "memory_id": memory_id,
        "revision_hash": str(entry["revision_hash"]),
        "report_fingerprint": str(entry["report_fingerprint"]),
        "source_locator": str(entry["source_locator"]),
        "author_node": str(frontmatter.get("author_node") or node_id),
        "author_adapter": str(frontmatter.get("author_adapter") or ""),
        "category_hint": str(frontmatter.get("category_hint") or ""),
        "confidence": str(frontmatter.get("confidence") or "unknown"),
        "source_type": str(frontmatter.get("source_type") or "agent_memory"),
        "redaction_pattern_version": str(
            frontmatter.get("redaction_pattern_version") or ""
        ),
        "adapter_config_revision": str(
            frontmatter.get("adapter_config_revision") or ""
        ),
        "created_at": str(frontmatter.get("created_at") or ""),
        "updated_at": str(frontmatter.get("updated_at") or ""),
        "reported_at": str(frontmatter.get("reported_at") or ""),
        "content": content,
        "body": body,
        "leaf_branch": leaf_branch,
        "leaf_health": leaf_health,
        "source_branch_sha": branch_sha,
        "relative_path": relative_path,
    }


def _scanner_audit_summary(leaf_healths: Iterable[dict[str, Any]]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for health in leaf_healths:
        incidents = health.get("redaction_incidents")
        if not isinstance(incidents, dict):
            continue
        scanner_hits = incidents.get("scanner_hits")
        if not isinstance(scanner_hits, dict):
            continue
        for scanner, count in scanner_hits.items():
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                continue
            summary[str(scanner)] = summary.get(str(scanner), 0) + count
    return summary


def _replay_live_calls(output: DistillationOutput) -> int:
    """Calls that hit a live provider this run = total calls minus replay hits.

    Equals total calls on a normal run (no replay), 0 under full strict replay,
    and the miss count under allow-live-on-miss.
    """
    total_calls = output.clusters_attempted + output.fallback_attempts
    return max(0, total_calls - output.replay_hits)


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _distill_gate_error(output: DistillationOutput) -> str | None:
    failures: list[str] = []
    if output.parse_failures:
        failures.append(f"parse failures: {output.parse_failures}")
    if output.citation_failures:
        failures.append(f"citation failures: {output.citation_failures}")
    if not failures:
        return None
    return "distill quality gate failed: " + "; ".join(failures)


def _copy_frozen_artifacts(source_repo: Path, target_repo: Path) -> None:
    for rel in ("manifest.json", ".health.json", TEAM_PREFIX, "distill_runs"):
        source = source_repo / rel
        if not source.exists():
            continue
        target = target_repo / rel
        if source.is_dir():
            _copy_tree_contents(source, target)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _copy_tree_contents(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        destination = target / child.name
        if child.is_dir():
            _copy_tree_contents(child, destination)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(child, destination)


def _citation_ref(citation: dict[str, Any]) -> str:
    return f"{citation['memory_id']}@{citation['revision_hash']}"


def _citation_backrefs(
    products_active: dict[str, dict[str, Any]],
) -> dict[str, list[str]]:
    refs: dict[str, set[str]] = {}
    for distillation_id, product in products_active.items():
        for citation in product.get("citations", []):
            refs.setdefault(str(citation), set()).add(distillation_id)
    return {key: sorted(value) for key, value in refs.items()}


def _subject_index(
    products_active: dict[str, dict[str, Any]],
    products_tombstones: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for distillation_id, product in products_active.items():
        for contributor in product.get("contributors", []):
            subject = index.setdefault(
                str(contributor),
                {
                    "active_products": [],
                    "tombstoned_products": [],
                    "active_memory_refs": {},
                    "tombstoned_memory_refs": {},
                },
            )
            if distillation_id not in subject["active_products"]:
                subject["active_products"].append(distillation_id)
            if product.get("run_id"):
                subject["last_contribution_at"] = product.get("run_id")
            for citation in product.get("citations", []):
                refs = subject["active_memory_refs"].setdefault(str(citation), [])
                if distillation_id not in refs:
                    refs.append(distillation_id)

    for distillation_id, product in products_tombstones.items():
        for contributor in product.get("contributors", []):
            subject = index.setdefault(
                str(contributor),
                {
                    "active_products": [],
                    "tombstoned_products": [],
                    "active_memory_refs": {},
                    "tombstoned_memory_refs": {},
                },
            )
            if distillation_id not in subject["tombstoned_products"]:
                subject["tombstoned_products"].append(distillation_id)
            for citation in product.get("cited_memories", []):
                refs = subject["tombstoned_memory_refs"].setdefault(str(citation), [])
                if distillation_id not in refs:
                    refs.append(distillation_id)
    for subject in index.values():
        subject["active_products"].sort()
        subject["tombstoned_products"].sort()
        for refs in subject["active_memory_refs"].values():
            refs.sort()
        for refs in subject["tombstoned_memory_refs"].values():
            refs.sort()
    return index


def _master_path_allowed(path: str) -> bool:
    return path in MASTER_ALLOWED_EXACT_PATHS or any(
        path.startswith(f"{prefix}/") for prefix in MASTER_ALLOWED_PREFIXES
    )


def _is_superseded_push_error(message: str) -> bool:
    lowered = message.lower()
    return "non-fast-forward" in lowered or "rejected" in lowered
