import json
import shutil
from pathlib import Path

from memory_net.distiller.metrics import (
    citation_resolution_rate,
    parse_failure_rate,
)
from scripts.golden_eval import evaluate
from scripts.metrics_aggregate import aggregate


def _write_run(repo: Path, run_id: str, metrics: dict[str, object]) -> None:
    run = repo / "distill_runs" / run_id
    run.mkdir(parents=True)
    (run / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")


def test_metrics_aggregate_sums_runs(tmp_path: Path) -> None:
    run = tmp_path / "distill_runs" / "r1"
    run.mkdir(parents=True)
    (run / "metrics.json").write_text(
        json.dumps(
            {
                "input_tokens": 1,
                "output_tokens": 2,
                "estimated_cost_usd": 0.5,
                "pre_publish_duration_seconds": 1.25,
            }
        ),
        encoding="utf-8",
    )

    report = aggregate(tmp_path)

    assert report["run_count"] == 1
    assert report["totals"]["input_tokens"] == 1
    assert report["totals"]["estimated_cost_usd"] == 0.5
    assert report["totals"]["pre_publish_duration_seconds"] == 1.25


def test_metrics_aggregate_rolls_up_tuning_funnel(tmp_path: Path) -> None:
    _write_run(
        tmp_path,
        "r1",
        {
            "clusters_attempted": 4,
            "parse_failures": 1,
            "citations_emitted": 6,
            "citations_resolved": 5,
            "fallback_selected": 2,
            "fallback_accepted": 1,
            "evidence_rule_rejections": 1,
            "category_distribution": {"best_practices": 2, "risk_signals": 1},
        },
    )
    _write_run(
        tmp_path,
        "r2",
        {
            "clusters_attempted": 6,
            "parse_failures": 0,
            "citations_emitted": 4,
            "citations_resolved": 4,
            "category_distribution": {"best_practices": 1, "common_knowledge": 3},
        },
    )

    report = aggregate(tmp_path)

    assert report["run_count"] == 2
    totals = report["totals"]
    assert totals["clusters_attempted"] == 10
    assert totals["parse_failures"] == 1
    assert totals["citations_emitted"] == 10
    assert totals["citations_resolved"] == 9
    assert totals["fallback_selected"] == 2
    assert totals["fallback_accepted"] == 1
    assert totals["evidence_rule_rejections"] == 1
    # Global rates recomputed from summed numerator/denominator, not averaged.
    assert report["rates"]["parse_failure_rate"] == 0.1
    assert report["rates"]["citation_resolution_rate"] == 0.9
    assert report["category_distribution"] == {
        "best_practices": 3,
        "common_knowledge": 3,
        "risk_signals": 1,
    }


def test_rate_helpers_handle_zero_denominator() -> None:
    assert parse_failure_rate(0, 0) == 0.0
    assert parse_failure_rate(1, 4) == 0.25
    assert citation_resolution_rate(0, 0) == 1.0
    assert citation_resolution_rate(9, 10) == 0.9


def test_golden_eval_fixture_passes() -> None:
    report = evaluate(Path("tests/fixtures/golden_cluster"))

    assert report["passed"] is True
    assert report["semantic_assertions"]


def test_golden_eval_suite_passes() -> None:
    report = evaluate(Path("tests/fixtures/golden_clusters"))

    assert report["passed"] is True
    assert report["fixture_count"] == 3
    assert all(item["semantic_assertions"] for item in report["fixtures"])


def test_golden_eval_does_not_mutate_fixture_prompts(tmp_path: Path) -> None:
    fixture = tmp_path / "golden_cluster"
    shutil.copytree(Path("tests/fixtures/golden_cluster"), fixture)
    system_before = (fixture / "system.md").read_text(encoding="utf-8")
    user_before = (fixture / "user.md").read_text(encoding="utf-8")

    evaluate(fixture)

    assert (fixture / "system.md").read_text(encoding="utf-8") == system_before
    assert (fixture / "user.md").read_text(encoding="utf-8") == user_before
