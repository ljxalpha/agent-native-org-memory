"""The embedding policy travels from the profile to both consumers (§5.5.7).

Two properties, and the second is the one worth the trouble. *That* a policy
reaches the embedder is easy to check. That the clusterer ends up on the same
one -- always, not by convention -- is the property whose absence has no runtime
symptom: real vectors grouped by the single-cluster fallback produce a run that
reports success and clusters nothing.
"""

from pathlib import Path
from typing import Any

import pytest

from memory_net.distiller import cli as cli_module
from memory_net.distiller.clustering.policy import (
    DEFAULT_MODEL_NAME,
    EmbeddingPolicy,
)
from memory_net.distiller.llm.mock import MockLLMProvider
from memory_net.distiller.pipeline import DistillationPipeline


class _StopHere(Exception):
    """Ends main() once the assembly under test has happened."""


# --------------------------------------------------------------------------
# Profile -> policy
# --------------------------------------------------------------------------


def test_the_profile_names_both_the_backend_and_the_model() -> None:
    """The model name was the gap in the first design of this: naming only the
    backend leaves the fastembed branch with nothing to load."""
    policy = cli_module._embedding_policy(
        {"clustering": {"embedding_backend": "fastembed", "embedding_model": "m/x"}}
    )

    assert policy.backend == "fastembed"
    assert policy.model_name == "m/x"


def test_a_profile_silent_about_embeddings_keeps_the_stand_in() -> None:
    """Silence means a development profile; a production one cannot be silent,
    because the schema refuses it."""
    policy = cli_module._embedding_policy({})

    assert policy.backend == "test-cheap"
    assert policy.model_name == DEFAULT_MODEL_NAME


def test_an_unknown_backend_in_the_profile_stops_the_run() -> None:
    with pytest.raises(SystemExit, match="clustering"):
        cli_module._embedding_policy({"clustering": {"embedding_backend": "word2vec"}})


# --------------------------------------------------------------------------
# One policy, both consumers
# --------------------------------------------------------------------------


def test_the_clusterer_is_handed_the_policy_of_the_embedder_that_made_the_vectors(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Resolved from the producer rather than looked up a second time: two
    lookups can disagree, and reading it off the producer cannot."""
    import memory_net.distiller.pipeline as pipeline_module
    from memory_net.distiller.clustering.embedder import FastEmbedder

    seen: list[Any] = []

    def spy(vectors: Any, **kwargs: Any) -> list[int]:
        seen.append(kwargs.get("policy"))
        return [0 for _ in vectors]

    monkeypatch.setattr(pipeline_module, "cluster_vectors", spy)
    policy = EmbeddingPolicy("test-cheap")
    system = tmp_path / "s.md"
    user = tmp_path / "u.md"
    system.write_text("s", encoding="utf-8")
    user.write_text("{{ records_json }}", encoding="utf-8")

    DistillationPipeline(
        provider=MockLLMProvider(),
        embedder=FastEmbedder(policy=policy),
        system_prompt_path=system,
        user_prompt_path=user,
    ).run([_record()], run_id="run")

    assert seen == [policy]


# --------------------------------------------------------------------------
# The probe, and where it sits
# --------------------------------------------------------------------------


def test_a_backend_that_cannot_load_stops_the_run_before_any_model_is_called(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Discovering it later means the run has already fetched, read every leaf
    branch and possibly been billed, all for a run that cannot succeed."""
    calls: list[str] = []
    monkeypatch.setattr(
        cli_module, "_base_provider", lambda *a, **k: calls.append("provider")
    )
    monkeypatch.setattr(
        EmbeddingPolicy,
        "probe",
        lambda self: (_ for _ in ()).throw(RuntimeError("could not be loaded")),
    )
    repo = _repo(tmp_path)

    with pytest.raises(RuntimeError, match="could not be loaded"):
        cli_module.main(["run", "--repo", str(repo), "--dry-run"])

    assert calls == []


def test_the_probe_runs_after_the_profile_is_read(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """It cannot run earlier: the backend is named in the profile, and the
    profile is read out of the frozen snapshot, which the fetch produces."""
    order: list[str] = []
    real_read = cli_module._read_profile_text

    monkeypatch.setattr(
        cli_module,
        "_read_profile_text",
        lambda *a, **k: (order.append("profile"), real_read(*a, **k))[1],
    )
    monkeypatch.setattr(
        EmbeddingPolicy, "probe", lambda self: order.append("probe") and None
    )
    monkeypatch.setattr(
        cli_module,
        "DistillOrchestrator",
        lambda **kwargs: (_ for _ in ()).throw(_StopHere()),
    )
    repo = _repo(tmp_path)

    with pytest.raises(_StopHere):
        cli_module.main(["run", "--repo", str(repo), "--dry-run"])

    assert order == ["profile", "probe"]


# --------------------------------------------------------------------------
# Preflight: the checks that need no profile
# --------------------------------------------------------------------------


def test_preflight_runs_before_anything_is_fetched(
    tmp_path: Path, monkeypatch: Any
) -> None:
    order: list[str] = []
    monkeypatch.setattr(
        cli_module, "_preflight", lambda args: order.append("preflight")
    )
    monkeypatch.setattr(
        cli_module,
        "_freeze_master_snapshot",
        lambda repo: order.append("fetch") and None,
    )
    monkeypatch.setattr(
        cli_module,
        "DistillOrchestrator",
        lambda **kwargs: (_ for _ in ()).throw(_StopHere()),
    )
    repo = _repo(tmp_path)

    with pytest.raises(_StopHere):
        cli_module.main(["run", "--repo", str(repo), "--dry-run"])

    assert order == ["preflight", "fetch"]


def test_preflight_refuses_the_production_backend_without_its_credentials(
    monkeypatch: Any,
) -> None:
    import argparse

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)

    with pytest.raises(SystemExit, match="DEEPSEEK_API_KEY"):
        cli_module._preflight(argparse.Namespace(provider="deepseek"))


def test_preflight_is_silent_for_every_other_provider(monkeypatch: Any) -> None:
    """Only the production lane needs those variables; failing a mock run over
    them would make the check itself the outage."""
    import argparse

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    for provider in (None, "mock", "hermes_tool", "anthropic"):
        cli_module._preflight(argparse.Namespace(provider=provider))


def _record() -> dict[str, str]:
    return {
        "memory_id": "leaf-a-mem-1",
        "revision_hash": "abc12345",
        "author_node": "alice",
        "source_branch_sha": "deadbeef",
        "relative_path": "memories/auth/retry-abc12345.md",
        "category_hint": "auth",
        "content": "---\ntitle: Retry\n---\nUse backoff.",
    }


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    (repo / "prompts").mkdir(parents=True)
    (repo / "config" / "distill.yaml").write_text(
        "llm:\n  provider: mock\n", encoding="utf-8"
    )
    (repo / "prompts" / "distill_system.md").write_text("system", encoding="utf-8")
    (repo / "prompts" / "distill_user.md").write_text(
        "records {{ records_json }}", encoding="utf-8"
    )
    (repo / "bootstrap_state.json").write_text(
        '{"schema_version":1,"compatible_code_range":">=0.1.0,<0.2.0",'
        '"compatible_build_api_range":">=1,<2",'
        '"manifest_schema_version":1,"record_frontmatter_schema_version":1,'
        '"product_frontmatter_schema_version":1,"health_schema_version":1,'
        '"owner_schema_version":1}',
        encoding="utf-8",
    )
    return repo
