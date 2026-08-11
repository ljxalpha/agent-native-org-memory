"""The production distillation backend: a direct OpenAI-compatible endpoint.

DeepSeek is the endpoint this targets (``DEEPSEEK_BASE_URL``), but nothing here
is DeepSeek-specific beyond the credential names. It is the production backend
because of what the channel can carry, not because of maturity (§5.3.3):

* ``system`` and ``user`` stay two separate messages. Leaf records are untrusted
  input; flattening them into one prompt would let a record rewrite the
  instruction it was supposed to be data for.
* ``response_format={"type": "json_object"}`` is a service-level constraint, so
  the prompt's "only output JSON" stops being the only defence.
* ``max_tokens`` is honoured, which is what makes the budget's upper bound real.
* ``usage`` comes back from the service, so the run can honestly claim
  ``usage_source="reported"`` rather than estimating its own spend.

Credentials are read from the process environment only. This module never opens
a ``.env`` file: that file holds far more than the two keys we are entitled to,
and narrowing it down is the launcher's job, not this provider's.
"""

from __future__ import annotations

import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from memory_net.distiller.llm.base import LLMProvider, LLMRequest, LLMResponse
from memory_net.distiller.llm.ledger import (
    Attempt,
    AttemptCeilingExceeded,
    AttemptLedger,
)
from memory_net.distiller.runctx import (
    BudgetExhausted,
    Reservation,
    RunBudget,
    prompt_tokens_upper,
)

PROVIDER_BACKEND = "deepseek"

DEFAULT_MODEL = "deepseek-v4-flash"

_REFUSED = object()
"""Sentinel for "the project ledger refused this attempt"; see _reserve_attempt."""
"""Model used when neither the profile nor the request names one.

The cheaper of the two available tiers, deliberately: an unconfigured run
should not silently pick the more expensive model. ``deepseek-v4-pro`` is the
other supported id, selected explicitly via ``llm.model`` or ``--model``.
"""
DEFAULT_TIMEOUT_S = 120.0
DEFAULT_MAX_RETRIES = 2
_BACKOFF_BASE_S = 1.0

API_KEY_ENV = "DEEPSEEK_API_KEY"
BASE_URL_ENV = "DEEPSEEK_BASE_URL"


class ProviderCallError(RuntimeError):
    """A transport/API failure, restated without the SDK's own message.

    The SDK's exception text is built from the request it just made, so it can
    carry the endpoint URL and, depending on the client, fragments of the
    authenticated request. That text does not stay in memory: a failed run has
    ``str(exc)`` written into its failure health and metrics files by the
    orchestrator, which are artifacts the CI publishes. So the message that
    escapes this provider names only the exception type and the HTTP status --
    enough to act on -- while the original stays reachable as ``__cause__`` for
    an interactive traceback that never reaches disk.
    """


@dataclass(frozen=True)
class _Outcome:
    """How one failed attempt should be retried and charged.

    ``billable`` is the conservative axis: it means "we cannot prove the service
    did not run this", so the reservation keeps its full hold. Getting this wrong
    in the optimistic direction is what lets a run overspend.
    """

    retryable: bool
    billable: bool


