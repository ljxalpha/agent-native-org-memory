"""The bundle transaction, end to end through the orchestrator (§5.5.12).

Assertions are on paths, not return values: "the second cluster was refused"
must mean the first cluster's calls are on disk nowhere at all, and only the
filesystem can answer that.
"""

from __future__ import annotations

import argparse
import contextlib
import json
from pathlib import Path
from typing import Any

import pytest

from memory_net.distiller import cli as cli_module
from memory_net.distiller import orchestrator as orchestrator_module
from memory_net.distiller.llm.base import LLMProvider, LLMRequest, LLMResponse
from memory_net.distiller.llm.recording import BundleRecorder, RecordingProvider
from memory_net.distiller.orchestrator import (
    BundleContentError,
    DistillGateError,
    DistillOrchestrator,
)
from memory_net.distiller.output_scanner import (
    OutputScanner,
    RedactionViolation,
    Ruleset,
)
from memory_net.distiller.pipeline import DistillationOutput
from memory_net.distiller.runctx import RunContextBuilder, RunIdentity

PATTERNS_PATH = Path("config/redaction_patterns.yaml")
HOME_PATH = "/" + "home/" + "example-user/notes.md"
RUN_ID = "20260810T101010Z-a1b2c3d4"


@pytest.fixture()
def scanner() -> OutputScanner:
    return OutputScanner(Ruleset.from_yaml(PATTERNS_PATH))


class _TwoClusterPipeline:
    """Stands in for the pipeline: records two calls, the second one leaking."""

    def __init__(self, provider: LLMProvider, second_text: str) -> None:
        self.provider = provider
        self.second_text = second_text
        self.system_prompt_path = Path("prompts/distill_system.md")
        self.user_prompt_path = Path("prompts/distill_user.md")

    def run(self, records: list[dict[str, Any]], *, run_id: str) -> DistillationOutput:
        for index, text in enumerate(("first cluster", self.second_text)):
            self.provider.generate(
                LLMRequest(system_prompt="s", user_prompt=f"cluster {index}", model="m")
            )
        return _output()


