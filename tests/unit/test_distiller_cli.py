import json
import platform
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

from memory_net.distiller.cli import (
    _build_providers,
    _category_names,
    _distill_config,
    _freeze_master_snapshot,
    _min_contributors_by_category,
    _preflight,
    _replay_mode,
    _resolve_prompt,
    _setup_runtime,
    main,
)
from memory_net.distiller.llm.hermes_tool import HermesToolProvider
from memory_net.distiller.llm.mock import MockLLMProvider
from memory_net.distiller.llm.recording import RecordingProvider, ReplayProvider


def test_replay_mode_labels_run_for_metrics() -> None:
    assert _replay_mode(Namespace(replay=None)) == "live"
    assert (
        _replay_mode(
            Namespace(replay=Path("x"), replay_mode="prompt-only", replay_strict=True)
        )
        == "prompt-only"
    )
    assert (
        _replay_mode(Namespace(replay=Path("x"), replay_mode=None, replay_strict=True))
        == "strict"
    )
    assert (
        _replay_mode(Namespace(replay=Path("x"), replay_mode=None, replay_strict=False))
        == "prompt-only"
    )


def test_resolve_prompt_binds_relative_path_to_repo_even_when_missing(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    assert _resolve_prompt(repo, Path("prompts/distill_system.md")) == (
        repo / "prompts" / "distill_system.md"
    )


def test_resolve_prompt_rejects_absolute_path_outside_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    prompt = tmp_path / "system.md"

    with pytest.raises(SystemExit, match="inside repo"):
        _resolve_prompt(repo, prompt)


def test_distill_config_reads_frozen_snapshot(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    repo = tmp_path / "repo"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(tmp_path, "clone", str(remote), str(seed))
    _configure(seed)
    (seed / "config").mkdir()
    (seed / "config" / "distill.yaml").write_text(
        "llm:\n  provider: mock\n",
        encoding="utf-8",
    )
    (seed / "bootstrap_state.json").write_text(
        '{"schema_version":1,"compatible_code_range":">=0.1.0,<0.2.0",'
        '"compatible_build_api_range":">=1,<2",'
        '"manifest_schema_version":1,"record_frontmatter_schema_version":1,'
        '"product_frontmatter_schema_version":1,"health_schema_version":1,'
        '"owner_schema_version":1}',
        encoding="utf-8",
    )
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "push", "origin", "master")
    _git(tmp_path, "clone", str(remote), str(repo))
    _configure(repo)

    snapshot = _freeze_master_snapshot(repo)
    (seed / "config" / "distill.yaml").write_text(
        "llm:\n  provider: anthropic\n",
        encoding="utf-8",
    )
    _git(seed, "add", "config/distill.yaml")
    _git(seed, "commit", "-m", "advance config")
    _git(seed, "push", "origin", "master")

    assert _distill_config(repo, snapshot)["llm"]["provider"] == "mock"


def test_category_names_prefers_configured_categories_over_partial_rules() -> None:
    assert _category_names(
        {
            "categories": ["auth", "risk"],
            "category_evidence_rules": {"auth": {"min_citations": 2}},
        }
    ) == ["auth", "risk"]


def test_min_contributors_by_category_reads_evidence_rules() -> None:
    assert _min_contributors_by_category(
        {
            "category_evidence_rules": {
                "common_knowledge": {"min_contributors": 2},
                "best_practices": {"min_citations": 1},
            }
        }
    ) == {"common_knowledge": 2}


def test_distiller_run_refuses_to_reset_unpushed_local_master(
    tmp_path: Path,
) -> None:
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    repo = tmp_path / "repo"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(tmp_path, "clone", str(remote), str(seed))
    _configure(seed)
    _write_distiller_seed(seed)
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "push", "origin", "master")
    _git(tmp_path, "clone", str(remote), str(repo))
    _configure(repo)
    (repo / "local-only.txt").write_text("local\n", encoding="utf-8")
    _git(repo, "add", "local-only.txt")
    _git(repo, "commit", "-m", "local only")
    before = _git(repo, "rev-parse", "master")

    with pytest.raises(RuntimeError, match="unpushed commits"):
        main(["run", "--repo", str(repo)])

    assert _git(repo, "rev-parse", "master") == before


def test_runtime_config_reads_frozen_snapshot(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    repo = tmp_path / "repo"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(tmp_path, "clone", str(remote), str(seed))
    _configure(seed)
    _write_distiller_seed(seed)
    (seed / "config" / "platform.yaml").write_text(
        f"runtime:\n  supported_os: ['{platform.system()}']\n",
        encoding="utf-8",
    )
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "push", "origin", "master")
    _git(tmp_path, "clone", str(remote), str(repo))
    _configure(repo)
    snapshot = _freeze_master_snapshot(repo)
    (repo / "config" / "platform.yaml").write_text(
        "runtime:\n  supported_os: ['UnsupportedOS']\n",
        encoding="utf-8",
    )

    _setup_runtime(Namespace(repo=repo), snapshot_sha=snapshot)


def test_distiller_run_writes_record_bundle(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    repo = tmp_path / "repo"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(tmp_path, "clone", str(remote), str(seed))
    _configure(seed)
    _write_distiller_seed(seed)
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "push", "origin", "master")
    _git(tmp_path, "clone", str(remote), str(repo))
    _configure(repo)
    bundle = tmp_path / "record_runs" / "run"

    assert (
        main(["run", "--repo", str(repo), "--no-push", "--record-dir", str(bundle)])
        == 0
    )

    record_metrics = json.loads(
        (bundle / "record_metrics.json").read_text(encoding="utf-8")
    )
    assert record_metrics["replay_mode"] == "live"
    assert record_metrics["calls"] == 0
    assert (bundle / "cluster_assignment.json").exists()
    assert (bundle / "snapshot.json").exists()
    # A zero-call bundle still carries an (empty) calls.jsonl
    # so it stays self-consistent and replayable.
    assert (bundle / "calls.jsonl").exists()
    assert (bundle / "calls.jsonl").read_text(encoding="utf-8") == ""


