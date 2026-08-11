"""The hermetic golden input repository (§9.10).

The commit shas are asserted as constants. That is the point of the file: a
recorded bundle is matched against the prompts a replay assembles, and those
prompts embed the branch shas, so a sha that drifts silently invalidates the
bundle. Pinning them turns that into a failing test instead of a replay that
misses on every call for reasons nobody can see.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from memory_net.distiller.orchestrator import DistillOrchestrator
from memory_net.distiller.output_scanner import PROSE, Ruleset, scan

REPO_ROOT = Path(__file__).resolve().parents[2]
ASSETS = REPO_ROOT / "tests" / "fixtures" / "golden_repo"
SHIPPED_PROFILE = REPO_ROOT / "config" / "distill.deepseek.yaml"
PATTERNS_PATH = REPO_ROOT / "config" / "redaction_patterns.yaml"

MASTER_SHA = "64b77150b9020daa17522bee10cae2e161f6e563"
LEAF_SHAS = {
    "auth_retry": "13f8821e0ffcea4cd5ff96a0cd7062b9d8857c7c",
    "deploy_window": "e6314c11c44d80a0a64cf77d978972454c445bb0",
    "search_index": "b9cdfb4e98a5c316265348035853985c1fc9ac90",
}

EXPECTED_RECORDS = 6

RUN_RELEVANT_KEYS = (
    "profile_class",
    "llm.provider",
    "llm.model",
    "llm.max_tokens",
    "llm.max_attempts",
    "llm.pricing",
    "prompts",
    "output",
    "categories",
    "clustering",
    "output_scanner",
    "category_evidence_rules",
)
"""What the fixture profile must share with the shipped production one.

Everything that changes what a run does, and therefore what it records. Only
``llm.budget_usd`` is allowed to differ: this repository is a fixed
three-cluster fixture, and a ceiling sized for it is the tighter guard.
"""


def _load_builder() -> ModuleType:
    """Import the builder by path: it lives under ``tests/fixtures``, which is
    a data directory rather than an importable package."""
    spec = importlib.util.spec_from_file_location(
        "golden_repo_build_repo", ASSETS / "build_repo.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    # No bytecode beside a fixture. A `.pyc` embeds the absolute path of the
    # source it was compiled from, so importing this file writes one machine's
    # username into the fixture tree -- gitignored, but present in any archive
    # or support bundle taken from a working copy.
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


builder = _load_builder()


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Any:
    return builder.build(tmp_path_factory.mktemp("golden") / "repo")


def _dig(config: dict[str, Any], dotted: str) -> Any:
    value: Any = config
    for part in dotted.split("."):
        value = value[part]
    return value


def test_the_commit_shas_are_the_pinned_ones(built: Any) -> None:
    assert built.master_sha == MASTER_SHA
    assert built.leaf_shas == LEAF_SHAS


def test_two_builds_agree(tmp_path: Path, built: Any) -> None:
    """Reproducible, not merely pinned: the same assets on the same machine
    must produce the same tree twice, or the constant above is an accident."""
    again = builder.build(tmp_path / "again")
    assert again.master_sha == built.master_sha
    assert again.leaf_shas == built.leaf_shas


def test_the_remote_is_a_sibling_and_not_inside_the_working_clone(
    built: Any,
) -> None:
    assert built.remote != built.work
    assert built.work not in built.remote.parents
    assert (built.remote / "HEAD").exists()


def test_the_orchestrator_reads_every_record(built: Any) -> None:
    """The builder's real contract: not "it made commits" but "the distiller
    accepts them". A leaf whose manifest and frontmatter disagree is dropped
    silently into a quarantine counter, and the run still reports success."""
    orchestrator = DistillOrchestrator(repo_path=built.work, pipeline=None)  # type: ignore[arg-type]
    records = orchestrator._load_leaf_records()

    assert len(records) == EXPECTED_RECORDS
    assert orchestrator.incompatible_leaves == 0
    assert orchestrator.quarantined_records == 0
    assert len(orchestrator.leaf_refs) == len(LEAF_SHAS)


MOCK_PROFILE = "config/distill.mock.yaml"

MOCK_LANE_DIFFERENCES = {"profile_class", "llm.provider", "llm.model"}
"""The only fields the two lanes of the equivalence contract may differ in."""


def test_the_mock_lane_profile_differs_only_in_which_backend_it_names() -> None:
    """The contract compares two lanes over the same records. If the profiles
    differ in anything else -- the categories, the evidence rules, the
    embedding backend -- a difference in the invariants describes the fixture
    rather than the backends."""
    mock = yaml.safe_load((ASSETS / MOCK_PROFILE).read_text(encoding="utf-8"))
    production = yaml.safe_load(
        (ASSETS / builder.PROFILE_PATH).read_text(encoding="utf-8")
    )
    for dotted in RUN_RELEVANT_KEYS:
        if dotted in MOCK_LANE_DIFFERENCES:
            continue
        assert _dig(mock, dotted) == _dig(production, dotted), dotted
    assert _dig(mock, "llm.budget_usd") == _dig(production, "llm.budget_usd")
    assert _dig(mock, "profile_class") == "development"
    assert _dig(mock, "llm.provider") == "mock"


def test_every_profile_asset_reaches_the_frozen_snapshot(built: Any) -> None:
    """A profile that exists as a file but was left out of the commit is
    unreadable through `git show`, and the run says only "cannot resolve
    config" -- long after the flag naming it was accepted."""
    committed = set(
        builder._git(built.work, "ls-tree", "-r", "--name-only", built.master_sha)
        .strip()
        .splitlines()
    )
    for relative in builder._asset_tree("config", "prompts"):
        assert relative in committed


