"""The run ceiling in front of a provider that does not reserve (§5.5.2).

The property under test is pre-call refusal, not accounting: a lane whose only
gate is the pipeline's post-hoc accumulator can always make one over-ceiling
call before anything notices, and never accounts for a retry or a timeout at
all.
"""

import pytest

from memory_net.distiller.llm.base import LLMProvider, LLMRequest, LLMResponse
from memory_net.distiller.llm.budgeted import (
    DEFAULT_TIMEOUT_S_UPPER,
    BudgetedProvider,
)
from memory_net.distiller.runctx import BudgetExhausted, RunBudget


class _Spy(LLMProvider):
    """Counts calls and returns a scripted response, or raises."""

    def __init__(
        self,
        *,
        usage_source: str = "reported",
        usage: dict[str, int] | None = None,
        raises: BaseException | None = None,
        timeout_s: float | None = None,
    ) -> None:
        self.calls = 0
        self.usage_source = usage_source
        self.usage = (
            usage
            if usage is not None
            else {
                "input_tokens": 1,
                "output_tokens": 1,
            }
        )
        self.raises = raises
        if timeout_s is not None:
            self.timeout_s = timeout_s

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return LLMResponse(
            text="{}",
            model=request.model,
            usage=dict(self.usage),
            usage_source=self.usage_source,
            provider_backend="spy",
        )


def _request() -> LLMRequest:
    return LLMRequest(
        system_prompt="you distill",
        user_prompt="records here",
        model="m",
        max_tokens=1000,
    )


def test_an_exhausted_ceiling_refuses_before_the_delegate_is_touched() -> None:
    """The pipeline accumulator can only refuse *after* the expensive call has
    already gone out. Reserving is what makes the refusal come first."""
    spy = _Spy()
    provider = BudgetedProvider(spy, budget=RunBudget(1.0, 10.0, max_usd=0.0))

    with pytest.raises(BudgetExhausted):
        provider.generate(_request())

    assert spy.calls == 0


def test_a_reported_usage_settles_at_its_real_cost() -> None:
    budget = RunBudget(1.0, 10.0, max_usd=None)
    provider = BudgetedProvider(
        _Spy(usage={"input_tokens": 1_000_000, "output_tokens": 0}), budget=budget
    )

    provider.generate(_request())

    assert budget.spent_usd == pytest.approx(1.0)
    assert budget.attempts == 1
    budget.assert_all_settled()


@pytest.mark.parametrize("usage_source", ["estimated", "unknown"])
def test_an_unreported_usage_keeps_the_whole_hold(usage_source: str) -> None:
    """Refunding against a count the backend never confirmed would release
    budget for spend that may well have happened."""
    budget = RunBudget(1.0, 10.0, max_usd=None)
    provider = BudgetedProvider(_Spy(usage_source=usage_source), budget=budget)

    provider.generate(_request())

    # The reserved upper bound, not the near-zero usage the delegate reported.
    assert budget.spent_usd == pytest.approx(
        budget.cost_of(input_tokens=6, output_tokens=1000)
    )
    budget.assert_all_settled()


def test_a_failing_delegate_still_settles_and_keeps_its_hold() -> None:
    """A failure cannot prove the backend did not run, so the hold stays."""
    budget = RunBudget(1.0, 10.0, max_usd=None)
    provider = BudgetedProvider(_Spy(raises=RuntimeError("boom")), budget=budget)

    with pytest.raises(RuntimeError):
        provider.generate(_request())

    budget.assert_all_settled()
    assert budget.attempts == 1
    assert budget.spent_usd > 0.0


def test_an_interrupt_does_not_leak_the_hold() -> None:
    """KeyboardInterrupt is a BaseException: catching only Exception would let
    the reservation escape unsettled."""
    budget = RunBudget(1.0, 10.0, max_usd=None)
    provider = BudgetedProvider(_Spy(raises=KeyboardInterrupt()), budget=budget)

    with pytest.raises(KeyboardInterrupt):
        provider.generate(_request())

    budget.assert_all_settled()


def test_the_ceiling_is_shared_so_a_second_lane_is_starved() -> None:
    """One budget, two wrapped lanes: what the first spends the second cannot."""
    shared = RunBudget(1.0, 10.0, max_usd=1.0)
    first = BudgetedProvider(
        _Spy(usage={"input_tokens": 1_000_000, "output_tokens": 0}), budget=shared
    )
    starved = _Spy()
    second = BudgetedProvider(starved, budget=shared)

    first.generate(_request())

    with pytest.raises(BudgetExhausted):
        second.generate(_request())
    assert starved.calls == 0


def test_a_delegate_that_declares_a_timeout_bounds_its_own_wall_clock_hold() -> None:
    """A provider that knows its ceiling should not be charged a generic one."""
    assert BudgetedProvider(
        _Spy(timeout_s=42.0), budget=RunBudget(1.0, 10.0)
    ).timeout_s_upper == pytest.approx(42.0)
    assert BudgetedProvider(
        _Spy(), budget=RunBudget(1.0, 10.0)
    ).timeout_s_upper == pytest.approx(DEFAULT_TIMEOUT_S_UPPER)


def test_the_wall_clock_ceiling_refuses_a_call_that_cannot_fit() -> None:
    budget = RunBudget(1.0, 10.0, max_usd=None, max_wall_clock_s=10.0)
    spy = _Spy(timeout_s=300.0)

    with pytest.raises(BudgetExhausted, match="wall clock"):
        BudgetedProvider(spy, budget=budget).generate(_request())

    assert spy.calls == 0
