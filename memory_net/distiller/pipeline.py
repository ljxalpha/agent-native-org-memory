from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from memory_net.distiller.categories import canonical_category
from memory_net.distiller.clustering.dbscan import cluster_vectors
from memory_net.distiller.clustering.embedder import FastEmbedder
from memory_net.distiller.conflict import CONFLICT_MARKER, normalize_conflicts
from memory_net.distiller.llm.base import LLMProvider, LLMRequest, LLMResponse
from memory_net.distiller.provenance import RunProvenance, aggregate
from memory_net.distiller.runctx import (
    BudgetExhausted,
    RunBudget,
    RunContext,
    utc_now,
)
from memory_net.shared.models import TeamProduct
from memory_net.shared.util.frontmatter import render_frontmatter
from memory_net.shared.util.hashing import short_hash


class EvidenceRuleRejection(ValueError):
    """Raised when a product violates a §7.3 category evidence rule.

    A subtype of ``ValueError`` so the existing render error handling keeps
    counting it as a citation failure, while letting the pipeline attribute the
    specific ``min_citations`` / ``min_contributors`` threshold rejections to
    the ``evidence_rule_rejections`` tuning metric.
    """


@dataclass(frozen=True)
class DistillationOutput:
    products: list[TeamProduct]
    response: LLMResponse
    skipped_clusters: int = 0
    estimated_cost_usd: float = 0.0
    cluster_assignments: list[dict[str, Any]] | None = None
    fallback_attempts: int = 0
    parse_failures: int = 0
    citation_failures: int = 0
    parse_error_details: list[str] | None = None
    # Tuning metrics (METRICS-001): counted in the same single distill pass,
    # no extra LLM calls. Raw numerator/denominator are surfaced so the derived
    # rates in metrics.json never hide a 0/0.
    clusters_attempted: int = 0
    citations_emitted: int = 0
    citations_resolved: int = 0
    fallback_accepted: int = 0
    evidence_rule_rejections: int = 0
    category_distribution: dict[str, int] | None = None
    # Replay source counters (RECORD-001), summed from each call's usage. Zero on
    # a live/record run (no replay markers present).
    replay_hits: int = 0
    replay_misses: int = 0
    # Run-level fold of every call's provenance, primary and fallback lanes alike.
    provenance: RunProvenance = field(default_factory=RunProvenance.empty)


