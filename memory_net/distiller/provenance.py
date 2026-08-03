"""Run-level aggregation of per-call LLM provenance (§9.5 of the design doc).

Pure functions, no IO. Per-call provenance stays verbatim in the recorded
``calls.jsonl``; this module only derives the run-level view that lands in
``metrics.json`` and the bundle's ``record_metrics.json``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from memory_net.distiller.llm.base import USAGE_SOURCES, LLMResponse

RUN_USAGE_SOURCES = ("none", *USAGE_SOURCES, "mixed")
"""Value domain of the run-level ``usage_source`` scalar.

Extends the per-call domain with ``none`` (no call happened) and ``mixed`` (calls
disagreed) — neither is ever a per-call value.
"""


@dataclass(frozen=True)
class RunProvenance:
    """Where one run's token accounting came from.

    ``provider_backends`` is deliberately a list and has no scalar counterpart: a
    scalar could only encode a mixed run as ``"mixed"``, which is lossy and
    invites reading a partially-estimated run as fully trustworthy.
    """

    provider_backends: list[str] = field(default_factory=list)
    usage_source: str = "none"
    usage_source_counts: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(USAGE_SOURCES, 0)
    )

    @classmethod
    def empty(cls) -> RunProvenance:
        """The zero-call run, i.e. ``aggregate([])``."""
        return cls()


def aggregate(responses: Iterable[LLMResponse]) -> RunProvenance:
    """Fold every call of a run — both the primary and the fallback lane.

    A run mixing lanes is exactly why the rule is fixed here rather than left to
    each call site: ``usage_source`` collapses to the single shared value when
    the calls agree, to ``"mixed"`` when they do not, and ``usage_source_counts``
    always carries all three per-call keys so a ``"mixed"`` run stays auditable.
    """
    backends: set[str] = set()
    counts = dict.fromkeys(USAGE_SOURCES, 0)
    for response in responses:
        backends.add(response.provider_backend)
        counts[response.usage_source] += 1

    observed = {source for source, count in counts.items() if count > 0}
    if not observed:
        usage_source = "none"
    elif len(observed) == 1:
        usage_source = observed.pop()
    else:
        usage_source = "mixed"

    return RunProvenance(
        provider_backends=sorted(backends),
        usage_source=usage_source,
        usage_source_counts=counts,
    )
