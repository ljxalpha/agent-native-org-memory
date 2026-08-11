"""Strict replay of the recorded golden bundle (§9.11), the offline regression.

This is the only test that exercises a real model's output end to end -- the
parsing, the rendering, the citation lookback, the manifest and the metrics --
and it does so without a network, without a credential and without spending
anything. It is therefore also the test that has to be hard to pass by
accident: a replay that silently missed every call, or that compared a run
against itself, would look exactly like a green one.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from memory_net.distiller.cli import main
from memory_net.distiller.normalize import RunAssets, assert_normalized_equal, normalize
from memory_net.distiller.runctx import RunIdentity

REPO_ROOT = Path(__file__).resolve().parents[2]
BUNDLE_ROOT = REPO_ROOT / "tests" / "fixtures" / "golden_bundle"
GOLDEN_REPO_ASSETS = REPO_ROOT / "tests" / "fixtures" / "golden_repo"
PROFILE = "config/distill.deepseek.yaml"

VERSION_DIR = re.compile(r"^v[0-9]{3,}$")

EXPECTED_CLUSTERS = 3
EXPECTED_PRODUCTS = 3

FIXED_RUN_ID = "20260101T000000Z-fixed0001"
FIXED_NOW = "2026-01-01T00:00:00Z"


def read_current_bundle(root: Path) -> Path:
    """Resolve the ``CURRENT`` pointer to a bundle directory, or refuse.

    Every part of this is a boundary check rather than convenience. ``CURRENT``
    is a file whose contents become a path, so without the pattern and the
    containment check a one-line edit would point the replay at anything on the
    machine -- and the replay is what decides whether a change is a regression.
    """
    raw = (root / "CURRENT").read_text(encoding="utf-8")
    lines = [line for line in raw.splitlines() if line != ""]
    if len(lines) != 1:
        raise ValueError(f"CURRENT must hold exactly one line, got {len(lines)}")
    name = lines[0]
    if not VERSION_DIR.fullmatch(name):
        raise ValueError(f"CURRENT must match {VERSION_DIR.pattern}, got {name!r}")
    resolved = (root / name).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError(f"CURRENT escapes the bundle root: {name!r}")
    return resolved


def _load_builder() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "golden_replay_build_repo", GOLDEN_REPO_ASSETS / "build_repo.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


builder = _load_builder()


def _replay(repo: Path) -> int:
    return main(
        [
            "run",
            "--repo",
            str(repo),
            "--config",
            PROFILE,
            "--provider",
            "deepseek",
            "--replay",
            str(read_current_bundle(BUNDLE_ROOT)),
            "--replay-mode",
            "strict",
            "--no-push",
        ]
    )


def _run_once(tmp_path: Path, name: str) -> tuple[Path, str]:
    """One replayed run in a fresh repository; returns the repo and its run id."""
    repo = builder.build(tmp_path / name).work
    assert _replay(repo) == 0
    run_id = json.loads((repo / "manifest.json").read_text())["last_run_id"]
    return repo, str(run_id)


def _metrics(repo: Path, run_id: str) -> dict[str, Any]:
    return json.loads((repo / "distill_runs" / run_id / "metrics.json").read_text())


@pytest.fixture(scope="module")
def replayed(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str]:
    return _run_once(tmp_path_factory.mktemp("replay"), "repo")


# -- the pointer ---------------------------------------------------------


def test_current_points_at_a_bundle_that_exists() -> None:
    bundle = read_current_bundle(BUNDLE_ROOT)
    assert bundle.is_dir()
    assert (bundle / "calls.jsonl").is_file()
    assert (bundle / "record_metrics.json").is_file()


@pytest.mark.parametrize(
    ("contents", "match"),
    [
        ("v001\nv002\n", "exactly one line"),
        ("../../../etc\n", "must match"),
        ("latest\n", "must match"),
        ("v1\n", "must match"),
    ],
)
def test_current_refuses_anything_but_a_version_directory(
    tmp_path: Path, contents: str, match: str
) -> None:
    (tmp_path / "CURRENT").write_text(contents, encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        read_current_bundle(tmp_path)


# -- the replay itself ---------------------------------------------------


def test_the_replay_hits_every_recorded_call(replayed: tuple[Path, str]) -> None:
    """The assertion that separates a real replay from a silent miss."""
    metrics = _metrics(*replayed)

    assert metrics["replay_mode"] == "strict"
    assert metrics["replay_live_calls"] == 0
    assert metrics["replay_misses"] == 0
    assert metrics["replay_hits"] == EXPECTED_CLUSTERS
    assert metrics["status"] == "success"


def test_the_replay_carries_the_recording_s_provenance(
    replayed: tuple[Path, str],
) -> None:
    """Replay must not relabel where the calls came from.

    Without this the offline lane could not show that the token counts are the
    service's own -- the one fact that separates a measured run from an
    estimated one -- and that claim would only ever be checkable by paying.
    """
    metrics = _metrics(*replayed)

    assert metrics["provider_backends"] == ["deepseek"]
    assert metrics["usage_source"] == "reported"
    assert metrics["usage_source_counts"] == {
        "reported": EXPECTED_CLUSTERS,
        "estimated": 0,
        "unknown": 0,
    }


def test_the_replayed_run_is_a_clean_production_run(
    replayed: tuple[Path, str],
) -> None:
    metrics = _metrics(*replayed)

    assert metrics["citation_resolution_rate"] == 1.0
    assert metrics["parse_failure_rate"] == 0.0
    assert metrics["products"] == EXPECTED_PRODUCTS
    assert metrics["skipped_clusters"] == 0
    assert metrics["output_scanner_mode"] == "enforce"
    # The profile forbids the stand-in embedding, so this also pins that the
    # clustering the prompts were assembled from was the real one.
    assert metrics["embedding_test_only"] is False
    assert metrics["profile_path"] == PROFILE


def test_the_input_still_clusters_the_way_the_recording_assumed(
    replayed: tuple[Path, str],
) -> None:
    """The bundle was recorded against three clusters, and strict replay matches
    the assembled prompt: a different grouping produces different prompts and
    misses every call. Asserting the count names the cause directly instead of
    leaving it to be read out of a miss."""
    assert _metrics(*replayed)["clusters"] == EXPECTED_CLUSTERS


def test_the_products_come_from_the_recorded_model_text(
    replayed: tuple[Path, str],
) -> None:
    """Not merely "three files appeared": each product's summary has to be text
    the recorded response actually contains."""
    repo, run_id = replayed
    recorded = (read_current_bundle(BUNDLE_ROOT) / "calls.jsonl").read_text()
    summaries = [
        product["summary"]
        for line in recorded.splitlines()
        for product in json.loads(json.loads(line)["response"]["text"])["products"]
    ]
    assert len(summaries) == EXPECTED_PRODUCTS

    published = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((repo / "distill_runs" / run_id / "team").rglob("*.md"))
    )
    for summary in summaries:
        assert summary in published


# -- determinism ---------------------------------------------------------


def test_two_replays_are_equal_after_normalization(tmp_path: Path) -> None:
    """Two runs of one bundle, compared through the normalization algorithm.

    Run ids and clock readings differ by construction, so this is what
    "unchanged" can mean without freezing anything.
    """
    first_repo, first_id = _run_once(tmp_path, "first")
    second_repo, second_id = _run_once(tmp_path, "second")
    assert first_id != second_id

    assert_normalized_equal(
        normalize(RunAssets.collect(first_repo, run_id=first_id)),
        normalize(RunAssets.collect(second_repo, run_id=second_id)),
    )


def test_two_replays_are_byte_identical_under_a_frozen_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stronger claim, and the one that makes the weaker one meaningful.

    Normalized equality would also hold if the normalization were removing too
    much. With the run id and the clock injected there is nothing left to
    normalize, so the artifacts have to match byte for byte.
    """
    monkeypatch.setattr(
        RunIdentity,
        "system",
        classmethod(lambda cls: cls(run_id=FIXED_RUN_ID, now=lambda: FIXED_NOW)),
    )
    first_repo, first_id = _run_once(tmp_path, "frozen-a")
    second_repo, second_id = _run_once(tmp_path, "frozen-b")

    assert first_id == second_id == FIXED_RUN_ID
    assert _read_tree(first_repo, first_id) == _read_tree(second_repo, second_id)


def _read_tree(repo: Path, run_id: str) -> dict[str, bytes]:
    """The run's own published artifacts, keyed by repo-relative path.

    ``metrics.json`` is excluded and nothing else is: it carries a measured
    duration, which is a property of the machine rather than of the run's
    output. Everything a reader would call a result is in here.
    """
    run_dir = repo / "distill_runs" / run_id
    files = {
        repo / "manifest.json",
        run_dir / "manifest.json",
        run_dir / "snapshot.json",
    }
    files.update((run_dir / "team").rglob("*.md"))
    return {
        str(path.relative_to(repo)): path.read_bytes()
        for path in sorted(files)
        if path.is_file()
    }
