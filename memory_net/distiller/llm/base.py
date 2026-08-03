from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class LLMRequest:
    system_prompt: str
    user_prompt: str
    model: str
    max_tokens: int = 2000
    metadata: dict[str, Any] = field(default_factory=dict)


USAGE_SOURCES = ("reported", "estimated", "unknown")
"""Fixed value domain of ``LLMResponse.usage_source``.

``reported`` = token counts returned by the backend itself; ``estimated`` = counted
locally (e.g. word counts); ``unknown`` = no basis to tell. Run-level aggregates add
``none`` / ``mixed``, but those are never per-call values.
"""


@dataclass(frozen=True)
class LLMResponse:
    """One LLM call's result plus the provenance of its token accounting.

    Provenance lives in dedicated top-level fields rather than inside ``usage``:
    ``usage`` is an ``int``-valued mapping (consumers coerce every value with
    ``int(v)``), so a string such as ``"estimated"`` in there would raise at the
    consumer. All provenance fields default, so existing construction sites stay
    valid; they are carried verbatim through record/replay and only aggregated at
    the run level.
    """

    text: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)
    usage_source: str = "unknown"
    provider_backend: str = "unknown"
    # String scalars only (model version, finish reason, ...). Never the raw
    # response body: that would create another unscanned surface reaching disk.
    raw_meta: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.usage_source not in USAGE_SOURCES:
            raise ValueError(
                f"usage_source must be one of {USAGE_SOURCES}, got {self.usage_source!r}"
            )
        if not self.provider_backend:
            raise ValueError("provider_backend must be a non-empty label")
        for key, value in self.raw_meta.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise TypeError(
                    "raw_meta accepts string keys and string values only, "
                    f"got {key!r}: {value!r}"
                )


class LLMProvider(ABC):
    @abstractmethod
    def generate(self, request: LLMRequest) -> LLMResponse:
        raise NotImplementedError
