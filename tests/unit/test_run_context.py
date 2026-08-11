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


class _CountingMock(MockLLMProvider):
    """A mock that reports whether the gate let a call through."""

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        return super().generate(request)


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

    # Deliberately passing a *different* run_id than the identity carries would
    # raise; passing none lets the identity supply it, which is the contract.
    first = _pipeline(tmp_path, context=context).run([_record()], run_id="")
    second = _pipeline(tmp_path, context=context).run([_record()], run_id="")

    assert first.products
    assert [p.content for p in first.products] == [p.content for p in second.products]
    assert "2026-01-01T00:00:00Z" in first.products[0].content
    # The identity supplied the id, not the caller.
    assert "run-fixed" in first.products[0].frontmatter["distillation_id"]


def test_without_a_context_the_pipeline_keeps_its_wall_clock_behaviour(
    tmp_path: Path,
) -> None:
    """The default must stay byte-identical to the pre-injection behaviour, or
    the wiring could not land one consumer at a time."""
    output = _pipeline(tmp_path).run([_record()], run_id="run")

    assert output.products
    assert output.products[0].frontmatter["generated_at"].endswith("Z")


def test_a_run_id_conflicting_with_the_injected_identity_is_rejected(
    tmp_path: Path,
) -> None:
    """Two ids for one run would land in different artifacts of that run and
    only surface later, as a manifest nobody can reconcile."""
    context = RunContext(
        identity=RunIdentity(run_id="run-a", now=lambda: "2026-01-01T00:00:00Z")
    )

    with pytest.raises(ValueError, match="conflicts with the injected identity"):
        _pipeline(tmp_path, context=context).run([_record()], run_id="run-b")


# --------------------------------------------------------------------------
# Budget injection
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


def test_the_legacy_gate_survives_an_injected_budget(tmp_path: Path) -> None:
    """A RunBudget only constrains providers that call reserve(). Switching the
    accumulator off whenever a budget exists would leave the mock, Anthropic and
    bridge lanes with no ceiling at all -- which is the default profile exactly:
    a mock provider plus a configured budget_usd.
    """
    pipeline = _pipeline(
        tmp_path,
        cost_budget_usd=0.0,
        context=RunContext(budget=RunBudget(1.0, 10.0, max_usd=100.0)),
    )

    assert pipeline._budget_exhausted(0.0) is True
    assert pipeline.run([_record()], run_id="run").skipped_clusters == 1


def test_a_mock_lane_under_a_configured_budget_is_still_gated(
    tmp_path: Path,
) -> None:
    """The regression this guards: the shipped default profile pairs
    provider: mock with budget_usd, and the mock never reserves."""
    provider = _CountingMock()
    pipeline = _pipeline(
        tmp_path,
        provider=provider,
        cost_budget_usd=0.0,
        # Ample headroom, and never consulted: the mock lane never reserves.
        context=RunContext(budget=RunBudget(1.0, 10.0, max_usd=1000.0)),
    )

    output = pipeline.run([_record()], run_id="run")

    assert provider.calls == 0, "an exhausted cost budget must stop the call"
    assert output.products == []
    assert output.skipped_clusters == 1


def test_without_a_context_the_legacy_cost_gate_still_applies(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path, cost_budget_usd=0.0)

    assert pipeline._budget_exhausted(0.0) is True
    assert pipeline.run([_record()], run_id="run").skipped_clusters == 1


# --------------------------------------------------------------------------
# Structural tripwire
# --------------------------------------------------------------------------

BANNED_CLOCK = {"datetime.datetime.now", "datetime.datetime.utcnow"}
BANNED_IDGEN = {"uuid.uuid4"}

# Exempt by *named function*, not by whole module: exempting a whole file lets
# any later function in it acquire an ambient clock for free.
EXEMPT: dict[str, set[str]] = {
    # The one sanctioned clock/run-id factory (utc_now, RunIdentity.system).
    "runctx.py": {"utc_now", "system"},
    # Names a temp directory alongside os.getpid(). The value never reaches a
    # product, manifest or metrics file.
    "orchestrator.py": {"_write_team_products"},
}


