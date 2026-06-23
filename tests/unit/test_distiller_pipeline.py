from pathlib import Path
from typing import Any

import pytest

from memory_net.distiller.llm.base import LLMProvider, LLMRequest, LLMResponse
from memory_net.distiller.llm.mock import MockLLMProvider
from memory_net.distiller.llm.recording import (
    BundleRecorder,
    RecordingProvider,
    ReplayProvider,
)
from memory_net.distiller.pipeline import DistillationPipeline, render_products


def test_distiller_pipeline_renders_product(tmp_path: Path) -> None:
    system = tmp_path / "system.md"
    user = tmp_path / "user.md"
    system.write_text("system", encoding="utf-8")
    user.write_text("records {{ records_json }}", encoding="utf-8")
    pipeline = DistillationPipeline(
        provider=MockLLMProvider(),
        system_prompt_path=system,
        user_prompt_path=user,
    )

    output = pipeline.run(
        [
            {
                "memory_id": "leaf-a-mem-1",
                "revision_hash": "abc12345",
                "author_node": "alice",
                "source_branch_sha": "deadbeef",
                "relative_path": "memories/auth/retry-abc12345.md",
                "category_hint": "auth",
                "content": "---\ntitle: Retry\n---\nUse backoff.",
                "source_frontmatter": {"title": "Retry"},
            }
        ],
        run_id="run",
    )

    assert len(output.products) == 1
    assert output.products[0].relative_path.as_posix().startswith("team/auth/")
    assert "leaf-a-mem-1@abc12345" in output.products[0].content
    assert output.products[0].frontmatter["citations"][0]["revision_hash"] == "abc12345"


def test_bundled_prompts_describe_accepted_product_schema() -> None:
    text = "\n".join(
        [
            Path("prompts/distill_system.md").read_text(encoding="utf-8"),
            Path("prompts/distill_user.md").read_text(encoding="utf-8"),
        ]
    )

    assert "`products`" in text
    assert "`items`" not in text
    assert "content_md" not in text
    assert "{{ categories_json }}" in text
    assert "{{ category_evidence_rules_json }}" in text
    assert "{{ response_schema_json }}" in text


def test_distiller_pipeline_skips_product_without_evidence(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path, EmptyEvidenceProvider())

    output = pipeline.run([_record()], run_id="run")

    assert output.products == []
    assert output.skipped_clusters == 1


def test_distiller_pipeline_zero_budget_skips_before_llm_call(tmp_path: Path) -> None:
    provider = CountingProvider()
    pipeline = _pipeline(tmp_path, provider, cost_budget_usd=0.0)

    output = pipeline.run([_record()], run_id="run")

    assert provider.calls == 0
    assert output.products == []
    assert output.skipped_clusters == 1


def test_distiller_pipeline_records_estimated_cost(tmp_path: Path) -> None:
    pipeline = _pipeline(
        tmp_path,
        CostedProvider(),
        input_cost_per_mtok_usd=1.0,
        output_cost_per_mtok_usd=2.0,
    )

    output = pipeline.run([_record()], run_id="run")

    assert output.estimated_cost_usd == pytest.approx(0.0005)
    assert output.response.usage["estimated_cost_microusd"] == 500


def test_distiller_pipeline_records_primary_and_fallback_parse_errors(
    tmp_path: Path,
) -> None:
    pipeline = _pipeline(
        tmp_path,
        InvalidJsonProvider("not json"),
        fallback_provider=InvalidJsonProvider('{"products": "nope"}'),
    )

    output = pipeline.run([_record()], run_id="run")

    assert output.products == []
    assert output.fallback_attempts == 1
    assert output.parse_failures == 1
    assert output.parse_error_details is not None
    assert "primary=" in output.parse_error_details[0]
    assert "fallback=" in output.parse_error_details[0]


def test_distiller_pipeline_renders_conflicts_in_frontmatter_and_body(
    tmp_path: Path,
) -> None:
    pipeline = _pipeline(tmp_path, ConflictProvider())

    output = pipeline.run([_record()], run_id="run")

    product = output.products[0]
    assert product.frontmatter["conflicts"]
    assert "⚠️ 存在分歧" in product.content
    assert "Use retries" in product.content


def test_render_products_does_not_leak_seen_topics_on_failed_batch() -> None:
    seen_topics: set[tuple[str, str]] = set()
    invalid_batch = {
        "products": [
            _raw_product("Retry"),
            _raw_product("Retry"),
        ]
    }

    with pytest.raises(ValueError, match="duplicate active topic"):
        render_products(
            invalid_batch,
            [_record()],
            run_id="run",
            seen_topics=seen_topics,
        )

    rendered = render_products(
        {"products": [_raw_product("Retry")]},
        [_record()],
        run_id="run",
        seen_topics=seen_topics,
    )

    assert len(rendered) == 1
    assert rendered[0].frontmatter["topic_id"] == "topic-v1-retry"


