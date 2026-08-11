"""DeepSeek / OpenAI-compatible production provider (§5.3.3).

Every test drives an injected fake client. No network call is made and no
credential is read: the four properties that made this the production backend --
role separation, a service-level JSON constraint, a real max_tokens, and
service-reported usage -- are all observable at the client boundary.
"""

from typing import Any

import pytest

from memory_net.distiller.llm.base import LLMRequest
from memory_net.distiller.llm.openai_compat import (
    PROVIDER_BACKEND,
    OpenAICompatProvider,
    ProviderCallError,
)
from memory_net.distiller.runctx import BudgetExhausted, RunBudget


class _Usage:
    def __init__(self, prompt_tokens: Any, completion_tokens: Any) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _PartialUsage:
    """A usage object carrying only the output side, as a terse backend may."""

    def __init__(self, completion_tokens: int) -> None:
        self.completion_tokens = completion_tokens


class _Message:
    def __init__(self, content: str) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str, finish_reason: str) -> None:
        self.message = _Message(content)
        self.finish_reason = finish_reason


class _Completion:
    def __init__(
        self,
        *,
        content: str = '{"products": []}',
        finish_reason: str = "stop",
        usage: _Usage | None = None,
        model: str = "deepseek-v4-flash",
    ) -> None:
        self.choices = [_Choice(content, finish_reason)]
        self.usage = usage
        self.model = model


class _FakeClient:
    """Records the kwargs it was called with; replays a scripted outcome list."""

    def __init__(self, outcomes: list[Any]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []
        self.chat = self

    @property
    def completions(self) -> "_FakeClient":
        return self

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _HTTPError(Exception):
    """Stands in for the SDK's status-carrying error type."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"http {status_code}")
        self.status_code = status_code


class _TimeoutError(Exception):
    """Named so the classifier's duck typing sees a timeout."""


def _provider(
    outcomes: list[Any], **kwargs: Any
) -> tuple[OpenAICompatProvider, _FakeClient]:
    client = _FakeClient(outcomes)
    provider = OpenAICompatProvider(
        api_key="unused-because-a-client-is-injected",
        base_url="https://example.invalid/v1",
        model="deepseek-v4-flash",
        client=client,
        sleep=lambda _seconds: None,
        **kwargs,
    )
    return provider, client


def _request() -> LLMRequest:
    return LLMRequest(
        system_prompt="you distill",
        user_prompt="records here",
        model="ignored-when-model-is-set",
        max_tokens=1234,
    )


# --------------------------------------------------------------------------
# The four capabilities that justify this being the production backend
# --------------------------------------------------------------------------


def test_system_and_user_stay_two_separate_messages() -> None:
    """Leaf records are untrusted input. Concatenating them into the system
    instruction would let a record rewrite the instruction."""
    provider, client = _provider([_Completion(usage=_Usage(11, 22))])

    provider.generate(_request())

    assert client.calls[0]["messages"] == [
        {"role": "system", "content": "you distill"},
        {"role": "user", "content": "records here"},
    ]


def test_the_json_object_constraint_and_max_tokens_are_sent() -> None:
    provider, client = _provider([_Completion(usage=_Usage(1, 1))])

    provider.generate(_request())

    assert client.calls[0]["response_format"] == {"type": "json_object"}
    assert client.calls[0]["max_tokens"] == 1234


def test_service_reported_usage_is_carried_through_verbatim() -> None:
    provider, _ = _provider([_Completion(usage=_Usage(11, 22))])

    response = provider.generate(_request())

    assert response.usage == {"input_tokens": 11, "output_tokens": 22}
    assert response.usage_source == "reported"
    assert response.provider_backend == PROVIDER_BACKEND
    assert response.raw_meta == {
        "finish_reason": "stop",
        "response_model": "deepseek-v4-flash",
    }


def test_a_missing_usage_object_is_estimated_never_claimed_as_reported() -> None:
    """Labelling a local estimate as service-reported would make the run-level
    aggregate assert something the backend never said."""
    provider, _ = _provider([_Completion(content="12345678", usage=None)])

    response = provider.generate(_request())

    assert response.usage_source == "estimated"
    assert response.usage["output_tokens"] == 2


@pytest.mark.parametrize(
    "usage",
    [
        pytest.param(_Usage(None, 22), id="null-prompt-tokens"),
        pytest.param(_Usage(11, None), id="null-completion-tokens"),
        pytest.param(_PartialUsage(22), id="prompt-tokens-absent"),
    ],
)
def test_a_usage_object_missing_a_counter_is_not_a_report(usage: Any) -> None:
    """A present-but-incomplete usage object is not the service reporting its
    billing. Coercing the gap to zero would claim ``reported`` for a call the
    backend never accounted for, which is exactly the claim A1 rests on."""
    provider, _ = _provider([_Completion(content="12345678", usage=usage)])

    response = provider.generate(_request())

    assert response.usage_source == "estimated"
    assert response.usage == {"input_tokens": 0, "output_tokens": 2}


def test_an_unreported_usage_keeps_the_whole_hold_instead_of_refunding_it() -> None:
    """Settling refunds the difference against the reserved bound. An estimate
    derived from the response text knows nothing about the input tokens that
    were billed, so refunding against it releases budget for real spend."""
    reported_budget = RunBudget(1.0, 10.0, max_usd=None)
    estimated_budget = RunBudget(1.0, 10.0, max_usd=None)
    reported, _ = _provider(
        [_Completion(content="12345678", usage=_Usage(1, 1))], budget=reported_budget
    )
    estimated, _ = _provider(
        [_Completion(content="12345678", usage=None)], budget=estimated_budget
    )

    reported.generate(_request())
    estimated.generate(_request())

    # The reserved upper bound is the same for both: the same prompt, the same
    # max_tokens. Only the settlement differs.
    assert estimated_budget.spent_usd > reported_budget.spent_usd
    assert estimated_budget.spent_usd == pytest.approx(
        estimated_budget.cost_of(input_tokens=6, output_tokens=1234)
    )
    reported_budget.assert_all_settled()
    estimated_budget.assert_all_settled()


def test_a_truncated_response_raises_instead_of_returning_half_a_json() -> None:
    """finish_reason=length means the JSON is cut off. Passing it downstream
    only converts a clear failure into a confusing parse error."""
    provider, _ = _provider(
        [_Completion(content='{"products": [', finish_reason="length")]
    )

    with pytest.raises(RuntimeError, match="truncated"):
        provider.generate(_request())


def test_a_truncated_response_is_still_settled_because_it_was_billed() -> None:
    budget = RunBudget(1.0, 10.0, max_usd=None)
    provider, _ = _provider(
        [_Completion(content="x", finish_reason="length", usage=_Usage(1_000_000, 0))],
        budget=budget,
    )

    with pytest.raises(RuntimeError, match="truncated"):
        provider.generate(_request())

    assert budget.snapshot()["attempts"] == 1
    assert budget.snapshot()["spent_usd"] == pytest.approx(1.0)
    budget.assert_all_settled()


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------


def test_a_missing_api_key_raises_before_any_call(monkeypatch: Any) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        OpenAICompatProvider().generate(_request())


def test_a_missing_base_url_raises_before_any_call(monkeypatch: Any) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="DEEPSEEK_BASE_URL"):
        OpenAICompatProvider().generate(_request())


