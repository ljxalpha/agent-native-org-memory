"""Cheapest-cluster-first scheduling, and the profiles that must never use it.

Replay compares calls positionally against the recording. Re-ordering clusters
therefore turns a golden bundle into a wall of misses, which is why the switch
defaults to off and why every recorded profile is asserted to leave it that way.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from memory_net.distiller.cli import _clustering_flag, _singleton_lane_cap
from memory_net.distiller.llm.base import LLMProvider, LLMRequest, LLMResponse
from memory_net.distiller.pipeline import DistillationPipeline

RECORDED_PROFILES = (
    Path("config/distill.yaml"),
    Path("config/distill.deepseek.yaml"),
    Path("tests/fixtures/golden_repo/config/distill.mock.yaml"),
    Path("tests/fixtures/golden_repo/config/distill.deepseek.yaml"),
)


class OrderRecordingProvider(LLMProvider):
    def __init__(self) -> None:
        self.calls: list[str] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        records = list(request.metadata["records"])
        self.calls.append(str(records[0]["memory_id"]))
        payload = {
            "products": [
                {
                    "title": f"Topic {records[0]['memory_id']}",
                    "topic_key": str(records[0]["memory_id"]),
                    "category": "common_knowledge",
                    "summary": "Summary.",
                    "evidence": "Evidence.",
                    "citations": [
                        {
                            "memory_id": str(record["memory_id"]),
                            "revision_hash": str(record["revision_hash"]),
                        }
                        for record in records
                    ],
                }
            ]
        }
        return LLMResponse(
            text=json.dumps(payload),
            model="test",
            usage={"input_tokens": 1, "output_tokens": 1},
        )


def _record(index: int, *, size: int) -> dict[str, Any]:
    return {
        "memory_id": f"leaf-a-mem-{index}",
        "revision_hash": f"rev{index:05d}",
        "author_node": "alice",
        "source_branch_sha": "deadbeef",
        "relative_path": f"memories/note-{index}.md",
        "content": "x" * size,
    }


def _pipeline(tmp_path: Path, provider: LLMProvider, **kwargs: Any) -> Any:
    system = tmp_path / "system.md"
    user = tmp_path / "user.md"
    system.write_text("system", encoding="utf-8")
    user.write_text("records {{ records_json }}", encoding="utf-8")
    pipeline = DistillationPipeline(
        provider=provider,
        system_prompt_path=system,
        user_prompt_path=user,
        **kwargs,
    )
    pipeline._cluster_records = lambda records: [  # type: ignore[method-assign]
        [record] for record in records
    ]
    return pipeline


def _records() -> list[dict[str, Any]]:
    return [
        _record(1, size=300),
        _record(2, size=10),
        _record(3, size=100),
    ]


def test_default_keeps_the_clustering_order(tmp_path: Path) -> None:
    provider = OrderRecordingProvider()
    pipeline = _pipeline(tmp_path, provider)

    pipeline.run(_records(), run_id="run")

    assert provider.calls == ["leaf-a-mem-1", "leaf-a-mem-2", "leaf-a-mem-3"]


def test_enabled_runs_the_cheapest_cluster_first(tmp_path: Path) -> None:
    provider = OrderRecordingProvider()
    pipeline = _pipeline(tmp_path, provider, schedule_by_estimated_cost=True)

    pipeline.run(_records(), run_id="run")

    assert provider.calls == ["leaf-a-mem-2", "leaf-a-mem-3", "leaf-a-mem-1"]


def test_equal_cost_clusters_keep_their_clustering_order(tmp_path: Path) -> None:
    provider = OrderRecordingProvider()
    pipeline = _pipeline(tmp_path, provider, schedule_by_estimated_cost=True)

    pipeline.run(
        [_record(1, size=10), _record(2, size=10), _record(3, size=10)],
        run_id="run",
    )

    assert provider.calls == ["leaf-a-mem-1", "leaf-a-mem-2", "leaf-a-mem-3"]


def test_cluster_assignments_stay_in_clustering_order(tmp_path: Path) -> None:
    pipeline = _pipeline(
        tmp_path, OrderRecordingProvider(), schedule_by_estimated_cost=True
    )

    output = pipeline.run(_records(), run_id="run")

    assert [entry["memory_ids"] for entry in output.cluster_assignments or []] == [
        ["leaf-a-mem-1"],
        ["leaf-a-mem-2"],
        ["leaf-a-mem-3"],
    ]


@pytest.mark.parametrize("profile", RECORDED_PROFILES, ids=lambda path: path.name)
def test_recorded_profiles_never_enable_cost_scheduling(profile: Path) -> None:
    config = yaml.safe_load(profile.read_text(encoding="utf-8")) or {}
    clustering = config.get("clustering") or {}

    assert clustering.get("schedule_by_estimated_cost") in (None, False)
    assert _clustering_flag(config, "schedule_by_estimated_cost") is False


def test_cli_reads_the_singleton_lane_cap() -> None:
    assert _singleton_lane_cap({}) is None
    assert _singleton_lane_cap({"clustering": {"singleton_lane": {}}}) is None
    assert (
        _singleton_lane_cap({"clustering": {"singleton_lane": {"max_records": 2}}}) == 2
    )
    with pytest.raises(SystemExit, match="must not be negative"):
        _singleton_lane_cap({"clustering": {"singleton_lane": {"max_records": -1}}})
