from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from memory_net import __version__
from memory_net.distiller.categories import canonical_category
from memory_net.distiller.clustering.embedder import FastEmbedder
from memory_net.distiller.clustering.policy import (
    DEFAULT_MODEL_NAME,
    TEST_CHEAP,
    EmbeddingPolicy,
)
from memory_net.distiller.golden_eval import evaluate_golden_fixture
from memory_net.distiller.llm.anthropic_sdk import (
    DEFAULT_ANTHROPIC_MODEL,
    AnthropicSDKProvider,
)
from memory_net.distiller.llm.base import LLMProvider
from memory_net.distiller.llm.budgeted import BudgetedProvider
from memory_net.distiller.llm.hermes_tool import HermesToolProvider
from memory_net.distiller.llm.ledger import AttemptLedger
from memory_net.distiller.llm.mock import MockLLMProvider
from memory_net.distiller.llm.openai_compat import (
    API_KEY_ENV,
    BASE_URL_ENV,
    DEFAULT_MODEL,
    OpenAICompatProvider,
)
from memory_net.distiller.llm.recording import (
    BundleRecorder,
    RecordingProvider,
    ReplayProvider,
    ScanningProvider,
    recorded_backends,
)
from memory_net.distiller.orchestrator import DistillOrchestrator
from memory_net.distiller.output_scanner import (
    MODE_ENV_VAR,
    OutputScanner,
    Ruleset,
    ScannerConfigError,
    resolve_scanner_settings,
)
from memory_net.distiller.pipeline import DistillationPipeline
from memory_net.distiller.profile import (
    OUTPUT_SCANNER_MODES,
    PRODUCTION,
    ProfileError,
    ProfileProvenance,
    profile_class,
    validate_effective_provider,
    validate_profile,
)
from memory_net.distiller.runctx import (
    RunBudget,
    RunContext,
    RunContextBuilder,
    RunIdentity,
)
from memory_net.leaf.identity import read_host
from memory_net.shared.git_io import GitError, GitRepo
from memory_net.shared.observability import setup_logging
from memory_net.shared.schema import validate_bootstrap_state
from memory_net.shared.util.paths import safe_posix_relative_path

# Renamed here rather than aliased at the import, so that "which default"
# stays readable at every use site while the import block holds no aliases.
DEFAULT_EMBEDDING_MODEL = DEFAULT_MODEL_NAME
DEFAULT_DEEPSEEK_MODEL = DEFAULT_MODEL

BUILD_API = 1
MOCK_MODEL = "mock-memory-distiller"
LOGGER = logging.getLogger(__name__)


