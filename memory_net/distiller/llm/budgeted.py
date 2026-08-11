"""Puts a run ceiling in front of a provider that does not reserve for itself.

``RunBudget`` only binds providers that call ``reserve()``. Today that is the
production backend alone, so every other paid lane -- the Anthropic SDK, the
out-of-process bridge -- reaches the network with nothing but the pipeline's
post-hoc accumulator behind it. That accumulator sums the cost of calls that
*returned*, so it can only refuse the call after the expensive one has already
been made, and it never sees a retry or a timed-out attempt at all.

This decorator closes that gap without every provider having to learn about
budgets: it reserves before delegating and settles on the way out. It is
deliberately *not* applied to the mock lane. The mock costs nothing, and
charging it against a configured ``budget_usd`` would let the shipped default
profile starve its own deterministic run.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from memory_net.distiller.llm.base import LLMProvider, LLMRequest, LLMResponse
from memory_net.distiller.runctx import RunBudget, prompt_tokens_upper

DEFAULT_TIMEOUT_S_UPPER = 300.0
"""Wall-clock upper bound assumed for a provider that declares none.

Only used for the wall-clock dimension of the reservation. A provider that
knows its own ceiling exposes ``timeout_s`` and that value is preferred.
"""


class BudgetedProvider(LLMProvider):
    """Reserve, delegate, settle -- around a provider that does none of it."""

    def __init__(
        self,
        inner: LLMProvider,
        *,
        budget: RunBudget,
        timeout_s_upper: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.inner = inner
        self.budget = budget
        self.timeout_s_upper = (
            timeout_s_upper
            if timeout_s_upper is not None
            else float(getattr(inner, "timeout_s", DEFAULT_TIMEOUT_S_UPPER))
        )
        self.monotonic = monotonic

    def generate(self, request: LLMRequest) -> LLMResponse:
        started = self.monotonic()
        # Raises BudgetExhausted before the delegate is touched. That is the
        # whole point: settling afterwards can report an overspend but not
        # prevent one.
        reservation = self.budget.reserve(
            input_tokens_upper=prompt_tokens_upper(
                request.system_prompt, request.user_prompt
            ),
            output_tokens_upper=request.max_tokens,
            timeout_s_upper=self.timeout_s_upper,
        )
        try:
            response = self.inner.generate(request)
        except BaseException:
            # BaseException, not Exception: an interrupt between the reserve and
            # the settle would otherwise leak the hold, and an unsettled hold is
            # an attempt whose cost silently missed the ceiling. A failure here
            # cannot prove the backend did not run, so the hold is kept.
            reservation.settle_unknown(wall_s=self.monotonic() - started)
            raise
        wall_s = self.monotonic() - started
        if response.usage_source == "reported":
            reservation.settle(usage=response.usage, wall_s=wall_s)
        else:
            # An estimated or unknown count is not a basis for refunding the
            # difference against the reserved bound: the refund would release
            # budget for spend that may well have happened.
            reservation.settle_unknown(wall_s=wall_s)
        return response