def _alias_map(tree: ast.AST) -> dict[str, str]:
    """Local name -> canonical dotted path, so aliased imports still resolve."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for entry in node.names:
                root = entry.name.split(".")[0]
                aliases[entry.asname or root] = entry.name if entry.asname else root
        elif isinstance(node, ast.ImportFrom) and node.module:
            for entry in node.names:
                aliases[entry.asname or entry.name] = f"{node.module}.{entry.name}"
    return aliases


def _resolve(node: ast.AST, aliases: dict[str, str]) -> str | None:
    """Canonical dotted path of an attribute/name expression, if resolvable."""
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    return ".".join([aliases.get(current.id, current.id), *reversed(parts)])


def _owner_by_line(tree: ast.AST) -> dict[int, str]:
    owner: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for line in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                owner[line] = node.name
    return owner


def scan_for_ambient_run_state(root: Path) -> list[str]:
    """Find modules that read the clock, mint a run id, or take a freeze switch.

    Extracted as a function taking a root so the tripwire itself can be run
    end to end against a synthetic tree -- an assertion that can never fail
    protects nothing.
    """
    offenders: list[str] = []
    for module in sorted(root.rglob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        aliases = _alias_map(tree)
        owners = _owner_by_line(tree)
        exempt = EXEMPT.get(module.name, set())
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.startswith("AOMT_FROZEN"):
                    offenders.append(f"{module.name}:{node.lineno} {node.value}")
                continue
            if not isinstance(node, ast.Attribute | ast.Name):
                continue
            if isinstance(node, ast.Name) and isinstance(
                getattr(node, "ctx", None), ast.Store
            ):
                continue
            path = _resolve(node, aliases)
            if path is None or path not in (BANNED_CLOCK | BANNED_IDGEN):
                continue
            if owners.get(node.lineno, "<module>") in exempt:
                continue
            offenders.append(f"{module.name}:{node.lineno} {path}")
    return sorted(set(offenders))


def test_no_distiller_module_carries_ambient_run_state() -> None:
    """A module that reads the clock itself opts out of reproducibility.

    Injecting a frozen identity is what makes two runs of the same input
    compare byte for byte; one stray clock read breaks that, invisibly at
    runtime. An env-var freeze switch is the same hole from the other side: it
    would be a back door around the snapshot-frozen configuration.
    """
    assert scan_for_ambient_run_state(DISTILLER_DIR) == []


@pytest.mark.parametrize(
    "source",
    [
        "from datetime import datetime\nx = datetime.now()\n",
        "import datetime\nx = datetime.datetime.now()\n",
        # The alias forms a naive matcher would miss entirely.
        "from datetime import datetime as D\nx = D.now()\n",
        "import datetime as dt\nx = dt.datetime.utcnow()\n",
        "import uuid\nx = uuid.uuid4()\n",
        "from uuid import uuid4 as fresh\nx = fresh()\n",
        "import os\nx = os.environ.get('AOMT_FROZEN_RUN_ID')\n",
    ],
)
def test_the_tripwire_actually_fires(tmp_path: Path, source: str) -> None:
    """Guards the guard, end to end on a real tree rather than on a matcher."""
    (tmp_path / "sneaky.py").write_text(source, encoding="utf-8")

    assert scan_for_ambient_run_state(tmp_path) != []


def test_the_tripwire_does_not_fire_on_clean_code(tmp_path: Path) -> None:
    """The other half: a scanner that flags everything is equally useless."""
    (tmp_path / "clean.py").write_text(
        "from memory_net.distiller.runctx import RunIdentity\n"
        "def f(identity: RunIdentity) -> str:\n    return identity.now()\n",
        encoding="utf-8",
    )

    assert scan_for_ambient_run_state(tmp_path) == []


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