PROVIDER_CHOICES = ["mock", "anthropic", "hermes_tool", "deepseek"]
"""Selectable distillation backends. ``deepseek`` is the production one (§5.3.3);
``hermes_tool`` is the experimental out-of-process entry point (§5.3.2)."""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memory-net-distiller")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run")
    run_parser.add_argument("--repo", required=True, type=Path)
    run_parser.add_argument(
        "--config",
        default=None,
        metavar="REPO_RELATIVE_PATH",
        help=(
            "distillation profile to run, as a path inside the repo "
            f"(default {DEFAULT_PROFILE_PATH}; env {PROFILE_ENV_VAR}). "
            "Read from the frozen master snapshot, never from the working tree."
        ),
    )
    run_parser.add_argument("--provider", choices=PROVIDER_CHOICES, default=None)
    run_parser.add_argument(
        "--fallback-provider",
        choices=["none", *PROVIDER_CHOICES],
        default=None,
    )
    run_parser.add_argument("--model", default=None)
    run_parser.add_argument("--max-tokens", type=int, default=None)
    run_parser.add_argument("--system-prompt", type=Path)
    run_parser.add_argument("--user-prompt", type=Path)
    run_parser.add_argument("--record", type=Path)
    run_parser.add_argument("--record-dir", type=Path)
    run_parser.add_argument("--replay", type=Path)
    run_parser.add_argument(
        "--strict", dest="replay_strict", action="store_true", default=True
    )
    run_parser.add_argument("--loose", dest="replay_strict", action="store_false")
    run_parser.add_argument(
        "--replay-mode",
        choices=["strict", "prompt-only", "allow-live-on-miss"],
        default=None,
    )
    run_parser.add_argument(
        "--output-scanner-mode",
        choices=list(OUTPUT_SCANNER_MODES),
        default=None,
        help=(
            "output scanner mode for this run (non-production profiles only; "
            f"env {MODE_ENV_VAR}). A production profile rejects this flag "
            "outright, whatever its value."
        ),
    )
    run_parser.add_argument("--dry-run", action="store_true")
    run_parser.add_argument("--no-push", action="store_true")
    run_parser.add_argument("--eval", dest="eval_fixture", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    _enforce_supported_platform()
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "run":
        # Before the fetch, because these need no profile and every one of them
        # is a reason the run cannot possibly succeed.
        _preflight(args)
        snapshot_sha = _freeze_master_snapshot(args.repo)
        _setup_runtime(args, snapshot_sha=snapshot_sha)
        _validate_bootstrap_state_from_repo(args.repo, snapshot_sha)
        profile = _selected_profile(args)
        profile_bytes = _read_profile_bytes(
            args.repo, snapshot_sha, profile=profile.path
        )
        distill_config = _parse_profile(profile_bytes, profile.path)
        _validated_profile(distill_config, profile.path)
        provider_name = _provider_name(args, distill_config)
        fallback_name = _fallback_provider_name(args, distill_config)
        # Re-checked against the *resolved* names, because --provider overrides
        # the profile: a production profile invoked with --provider mock would
        # otherwise publish fabricated products as team knowledge. Before the
        # probe on purpose -- probing loads a real embedding model, and a run
        # already known to be misconfigured must not pay for that.
        try:
            validate_effective_provider(
                profile_class(distill_config),
                provider=provider_name,
                fallback=fallback_name,
                replay=bool(args.replay),
                # The bundle's own per-call provenance, so a production
                # profile can replay a recording of itself while still
                # refusing one that came from the mock.
                replay_backends=(
                    recorded_backends(args.replay) if args.replay else None
                ),
            )
        except ProfileError as exc:
            raise SystemExit(f"invalid profile {profile.path}: {exc}") from exc
        embedding = _embedding_policy(distill_config)
        # After the profile (which is where the backend is named) and before
        # any leaf branch is read or any model is called. A backend that cannot
        # load is fatal, and discovering it later means the run has already
        # done the expensive, and possibly billed, part for nothing.
        embedding.probe()
        # One assembly, one freeze, before any consumer exists (§5.5.4). The
        # run id has to precede its consumers: anything named after it could
        # not be constructed otherwise.
        identity = RunIdentity.system()
        if args.record and profile_class(distill_config) == PRODUCTION:
            # A flat recording never passes the commit-time full rescan that a
            # bundle does, so it is not an acceptable production artifact.
            raise SystemExit(
                "a production profile cannot use --record; record a bundle with "
                "--record-dir, which is scanned in full before it is published"
            )
        context = (
            RunContextBuilder()
            .set_identity(identity)
            .set_budget(_run_budget(distill_config))
            # Named after the run id, so it is built here rather than inside
            # _build_providers, and handed to the orchestrator to commit or
            # abort as one transaction.
            .set_recorder(_record_recorder(args, run_id=identity.run_id))
            # Built here and read from the context by every consumer, so a run
            # cannot end up with two scanners disagreeing about the mode.
            .set_scanner(
                _output_scanner(
                    args,
                    distill_config,
                    repo_path=args.repo,
                    snapshot_sha=snapshot_sha,
                )
            )
            # None when no profile file was found at all, which keeps the run
            # artifacts byte-identical to what they were before provenance
            # existed rather than recording a hash of nothing.
            .set_profile(
                None
                if profile_bytes is None
                else ProfileProvenance.of(
                    profile_path=profile.path,
                    raw=profile_bytes,
                    master_snapshot_sha=snapshot_sha,
                )
            )
            .freeze()
        )
        provider, fallback_provider = _build_providers(
            args,
            provider_name,
            fallback_name,
            distill_config,
            budget=context.budget,
            context=context,
        )
        pipeline = DistillationPipeline(
            context=context,
            provider=provider,
            fallback_provider=fallback_provider,
            # The clusterer reads its policy back off this embedder, so the
            # vectors and the grouping cannot end up on different backends.
            embedder=FastEmbedder(policy=embedding),
            system_prompt_path=_resolve_prompt(
                args.repo,
                _prompt_path(
                    args.system_prompt,
                    distill_config,
                    "system_template_path",
                    Path("prompts/distill_system.md"),
                ),
            ),
            user_prompt_path=_resolve_prompt(
                args.repo,
                _prompt_path(
                    args.user_prompt,
                    distill_config,
                    "user_template_path",
                    Path("prompts/distill_user.md"),
                ),
            ),
            model=_pipeline_model(args, provider_name, distill_config),
            max_tokens=_max_tokens(args, distill_config),
            cost_budget_usd=_cost_budget_usd(distill_config),
            input_cost_per_mtok_usd=_input_cost_per_mtok_usd(distill_config),
            output_cost_per_mtok_usd=_output_cost_per_mtok_usd(distill_config),
            dbscan_eps=_dbscan_eps(distill_config),
            dbscan_min_samples=_dbscan_min_samples(distill_config),
            categories=_category_names(distill_config),
            min_citations_by_category=_min_citations_by_category(distill_config),
            min_contributors_by_category=_min_contributors_by_category(distill_config),
        )
        result = DistillOrchestrator(
            context=context,
            repo_path=args.repo,
            pipeline=pipeline,
            dry_run=args.dry_run,
            no_push=args.no_push,
            publish_branch_template=_publish_branch_template(distill_config),
            frozen_master_snapshot_sha=snapshot_sha,
            record_bundle_dir=args.record_dir,
            replay_mode=_replay_mode(args),
        ).run()
        response: dict[str, Any] = {
            "run_id": result.run_id,
            "input_records": result.input_records,
            "products": result.products,
            "manifest_path": str(result.manifest_path),
        }
        if args.eval_fixture is not None:
            response["golden_eval"] = evaluate_golden_fixture(args.eval_fixture)
        print(json.dumps(response, sort_keys=True))
        return 0
    parser.error("unknown command")
    return 2


def _build_providers(
    args: argparse.Namespace,
    provider_name: str,
    fallback_name: str,
    config: dict[str, Any],
    *,
    budget: RunBudget | None = None,
    context: RunContext | None = None,
) -> tuple[LLMProvider, LLMProvider | None]:
    """Build the (primary, fallback) providers, sharing record/replay state.

    On record the primary and fallback wrap one shared ``BundleRecorder`` so both
    land in a single ordered bundle (RECORD-001). On replay they share one
    ``ReplayProvider`` cursor so a recorded primary->fallback sequence replays in
    order; the fallback is attached only when one is configured, otherwise a
    recorded primary parse failure with no fallback would consume the next
    cluster's record.

    ``budget`` follows the same rule as the recorder: run-level state exists once
    and both lanes get that one instance, so the primary cannot spend a ceiling
    the fallback then spends again.
    """
    if args.replay:
        replay = _replay_provider(args, provider_name, budget=budget, context=context)
        if fallback_name == "none":
            return replay, None
        # Share one ordered cursor, but give the fallback lane its own
        # live-on-miss provider (the configured fallback) so allow-live-on-miss
        # misses do not fall through to the primary provider.
        fallback_live = (
            # Wrapped like any other live lane: a replay *miss* is a real call
            # with a real prompt, and the gate cannot be a property of whether
            # the bundle happened to contain the answer.
            _wrap_record(
                _base_provider(
                    fallback_name,
                    args.model or _fallback_model(config),
                    budget=budget,
                ),
                None,
                context,
            )
            if args.replay_mode == "allow-live-on-miss"
            else None
        )
        return replay, replay.sharing_cursor(fallback_provider=fallback_live)
    # Built by main() before the freeze, because the staging directory is named
    # after the run id. Taking it off the context is what lets the orchestrator
    # commit or abort the very recorder these lanes wrote to.
    recorder = context.recorder if context is not None else _record_recorder(args)
    primary = _wrap_record(
        _base_provider(provider_name, args.model, budget=budget), recorder, context
    )
    if fallback_name == "none":
        return primary, None
    fallback_model = _fallback_model(config)
    fallback = _wrap_record(
        _base_provider(fallback_name, args.model or fallback_model, budget=budget),
        recorder,
        context,
    )
    return primary, fallback


def _replay_provider(
    args: argparse.Namespace,
    provider_name: str,
    *,
    budget: RunBudget | None = None,
    context: RunContext | None = None,
) -> ReplayProvider:
    live_on_miss = (
        # Same wrapper as a live run: on a miss this provider sends the prompt.
        _wrap_record(
            _base_provider(provider_name, args.model, budget=budget), None, context
        )
        if args.replay_mode == "allow-live-on-miss"
        else None
    )
    return ReplayProvider(
        args.replay,
        strict=args.replay_strict,
        mode=args.replay_mode,
        fallback_provider=live_on_miss,
    )


def _output_scanner(
    args: argparse.Namespace,
    config: dict[str, Any],
    *,
    repo_path: Path,
    snapshot_sha: str | None,
) -> OutputScanner | None:
    """The run's one scanner, or ``None`` when this repo has no rules (§5.5.9).

    ``None`` is reachable only when the profile declares no ``output_scanner``
    block *and* the default rule file is absent from the snapshot -- a
    repository that has nothing to enforce. It cannot happen in production: a
    production profile is required to declare the block, and a declared block
    whose rule file is missing stops the run.
    """
    try:
        settings = resolve_scanner_settings(
            config,
            declared_class=profile_class(config),
            cli_mode=args.output_scanner_mode,
            env_mode=os.environ.get(MODE_ENV_VAR) or None,
        )
    except (ScannerConfigError, ProfileError) as exc:
        raise SystemExit(f"invalid output scanner configuration: {exc}") from exc
    if settings.mode == "observe" and (args.record or args.record_dir):
        # Not a process rule but a hard one: under observe the scanner does not
        # block, so the recorder would write the full prompt and response --
        # matched text included -- into a bundle. A calibration run needs the
        # distribution, and that is in metrics.json, without any content.
        raise SystemExit(
            "output scanner mode 'observe' cannot be combined with --record or "
            "--record-dir: an unblocked run would record the matched content"
        )
    declared = config.get("output_scanner") is not None
    text = _snapshot_text(
        repo_path,
        snapshot_sha,
        settings.patterns_path,
        label="output scanner patterns",
        required=declared,
    )
    if text is None:
        if args.record or args.record_dir:
            # The only reachable way for prompts and responses to be written to
            # disk unscanned: without a ruleset the recorder receives an
            # explicit "not scanned" verdict and records the call anyway. A run
            # with no scanner is tolerable only while it persists no model text.
            raise SystemExit(
                f"no output scanner rules at {settings.patterns_path}: recording "
                "would write prompts and responses that nothing has scanned"
            )
        # A warning, not an info line: this run publishes model-derived
        # products with no output gate at all. It is reachable only in a
        # repository that ships no rules -- a production profile must declare
        # the block, and a declared block whose file is missing stops the run.
        LOGGER.warning(
            "no output scanner rules at %s; this run publishes unscanned output",
            settings.patterns_path,
        )
        return None
    try:
        # The machine's own name is a leak in published output but must never be
        # written into a tracked rule file, so it is injected here. Read through
        # the package's single hostname call site (§8.7.2), not directly.
        ruleset = Ruleset.from_text(
            text, dynamic_values={"local_hostname": [read_host()]}
        )
    except (ValueError, yaml.YAMLError) as exc:
        raise SystemExit(
            f"invalid output scanner patterns {settings.patterns_path}: {exc}"
        ) from exc
    return OutputScanner(ruleset, mode=settings.mode)


def _snapshot_text(
    repo_path: Path,
    snapshot_sha: str | None,
    relative: str,
    *,
    label: str,
    required: bool,
) -> str | None:
    """One repo-relative file, read from the frozen snapshot (§5.5.5).

    Same rule as the profile: reading the working tree instead would leave the
    run's recorded ``master_snapshot_sha`` describing bytes it did not use.
    """
    if snapshot_sha is not None and (repo_path / ".git").exists():
        try:
            blob = GitRepo(repo_path).show_bytes(f"{snapshot_sha}:{relative}")
        except GitError:
            blob = b""
        if blob.strip():
            try:
                return blob.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SystemExit(f"{label} is not valid UTF-8: {relative}") from exc
    else:
        path = repo_path / relative
        if path.exists():
            return path.read_text(encoding="utf-8")
    if required:
        raise SystemExit(f"{label} is not in the frozen snapshot: {relative}")
    return None


def _record_recorder(
    args: argparse.Namespace, *, run_id: str | None = None
) -> BundleRecorder | None:
    """The run's recorder, staged when a run id is available (§5.5.12).

    Without a run id there is no unique staging name, so the recorder writes
    straight through exactly as it did before staging existed.
    """
    if args.replay:
        return None
    if args.record_dir and args.record:
        raise SystemExit(
            "--record and --record-dir cannot be combined: a run records either "
            "one flat file or one bundle, not both"
        )
    if args.record_dir:
        if run_id is None:
            return BundleRecorder(
                args.record_dir / "calls.jsonl",
                requests_dir=args.record_dir / "requests",
            )
        return BundleRecorder.staged_bundle(args.record_dir, run_id=run_id)
    if args.record:
        if run_id is None:
            return BundleRecorder(args.record)
        return BundleRecorder.staged_file(args.record, run_id=run_id)
    return None


def _wrap_record(
    provider: LLMProvider,
    recorder: BundleRecorder | None,
    context: RunContext | None = None,
) -> LLMProvider:
    """Wrap a lane so its calls are scanned, and recorded when asked (§5.5.10).

    The context is what carries the run's scanner; without it the wrapper keeps
    its pre-injection behaviour and records unscanned.

    A lane with no recorder is still wrapped, for the outbound scan: not
    recording means the prompt is never written down, not that it is never
    sent, and the prompt is what carries the leaf records.
    """
    if recorder is None:
        # Only when there is something to scan with: a run with no scanner must
        # keep the exact call graph it had before any of this existed.
        scanning = ScanningProvider(provider, context=context)
        return scanning if scanning.scanner is not None else provider
    return RecordingProvider(provider, recorder=recorder, context=context)


_OFFLINE_REPLAY_MODES = frozenset({"strict", "prompt-only"})
"""Replay modes that provably make no request, whatever the provider is named."""


def _replay_mode(args: argparse.Namespace) -> str:
    if not args.replay:
        return "live"
    if args.replay_mode:
        return str(args.replay_mode)
    return "strict" if args.replay_strict else "prompt-only"


def _fallback_provider_name(args: argparse.Namespace, config: dict[str, Any]) -> str:
    provider_name = args.fallback_provider
    llm = config.get("llm")
    if provider_name is None:
        if isinstance(llm, dict) and llm.get("fallback_provider"):
            provider_name = str(llm["fallback_provider"])
        elif config.get("fallback_provider"):
            provider_name = str(config["fallback_provider"])
        elif isinstance(llm, dict) and llm.get("fallback_model"):
            provider_name = _provider_name(args, config)
        else:
            provider_name = "none"
    return str(provider_name)


def _fallback_model(config: dict[str, Any]) -> str | None:
    llm = config.get("llm")
    if isinstance(llm, dict) and llm.get("fallback_model"):
        return str(llm["fallback_model"])
    return None


def _base_provider(
    provider_name: str,
    model: str | None,
    *,
    budget: RunBudget | None = None,
) -> LLMProvider:
    """The single provider factory.

    ``budget`` is injected at construction rather than passed per call, so a
    provider's internal retries do not have to thread it through.
    """
    if provider_name == "deepseek":
        # Reserves for itself, so wrapping it would reserve twice per call.
        # The attempt ledger is attached unconditionally: it is the only ceiling
        # that survives across processes and runs, and the production backend is
        # the one lane where a real request is billed (§5.5.14).
        return OpenAICompatProvider(model=model, budget=budget, ledger=AttemptLedger())
    if provider_name == "anthropic":
        return _budgeted(AnthropicSDKProvider(model=model), budget)
    if provider_name == "hermes_tool":
        return _budgeted(HermesToolProvider(), budget)
    if provider_name == "mock":
        # The mock costs nothing. Charging it against a configured budget_usd
        # would let the shipped default profile starve its own deterministic run.
        return MockLLMProvider()
    # Named rather than defaulted. --provider is constrained by argparse, but a
    # profile's llm.provider is not, and a typo silently resolving to the mock
    # produces a run that never touches a model and publishes anyway.
    raise SystemExit(
        f"unknown provider {provider_name!r}; choose one of "
        + ", ".join(PROVIDER_CHOICES)
    )


def _budgeted(provider: LLMProvider, budget: RunBudget | None) -> LLMProvider:
    """Give a paid lane a pre-call ceiling, if the profile configured one.

    Without this the lane's only gate is the pipeline's post-hoc accumulator,
    which sums calls that already returned: it can refuse the *next* call but
    never the over-ceiling one, and it never sees a retry at all.
    """
    if budget is None:
        return provider
    return BudgetedProvider(provider, budget=budget)


def _provider_name(args: argparse.Namespace, config: dict[str, Any]) -> str:
    if args.provider:
        return str(args.provider)
    llm = config.get("llm")
    if isinstance(llm, dict) and llm.get("provider"):
        return str(llm["provider"])
    return "mock"


def _pipeline_model(
    args: argparse.Namespace,
    provider_name: str,
    config: dict[str, Any],
) -> str:
    if args.model:
        return str(args.model)
    llm = config.get("llm")
    if isinstance(llm, dict) and llm.get("model"):
        return str(llm["model"])
    if provider_name == "anthropic":
        return DEFAULT_ANTHROPIC_MODEL
    if provider_name == "deepseek":
        return DEFAULT_DEEPSEEK_MODEL
    return MOCK_MODEL


def _max_tokens(args: argparse.Namespace, config: dict[str, Any]) -> int:
    if args.max_tokens is not None:
        return int(args.max_tokens)
    llm = config.get("llm")
    if isinstance(llm, dict) and llm.get("max_tokens") is not None:
        return int(llm["max_tokens"])
    return 2000


def _cost_budget_usd(config: dict[str, Any]) -> float | None:
    llm = config.get("llm")
    if isinstance(llm, dict) and llm.get("budget_usd") is not None:
        return float(llm["budget_usd"])
    cost_budget = config.get("cost_budget")
    if isinstance(cost_budget, dict) and cost_budget.get("budget_usd") is not None:
        return float(cost_budget["budget_usd"])
    return None


def _run_budget(config: dict[str, Any]) -> RunBudget | None:
    """The run's shared budget, or None when the profile configures no ceiling.

    Absent configuration must keep the pre-injection behaviour: without a budget
    the pipeline falls back to its own cost gate rather than silently gaining a
    new one that nobody asked for.
    """
    max_usd = _cost_budget_usd(config)
    max_attempts = _budget_limit_int(config, "max_attempts")
    max_wall_clock_s = _budget_limit_float(config, "max_wall_clock_s")
    # Any one of the three is enough to build the budget. Keying only off the
    # cost ceiling would silently discard an attempt or wall-clock limit the
    # profile did configure, leaving every paid lane with no pre-call guard at
    # all -- the exact failure the limits were written to prevent.
    if max_usd is None and max_attempts is None and max_wall_clock_s is None:
        return None
    return RunBudget(
        input_per_mtok_usd=_input_cost_per_mtok_usd(config),
        output_per_mtok_usd=_output_cost_per_mtok_usd(config),
        # Each dimension stays None unless the profile names it. A cost ceiling
        # alone leaves a run that retries forever, or one that wedges against a
        # slow endpoint, with nothing to stop it; and a silent default here
        # would cap runs nobody asked to cap.
        max_usd=max_usd,
        max_attempts=max_attempts,
        max_wall_clock_s=max_wall_clock_s,
    )


def _budget_limit_int(config: dict[str, Any], key: str) -> int | None:
    value = _budget_limit(config, key)
    return None if value is None else int(value)


def _budget_limit_float(config: dict[str, Any], key: str) -> float | None:
    value = _budget_limit(config, key)
    return None if value is None else float(value)


def _budget_limit(config: dict[str, Any], key: str) -> Any:
    """Read one budget ceiling from ``llm.*``, falling back to ``cost_budget.*``.

    Same two-place lookup as the cost ceiling and the pricing, so a profile does
    not have to spread one concept across two sections.
    """
    llm = config.get("llm")
    if isinstance(llm, dict) and llm.get(key) is not None:
        return llm[key]
    cost_budget = config.get("cost_budget")
    if isinstance(cost_budget, dict) and cost_budget.get(key) is not None:
        return cost_budget[key]
    return None


def _input_cost_per_mtok_usd(config: dict[str, Any]) -> float:
    return _pricing_value(config, "input_per_mtok_usd", default=3.0)


def _output_cost_per_mtok_usd(config: dict[str, Any]) -> float:
    return _pricing_value(config, "output_per_mtok_usd", default=15.0)


def _pricing_value(config: dict[str, Any], key: str, *, default: float) -> float:
    llm = config.get("llm")
    pricing = llm.get("pricing") if isinstance(llm, dict) else None
    if isinstance(pricing, dict) and pricing.get(key) is not None:
        return float(pricing[key])
    cost_budget = config.get("cost_budget")
    if isinstance(cost_budget, dict) and cost_budget.get(key) is not None:
        return float(cost_budget[key])
    return default


def _dbscan_eps(config: dict[str, Any]) -> float:
    clustering = config.get("clustering")
    if isinstance(clustering, dict) and clustering.get("dbscan_eps") is not None:
        return float(clustering["dbscan_eps"])
    if isinstance(clustering, dict) and clustering.get("eps") is not None:
        return float(clustering["eps"])
    return 0.5


def _dbscan_min_samples(config: dict[str, Any]) -> int:
    clustering = config.get("clustering")
    if isinstance(clustering, dict) and clustering.get("min_samples") is not None:
        return int(clustering["min_samples"])
    return 2


def _min_citations_by_category(config: dict[str, Any]) -> dict[str, int]:
    raw = config.get("category_evidence_rules") or config.get("categories")
    rules = raw if isinstance(raw, dict) else {}
    result: dict[str, int] = {}
    for category, value in rules.items():
        if isinstance(value, dict):
            raw_min = value.get("min_citations") or value.get("min_evidence")
        else:
            raw_min = value
        if raw_min is not None:
            result[canonical_category(str(category))] = int(raw_min)
    return result


def _min_contributors_by_category(config: dict[str, Any]) -> dict[str, int]:
    raw = config.get("category_evidence_rules") or config.get("categories")
    rules = raw if isinstance(raw, dict) else {}
    result: dict[str, int] = {}
    for category, value in rules.items():
        if isinstance(value, dict) and value.get("min_contributors") is not None:
            result[canonical_category(str(category))] = int(value["min_contributors"])
    return result


def _category_names(config: dict[str, Any]) -> list[str]:
    raw = config.get("categories")
    if isinstance(raw, dict):
        return sorted(canonical_category(str(category)) for category in raw)
    if isinstance(raw, list):
        return sorted(canonical_category(str(category)) for category in raw)
    raw = config.get("category_evidence_rules")
    if isinstance(raw, dict):
        return sorted(canonical_category(str(category)) for category in raw)
    output = config.get("output")
    if isinstance(output, dict) and output.get("default_category"):
        return [canonical_category(str(output["default_category"]))]
    return []


def _publish_branch_template(config: dict[str, Any]) -> str | None:
    review_gate = config.get("review_gate")
    if not isinstance(review_gate, dict) or not review_gate.get("enabled"):
        return None
    return str(review_gate.get("branch_template") or "release/<run_id>")


def _prompt_path(
    cli_path: Path | None,
    config: dict[str, Any],
    key: str,
    default: Path,
) -> Path:
    if cli_path is not None:
        return cli_path
    prompts = config.get("prompts")
    if isinstance(prompts, dict) and prompts.get(key):
        return Path(str(prompts[key]))
    return default


def _resolve_prompt(repo: Path, path: Path) -> Path:
    resolved = path if path.is_absolute() else repo / path
    try:
        resolved.resolve().relative_to(repo.resolve())
    except ValueError as exc:
        raise SystemExit(f"prompt path must be inside repo: {path}") from exc
    return resolved


def _setup_runtime(
    args: argparse.Namespace,
    *,
    snapshot_sha: str | None = None,
) -> None:
    with tempfile.TemporaryDirectory(prefix="memory-net-runtime-config-") as tmpdir:
        tmp = Path(tmpdir)
        _enforce_supported_platform(
            _runtime_config_path(args.repo, "platform.yaml", snapshot_sha, tmp)
        )
        setup_logging(
            config_path=_runtime_config_path(
                args.repo,
                "observability.yaml",
                snapshot_sha,
                tmp,
            )
        )


def _enforce_supported_platform(config_path: Path | None = None) -> None:
    current = platform.system()
    supported = _supported_platforms(config_path)
    if current not in supported:
        print(
            f"[ABORT] unsupported OS: {current}; supported: {', '.join(supported)}",
            file=sys.stderr,
        )
        raise SystemExit(2)


def _validate_bootstrap_state_from_repo(
    repo_path: Path,
    snapshot_sha: str | None = None,
) -> None:
    text = _read_bootstrap_state(repo_path, snapshot_sha)
    try:
        value = json.loads(text)
        validate_bootstrap_state(
            value,
            build_version=__version__,
            build_api=BUILD_API,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise SystemExit(f"bootstrap_state.json is not compatible: {exc}") from exc


def _read_bootstrap_state(repo_path: Path, snapshot_sha: str | None = None) -> str:
    if (repo_path / ".git").exists():
        repo = GitRepo(repo_path)
        ref = snapshot_sha or "origin/master"
        if snapshot_sha is None:
            repo.fetch()
        result = repo.run(
            "show",
            f"{ref}:bootstrap_state.json",
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
    path = repo_path / "bootstrap_state.json"
    if path.exists():
        return path.read_text(encoding="utf-8")
    raise SystemExit("bootstrap_state.json is required before running distiller")


def _runtime_config_path(
    repo_path: Path,
    filename: str,
    snapshot_sha: str | None,
    tmp: Path,
) -> Path | None:
    if snapshot_sha is not None and (repo_path / ".git").exists():
        repo = GitRepo(repo_path)
        result = repo.run("show", f"{snapshot_sha}:config/{filename}", check=False)
        if result.returncode == 0 and result.stdout.strip():
            path = tmp / filename
            path.write_text(result.stdout, encoding="utf-8")
            return path
    return _first_existing_config(repo_path, filename)


def _first_existing_config(repo_path: Path, filename: str) -> Path | None:
    for path in [repo_path / "config" / filename, Path("config") / filename]:
        if path.exists():
            return path
    return None


def _supported_platforms(config_path: Path | None) -> tuple[str, ...]:
    config = _load_yaml_mapping(config_path) if config_path is not None else {}
    runtime = config.get("runtime")
    raw = runtime.get("supported_os") if isinstance(runtime, dict) else None
    if raw is None:
        raw = config.get("supported_os")
    if isinstance(raw, list) and raw:
        return tuple(str(item) for item in raw)
    return ("Linux", "Darwin")


def _freeze_master_snapshot(repo_path: Path) -> str | None:
    if not (repo_path / ".git").exists():
        return None
    repo = GitRepo(repo_path)
    repo.fetch()
    result = repo.run("rev-parse", "origin/master", check=False)
    snapshot_sha = result.stdout.strip()
    if result.returncode != 0 or not snapshot_sha:
        raise SystemExit("cannot resolve origin/master")
    return snapshot_sha


DEFAULT_PROFILE_PATH = "config/distill.yaml"
PROFILE_ENV_VAR = "AOMT_DISTILL_PROFILE"


@dataclass(frozen=True)
class SelectedProfile:
    """Which distillation profile this run uses, and whether it was asked for.

    ``explicit`` drives the missing-file behaviour, which is deliberately
    asymmetric: a named profile that is absent must stop the run, while the
    default one falling back is what lets a brand-new repo work at all.
    """

    path: str
    explicit: bool


def _selected_profile(args: argparse.Namespace) -> SelectedProfile:
    """Resolve the profile path: flag, then environment, then the default.

    The flag beats the environment variable because it is typed for this one
    run while the variable is ambient, and ambient state quietly winning is how
    a run ends up on a budget nobody chose.
    """
    raw = getattr(args, "config", None)
    if raw is None:
        raw = os.environ.get(PROFILE_ENV_VAR)
    if raw is None:
        return SelectedProfile(path=DEFAULT_PROFILE_PATH, explicit=False)
    # An empty selection is not the default: the caller asked for something,
    # and running the default instead would run a profile nobody named.
    try:
        # Repo-relative only. A profile read from outside the snapshot would
        # leave the run's recorded master_snapshot_sha looking clean while the
        # configuration it actually ran under was never in that snapshot --
        # exactly the tampering the frozen-snapshot design exists to prevent.
        safe = safe_posix_relative_path(str(raw))
    except ValueError as exc:
        raise SystemExit(f"profile path must stay inside the repo: {raw!r}") from exc
    return SelectedProfile(path=safe.as_posix(), explicit=True)


def _embedding_policy(config: dict[str, Any]) -> EmbeddingPolicy:
    """The run's one embedding decision, taken from the profile (§5.5.7).

    Absent keys keep the pre-policy default -- the test stand-in -- because a
    profile that says nothing about embeddings is a development profile, and a
    production one cannot say nothing: the schema refuses it.
    """
    clustering = config.get("clustering")
    clustering = clustering if isinstance(clustering, dict) else {}
    backend = clustering.get("embedding_backend")
    model_name = clustering.get("embedding_model")
    try:
        return EmbeddingPolicy.of(
            str(backend) if backend else TEST_CHEAP,
            str(model_name) if model_name else DEFAULT_EMBEDDING_MODEL,
        )
    except ValueError as exc:
        raise SystemExit(f"invalid clustering configuration: {exc}") from exc


def _preflight(args: argparse.Namespace) -> None:
    """Checks that need no profile, run before anything is fetched.

    Deliberately the only work placed this early. Everything else -- the
    embedding probe included -- depends on the profile, and the profile is read
    out of the frozen snapshot, which does not exist until the fetch has
    happened. So this covers exactly the failures that are knowable from the
    command line alone, and the profile-dependent ones fail immediately after
    the profile is parsed instead.

    Scoped to ``--provider deepseek`` rather than the resolved provider name for
    the same reason: resolving that requires the profile.
    """
    if getattr(args, "provider", None) != "deepseek":
        return
    if _replay_mode(args) in _OFFLINE_REPLAY_MODES:
        # A replay that cannot fall through to the live lane never constructs
        # the backend, so demanding its credentials would make the offline
        # regression -- the one that has to stay green on a public runner with
        # no secrets -- impossible to run at all. `allow-live-on-miss` is
        # excluded precisely because it can go live on a miss.
        return
    missing = [name for name in (API_KEY_ENV, BASE_URL_ENV) if not os.environ.get(name)]
    if missing:
        raise SystemExit(
            "production backend selected but its credentials are absent: "
            + ", ".join(missing)
        )
    try:
        import openai  # noqa: F401
    except ImportError as exc:
        raise SystemExit(
            "production backend selected but the 'openai' package is missing; "
            "install the 'llm' extra"
        ) from exc


def _read_profile_bytes(
    repo_path: Path,
    snapshot_sha: str | None = None,
    *,
    profile: str = DEFAULT_PROFILE_PATH,
) -> bytes | None:
    """The profile's raw bytes from the frozen snapshot (§5.5.5).

    Bytes rather than the parsed mapping because the provenance hash is taken
    over exactly these bytes: parsing discards comments, key order and
    formatting, so hashing the mapping could not answer "which bytes ran".
    Bytes rather than text for the same reason one step down -- the text layer
    normalizes line endings, and a hash of normalized content matches no blob.

    ``None`` means the default profile is simply absent, which a brand-new repo
    and any non-git working directory must both survive.
    """
    explicit = profile != DEFAULT_PROFILE_PATH
    if snapshot_sha is not None and (repo_path / ".git").exists():
        repo = GitRepo(repo_path)
        spec = f"{snapshot_sha}:{profile}"
        try:
            # show_bytes, not show/run: the text channel would normalize line
            # endings, and the provenance hash is taken over what comes back.
            blob = repo.show_bytes(spec)
        except GitError:
            blob = b""
        if blob.strip():
            return blob
        if explicit:
            raise SystemExit(f"profile is not in the frozen snapshot: {spec}")
    if explicit:
        # No snapshot to read from (a non-git working directory), so the
        # request cannot be honoured as specified. Falling back to the default
        # would run something other than what was asked for.
        raise SystemExit(f"profile is not in the frozen snapshot: {profile}")
    path = repo_path / DEFAULT_PROFILE_PATH
    if not path.exists():
        LOGGER.info("using default config; file not found: %s", path)
        return None
    return path.read_bytes()


def _distill_config(
    repo_path: Path,
    snapshot_sha: str | None = None,
    *,
    profile: str = DEFAULT_PROFILE_PATH,
) -> dict[str, Any]:
    """The parsed profile; ``{}`` when the default one is absent."""
    raw = _read_profile_bytes(repo_path, snapshot_sha, profile=profile)
    return _parse_profile(raw, profile)


def _parse_profile(raw: bytes | None, profile: str) -> dict[str, Any]:
    """Decode here and only here: the bytes exist for the provenance hash, and
    YAML is the one consumer that needs them as text."""
    if raw is None:
        return {}
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SystemExit(f"profile is not valid UTF-8: {profile}: {exc}") from exc
    return _load_yaml_text(text, profile)


def _validated_profile(config: dict[str, Any], profile: str) -> None:
    """Stop the run on a profile that must not start one (§5.5.6)."""
    try:
        validate_profile(config)
    except ProfileError as exc:
        raise SystemExit(f"invalid profile {profile}: {exc}") from exc


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    return _load_required_yaml_mapping(path)


def _load_required_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"config file is required: {path}")
    return _load_yaml_text(path.read_text(encoding="utf-8"), str(path))


def _load_optional_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        LOGGER.info("using default config; file not found: %s", path)
        return {}
    return _load_yaml_text(path.read_text(encoding="utf-8"), str(path))


def _load_yaml_text(text: str, label: str) -> dict[str, Any]:
    value = yaml.safe_load(text) or {}
    if not isinstance(value, dict):
        raise SystemExit(f"config must be a mapping: {label}")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