def test_distiller_pipeline_injects_dynamic_contract_into_prompt(
    tmp_path: Path,
) -> None:
    provider = CapturingProvider()
    pipeline = _pipeline(
        tmp_path,
        provider,
        categories=["auth", "risk"],
        min_citations_by_category={"auth": 2},
        min_contributors_by_category={"auth": 2},
    )

    pipeline.run([_record()], run_id="run")

    assert provider.request is not None
    assert '"auth"' in provider.request.user_prompt
    assert '"risk"' in provider.request.user_prompt
    assert '"min_citations": 2' in provider.request.user_prompt
    assert '"min_contributors": 2' in provider.request.user_prompt
    assert '"products"' in provider.request.user_prompt
    assert '"enum": [' in provider.request.user_prompt


def test_distiller_pipeline_appends_missing_contract_for_partial_template(
    tmp_path: Path,
) -> None:
    system = tmp_path / "system.md"
    user = tmp_path / "user.md"
    system.write_text("system", encoding="utf-8")
    user.write_text(
        "Categories {{ categories_json }}\nRecords {{ records_json }}",
        encoding="utf-8",
    )
    provider = CapturingProvider()
    pipeline = DistillationPipeline(
        provider=provider,
        system_prompt_path=system,
        user_prompt_path=user,
        categories=["auth"],
    )

    pipeline.run([_record()], run_id="run")

    assert provider.request is not None
    assert "Accepted response schema:" in provider.request.user_prompt
    assert '"enum": [' in provider.request.user_prompt


def test_mock_provider_reads_records_after_contract_lists(tmp_path: Path) -> None:
    system = tmp_path / "system.md"
    user = tmp_path / "user.md"
    system.write_text("system", encoding="utf-8")
    user.write_text(
        "Categories {{ categories_json }}\nRecords {{ records_json }}",
        encoding="utf-8",
    )
    pipeline = DistillationPipeline(
        provider=MockLLMProvider(),
        system_prompt_path=system,
        user_prompt_path=user,
        categories=["auth"],
    )

    output = pipeline.run([_record()], run_id="run")

    assert len(output.products) == 1
    assert output.products[0].frontmatter["citations"]


def test_distiller_pipeline_rejects_unknown_configured_category(
    tmp_path: Path,
) -> None:
    pipeline = _pipeline(
        tmp_path,
        UnknownCategoryProvider(),
        categories=["auth"],
    )

    output = pipeline.run([_record()], run_id="run")

    assert output.products == []
    assert output.citation_failures == 1
    assert output.skipped_clusters == 1


def test_mock_provider_maps_unknown_hints_to_configured_category(
    tmp_path: Path,
) -> None:
    pipeline = _pipeline(
        tmp_path,
        MockLLMProvider(),
        categories=["common_knowledge"],
    )

    output = pipeline.run([_record()], run_id="run")

    assert len(output.products) == 1
    assert output.products[0].category == "common_knowledge"
    assert (
        output.products[0].relative_path.as_posix().startswith("team/common_knowledge/")
    )


def test_render_products_requires_distinct_contributors_for_category() -> None:
    records = [
        _record(
            memory_id="leaf-a-mem-1",
            revision_hash="abc12345",
            author_node="alice",
        ),
        _record(
            memory_id="leaf-b-mem-1",
            revision_hash="def67890",
            author_node="alice",
        ),
    ]

    with pytest.raises(ValueError, match="contributors"):
        render_products(
            {"products": [_raw_product_with_citations()]},
            records,
            run_id="run",
            min_contributors_by_category={"auth": 2},
        )


def test_render_products_accepts_distinct_contributors_for_category() -> None:
    records = [
        _record(
            memory_id="leaf-a-mem-1",
            revision_hash="abc12345",
            author_node="alice",
        ),
        _record(
            memory_id="leaf-b-mem-1",
            revision_hash="def67890",
            author_node="bob",
        ),
    ]

    products = render_products(
        {"products": [_raw_product_with_citations()]},
        records,
        run_id="run",
        min_contributors_by_category={"auth": 2},
    )

    assert products[0].frontmatter["contributors"] == ["alice", "bob"]