def classify_failure(exc: BaseException) -> _Outcome:
    """Map a transport/API failure onto retry and billing decisions.

    Duck-typed on ``status_code`` rather than on the SDK's exception classes, so
    the mapping is testable without the SDK installed and does not break when
    the SDK reorganises its exception hierarchy.
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        if status >= 500 or status == 408:
            # A 5xx does not prove the request never ran: it may have completed
            # and failed on the way back, in which case it was billed.
            return _Outcome(retryable=True, billable=True)
        if status == 429:
            # Deliberately conservative. A rate-limit body does not distinguish
            # "rejected at the gate" from "throttled mid-inference", so the only
            # safe reading is that it may already have been billed.
            return _Outcome(retryable=True, billable=True)
        if 400 <= status < 500:
            # Rejected before inference (bad key, bad arguments): provably
            # unbilled, and retrying an unchanged request changes nothing.
            return _Outcome(retryable=False, billable=False)
    name = type(exc).__name__.lower()
    if "timeout" in name or "connection" in name:
        # The request may well have been received and executed.
        return _Outcome(retryable=True, billable=True)
    return _Outcome(retryable=False, billable=True)


class OpenAICompatProvider(LLMProvider):
    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        budget: RunBudget | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
        client: Any | None = None,
        ledger: AttemptLedger | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.api_key = api_key or os.environ.get(API_KEY_ENV)
        self.base_url = base_url or os.environ.get(BASE_URL_ENV)
        self.model = model
        # Run-level and shared with the other lane: see §5.5.2. None means no
        # ceiling is enforced, which is the pre-injection behaviour.
        self.budget = budget
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        # Injected by tests so the whole contract is exercised without a key or
        # a network call. Production leaves it None and the SDK client is built.
        self.client = client
        # The cross-process attempt ledger (§5.5.14). None means no project
        # ceiling is enforced, which is what a test double wants; cli.py always
        # attaches one, so every production call is counted.
        self.ledger = ledger
        self.sleep = sleep
        # Injected together: a test that fakes sleeping must be able to make
        # the clock advance with it, or the wall-clock ledger it asserts on
        # never moves and the assertion passes for the wrong reason.
        self.monotonic = monotonic

    def generate(self, request: LLMRequest) -> LLMResponse:
        try:
            client = self._client()
        except ProviderCallError:
            # Our own configuration errors (missing key, missing base url,
            # missing extra) are written by us and safe to surface verbatim.
            raise
        except Exception as exc:
            # Building the SDK client already touches the endpoint and the
            # credentials, so a failure here can carry both. It escapes before
            # the retry loop, so the loop's sanitizing is no help.
            raise _sanitized(exc, attempts=0) from exc
        model = self.model or request.model or DEFAULT_MODEL
        last_exc: BaseException | None = None

        for attempt in range(self.max_retries + 1):
            backoff_s = _BACKOFF_BASE_S * (2 ** (attempt - 1)) if attempt else 0.0
            started = self.monotonic()
            # Reserved *before* the backoff, and for the backoff plus the request
            # timeout. Reserving only the timeout would let the run start a call
            # whose worst case overshoots the wall-clock ceiling by exactly the
            # time spent waiting to make it.
            reservation = self._reserve(
                request, wall_upper_s=backoff_s + self.timeout_s
            )
            # The project ledger, after the run budget and before the request:
            # one entry per attempt, which is exactly what this loop multiplies
            # and what the endpoint bills (§5.5.14).
            try:
                ledger_attempt = self._reserve_attempt(request, model=model)
            except BaseException:
                # A ledger lock timeout, an IO error or an interrupt here would
                # otherwise strand the run-budget hold taken one line above --
                # and the orchestrator skips assert_all_settled() while an
                # exception is in flight, so nothing would ever report it.
                if reservation is not None:
                    reservation.release(wall_s=0.0)
                raise
            if ledger_attempt is _REFUSED:
                if reservation is not None:
                    # Provably not executed: the request never went out.
                    reservation.release(wall_s=0.0)
                raise BudgetExhausted(
                    "project attempt ceiling reached; no request was made"
                )
            try:
                if backoff_s:
                    self.sleep(backoff_s)
                completion = self._call(client, request=request, model=model)
            except BaseException as exc:
                # BaseException, not Exception: a KeyboardInterrupt between the
                # reserve and the settle would otherwise leak the hold, and an
                # unsettled hold is an attempt whose cost silently missed the
                # ceiling.
                outcome = (
                    classify_failure(exc)
                    if isinstance(exc, Exception)
                    else _Outcome(retryable=False, billable=True)
                )
                self._close(reservation, billable=outcome.billable, started=started)
                self._settle_attempt(ledger_attempt, outcome="failed")
                last_exc = exc
                if not outcome.retryable or attempt == self.max_retries:
                    # Only ``Exception`` is restated. Wrapping a
                    # ``KeyboardInterrupt`` in a ``RuntimeError`` would make an
                    # interrupt look like a provider failure and let the run's
                    # error handling swallow it.
                    if isinstance(exc, Exception):
                        raise _sanitized(exc, attempts=attempt + 1) from exc
                    raise
                continue

            # From here the request has been billed, so the reservation must
            # reach a terminal state even if decoding the response blows up on
            # a malformed body.
            try:
                self._settle_attempt(ledger_attempt, outcome="ok")
            except BaseException:
                # Same reasoning as the reservation above, on the way out: a
                # ledger write that fails here would otherwise strand the
                # run-budget hold, and nothing downstream would say so.
                self._close(reservation, billable=True, started=started)
                raise
            try:
                return self._to_response(
                    completion,
                    request=request,
                    model=model,
                    reservation=reservation,
                    started=started,
                )
            except BaseException as exc:
                self._close(reservation, billable=True, started=started)
                # Decoding reads the SDK's own response object, so an
                # AttributeError or TypeError from it can quote that object.
                # ProviderCallError instances are ours (the truncation error)
                # and already safe.
                if isinstance(exc, Exception) and not isinstance(
                    exc, ProviderCallError
                ):
                    raise _sanitized(exc, attempts=attempt + 1) from exc
                raise

        # Unreachable: the loop either returns or re-raises on its last attempt.
        raise RuntimeError(f"{PROVIDER_BACKEND} call failed") from last_exc

    def _call(self, client: Any, *, request: LLMRequest, model: str) -> Any:
        return client.chat.completions.create(
            model=model,
            max_tokens=request.max_tokens,
            # Two messages, never one: the role boundary is the point.
            messages=[
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
            response_format={"type": "json_object"},
            timeout=self.timeout_s,
        )

    # -- helpers ---------------------------------------------------------

    def _client(self) -> Any:
        if self.client is not None:
            return self.client
        # Credentials are checked before the import: a missing key is a
        # configuration error and should say so, rather than being masked by
        # "the optional extra is not installed".
        if not self.api_key:
            raise ProviderCallError(
                f"{API_KEY_ENV} is required for OpenAICompatProvider"
            )
        if not self.base_url:
            raise ProviderCallError(
                f"{BASE_URL_ENV} is required for OpenAICompatProvider"
            )
        try:
            import openai
        except ImportError as exc:
            raise ProviderCallError(
                "openai package is required for OpenAICompatProvider; "
                "install the 'llm' extra"
            ) from exc
        # `max_retries=0` is load-bearing, not tuning. The SDK's own retry loop
        # is invisible to everything above it: this class reserves one ledger
        # attempt and one budget hold per iteration of *its* loop, so an SDK
        # that quietly retried twice more would bill three wire attempts
        # against one reservation -- and the project ceiling counts
        # reservations. Retries belong to the loop that can account for them.
        return openai.OpenAI(
            api_key=self.api_key, base_url=self.base_url, max_retries=0
        )

    def _reserve_attempt(self, request: LLMRequest, *, model: str) -> Any:
        """Take one attempt from the project ledger, or report the refusal.

        Returns ``_REFUSED`` rather than raising so the caller can release the
        run-budget hold it already took before turning this into the same
        ``BudgetExhausted`` every other pre-call refusal produces.
        """
        if self.ledger is None:
            return None
        try:
            return self.ledger.reserve(
                kind="distill_live",
                run_id=str((request.metadata or {}).get("run_id") or "unknown"),
                provider="deepseek",
                model=model,
            )
        except AttemptCeilingExceeded:
            return _REFUSED

    def _settle_attempt(self, attempt: Any, *, outcome: str) -> None:
        if isinstance(attempt, Attempt):
            self.ledger.settle(attempt, outcome=outcome)  # type: ignore[union-attr]

    def _reserve(
        self, request: LLMRequest, *, wall_upper_s: float
    ) -> Reservation | None:
        """Hold budget for one attempt, or let ``BudgetExhausted`` stop it.

        Only token upper bounds are supplied: pricing lives in the budget, so
        this provider never needs to know what a token costs.
        """
        if self.budget is None:
            return None
        return self.budget.reserve(
            input_tokens_upper=prompt_tokens_upper(
                request.system_prompt, request.user_prompt
            ),
            output_tokens_upper=request.max_tokens,
            timeout_s_upper=wall_upper_s,
        )

    def _close(
        self,
        reservation: Reservation | None,
        *,
        billable: bool,
        started: float,
        usage: dict[str, int] | None = None,
    ) -> None:
        if reservation is None or reservation.is_settled:
            # Idempotent: the guard around response decoding closes the
            # reservation only if _to_response did not already get there.
            return
        wall_s = self.monotonic() - started
        if usage is not None:
            reservation.settle(usage=usage, wall_s=wall_s)
        elif billable:
            reservation.settle_unknown(wall_s=wall_s)
        else:
            reservation.release(wall_s=wall_s)

    def _to_response(
        self,
        completion: Any,
        *,
        request: LLMRequest,
        model: str,
        reservation: Reservation | None,
        started: float,
    ) -> LLMResponse:
        choice = completion.choices[0] if completion.choices else None
        text = str(getattr(getattr(choice, "message", None), "content", "") or "")
        finish_reason = str(getattr(choice, "finish_reason", "") or "")
        usage, usage_source = _usage_of(completion, text)

        # Settle before raising: the tokens were generated and billed even
        # though the output is unusable.
        #
        # Only a *reported* usage may settle at its real cost, because settling
        # refunds the difference against the reserved upper bound. An estimate
        # derived from the response text knows nothing about the input tokens
        # the service billed, so refunding against it would release budget for
        # spend that did happen. Without a report the hold is kept in full.
        self._close(
            reservation,
            billable=True,
            started=started,
            usage=usage if usage_source == "reported" else None,
        )

        if finish_reason == "length":
            raise ProviderCallError(
                f"{PROVIDER_BACKEND} response was truncated at max_tokens="
                f"{request.max_tokens}; the JSON body is incomplete"
            )

        raw_meta = {}
        response_model = str(getattr(completion, "model", "") or "")
        if response_model:
            raw_meta["response_model"] = response_model
        if finish_reason:
            raw_meta["finish_reason"] = finish_reason

        return LLMResponse(
            text=text,
            model=model,
            usage=usage,
            usage_source=usage_source,
            provider_backend=PROVIDER_BACKEND,
            raw_meta=raw_meta,
        )


def _sanitized(exc: Exception, *, attempts: int) -> ProviderCallError:
    """Restate a transport failure using only its type and HTTP status."""
    status = getattr(exc, "status_code", None)
    detail = f" status={status}" if isinstance(status, int) else ""
    return ProviderCallError(
        f"{PROVIDER_BACKEND} call failed after {attempts} attempt(s): "
        f"{type(exc).__name__}{detail}"
    )


def _usage_of(completion: Any, text: str) -> tuple[dict[str, int], str]:
    """Service-reported counts when present, a local estimate otherwise.

    The estimate is never labelled ``reported``: doing so would make the
    run-level aggregate assert something the backend never said.

    "Present" means *both* counters are real integers. A ``usage`` object that
    exists but carries ``None`` or a missing counter is not a report; treating
    it as one would coerce the gap to zero and hand the run a
    ``usage_source="reported"`` with no tokens behind it -- a claim that reads
    as "the service billed us nothing" and both understates the spend and makes
    the A1 acceptance criterion pass vacuously.
    """
    usage = getattr(completion, "usage", None)
    input_tokens = _reported_count(usage, "prompt_tokens")
    output_tokens = _reported_count(usage, "completion_tokens")
    if input_tokens is None or output_tokens is None:
        return {"input_tokens": 0, "output_tokens": math.ceil(len(text) / 4)}, (
            "estimated"
        )
    counts = {"input_tokens": input_tokens, "output_tokens": output_tokens}
    counts.update(_cache_counts(usage))
    return counts, "reported"


def _cache_counts(usage: Any) -> dict[str, int]:
    """Prompt-cache counters, present only when the service reported them.

    The names are provider-neutral on purpose: the metric is "how much of this
    prompt was served from a cache", not "what this vendor calls it". A backend
    that says nothing leaves the keys *absent* rather than zero, so the run can
    later distinguish "no hits" from "not reported" and never has to estimate
    one from the other.
    """
    details = getattr(usage, "prompt_tokens_details", None)
    counts: dict[str, int] = {}
    read = _reported_count(details, "cached_tokens")
    if read is None:
        read = _reported_count(usage, "cache_read_input_tokens")
    if read is not None:
        counts["cache_read_input_tokens"] = read
    created = _reported_count(details, "cache_creation_tokens")
    if created is None:
        created = _reported_count(usage, "cache_creation_input_tokens")
    if created is not None:
        counts["cache_creation_input_tokens"] = created
    return counts


def _reported_count(usage: Any, name: str) -> int | None:
    """One service-reported counter, or ``None`` when it was not reported.

    ``bool`` is rejected explicitly: it is an ``int`` subclass, so a stray
    ``True`` would otherwise pass for a token count of one. Negatives are
    rejected because settling charges what it is given, so a negative count
    would reduce the run's recorded spend and hand back budget already used.
    """
    if usage is None:
        return None
    value = getattr(usage, name, None)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value
