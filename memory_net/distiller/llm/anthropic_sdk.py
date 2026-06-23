from __future__ import annotations

import os

from memory_net.distiller.llm.base import LLMProvider, LLMRequest, LLMResponse

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-6"


class AnthropicSDKProvider(LLMProvider):
    def __init__(self, *, api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.model = model

    def generate(self, request: LLMRequest) -> LLMResponse:
        try:
            import anthropic
        except ImportError as exc:
            raise RuntimeError(
                "anthropic package is required for AnthropicSDKProvider"
            ) from exc
        if not self.api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is required for AnthropicSDKProvider")

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
        return LLMResponse(
            text=text,
            model=model,
            usage={
                "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
                "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
            },
        )
