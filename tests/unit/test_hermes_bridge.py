"""hermes-memory-distill bridge tests (DN-P3).

Proves the reference bridge script satisfies the HermesToolProvider JSON contract
and that the distillation pipeline driven via ``--provider hermes_tool`` (real
subprocess + JSON boundary) yields the same products as the in-process mock.
Zero LLM quota: the bridge backend is MockLLMProvider.
"""

import json
import shlex
import sys
from pathlib import Path

from memory_net.distiller.llm.base import LLMRequest
from memory_net.distiller.llm.hermes_tool import HermesToolProvider
from memory_net.distiller.llm.mock import MockLLMProvider
from memory_net.distiller.pipeline import DistillationPipeline

REPO_ROOT = Path(__file__).resolve().parents[2]
BRIDGE = REPO_ROOT / "scripts" / "hermes-memory-distill"


def _bridge_command() -> str:
    # Invoke via the current interpreter so the test does not depend on the
    # script's exec bit / shebang resolution.
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(BRIDGE))}"


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


def test_bridge_satisfies_hermes_tool_contract() -> None:
    """The bridge, run as a real subprocess, returns a contract-valid response
    whose ``text`` parses to the expected distillation products."""
    request = LLMRequest(
        system_prompt="system",
        user_prompt="records " + json.dumps([_record()]),
        model="mock-model",
    )

    response = HermesToolProvider(command=_bridge_command()).generate(request)

    assert response.model == "mock-model"
    assert response.usage.get("output_tokens", 0) > 0
    parsed = json.loads(response.text)
    assert [p["category"] for p in parsed["products"]] == ["auth"]
    assert parsed["products"][0]["citations"][0]["memory_id"] == "leaf-a-mem-1"


def test_bridge_reports_failure_as_nonzero_exit() -> None:
    """Invalid stdin must surface as a RuntimeError from the provider."""
    request = LLMRequest(system_prompt="s", user_prompt="no records here", model="m")
    try:
        HermesToolProvider(command=_bridge_command()).generate(request)
    except RuntimeError as exc:
        assert "hermes tool failed" in str(exc)
    else:  # pragma: no cover - explicit failure if no error raised
        raise AssertionError("expected RuntimeError on unparseable user_prompt")


def test_distiller_pipeline_with_hermes_bridge_matches_mock(tmp_path: Path) -> None:
    """The distillation pipeline driven by the real hermes_tool bridge subprocess
    produces the same products as the in-process mock provider."""
    system = tmp_path / "system.md"
    user = tmp_path / "user.md"
    system.write_text("system", encoding="utf-8")
    user.write_text("records {{ records_json }}", encoding="utf-8")
    records = [_record()]

    mock_out = DistillationPipeline(
        provider=MockLLMProvider(),
        system_prompt_path=system,
        user_prompt_path=user,
    ).run(records, run_id="run")
    bridge_out = DistillationPipeline(
        provider=HermesToolProvider(command=_bridge_command()),
        system_prompt_path=system,
        user_prompt_path=user,
    ).run(records, run_id="run")

    assert bridge_out.products, "bridge pipeline produced no products"
    assert [p.frontmatter["topic_id"] for p in bridge_out.products] == [
        p.frontmatter["topic_id"] for p in mock_out.products
    ]
