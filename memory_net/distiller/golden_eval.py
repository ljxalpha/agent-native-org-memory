from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from memory_net.distiller.llm.mock import MockLLMProvider
from memory_net.distiller.pipeline import DistillationPipeline


def evaluate_golden_fixture(fixture: Path) -> dict[str, Any]:
    if not (fixture / "records.json").exists():
        return _evaluate_suite(fixture)
    return _evaluate_one_fixture(fixture)


def _evaluate_suite(root: Path) -> dict[str, Any]:
    fixtures = sorted(
        path for path in root.iterdir() if (path / "records.json").exists()
    )
    if not fixtures:
        raise FileNotFoundError(f"golden fixture not found: {root}")
    results = [_evaluate_one_fixture(path) for path in fixtures]
    return {
        "schema_version": "memory-net/golden-eval-suite/v1",
        "passed": all(result["passed"] for result in results),
        "fixture_count": len(results),
        "fixtures": results,
    }


def _evaluate_one_fixture(fixture: Path) -> dict[str, Any]:
    records = json.loads((fixture / "records.json").read_text(encoding="utf-8"))
    expected = json.loads((fixture / "expected.json").read_text(encoding="utf-8"))
    output = DistillationPipeline(
        provider=MockLLMProvider(),
        system_prompt_path=fixture / "system.md",
        user_prompt_path=fixture / "user.md",
    ).run(records, run_id="golden")
    semantic_assertions = _semantic_assertions(fixture, expected, output)
    passed = len(output.products) >= int(expected.get("min_products", 1))
    if expected.get("required_category"):
        passed = passed and any(
            product.category == expected["required_category"]
            for product in output.products
        )
    passed = passed and all(item["passed"] for item in semantic_assertions)
    return {
        "schema_version": "memory-net/golden-eval/v1",
        "fixture": fixture.name,
        "passed": passed,
        "products": len(output.products),
        "categories": [product.category for product in output.products],
        "semantic_assertions": semantic_assertions,
    }


def _semantic_assertions(
    fixture: Path,
    expected: dict[str, Any],
    output: Any,
) -> list[dict[str, Any]]:
    required_terms = _required_terms(fixture, expected)
    if not required_terms:
        return [
            {
                "name": "required_terms_present",
                "passed": False,
                "message": "golden fixture must define required_terms",
            }
        ]
    text = "\n".join(product.content for product in output.products)
    return [{"term": str(term), "passed": str(term) in text} for term in required_terms]


def _required_terms(fixture: Path, expected: dict[str, Any]) -> list[str]:
    assertions_path = fixture / "assertions.yaml"
    if assertions_path.exists():
        value = yaml.safe_load(assertions_path.read_text(encoding="utf-8")) or {}
        if not isinstance(value, dict):
            raise ValueError(f"assertions must be a mapping: {assertions_path}")
        raw = value.get("required_terms") or value.get("semantic_assertions")
    else:
        raw = expected.get("required_terms")
    return [str(item) for item in raw or []]
