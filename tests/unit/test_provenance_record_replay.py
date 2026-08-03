"""Provenance survives the record -> replay round trip (§9.2 of the design doc).

Replay must hand back the *recorded* provenance verbatim; if it rewrote the
fields to say "replay", a bundle recorded against a real backend would lose the
fact that its token counts came from that backend, and an offline replay lane
could no longer reproduce that assertion.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memory_net.distiller.llm.base import LLMProvider, LLMRequest, LLMResponse
from memory_net.distiller.llm.recording import (
    BundleRecorder,
    RecordingProvider,
    ReplayProvider,
)


class _StubProvider(LLMProvider):
    def __init__(self, response: LLMResponse) -> None:
        self.response = response
        self.calls = 0

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        return self.response


def _request(user_prompt: str = "u") -> LLMRequest:
    return LLMRequest(system_prompt="s", user_prompt=user_prompt, model="m")


def _response(**kwargs: object) -> LLMResponse:
    base: dict[str, object] = {
        "text": '{"products": []}',
        "model": "m",
        "usage": {"input_tokens": 3, "output_tokens": 4},
        "usage_source": "reported",
        "provider_backend": "stub",
        "raw_meta": {"finish_reason": "stop"},
    }
    base.update(kwargs)
    return LLMResponse(**base)  # type: ignore[arg-type]


def test_record_serializes_provenance(tmp_path: Path) -> None:
    path = tmp_path / "calls.jsonl"
    RecordingProvider(_StubProvider(_response()), path).generate(_request())

    item = json.loads(path.read_text(encoding="utf-8").strip())
    assert item["response"]["usage_source"] == "reported"
    assert item["response"]["provider_backend"] == "stub"
    assert item["response"]["raw_meta"] == {"finish_reason": "stop"}


def test_replay_returns_recorded_provenance_verbatim(tmp_path: Path) -> None:
    path = tmp_path / "calls.jsonl"
    RecordingProvider(_StubProvider(_response()), path).generate(_request())

    replayed = ReplayProvider(path).generate(_request())
    assert replayed.usage_source == "reported"
    assert replayed.provider_backend == "stub"
    assert replayed.raw_meta == {"finish_reason": "stop"}
    # Replay is expressed by the orthogonal replay_* counters, not by rewriting
    # provenance.
    assert replayed.usage["replay_hits"] == 1


def test_replay_from_bundle_directory_keeps_provenance(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    recorder = BundleRecorder(bundle / "calls.jsonl", requests_dir=bundle / "requests")
    RecordingProvider(_StubProvider(_response()), recorder=recorder).generate(
        _request()
    )

    replayed = ReplayProvider(bundle).generate(_request())
    assert replayed.provider_backend == "stub"

    recorded_request = json.loads(
        next((bundle / "requests").iterdir()).read_text(encoding="utf-8")
    )
    assert recorded_request["response"]["usage_source"] == "reported"


def test_legacy_records_without_provenance_fall_back_to_unknown(tmp_path: Path) -> None:
    path = tmp_path / "calls.jsonl"
    path.write_text(
        json.dumps(
            {
                "prompt_hash": "ignored-in-strict-mode",
                "request": {
                    "system_prompt": "s",
                    "user_prompt": "u",
                    "model": "m",
                    "max_tokens": 2000,
                },
                "response": {"text": "{}", "model": "m", "usage": {"input_tokens": 1}},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    replayed = ReplayProvider(path).generate(_request())
    assert replayed.usage_source == "unknown"
    assert replayed.provider_backend == "unknown"
    assert replayed.raw_meta == {}


@pytest.mark.parametrize("raw_meta", [{"finish_reason": 1}, "stop", ["stop"]])
def test_replay_rejects_non_string_raw_meta(tmp_path: Path, raw_meta: object) -> None:
    path = tmp_path / "calls.jsonl"
    path.write_text(
        json.dumps(
            {
                "prompt_hash": "h",
                "request": {
                    "system_prompt": "s",
                    "user_prompt": "u",
                    "model": "m",
                    "max_tokens": 2000,
                },
                "response": {
                    "text": "{}",
                    "model": "m",
                    "usage": {},
                    "raw_meta": raw_meta,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="raw_meta"):
        ReplayProvider(path).generate(_request())


def test_live_on_miss_keeps_the_live_response_provenance(tmp_path: Path) -> None:
    path = tmp_path / "calls.jsonl"
    path.write_text("", encoding="utf-8")
    live = _StubProvider(_response(usage_source="estimated", provider_backend="mock"))

    replayed = ReplayProvider(
        path, mode="allow-live-on-miss", fallback_provider=live
    ).generate(_request())

    assert live.calls == 1
    assert replayed.usage_source == "estimated"
    assert replayed.provider_backend == "mock"
    assert replayed.usage["replay_misses"] == 1


def test_strict_request_matching_is_unchanged(tmp_path: Path) -> None:
    """Regression: provenance must not enter the strict comparison tuple."""
    path = tmp_path / "calls.jsonl"
    RecordingProvider(_StubProvider(_response()), path).generate(_request("u"))

    with pytest.raises(RuntimeError, match="replay request mismatch"):
        ReplayProvider(path).generate(_request("different"))


def test_prompt_hash_ignores_provenance(tmp_path: Path) -> None:
    """Bundle request filenames and prompt-only matching stay stable."""
    first = tmp_path / "a.jsonl"
    second = tmp_path / "b.jsonl"
    RecordingProvider(_StubProvider(_response()), first).generate(_request())
    RecordingProvider(
        _StubProvider(_response(usage_source="unknown", provider_backend="other")),
        second,
    ).generate(_request())

    assert (
        json.loads(first.read_text(encoding="utf-8"))["prompt_hash"]
        == json.loads(second.read_text(encoding="utf-8"))["prompt_hash"]
    )