def test_the_provider_never_reads_a_credential_file() -> None:
    """Reading a dotenv file here would pull in whatever else lives beside the
    two keys we are entitled to; narrowing that down is the launcher's job.

    Checked structurally rather than by behaviour because the failure mode is a
    future edit adding a convenience file read, not a bug in today's code.
    """
    import ast
    import inspect

    from memory_net.distiller.llm import openai_compat

    tree = ast.parse(inspect.getsource(openai_compat))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    } | {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }

    assert not called & {"open", "read_text", "read_bytes", "load_dotenv"}
    assert "dotenv" not in imported
    # The only environment reads are the two credential names.
    assert {"DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL"} == {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith("DEEPSEEK_")
    }


# --------------------------------------------------------------------------
# Retry / budget interaction
# --------------------------------------------------------------------------


def test_a_429_is_retried_and_the_second_attempt_succeeds() -> None:
    provider, client = _provider([_HTTPError(429), _Completion(usage=_Usage(1, 1))])

    response = provider.generate(_request())

    assert len(client.calls) == 2
    assert response.usage_source == "reported"


def test_retries_stop_at_the_configured_ceiling() -> None:
    provider, client = _provider(
        [_HTTPError(500), _HTTPError(500), _HTTPError(500)], max_retries=2
    )

    with pytest.raises(ProviderCallError):
        provider.generate(_request())

    assert len(client.calls) == 3


