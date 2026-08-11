from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from memory_net.distiller.provenance import RunProvenance


@dataclass(frozen=True)
class DistillMetrics:
    run_id: str
    input_records: int
    products: int
    input_tokens: int = 0
    output_tokens: int = 0
    skipped_clusters: int = 0
    estimated_cost_usd: float = 0.0
    pre_publish_duration_seconds: float = 0.0
    clusters: int = 0
    fallback_attempts: int = 0
    parse_failures: int = 0
    parse_error_details: list[str] = field(default_factory=list)
    citation_failures: int = 0
    config_revision: str = ""
    master_snapshot_sha: str = ""
    replay_mode: str = "live"
    id_collisions: int = 0
    incompatible_leaves: int = 0
    quarantined_records: int = 0
    scanner_audit_summary: dict[str, int] = field(default_factory=dict)
    review_gate_status: str = "disabled"
    publish_branch: str = "master"
    target_branch: str = "master"
    status: str = "success"
    recent_errors: list[str] = field(default_factory=list)
    # Tuning metrics (METRICS-001). Raw numerator/denominator are stored next to
    # every derived rate so a 0/0 is never silently hidden behind a bare ratio.
    clusters_attempted: int = 0
    # Clusters that issued at least one call, which is the denominator the
    # per-cluster ``parse_failures`` belongs over. Equal to
    # ``clusters_attempted`` unless llm.max_records_per_prompt is set, where
    # that counter counts sub-prompts because total_calls is derived from it.
    clusters_prompted: int = 0
    parse_failure_rate: float = 0.0
    citations_emitted: int = 0
    citations_resolved: int = 0
    citation_resolution_rate: float = 1.0
    fallback_selected: int = 0
    fallback_accepted: int = 0
    category_distribution: dict[str, int] = field(default_factory=dict)
    evidence_rule_rejections: int = 0
    # Replay source metrics (RECORD-001). replay_mode is declared above; these are
    # zero / "live" on a normal run.
    replay_hits: int = 0
    replay_misses: int = 0
    replay_live_calls: int = 0
    # Run-level provenance. Orthogonal to the replay_* counters above: those say
    # whether calls were replayed, these say where their accounting came from.
    # The defaults describe a run that made no call at all.
    provider_backends: list[str] = field(
        default_factory=lambda: RunProvenance.empty().provider_backends
    )
    usage_source: str = "none"
    usage_source_counts: dict[str, int] = field(
        default_factory=lambda: RunProvenance.empty().usage_source_counts
    )
    # Which profile bytes produced this run, and whether its vectors were real.
    # Empty / False describe a run with no profile and the default backend, so
    # a pre-provenance run serializes exactly as it always did.
    profile_path: str = ""
    profile_blob_sha256: str = ""
    embedding_test_only: bool = False
    # Output scanner (§5.5.9). ``None`` means this run had no scanner at all,
    # and the key is then left out entirely rather than serialized as null: a
    # run predating the scanner must produce the same key set it always did.
    # ``redaction_observations`` is a rule x field-kind x count map, never text.
    output_scanner_mode: str | None = None
    output_scanner_ruleset_version: str | None = None
    redaction_observations: dict[str, dict[str, int]] | None = None
    # The enforce-mode counterpart: how many hits refused this run, and which
    # rules produced them. Written on the failure path, where "which rules
    # fired" is the only thing that can be said without quoting the content.
    redaction_failures: int | None = None
    redaction_rule_counts: dict[str, int] | None = None
    # Cluster and record accounting (DISTILL-001). ``clusters_attempted`` above
    # keeps its own meaning -- the clusters that actually issued a call, which
    # total_calls, the replay counters and the parse-failure rate all read --
    # so these are new names rather than a redefinition of it.
    # Prompt-cache hits, as the backend reported them. ``unreported`` with two
    # zeros is the honest description of a backend that says nothing; nothing
    # here is ever estimated (§7.2 DISTILL-001).
    prompt_cache_read_tokens: int = 0
    prompt_cache_creation_tokens: int = 0
    prompt_cache_source: str = "unreported"
    clusters_total: int = 0
    clusters_terminal: int = 0
    cluster_terminal_counts: dict[str, int] = field(default_factory=dict)
    records_cited: int = 0
    records_unclustered: int = 0
    accounting_mismatch: bool = False
    accounting_audited: bool = True
    # Which run this one continued, empty when it started from scratch.
    resumed_from: str = ""

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.as_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def as_dict(self) -> dict[str, object]:
        """The serialized form: keys that do not apply are absent, not null.

        Only the fields listed in ``OPTIONAL_KEYS`` may vanish. Every other key
        is present on every run, so a consumer can read them unconditionally.
        """
        payload: dict[str, object] = asdict(self)
        for key in OPTIONAL_KEYS:
            if payload.get(key) is None:
                payload.pop(key, None)
        return payload