class DistillationPipeline:
    def __init__(
        self,
        *,
        provider: LLMProvider,
        system_prompt_path: Path,
        user_prompt_path: Path,
        fallback_provider: LLMProvider | None = None,
        embedder: FastEmbedder | None = None,
        model: str = "mock-memory-distiller",
        max_tokens: int = 2000,
        cost_budget_usd: float | None = None,
        input_cost_per_mtok_usd: float = 3.0,
        output_cost_per_mtok_usd: float = 15.0,
        dbscan_eps: float = 0.5,
        dbscan_min_samples: int = 2,
        categories: list[str] | None = None,
        min_citations_by_category: dict[str, int] | None = None,
        min_contributors_by_category: dict[str, int] | None = None,
        context: RunContext | None = None,
    ) -> None:
        self.provider = provider
        self.fallback_provider = fallback_provider
        self.embedder = embedder or FastEmbedder()
        self.system_prompt_path = system_prompt_path
        self.user_prompt_path = user_prompt_path
        self.model = model
        self.max_tokens = max_tokens
        # The single injection carrier (§5.5.3): every later run-level feature
        # arrives as a field on it, never as another constructor parameter.
        # None keeps the pre-injection behaviour field for field.
        self.context = context
        self.cost_budget_usd = cost_budget_usd
        self.input_cost_per_mtok_usd = input_cost_per_mtok_usd
        self.output_cost_per_mtok_usd = output_cost_per_mtok_usd
        self.dbscan_eps = dbscan_eps
        self.dbscan_min_samples = dbscan_min_samples
        self.min_citations_by_category = {
            canonical_category(category): value
            for category, value in (min_citations_by_category or {}).items()
        }
        self.min_contributors_by_category = {
            canonical_category(category): value
            for category, value in (min_contributors_by_category or {}).items()
        }
        self.category_evidence_rules = _category_evidence_rules(
            min_citations_by_category=self.min_citations_by_category,
            min_contributors_by_category=self.min_contributors_by_category,
        )
        self.categories = sorted(
            {
                canonical_category(category)
                for category in (categories or self.category_evidence_rules)
            }
        )

    def run(self, records: list[dict[str, Any]], *, run_id: str) -> DistillationOutput:
        run_id = self._checked_run_id(run_id)
        system_prompt = self.system_prompt_path.read_text(encoding="utf-8")
        template = self.user_prompt_path.read_text(encoding="utf-8")
        clusters = self._cluster_records(records)
        cluster_assignments = [
            {
                "cluster_id": index,
                "memory_ids": [str(record.get("memory_id")) for record in cluster],
                "size": len(cluster),
            }
            for index, cluster in enumerate(clusters)
        ]
        products: list[TeamProduct] = []
        skipped_clusters = 0
        fallback_attempts = 0
        parse_failures = 0
        citation_failures = 0
        input_tokens = 0
        output_tokens = 0
        raw_texts: list[str] = []
        parse_error_details: list[str] = []
        estimated_cost_usd = 0.0
        seen_topics: set[tuple[str, str]] = set()
        clusters_attempted = 0
        citations_emitted = 0
        citations_resolved = 0
        fallback_accepted = 0
        evidence_rule_rejections = 0
        replay_hits = 0
        replay_misses = 0
        # Every actual call, in invocation order, primary and fallback alike —
        # the fallback lane is precisely what makes a run's provenance "mixed".
        call_responses: list[LLMResponse] = []

        for cluster_records in clusters:
            if self._budget_exhausted(estimated_cost_usd):
                skipped_clusters += 1
                continue
            clusters_attempted += 1
            try:
                response = self._generate_cluster(
                    self.provider,
                    system_prompt=system_prompt,
                    template=template,
                    records=cluster_records,
                    run_id=run_id,
                )
            except BudgetExhausted:
                # The run budget refused before the call went out. Respect the
                # refusal rather than re-deriving it: this cluster is skipped,
                # and every later one will be refused the same way.
                clusters_attempted -= 1
                skipped_clusters += 1
                continue
            call_responses.append(response)
            estimated_cost_usd += self._estimate_response_cost(response)
            input_tokens += int(response.usage.get("input_tokens", 0))
            output_tokens += int(response.usage.get("output_tokens", 0))
            replay_hits += int(response.usage.get("replay_hits", 0))
            replay_misses += int(response.usage.get("replay_misses", 0))
            raw_texts.append(response.text)
            try:
                parsed = parse_json_response(response.text)
            except (json.JSONDecodeError, ValueError) as first_exc:
                if self.fallback_provider is None or self._budget_exhausted(
                    estimated_cost_usd
                ):
                    parse_failures += 1
                    skipped_clusters += 1
                    continue
                fallback_attempts += 1
                try:
                    fallback_response = self._generate_cluster(
                        self.fallback_provider,
                        system_prompt=system_prompt,
                        template=template,
                        records=cluster_records,
                        run_id=run_id,
                    )
                except BudgetExhausted:
                    # The shared budget is spent: the primary lane's calls have
                    # already consumed what the fallback would have needed.
                    fallback_attempts -= 1
                    parse_failures += 1
                    skipped_clusters += 1
                    continue
                call_responses.append(fallback_response)
                estimated_cost_usd += self._estimate_response_cost(fallback_response)
                input_tokens += int(fallback_response.usage.get("input_tokens", 0))
                output_tokens += int(fallback_response.usage.get("output_tokens", 0))
                replay_hits += int(fallback_response.usage.get("replay_hits", 0))
                replay_misses += int(fallback_response.usage.get("replay_misses", 0))
                raw_texts.append(fallback_response.text)
                try:
                    parsed = parse_json_response(fallback_response.text)
                except (json.JSONDecodeError, ValueError) as second_exc:
                    parse_error_details.append(
                        _parse_failure_detail(first_exc, second_exc)
                    )
                    parse_failures += 1
                    skipped_clusters += 1
                    continue
                fallback_accepted += 1
            emitted, resolved = _count_citation_resolution(parsed, cluster_records)
            citations_emitted += emitted
            citations_resolved += resolved
            try:
                rendered = render_products(
                    parsed,
                    cluster_records,
                    run_id=run_id,
                    start_index=len(products) + 1,
                    min_citations_by_category=self.min_citations_by_category,
                    min_contributors_by_category=self.min_contributors_by_category,
                    allowed_categories=(
                        set(self.categories) if self.categories else None
                    ),
                    seen_topics=seen_topics,
                    now=self._now,
                )
            except EvidenceRuleRejection:
                evidence_rule_rejections += 1
                citation_failures += 1
                skipped_clusters += 1
                continue
            except ValueError:
                citation_failures += 1
                skipped_clusters += 1
                continue
            products.extend(rendered)

        response = LLMResponse(
            text="\n".join(raw_texts),
            model=self.model,
            usage={
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "skipped_clusters": skipped_clusters,
                "estimated_cost_microusd": round(estimated_cost_usd * 1_000_000),
            },
        )
        category_distribution: dict[str, int] = {}
        for product in products:
            category_distribution[product.category] = (
                category_distribution.get(product.category, 0) + 1
            )
        return DistillationOutput(
            products=products,
            response=response,
            skipped_clusters=skipped_clusters,
            estimated_cost_usd=estimated_cost_usd,
            cluster_assignments=cluster_assignments,
            fallback_attempts=fallback_attempts,
            parse_failures=parse_failures,
            citation_failures=citation_failures,
            parse_error_details=parse_error_details,
            clusters_attempted=clusters_attempted,
            citations_emitted=citations_emitted,
            citations_resolved=citations_resolved,
            fallback_accepted=fallback_accepted,
            evidence_rule_rejections=evidence_rule_rejections,
            category_distribution=dict(sorted(category_distribution.items())),
            replay_hits=replay_hits,
            replay_misses=replay_misses,
            # The synthesized aggregate ``response`` above is not a call, so it
            # is deliberately not part of the fold.
            provenance=aggregate(call_responses),
        )

    def _cluster_records(
        self, records: list[dict[str, Any]]
    ) -> list[list[dict[str, Any]]]:
        if not records:
            return []
        texts = [
            str(record.get("body") or record.get("content") or "") for record in records
        ]
        vectors = self.embedder.embed(texts).vectors
        labels = cluster_vectors(
            vectors,
            eps=self.dbscan_eps,
            min_samples=self.dbscan_min_samples,
            # Taken from the embedder that produced these very vectors, rather
            # than resolved a second time. Two lookups of the same policy can
            # disagree; reading it off the producer cannot (§5.5.7).
            policy=self.embedder.policy,
        )
        grouped: dict[int, list[dict[str, Any]]] = {}
        for label, record in zip(labels, records, strict=True):
            grouped.setdefault(label, []).append(record)
        return [grouped[label] for label in sorted(grouped)]

    def _generate_cluster(
        self,
        provider: LLMProvider,
        *,
        system_prompt: str,
        template: str,
        records: list[dict[str, Any]],
        run_id: str,
    ) -> LLMResponse:
        user_prompt = _render_user_prompt(
            template,
            records=records,
            categories=self.categories,
            category_evidence_rules=self.category_evidence_rules,
        )
        return provider.generate(
            LLMRequest(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                model=self.model,
                max_tokens=self.max_tokens,
                metadata={"records": records, "run_id": run_id},
            )
        )

    def _budget_exhausted(self, estimated_cost_usd: float) -> bool:
        """The post-hoc cost accumulator, kept as a backstop for every lane.

        This deliberately stays active even when ``context.budget`` is injected.
        A ``RunBudget`` only constrains providers that actually call
        ``reserve()`` -- today just the production backend -- so switching this
        off whenever a budget exists would leave the mock, Anthropic and bridge
        lanes with no ceiling at all. That is precisely the default profile
        (a mock provider plus a configured ``budget_usd``).

        The two are not duplicates of one rule: the budget is the *pre-call*
        ceiling that stops an over-ceiling request from being issued, while this
        is an *after the fact* accumulator over observed usage. They are
        computed from the same profile pricing, so they agree.
        """
        if self.cost_budget_usd is None:
            return False
        return estimated_cost_usd >= self.cost_budget_usd

    @property
    def run_budget(self) -> RunBudget | None:
        return self.context.budget if self.context is not None else None

    def _now(self) -> str:
        """The run's clock: injected identity first, wall clock otherwise."""
        identity = self.context.identity if self.context is not None else None
        return identity.now() if identity is not None else utc_now()

    def _checked_run_id(self, run_id: str) -> str:
        """With an identity injected, it is the only source of the run id.

        A caller-supplied id that disagrees is a wiring bug, not something to
        paper over: the two would end up in different artifacts of the same run
        (the request metadata against the product ids), and the mismatch would
        only surface much later, in a manifest nobody can reconcile.
        """
        identity = self.context.identity if self.context is not None else None
        if identity is None:
            return run_id
        if run_id and run_id != identity.run_id:
            raise ValueError(
                f"run_id {run_id!r} conflicts with the injected identity "
                f"{identity.run_id!r}"
            )
        return identity.run_id

    def _estimate_response_cost(self, response: LLMResponse) -> float:
        input_tokens = int(response.usage.get("input_tokens", 0))
        output_tokens = int(response.usage.get("output_tokens", 0))
        return (input_tokens / 1_000_000) * self.input_cost_per_mtok_usd + (
            output_tokens / 1_000_000
        ) * self.output_cost_per_mtok_usd


