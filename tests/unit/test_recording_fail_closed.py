"""The recorder's two-stage scan (§5.5.10).

The assertion that matters most is the call count of the inner provider: a
request-side rule that fires *after* the call has gone out protects nothing, so
"the prompt never left" is asserted directly rather than inferred from an
exception type.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memory_net.distiller import cli as cli_module
from memory_net.distiller.llm.base import LLMProvider, LLMRequest, LLMResponse
from memory_net.distiller.llm.recording import (
    UNSCANNED,
    BundleRecorder,
    RecordingProvider,
    ScanningProvider,
)
from memory_net.distiller.orchestrator import DistillOrchestrator
from memory_net.distiller.output_scanner import (
    OutputScanner,
    RedactionViolation,
    Ruleset,
    ScanVerdict,
)
from memory_net.distiller.pipeline import DistillationPipeline
from memory_net.distiller.runctx import RunContextBuilder

PATTERNS_PATH = Path("config/redaction_patterns.yaml")
HOME_PATH = "/" + "home/" + "example-user/notes.md"


class _CountingProvider(LLMProvider):
    """Records how many times it was actually asked to generate."""

    def __init__(self, text: str = "clean answer", **response: object) -> None:
        self.calls = 0
        self.text = text
        self.response_kwargs = response

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        return LLMResponse(
            text=self.text,
            model=request.model,
            usage={"input_tokens": 1, "output_tokens": 1},
            **self.response_kwargs,  # type: ignore[arg-type]
        )


@pytest.fixture()
def ruleset() -> Ruleset:
    return Ruleset.from_yaml(PATTERNS_PATH)


def _request(**overrides: object) -> LLMRequest:
    values: dict[str, object] = {
        "system_prompt": "be terse",
        "user_prompt": "summarize",
        "model": "test-model",
    }
    values.update(overrides)
    return LLMRequest(**values)  # type: ignore[arg-type]


def _recorder(tmp_path: Path) -> BundleRecorder:
    return BundleRecorder(
        tmp_path / "bundle" / "calls.jsonl",
        requests_dir=tmp_path / "bundle" / "requests",
    )


def _wrapped(
    tmp_path: Path,
    scanner: OutputScanner | None,
    provider: _CountingProvider,
) -> RecordingProvider:
    context = RunContextBuilder().set_scanner(scanner).freeze()
    return RecordingProvider(provider, recorder=_recorder(tmp_path), context=context)


def _assert_nothing_on_disk(tmp_path: Path) -> None:
    assert not (tmp_path / "bundle" / "calls.jsonl").exists()
    requests = tmp_path / "bundle" / "requests"
    assert not requests.exists() or list(requests.iterdir()) == []


def test_a_clean_call_is_recorded(tmp_path: Path, ruleset: Ruleset) -> None:
    inner = _CountingProvider()
    provider = _wrapped(tmp_path, OutputScanner(ruleset), inner)

    response = provider.generate(_request())

    assert response.text == "clean answer"
    assert inner.calls == 1
    assert (tmp_path / "bundle" / "calls.jsonl").read_text(encoding="utf-8").strip()
    assert len(list((tmp_path / "bundle" / "requests").iterdir())) == 1


def test_a_leaking_prompt_never_reaches_the_provider(
    tmp_path: Path, ruleset: Ruleset
) -> None:
    """The core of the two-stage split: refusing to write a prompt down after
    sending it would protect nothing."""
    inner = _CountingProvider()
    provider = _wrapped(tmp_path, OutputScanner(ruleset), inner)

    with pytest.raises(RedactionViolation):
        provider.generate(_request(user_prompt=f"see {HOME_PATH}"))

    assert inner.calls == 0
    _assert_nothing_on_disk(tmp_path)


def test_a_leaking_system_prompt_never_reaches_the_provider(
    tmp_path: Path, ruleset: Ruleset
) -> None:
    inner = _CountingProvider()
    provider = _wrapped(tmp_path, OutputScanner(ruleset), inner)

    with pytest.raises(RedactionViolation):
        provider.generate(_request(system_prompt=f"context: {HOME_PATH}"))

    assert inner.calls == 0
    _assert_nothing_on_disk(tmp_path)


def test_leaking_request_metadata_never_reaches_the_provider(
    tmp_path: Path, ruleset: Ruleset
) -> None:
    inner = _CountingProvider()
    provider = _wrapped(tmp_path, OutputScanner(ruleset), inner)

    with pytest.raises(RedactionViolation):
        provider.generate(_request(metadata={"source": HOME_PATH}))

    assert inner.calls == 0
    _assert_nothing_on_disk(tmp_path)


def test_a_leaking_response_is_not_written(tmp_path: Path, ruleset: Ruleset) -> None:
    """The call has happened by then -- unavoidable -- but nothing lands."""
    inner = _CountingProvider(text=f"the file is at {HOME_PATH}")
    provider = _wrapped(tmp_path, OutputScanner(ruleset), inner)

    with pytest.raises(RedactionViolation):
        provider.generate(_request())

    assert inner.calls == 1
    _assert_nothing_on_disk(tmp_path)


def test_leaking_response_metadata_is_not_written(
    tmp_path: Path, ruleset: Ruleset
) -> None:
    inner = _CountingProvider(raw_meta={"upstream": HOME_PATH})
    provider = _wrapped(tmp_path, OutputScanner(ruleset), inner)

    with pytest.raises(RedactionViolation):
        provider.generate(_request())

    _assert_nothing_on_disk(tmp_path)


def test_writing_a_blocked_verdict_directly_is_refused(
    tmp_path: Path, ruleset: Ruleset
) -> None:
    """The bypass guard: the write point asks for the scan's verdict and
    refuses one that says no."""
    scanner = OutputScanner(ruleset, mode="observe")
    verdict = scanner.check(f"see {HOME_PATH}", field_kind="prose")
    recorder = _recorder(tmp_path)

    with pytest.raises(RedactionViolation):
        recorder.record(
            _request(),
            LLMResponse(text="x", model="m"),
            scanned=ScanVerdict(
                hits=verdict.hits,
                ruleset_version=verdict.ruleset_version,
                mode="enforce",
            ),
        )

    _assert_nothing_on_disk(tmp_path)


def test_record_cannot_be_called_without_a_verdict(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        _recorder(tmp_path).record(  # type: ignore[call-arg]
            _request(), LLMResponse(text="x", model="m")
        )


def test_observe_records_the_hit_and_still_writes(
    tmp_path: Path, ruleset: Ruleset
) -> None:
    """Reachable only by constructing the provider directly: the CLI refuses
    --record under observe. It is tested so the mode's own semantics are
    pinned, not because a run can get here."""
    scanner = OutputScanner(ruleset, mode="observe")
    inner = _CountingProvider(text=f"the file is at {HOME_PATH}")
    provider = _wrapped(tmp_path, scanner, inner)

    provider.generate(_request())

    assert inner.calls == 1
    assert (tmp_path / "bundle" / "calls.jsonl").read_text(encoding="utf-8").strip()
    assert scanner.rule_counts == {"abs_home_path": 1}


def test_without_a_context_behaviour_is_unchanged(tmp_path: Path) -> None:
    """Regression: the pre-injection path still records exactly as it did."""
    inner = _CountingProvider(text=f"the file is at {HOME_PATH}")
    provider = RecordingProvider(inner, recorder=_recorder(tmp_path))

    response = provider.generate(_request(user_prompt=f"about {HOME_PATH}"))

    assert response.text.endswith(HOME_PATH)
    assert inner.calls == 1
    assert (tmp_path / "bundle" / "calls.jsonl").read_text(encoding="utf-8").strip()
    assert provider.scanner is None
    assert UNSCANNED.mode == "none"


def test_the_three_consumers_share_one_scanner(
    tmp_path: Path, ruleset: Ruleset
) -> None:
    """Every consumer reads the scanner off the context. Two instances would
    mean two hit counters and, worse, two answers about the mode."""
    scanner = OutputScanner(ruleset)
    context = RunContextBuilder().set_scanner(scanner).freeze()
    recording = RecordingProvider(
        _CountingProvider(), recorder=_recorder(tmp_path), context=context
    )
    pipeline = DistillationPipeline(
        context=context,
        provider=recording,
        system_prompt_path=tmp_path / "system.md",
        user_prompt_path=tmp_path / "user.md",
    )
    orchestrator = DistillOrchestrator(
        repo_path=tmp_path,
        pipeline=pipeline,
        context=context,
    )

    assert recording.scanner is scanner
    assert pipeline.context is not None and pipeline.context.scanner is scanner
    assert orchestrator.context is not None
    assert orchestrator.context.scanner is scanner


# --------------------------------------------------------------------------
# The lane that records nothing is still gated on the way out
# --------------------------------------------------------------------------


def test_a_non_recording_lane_still_scans_before_the_call(ruleset: Ruleset) -> None:
    """Not recording means the prompt is never written down, not that it is
    never sent -- and the prompt is what carries the leaf records."""
    inner = _CountingProvider()
    provider = ScanningProvider(
        inner, context=RunContextBuilder().set_scanner(OutputScanner(ruleset)).freeze()
    )

    with pytest.raises(RedactionViolation):
        provider.generate(_request(user_prompt=f"about {HOME_PATH}"))

    assert inner.calls == 0


def test_a_non_recording_lane_passes_a_clean_call_through(ruleset: Ruleset) -> None:
    inner = _CountingProvider()
    provider = ScanningProvider(
        inner, context=RunContextBuilder().set_scanner(OutputScanner(ruleset)).freeze()
    )

    assert provider.generate(_request()).text == "clean answer"
    assert inner.calls == 1


def test_a_non_recording_lane_without_a_scanner_is_transparent() -> None:
    inner = _CountingProvider()
    provider = ScanningProvider(inner, context=None)

    assert provider.generate(_request(user_prompt=f"about {HOME_PATH}")).text
    assert inner.calls == 1


def test_the_cli_wraps_an_unrecorded_lane_in_the_scanner(ruleset: Ruleset) -> None:
    context = RunContextBuilder().set_scanner(OutputScanner(ruleset)).freeze()

    wrapped = cli_module._wrap_record(_CountingProvider(), None, context)

    assert isinstance(wrapped, ScanningProvider)
    assert wrapped.scanner is context.scanner


def test_the_cli_leaves_a_scannerless_lane_exactly_as_it_was() -> None:
    inner = _CountingProvider()

    assert cli_module._wrap_record(inner, None, None) is inner


def test_a_replay_miss_still_scans_before_it_calls_the_model(
    tmp_path: Path, ruleset: Ruleset
) -> None:
    """A replay *miss* is a real call with a real prompt: the gate cannot be a
    property of whether the bundle happened to hold the answer."""
    import argparse

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "calls.jsonl").write_text("", encoding="utf-8")
    context = RunContextBuilder().set_scanner(OutputScanner(ruleset)).freeze()
    args = argparse.Namespace(
        replay=bundle,
        replay_strict=False,
        replay_mode="allow-live-on-miss",
        model=None,
        record=None,
        record_dir=None,
    )

    replay = cli_module._replay_provider(args, "mock", context=context)

    assert isinstance(replay.fallback_provider, ScanningProvider)


def test_both_replay_lanes_get_the_scanner(tmp_path: Path, ruleset: Ruleset) -> None:
    import argparse

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "calls.jsonl").write_text("", encoding="utf-8")
    context = RunContextBuilder().set_scanner(OutputScanner(ruleset)).freeze()
    args = argparse.Namespace(
        replay=bundle,
        replay_strict=False,
        replay_mode="allow-live-on-miss",
        model=None,
        record=None,
        record_dir=None,
        provider="mock",
        fallback_provider="mock",
    )

    primary, fallback = cli_module._build_providers(
        args, "mock", "mock", {}, context=context
    )

    assert isinstance(primary.fallback_provider, ScanningProvider)
    assert fallback is not None
    assert isinstance(fallback.fallback_provider, ScanningProvider)
