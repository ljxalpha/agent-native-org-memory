"""The bundle's transaction mechanics (§5.5.11).

Every assertion here is against the filesystem rather than a return value: the
property being tested is "what is on disk after this", and a method that returns
a path it never created would pass any weaker check.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memory_net.distiller.llm.base import LLMRequest, LLMResponse
from memory_net.distiller.llm.recording import UNSCANNED, BundleRecorder

RUN_ID = "20260810T101010Z-a1b2c3d4"


def _record(recorder: BundleRecorder, text: str = "answer") -> None:
    recorder.record(
        LLMRequest(system_prompt="s", user_prompt="u", model="m"),
        LLMResponse(text=text, model="m"),
        scanned=UNSCANNED,
    )


def test_writes_land_in_staging_beside_the_target(tmp_path: Path) -> None:
    target = tmp_path / "bundle"
    recorder = BundleRecorder.staged_bundle(target, run_id=RUN_ID)

    _record(recorder)

    assert recorder.staging is not None
    assert recorder.staging.parent == target.parent  # same filesystem, atomic rename
    assert recorder.staging.name == f"bundle.staging-{RUN_ID}"
    assert (recorder.staging / "calls.jsonl").exists()
    assert not target.exists()  # the target is not created before the commit


def test_commit_publishes_and_leaves_no_staging(tmp_path: Path) -> None:
    target = tmp_path / "bundle"
    recorder = BundleRecorder.staged_bundle(target, run_id=RUN_ID)
    _record(recorder)

    published = recorder.commit()

    assert published == target
    assert (target / "calls.jsonl").read_text(encoding="utf-8").strip()
    assert recorder.staging is not None and not recorder.staging.exists()


def test_a_zero_call_run_still_commits_a_loadable_bundle(tmp_path: Path) -> None:
    target = tmp_path / "bundle"
    recorder = BundleRecorder.staged_bundle(target, run_id=RUN_ID)

    recorder.commit()

    assert (target / "calls.jsonl").exists()
    assert (target / "requests").is_dir()


def test_abort_leaves_neither_path(tmp_path: Path) -> None:
    target = tmp_path / "bundle"
    recorder = BundleRecorder.staged_bundle(target, run_id=RUN_ID)
    _record(recorder)

    recorder.abort()

    assert not target.exists()
    assert recorder.staging is not None and not recorder.staging.exists()


def test_abort_is_idempotent(tmp_path: Path) -> None:
    recorder = BundleRecorder.staged_bundle(tmp_path / "bundle", run_id=RUN_ID)
    _record(recorder)

    recorder.abort()
    recorder.abort()

    assert not (tmp_path / "bundle").exists()


def test_abort_after_commit_does_not_delete_the_published_bundle(
    tmp_path: Path,
) -> None:
    """A caller wrapping the run in try/finally aborts on the way out either
    way; that must not undo a successful publish."""
    target = tmp_path / "bundle"
    recorder = BundleRecorder.staged_bundle(target, run_id=RUN_ID)
    _record(recorder)
    recorder.commit()

    recorder.abort()

    assert (target / "calls.jsonl").exists()


def test_a_leftover_staging_path_is_refused(tmp_path: Path) -> None:
    """It may belong to a run that is still going."""
    target = tmp_path / "bundle"
    (tmp_path / f"bundle.staging-{RUN_ID}").mkdir()

    with pytest.raises(FileExistsError, match="staging"):
        BundleRecorder.staged_bundle(target, run_id=RUN_ID)


def test_a_failing_precheck_keeps_staging_and_publishes_nothing(
    tmp_path: Path,
) -> None:
    target = tmp_path / "bundle"
    recorder = BundleRecorder.staged_bundle(target, run_id=RUN_ID)
    _record(recorder)
    seen: list[Path] = []

    def precheck(path: Path) -> None:
        seen.append(path)
        raise RuntimeError("staged bundle is not clean")

    with pytest.raises(RuntimeError, match="not clean"):
        recorder.commit(precheck=precheck)

    assert seen == [recorder.staging]
    assert not target.exists()
    assert recorder.staging is not None and recorder.staging.exists()


def test_an_existing_target_is_refused_rather_than_replaced(tmp_path: Path) -> None:
    target = tmp_path / "bundle"
    (target / "requests").mkdir(parents=True)
    recorder = BundleRecorder.staged_bundle(target, run_id=RUN_ID)
    _record(recorder)

    with pytest.raises(FileExistsError, match="target"):
        recorder.commit()


def test_committing_twice_is_refused(tmp_path: Path) -> None:
    recorder = BundleRecorder.staged_bundle(tmp_path / "bundle", run_id=RUN_ID)
    _record(recorder)
    recorder.commit()

    with pytest.raises(RuntimeError, match="already committed"):
        recorder.commit()


def test_a_run_id_is_required_for_staging(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="run id"):
        BundleRecorder.staged_bundle(tmp_path / "bundle", run_id="")


# --------------------------------------------------------------------------
# The flat --record file
# --------------------------------------------------------------------------


def test_the_flat_file_is_staged_too(tmp_path: Path) -> None:
    target = tmp_path / "calls.jsonl"
    recorder = BundleRecorder.staged_file(target, run_id=RUN_ID)

    _record(recorder)

    assert not target.exists()
    assert recorder.staging is not None and recorder.staging.is_file()


def test_the_flat_file_is_published_on_commit(tmp_path: Path) -> None:
    target = tmp_path / "calls.jsonl"
    recorder = BundleRecorder.staged_file(target, run_id=RUN_ID)
    _record(recorder)

    recorder.commit()

    assert target.read_text(encoding="utf-8").strip()
    assert recorder.staging is not None and not recorder.staging.exists()


def test_an_aborted_flat_recording_leaves_the_target_absent(tmp_path: Path) -> None:
    target = tmp_path / "calls.jsonl"
    recorder = BundleRecorder.staged_file(target, run_id=RUN_ID)
    _record(recorder)

    recorder.abort()

    assert not target.exists()
    assert recorder.staging is not None and not recorder.staging.exists()


# --------------------------------------------------------------------------
# The direct-write path
# --------------------------------------------------------------------------


def test_a_recorder_without_a_run_id_writes_straight_through(tmp_path: Path) -> None:
    """Regression: the pre-staging behaviour is what an unstaged recorder keeps."""
    target = tmp_path / "calls.jsonl"
    recorder = BundleRecorder(target)

    _record(recorder)

    assert target.read_text(encoding="utf-8").strip()
    assert recorder.staging is None
    assert recorder.commit() == target
