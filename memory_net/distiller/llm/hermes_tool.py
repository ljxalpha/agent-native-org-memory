from __future__ import annotations

import json
import os
import shlex
import subprocess

from memory_net.distiller.llm.base import (
    USAGE_SOURCES,
    LLMProvider,
    LLMRequest,
    LLMResponse,
)

DEFAULT_PROVIDER_BACKEND = "hermes_tool"


class HermesToolProvider(LLMProvider):
    def __init__(self, *, command: str | None = None) -> None:
        self.command = command or os.environ.get("HERMES_TOOL_COMMAND")

    def generate(self, request: LLMRequest) -> LLMResponse:
        if not self.command:
            raise RuntimeError("HERMES_TOOL_COMMAND is required for HermesToolProvider")
        proc = subprocess.run(
            shlex.split(self.command),
            input=json.dumps(
                {
                    "system_prompt": request.system_prompt,
                    "user_prompt": request.user_prompt,
                    "model": request.model,
                    "max_tokens": request.max_tokens,
                    "metadata": request.metadata,
                },
                ensure_ascii=False,
            ),
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"hermes tool failed: {proc.stderr.strip()}")
        payload = json.loads(proc.stdout)
        mapping = payload if isinstance(payload, dict) else {}
        usage = mapping.get("usage")
        return LLMResponse(
            text=str(mapping.get("text") if isinstance(payload, dict) else proc.stdout),
            model=(
                str(mapping.get("model") or request.model)
                if isinstance(payload, dict)
                else request.model
            ),
            usage=(
                {str(k): int(v) for k, v in usage.items()}
                if isinstance(usage, dict)
                else {}
            ),
            usage_source=_usage_source(mapping.get("usage_source")),
            provider_backend=_provider_backend(mapping.get("provider_backend")),
            raw_meta=_raw_meta(mapping.get("raw_meta")),
        )


def _usage_source(value: object) -> str:
    """Honour only a declared, in-domain label.

    The bridge is a relay: it cannot assert on the downstream backend's behalf
    that the counts came from a service, so an absent or unrecognised label
    degrades to ``unknown`` rather than optimistically to ``reported``.
    """
    return value if isinstance(value, str) and value in USAGE_SOURCES else "unknown"


def _provider_backend(value: object) -> str:
    """The bridge may name the backend it actually relayed to; else it is us."""
    if isinstance(value, str) and value:
        return value
    return DEFAULT_PROVIDER_BACKEND


def _raw_meta(value: object) -> dict[str, str]:
    """String scalars only — anything else is dropped, never coerced."""
    if not isinstance(value, dict):
        return {}
    return {
        key: item
        for key, item in value.items()
        if isinstance(key, str) and isinstance(item, str)
    }
