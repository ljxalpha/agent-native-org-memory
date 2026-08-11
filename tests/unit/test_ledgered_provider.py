"""The attempt ledger attached to a provider that has none of its own.

The ledger is the only ceiling that survives across processes. A run budget
lives and dies with one process, so a billable lane that skips the ledger has
no cross-run limit at all -- and the project-wide guarantee is only as strong
as its weakest billable lane.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memory_net.distiller.llm.base import LLMProvider, LLMRequest, LLMResponse
from memory_net.distiller.llm.ledger import AttemptCeilingExceeded, AttemptLedger
from memory_net.distiller.llm.ledgered import LedgeredProvider

SYSTEM_PROMPT = "You distill memories. UNIQUE-SYSTEM-MARKER-8f21."
USER_PROMPT = "Records: ... UNIQUE-USER-MARKER-3c07."

REQUEST = LLMRequest(
    system_prompt=SYSTEM_PROMPT,
    user_prompt=USER_PROMPT,
    model="m",
    max_tokens=16,
    metadata={"run_id": "20260101T000000Z-test0001"},
)


class _Stub(LLMProvider):
    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        if self.fail:
            raise RuntimeError("endpoint said no")
        return LLMResponse(text="{}", model="m", usage={}, usage_source="estimated")


def _ledger(tmp_path: Path, *, ceiling: int = 50) -> AttemptLedger:
    return AttemptLedger(path=tmp_path / "attempts.jsonl", ceiling=ceiling)


def _wrapped(tmp_path: Path, inner: LLMProvider, **kwargs: int) -> LedgeredProvider:
    return LedgeredProvider(
        inner, ledger=_ledger(tmp_path, **kwargs), provider="anthropic", model="m"
    )


def test_a_successful_call_is_counted(tmp_path: Path) -> None:
    provider = _wrapped(tmp_path, _Stub())

    provider.generate(REQUEST)

    assert provider.ledger.spent() == 1


def test_a_failed_call_is_counted_too(tmp_path: Path) -> None:
    """The one that matters. A failed attempt still reached the endpoint and
    still consumed rate budget, so a ceiling counting only successes would let
    a failing loop run without limit -- which is the shape a runaway has."""
    provider = _wrapped(tmp_path, _Stub(fail=True))

    with pytest.raises(RuntimeError):
        provider.generate(REQUEST)

    assert provider.ledger.spent() == 1


def test_an_interrupt_does_not_strand_the_reservation(tmp_path: Path) -> None:
    """A reserved-but-unsettled entry would shrink the remaining ceiling for
    every later run, permanently, on one Ctrl-C."""

    class _Interrupted(LLMProvider):
        def generate(self, request: LLMRequest) -> LLMResponse:
            raise KeyboardInterrupt

    provider = _wrapped(tmp_path, _Interrupted())

    with pytest.raises(KeyboardInterrupt):
        provider.generate(REQUEST)

    outcomes = [entry.get("outcome") for entry in provider.ledger.entries()]
    assert "reserved" not in outcomes[-1:]
    assert provider.ledger.entries()[-1]["outcome"] == "failed"


def test_the_ceiling_refuses_before_the_call_is_made(tmp_path: Path) -> None:
    """Before, not after: a refusal that arrived after the request would have
    already spent the thing it was refusing."""
    inner = _Stub()
    provider = _wrapped(tmp_path, inner, ceiling=2)

    provider.generate(REQUEST)
    provider.generate(REQUEST)
    with pytest.raises(AttemptCeilingExceeded):
        provider.generate(REQUEST)

    assert inner.calls == 2


def test_the_ledger_survives_a_new_provider_instance(tmp_path: Path) -> None:
    """What separates this from the run budget: a second process gets a fresh
    budget but the same ledger."""
    _wrapped(tmp_path, _Stub(), ceiling=2).generate(REQUEST)
    second = _wrapped(tmp_path, _Stub(), ceiling=2)
    second.generate(REQUEST)

    with pytest.raises(AttemptCeilingExceeded):
        _wrapped(tmp_path, _Stub(), ceiling=2).generate(REQUEST)

    assert second.ledger.spent() == 2


def test_the_ledger_records_no_prompt_or_response(tmp_path: Path) -> None:
    """A count does not need content to be auditable, and the ledger is a file
    that outlives the run."""
    provider = _wrapped(tmp_path, _Stub())
    provider.generate(REQUEST)

    written = (tmp_path / "attempts.jsonl").read_text(encoding="utf-8")
    assert "UNIQUE-SYSTEM-MARKER-8f21" not in written
    assert "UNIQUE-USER-MARKER-3c07" not in written
