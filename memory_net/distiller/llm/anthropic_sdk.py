from __future__ import annotations

import os
from typing import Any

from memory_net.distiller.llm.base import LLMProvider, LLMRequest, LLMResponse

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-6"

PROVIDER_BACKEND = "anthropic_sdk"


class AnthropicSDKProvider(LLMProvider):
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.model = model
        # Injected only by tests, so the provenance contract can be exercised
        # without an API key or a network call. Production leaves it None and
        # the SDK client is built below.
        self.client = client

    def generate(self, request: LLMRequest) -> LLMResponse:
        client = self.client
        if client is None:
            try:
                import anthropic
            except ImportError as exc:
                raise RuntimeError(
                    "anthropic package is required for AnthropicSDKProvider"
                ) from exc
            if not self.api_key:
                raise RuntimeError(
                    "ANTHROPIC_API_KEY is required for AnthropicSDKProvider"
                )
            client = anthropic.Anthropic(api_key=self.api_key)
        model = self.model or request.model or DEFAULT_ANTHROPIC_MODEL
        message = client.messages.create(
            model=model,
            max_tokens=request.max_tokens,
            system=request.system_prompt,
            messages=[{"role": "user", "content": request.user_prompt}],
        )
        text = "".join(getattr(block, "text", "") for block in message.content)
        usage = getattr(message, "usage", None)
        # Only claim "reported" when the service actually handed back a usage
        # object. Without one the counts below are placeholder zeros, and
        # labelling those as service-reported would make the run-level aggregate
        # assert something the backend never said.
        usage_source = "reported" if usage is not None else "unknown"
        return LLMResponse(
            text=text,
            model=model,
            usage={
                "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
                "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
            },
            usage_source=usage_source,
            provider_backend=PROVIDER_BACKEND,
            raw_meta=_raw_meta(message),
        )


def _raw_meta(message: Any) -> dict[str, str]:
    """String scalars worth keeping from the response envelope, never the body."""
    meta: dict[str, str] = {}
    for key in ("stop_reason", "model"):
        value = getattr(message, key, None)
        if isinstance(value, str) and value:
            meta[f"response_{key}" if key == "model" else key] = value
    return meta
