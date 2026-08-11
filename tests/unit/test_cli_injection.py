"""CLI assembly of the RunContext (§5.5.4).

cli.py is the only place that constructs the providers, the pipeline and the
orchestrator, so it is the only place that can guarantee they share one context
and one budget. These tests assert exactly that sharing, because a second budget
instance is invisible at runtime and silently doubles a run's ceiling.
"""

import argparse
from pathlib import Path
from typing import Any

import pytest

from memory_net.distiller import cli as cli_module
from memory_net.distiller.llm.budgeted import BudgetedProvider
from memory_net.distiller.llm.mock import MockLLMProvider
from memory_net.distiller.llm.openai_compat import OpenAICompatProvider
from memory_net.distiller.runctx import RunBudget, RunContext


def _args(**overrides: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "provider": None,
        "fallback_provider": None,
        "model": None,
        "replay": None,
        "replay_mode": None,
        "replay_strict": True,
        "record": None,
        "record_dir": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


# --------------------------------------------------------------------------
# Provider factory
# --------------------------------------------------------------------------


def test_the_deepseek_branch_builds_the_openai_compatible_provider() -> None:
    budget = RunBudget(1.0, 10.0, max_usd=1.0)

    provider = cli_module._base_provider("deepseek", "deepseek-v4-flash", budget=budget)

    assert isinstance(provider, OpenAICompatProvider)
    assert provider.budget is budget
    assert provider.model == "deepseek-v4-flash"


def test_deepseek_is_selectable_on_both_provider_flags() -> None:
    """A production primary with no production fallback would silently fall back
    to a mock on the first parse failure."""
    parser = cli_module._build_parser()
    actions = {
        action.dest: action
        for action in parser._subparsers._group_actions[0]  # type: ignore[union-attr]
        .choices["run"]
        ._actions
    }

    assert "deepseek" in actions["provider"].choices
    assert "deepseek" in actions["fallback_provider"].choices


def test_the_mock_lane_is_never_charged_against_the_ceiling() -> None:
    """It costs nothing, and charging it would let the shipped default profile
    -- provider: mock plus budget_usd -- starve its own deterministic run."""
    budget = RunBudget(1.0, 10.0)

    assert isinstance(
        cli_module._base_provider("mock", None, budget=budget), MockLLMProvider
    )


@pytest.mark.parametrize("provider_name", ["anthropic", "hermes_tool"])
def test_the_paid_non_reserving_lanes_get_a_pre_call_ceiling(
    provider_name: str,
) -> None:
    """These providers never reserve, so without the wrapper their only gate is
    the pipeline accumulator -- which can only refuse the call *after* the
    over-ceiling one has already gone out."""
    budget = RunBudget(1.0, 10.0, max_usd=1.0)

    provider = cli_module._base_provider(provider_name, None, budget=budget)

    assert isinstance(provider, BudgetedProvider)
    assert provider.budget is budget


@pytest.mark.parametrize("provider_name", ["anthropic", "hermes_tool"])
def test_no_configured_ceiling_leaves_the_lane_unwrapped(provider_name: str) -> None:
    """A profile without budget_usd must keep the pre-injection behaviour."""
    assert not isinstance(
        cli_module._base_provider(provider_name, None, budget=None), BudgetedProvider
    )


def test_the_production_lane_is_not_wrapped_because_it_reserves_itself() -> None:
    """Wrapping it would reserve twice for every single call."""
    provider = cli_module._base_provider(
        "deepseek", None, budget=RunBudget(1.0, 10.0, max_usd=1.0)
    )

    assert not isinstance(provider, BudgetedProvider)


# --------------------------------------------------------------------------
# One budget across both lanes
# --------------------------------------------------------------------------


def test_both_lanes_receive_the_same_budget_instance() -> None:
    """The run-level ceiling only holds if there is exactly one of it: two
    instances would let a run capped at 5 USD spend 10."""
    budget = RunBudget(1.0, 10.0, max_usd=5.0)

    primary, fallback = cli_module._build_providers(
        _args(provider="deepseek", fallback_provider="deepseek"),
        "deepseek",
        "deepseek",
        {},
        budget=budget,
    )

    assert isinstance(primary, OpenAICompatProvider)
    assert isinstance(fallback, OpenAICompatProvider)
    assert primary.budget is fallback.budget is budget


def test_no_fallback_configured_leaves_the_fallback_lane_unbuilt() -> None:
    _, fallback = cli_module._build_providers(
        _args(provider="deepseek"), "deepseek", "none", {}, budget=RunBudget(1.0, 10.0)
    )

    assert fallback is None


# --------------------------------------------------------------------------
# Budget construction from the profile
# --------------------------------------------------------------------------


def test_a_profile_without_a_budget_builds_no_budget_at_all() -> None:
    """Absent configuration must keep the pre-injection behaviour: the pipeline
    falls back to its own cost gate rather than silently gaining a new one."""
    assert cli_module._run_budget({}) is None


def test_the_budget_takes_its_ceiling_and_pricing_from_the_profile() -> None:
    config = {
        "llm": {
            "budget_usd": 2.5,
            "pricing": {"input_per_mtok_usd": 0.27, "output_per_mtok_usd": 1.1},
        }
    }

    budget = cli_module._run_budget(config)

    assert budget is not None
    assert budget.max_usd == 2.5
    assert budget.input_per_mtok_usd == 0.27
    assert budget.output_per_mtok_usd == 1.1


def test_the_attempt_and_wall_clock_ceilings_are_wired_from_the_profile() -> None:
    """A cost ceiling alone leaves a run that retries forever, or one wedged
    against a slow endpoint, with nothing to stop it."""
    budget = cli_module._run_budget(
        {"llm": {"budget_usd": 1.0, "max_attempts": 7, "max_wall_clock_s": 90}}
    )

    assert budget is not None
    assert budget.max_attempts == 7
    assert budget.max_wall_clock_s == pytest.approx(90.0)


def test_the_ceilings_also_read_from_the_cost_budget_section() -> None:
    """Same two-place lookup as the cost ceiling and the pricing, so a profile
    does not have to spread one concept across two sections."""
    budget = cli_module._run_budget(
        {"cost_budget": {"budget_usd": 1.0, "max_attempts": 3, "max_wall_clock_s": 5}}
    )

    assert budget is not None
    assert budget.max_attempts == 3
    assert budget.max_wall_clock_s == pytest.approx(5.0)


@pytest.mark.parametrize(
    "limits",
    [
        pytest.param({"max_attempts": 3}, id="attempts-only"),
        pytest.param({"max_wall_clock_s": 30}, id="wall-clock-only"),
    ],
)
def test_a_non_cost_ceiling_alone_still_builds_a_budget(
    limits: dict[str, Any],
) -> None:
    """Keying only off budget_usd would silently discard a limit the profile
    did configure, leaving every paid lane with no pre-call guard at all."""
    budget = cli_module._run_budget({"llm": dict(limits)})

    assert budget is not None
    assert budget.max_usd is None


def test_unnamed_ceilings_stay_unset_rather_than_defaulting() -> None:
    """A silent default here would cap runs nobody asked to cap."""
    budget = cli_module._run_budget({"llm": {"budget_usd": 1.0}})

    assert budget is not None
    assert budget.max_attempts is None
    assert budget.max_wall_clock_s is None


# --------------------------------------------------------------------------
# The assembled context reaches every consumer
# --------------------------------------------------------------------------


def test_the_pipeline_and_orchestrator_receive_the_same_context(
    tmp_path: Path, monkeypatch: Any
) -> None:
    seen: dict[str, RunContext | None] = {}

    real_pipeline = cli_module.DistillationPipeline
    real_orchestrator = cli_module.DistillOrchestrator

    class _SpyPipeline(real_pipeline):  # type: ignore[misc, valid-type]
        def __init__(self, **kwargs: Any) -> None:
            seen["pipeline"] = kwargs.get("context")
            super().__init__(**kwargs)

    class _SpyOrchestrator(real_orchestrator):  # type: ignore[misc, valid-type]
        def __init__(self, **kwargs: Any) -> None:
            seen["orchestrator"] = kwargs.get("context")
            super().__init__(**kwargs)

        def run(self) -> Any:
            raise _StopHere

    monkeypatch.setattr(cli_module, "DistillationPipeline", _SpyPipeline)
    monkeypatch.setattr(cli_module, "DistillOrchestrator", _SpyOrchestrator)

    repo = _repo(tmp_path)
    with pytest.raises(_StopHere):
        cli_module.main(["run", "--repo", str(repo), "--dry-run"])

    assert seen["pipeline"] is not None
    assert seen["pipeline"] is seen["orchestrator"]


def test_the_run_id_is_minted_by_the_cli_not_the_orchestrator(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The run id has to exist before its consumers do: anything named after it
    cannot be constructed otherwise."""
    seen: dict[str, RunContext | None] = {}
    real_orchestrator = cli_module.DistillOrchestrator

    class _SpyOrchestrator(real_orchestrator):  # type: ignore[misc, valid-type]
        def __init__(self, **kwargs: Any) -> None:
            seen["context"] = kwargs.get("context")
            super().__init__(**kwargs)

        def run(self) -> Any:
            raise _StopHere

    monkeypatch.setattr(cli_module, "DistillOrchestrator", _SpyOrchestrator)

    repo = _repo(tmp_path)
    with pytest.raises(_StopHere):
        cli_module.main(["run", "--repo", str(repo), "--dry-run"])

    context = seen["context"]
    assert context is not None
    assert context.identity is not None
    assert context.identity.run_id
    assert context.identity.now().endswith("Z")


class _StopHere(Exception):
    """Ends main() once the assembly under test has happened."""


def _repo(tmp_path: Path) -> Path:
    """A minimal non-git repo: enough for the CLI to assemble and then stop."""
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