def test_a_client_error_is_not_retried() -> None:
    """A 400 was rejected before inference; sending it again changes nothing."""
    provider, client = _provider([_HTTPError(400), _Completion()])

    with pytest.raises(ProviderCallError):
        provider.generate(_request())

    assert len(client.calls) == 1


def test_the_escaping_error_restates_the_failure_without_the_sdk_message() -> None:
    """A failed run has str(exc) written into its failure health and metrics
    files, which CI publishes. The SDK builds its message from the request it
    just made, so that message must not be the one that escapes."""

    class _Chatty(Exception):
        def __init__(self) -> None:
            super().__init__(
                "POST https://secret-endpoint.invalid/v1/chat/completions "
                "failed (Authorization: Bearer sk-live-topsecret)"
            )
            self.status_code = 503

    provider, _ = _provider([_Chatty(), _Chatty(), _Chatty()], max_retries=2)

    with pytest.raises(ProviderCallError) as raised:
        provider.generate(_request())

    message = str(raised.value)
    assert "sk-live-topsecret" not in message
    assert "secret-endpoint.invalid" not in message
    # Still actionable: the type, the status and how many attempts were made.
    assert "_Chatty" in message
    assert "status=503" in message
    assert "3 attempt(s)" in message
    # The original stays reachable for an interactive traceback, which never
    # reaches disk -- only str(exc) does.
    assert isinstance(raised.value.__cause__, _Chatty)


def test_an_exhausted_budget_stops_the_call_from_being_made_at_all() -> None:
    """The whole point of reserving up front: settling afterwards can report an
    overspend but cannot prevent one."""
    budget = RunBudget(1.0, 10.0, max_usd=0.0)
    provider, client = _provider([_Completion(usage=_Usage(1, 1))], budget=budget)

    with pytest.raises(BudgetExhausted):
        provider.generate(_request())

    assert client.calls == []


def test_a_timeout_keeps_its_hold_but_a_rejected_request_gets_it_back() -> None:
    """An unknown outcome may already have been billed upstream; a request the
    server rejected before inference provably was not."""
    unknown_budget = RunBudget(1.0, 10.0, max_usd=None)
    released_budget = RunBudget(1.0, 10.0, max_usd=None)
    timeout_provider, _ = _provider(
        [_TimeoutError(), _TimeoutError(), _TimeoutError()],
        budget=unknown_budget,
        max_retries=2,
    )
    rejected_provider, _ = _provider([_HTTPError(401)], budget=released_budget)

    with pytest.raises(ProviderCallError):
        timeout_provider.generate(_request())
    with pytest.raises(ProviderCallError):
        rejected_provider.generate(_request())

    assert unknown_budget.snapshot()["spent_usd"] > 0.0
    assert released_budget.snapshot()["spent_usd"] == pytest.approx(0.0)


def test_three_attempts_count_three_and_stay_inside_the_ceiling() -> None:
    """Timeout -> retry -> retry, all against one shared run budget. The
    reservation owns the counter, so no attempt is counted twice."""
    budget = RunBudget(1.0, 10.0, max_usd=10.0, max_wall_clock_s=None)
    provider, client = _provider(
        [_TimeoutError(), _TimeoutError(), _TimeoutError()],
        budget=budget,
        max_retries=2,
    )

    with pytest.raises(ProviderCallError):
        provider.generate(_request())

    assert len(client.calls) == 3
    assert budget.snapshot()["attempts"] == 3
    assert budget.snapshot()["spent_usd"] <= 10.0
    budget.assert_all_settled()


def test_the_shared_budget_starves_the_fallback_lane_after_the_primary_spends() -> None:
    """primary and fallback are distinct provider instances sharing one budget,
    which is what stops a run's ceiling from being spent twice over."""
    shared = RunBudget(1.0, 10.0, max_usd=1.0)
    primary, _ = _provider([_Completion(usage=_Usage(1_000_000, 0))], budget=shared)
    fallback, fallback_client = _provider([_Completion()], budget=shared)

    primary.generate(_request())

    with pytest.raises(BudgetExhausted):
        fallback.generate(_request())
    assert fallback_client.calls == []


def test_every_attempt_settles_its_reservation() -> None:
    """A leaked hold is an attempt whose cost silently missed the ceiling."""
    budget = RunBudget(1.0, 10.0, max_usd=None)
    provider, _ = _provider(
        [_HTTPError(500), _HTTPError(429), _Completion(usage=_Usage(1, 1))],
        budget=budget,
        max_retries=2,
    )

    provider.generate(_request())

    budget.assert_all_settled()
    assert budget.snapshot()["attempts"] == 3


