from __future__ import annotations

import json
from typing import Any

from memory_net.distiller.categories import canonical_category
from memory_net.distiller.llm.base import LLMProvider, LLMRequest, LLMResponse
from memory_net.shared.util.frontmatter import split_frontmatter


class MockLLMProvider(LLMProvider):
    def generate(self, request: LLMRequest) -> LLMResponse:
        records = _records_from_prompt(request.user_prompt)
        allowed_categories = _categories_from_prompt(request.user_prompt)
        products = []
        by_category: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            category = _product_category(
                str(record.get("category_hint") or "common_knowledge"),
                allowed_categories,
            )
            by_category.setdefault(
                category,
                [],
            ).append(record)

        for category, items in sorted(by_category.items()):
            first = items[0]
            _frontmatter, body = split_frontmatter(str(first.get("content") or ""))
            title = _title(first, category)
            products.append(
                {
                    "title": title,
                    "category": category,
                    "summary": _summary(body),
                    "evidence": _summary(body),
                    "citations": [
                        {
                            "memory_id": str(item["memory_id"]),
                            "revision_hash": str(item["revision_hash"]),
                        }
                        for item in items
                    ],
                }
            )
        return LLMResponse(
            text=json.dumps({"products": products}, ensure_ascii=False),
            model=request.model,
            usage={
                "input_tokens": len(request.user_prompt.split()),
                "output_tokens": 32 * max(1, len(products)),
            },
        )


def _records_from_prompt(user_prompt: str) -> list[dict[str, Any]]:
    decoder = json.JSONDecoder()
    saw_empty_list = False
    for index, char in enumerate(user_prompt):
        if char != "[":
            continue
        try:
            value, _end = decoder.raw_decode(user_prompt[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, list):
            records = [item for item in value if isinstance(item, dict)]
            if records and all("memory_id" in record for record in records):
                return records
            if not value:
                saw_empty_list = True
    if saw_empty_list:
        return []
    raise RuntimeError("mock provider could not find records JSON in user_prompt")


def _categories_from_prompt(user_prompt: str) -> list[str]:
    decoder = json.JSONDecoder()
    for index, char in enumerate(user_prompt):
        if char != "[":
            continue
        try:
            value, _end = decoder.raw_decode(user_prompt[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return [canonical_category(str(item)) for item in value]
    return []


def _product_category(raw_category: str, allowed_categories: list[str]) -> str:
    raw_category = canonical_category(raw_category)
    if not allowed_categories or raw_category in allowed_categories:
        return raw_category
    if "common_knowledge" in allowed_categories:
        return "common_knowledge"
    return allowed_categories[0]


def _title(record: dict[str, Any], category: str) -> str:
    source_fm = record.get("source_frontmatter")
    if isinstance(source_fm, dict) and source_fm.get("title"):
        return str(source_fm["title"])
    return category.replace("_", " ").replace("-", " ").title()


def _summary(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped[:240]
    return "No summary available."