def test_render_products_enforces_underscore_category_rules() -> None:
    records = [
        _record(
            memory_id="leaf-a-mem-1",
            revision_hash="abc12345",
            author_node="alice",
        ),
        _record(
            memory_id="leaf-b-mem-1",
            revision_hash="def67890",
            author_node="alice",
        ),
    ]
    product = _raw_product_with_citations()
    product["category"] = "common_knowledge"

    with pytest.raises(ValueError, match="contributors"):
        render_products(
            {"products": [product]},
            records,
            run_id="run",
            min_contributors_by_category={"common_knowledge": 2},
        )


def test_distiller_pipeline_counts_clusters_attempted_and_category_distribution(
    tmp_path: Path,
) -> None:
    pipeline = _pipeline(tmp_path, CostedProvider())

    output = pipeline.run([_record()], run_id="run")

    assert output.clusters_attempted == 1
    assert output.category_distribution == {"auth": 1}


def test_distiller_pipeline_zero_budget_attempts_no_clusters(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path, CountingProvider(), cost_budget_usd=0.0)

    output = pipeline.run([_record()], run_id="run")

    assert output.clusters_attempted == 0
    assert output.category_distribution == {}


def test_distiller_pipeline_counts_citation_resolution(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path, PartialCitationProvider())

    output = pipeline.run([_record()], run_id="run")

    # Two citations emitted; only the matching revision resolves. The unresolved
    # one also trips the render citation gate, so the cluster yields no product.
    assert output.citations_emitted == 2
    assert output.citations_resolved == 1
    assert output.products == []
    assert output.citation_failures == 1
    assert output.evidence_rule_rejections == 0


def test_distiller_pipeline_counts_accepted_fallback(tmp_path: Path) -> None:
    pipeline = _pipeline(
        tmp_path,
        InvalidJsonProvider("not json"),
        fallback_provider=CostedProvider(),
    )

    output = pipeline.run([_record()], run_id="run")

    assert output.fallback_attempts == 1
    assert output.fallback_accepted == 1
    assert len(output.products) == 1
    assert output.parse_failures == 0


def test_distiller_pipeline_counts_evidence_rule_rejections(tmp_path: Path) -> None:
    pipeline = _pipeline(
        tmp_path,
        CostedProvider(),
        min_citations_by_category={"auth": 2},
    )

    output = pipeline.run([_record()], run_id="run")

    assert output.products == []
    assert output.evidence_rule_rejections == 1
    assert output.citation_failures == 1


def test_distiller_pipeline_aggregates_replay_source_usage(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path, ReplayMarkerProvider())

    output = pipeline.run([_record()], run_id="run")

    assert output.replay_hits == 1
    assert output.replay_misses == 0