# --------------------------------------------------------------------------
# Every attempt reaches a terminal state, including on the ugly paths
# --------------------------------------------------------------------------


class _Malformed:
    """A response object whose decoding blows up after the call was billed."""

    @property
    def choices(self) -> Any:
        raise AttributeError("malformed SDK response")


def test_a_malformed_response_still_settles_its_reservation() -> None:
    """Decoding happens after the request was billed, so a body we cannot read
    must not leave the hold open -- that would drop the cost off the ceiling."""
    budget = RunBudget(1.0, 10.0, max_usd=None)
    provider, _ = _provider([_Malformed()], budget=budget)

    with pytest.raises(AttributeError):
        provider.generate(_request())

    budget.assert_all_settled()
    assert budget.snapshot()["attempts"] == 1


def test_an_interrupt_between_reserve_and_settle_does_not_leak_the_hold() -> None:
    """KeyboardInterrupt is a BaseException: catching only Exception would let
    the reservation escape unsettled."""
    budget = RunBudget(1.0, 10.0, max_usd=None)
    provider, _ = _provider([KeyboardInterrupt()], budget=budget)

    with pytest.raises(KeyboardInterrupt):
        provider.generate(_request())

    budget.assert_all_settled()
    # Conservative: an interrupt cannot prove the service did not run.
    assert budget.snapshot()["spent_usd"] > 0.0


def test_an_interrupt_is_not_retried() -> None:
    provider, client = _provider(
        [KeyboardInterrupt(), _Completion(usage=_Usage(1, 1))], max_retries=2
    )

    with pytest.raises(KeyboardInterrupt):
        provider.generate(_request())

    assert len(client.calls) == 1


class _FakeClock:
    """A clock that sleeping actually advances.

    Faking sleep without advancing the clock is how a wall-clock assertion
    passes for the wrong reason: nothing moves, so anything looks in budget.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _clocked_provider(
    outcomes: list[Any], **kwargs: Any
) -> tuple[OpenAICompatProvider, _FakeClient, _FakeClock]:
    clock = _FakeClock()
    client = _FakeClient(outcomes)
    provider = OpenAICompatProvider(
        api_key="unused-because-a-client-is-injected",
        base_url="https://example.invalid/v1",
        model="deepseek-v4-flash",
        client=client,
        sleep=clock.sleep,
        monotonic=clock,
        **kwargs,
    )
    return provider, client, clock


def test_retry_backoff_lands_in_the_run_wall_clock_ledger() -> None:
    """Time spent waiting to retry is time the run spent. Asserted on the
    ledger itself, not on the shape of the source."""
    budget = RunBudget(1.0, 10.0, max_usd=None, max_wall_clock_s=None)
    provider, client, _ = _clocked_provider(
        [_HTTPError(500), _Completion(usage=_Usage(1, 1))],
        budget=budget,
        max_retries=2,
    )

    provider.generate(_request())

    assert len(client.calls) == 2
    # Requests take no time on the fake clock, so the whole ledger is the one
    # 1s backoff between the two attempts.
    assert budget.snapshot()["wall_clock_s"] == pytest.approx(1.0)


def test_the_wall_clock_ceiling_accounts_for_the_backoff_before_retrying() -> None:
    """The retry's worst case is backoff + timeout. Reserving only the timeout
    would let a run start a call that overshoots the ceiling by exactly the time
    it spent waiting to make it: 120.5s left, a 120s timeout and a 1s backoff
    means the second attempt can run to 121s.
    """
    budget = RunBudget(1.0, 10.0, max_usd=None, max_wall_clock_s=120.5)
    provider, client, clock = _clocked_provider(
        [_HTTPError(500), _Completion(usage=_Usage(1, 1))],
        budget=budget,
        max_retries=2,
        timeout_s=120.0,
    )

    with pytest.raises(BudgetExhausted, match="wall clock"):
        provider.generate(_request())

    # The retry was refused before it went out, not after.
    assert len(client.calls) == 1
    # And refused before the backoff was even waited out: an unmoved clock rules
    # out a "sleep first, refuse afterwards" ordering that client.calls alone
    # cannot distinguish.
    assert clock.now == pytest.approx(0.0)
    budget.assert_all_settled()
