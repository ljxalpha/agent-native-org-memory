"""Profile validation and provenance, from the CLI to the run artifacts (§5.5.6).

The unit under test is the join: the CLI computes the provenance once and the
orchestrator writes whatever it was given. A test that only checked the value
object would pass while nothing ever reached disk.
"""

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from memory_net.distiller import cli as cli_module
from memory_net.distiller.orchestrator import DistillOrchestrator
from memory_net.distiller.profile import ProfileProvenance
from memory_net.distiller.runctx import RunContext


class _StopHere(Exception):
    """Ends main() once the assembly under test has happened."""


def _repo(tmp_path: Path, profile: str) -> Path:
    """A minimal non-git repo: enough for the CLI to assemble and then stop."""
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    (repo / "prompts").mkdir(parents=True)
    (repo / "config" / "distill.yaml").write_text(profile, encoding="utf-8")
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


def _run(repo: Path, monkeypatch: Any) -> RunContext:
    """Run main() far enough to capture the assembled context, then stop."""
    seen: dict[str, RunContext] = {}
    real = cli_module.DistillOrchestrator

    class _Spy(real):  # type: ignore[misc, valid-type]
        def __init__(self, **kwargs: Any) -> None:
            seen["context"] = kwargs["context"]
            super().__init__(**kwargs)

        def run(self) -> Any:
            raise _StopHere

    monkeypatch.setattr(cli_module, "DistillOrchestrator", _Spy)
    with pytest.raises(_StopHere):
        cli_module.main(["run", "--repo", str(repo), "--dry-run"])
    return seen["context"]


# --------------------------------------------------------------------------
# Validation stops the run
# --------------------------------------------------------------------------


def test_an_invalid_profile_stops_the_run_before_anything_is_built(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A production profile leaning on defaults must not reach a provider: the
    whole point is that its failure mode is a run that quietly succeeds."""
    repo = _repo(tmp_path, "profile_class: production\nllm:\n  provider: mock\n")
    built: list[str] = []
    monkeypatch.setattr(
        cli_module,
        "_base_provider",
        lambda *a, **k: built.append("provider"),
    )

    with pytest.raises(SystemExit, match="invalid profile"):
        cli_module.main(["run", "--repo", str(repo), "--dry-run"])

    assert built == []


def test_the_shipped_default_profile_still_starts(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """It carries no profile_class, so it is a development profile and none of
    the production requirements apply."""
    repo = _repo(tmp_path, "llm:\n  provider: mock\n")

    assert _run(repo, monkeypatch) is not None


# --------------------------------------------------------------------------
# Provenance reaches the context, then the artifact
# --------------------------------------------------------------------------


def test_the_cli_records_which_profile_bytes_it_read(
    tmp_path: Path, monkeypatch: Any
) -> None:
    import hashlib

    raw = "llm:\n  provider: mock\n"
    repo = _repo(tmp_path, raw)

    provenance = _run(repo, monkeypatch).profile

    assert isinstance(provenance, ProfileProvenance)
    assert provenance.profile_path == "config/distill.yaml"
    assert provenance.blob_sha256 == hashlib.sha256(raw.encode("utf-8")).hexdigest()


def test_a_run_with_no_profile_file_records_no_provenance(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Recording a hash of nothing would be worse than recording nothing."""
    repo = _repo(tmp_path, "llm:\n  provider: mock\n")
    (repo / "config" / "distill.yaml").unlink()

    assert _run(repo, monkeypatch).profile is None


def test_the_snapshot_artifact_carries_the_profile_that_produced_it(
    tmp_path: Path,
) -> None:
    orchestrator = _orchestrator(
        tmp_path,
        ProfileProvenance.of(
            profile_path="config/profiles/production.yaml",
            raw="llm: {}\n",
            master_snapshot_sha="from-the-profile",
        ),
    )

    payload = json.loads(
        orchestrator._snapshot_payload(run_id="run-1", snapshot_sha="from-the-run")
    )

    assert payload["profile_path"] == "config/profiles/production.yaml"
    assert len(payload["profile_blob_sha256"]) == 64
    # The run's own snapshot sha wins: letting the profile's copy through would
    # silently change what that field means depending on the injection.
    assert payload["master_snapshot_sha"] == "from-the-run"


def test_the_snapshot_artifact_is_unchanged_without_a_profile(tmp_path: Path) -> None:
    """Every run that predates provenance must keep producing the same bytes."""
    payload = json.loads(
        _orchestrator(tmp_path, None)._snapshot_payload(
            run_id="run-1", snapshot_sha="sha"
        )
    )

    assert set(payload) == {"leaf_refs", "master_snapshot_sha", "run_id"}


def test_both_snapshot_writers_share_one_payload() -> None:
    """They used to carry duplicated literals, which is how one of them gains a
    field the other never does."""
    source = Path(DistillOrchestrator.__module__.replace(".", "/") + ".py")
    text = (Path.cwd() / source).read_text(encoding="utf-8")

    assert text.count("_snapshot_payload(run_id=") == 2


def _orchestrator(tmp_path: Path, provenance: ProfileProvenance | None) -> Any:
    from memory_net.distiller.llm.mock import MockLLMProvider
    from memory_net.distiller.pipeline import DistillationPipeline

    system = tmp_path / "s.md"
    user = tmp_path / "u.md"
    system.write_text("s", encoding="utf-8")
    user.write_text("{{ records_json }}", encoding="utf-8")
    return DistillOrchestrator(
        repo_path=tmp_path,
        pipeline=DistillationPipeline(
            provider=MockLLMProvider(),
            system_prompt_path=system,
            user_prompt_path=user,
        ),
        context=RunContext(profile=provenance),
    )


def _git(repo: Path, *args: str) -> None:  # pragma: no cover - helper
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