def test_distiller_replays_zero_call_bundle(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    repo = tmp_path / "repo"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(tmp_path, "clone", str(remote), str(seed))
    _configure(seed)
    _write_distiller_seed(seed)
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "push", "origin", "master")
    _git(tmp_path, "clone", str(remote), str(repo))
    _configure(repo)
    bundle = tmp_path / "record_runs" / "run"

    assert (
        main(["run", "--repo", str(repo), "--no-push", "--record-dir", str(bundle)])
        == 0
    )
    # Replaying the empty bundle directory must not raise on construction and must
    # complete the (zero-cluster) run instead of reissuing live calls.
    assert main(["run", "--repo", str(repo), "--no-push", "--replay", str(bundle)]) == 0


def test_build_providers_shares_recorder_across_primary_and_fallback(
    tmp_path: Path,
) -> None:
    # With a fallback configured, --record-dir wraps BOTH the
    # primary and fallback in RecordingProviders that share one BundleRecorder, so
    # both calls land in the same ordered bundle.
    args = Namespace(
        replay=None,
        record=None,
        record_dir=tmp_path / "bundle",
        model=None,
    )

    provider, fallback = _build_providers(args, "mock", "mock", {})

    assert isinstance(provider, RecordingProvider)
    assert isinstance(fallback, RecordingProvider)
    assert provider.recorder is fallback.recorder


def test_build_providers_replays_fallback_through_shared_cursor(
    tmp_path: Path,
) -> None:
    # On replay the primary and fallback are DISTINCT
    # ReplayProviders that share one ordered cursor (so lane-specific live-on-miss
    # is possible), and the fallback is None when none is configured.
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "calls.jsonl").write_text("", encoding="utf-8")
    args = Namespace(
        replay=bundle,
        replay_strict=True,
        replay_mode="strict",
        model=None,
    )

    provider, fallback = _build_providers(args, "mock", "mock", {})
    assert isinstance(provider, ReplayProvider)
    assert isinstance(fallback, ReplayProvider)
    assert fallback is not provider
    assert fallback._source is provider._source

    provider_no_fb, fallback_none = _build_providers(args, "mock", "none", {})
    assert isinstance(provider_no_fb, ReplayProvider)
    assert fallback_none is None


def test_build_providers_replay_fallback_lane_uses_configured_provider(
    tmp_path: Path,
) -> None:
    # Under allow-live-on-miss the fallback lane's live-on-miss
    # provider is the CONFIGURED fallback (hermes_tool), not the primary (mock).
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "calls.jsonl").write_text("", encoding="utf-8")
    args = Namespace(
        replay=bundle,
        replay_strict=False,
        replay_mode="allow-live-on-miss",
        model=None,
    )

    provider, fallback = _build_providers(args, "mock", "hermes_tool", {})

    assert isinstance(provider, ReplayProvider)
    assert isinstance(fallback, ReplayProvider)
    assert isinstance(provider.fallback_provider, MockLLMProvider)
    assert isinstance(fallback.fallback_provider, HermesToolProvider)


def _configure(repo: Path) -> None:
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")


def _write_distiller_seed(repo: Path) -> None:
    (repo / "config").mkdir()
    (repo / "prompts").mkdir()
    (repo / "config" / "distill.yaml").write_text("llm:\n  provider: mock\n")
    # The shipped rules, verbatim: a recording run refuses to start without a
    # ruleset, and a seed carrying its own abbreviated copy would be testing a
    # corpus nothing else in the project uses.
    (repo / "config" / "redaction_patterns.yaml").write_text(
        Path("config/redaction_patterns.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (repo / "prompts" / "distill_system.md").write_text("system", encoding="utf-8")
    (repo / "prompts" / "distill_user.md").write_text(
        "records {{ records_json }}",
        encoding="utf-8",
    )
    (repo / "bootstrap_state.json").write_text(
        '{"schema_version":1,"compatible_code_range":">=0.1.0,<0.2.0",'
        '"compatible_build_api_range":">=1,<2",'
        '"manifest_schema_version":1,"record_frontmatter_schema_version":1,'
        '"product_frontmatter_schema_version":1,"health_schema_version":1,'
        '"owner_schema_version":1}',
        encoding="utf-8",
    )
    (repo / "manifest.json").write_text(
        '{"schema_version":"memory-net/master-manifest/v1",'
        '"compatible_leaf_range":">=0.1.0,<0.2.0","products_active":{}}',
        encoding="utf-8",
    )


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


def test_preflight_skips_credentials_for_an_offline_replay() -> None:
    """The regression that has to stay green on a public runner with no
    secrets. `allow-live-on-miss` is excluded because it can still go live."""
    for mode in ("strict", "prompt-only"):
        _preflight(
            Namespace(provider="deepseek", replay=Path("bundle"), replay_mode=mode)
        )


def test_preflight_still_demands_credentials_when_a_replay_may_go_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)

    with pytest.raises(SystemExit, match="credentials are absent"):
        _preflight(
            Namespace(
                provider="deepseek",
                replay=Path("bundle"),
                replay_mode="allow-live-on-miss",
            )
        )
    with pytest.raises(SystemExit, match="credentials are absent"):
        _preflight(Namespace(provider="deepseek", replay=None, replay_mode=None))
