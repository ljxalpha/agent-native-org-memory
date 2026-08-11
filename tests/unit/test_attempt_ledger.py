"""The project-level attempt ledger (§5.5.14).

Two properties carry the weight. The ceiling must not be raisable from the
environment -- a limit the caller can raise is not a limit -- and the count must
survive a crash, since a counter that resets on restart lets the same ceiling be
spent twice.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from memory_net.distiller.llm.ledger import (
    ATTEMPT_CEILING_HARD,
    CEILING_ENV_VAR,
    LEDGER_ENV_VAR,
    AttemptCeilingExceeded,
    AttemptLedger,
    LedgerError,
    attempt_ceiling,
    default_ledger_path,
)


def _ledger(tmp_path: Path, *, ceiling: int | None = None) -> AttemptLedger:
    return AttemptLedger(tmp_path / "state" / "llm_attempts.jsonl", ceiling=ceiling)


# --------------------------------------------------------------------------
# The ceiling
# --------------------------------------------------------------------------


def test_the_hard_ceiling_is_a_constant() -> None:
    assert ATTEMPT_CEILING_HARD == 50
    assert attempt_ceiling({}) == ATTEMPT_CEILING_HARD


def test_the_environment_may_lower_the_ceiling() -> None:
    assert attempt_ceiling({CEILING_ENV_VAR: "5"}) == 5


def test_the_environment_cannot_raise_the_ceiling() -> None:
    """The negative case this constant exists for: a ceiling an environment
    variable can raise belongs to whoever sets that variable."""
    assert attempt_ceiling({CEILING_ENV_VAR: "999"}) == ATTEMPT_CEILING_HARD


@pytest.mark.parametrize("value", ["", "lots", "-1", "1e9"])
def test_an_unusable_ceiling_value_falls_back_to_the_constant(value: str) -> None:
    assert attempt_ceiling({CEILING_ENV_VAR: value}) == ATTEMPT_CEILING_HARD


def test_a_constructed_ceiling_cannot_exceed_the_constant(tmp_path: Path) -> None:
    """The injection point exists for tests, not for headroom: a ceiling the
    constructor can raise is a limit on one code path, not on the project."""
    ledger = AttemptLedger(tmp_path / "l.jsonl", ceiling=999)

    assert ledger.ceiling == ATTEMPT_CEILING_HARD


def test_a_constructed_ceiling_may_still_be_lower(tmp_path: Path) -> None:
    assert AttemptLedger(tmp_path / "l.jsonl", ceiling=3).ceiling == 3


@pytest.mark.parametrize("value", [-1, True, 2.5, "5"])
def test_an_unusable_constructed_ceiling_is_refused(value: Any, tmp_path: Path) -> None:
    """Refused rather than coerced: ``ceiling=True`` reads as one attempt."""
    with pytest.raises(LedgerError):
        AttemptLedger(tmp_path / "l.jsonl", ceiling=value)


def test_the_ledger_path_follows_the_environment(tmp_path: Path) -> None:
    assert default_ledger_path({LEDGER_ENV_VAR: str(tmp_path / "l.jsonl")}) == (
        tmp_path / "l.jsonl"
    )
    assert default_ledger_path({"MEMORY_NET_HOME": str(tmp_path)}) == (
        tmp_path / "state" / "llm_attempts.jsonl"
    )


# --------------------------------------------------------------------------
# Counting
# --------------------------------------------------------------------------


def test_each_reservation_is_counted_once(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, ceiling=3)

    ledger.reserve(kind="distill_live", run_id="r1", provider="deepseek")
    ledger.reserve(kind="distill_live", run_id="r1", provider="deepseek")

    assert ledger.spent() == 2
    assert ledger.remaining() == 1


def test_settling_does_not_un_count_an_attempt(tmp_path: Path) -> None:
    """The request went out; whether the endpoint billed it is not knowable
    from this side, so a failure still costs an attempt."""
    ledger = _ledger(tmp_path, ceiling=3)
    attempt = ledger.reserve(kind="distill_live", run_id="r1")

    ledger.settle(attempt, outcome="failed")

    assert ledger.spent() == 1


def test_reaching_the_ceiling_refuses_further_attempts(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, ceiling=1)
    ledger.reserve(kind="distill_live", run_id="r1")

    with pytest.raises(AttemptCeilingExceeded, match="1/1"):
        ledger.reserve(kind="distill_live", run_id="r1")

    assert ledger.spent() == 1


def test_a_refusal_is_recorded_but_not_counted(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, ceiling=1)
    ledger.reserve(kind="distill_live", run_id="r1")
    with pytest.raises(AttemptCeilingExceeded):
        ledger.reserve(kind="tier_b", run_id="r2")

    outcomes = [entry["outcome"] for entry in ledger.entries()]

    assert outcomes == ["reserved", "refused"]
    assert ledger.spent() == 1


def test_an_unknown_kind_is_refused(tmp_path: Path) -> None:
    with pytest.raises(LedgerError, match="attempt kind"):
        _ledger(tmp_path, ceiling=5).reserve(kind="whatever", run_id="r1")


def test_the_ledger_records_no_content(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, ceiling=5)

    ledger.reserve(
        kind="distill_live", run_id="r1", provider="deepseek", model="a-model"
    )
    text = ledger.path.read_text(encoding="utf-8")

    for entry in ledger.entries():
        assert set(entry) <= {
            "kind",
            "outcome",
            "run_id",
            "sequence",
            "settles",
            "ts",
            "provider",
            "model",
        }
    assert "prompt" not in text
    assert "api_key" not in text.lower()


# --------------------------------------------------------------------------
# Surviving a crash, and a second process
# --------------------------------------------------------------------------


def test_the_count_is_recovered_from_the_file(tmp_path: Path) -> None:
    """A fresh instance -- a restarted process -- sees what was already spent."""
    first = _ledger(tmp_path, ceiling=2)
    first.reserve(kind="distill_live", run_id="r1")

    second = _ledger(tmp_path, ceiling=2)

    assert second.spent() == 1
    second.reserve(kind="distill_live", run_id="r2")
    with pytest.raises(AttemptCeilingExceeded):
        _ledger(tmp_path, ceiling=2).reserve(kind="distill_live", run_id="r3")


def test_a_torn_final_line_is_skipped(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, ceiling=5)
    ledger.reserve(kind="distill_live", run_id="r1")
    with ledger.path.open("a", encoding="utf-8") as handle:
        handle.write('{"kind": "distill_live", "outcome": "reserv')

    assert ledger.spent() == 1


def test_two_processes_cannot_exceed_the_ceiling_together(tmp_path: Path) -> None:
    """The lock is the only thing between two runs and a double spend."""
    ledger_path = tmp_path / "state" / "llm_attempts.jsonl"
    program = (
        "from memory_net.distiller.llm.ledger import AttemptLedger, "
        "AttemptCeilingExceeded\n"
        "from pathlib import Path\n"
        "import sys\n"
        "ledger = AttemptLedger(Path(sys.argv[1]), ceiling=4)\n"
        "taken = 0\n"
        "for _ in range(4):\n"
        "    try:\n"
        "        ledger.reserve(kind='distill_live', run_id=sys.argv[2])\n"
        "        taken += 1\n"
        "    except AttemptCeilingExceeded:\n"
        "        break\n"
        "print(taken)\n"
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", program, str(ledger_path), f"run-{index}"],
            stdout=subprocess.PIPE,
            text=True,
            cwd=Path.cwd(),
        )
        for index in range(2)
    ]
    taken = [int(process.communicate()[0].strip()) for process in processes]

    assert sum(taken) == 4
    assert AttemptLedger(ledger_path, ceiling=4).spent() == 4


def test_the_lock_is_released_after_each_reservation(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, ceiling=5)

    ledger.reserve(kind="distill_live", run_id="r1")

    assert not ledger.lock_path.exists()


def test_a_held_lock_eventually_fails_loudly(tmp_path: Path, monkeypatch: Any) -> None:
    ledger = _ledger(tmp_path, ceiling=5)
    ledger.lock_path.parent.mkdir(parents=True, exist_ok=True)
    ledger.lock_path.touch()
    monkeypatch.setattr("memory_net.distiller.llm.ledger._LOCK_TIMEOUT_S", 0.05)

    with pytest.raises(LedgerError, match="lock is held"):
        ledger.reserve(kind="distill_live", run_id="r1")


def test_entries_are_json_lines(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, ceiling=5)
    ledger.reserve(kind="distill_live", run_id="r1")

    lines = ledger.path.read_text(encoding="utf-8").splitlines()

    assert [json.loads(line)["run_id"] for line in lines] == ["r1"]


# --------------------------------------------------------------------------
# The production provider reserves before every request
# --------------------------------------------------------------------------


class _CountingClient:
    """Stands in for the SDK client, and counts requests actually made."""

    def __init__(self, error: Exception | None = None) -> None:
        self.requests = 0
        self.error = error
        self.chat = self

    @property
    def completions(self) -> _CountingClient:
        return self

    def create(self, **_kwargs: Any) -> Any:
        self.requests += 1
        if self.error is not None:
            raise self.error
        message = type("Message", (), {"content": '{"products": []}'})()
        choice = type("Choice", (), {"message": message, "finish_reason": "stop"})()
        usage = type("Usage", (), {"prompt_tokens": 1, "completion_tokens": 1})()
        return type(
            "Completion", (), {"choices": [choice], "usage": usage, "model": "m"}
        )()


def _request() -> Any:
    from memory_net.distiller.llm.base import LLMRequest

    return LLMRequest(
        system_prompt="s",
        user_prompt="u",
        model="m",
        metadata={"run_id": "20260810T101010Z-a1b2c3d4"},
    )


def test_the_provider_records_one_attempt_per_request(tmp_path: Path) -> None:
    from memory_net.distiller.llm.openai_compat import OpenAICompatProvider

    ledger = _ledger(tmp_path, ceiling=5)
    client = _CountingClient()
    provider = OpenAICompatProvider(client=client, ledger=ledger, model="m")

    provider.generate(_request())

    assert client.requests == 1
    assert ledger.spent() == 1
    reserved = [item for item in ledger.entries() if item["outcome"] == "reserved"]
    assert reserved[0]["run_id"] == "20260810T101010Z-a1b2c3d4"
    assert reserved[0]["provider"] == "deepseek"


def test_the_provider_makes_no_request_once_the_ceiling_is_reached(
    tmp_path: Path,
) -> None:
    """The assertion that matters: the refusal happens before the wire, so the
    fake client's request count stays at zero."""
    from memory_net.distiller.llm.openai_compat import OpenAICompatProvider
    from memory_net.distiller.runctx import BudgetExhausted

    ledger = _ledger(tmp_path, ceiling=1)
    ledger.reserve(kind="distill_live", run_id="earlier-run")
    client = _CountingClient()
    provider = OpenAICompatProvider(client=client, ledger=ledger, model="m")

    with pytest.raises(BudgetExhausted, match="attempt ceiling"):
        provider.generate(_request())

    assert client.requests == 0
    assert ledger.spent() == 1


