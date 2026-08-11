"""Prompt-cache accounting, from the provider's usage object to metrics.json.

The rule under test is a provenance rule, not an arithmetic one: a backend that
reports nothing produces zeros labelled ``unreported``, and the zeros are never
allowed to come from an estimate. Estimating cache hits would put a number that
looks measured next to numbers that are.
"""

from __future__ import annotations

from typing import Any

from memory_net.distiller.llm.openai_compat import _usage_of


class _Details:
    def __init__(self, **fields: Any) -> None:
        for name, value in fields.items():
            setattr(self, name, value)


class _Usage:
    def __init__(self, prompt: Any = 11, completion: Any = 22, **fields: Any) -> None:
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        for name, value in fields.items():
            setattr(self, name, value)


class _Completion:
    def __init__(self, usage: Any) -> None:
        self.usage = usage


def test_a_backend_that_reports_no_cache_leaves_the_keys_absent() -> None:
    usage, source = _usage_of(_Completion(_Usage()), "text")

    assert source == "reported"
    assert usage == {"input_tokens": 11, "output_tokens": 22}


def test_the_openai_shaped_details_object_is_read() -> None:
    completion = _Completion(
        _Usage(prompt_tokens_details=_Details(cached_tokens=7, cache_creation_tokens=3))
    )

    usage, _ = _usage_of(completion, "text")

    assert usage["cache_read_input_tokens"] == 7
    assert usage["cache_creation_input_tokens"] == 3


def test_flat_vendor_neutral_counters_are_read_too() -> None:
    completion = _Completion(
        _Usage(cache_read_input_tokens=5, cache_creation_input_tokens=1)
    )

    usage, _ = _usage_of(completion, "text")

    assert usage["cache_read_input_tokens"] == 5
    assert usage["cache_creation_input_tokens"] == 1


def test_a_zero_report_is_kept_and_is_not_the_same_as_silence() -> None:
    reported, _ = _usage_of(_Completion(_Usage(cache_read_input_tokens=0)), "text")
    silent, _ = _usage_of(_Completion(_Usage()), "text")

    assert reported["cache_read_input_tokens"] == 0
    assert "cache_read_input_tokens" not in silent


def test_a_malformed_cache_counter_is_treated_as_not_reported() -> None:
    completion = _Completion(
        _Usage(prompt_tokens_details=_Details(cached_tokens=-4)),
    )
    other = _Completion(_Usage(cache_read_input_tokens=True))

    assert "cache_read_input_tokens" not in _usage_of(completion, "text")[0]
    assert "cache_read_input_tokens" not in _usage_of(other, "text")[0]


def test_an_estimated_usage_never_carries_cache_counters() -> None:
    # No usage object at all: the counts are a local estimate, and a cache
    # figure attached to an estimate would be a fabrication.
    usage, source = _usage_of(_Completion(None), "12345678")

    assert source == "estimated"
    assert usage == {"input_tokens": 0, "output_tokens": 2}
