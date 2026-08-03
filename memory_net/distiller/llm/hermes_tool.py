from __future__ import annotations

import json
import os
import shlex
import subprocess

from memory_net.distiller.llm.base import LLMProvider, LLMRequest, LLMResponse


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
        usage = payload.get("usage") if isinstance(payload, dict) else None
        return LLMResponse(
            text=str(payload.get("text") if isinstance(payload, dict) else proc.stdout),
            model=(
                str(payload.get("model") or request.model)
                if isinstance(payload, dict)
                else request.model
            ),
            usage=(
                {str(k): int(v) for k, v in (usage or {}).items()}
                if isinstance(usage, dict)
                else {}
            ),
        )