def test_a_refused_attempt_leaves_no_unsettled_run_budget(tmp_path: Path) -> None:
    """The run budget hold taken just before must be released, or the run ends
    reporting a leaked reservation instead of an exhausted ceiling."""
    from memory_net.distiller.llm.openai_compat import OpenAICompatProvider
    from memory_net.distiller.runctx import BudgetExhausted, RunBudget

    ledger = _ledger(tmp_path, ceiling=1)
    ledger.reserve(kind="distill_live", run_id="earlier-run")
    budget = RunBudget(input_per_mtok_usd=0.6, output_per_mtok_usd=2.4, max_usd=10.0)
    provider = OpenAICompatProvider(
        client=_CountingClient(), ledger=ledger, model="m", budget=budget
    )

    with pytest.raises(BudgetExhausted):
        provider.generate(_request())

    budget.assert_all_settled()


def test_a_broken_ledger_leaves_no_unsettled_run_budget(tmp_path: Path) -> None:
    """A ledger that cannot be written is not a refusal but a fault, and the
    hold taken one line earlier still has to be given back -- the orchestrator
    skips its leaked-reservation check while an exception is propagating."""
    from memory_net.distiller.llm.openai_compat import OpenAICompatProvider
    from memory_net.distiller.runctx import RunBudget

    class _BrokenLedger(AttemptLedger):
        def reserve(self, *, kind: str, run_id: str, **facts: str) -> Any:
            raise LedgerError("the ledger lock is held")

    budget = RunBudget(input_per_mtok_usd=0.6, output_per_mtok_usd=2.4, max_usd=10.0)
    client = _CountingClient()
    provider = OpenAICompatProvider(
        client=client,
        ledger=_BrokenLedger(tmp_path / "l.jsonl", ceiling=5),
        model="m",
        budget=budget,
    )

    with pytest.raises(LedgerError):
        provider.generate(_request())

    assert client.requests == 0
    budget.assert_all_settled()


def test_each_retry_costs_its_own_attempt(tmp_path: Path) -> None:
    """A retry is a second request on the wire, and the endpoint bills it."""
    from memory_net.distiller.llm.openai_compat import OpenAICompatProvider

    ledger = _ledger(tmp_path, ceiling=5)
    client = _CountingClient(error=TimeoutError("upstream timed out"))
    provider = OpenAICompatProvider(
        client=client, ledger=ledger, model="m", max_retries=2, sleep=lambda _s: None
    )

    with pytest.raises(Exception):  # noqa: B017 - sanitized provider error
        provider.generate(_request())

    assert client.requests == 3
    assert ledger.spent() == 3


def test_without_a_ledger_the_provider_is_unchanged(tmp_path: Path) -> None:
    from memory_net.distiller.llm.openai_compat import OpenAICompatProvider

    client = _CountingClient()
    provider = OpenAICompatProvider(client=client, model="m")

    provider.generate(_request())

    assert client.requests == 1
    assert provider.ledger is None
