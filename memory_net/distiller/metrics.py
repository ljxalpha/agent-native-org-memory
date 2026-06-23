from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


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

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(asdict(self), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )


def parse_failure_rate(parse_failures: int, clusters_attempted: int) -> float:
    """Fraction of attempted clusters whose output failed to parse.

    ``0.0`` when no cluster was attempted (e.g. empty input or a fully
    budget-exhausted run), so the rate never raises on a 0/0.
    """
    if clusters_attempted <= 0:
        return 0.0
    return round(parse_failures / clusters_attempted, 6)


def citation_resolution_rate(citations_resolved: int, citations_emitted: int) -> float:
    """Fraction of LLM-emitted citations that resolved to a snapshot revision.

    ``1.0`` when no citation was emitted (no unresolved citation exists); the
    raw ``citations_emitted`` / ``citations_resolved`` counts disclose the 0/0.
    """
    if citations_emitted <= 0:
        return 1.0
    return round(citations_resolved / citations_emitted, 6)
