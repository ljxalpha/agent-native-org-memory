"""Deciding whether an agent actually consumed an injected team hint (§9.13).

Pure functions over evidence that has already been gathered. Nothing here runs
an agent, reads the environment or touches a network -- which is what lets the
decision rules be tested exhaustively against recorded evidence, at no cost,
including the failure cases a real run would be expensive to provoke.

**Why the sentinel is the primary evidence, not the session trace.** The
original plan read the tool set and the tool-call trace out of
``${HERMES_HOME}/sessions/<id>.jsonl``. Measured on this machine, a *successful*
oneshot writes no session file at all -- the file appears only on failure, and
then as a ``request_dump_*.json`` carrying a partially masked key. So a trace is
not something a passing run can be required to produce.

What a passing run does produce is its answer. A sentinel is sixteen random
characters that exist in exactly one place: the injected ``INDEX.md``. It is not
in the prompt and not in the instruction file. A model that emits it has read
that file, and a control run with the hint removed must not emit it. That pair
is a different statement from a tool call, and a stronger one for the question
that matters: a trace shows a file was opened, while the sentinel shows its
contents reached the model.

It is worth being exact about what it does *not* show. A sentinel that comes
back cannot distinguish the agent going and reading the file from the framework
inlining the file the hint referenced. Both are consumption of the hint, and
both answer "will the team memory reach this agent" -- neither answers "does
the agent use a tool to fetch it". The verdict says so in a note rather than
leaving the reader to assume the stronger reading.

A trace is still checked when one exists, because it can say things the answer
cannot -- which tools were offered, and whether any write was attempted.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

SENTINEL_PATTERN = re.compile(r"^[a-z0-9]{16}$")
"""Sixteen lowercase alphanumerics. Long enough not to be guessed, neutral
enough to carry no meaning if it ever appears in a log."""

WRITE_TOOLS = ("write_file", "patch")
"""Tools whose use during a read-only verification is an escalation.

The toolset the check needs includes these, so the namespace mounts the agent
root read-only and this is the second line rather than the first.
"""

FAILURE_RESIDUE = "request_dump_"
"""Prefix of the file a failed agent run leaves behind.