def test_the_fixture_profile_matches_the_shipped_production_one() -> None:
    fixture = yaml.safe_load(
        (ASSETS / builder.PROFILE_PATH).read_text(encoding="utf-8")
    )
    shipped = yaml.safe_load(SHIPPED_PROFILE.read_text(encoding="utf-8"))
    for dotted in RUN_RELEVANT_KEYS:
        assert _dig(fixture, dotted) == _dig(shipped, dotted), dotted
    # And the one permitted difference is a real one, so that this test would
    # notice if the fixture silently became a copy.
    assert fixture["llm"]["budget_usd"] < shipped["llm"]["budget_usd"]


SCANNED_ASSETS = ("leaves", "prompts", "manifest.json", "bootstrap.json")
"""The data assets, which are the ones a run sends and commits.

``build_repo.py`` and the rule file are excluded and each has its own check
below instead. Scanning a rule file with its own rules only ever reports the
rules; scanning the builder reports its fixture git identity, which is a
``.invalid`` placeholder by design.
"""


def test_the_patterns_copy_is_byte_identical_to_the_shipped_rules() -> None:
    """One corpus, copied rather than forked: a fixture with its own rules
    would let a golden bundle be recorded under a gate nobody else runs."""
    assert (ASSETS / "config" / "redaction_patterns.yaml").read_bytes() == (
        PATTERNS_PATH.read_bytes()
    )


def test_the_builder_uses_a_placeholder_git_identity() -> None:
    """The one address in these assets, and it must stay unroutable."""
    for key in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        assert builder.FIXTURE_IDENTITY[key].endswith("@example.invalid")


def test_the_assets_are_clean_under_the_output_scanner() -> None:
    """These files ship in a public repository. The same rules that gate model
    output gate them, so that "reviewed by eye" is not the only check."""
    ruleset = Ruleset.from_yaml(PATTERNS_PATH)
    candidates: list[Path] = []
    for name in SCANNED_ASSETS:
        target = ASSETS / name
        candidates.extend(sorted(target.rglob("*")) if target.is_dir() else [target])
    candidates.append(ASSETS / builder.PROFILE_PATH)
    hits = []
    for path in sorted(candidates):
        if not path.is_file():
            continue
        hits.extend(
            scan(
                path.read_text(encoding="utf-8"),
                field_kind=PROSE,
                field_path=str(path.relative_to(ASSETS)),
                ruleset=ruleset,
            )
        )
    assert [f"{hit.rule}@{hit.field_path}" for hit in hits] == []


def test_the_leaf_assets_carry_no_personal_memory() -> None:
    """Synthetic content is a requirement, not a convention: these records are
    what a live recording sends to a third-party endpoint and then commits."""
    for name in builder.LEAF_NAMES:
        leaf = json.loads((ASSETS / "leaves" / f"{name}.json").read_text("utf-8"))
        assert leaf["node_id"].endswith("-claude-01")
        for record in leaf["records"]:
            assert record["memory_id"].startswith("golden-")
            assert record["source_locator"].startswith("memory/shared/")


def test_preflight_refuses_a_snapshot_missing_a_referenced_resource(
    tmp_path: Path,
) -> None:
    """The check that has to fail *before* a billed run, not during one.

    The scanner rule file is read from the frozen snapshot at the moment the
    run builds its scanner, which is after the profile has been accepted. A
    missing one there stops a run that has already started paying.
    """
    result = builder.build(tmp_path / "trimmed")
    builder._git(result.work, "rm", "-q", "config/redaction_patterns.yaml")
    builder._git(result.work, "commit", "--quiet", "-m", "drop the rules")
    trimmed = builder.BuildResult(
        work=result.work,
        remote=result.remote,
        master_sha=builder._git(result.work, "rev-parse", "HEAD").strip(),
        leaf_shas=result.leaf_shas,
    )
    with pytest.raises(Exception, match="redaction_patterns"):
        builder.preflight(trimmed)
