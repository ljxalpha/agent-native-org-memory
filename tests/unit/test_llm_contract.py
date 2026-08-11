"""LLMResponse provenance contract (§5.3.1 of the design doc).

Provenance travels as independent top-level fields, never inside ``usage``:
``usage`` values are consumed with ``int(v)`` downstream, so a string there
raises at the consumer instead of at construction.
"""

from __future__ import annotations

import dataclasses

import pytest

from memory_net.distiller.llm.base import USAGE_SOURCES, LLMRequest, LLMResponse
from memory_net.distiller.llm.mock import MockLLMProvider


def test_provenance_fields_exist_with_defaults() -> None:
    response = LLMResponse(text="{}", model="m")
    assert response.usage_source == "unknown"
    assert response.provider_backend == "unknown"
    assert response.raw_meta == {}


def test_raw_meta_default_is_not_shared_between_instances() -> None:
    first = LLMResponse(text="{}", model="m")
    second = LLMResponse(text="{}", model="m")
    assert first.raw_meta is not second.raw_meta


def test_field_set_is_pinned() -> None:
    """Guards the wire contract: record/replay and metrics key off these names."""
    names = [field.name for field in dataclasses.fields(LLMResponse)]
    assert names == [
        "text",
        "model",
        "usage",
        "usage_source",
        "provider_backend",
        "raw_meta",
    ]


def test_usage_source_domain_is_fixed() -> None:
    assert USAGE_SOURCES == ("reported", "estimated", "unknown")
    for value in USAGE_SOURCES:
        assert (
            LLMResponse(
                text="{}",
                model="m",
                # Backed by real counters: "reported" is only honoured when
                # both are present, so an empty usage here would be testing the
                # downgrade rather than the value domain.
                usage={"input_tokens": 1, "output_tokens": 1},
                usage_source=value,
            ).usage_source
            == value
        )


@pytest.mark.parametrize(
    "usage",
    [
        pytest.param({}, id="no-counters"),
        pytest.param({"input_tokens": 1}, id="output-counter-missing"),
        pytest.param({"output_tokens": 1}, id="input-counter-missing"),
        pytest.param({"input_tokens": -1, "output_tokens": 1}, id="negative-input"),
        pytest.param({"input_tokens": 1, "output_tokens": -1}, id="negative-output"),
    ],
)
def test_reported_is_downgraded_when_the_counts_do_not_back_it(
    usage: dict[str, int],
) -> None:
    """ "reported" is the one provenance value money depends on: a settlement
    refunds the reserved upper bound and charges these counts instead. A
    backend returning a partial usage object, or a relay that cannot vouch for
    what it forwarded, must not be able to claim it. A negative count is worse
    than a missing one -- it would *reduce* the recorded spend."""
    response = LLMResponse(text="{}", model="m", usage=usage, usage_source="reported")

    assert response.usage_source == "unknown"
    # The counts themselves are kept; only the claim about them is corrected.
    assert response.usage == usage


def test_the_downgrade_leaves_a_well_formed_report_alone() -> None:
    """A scanner that flags everything would be no better than none."""
    response = LLMResponse(
        text="{}",
        model="m",
        usage={"input_tokens": 0, "output_tokens": 0},
        usage_source="reported",
    )

    assert response.usage_source == "reported"


@pytest.mark.parametrize("value", ["", "replay", "REPORTED", "mixed", "none"])
def test_usage_source_rejects_values_outside_the_domain(value: str) -> None:
    with pytest.raises(ValueError, match="usage_source"):
        LLMResponse(text="{}", model="m", usage_source=value)


def test_provider_backend_rejects_empty() -> None:
    with pytest.raises(ValueError, match="provider_backend"):
        LLMResponse(text="{}", model="m", provider_backend="")


def test_provider_backend_accepts_any_non_empty_label() -> None:
    response = LLMResponse(text="{}", model="m", provider_backend="hermes_tool")
    assert response.provider_backend == "hermes_tool"


@pytest.mark.parametrize("raw_meta", [{"finish_reason": 1}, {2: "stop"}, {"a": None}])
def test_raw_meta_rejects_non_string_scalars(raw_meta: dict[object, object]) -> None:
    with pytest.raises(TypeError, match="raw_meta"):
        LLMResponse(text="{}", model="m", raw_meta=raw_meta)  # type: ignore[arg-type]


def test_raw_meta_accepts_string_scalars() -> None:
    response = LLMResponse(
        text="{}",
        model="m",
        raw_meta={"finish_reason": "stop", "model_version": "2026-01-01"},
    )
    assert response.raw_meta["finish_reason"] == "stop"


def test_usage_stays_integer_valued() -> None:
    """``usage`` must never carry provenance strings (see §5.3.1 constraint 1)."""
    response = MockLLMProvider().generate(_request())
    assert response.usage
    assert all(isinstance(value, int) for value in response.usage.values())


def test_mock_provider_reports_estimated_usage() -> None:
    response = MockLLMProvider().generate(_request())
    assert response.usage_source == "estimated"
    assert response.provider_backend == "mock"
    assert response.raw_meta == {}


def _request() -> LLMRequest:
    return LLMRequest(
        system_prompt="sys",
        user_prompt=(
            '["common_knowledge"] '
            '[{"memory_id": "m1", "revision_hash": "r1", '
            '"category_hint": "common_knowledge", "content": "hello world"}]'
        ),
        model="mock-memory-distiller",
    )