It contains a partially masked key, so its presence is a hard failure: the
evidence directory is read, quoted and filed, and this must never be in it.
"""


@dataclass(frozen=True)
class ConsumptionEvidence:
    """Everything gathered around one verification, positive and control."""

    sentinel: str
    #: The agent's answer with the hint installed.
    positive_output: str
    #: The agent's answer with the hint absent and everything else identical.
    control_output: str
    #: The prompt handed to both runs; the sentinel must not be in it.
    prompt: str
    #: The instruction file's contents; the sentinel must not be in it either.
    instructions: str
    #: Contents of the injected index, which is the sentinel's only home.
    index_content: str
    #: Session files present before and after the positive run.
    sessions_before: Sequence[str] = ()
    sessions_after: Sequence[str] = ()
    #: JSONL lines of this run's session trace, when one was written.
    trace_lines: Sequence[str] = ()
    #: Files found in the agent's state directory after the run.
    residue: Sequence[str] = ()
    #: Tool function names the check expected to be offered.
    expected_tools: Sequence[str] = ()
    #: The canonical path the index was written to.
    index_path: str = ""
    inlining_ruled_out: str = ""
    """Why the framework *cannot* have inlined the file the hint references.

    Empty by default, which is the conservative reading: a sentinel that comes
    back proves the index's contents reached the model, and if the framework
    expands the reference while assembling the system prompt then no tool was
    used and nothing was followed.

    Set only from evidence about the agent under test -- a source-level fact
    about how it loads its instruction file, or a control run with the tools
    removed. A reason string rather than a boolean, because the verdict has to
    record *why* the claim holds; the stronger conclusion is worth exactly as
    much as the basis stated here.
    """


@dataclass(frozen=True)
class ConsumptionVerdict:
    consumed: bool
    #: What the verdict rests on: ``sentinel`` alone, or ``sentinel+trace``.
    evidence_basis: str
    failures: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)


def verify_consumption(evidence: ConsumptionEvidence) -> ConsumptionVerdict:
    """Decide, and say what the decision rests on.

    ``consumed`` is never true on partial evidence. Each rule below exists
    because without it a specific wrong conclusion becomes reachable, and the
    rule names that conclusion.
    """
    failures: list[str] = []
    notes: list[str] = []

    if not SENTINEL_PATTERN.fullmatch(evidence.sentinel):
        failures.append(f"sentinel is not {SENTINEL_PATTERN.pattern}")

    # The sentinel must be reachable *only* by reading the index. If it is in
    # the prompt, the model is repeating its input; if it is in the instruction
    # file, that file is inlined into the system prompt and nothing was
    # followed.
    if evidence.sentinel in evidence.prompt:
        failures.append("sentinel appears in the prompt")
    if evidence.sentinel in evidence.instructions:
        failures.append("sentinel appears in the instruction file")
    if evidence.sentinel not in evidence.index_content:
        failures.append("sentinel is not in the index that was written")

    if evidence.sentinel not in evidence.positive_output:
        failures.append("sentinel absent from the agent's answer")
    # The control is what separates "followed the reference" from "found the
    # file some other way". It must differ from the positive run in the hint
    # alone -- the index stays, carrying the same sentinel -- because a control
    # that also removed the index would leave an agent that searched the tree
    # producing the passing pattern without ever following anything.
    if evidence.sentinel in evidence.control_output:
        failures.append("sentinel present in the control run, which had no hint")

    # Matched on the last path component: the dump's directory varies with the
    # profile in use, and a residue one level down is the same key on disk.
    residue = [
        name
        for name in evidence.residue
        if name.rsplit("/", 1)[-1].startswith(FAILURE_RESIDUE)
    ]
    if residue:
        failures.append(f"failure residue left behind: {sorted(residue)}")

    basis = "sentinel"
    if evidence.trace_lines:
        basis = "sentinel+trace"
        failures.extend(_trace_failures(evidence))
    else:
        # Stated rather than passed over: a reader must not take the absence of
        # a trace for a trace that was checked and found clean.
        notes.append(
            "no session trace was written; a successful oneshot does not "
            "produce one, so the tool set and call sequence are unverified"
        )
        # The two are worth separating, because only the second is a claim
        # about tool use. Which one an integrator needs depends on what they
        # are asking: "will the agent see the team memory" is answered by the
        # first; "does the agent go and read it" is not.
        if evidence.inlining_ruled_out:
            basis = "sentinel+no-inlining"
            notes.append(
                "the sentinel shows the index's contents reached the model, "
                "and inlining is ruled out, so a tool read is the only "
                f"remaining path: {evidence.inlining_ruled_out}"
            )
        else:
            notes.append(
                "the sentinel shows the index's contents reached the model; on "
                "this basis alone it does not distinguish an active tool read "
                "from the framework inlining the referenced file"
            )

    new_sessions = sorted(set(evidence.sessions_after) - set(evidence.sessions_before))
    if evidence.trace_lines and len(new_sessions) != 1:
        failures.append(
            f"expected exactly one new session file, found {len(new_sessions)}"
        )

    return ConsumptionVerdict(
        consumed=not failures,
        evidence_basis=basis,
        failures=tuple(failures),
        notes=tuple(notes),
    )


def _trace_failures(evidence: ConsumptionEvidence) -> list[str]:
    """The checks a trace makes possible, when one exists."""
    failures: list[str] = []
    try:
        records = [json.loads(line) for line in evidence.trace_lines if line.strip()]
    except json.JSONDecodeError as exc:
        return [f"session trace is not JSONL: {exc}"]
    if not records:
        return ["session trace is empty"]

    offered = tool_names(records[0])
    if evidence.expected_tools and offered != set(evidence.expected_tools):
        failures.append(
            f"tools offered {sorted(offered)} != expected "
            f"{sorted(evidence.expected_tools)}"
        )

    calls = list(tool_calls(records))
    written = [name for name, _ in calls if name in WRITE_TOOLS]
    if written:
        failures.append(f"write tools were called: {sorted(set(written))}")

    if evidence.index_path:
        read_paths = [
            arguments.get("path") or arguments.get("file_path")
            for name, arguments in calls
            if name == "read_file"
        ]
        if evidence.index_path not in read_paths:
            failures.append(f"no read_file of {evidence.index_path}")

    # "The model said it read the file" versus "the file's bytes came back":
    # only a tool-result line shows the second.
    returned = "\n".join(
        str(record.get("content") or "")
        for record in records
        if record.get("role") == "tool"
    )
    if evidence.sentinel not in returned:
        failures.append("sentinel not present in any tool result")
    return failures


def tool_names(header: Mapping[str, Any]) -> set[str]:
    """The function names offered to the model, from the session header."""
    names: set[str] = set()
    for tool in header.get("tools") or []:
        if isinstance(tool, Mapping):
            function = tool.get("function")
            if isinstance(function, Mapping) and function.get("name"):
                names.add(str(function["name"]))
    return names


def tool_calls(
    records: Sequence[Mapping[str, Any]],
) -> list[tuple[str, dict[str, Any]]]:
    """``(name, arguments)`` for every tool call in a session trace."""
    calls: list[tuple[str, dict[str, Any]]] = []
    for record in records:
        for call in record.get("tool_calls") or []:
            if not isinstance(call, Mapping):
                continue
            function = call.get("function")
            if not isinstance(function, Mapping):
                continue
            raw = function.get("arguments")
            if isinstance(raw, str):
                try:
                    arguments = json.loads(raw)
                except json.JSONDecodeError:
                    arguments = {}
            elif isinstance(raw, Mapping):
                arguments = dict(raw)
            else:
                arguments = {}
            calls.append((str(function.get("name") or ""), arguments))
    return calls
