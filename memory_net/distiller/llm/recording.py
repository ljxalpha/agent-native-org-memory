from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
from typing import Any

from memory_net.distiller.llm.base import LLMProvider, LLMRequest, LLMResponse
from memory_net.distiller.output_scanner import (
    PROSE,
    OutputScanner,
    RedactionViolation,
    ScanVerdict,
)
from memory_net.distiller.runctx import RunContext
from memory_net.shared.util.hashing import short_hash

UNSCANNED = ScanVerdict(hits=(), ruleset_version="", mode="none")
"""The verdict for a run that has no scanner configured at all.

Stated rather than omitted: ``BundleRecorder.record`` demands a verdict, and
"there was no scanner" has to be something a caller can say out loud instead of
a missing argument that reads the same as a forgotten scan.
"""


class BundleRecorder:
    """Shared sink for one RECORD-001 run's recorded LLM calls.

    Owns the consolidated append-only ``calls.jsonl`` plus the per-call
    ``requests/<NNNN>-<prompt_hash>.json`` files and a single monotonic sequence.
    A run's primary and fallback providers are wrapped by separate
    ``RecordingProvider`` instances that share one ``BundleRecorder``, so their
    calls land in the same ordered log and never collide on a request filename —
    a cluster's primary and fallback reuse the same prompt, hence the same
    ``prompt_hash``, and only the shared sequence keeps the names distinct.
    """

    def __init__(self, path: Path, *, requests_dir: Path | None = None) -> None:
        self.path = path
        # When set (run-scoped bundle), every call is also written as its own JSON
        # file under requests_dir, alongside the consolidated JSONL at ``path``.
        # Left None for the flat --record file.
        self.requests_dir = requests_dir
        self._sequence = 0

    def record(
        self, request: LLMRequest, response: LLMResponse, *, scanned: ScanVerdict
    ) -> None:
        """Append one call, given evidence that it was scanned first (§5.5.10).

        ``scanned`` is required so this write point asks for the evidence rather
        than trusting that some caller upstream remembered to scan. A verdict
        that carries hits under ``enforce`` is refused outright: a caller that
        scanned, was told no, and wrote anyway is the bypass this guards.
        """
        if scanned.hits and scanned.mode == "enforce":
            raise RedactionViolation(scanned.hits)
        record = _record_item(request, response)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        if self.requests_dir is not None:
            self._sequence += 1
            self.requests_dir.mkdir(parents=True, exist_ok=True)
            name = f"{self._sequence:04d}-{record['prompt_hash']}.json"
            (self.requests_dir / name).write_text(
                json.dumps(
                    {**record, "sequence": self._sequence},
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )


class RecordingProvider(LLMProvider):
    """Records a call, and scans in two stages around it (§5.5.10).

    The stages are not symmetric on purpose. The request is scanned *before the
    inner provider is called at all*: once a prompt has gone out, refusing to
    write it down protects nothing. The response can only be scanned after the
    call, so there the guarantee is narrower -- it never reaches disk.
    """

    def __init__(
        self,
        provider: LLMProvider,
        path: Path | None = None,
        *,
        requests_dir: Path | None = None,
        recorder: BundleRecorder | None = None,
        context: RunContext | None = None,
    ) -> None:
        self.provider = provider
        if recorder is None:
            if path is None:
                raise ValueError("RecordingProvider requires a path or a recorder")
            recorder = BundleRecorder(path, requests_dir=requests_dir)
        # Shared so a run's primary and fallback providers record to one bundle.
        self.recorder = recorder
        # The run's scanner is read off the context, never constructed here: the
        # recorder, the pipeline and the orchestrator must share one instance,
        # which is also the run's hit counter.
        self.context = context

    @property
    def scanner(self) -> OutputScanner | None:
        scanner = self.context.scanner if self.context is not None else None
        return scanner if isinstance(scanner, OutputScanner) else None

    def generate(self, request: LLMRequest) -> LLMResponse:
        scanner = self.scanner
        if scanner is None:
            # Pre-injection behaviour, field for field. The verdict is explicit
            # rather than absent so the write point's contract stays "show me
            # what the scan said", with "there was no scanner" a statable answer.
            response = self.provider.generate(request)
            self.recorder.record(request, response, scanned=UNSCANNED)
            return response
        # Stage 1, before anything leaves this process.
        scanner.check(
            request.system_prompt, field_kind=PROSE, field_path="request.system_prompt"
        )
        scanner.check(
            request.user_prompt, field_kind=PROSE, field_path="request.user_prompt"
        )
        scanner.check(
            dict(request.metadata or {}),
            field_kind=PROSE,
            field_path="request.metadata",
        )
        response = self.provider.generate(request)
        # Stage 2, before anything reaches disk.
        text_verdict = scanner.check(
            response.text, field_kind=PROSE, field_path="response.text"
        )
        meta_verdict = scanner.check(
            dict(response.raw_meta or {}),
            field_kind=PROSE,
            field_path="response.raw_meta",
        )
        self.recorder.record(
            request,
            response,
            scanned=ScanVerdict(
                hits=text_verdict.hits + meta_verdict.hits,
                ruleset_version=scanner.ruleset.version,
                mode=scanner.mode,
            ),
        )
        return response


class ReplayProvider(LLMProvider):
    def __init__(
        self,
        path: Path,
        *,
        strict: bool = True,
        mode: str | None = None,
        fallback_provider: LLMProvider | None = None,
        source: _ReplaySource | None = None,
    ) -> None:
        # Accept either a flat JSONL file or a RECORD-001 bundle directory
        # (in which case the consolidated calls.jsonl inside it is replayed).
        self.path = path / "calls.jsonl" if path.is_dir() else path
        if mode is None:
            mode = "strict" if strict else "prompt-only"
        if mode not in {"strict", "prompt-only", "allow-live-on-miss"}:
            raise ValueError(f"unsupported replay mode: {mode}")
        self.mode = mode
        self.fallback_provider = fallback_provider
        # A run's primary and fallback lanes share one source so a recorded
        # primary->fallback sequence replays in order, while each lane keeps its
        # own live-on-miss provider (see sharing_cursor).
        self._source = source or _ReplaySource.load(
            self.path, strict_order=(mode == "strict")
        )

    def sharing_cursor(
        self, *, fallback_provider: LLMProvider | None
    ) -> ReplayProvider:
        """A second ReplayProvider over the same cursor with its own live-on-miss.

        Lets a run's fallback lane consume the shared ordered cursor while using
        the configured fallback provider for allow-live-on-miss, instead of the
        primary lane's live provider.
        """
        return ReplayProvider(
            self.path,
            mode=self.mode,
            fallback_provider=fallback_provider,
            source=self._source,
        )

    def generate(self, request: LLMRequest) -> LLMResponse:
        prompt_hash = _prompt_hash(request)
        item = self._source.take(prompt_hash)
        if item is None:
            if self.mode == "allow-live-on-miss" and self.fallback_provider is not None:
                response = self.fallback_provider.generate(request)
                usage = dict(response.usage)
                usage["replay_misses"] = int(usage.get("replay_misses", 0)) + 1
                # A live-on-miss call really did happen, so its own provenance
                # is the truthful one to carry forward.
                return LLMResponse(
                    text=response.text,
                    model=response.model,
                    usage=usage,
                    usage_source=response.usage_source,
                    provider_backend=response.provider_backend,
                    raw_meta=response.raw_meta,
                )
            if self.mode == "strict":
                raise RuntimeError("replay record exhausted")
            raise RuntimeError("replay record not found")
        recorded_request = _mapping(item.get("request"), "request")
        if self.mode == "strict" and _request_tuple(recorded_request) != _request_tuple(
            {
                "system_prompt": request.system_prompt,
                "user_prompt": request.user_prompt,
                "model": request.model,
                "max_tokens": request.max_tokens,
            }
        ):
            raise RuntimeError("replay request mismatch")
        if (
            self.mode == "prompt-only"
            and str(item.get("prompt_hash") or recorded_request.get("prompt_hash"))
            != prompt_hash
        ):
            raise RuntimeError("replay request mismatch")
        recorded_response = _mapping(item.get("response"), "response")
        usage = _usage(recorded_response.get("usage"))
        usage["replay_hits"] = int(usage.get("replay_hits", 0)) + 1
        # Provenance is replayed exactly as recorded — never rewritten to
        # "replay". The fact that this run is a replay is carried by the
        # orthogonal replay_mode / replay_hits / replay_misses counters.
        return LLMResponse(
            text=str(recorded_response["text"]),
            model=str(recorded_response["model"]),
            usage=usage,
            usage_source=_provenance_str(recorded_response.get("usage_source")),
            provider_backend=_provenance_str(recorded_response.get("provider_backend")),
            raw_meta=_raw_meta(recorded_response.get("raw_meta")),
        )


class _ReplaySource:
    """Ordered cursor over one recorded bundle's calls, shared by a run's lanes.

    A run's primary and fallback ReplayProviders share one ``_ReplaySource`` so
    the recorded primary->fallback order replays correctly (strict by offset,
    prompt-only by per-hash candidate queue), while each lane keeps its own
    live-on-miss provider.
    """

    def __init__(self, items: list[dict[str, Any]], *, strict_order: bool) -> None:
        self._items = items
        self._strict_order = strict_order
        self._offset = 0
        self._by_prompt_hash: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            request = _mapping(item.get("request"), "request")
            prompt_hash = str(item.get("prompt_hash") or request.get("prompt_hash"))
            self._by_prompt_hash.setdefault(prompt_hash, []).append(item)

    @classmethod
    def load(cls, path: Path, *, strict_order: bool) -> _ReplaySource:
        return cls(_load_items(path), strict_order=strict_order)

    def take(self, prompt_hash: str) -> dict[str, Any] | None:
        if self._strict_order:
            if self._offset >= len(self._items):
                return None
            item = self._items[self._offset]
            self._offset += 1
            return item
        candidates = self._by_prompt_hash.get(prompt_hash) or []
        if not candidates:
            return None
        return candidates.pop(0)


def _record_item(request: LLMRequest, response: LLMResponse) -> dict[str, object]:
    prompt_hash = _prompt_hash(request)
    return {
        "prompt_hash": prompt_hash,
        "request": {
            "system_prompt": request.system_prompt,
            "user_prompt": request.user_prompt,
            "prompt_hash": prompt_hash,
            "model": request.model,
            "max_tokens": request.max_tokens,
            "metadata": request.metadata,
        },
        "response": {
            "text": response.text,
            "model": response.model,
            "usage": response.usage,
            # Per-call provenance is stored verbatim and never aggregated here:
            # calls.jsonl is the single source of truth the run-level aggregate
            # is derived from.
            "usage_source": response.usage_source,
            "provider_backend": response.provider_backend,
            "raw_meta": response.raw_meta,
        },
    }


def _load_items(path: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            item = json.loads(line)
            if isinstance(item, dict):
                items.append(item)
    return items


def _prompt_hash(request: LLMRequest) -> str:
    return short_hash(
        json.dumps(
            {
                "system_prompt": request.system_prompt,
                "user_prompt": request.user_prompt,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        16,
    )


def _request_tuple(value: dict[str, object]) -> tuple[object, object, object, object]:
    return (
        value.get("system_prompt"),
        value.get("user_prompt"),
        value.get("model"),
        value.get("max_tokens"),
    )


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RuntimeError(f"replay {label} must be an object")
    return value


def _provenance_str(value: object) -> str:
    """Read one provenance label, falling back for bundles recorded before them.

    A bundle predating the provenance contract simply has no such key; failing
    on it would turn every historical bundle red, so a missing value degrades to
    ``"unknown"``. A present-but-invalid value is not degraded — ``LLMResponse``
    validates it and raises.
    """
    if value is None:
        return "unknown"
    if not isinstance(value, str):
        raise TypeError(f"replay provenance label must be a string, got {value!r}")
    return value


def _raw_meta(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TypeError(f"replay raw_meta must be an object, got {value!r}")
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise TypeError(
                f"replay raw_meta accepts string scalars only, got {key!r}: {item!r}"
            )
    return {str(key): str(item) for key, item in value.items()}


def _usage(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {str(key): int(item) for key, item in value.items()}