def parse_json_response(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as first_exc:
        try:
            value = _raw_decode_object(text)
        except json.JSONDecodeError as exc:
            raise exc from first_exc
    if not isinstance(value, dict):
        raise ValueError("distiller response must be a JSON object")
    if "products" not in value or not isinstance(value["products"], list):
        raise ValueError("distiller response must include products[]")
    return value


def _render_user_prompt(
    template: str,
    *,
    records: list[dict[str, Any]],
    categories: list[str],
    category_evidence_rules: dict[str, dict[str, int]],
) -> str:
    context = {
        "records_json": records,
        "categories_json": categories,
        "category_evidence_rules_json": category_evidence_rules,
        "response_schema_json": _response_schema(categories),
    }
    contract_placeholders = {
        "categories_json",
        "category_evidence_rules_json",
        "response_schema_json",
    }
    rendered = template
    used_contract_placeholders: set[str] = set()
    for placeholder, value in context.items():
        token = "{{ " + placeholder + " }}"
        if token in rendered and placeholder in contract_placeholders:
            used_contract_placeholders.add(placeholder)
        rendered = rendered.replace(
            token,
            json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2),
        )
    if used_contract_placeholders == contract_placeholders:
        return rendered
    return (
        rendered.rstrip()
        + "\n\n"
        + _contract_block(
            categories=categories,
            category_evidence_rules=category_evidence_rules,
        )
    )


