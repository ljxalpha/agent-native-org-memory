"""RunContext injection into the pipeline and the orchestrator (§5.5.3).

Two properties are under test. Behavioural: an injected identity makes a run
reproducible, and an injected budget becomes the run's only spend gate. And
structural: no module under ``memory_net/distiller`` may read the clock or mint
a run id of its own, since a single such read silently defeats the first
property in a way nothing at runtime would notice.
"""

import ast
from pathlib import Path
from typing import Any

import pytest

from memory_net.distiller.llm.base import LLMRequest, LLMResponse
from memory_net.distiller.llm.mock import MockLLMProvider
from memory_net.distiller.pipeline import DistillationPipeline
from memory_net.distiller.runctx import (
    BudgetExhausted,
    RunBudget,
    RunContext,
    RunIdentity,
)

DISTILLER_DIR = Path(__file__).resolve().parents[2] / "memory_net" / "distiller"

# runctx.py owns the one sanctioned clock/id factory; cli.py is the single
# assembly point allowed to construct a RunIdentity for a run.
CLOCK_EXEMPT_MODULES = {"runctx.py", "cli.py"}

# Named (module, function) exemptions for uuid4. Named rather than blanket, so a
# new module cannot quietly acquire an id source of its own.
UUID_EXEMPTIONS = {
    # Names a temp directory alongside os.getpid() for concurrency safety. The
    # value never reaches a product, manifest or metrics file.
    ("orchestrator.py", "_write_team_products"),
}


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


def _prompts(tmp_path: Path) -> tuple[Path, Path]:
    system = tmp_path / "system.md"
    user = tmp_path / "user.md"
    system.write_text("system", encoding="utf-8")
    user.write_text("records {{ records_json }}", encoding="utf-8")
    return system, user


def _pipeline(tmp_path: Path, **kwargs: Any) -> DistillationPipeline:
    system, user = _prompts(tmp_path)
    return DistillationPipeline(
        provider=kwargs.pop("provider", MockLLMProvider()),
        system_prompt_path=system,
        user_prompt_path=user,
        **kwargs,
    )


# --------------------------------------------------------------------------
# Identity injection
# --------------------------------------------------------------------------


def test_a_frozen_identity_makes_two_runs_produce_identical_products(
    tmp_path: Path,
) -> None:
    """generated_at feeds the product body, whose hash feeds the manifest, so a
    frozen clock is what makes a recorded run reproducible byte for byte."""
    identity = RunIdentity(run_id="run-fixed", now=lambda: "2026-01-01T00:00:00Z")
    context = RunContext(identity=identity)

    first = _pipeline(tmp_path, context=context).run([_record()], run_id="run-fixed")
    second = _pipeline(tmp_path, context=context).run([_record()], run_id="run-fixed")

    assert first.products
    assert [p.content for p in first.products] == [p.content for p in second.products]
    assert "2026-01-01T00:00:00Z" in first.products[0].content


def test_without_a_context_the_pipeline_keeps_its_wall_clock_behaviour(
    tmp_path: Path,
) -> None:
    """The default must stay byte-identical to the pre-injection behaviour, or
    the wiring could not land one consumer at a time."""
    output = _pipeline(tmp_path).run([_record()], run_id="run")

    assert output.products
    assert output.products[0].frontmatter["generated_at"].endswith("Z")


# --------------------------------------------------------------------------
# Budget injection: the run budget is the only gate
# --------------------------------------------------------------------------


class _RefusingProvider(MockLLMProvider):
    """Stands in for a provider whose reserve() refused before the call."""

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        raise BudgetExhausted("run budget exhausted: cost")


def test_a_refused_reservation_skips_the_cluster_instead_of_failing_the_run(
    tmp_path: Path,
) -> None:
    provider = _RefusingProvider()

    output = _pipeline(
        tmp_path,
        provider=provider,
        context=RunContext(budget=RunBudget(1.0, 10.0, max_usd=0.0)),
    ).run([_record()], run_id="run")

    assert provider.calls == 1
    assert output.products == []
    assert output.skipped_clusters == 1
    # The cluster was never actually attempted against a model, so it must not
    # inflate the denominator of the parse-failure rate.
    assert output.clusters_attempted == 0
    assert output.parse_failures == 0


