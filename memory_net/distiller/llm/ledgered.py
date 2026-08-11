"""Attaching the project attempt ledger to a provider that has no ledger of its own.

The ledger is the only ceiling that survives across processes and runs: a run
budget lives and dies with one process, so N sequential runs cost N budgets. A
billable lane that skips the ledger therefore has no cross-run ceiling at all,
and the project-wide guarantee is only as strong as its weakest billable lane.

``OpenAICompatProvider`` reserves for itself, because it has to interleave the
reservation with its own retry loop. Everything else billable is wrapped here.
"""

from __future__ import annotations

from memory_net.distiller.llm.base import LLMProvider, LLMRequest, LLMResponse
from memory_net.distiller.llm.ledger import Attempt, AttemptLedger


class LedgeredProvider(LLMProvider):
    """Count one attempt before each call, and settle it after.

    Before, not after: an attempt that failed still consumed rate budget and
    still reached the endpoint, so a ceiling that counted only successes would
    let a failing loop run without limit -- which is the shape a runaway
    actually has.
    """

    def __init__(
        self,
        inner: LLMProvider,
        *,
        ledger: AttemptLedger,
        provider: str,
        model: str | None = None,
    ) -> None:
        self.inner = inner
        self.ledger = ledger
        self.provider = provider
        self.model = model

    def generate(self, request: LLMRequest) -> LLMResponse:
        attempt = self._reserve(request)
        try:
            response = self.inner.generate(request)
        except BaseException:
            # Settled as failed rather than left reserved: an interrupt here
            # would otherwise strand the entry and permanently shrink the
            # remaining ceiling for every later run.
            self._settle(attempt, outcome="failed")
            raise
        self._settle(attempt, outcome="ok")
        return response

    def _reserve(self, request: LLMRequest) -> Attempt:
        # ``AttemptCeilingExceeded`` propagates rather than being converted:
        # this provider holds no run budget to release, so there is nothing to
        # unwind and nothing the caller can usefully do except stop.
        return self.ledger.reserve(
            kind="distill_live",
            run_id=str((request.metadata or {}).get("run_id") or "unknown"),
            provider=self.provider,
            model=self.model or "",
        )

    def _settle(self, attempt: Attempt, *, outcome: str) -> None:
        self.ledger.settle(attempt, outcome=outcome)