def _contract_block(
    *,
    categories: list[str],
    category_evidence_rules: dict[str, dict[str, int]],
) -> str:
    return "\n".join(
        [
            "Distill contract:",
            "Categories:",
            json.dumps(categories, ensure_ascii=False, sort_keys=True, indent=2),
            "Category evidence rules:",
            json.dumps(
                category_evidence_rules,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ),
            "Accepted response schema:",
            json.dumps(
                _response_schema(categories),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ),
        ]
    )


def _response_schema(categories: list[str]) -> dict[str, Any]:
    category_schema: dict[str, Any] = {"type": "string"}
    if categories:
        category_schema["enum"] = categories
    return {
        "type": "object",
        "required": ["products"],
        "properties": {
            "products": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": [
                        "title",
                        "category",
                        "summary",
                        "evidence",
                        "citations",
                    ],
                    "properties": {
                        "title": {"type": "string"},
                        "category": category_schema,
                        "summary": {"type": "string"},
                        "evidence": {"type": "string"},
                        "topic_key": {"type": "string"},
                        "confidence": {"type": "string"},
                        "citations": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["memory_id", "revision_hash"],
                            },
                        },
                        "conflicts": {"type": "array"},
                    },
                },
            }
        },
    }


def _parse_failure_detail(first_exc: Exception, second_exc: Exception) -> str:
    return (
        f"primary={first_exc.__class__.__name__}: {first_exc}; "
        f"fallback={second_exc.__class__.__name__}: {second_exc}"
    )


def _count_citation_resolution(
    parsed: dict[str, Any],
    records: list[dict[str, Any]],
) -> tuple[int, int]:
    """Count ``(emitted, resolved)`` LLM citations against the cluster snapshot.

    Read-only diagnostic feeding the ``citation_resolution_rate`` tuning metric;
    it never alters the render-stage citation gate. A citation resolves when its
    ``memory_id`` is present in this cluster's input records and the
    ``revision_hash`` matches the snapshot revision.
    """
    records_by_id = {str(record.get("memory_id")): record for record in records}
    emitted = 0
    resolved = 0
    for raw in parsed.get("products") or []:
        if not isinstance(raw, dict):
            continue
        for citation in raw.get("citations") or []:
            if not isinstance(citation, dict):
                continue
            emitted += 1
            memory_id = str(citation.get("memory_id") or "")
            revision_hash = str(citation.get("revision_hash") or "")
            record = records_by_id.get(memory_id)
            if record is not None and str(record.get("revision_hash")) == revision_hash:
                resolved += 1
    return emitted, resolved


