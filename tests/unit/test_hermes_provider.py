import shlex
import sys
from pathlib import Path

import pytest

from memory_net.distiller.llm.base import LLMRequest
from memory_net.distiller.llm.hermes_tool import HermesToolProvider
from memory_net.distiller.llm.mock import MockLLMProvider
from memory_net.distiller.pipeline import DistillationPipeline


def test_hermes_tool_provider_requires_command() -> None:
    with pytest.raises(RuntimeError, match="HERMES_TOOL_COMMAND"):
        HermesToolProvider(command=None).generate(LLMRequest("s", "u", "m"))


def test_hermes_tool_provider_accepts_quoted_command_args() -> None:
    script = (
        "import json, sys; "
        "request=json.load(sys.stdin); "
        "print(json.dumps({'text': request['user_prompt'], 'model': request['model']}))"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"

    response = HermesToolProvider(command=command).generate(
        LLMRequest("system", "user", "model")
    )

    assert response.text == "user"
    assert response.model == "model"


def test_mock_and_hermes_fixture_pipeline_are_equivalent(tmp_path: Path) -> None:
    system = tmp_path / "system.md"
    user = tmp_path / "user.md"
    system.write_text("system", encoding="utf-8")
    user.write_text("records {{ records_json }}", encoding="utf-8")
    records = [_record()]
    mock_output = DistillationPipeline(
        provider=MockLLMProvider(),
        system_prompt_path=system,
        user_prompt_path=user,
    ).run(records, run_id="run")
    hermes_output = DistillationPipeline(
        provider=FixedProvider(mock_output.response.text),
        system_prompt_path=system,
        user_prompt_path=user,
    ).run(records, run_id="run")

    assert [p.frontmatter["topic_id"] for p in mock_output.products] == [
        p.frontmatter["topic_id"] for p in hermes_output.products
    ]


class FixedProvider(MockLLMProvider):
    def __init__(self, text: str) -> None:
        self.text = text

    def generate(self, request: LLMRequest):  # type: ignore[no-untyped-def]
        from memory_net.distiller.llm.base import LLMResponse

        return LLMResponse(text=self.text, model=request.model)


def _record() -> dict[str, str]:
    return {
        "memory_id": "leaf-a-mem-1",
        "revision_hash": "abc12345",
        "author_node": "alice",
        "source_branch_sha": "deadbeef",
        "relative_path": "memories/auth/retry-abc12345.md",
        "category_hint": "auth",
        "content": "---\ntitle: Retry\n---\nUse backoff.",
    }