def test_an_injected_budget_disables_the_legacy_self_accounting_gate(
    tmp_path: Path,
) -> None:
    """Two gates deciding the same question is two copies of the budget rules."""
    pipeline = _pipeline(
        tmp_path,
        cost_budget_usd=0.0,
        context=RunContext(budget=RunBudget(1.0, 10.0, max_usd=100.0)),
    )

    # cost_budget_usd=0.0 alone would skip every cluster; the injected budget
    # has headroom, so the run proceeds.
    assert pipeline._budget_exhausted(999.0) is False
    assert pipeline.run([_record()], run_id="run").products


def test_without_a_context_the_legacy_cost_gate_still_applies(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path, cost_budget_usd=0.0)

    assert pipeline._budget_exhausted(0.0) is True
    assert pipeline.run([_record()], run_id="run").skipped_clusters == 1


# --------------------------------------------------------------------------
# Structural tripwire
# --------------------------------------------------------------------------


def _python_modules() -> list[Path]:
    return sorted(DISTILLER_DIR.rglob("*.py"))


def _enclosing_functions(tree: ast.AST) -> dict[int, str]:
    """Map every line inside a function body to that function's name."""
    owner: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            end = node.end_lineno or node.lineno
            for line in range(node.lineno, end + 1):
                owner[line] = node.name
    return owner


def _attribute_path(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def test_no_distiller_module_reads_the_clock_or_mints_a_run_id() -> None:
    """A module that reads the clock itself opts out of reproducibility.

    Injecting a frozen identity is what makes two runs of the same input compare
    byte for byte; one stray ``datetime.now`` breaks that, and the breakage is
    invisible at runtime. This test is the tripwire for the next such module.
    """
    offenders: list[str] = []
    for module in _python_modules():
        tree = ast.parse(module.read_text(encoding="utf-8"))
        owners = _enclosing_functions(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            path = _attribute_path(node)
            location = f"{module.name}:{node.lineno} {path}"
            if path in {"datetime.now", "datetime.utcnow"}:
                if module.name not in CLOCK_EXEMPT_MODULES:
                    offenders.append(location)
            elif path == "uuid.uuid4":
                owner = owners.get(node.lineno, "<module>")
                if (
                    module.name not in CLOCK_EXEMPT_MODULES
                    and (module.name, owner) not in UUID_EXEMPTIONS
                ):
                    offenders.append(f"{location} in {owner}")

    assert offenders == []


def test_no_distiller_module_takes_a_freeze_switch_from_the_environment() -> None:
    """Freezing happens by injection only. An env-var switch would be a bypass
    around the snapshot-frozen configuration, i.e. a tamper-proofing back door.
    """
    offenders = [
        f"{module.name}:{node.lineno}"
        for module in _python_modules()
        for node in ast.walk(ast.parse(module.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith("AOMT_FROZEN")
    ]

    assert offenders == []


def test_the_clock_tripwire_actually_fires(tmp_path: Path) -> None:
    """Guards the guard: an assertion that can never fail protects nothing."""
    module = tmp_path / "sneaky.py"
    module.write_text("from datetime import datetime\nx = datetime.now()\n", "utf-8")
    tree = ast.parse(module.read_text(encoding="utf-8"))

    found = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and _attribute_path(node) == "datetime.now"
    ]

    assert found, "the AST matcher no longer detects a direct clock read"


# --------------------------------------------------------------------------
# Orchestrator wiring
# --------------------------------------------------------------------------


def test_orchestrator_accepts_a_context_and_defaults_to_none() -> None:
    """Only one new constructor parameter, ever: later features are fields on
    the context, not new parameters on the consumers."""
    import inspect

    from memory_net.distiller.orchestrator import DistillOrchestrator

    parameter = inspect.signature(DistillOrchestrator.__init__).parameters["context"]

    assert parameter.default is None


def test_orchestrator_reports_an_unsettled_reservation(tmp_path: Path) -> None:
    """A leaked reservation means an attempt's cost never landed on the ceiling."""
    budget = RunBudget(1.0, 10.0, max_usd=None)
    budget.reserve(input_tokens_upper=1, output_tokens_upper=1, timeout_s_upper=1.0)

    with pytest.raises(RuntimeError, match="unsettled"):
        budget.assert_all_settled()
