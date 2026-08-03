"""Out-of-process agent bridge — an EXPERIMENTAL integration entry point.

This provider is **not** the production distillation backend and is **not** the
baseline for product-equivalence checks. Production distillation runs through
``--provider deepseek`` (``OpenAICompatProvider``); see §5.3.2 of
``mvp_tech_design.md``.

The reason is the channel, not maturity: the bridge hands ``system_prompt`` and
``user_prompt`` to a CLI that accepts a single text prompt, so the system/user
role boundary does not exist on the wire, and there is no service-level
structured-output constraint. A channel with neither property must not be the
one that feeds untrusted leaf records to a model. What this provider *does*
prove is that the ``LLMProvider`` abstraction can host an out-of-process backend.

Crossing a process boundary still requires a minimum production contract, which
is implemented below: a wall-clock timeout, process-*group* teardown so the
agent's own grandchildren cannot outlive us, and typed errors that carry both
streams instead of leaking a raw ``JSONDecodeError``.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess

from memory_net.distiller.llm.base import (
    USAGE_SOURCES,
    LLMProvider,
    LLMRequest,
    LLMResponse,
)

DEFAULT_PROVIDER_BACKEND = "hermes_tool"

DEFAULT_TIMEOUT_S = 300.0
"""Wall-clock ceiling for one bridge call.

Without one a wedged agent hangs the distillation forever; the whole run then
holds its snapshot worktree and its budget open with no way to progress.
"""

_TEARDOWN_GRACE_S = 5.0
"""How long the group gets to exit on SIGTERM before SIGKILL."""

_STREAM_EXCERPT_CHARS = 200
"""Bound on each stream excerpt in an error message.

A real agent's stdout can be megabytes; an unbounded excerpt would push the
whole transcript into logs, which is both unreadable and an extra leak surface.
"""


class HermesToolProvider(LLMProvider):
    def __init__(
        self,
        *,
        command: str | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.command = command or os.environ.get("HERMES_TOOL_COMMAND")
        self.timeout_s = timeout_s

    def generate(self, request: LLMRequest) -> LLMResponse:
        if not self.command:
            raise RuntimeError("HERMES_TOOL_COMMAND is required for HermesToolProvider")
        stdout, stderr = self._run_bridge(
            json.dumps(
                {
                    "system_prompt": request.system_prompt,
                    "user_prompt": request.user_prompt,
                    "model": request.model,
                    "max_tokens": request.max_tokens,
                    "metadata": request.metadata,
                },
                ensure_ascii=False,
            )
        )
        mapping = _decode_response(stdout, stderr)
        usage = mapping.get("usage")
        return LLMResponse(
            text=str(mapping.get("text", "")),
            model=str(mapping.get("model") or request.model),
            usage=(
                {str(k): int(v) for k, v in usage.items()}
                if isinstance(usage, dict)
                else {}
            ),
            usage_source=_usage_source(mapping.get("usage_source")),
            provider_backend=_provider_backend(mapping.get("provider_backend")),
            raw_meta=_raw_meta(mapping.get("raw_meta")),
        )

    def _run_bridge(self, payload: str) -> tuple[str, str]:
        """Run the bridge to completion, or tear down its whole process group."""
        proc = subprocess.Popen(
            shlex.split(self.command or ""),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            # New session => the bridge and everything it spawns share one
            # process group id that is ours to signal, and no signal we send can
            # reach the distiller's own process group.
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(payload, timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            stderr = _teardown(proc)
            raise RuntimeError(
                f"hermes tool timed out after {self.timeout_s}s"
                f"{_excerpt('stderr', stderr)}"
            ) from None
        except BaseException:
            # Any other exit from this frame (KeyboardInterrupt, a cancelled
            # run) must not leave the agent running either.
            _teardown(proc)
            raise
        if proc.returncode != 0:
            raise RuntimeError(f"hermes tool failed: {stderr.strip()}")
        return stdout, stderr


def _teardown(proc: subprocess.Popen[str]) -> str:
    """Signal the bridge's whole process group, then drain what it wrote.

    Signalling only ``proc`` is not enough: an agent CLI spawns children of its
    own, and those grandchildren survive their parent's death (they are
    reparented to init), keeping a model session — and its quota burn — alive
    after we have already given up.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        pgid = None
    if pgid is not None:
        _signal_group(pgid, signal.SIGTERM)
        _wait_briefly(proc)
        # Unconditional, not "only if proc is still alive": the bridge may exit
        # on SIGTERM while a grandchild ignores it, and that grandchild is
        # exactly what this teardown exists to reap.
        _signal_group(pgid, signal.SIGKILL)
    _wait_briefly(proc)
    try:
        _, stderr = proc.communicate(timeout=_TEARDOWN_GRACE_S)
    except (subprocess.TimeoutExpired, ValueError):
        return ""
    return stderr or ""


def _signal_group(pgid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        return


def _wait_briefly(proc: subprocess.Popen[str]) -> None:
    try:
        proc.wait(timeout=_TEARDOWN_GRACE_S)
    except subprocess.TimeoutExpired:
        return


def _decode_response(stdout: str, stderr: str) -> dict[str, object]:
    """Parse the bridge's stdout, or fail with both streams in the message.

    A real agent that prints one extra line makes ``json.loads`` raise; letting
    that bare ``JSONDecodeError`` escape would both violate this provider's
    declared error type and drop the evidence needed to see what it printed.
    """
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"hermes tool returned non-JSON stdout: {exc}"
            f"{_excerpt('stdout', stdout)}{_excerpt('stderr', stderr)}"
        ) from exc
    if not isinstance(payload, dict):
        # The suppressed TRY004 is deliberate: RuntimeError is load-bearing
        # here. This guard validates a *subprocess's* output, and the declared
        # failure mode for a misbehaving bridge is RuntimeError; a TypeError
        # would escape the callers that catch provider failures.
        raise RuntimeError(  # noqa: TRY004
            "hermes tool response must be a JSON object"
            f"{_excerpt('stdout', stdout)}{_excerpt('stderr', stderr)}"
        )
    return payload


def _excerpt(label: str, text: str) -> str:
    """A bounded, labelled slice of one stream; empty streams add nothing."""
    stripped = (text or "").strip()
    if not stripped:
        return ""
    if label == "stderr":
        # Failures announce themselves at the end of stderr, not the start.
        body = stripped[-_STREAM_EXCERPT_CHARS:]
    else:
        body = stripped[:_STREAM_EXCERPT_CHARS]
    return f"; {label}={body!r}"


def _usage_source(value: object) -> str:
    """Honour only a declared, in-domain label.

    The bridge is a relay: it cannot assert on the downstream backend's behalf
    that the counts came from a service, so an absent or unrecognised label
    degrades to ``unknown`` rather than optimistically to ``reported``.
    """
    return value if isinstance(value, str) and value in USAGE_SOURCES else "unknown"


def _provider_backend(value: object) -> str:
    """The bridge may name the backend it actually relayed to; else it is us."""
    if isinstance(value, str) and value:
        return value
    return DEFAULT_PROVIDER_BACKEND


def _raw_meta(value: object) -> dict[str, str]:
    """String scalars only — anything else is dropped, never coerced."""
    if not isinstance(value, dict):
        return {}
    return {
        key: item
        for key, item in value.items()
        if isinstance(key, str) and isinstance(item, str)
    }