def test_pipeline_records_and_replays_fallback_through_shared_bundle(
    tmp_path: Path,
) -> None:
    # A fallback-triggering run records BOTH the primary (bad)
    # and fallback (good) calls into one bundle, in order; replaying through a
    # single shared ReplayProvider (primary is fallback) reproduces the run with
    # zero live calls.
    bundle = tmp_path / "bundle"
    recorder = BundleRecorder(bundle / "calls.jsonl", requests_dir=bundle / "requests")
    primary = RecordingProvider(InvalidJsonProvider("not json"), recorder=recorder)
    fallback = RecordingProvider(CostedProvider(), recorder=recorder)
    pipeline = _pipeline(tmp_path, primary, fallback_provider=fallback)

    output = pipeline.run([_record()], run_id="run")

    assert output.fallback_attempts == 1
    assert output.fallback_accepted == 1
    assert len(output.products) == 1
    calls = (bundle / "calls.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(calls) == 2
    request_files = sorted((bundle / "requests").glob("*.json"))
    assert [path.name.split("-", 1)[0] for path in request_files] == ["0001", "0002"]

    replay = ReplayProvider(bundle)
    replay_pipeline = _pipeline(tmp_path, replay, fallback_provider=replay)
    replayed = replay_pipeline.run([_record()], run_id="run")

    assert replayed.fallback_attempts == 1
    assert replayed.fallback_accepted == 1
    assert len(replayed.products) == 1
    # Both calls replayed from the bundle; nothing hit a live provider.
    assert replayed.replay_hits == 2
    assert replayed.replay_misses == 0


class ReplayMarkerProvider(LLMProvider):
    def generate(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            text=(
                '{"products":[{"title":"Retry","category":"auth",'
                '"summary":"Use backoff.","evidence":"Use backoff.",'
                '"citations":[{"memory_id":"leaf-a-mem-1",'
                '"revision_hash":"abc12345"}]}]}'
            ),
            model=request.model,
            usage={"input_tokens": 1, "output_tokens": 1, "replay_hits": 1},
        )


class PartialCitationProvider(LLMProvider):
    def generate(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            text=(
                '{"products":[{"title":"Retry","category":"auth",'
                '"summary":"Use backoff.","evidence":"Use backoff.",'
                '"citations":[{"memory_id":"leaf-a-mem-1",'
                '"revision_hash":"abc12345"},'
                '{"memory_id":"leaf-a-mem-1","revision_hash":"wrongrev"}]}]}'
            ),
            model=request.model,
            usage={"input_tokens": 1, "output_tokens": 1},
        )


class EmptyEvidenceProvider(LLMProvider):
    def generate(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            text=(
                '{"products":[{"title":"Retry","category":"auth",'
                '"summary":"Use backoff.","evidence":"",'
                '"citations":[{"memory_id":"leaf-a-mem-1",'
                '"revision_hash":"abc12345"}]}]}'
            ),
            model=request.model,
            usage={"input_tokens": 1, "output_tokens": 1},
        )


class CountingProvider(LLMProvider):
    def __init__(self) -> None:
        self.calls = 0

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        return MockLLMProvider().generate(request)


class CostedProvider(LLMProvider):
    def generate(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            text=(
                '{"products":[{"title":"Retry","category":"auth",'
                '"summary":"Use backoff.","evidence":"Use backoff.",'
                '"citations":[{"memory_id":"leaf-a-mem-1",'
                '"revision_hash":"abc12345"}]}]}'
            ),
            model=request.model,
            usage={"input_tokens": 100, "output_tokens": 200},
        )


class InvalidJsonProvider(LLMProvider):
    def __init__(self, text: str) -> None:
        self.text = text

    def generate(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(text=self.text, model=request.model)


class ConflictProvider(LLMProvider):
    def generate(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            text=(
                '{"products":[{"title":"Retry","category":"auth",'
                '"summary":"Conflict summary.","evidence":"Two positions.",'
                '"citations":[{"memory_id":"leaf-a-mem-1",'
                '"revision_hash":"abc12345"}],'
                '"conflicts":[{"description":"Retry policy differs",'
                '"positions":[{"claim":"Use retries",'
                '"contributors":["alice"]}]}]}]}'
            ),
            model=request.model,
        )


class CapturingProvider(LLMProvider):
    def __init__(self) -> None:
        self.request: LLMRequest | None = None

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.request = request
        return LLMResponse(
            text=(
                '{"products":[{"title":"Retry","category":"common_knowledge",'
                '"summary":"Use backoff.","evidence":"Use backoff.",'
                '"citations":[{"memory_id":"leaf-a-mem-1",'
                '"revision_hash":"abc12345"}]}]}'
            ),
            model=request.model,
        )


class UnknownCategoryProvider(LLMProvider):
    def generate(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            text=(
                '{"products":[{"title":"Retry","category":"unknown",'
                '"summary":"Use backoff.","evidence":"Use backoff.",'
                '"citations":[{"memory_id":"leaf-a-mem-1",'
                '"revision_hash":"abc12345"}]}]}'
            ),
            model=request.model,
        )


def _pipeline(
    tmp_path: Path,
    provider: LLMProvider,
    **kwargs: Any,
) -> DistillationPipeline:
    system = tmp_path / "system.md"
    user = tmp_path / "user.md"
    system.write_text("system", encoding="utf-8")
    user.write_text("records {{ records_json }}", encoding="utf-8")
    return DistillationPipeline(
        provider=provider,
        system_prompt_path=system,
        user_prompt_path=user,
        **kwargs,
    )


def _record(
    *,
    memory_id: str = "leaf-a-mem-1",
    revision_hash: str = "abc12345",
    author_node: str = "alice",
) -> dict[str, str]:
    return {
        "memory_id": memory_id,
        "revision_hash": revision_hash,
        "author_node": author_node,
        "source_branch_sha": "deadbeef",
        "relative_path": f"memories/auth/retry-{revision_hash}.md",
        "category_hint": "auth",
        "content": "---\ntitle: Retry\n---\nUse backoff.",
    }


def _raw_product(title: str) -> dict[str, Any]:
    return {
        "title": title,
        "category": "auth",
        "summary": "Use backoff.",
        "evidence": "Use backoff.",
        "citations": [{"memory_id": "leaf-a-mem-1", "revision_hash": "abc12345"}],
    }


def _raw_product_with_citations() -> dict[str, Any]:
    return {
        "title": "Retry",
        "category": "auth",
        "summary": "Use backoff.",
        "evidence": "Use backoff.",
        "citations": [
            {"memory_id": "leaf-a-mem-1", "revision_hash": "abc12345"},
            {"memory_id": "leaf-b-mem-1", "revision_hash": "def67890"},
        ],
    }