def _category_evidence_rules(
    *,
    min_citations_by_category: dict[str, int],
    min_contributors_by_category: dict[str, int],
) -> dict[str, dict[str, int]]:
    categories = set(min_citations_by_category) | set(min_contributors_by_category)
    return {
        canonical_category(category): {
            key: value
            for key, value in {
                "min_citations": min_citations_by_category.get(category),
                "min_contributors": min_contributors_by_category.get(category),
            }.items()
            if value is not None
        }
        for category in sorted(categories)
    }


def render_products(
    parsed: dict[str, Any],
    records: list[dict[str, Any]],
    *,
    run_id: str,
    start_index: int = 1,
    min_citations_by_category: dict[str, int] | None = None,
    min_contributors_by_category: dict[str, int] | None = None,
    allowed_categories: set[str] | None = None,
    seen_topics: set[tuple[str, str]] | None = None,
    now: Callable[[], str] | None = None,
) -> list[TeamProduct]:
    records_by_id = {str(record["memory_id"]): record for record in records}
    min_citations_by_category = {
        canonical_category(category): value
        for category, value in (min_citations_by_category or {}).items()
    }
    min_contributors_by_category = {
        canonical_category(category): value
        for category, value in (min_contributors_by_category or {}).items()
    }
    seen_topics = seen_topics if seen_topics is not None else set()
    # The run's clock, not this module's: ``generated_at`` feeds the product
    # body, whose hash feeds the manifest, so a stray clock read here is
    # enough to make two runs of identical input differ.
    generated_at = now() if now is not None else utc_now()
    products: list[TeamProduct] = []
    pending_topics: set[tuple[str, str]] = set()
    for index, raw in enumerate(parsed["products"], start=start_index):
        if not isinstance(raw, dict):
            raise ValueError("product must be an object")
        citations = _normalize_citations(raw.get("citations") or [], records_by_id)
        category = canonical_category(str(raw.get("category") or "common_knowledge"))
        if allowed_categories is not None and category not in allowed_categories:
            raise ValueError(f"product category not allowed: {category}")
        min_citations = int(min_citations_by_category.get(category, 1))
        if len(citations) < min_citations:
            raise EvidenceRuleRejection(
                "product citations below category evidence rule"
            )
        title = str(raw.get("title") or category.replace("_", " ").title()).strip()
        summary = str(raw.get("summary") or "").strip()
        evidence = str(raw.get("evidence") or "").strip()
        if not evidence:
            raise ValueError("product evidence must not be empty")
        citation_refs = [_citation_ref(citation) for citation in citations]
        contributors = sorted(
            {
                str(citation.get("author_node"))
                for citation in citations
                if citation.get("author_node")
            }
        )
        min_contributors = int(min_contributors_by_category.get(category, 1))
        if len(contributors) < min_contributors:
            raise EvidenceRuleRejection(
                "product contributors below category evidence rule"
            )
        canonical_topic_key = _slug(str(raw.get("topic_key") or title or category))
        topic_id = f"topic-v1-{canonical_topic_key}"
        topic_key = (category, topic_id)
        if topic_key in seen_topics or topic_key in pending_topics:
            raise ValueError(f"duplicate active topic in run: {category}/{topic_id}")
        pending_topics.add(topic_key)
        evidence_hash = short_hash("|".join(citation_refs) + evidence + summary, 8)
        distillation_id = f"distill-{run_id}-{index:03d}"
        path = Path("team") / category / f"{_slug(title)}-{evidence_hash}.md"
        conflicts = normalize_conflicts(raw.get("conflicts"))
        frontmatter = {
            "schema_version": "memory-net/team-product/v1",
            "distillation_id": distillation_id,
            "title": title,
            "category": category,
            "topic_id": topic_id,
            "topic_normalization_version": 1,
            "canonical_topic_key": canonical_topic_key,
            "topic_key": str(raw.get("topic_key") or canonical_topic_key),
            "evidence_hash": evidence_hash,
            "confidence": str(raw.get("confidence") or "unknown"),
            "contributors": contributors,
            "citations": citations,
            "conflicts": conflicts,
            "generated_at": generated_at,
            "run_id": run_id,
        }
        body = _render_body(
            title=title,
            summary=summary,
            evidence=evidence,
            citations=citations,
            conflicts=conflicts,
        )
        products.append(
            TeamProduct(
                relative_path=path,
                content=render_frontmatter(frontmatter, body),
                frontmatter=frontmatter,
                category=category,
                distillation_id=distillation_id,
            )
        )
    seen_topics.update(pending_topics)
    return products


