from pathlib import Path

from memory_net.distiller.llm.base import LLMProvider, LLMRequest, LLMResponse
from memory_net.distiller.pipeline import DistillationPipeline


class ConflictLLMProvider(LLMProvider):
    def generate(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            text=(
                '{"products":[{"title":"Deploy","category":"best_practices",'
                '"summary":"Deployment advice has conflicting positions.",'
                '"evidence":"Claude and Hermes disagree on deploy timing.",'
                '"citations":[{"memory_id":"leaf-claude-mem-1","revision_hash":"aaa111"},'
                '{"memory_id":"leaf-hermes-mem-1","revision_hash":"bbb222"}],'
                '"conflicts":[{"description":"Deploy timing differs",'
                '"positions":[{"claim":"Deploy during business hours",'
                '"contributors":["claude"]},{"claim":"Deploy off hours",'
                '"contributors":["hermes"]}]}]}]}'
            ),
            model=request.model,
        )


def test_hermes_and_claude_conflict_e2e(tmp_path: Path) -> None:
    system = tmp_path / "system.md"
    user = tmp_path / "user.md"
    system.write_text("system", encoding="utf-8")
    user.write_text("records {{ records_json }}", encoding="utf-8")
    pipeline = DistillationPipeline(
        provider=ConflictLLMProvider(),
        system_prompt_path=system,
        user_prompt_path=user,
    )

    output = pipeline.run(
        [
            _record(
                "leaf-claude-mem-1", "aaa111", "claude", "Deploy during business hours."
            ),
            _record("leaf-hermes-mem-1", "bbb222", "hermes", "Deploy off hours."),
        ],
        run_id="run",
    )

    assert len(output.products) == 1
    product = output.products[0]
    assert product.frontmatter["conflicts"]
    assert "Deploy during business hours" in product.content
    assert "Deploy off hours" in product.content


def _record(
    memory_id: str, revision_hash: str, author: str, body: str
) -> dict[str, str]:
    return {
        "memory_id": memory_id,
        "revision_hash": revision_hash,
        "author_node": author,
        "source_branch_sha": revision_hash,
        "relative_path": f"memories/deploy/{memory_id}.md",
        "category_hint": "best_practices",
        "content": body,
    }
