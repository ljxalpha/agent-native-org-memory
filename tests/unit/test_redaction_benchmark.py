from pathlib import Path

from scripts.redaction_benchmark import evaluate


def test_redaction_benchmark_reports_pass_fail_counts() -> None:
    report = evaluate(
        Path("config/redaction_patterns.yaml"),
        Path("tests/fixtures/redaction/benchmark.jsonl"),
    )

    assert report["summary"] == {"passed": 3, "failed": 0, "total": 3}
