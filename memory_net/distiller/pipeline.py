from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from memory_net.distiller.categories import canonical_category
from memory_net.distiller.clustering.cache import EmbeddingCache
from memory_net.distiller.clustering.dbscan import cluster_vectors
from memory_net.distiller.clustering.embedder import FastEmbedder
from memory_net.distiller.conflict import CONFLICT_MARKER, normalize_conflicts
from memory_net.distiller.llm.base import LLMProvider, LLMRequest, LLMResponse
from memory_net.distiller.output_scanner import (
    PROSE,
    OutputScanner,
    RedactionViolation,
    RuleHit,
)
from memory_net.distiller.provenance import RunProvenance, aggregate
from memory_net.distiller.runctx import (
    NotExecuted,
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


class DistillationAborted(RuntimeError):
    """The run stopped mid-way, carrying the counts it had reached (§5.5.13).

    The output travels *with* the exception because the counts and the refusal
    have to arrive together: an exception that unwinds without them leaves the
    orchestrator able to say a run failed but not what it saw, and re-deriving
    that after the fact is not possible.
    """

    def __init__(self, output: DistillationOutput, message: str) -> None:
        self.output = output
        super().__init__(message)


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
    # Output scanner refusals (§5.5.13). Non-zero means the run was stopped, not
    # that a cluster was skipped; rule names and counts only, never content.
    redaction_failures: int = 0
    redaction_rule_counts: dict[str, int] | None = None
    # Cluster accounting (DISTILL-001): one mutually exclusive terminal per
    # cluster, so ``clusters_total == sum(counts)`` is checkable. ``unclustered``
    # is a *record*-level list -- ids, revisions and a reason code, never text --
    # and therefore deliberately not part of the cluster-level identity.
    cluster_terminal_counts: dict[str, int] | None = None
    unclustered: list[dict[str, str]] | None = None
    records_cited: int = 0
    # Run-level fold of every call's provenance, primary and fallback lanes alike.
    provenance: RunProvenance = field(default_factory=RunProvenance.empty)


_OK = "ok"
_PARSE_FAILED = "parse_failed"
_NOT_EXECUTED = "not_executed"
_ABORTED = "aborted"

DISTILLED = "distilled"
SKIPPED_BUDGET = "skipped_budget"
SKIPPED_PARSE = "skipped_parse"
SKIPPED_CITATION = "skipped_citation"
SKIPPED_EVIDENCE = "skipped_evidence"
NOISE = "noise"
RESUMED = "resumed"

TERMINAL_STATES = (
    DISTILLED,
    SKIPPED_BUDGET,
    SKIPPED_PARSE,
    SKIPPED_CITATION,
    SKIPPED_EVIDENCE,
    NOISE,
    RESUMED,
)
"""Every cluster reaches exactly one of these (§7.2 DISTILL-001).

``skipped_clusters`` stays what it always was -- the sum of several of them --
so nothing downstream that reads it changes meaning; these are the mutually
exclusive terminals underneath that sum, which is what makes the accounting
identity checkable at all.
"""

_REASON_BY_TERMINAL = {
    SKIPPED_BUDGET: "budget_skipped",
    SKIPPED_PARSE: "parse_failed",
    SKIPPED_CITATION: "citation_failed",
    SKIPPED_EVIDENCE: "evidence_rule_failed",
    NOISE: "noise",
}
"""Why a record of a non-distilled cluster is not represented in any product."""

UNCITED = "uncited"
"""Its cluster distilled, but no product cited this record."""


@dataclass(frozen=True)
class _ChunkOutcome:
    """What one sub-prompt produced: a payload, or the reason there is none."""

    state: str
    parsed: dict[str, Any] | None = None


@dataclass
class _RunTally:
    """The run's mutable counters, gathered in one place.

    They used to be two dozen locals in ``run()``. Collecting them lets the
    per-cluster and per-sub-prompt steps be separate methods without either
    growing a twenty-argument signature or returning a tuple nobody can read.
    """

    products: list[TeamProduct] = field(default_factory=list)
    skipped_clusters: int = 0
    fallback_attempts: int = 0
    parse_failures: int = 0
    citation_failures: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    replay_hits: int = 0
    replay_misses: int = 0
    estimated_cost_usd: float = 0.0
    clusters_attempted: int = 0
    citations_emitted: int = 0
    citations_resolved: int = 0
    fallback_accepted: int = 0
    evidence_rule_rejections: int = 0
    raw_texts: list[str] = field(default_factory=list)
    parse_error_details: list[str] = field(default_factory=list)
    # Every actual call, in invocation order, primary and fallback alike --
    # the fallback lane is precisely what makes a run's provenance "mixed".
    call_responses: list[LLMResponse] = field(default_factory=list)
    redaction_hits: list[RuleHit] = field(default_factory=list)
    seen_topics: set[tuple[str, str]] = field(default_factory=set)
    # One entry per cluster, in execution order, paired with that cluster's
    # records so a record-level reason can be derived without a second pass
    # over the clustering.
    terminals: list[tuple[str, list[dict[str, Any]]]] = field(default_factory=list)


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
        max_records_per_prompt: int | None = None,
        singleton_lane_enabled: bool = False,
        singleton_lane_max_records: int | None = None,
        schedule_by_estimated_cost: bool = False,
        embedding_cache: EmbeddingCache | None = None,
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
        # None, not a default number: an unset second split has to leave the
        # prompt exactly as it was, because a cluster sent as several prompts is
        # a different set of calls and a different recording (DISTILL-001).
        self.max_records_per_prompt = (
            None if max_records_per_prompt is None else int(max_records_per_prompt)
        )
        # Default false, and it has to stay that way in every recorded profile:
        # strict replay compares calls in recording order, so re-ordering the
        # clusters turns a whole golden bundle into misses (§7.2 DISTILL-001).
        self.schedule_by_estimated_cost = bool(schedule_by_estimated_cost)
        # None means "no cache", which is the default and is byte-identical to
        # the behaviour before the cache existed: every vector is recomputed.
        self.embedding_cache = embedding_cache
        self.singleton_lane_enabled = bool(singleton_lane_enabled)
        self.singleton_lane_max_records = (
            None
            if singleton_lane_max_records is None
            else int(singleton_lane_max_records)
        )
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
        tally = _RunTally()

        for index, cluster_records in enumerate(
            self._execution_order(clusters, tally=tally)
        ):
            terminal = self._run_cluster_in_scope(
                cluster_records,
                scope=f"cluster-{index}",
                tally=tally,
                system_prompt=system_prompt,
                template=template,
                run_id=run_id,
            )
            if terminal == _ABORTED:
                # The whole loop, not this cluster: this input and this prompt
                # just produced sensitive content, which says nothing good about
                # the clusters after it either.
                break
            tally.terminals.append((terminal, cluster_records))

        response = LLMResponse(
            text="\n".join(tally.raw_texts),
            model=self.model,
            usage={
                "input_tokens": tally.input_tokens,
                "output_tokens": tally.output_tokens,
                "skipped_clusters": tally.skipped_clusters,
                "estimated_cost_microusd": round(tally.estimated_cost_usd * 1_000_000),
            },
        )
        category_distribution: dict[str, int] = {}
        for product in tally.products:
            category_distribution[product.category] = (
                category_distribution.get(product.category, 0) + 1
            )
        rule_counts: dict[str, int] = {}
        for hit in tally.redaction_hits:
            rule_counts[hit.rule] = rule_counts.get(hit.rule, 0) + 1
        cited = _cited_memory_ids(tally.products)
        unclustered = _unclustered_records(tally.terminals, cited=cited)
        terminal_counts = {state: 0 for state in TERMINAL_STATES}
        for terminal, _ in tally.terminals:
            terminal_counts[terminal] += 1
        output = DistillationOutput(
            products=tally.products,
            response=response,
            skipped_clusters=tally.skipped_clusters,
            estimated_cost_usd=tally.estimated_cost_usd,
            cluster_assignments=cluster_assignments,
            fallback_attempts=tally.fallback_attempts,
            parse_failures=tally.parse_failures,
            citation_failures=tally.citation_failures,
            parse_error_details=tally.parse_error_details,
            clusters_attempted=tally.clusters_attempted,
            citations_emitted=tally.citations_emitted,
            citations_resolved=tally.citations_resolved,
            fallback_accepted=tally.fallback_accepted,
            evidence_rule_rejections=tally.evidence_rule_rejections,
            category_distribution=dict(sorted(category_distribution.items())),
            replay_hits=tally.replay_hits,
            replay_misses=tally.replay_misses,
            redaction_failures=len(tally.redaction_hits),
            redaction_rule_counts=dict(sorted(rule_counts.items())) or None,
            cluster_terminal_counts=terminal_counts,
            unclustered=unclustered,
            records_cited=len(
                [record for record in records if _id_of(record) in cited]
            ),
            # The synthesized aggregate ``response`` above is not a call, so it
            # is deliberately not part of the fold.
            provenance=aggregate(tally.call_responses),
        )
        if tally.redaction_hits:
            raise DistillationAborted(
                output,
                f"output scanner refused {len(tally.redaction_hits)} value(s): "
                + ", ".join(sorted(rule_counts)),
            )
        return output

    def _run_cluster_in_scope(
        self,
        cluster_records: list[dict[str, Any]],
        *,
        scope: str,
        tally: _RunTally,
        system_prompt: str,
        template: str,
        run_id: str,
    ) -> str:
        """One cluster, inside its own budget scope and behind the soft ceiling.

        The soft ceiling is checked *here* rather than inside ``reserve()``: it
        means "open no new cluster", which only the loop can honour. A budget
        that refused mid-cluster would abandon a cluster whose earlier
        sub-prompts have already been paid for.
        """
        budget = self.run_budget
        if budget is None:
            return self._run_cluster(
                cluster_records,
                tally=tally,
                system_prompt=system_prompt,
                template=template,
                run_id=run_id,
            )
        if budget.soft_exhausted:
            tally.skipped_clusters += 1
            return SKIPPED_BUDGET
        with budget.enter_scope(scope):
            return self._run_cluster(
                cluster_records,
                tally=tally,
                system_prompt=system_prompt,
                template=template,
                run_id=run_id,
            )

    def _run_cluster(
        self,
        cluster_records: list[dict[str, Any]],
        *,
        tally: _RunTally,
        system_prompt: str,
        template: str,
        run_id: str,
    ) -> str:
        """Distil one cluster into the tally, returning its terminal state.

        One cluster is one *rendered* unit even when it is sent as several
        sub-prompts: the split exists to keep a prompt inside the model's
        context window, not to turn one cluster into several products, so the
        payloads are merged before render and the cluster-level counters move
        exactly once.

        The return value is one of ``TERMINAL_STATES``, or ``_ABORTED`` -- which
        is not a terminal but the end of the run, and therefore never counted.
        """
        if self._budget_exhausted(tally.estimated_cost_usd):
            tally.skipped_clusters += 1
            return SKIPPED_BUDGET
        payloads: list[dict[str, Any]] = []
        for chunk in self._split_cluster(cluster_records):
            outcome = self._chunk_payload(
                chunk,
                tally=tally,
                system_prompt=system_prompt,
                template=template,
                run_id=run_id,
            )
            if outcome.state == _ABORTED:
                return _ABORTED
            if outcome.state == _PARSE_FAILED:
                # One sub-prompt failing fails the cluster: rendering the
                # remainder would publish a product whose evidence is a subset
                # of its cluster, and nothing downstream could tell that apart
                # from the whole.
                tally.parse_failures += 1
                tally.skipped_clusters += 1
                return SKIPPED_PARSE
            if outcome.state == _NOT_EXECUTED:
                # A gate refused before the call went out -- the run budget, or
                # the cross-run attempt ceiling. Respect the refusal rather than
                # re-deriving it: every later cluster is refused the same way.
                break
            if outcome.parsed is not None:
                payloads.append(outcome.parsed)
        if not payloads:
            tally.skipped_clusters += 1
            return SKIPPED_BUDGET
        parsed = payloads[0] if len(payloads) == 1 else _merge_payloads(payloads)
        emitted, resolved = _count_citation_resolution(parsed, cluster_records)
        tally.citations_emitted += emitted
        tally.citations_resolved += resolved
        try:
            rendered = render_products(
                parsed,
                cluster_records,
                run_id=run_id,
                start_index=len(tally.products) + 1,
                min_citations_by_category=self.min_citations_by_category,
                min_contributors_by_category=self.min_contributors_by_category,
                allowed_categories=(set(self.categories) if self.categories else None),
                seen_topics=tally.seen_topics,
                now=self._now,
            )
        except EvidenceRuleRejection:
            tally.evidence_rule_rejections += 1
            tally.citation_failures += 1
            tally.skipped_clusters += 1
            return SKIPPED_EVIDENCE
        except ValueError:
            tally.citation_failures += 1
            tally.skipped_clusters += 1
            return SKIPPED_CITATION
        tally.products.extend(rendered)
        return DISTILLED

    def _execution_order(
        self,
        clusters: list[list[dict[str, Any]]],
        *,
        tally: _RunTally,
    ) -> list[list[dict[str, Any]]]:
        """The order clusters are distilled in, and which ones are not.

        With the singleton lane off -- the default -- this is the identity, and
        a lone record is distilled in place exactly as it always was.

        With it on, singletons (DBSCAN noise points, which ``dbscan.py`` already
        numbers as clusters of their own) are deferred behind every real
        cluster, so a run that runs out of budget spends what it had on grouped
        evidence rather than on whichever lone record happened to sort first.
        ``singleton_lane_max_records`` caps how many of them the lane distils at
        all; the remainder terminate as ``noise`` and appear in
        ``unclustered.json`` with that reason.
        """
        if not self.singleton_lane_enabled:
            return self._by_estimated_cost(clusters)
        grouped = [cluster for cluster in clusters if len(cluster) > 1]
        singletons = [cluster for cluster in clusters if len(cluster) <= 1]
        cap = self.singleton_lane_max_records
        if cap is not None and cap < len(singletons):
            for cluster in singletons[cap:]:
                tally.terminals.append((NOISE, cluster))
            singletons = singletons[:cap]
        return self._by_estimated_cost(grouped) + self._by_estimated_cost(singletons)

    def _by_estimated_cost(
        self, clusters: list[list[dict[str, Any]]]
    ) -> list[list[dict[str, Any]]]:
        """Cheapest cluster first, when the profile asked for it.

        Cheapest-first maximises how many clusters finish before a ceiling is
        reached. It is off by default and must stay off wherever a run is
        recorded or replayed: replay matches calls positionally, so a re-ordered
        run misses on every one of them.

        The estimate is prompt size, taken from the same record text the
        embedder saw. Sorting is stable, so clusters of equal size keep their
        clustering order and the run stays reproducible.
        """
        if not self.schedule_by_estimated_cost:
            return clusters
        return sorted(clusters, key=_estimated_prompt_size)

    def _chunk_payload(
        self,
        chunk: list[dict[str, Any]],
        *,
        tally: _RunTally,
        system_prompt: str,
        template: str,
        run_id: str,
    ) -> _ChunkOutcome:
        """One sub-prompt: call, parse, fall back once, scan.

        Every counter touched here is a *call* counter rather than a cluster
        one, so it stays correct however many sub-prompts a cluster is sent as.
        """
        tally.clusters_attempted += 1
        try:
            response = self._generate_cluster(
                self.provider,
                system_prompt=system_prompt,
                template=template,
                records=chunk,
                run_id=run_id,
            )
        except NotExecuted:
            tally.clusters_attempted -= 1
            return _ChunkOutcome(state=_NOT_EXECUTED)
        self._absorb(response, tally=tally)
        try:
            parsed = parse_json_response(response.text)
        except (json.JSONDecodeError, ValueError) as first_exc:
            if self.fallback_provider is None or self._budget_exhausted(
                tally.estimated_cost_usd
            ):
                return _ChunkOutcome(state=_PARSE_FAILED)
            tally.fallback_attempts += 1
            try:
                fallback_response = self._generate_cluster(
                    self.fallback_provider,
                    system_prompt=system_prompt,
                    template=template,
                    records=chunk,
                    run_id=run_id,
                )
            except NotExecuted:
                # A gate refused before the call went out: the shared budget is
                # spent, or the cross-run attempt ceiling is reached. Either way
                # the primary lane's calls consumed what the fallback needed.
                tally.fallback_attempts -= 1
                return _ChunkOutcome(state=_PARSE_FAILED)
            self._absorb(fallback_response, tally=tally)
            try:
                parsed = parse_json_response(fallback_response.text)
            except (json.JSONDecodeError, ValueError) as second_exc:
                detail = _parse_failure_detail(first_exc, second_exc)
                try:
                    # The parser quotes model-controlled fragments (a candidate
                    # key name, the position of the failure), and this string is
                    # published in metrics.json. It is the one piece of model
                    # output that reaches an artifact without having been parsed
                    # first, so it is scanned like any other.
                    self._scan_parse_detail(detail)
                except RedactionViolation as exc:
                    tally.redaction_hits.extend(exc.hits)
                    return _ChunkOutcome(state=_ABORTED)
                tally.parse_error_details.append(detail)
                return _ChunkOutcome(state=_PARSE_FAILED)
            tally.fallback_accepted += 1
        try:
            # Parsed into an object, nothing written yet: the last moment at
            # which refusing costs nothing but the run.
            self._scan_parsed(parsed)
        except RedactionViolation as exc:
            tally.redaction_hits.extend(exc.hits)
            return _ChunkOutcome(state=_ABORTED)
        return _ChunkOutcome(state=_OK, parsed=parsed)

    def _absorb(self, response: LLMResponse, *, tally: _RunTally) -> None:
        """Fold one real call's usage into the run's counters."""
        tally.call_responses.append(response)
        tally.estimated_cost_usd += self._estimate_response_cost(response)
        tally.input_tokens += int(response.usage.get("input_tokens", 0))
        tally.output_tokens += int(response.usage.get("output_tokens", 0))
        tally.replay_hits += int(response.usage.get("replay_hits", 0))
        tally.replay_misses += int(response.usage.get("replay_misses", 0))
        tally.raw_texts.append(response.text)

    def _split_cluster(
        self, cluster_records: list[dict[str, Any]]
    ) -> list[list[dict[str, Any]]]:
        """Chunk a cluster's records for the prompt, or hand it back whole.

        Without ``max_records_per_prompt`` this is the identity, which is what
        keeps a profile that never mentions the split byte for byte on the
        behaviour it had before the split existed.
        """
        limit = self.max_records_per_prompt
        if limit is None or len(cluster_records) <= limit:
            return [cluster_records]
        return [
            cluster_records[start : start + limit]
            for start in range(0, len(cluster_records), limit)
        ]

    def _scan_parse_detail(self, detail: str) -> None:
        """Scan a parse-failure description before it can reach metrics."""
        scanner = self.context.scanner if self.context is not None else None
        if isinstance(scanner, OutputScanner):
            scanner.check(detail, field_kind=PROSE, field_path="parse_error_details")

    def _scan_parsed(self, parsed: dict[str, Any]) -> None:
        """Scan one cluster's parsed products (§5.5.13).

        The scanner comes off the context and is never constructed here: it is
        the run's single instance and its hit counter. No context, no scanner,
        and the behaviour is exactly what it was before this existed.
        """
        scanner = self.context.scanner if self.context is not None else None
        if isinstance(scanner, OutputScanner):
            scanner.check(parsed, field_kind=PROSE, field_path="products")

    def _cluster_records(
        self, records: list[dict[str, Any]]
    ) -> list[list[dict[str, Any]]]:
        if not records:
            return []
        texts = [
            str(record.get("body") or record.get("content") or "") for record in records
        ]
        vectors = self._embed(texts)
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

    def _embed(self, texts: list[str]) -> list[list[float]]:
        """This run's vectors, through the cache when the profile enabled one.

        The cache is keyed by the *policy* that produced the vectors, so a run
        can never be handed a vector from another backend or model; see
        ``clustering/cache.py``.
        """
        if self.embedding_cache is None:
            return self.embedder.embed(texts).vectors
        return self.embedding_cache.resolve(
            texts, lambda missing: self.embedder.embed(missing).vectors
        )

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


def _estimated_prompt_size(cluster_records: list[dict[str, Any]]) -> int:
    """How large this cluster's prompt will be, in characters.

    A proxy for cost, not a price: the ordering only needs to be monotone in
    prompt length, and a token estimate here would be a second, drifting copy of
    the one the budget already uses.
    """
    return sum(
        len(str(record.get("body") or record.get("content") or ""))
        for record in cluster_records
    )


def _id_of(record: dict[str, Any]) -> str:
    return str(record.get("memory_id"))


def _cited_memory_ids(products: list[TeamProduct]) -> set[str]:
    """Every memory id any product cites, which is what "used" means here."""
    cited: set[str] = set()
    for product in products:
        for citation in product.frontmatter.get("citations") or []:
            cited.add(str(citation.get("memory_id")))
    return cited


def _unclustered_records(
    terminals: list[tuple[str, list[dict[str, Any]]]],
    *,
    cited: set[str],
) -> list[dict[str, str]]:
    """Every input record no product cites, with the reason it is missing.

    Ids, revisions and a reason code only. Putting the record body here would
    create a second place model input lands on disk, one the output scanner
    never looked at -- the exact hole the write-time gate exists to close.

    Derived from the cluster terminals rather than recomputed, so the reason a
    record is absent is the same fact the cluster accounting reports.
    """
    seen: set[str] = set()
    missing: list[dict[str, str]] = []
    for terminal, cluster_records in terminals:
        reason = _REASON_BY_TERMINAL.get(terminal, UNCITED)
        for record in cluster_records:
            memory_id = _id_of(record)
            if memory_id in cited or memory_id in seen:
                continue
            seen.add(memory_id)
            missing.append(
                {
                    "memory_id": memory_id,
                    "revision_hash": str(record.get("revision_hash") or ""),
                    "reason_code": reason,
                }
            )
    return missing


def _merge_payloads(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold one cluster's sub-prompt payloads into a single payload.

    Keyed by ``(category, canonical topic key)``, which is what ``topic_id`` is
    derived from: two sub-prompts describing the same topic must not become two
    products, because the render stage would reject the second as a duplicate
    active topic and lose a whole sub-prompt's evidence with it.

    First occurrence wins for the prose fields -- a merge cannot concatenate
    summaries without inventing text no model produced -- while citations are
    unioned (deduplicated by ``memory_id@revision_hash``, order preserved) and
    conflicts appended. Anything that is not an object is passed through so the
    render stage still raises on it rather than having it silently dropped here.
    """
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    passthrough: list[Any] = []
    for payload in payloads:
        for raw in payload.get("products") or []:
            if not isinstance(raw, dict):
                passthrough.append(raw)
                continue
            category = canonical_category(str(raw.get("category") or ""))
            topic = _slug(str(raw.get("topic_key") or raw.get("title") or category))
            key = (category, topic)
            existing = merged.get(key)
            if existing is None:
                existing = dict(raw)
                existing["citations"] = []
                existing["conflicts"] = list(raw.get("conflicts") or [])
                merged[key] = existing
                order.append(key)
            else:
                existing["conflicts"].extend(raw.get("conflicts") or [])
            seen = {
                _raw_citation_ref(item)
                for item in existing["citations"]
                if isinstance(item, dict)
            }
            for citation in raw.get("citations") or []:
                ref = (
                    _raw_citation_ref(citation) if isinstance(citation, dict) else None
                )
                if ref is not None and ref in seen:
                    continue
                if ref is not None:
                    seen.add(ref)
                existing["citations"].append(citation)
    products: list[Any] = [merged[key] for key in order]
    products.extend(passthrough)
    return {"products": products}


def _raw_citation_ref(citation: dict[str, Any]) -> str:
    return f"{citation.get('memory_id')}@{citation.get('revision_hash')}"


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


MAX_DECODE_FAILURES = 64
"""How many opening braces may fail to decode before the scan gives up.

Set far above anything a real response produces -- a preamble mentioning braces
a few dozen times still fits -- and far below the point where the rescan cost of
adversarial nesting matters.
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
    index = 0
    failures = 0
    while index < len(text):
        if text[index] != "{":
            index += 1
            continue
        if failures >= MAX_DECODE_FAILURES:
            # A successful decode skips its own length; a failed one cannot,
            # because nothing in the error says where the next object begins.
            # Deeply nested truncated input ("{"x":{"x":...) therefore rescans
            # the remainder at every brace. Refusing is fail-closed and costs
            # nothing real: a response needing this many attempts to locate its
            # payload is not one to trust. Counts only, never content -- this
            # message is published in metrics.
            raise ValueError(
                "distiller response has more than "
                f"{MAX_DECODE_FAILURES} undecodable object candidates; "
                "refusing to keep scanning"
            )
        try:
            # Decoded in place rather than from ``text[index:]``: slicing at
            # every brace copies a suffix each time, so a brace-dense response
            # costs O(n^2) in both time and memory.
            value, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError as exc:
            if first_error is None:
                first_error = exc
            failures += 1
            index += 1
            continue
        # Skip past what was just decoded, so only *sibling* objects are
        # candidates. Scanning every brace would descend into the payload's own
        # nesting, and a product carrying a nested "products" key would then
        # look like a second, rival payload.
        index = end
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