class _EchoProvider(LLMProvider):
    def __init__(self, texts: list[str]) -> None:
        self.texts = texts

    def generate(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(text=self.texts.pop(0), model=request.model)


def _output(**overrides: Any) -> DistillationOutput:
    values: dict[str, Any] = {
        "products": [],
        "response": LLMResponse(text="", model="m"),
        "cluster_assignments": [{"cluster_id": 0, "memory_ids": ["m-1"], "size": 1}],
    }
    values.update(overrides)
    return DistillationOutput(**values)


def _orchestrator(
    tmp_path: Path,
    *,
    scanner: OutputScanner | None,
    recorder: BundleRecorder | None,
    pipeline: Any = None,
) -> DistillOrchestrator:
    context = (
        RunContextBuilder()
        .set_identity(RunIdentity(run_id=RUN_ID, now=lambda: "2026-08-10T00:00:00Z"))
        .set_scanner(scanner)
        .set_recorder(recorder)
        .freeze()
    )
    return DistillOrchestrator(
        repo_path=tmp_path / "repo",
        pipeline=pipeline,
        context=context,
    )


def _stub_repo_io(orchestrator: DistillOrchestrator) -> None:
    """Silence the git-backed inputs, leaving the transaction under test.

    These tests are about what is on disk after a run, not about reading a
    repository; a real one would only add setup that can fail for its own
    reasons.
    """
    orchestrator._load_leaf_records = list  # type: ignore[method-assign]
    orchestrator._load_master_manifest = dict  # type: ignore[method-assign]
    orchestrator._snapshot_prompts = (  # type: ignore[method-assign]
        lambda snapshot_sha: contextlib.nullcontext()
    )
    orchestrator._write_failure_outputs = (  # type: ignore[method-assign]
        lambda **kwargs: None
    )
    orchestrator._write_distill_outputs = (  # type: ignore[method-assign]
        lambda **kwargs: None
    )
    orchestrator._publish_outputs = (  # type: ignore[method-assign]
        lambda **kwargs: None
    )


def _staged(tmp_path: Path) -> BundleRecorder:
    return BundleRecorder.staged_bundle(tmp_path / "bundle", run_id=RUN_ID)


def _write_and_commit(
    orchestrator: DistillOrchestrator, output: DistillationOutput | None = None
) -> None:
    orchestrator._write_record_bundle(
        run_id=RUN_ID, snapshot_sha="deadbeef", output=output or _output()
    )
    orchestrator._commit_record_bundle()


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


def test_a_clean_run_publishes_the_bundle_and_leaves_no_staging(
    tmp_path: Path, scanner: OutputScanner
) -> None:
    recorder = _staged(tmp_path)
    orchestrator = _orchestrator(tmp_path, scanner=scanner, recorder=recorder)

    _write_and_commit(orchestrator)

    assert (tmp_path / "bundle" / "snapshot.json").exists()
    assert (tmp_path / "bundle" / "record_metrics.json").exists()
    assert recorder.staging is not None and not recorder.staging.exists()


def test_the_auxiliary_json_is_written_into_staging_not_the_target(
    tmp_path: Path, scanner: OutputScanner
) -> None:
    recorder = _staged(tmp_path)
    orchestrator = _orchestrator(tmp_path, scanner=scanner, recorder=recorder)

    orchestrator._write_record_bundle(
        run_id=RUN_ID, snapshot_sha="deadbeef", output=_output()
    )

    assert not (tmp_path / "bundle").exists()
    assert recorder.staging is not None
    assert (recorder.staging / "cluster_assignment.json").exists()


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


def test_a_second_leaking_cluster_leaves_the_first_one_nowhere(
    tmp_path: Path, scanner: OutputScanner
) -> None:
    """The transaction's whole reason for existing."""
    recorder = _staged(tmp_path)
    provider = RecordingProvider(
        _EchoProvider(["clean answer", f"the file is at {HOME_PATH}"]),
        recorder=recorder,
        context=RunContextBuilder().set_scanner(scanner).freeze(),
    )
    orchestrator = _orchestrator(
        tmp_path,
        scanner=scanner,
        recorder=recorder,
        pipeline=_TwoClusterPipeline(provider, f"the file is at {HOME_PATH}"),
    )
    orchestrator.repo_path.mkdir(parents=True)
    _stub_repo_io(orchestrator)

    with pytest.raises(RedactionViolation):
        orchestrator._run_at_snapshot(0.0, "deadbeef")

    assert not (tmp_path / "bundle").exists()
    assert recorder.staging is not None and not recorder.staging.exists()
    assert list(tmp_path.glob("**/calls.jsonl")) == []


def test_a_quality_gate_failure_leaves_no_bundle(
    tmp_path: Path, scanner: OutputScanner
) -> None:
    recorder = _staged(tmp_path)

    class _FailingPipeline(_TwoClusterPipeline):
        def run(
            self, records: list[dict[str, Any]], *, run_id: str
        ) -> DistillationOutput:
            self.provider.generate(
                LLMRequest(system_prompt="s", user_prompt="u", model="m")
            )
            return _output(parse_failures=1)

    provider = RecordingProvider(
        _EchoProvider(["clean answer"]),
        recorder=recorder,
        context=RunContextBuilder().set_scanner(scanner).freeze(),
    )
    orchestrator = _orchestrator(
        tmp_path,
        scanner=scanner,
        recorder=recorder,
        pipeline=_FailingPipeline(provider, "unused"),
    )
    orchestrator.repo_path.mkdir(parents=True)
    _stub_repo_io(orchestrator)

    with pytest.raises(DistillGateError, match="parse failures"):
        orchestrator._run_at_snapshot(0.0, "deadbeef")

    assert not (tmp_path / "bundle").exists()
    assert recorder.staging is not None and not recorder.staging.exists()


def test_any_exception_still_removes_the_staging(
    tmp_path: Path, scanner: OutputScanner
) -> None:
    """The finally covers the failure modes nobody enumerated."""
    recorder = _staged(tmp_path)

    class _ExplodingPipeline(_TwoClusterPipeline):
        def run(
            self, records: list[dict[str, Any]], *, run_id: str
        ) -> DistillationOutput:
            self.provider.generate(
                LLMRequest(system_prompt="s", user_prompt="u", model="m")
            )
            raise RuntimeError("something entirely unrelated")

    provider = RecordingProvider(
        _EchoProvider(["clean answer"]),
        recorder=recorder,
        context=RunContextBuilder().set_scanner(scanner).freeze(),
    )
    orchestrator = _orchestrator(
        tmp_path,
        scanner=scanner,
        recorder=recorder,
        pipeline=_ExplodingPipeline(provider, "unused"),
    )
    orchestrator.repo_path.mkdir(parents=True)
    _stub_repo_io(orchestrator)

    with pytest.raises(RuntimeError, match="entirely unrelated"):
        orchestrator._run_at_snapshot(0.0, "deadbeef")

    assert not (tmp_path / "bundle").exists()
    assert recorder.staging is not None and not recorder.staging.exists()


def test_a_leaking_auxiliary_json_aborts_the_commit(
    tmp_path: Path, scanner: OutputScanner
) -> None:
    """The aux files never passed through the per-call scan, so the commit-time
    pass is the only thing standing between them and publication."""
    recorder = _staged(tmp_path)
    orchestrator = _orchestrator(tmp_path, scanner=scanner, recorder=recorder)
    orchestrator.leaf_refs = {"leaf/alice": "a" * 40, "note": f"see {HOME_PATH}"}

    with pytest.raises(RedactionViolation):
        _write_and_commit(orchestrator)

    assert not (tmp_path / "bundle").exists()
    orchestrator._abort_record_bundle()
    assert recorder.staging is not None and not recorder.staging.exists()


def test_a_file_written_around_the_recorder_is_caught_at_commit(
    tmp_path: Path, scanner: OutputScanner
) -> None:
    recorder = _staged(tmp_path)
    orchestrator = _orchestrator(tmp_path, scanner=scanner, recorder=recorder)
    orchestrator._write_record_bundle(
        run_id=RUN_ID, snapshot_sha="deadbeef", output=_output()
    )
    assert recorder.staging is not None
    (recorder.staging / "requests").mkdir(exist_ok=True)
    (recorder.staging / "requests" / "9999-smuggled.json").write_text(
        json.dumps({"note": f"see {HOME_PATH}"}), encoding="utf-8"
    )

    with pytest.raises(RedactionViolation):
        orchestrator._commit_record_bundle()

    assert not (tmp_path / "bundle").exists()


def test_a_leaking_path_name_is_caught_at_commit(
    tmp_path: Path, scanner: OutputScanner
) -> None:
    """The listing scans the path strings themselves, not only file contents."""
    recorder = _staged(tmp_path)
    orchestrator = _orchestrator(tmp_path, scanner=scanner, recorder=recorder)
    orchestrator._write_record_bundle(
        run_id=RUN_ID, snapshot_sha="deadbeef", output=_output()
    )
    assert recorder.staging is not None
    (recorder.staging / "build-box.local.txt").write_text("x", encoding="utf-8")

    with pytest.raises(RedactionViolation):
        orchestrator._commit_record_bundle()

    assert not (tmp_path / "bundle").exists()


@pytest.mark.parametrize("name", ["requests/9999-smuggled.txt", "notes", "blob.bin"])
def test_a_file_the_extension_does_not_advertise_is_still_read(
    name: str, tmp_path: Path, scanner: OutputScanner
) -> None:
    """Dispatching on ``.json`` would leave the content of everything else
    published with only its path, size and hash ever looked at."""
    recorder = _staged(tmp_path)
    orchestrator = _orchestrator(tmp_path, scanner=scanner, recorder=recorder)
    orchestrator._write_record_bundle(
        run_id=RUN_ID, snapshot_sha="deadbeef", output=_output()
    )
    assert recorder.staging is not None
    smuggled = recorder.staging / name
    smuggled.parent.mkdir(parents=True, exist_ok=True)
    smuggled.write_bytes(f"see {HOME_PATH}".encode())

    with pytest.raises(RedactionViolation):
        orchestrator._commit_record_bundle()

    assert not (tmp_path / "bundle").exists()


@pytest.mark.parametrize(
    "payload",
    [
        b"\xff\xfe" + b"junk",
        f"see {HOME_PATH}".encode("utf-16"),
    ],
)
def test_bytes_that_are_not_utf8_are_refused_rather_than_decoded_loosely(
    payload: bytes, tmp_path: Path, scanner: OutputScanner
) -> None:
    """``errors="replace"`` turns UTF-16 into interleaved NULs, in which no rule
    can match the text a reader would see, so the commit is refused instead."""
    recorder = _staged(tmp_path)
    orchestrator = _orchestrator(tmp_path, scanner=scanner, recorder=recorder)
    orchestrator._write_record_bundle(
        run_id=RUN_ID, snapshot_sha="deadbeef", output=_output()
    )
    assert recorder.staging is not None
    (recorder.staging / "blob.bin").write_bytes(payload)

    with pytest.raises(BundleContentError, match="not valid UTF-8"):
        orchestrator._commit_record_bundle()

    assert not (tmp_path / "bundle").exists()


def test_a_symlink_is_refused_rather_than_followed(
    tmp_path: Path, scanner: OutputScanner
) -> None:
    """Following it would scan the target and then publish a link to content
    the bundle never contained and cannot keep."""
    recorder = _staged(tmp_path)
    orchestrator = _orchestrator(tmp_path, scanner=scanner, recorder=recorder)
    orchestrator._write_record_bundle(
        run_id=RUN_ID, snapshot_sha="deadbeef", output=_output()
    )
    assert recorder.staging is not None
    outside = tmp_path / "outside.txt"
    outside.write_text("clean for now", encoding="utf-8")
    (recorder.staging / "link.txt").symlink_to(outside)

    with pytest.raises(BundleContentError, match="not a regular file"):
        orchestrator._commit_record_bundle()

    assert not (tmp_path / "bundle").exists()


def test_an_empty_directory_name_is_still_scanned(
    tmp_path: Path, scanner: OutputScanner
) -> None:
    """It has no child to carry its name into the listing."""
    recorder = _staged(tmp_path)
    orchestrator = _orchestrator(tmp_path, scanner=scanner, recorder=recorder)
    orchestrator._write_record_bundle(
        run_id=RUN_ID, snapshot_sha="deadbeef", output=_output()
    )
    assert recorder.staging is not None
    (recorder.staging / "build-box.local").mkdir()

    with pytest.raises(RedactionViolation):
        orchestrator._commit_record_bundle()


def test_a_file_too_large_to_scan_is_refused(
    tmp_path: Path, scanner: OutputScanner, monkeypatch: Any
) -> None:
    """Reading it whole is how the fail-closed path runs out of memory instead
    of refusing."""
    monkeypatch.setattr(orchestrator_module, "MAX_STAGED_FILE_BYTES", 16)
    recorder = _staged(tmp_path)
    orchestrator = _orchestrator(tmp_path, scanner=scanner, recorder=recorder)
    orchestrator._write_record_bundle(
        run_id=RUN_ID, snapshot_sha="deadbeef", output=_output()
    )
    assert recorder.staging is not None
    (recorder.staging / "big.txt").write_text("x" * 64, encoding="utf-8")

    with pytest.raises(BundleContentError, match="too large"):
        orchestrator._commit_record_bundle()


def test_a_failure_after_the_commit_keeps_the_bundle(
    tmp_path: Path, scanner: OutputScanner
) -> None:
    """The transaction ends at the commit, deliberately.

    Everything the scan protects against happens before it; what can still fail
    afterwards is writing products, the manifest or the publication. The bundle
    records model calls that were already paid for and is the only material a
    rerun can replay instead of paying again, so it stays."""
    recorder = _staged(tmp_path)
    provider = RecordingProvider(
        _EchoProvider(["clean answer", "also clean"]),
        recorder=recorder,
        context=RunContextBuilder().set_scanner(scanner).freeze(),
    )
    orchestrator = _orchestrator(
        tmp_path,
        scanner=scanner,
        recorder=recorder,
        pipeline=_TwoClusterPipeline(provider, "also clean"),
    )
    orchestrator.repo_path.mkdir(parents=True)
    _stub_repo_io(orchestrator)

    def _explode(**kwargs: Any) -> None:
        raise OSError("no space left on device")

    orchestrator._write_distill_outputs = _explode  # type: ignore[method-assign]

    with pytest.raises(OSError, match="no space left"):
        orchestrator._run_at_snapshot(0.0, "deadbeef")

    assert (tmp_path / "bundle" / "calls.jsonl").exists()
    assert recorder.staging is not None and not recorder.staging.exists()


# --------------------------------------------------------------------------
# The published manifest
# --------------------------------------------------------------------------


def test_the_master_manifest_is_scanned_before_it_is_written(
    tmp_path: Path, scanner: OutputScanner
) -> None:
    """The manifest is a published artifact the staged-bundle scan cannot see,
    and it carries entries the previous manifest contributed."""
    orchestrator = _orchestrator(tmp_path, scanner=scanner, recorder=None)

    with pytest.raises(RedactionViolation):
        orchestrator._master_manifest(
            run_id=RUN_ID,
            snapshot_sha="deadbeef",
            products=[],
            previous_manifest={
                "products_active": {
                    "distill-old-001": {
                        "relative_path": f"team/{HOME_PATH}",
                        "category": "practice",
                        "contributors": ["alice"],
                    }
                }
            },
        )


def test_a_leaking_manifest_stops_the_run_before_anything_is_written(
    tmp_path: Path, scanner: OutputScanner
) -> None:
    """The manifest is built and scanned before the bundle commit and before
    team/ is replaced, so a refusal here leaves the repository untouched."""
    recorder = _staged(tmp_path)
    provider = RecordingProvider(
        _EchoProvider(["clean answer", "also clean"]),
        recorder=recorder,
        context=RunContextBuilder().set_scanner(scanner).freeze(),
    )
    orchestrator = _orchestrator(
        tmp_path,
        scanner=scanner,
        recorder=recorder,
        pipeline=_TwoClusterPipeline(provider, "also clean"),
    )
    orchestrator.repo_path.mkdir(parents=True)
    _stub_repo_io(orchestrator)
    written: list[str] = []
    orchestrator._write_distill_outputs = (  # type: ignore[method-assign]
        lambda **kwargs: written.append("products")
    )
    orchestrator._load_master_manifest = lambda: {  # type: ignore[method-assign]
        "products_active": {
            "distill-old-001": {
                "relative_path": f"team/{HOME_PATH}",
                "category": "practice",
            }
        }
    }

    with pytest.raises(RedactionViolation):
        orchestrator._run_at_snapshot(0.0, "deadbeef")

    assert written == []
    assert not (tmp_path / "bundle").exists()
    assert recorder.staging is not None and not recorder.staging.exists()


def test_a_clean_manifest_is_returned_unchanged(
    tmp_path: Path, scanner: OutputScanner
) -> None:
    orchestrator = _orchestrator(tmp_path, scanner=scanner, recorder=None)

    manifest = orchestrator._master_manifest(
        run_id=RUN_ID, snapshot_sha="deadbeef", products=[], previous_manifest={}
    )

    assert manifest["last_run_id"] == RUN_ID
    assert manifest["products_active"] == {}


# --------------------------------------------------------------------------
# Regression and CLI wiring
# --------------------------------------------------------------------------


def test_without_a_recorder_nothing_changes(tmp_path: Path) -> None:
    """A run that records nothing must behave exactly as it did."""
    orchestrator = _orchestrator(tmp_path, scanner=None, recorder=None)

    _write_and_commit(orchestrator)

    assert not (tmp_path / "bundle").exists()


def test_the_legacy_record_bundle_dir_still_writes_directly(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path, scanner=None, recorder=None)
    orchestrator.record_bundle_dir = tmp_path / "legacy"

    _write_and_commit(orchestrator)

    assert (tmp_path / "legacy" / "snapshot.json").exists()


def _args(**overrides: Any) -> argparse.Namespace:
    values: dict[str, Any] = {"replay": None, "record": None, "record_dir": None}
    values.update(overrides)
    return argparse.Namespace(**values)


def test_the_cli_stages_the_bundle_under_the_run_id(tmp_path: Path) -> None:
    recorder = cli_module._record_recorder(
        _args(record_dir=tmp_path / "bundle"), run_id=RUN_ID
    )

    assert recorder is not None
    assert recorder.staging == tmp_path / f"bundle.staging-{RUN_ID}"
    assert recorder.target == tmp_path / "bundle"


def test_the_cli_refuses_both_record_flags(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="cannot be combined"):
        cli_module._record_recorder(
            _args(record=tmp_path / "flat.jsonl", record_dir=tmp_path / "bundle"),
            run_id=RUN_ID,
        )


def test_replay_records_nothing(tmp_path: Path) -> None:
    assert (
        cli_module._record_recorder(
            _args(replay=tmp_path / "bundle", record_dir=tmp_path / "out"),
            run_id=RUN_ID,
        )
        is None
    )