def _normalize_citations(
    raw_citations: list[Any],
    records_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, str]]:
    citations: list[dict[str, str]] = []
    for item in raw_citations:
        if not isinstance(item, dict):
            raise ValueError("product citations must use {memory_id, revision_hash}")
        memory_id = str(item.get("memory_id") or "")
        revision_hash = str(item.get("revision_hash") or "")
        if not memory_id or not revision_hash:
            raise ValueError("product citation missing memory_id or revision_hash")
        record = records_by_id.get(memory_id)
        if record is None:
            raise ValueError(f"product cites unknown memory id: {memory_id}")
        if str(record.get("revision_hash")) != revision_hash:
            raise ValueError(
                f"product cites wrong revision for {memory_id}: {revision_hash}"
            )
        citations.append(
            {
                "memory_id": memory_id,
                "revision_hash": revision_hash,
                "author_node": str(record.get("author_node") or ""),
                "source_branch_sha": str(record.get("source_branch_sha") or ""),
                "relative_path": str(record.get("relative_path") or ""),
            }
        )
    return citations


def _citation_ref(citation: dict[str, str]) -> str:
    return f"{citation['memory_id']}@{citation['revision_hash']}"


def _render_body(
    *,
    title: str,
    summary: str,
    evidence: str,
    citations: list[dict[str, str]],
    conflicts: list[dict[str, Any]],
) -> str:
    lines = [f"# {title}", "", summary or "No summary available.", ""]
    if evidence:
        lines.extend(["## Evidence", "", evidence, ""])
    if conflicts:
        lines.extend([f"## {CONFLICT_MARKER}", ""])
        for conflict in conflicts:
            description = str(conflict.get("description") or "").strip()
            if description:
                lines.extend([description, ""])
            for position in conflict.get("positions") or []:
                lines.append(f"- {position['claim']}")
            lines.append("")
    lines.extend(["## Citations", ""])
    lines.extend(f"- `{_citation_ref(citation)}`" for citation in citations)
    lines.append("")
    return "\n".join(lines)


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "common"


class AmbiguousProductsPayload(ValueError):
    """More than one candidate object in the response carried ``products``.

    Picking one would be a guess about which the model meant, and the wrong
    guess publishes team knowledge derived from the wrong payload -- silently,
    since the products validate either way. Refusing is the only honest option.
    """


def _raw_decode_object(text: str) -> dict[str, Any]:
    """Find the response payload inside text that is not purely JSON.

    Scans *every* decodable object and takes the one carrying ``products``,
    rather than the first that parses. A reasoning model routinely emits a
    preamble object before its answer, and taking the first would either raise
    "must include products[]" against a payload that was never the answer, or
    -- far worse -- silently accept a leading object that happens to have a
    ``products`` key.
    """
    decoder = json.JSONDecoder()
    first_error: json.JSONDecodeError | None = None
    candidates: list[dict[str, Any]] = []
    matches: list[dict[str, Any]] = []
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError as exc:
            if first_error is None:
                first_error = exc
            continue
        if not isinstance(value, dict):
            continue
        candidates.append(value)
        if "products" in value:
            matches.append(value)
    if len(matches) > 1:
        raise AmbiguousProductsPayload(
            f"distiller response contains {len(matches)} objects with products[]; "
            "refusing to guess which one is the answer"
        )
    if matches:
        return matches[0]
    if candidates:
        # Key names only, never values: this message is written into
        # parse_error_details, which lands in metrics that get published.
        keys = sorted({key for candidate in candidates for key in candidate})
        raise ValueError(
            "distiller response has no object with products[]; "
            f"candidate keys were: {', '.join(keys) or '(none)'}"
        )
    if first_error is not None:
        raise first_error
    raise json.JSONDecodeError("no JSON object found", text, 0)