OPTIONAL_KEYS = (
    "output_scanner_mode",
    "output_scanner_ruleset_version",
    "redaction_observations",
    "redaction_failures",
    "redaction_rule_counts",
)
"""Keys written only when they apply. See ``DistillMetrics.as_dict``."""


@dataclass(frozen=True)
class RunAccounting:
    """The two identities a run's counts must satisfy (§7.2 DISTILL-001).

    Cluster level: every cluster reaches exactly one terminal, so the terminal
    counts sum to the number of clusters.

    Record level: every input record is either cited by some product or listed
    in ``unclustered.json``, so the two partition the input.

    Kept apart on purpose. ``unclustered.json`` is a *record* list, and adding
    it to a cluster-level sum would double-count a failed cluster -- once as a
    skipped cluster and again through each of its records.
    """

    clusters_total: int
    clusters_terminal: int
    terminal_counts: dict[str, int]
    records_cited: int
    records_unclustered: int
    input_records: int
    mismatch: bool
    audited: bool = True
    """Whether the identities above were actually checked this run.

    ``mismatch: false`` on a run that was never audited would read as "the
    counts add up" when nothing was compared -- the same provenance mistake as
    reporting an estimate as a measurement. See ``of``.
    """

    @classmethod
    def of(
        cls,
        *,
        clusters_total: int,
        terminal_counts: dict[str, int] | None,
        records_cited: int,
        records_unclustered: int,
        input_records: int,
        aborted: bool = False,
    ) -> RunAccounting:
        """Fold the counts, and decide whether they add up.

        ``aborted`` suppresses the verdict rather than the counts: a run the
        output scanner stopped mid-way has clusters that never reached a
        terminal *by design*, and reporting that as an accounting fault would
        bury the refusal that is the actual reason the run failed.

        ``terminal_counts is None`` means no terminals were reported at all --
        an output that predates them, or a stand-in pipeline. There is nothing
        to check then, and inventing a failure out of an absent report would say
        the counts disagree when none were given.
        """
        counts = dict(terminal_counts or {})
        clusters_terminal = sum(counts.values())
        mismatch = (
            terminal_counts is not None
            and not aborted
            and (
                clusters_total != clusters_terminal
                or input_records != records_cited + records_unclustered
            )
        )
        return cls(
            audited=terminal_counts is not None and not aborted,
            clusters_total=clusters_total,
            clusters_terminal=clusters_terminal,
            terminal_counts=counts,
            records_cited=records_cited,
            records_unclustered=records_unclustered,
            input_records=input_records,
            mismatch=mismatch,
        )

    def as_metrics_fields(self) -> dict[str, object]:
        return {
            "clusters_total": self.clusters_total,
            "clusters_terminal": self.clusters_terminal,
            "cluster_terminal_counts": self.terminal_counts,
            "records_cited": self.records_cited,
            "records_unclustered": self.records_unclustered,
            "accounting_mismatch": self.mismatch,
            "accounting_audited": self.audited,
        }


def parse_failure_rate(parse_failures: int, clusters_prompted: int) -> float:
    """Fraction of prompted clusters whose output failed to parse.

    The denominator is *clusters*, not calls: a parse failure is recorded once
    per cluster, and dividing it by a sub-prompt count would report a third of
    the real failure rate on a run that split its clusters in three.

    ``0.0`` when no cluster was prompted (e.g. empty input or a fully
    budget-exhausted run), so the rate never raises on a 0/0.
    """
    if clusters_prompted <= 0:
        return 0.0
    return round(parse_failures / clusters_prompted, 6)


def citation_resolution_rate(citations_resolved: int, citations_emitted: int) -> float:
    """Fraction of LLM-emitted citations that resolved to a snapshot revision.

    ``1.0`` when no citation was emitted (no unresolved citation exists); the
    raw ``citations_emitted`` / ``citations_resolved`` counts disclose the 0/0.
    """
    if citations_emitted <= 0:
        return 1.0
    return round(citations_resolved / citations_emitted, 6)
