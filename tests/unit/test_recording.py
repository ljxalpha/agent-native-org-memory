import json
from pathlib import Path

import pytest

from memory_net.distiller.llm.base import LLMProvider, LLMRequest, LLMResponse
from memory_net.distiller.llm.mock import MockLLMProvider
from memory_net.distiller.llm.recording import (
    BundleRecorder,
    RecordingProvider,
    ReplayProvider,
)


def _request(user_prompt: str) -> LLMRequest:
    return LLMRequest(
        system_prompt="system",
        user_prompt=user_prompt,
        model="mock",
        metadata={"records": []},
    )


def test_recording_provider_replays_strict_request(tmp_path: Path) -> None:
    record_path = tmp_path / "record.jsonl"
    request = LLMRequest(
        system_prompt="system",
        user_prompt="records []",
        model="mock",
        metadata={"records": []},
    )

    response = RecordingProvider(MockLLMProvider(), record_path).generate(request)
    replayed = ReplayProvider(record_path).generate(request)

    assert replayed.text == response.text
    assert replayed.model == response.model


def test_replay_provider_rejects_strict_request_mismatch(tmp_path: Path) -> None:
    record_path = tmp_path / "record.jsonl"
    request = LLMRequest(
        system_prompt="system",
        user_prompt="records []",
        model="mock",
        metadata={"records": []},
    )
    RecordingProvider(MockLLMProvider(), record_path).generate(request)

    with pytest.raises(RuntimeError, match="replay request mismatch"):
        ReplayProvider(record_path).generate(
            LLMRequest(
                system_prompt="changed",
                user_prompt="records []",
                model="mock",
                metadata={"records": []},
            )
        )


def test_replay_provider_exhaustion_raises(tmp_path: Path) -> None:
    record_path = tmp_path / "record.jsonl"
    record_path.write_text("", encoding="utf-8")
    request = LLMRequest(
        system_prompt="system",
        user_prompt="records []",
        model="mock",
        metadata={"records": []},
    )
    replay = ReplayProvider(record_path)

    with pytest.raises(RuntimeError, match="replay record exhausted"):
        replay.generate(request)


def test_recording_provider_writes_run_scoped_bundle(tmp_path: Path) -> None:
    bundle = tmp_path / "record_runs" / "run-1"
    recorder = RecordingProvider(
        MockLLMProvider(),
        bundle / "calls.jsonl",
        requests_dir=bundle / "requests",
    )

    recorder.generate(_request("records [] one"))
    recorder.generate(_request("records [] two"))

    calls = (bundle / "calls.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(calls) == 2
    request_files = sorted((bundle / "requests").glob("*.json"))
    assert [path.name.split("-", 1)[0] for path in request_files] == ["0001", "0002"]
    first = json.loads(request_files[0].read_text(encoding="utf-8"))
    assert first["sequence"] == 1
    assert first["prompt_hash"]
    assert first["request"]["user_prompt"] == "records [] one"
    # The per-request file name embeds the prompt hash from the same record.
    assert request_files[0].name == f"0001-{first['prompt_hash']}.json"


def test_replay_provider_reads_bundle_directory(tmp_path: Path) -> None:
    bundle = tmp_path / "record_runs" / "run-1"
    recorder = RecordingProvider(
        MockLLMProvider(),
        bundle / "calls.jsonl",
        requests_dir=bundle / "requests",
    )
    first = recorder.generate(_request("records [] one"))
    second = recorder.generate(_request("records [] two"))

    replay = ReplayProvider(bundle)
    replayed_first = replay.generate(_request("records [] one"))
    replayed_second = replay.generate(_request("records [] two"))

    assert replayed_first.text == first.text
    assert replayed_second.text == second.text
    assert replayed_first.usage["replay_hits"] == 1


def test_bundle_recorder_shared_by_primary_and_fallback(tmp_path: Path) -> None:
    # A cluster's primary and fallback reuse the same prompt (same prompt_hash);
    # two RecordingProviders sharing one BundleRecorder must still produce an
    # ordered calls.jsonl and non-colliding per-request files (distinguished only
    # by the shared sequence), and one shared ReplayProvider must hand them back
    # in recorded order under prompt-only matching.
    bundle = tmp_path / "record_runs" / "run-1"
    recorder = BundleRecorder(bundle / "calls.jsonl", requests_dir=bundle / "requests")
    primary = RecordingProvider(_FixedProvider("primary-bad"), recorder=recorder)
    fallback = RecordingProvider(_FixedProvider("fallback-good"), recorder=recorder)
    request = _request("records [] same")

    primary.generate(request)
    fallback.generate(request)

    calls = (bundle / "calls.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(calls) == 2
    request_files = sorted((bundle / "requests").glob("*.json"))
    assert [path.name.split("-", 1)[0] for path in request_files] == ["0001", "0002"]
    # Same prompt hash on both files; only the shared sequence keeps them distinct.
    suffixes = {path.name.split("-", 1)[1] for path in request_files}
    assert len(suffixes) == 1

    replay = ReplayProvider(bundle, mode="prompt-only")
    assert replay.generate(request).text == "primary-bad"
    assert replay.generate(request).text == "fallback-good"


def test_sharing_cursor_interleaves_two_lanes_in_order(tmp_path: Path) -> None:
    # Two distinct ReplayProviders sharing one cursor consume the recorded calls
    # in order across lanes (strict): primary takes call 1, fallback takes call 2.
    bundle = tmp_path / "bundle"
    recorder = BundleRecorder(bundle / "calls.jsonl")
    RecordingProvider(_FixedProvider("call-1"), recorder=recorder).generate(
        _request("p")
    )
    RecordingProvider(_FixedProvider("call-2"), recorder=recorder).generate(
        _request("p")
    )

    primary = ReplayProvider(bundle)
    fallback = primary.sharing_cursor(fallback_provider=None)

    assert primary.generate(_request("p")).text == "call-1"
    assert fallback.generate(_request("p")).text == "call-2"


def test_replay_fallback_lane_uses_its_own_live_on_miss(tmp_path: Path) -> None:
    # Under allow-live-on-miss the primary and fallback lanes
    # share one ordered cursor but keep lane-specific live-on-miss providers, so a
    # fallback-lane miss hits the configured fallback, not the primary provider.
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "calls.jsonl").write_text("", encoding="utf-8")  # every call misses
    primary = ReplayProvider(
        bundle,
        mode="allow-live-on-miss",
        fallback_provider=_FixedProvider("primary-live"),
    )
    fallback = primary.sharing_cursor(fallback_provider=_FixedProvider("fallback-live"))

    assert primary.generate(_request("a")).text == "primary-live"
    assert fallback.generate(_request("b")).text == "fallback-live"


class _FixedProvider(LLMProvider):
    def __init__(self, text: str) -> None:
        self.text = text

    def generate(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(text=self.text, model=request.model, usage={})
