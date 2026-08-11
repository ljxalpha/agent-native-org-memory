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
from memory_net.distiller.runctx import (
    Reservation,
    RunBudget,
    prompt_tokens_upper,
)

PROVIDER_BACKEND = "deepseek"

DEFAULT_MODEL = "deepseek-v4-flash"
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
        sleep: Callable[[float], None] = time.sleep,
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
        self.sleep = sleep

    def generate(self, request: LLMRequest) -> LLMResponse:
        client = self._client()
        model = self.model or request.model or DEFAULT_MODEL
        last_exc: BaseException | None = None

        for attempt in range(self.max_retries + 1):
            # The backoff is inside the attempt's own timing window, so the
            # run's wall-clock ledger accounts for time spent waiting to retry
            # rather than only for time spent inside the request.
            started = time.monotonic()
            if attempt:
                self.sleep(_BACKOFF_BASE_S * (2 ** (attempt - 1)))
            # Reserved before every attempt, including retries: a retry is a
            # fresh call with a fresh cost, and a ceiling that only counted the
            # first one would not be a ceiling.
            reservation = self._reserve(request)
            try:
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
                last_exc = exc
                if not outcome.retryable or attempt == self.max_retries:
                    raise
                continue

            # From here the request has been billed, so the reservation must
            # reach a terminal state even if decoding the response blows up on
            # a malformed body.
            try:
                return self._to_response(
                    completion,
                    request=request,
                    model=model,
                    reservation=reservation,
                    started=started,
                )
            except BaseException:
                self._close(reservation, billable=True, started=started)
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
            raise RuntimeError(f"{API_KEY_ENV} is required for OpenAICompatProvider")
        if not self.base_url:
            raise RuntimeError(f"{BASE_URL_ENV} is required for OpenAICompatProvider")
        try:
            import openai
        except ImportError as exc:
            raise RuntimeError(
                "openai package is required for OpenAICompatProvider; "
                "install the 'llm' extra"
            ) from exc
        return openai.OpenAI(api_key=self.api_key, base_url=self.base_url)

    def _reserve(self, request: LLMRequest) -> Reservation | None:
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
            timeout_s_upper=self.timeout_s,
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
        wall_s = time.monotonic() - started
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
        self._close(reservation, billable=True, started=started, usage=usage)

        if finish_reason == "length":
            raise RuntimeError(
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


def _usage_of(completion: Any, text: str) -> tuple[dict[str, int], str]:
    """Service-reported counts when present, a local estimate otherwise.

    The estimate is never labelled ``reported``: doing so would make the
    run-level aggregate assert something the backend never said.
    """
    usage = getattr(completion, "usage", None)
    if usage is None:
        return {"input_tokens": 0, "output_tokens": math.ceil(len(text) / 4)}, (
            "estimated"
        )
    return {
        "input_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
    }, "reported"
