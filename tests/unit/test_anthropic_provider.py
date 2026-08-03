"""AnthropicSDKProvider declares its own provenance (§5.3.1).

A production call that leaves the defaults in place is aggregated as "unknown
source, unknown backend", which makes the whole provenance chain true only on
the mock path. These tests drive the provider through an injected fake client,
so no API key and no network call are involved.
"""

from __future__ import annotations

from typing import Any

import pytest

from memory_net.distiller.llm.anthropic_sdk import AnthropicSDKProvider
from memory_net.distiller.llm.base import LLMRequest


class _Block:
    def __init__(self, text: str) -> None:
        self.text = text


class _Usage:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _Message:
    def __init__(self, *, usage: _Usage | None, **extra: Any) -> None:
        self.content = [_Block('{"products": []}')]
        self.usage = usage
        for key, value in extra.items():
            setattr(self, key, value)


class _Messages:
    def __init__(self, message: _Message) -> None:
        self._message = message
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _Message:
        self.calls.append(kwargs)
        return self._message


class _Client:
    def __init__(self, message: _Message) -> None:
        self.messages = _Messages(message)


def _request() -> LLMRequest:
    return LLMRequest(system_prompt="s", user_prompt="u", model="claude-x")


def test_reported_usage_is_labelled_reported_with_the_sdk_backend() -> None:
    client = _Client(
        _Message(
            usage=_Usage(11, 22),
            stop_reason="end_turn",
            model="claude-x-20260101",
        )
    )

    response = AnthropicSDKProvider(client=client).generate(_request())

    assert response.provider_backend == "anthropic_sdk"
    assert response.usage_source == "reported"
    assert response.usage == {"input_tokens": 11, "output_tokens": 22}
    assert response.raw_meta == {
        "stop_reason": "end_turn",
        "response_model": "claude-x-20260101",
    }


def test_missing_usage_is_unknown_rather_than_a_reported_zero() -> None:
    """Placeholder zeros must not be passed off as service-reported counts."""
    response = AnthropicSDKProvider(client=_Client(_Message(usage=None))).generate(
        _request()
    )

    assert response.usage_source == "unknown"
    assert response.provider_backend == "anthropic_sdk"
    assert response.usage == {"input_tokens": 0, "output_tokens": 0}


def test_raw_meta_drops_non_string_envelope_fields() -> None:
    """raw_meta is a string-scalar map; anything else must not reach disk."""
    client = _Client(_Message(usage=_Usage(1, 1), stop_reason=None, model=17))

    response = AnthropicSDKProvider(client=client).generate(_request())

    assert response.raw_meta == {}


def test_an_injected_client_needs_no_api_key(monkeypatch: Any) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = _Client(_Message(usage=_Usage(1, 2)))

    provider = AnthropicSDKProvider(client=client)

    assert provider.generate(_request()).usage_source == "reported"
    assert client.messages.calls[0]["model"] == "claude-x"


def test_without_a_client_a_missing_api_key_still_fails_loudly(
    monkeypatch: Any,
) -> None:
    """Regression: injecting a client must not weaken the production guard."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    pytest.importorskip("anthropic")

    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        AnthropicSDKProvider().generate(_request())
