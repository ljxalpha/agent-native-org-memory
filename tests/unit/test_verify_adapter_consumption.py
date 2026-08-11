"""The consumption check's decision rules and argv contract (§9.13).

No agent runs here. The decision rules are pure functions over recorded
evidence, which is what makes it possible to test the *failure* cases at all --
each of them would otherwise cost a real, billed run to provoke, and some of
them (a leaking sentinel, an attempted write) cannot be provoked on demand.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from memory_net.distiller.agent_trace import (
    SENTINEL_PATTERN,
    ConsumptionEvidence,
    verify_consumption,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "verify_adapter_consumption"

SENTINEL = "a1b2c3d4e5f6a7b8"
INDEX_PATH = "/work/agentroot/memory/team/INDEX.md"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "verify_adapter_consumption",
        SCRIPT,
        loader=importlib.machinery.SourceFileLoader(
            "verify_adapter_consumption", str(SCRIPT)
        ),
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


verifier = _load()
SOURCE = SCRIPT.read_text(encoding="utf-8")


def _evidence(**overrides: object) -> ConsumptionEvidence:
    base = {
        "sentinel": SENTINEL,
        "positive_output": f"The token is {SENTINEL}.",
        "control_output": "I could not find a team memory index.",
        "prompt": verifier.PROMPT,
        "instructions": "# Agent instructions\n\n@memory/team/INDEX.md\n",
        "index_content": f"Verification token: {SENTINEL}\n",
        "index_path": INDEX_PATH,
    }
    base.update(overrides)
    return ConsumptionEvidence(**base)  # type: ignore[arg-type]


def _trace(**overrides: object) -> list[str]:
    header = {
        "role": "session",
        "model": "m",
        "platform": "cli",
        "timestamp": "2026-08-11T00:00:00Z",
        "tools": [
            {"function": {"name": name}}
            for name in overrides.get("tools", verifier.FILE_TOOLSET)  # type: ignore[union-attr]
        ],
    }
    assistant = {
        "role": "assistant",
        "tool_calls": [
            {
                "function": {
                    "name": str(overrides.get("call", "read_file")),
                    "arguments": json.dumps({"path": INDEX_PATH}),
                }
            }
        ],
    }
    result = {
        "role": "tool",
        "content": str(overrides.get("result", f"Verification token: {SENTINEL}")),
    }
    return [json.dumps(item) for item in (header, assistant, result)]


# -- the sentinel lane, which is the one a passing run actually has ------


def test_a_sentinel_that_comes_back_and_a_control_that_does_not_is_consumption() -> (
    None
):
    verdict = verify_consumption(_evidence())

    assert verdict.consumed
    assert verdict.evidence_basis == "sentinel"


def test_a_run_without_a_trace_says_what_it_could_not_check() -> None:
    """A successful oneshot writes no session file on this platform, so the
    absence of a trace is the normal case -- and must not read as "the tool set
    was checked and was fine"."""
    verdict = verify_consumption(_evidence())

    assert any("unverified" in note for note in verdict.notes)


def test_a_sentinel_missing_from_the_answer_is_not_consumption() -> None:
    verdict = verify_consumption(_evidence(positive_output="I have no token."))

    assert not verdict.consumed
    assert "sentinel absent from the agent's answer" in verdict.failures


def test_a_control_run_that_produces_the_sentinel_fails_the_whole_check() -> None:
    """The clause that separates "read the file" from "reached it some other
    way we did not think of"."""
    verdict = verify_consumption(_evidence(control_output=f"The token is {SENTINEL}."))

    assert not verdict.consumed
    assert any("control run" in item for item in verdict.failures)


@pytest.mark.parametrize("field", ["prompt", "instructions"])
def test_a_sentinel_reachable_without_the_index_fails(field: str) -> None:
    """In the prompt, the model is repeating its input. In the instruction
    file, that file is inlined into the system prompt and nothing was
    followed."""
    verdict = verify_consumption(_evidence(**{field: f"token {SENTINEL}"}))

    assert not verdict.consumed


def test_a_sentinel_not_in_the_index_means_the_check_wrote_the_wrong_file() -> None:
    verdict = verify_consumption(_evidence(index_content="Verification token: \n"))

    assert not verdict.consumed
    assert "sentinel is not in the index that was written" in verdict.failures


def test_a_fixed_sentinel_shape_is_required() -> None:
    verdict = verify_consumption(_evidence(sentinel="short"))

    assert not verdict.consumed


def test_failure_residue_fails_the_check() -> None:
    """A failed agent run leaves a request dump holding a partially masked key,
    and the evidence directory is read, quoted and filed. Nested, because the
    dump's directory varies with the profile in use."""
    verdict = verify_consumption(
        _evidence(residue=["config.yaml", "sessions/request_dump_1754.json"])
    )

    assert not verdict.consumed
    assert any("residue" in item for item in verdict.failures)


# -- the trace lane, checked when a trace happens to exist ---------------


def test_a_clean_trace_upgrades_the_basis_and_still_passes() -> None:
    verdict = verify_consumption(
        _evidence(
            trace_lines=_trace(),
            expected_tools=verifier.FILE_TOOLSET,
            sessions_before=[],
            sessions_after=["s1.jsonl"],
        )
    )

    assert verdict.consumed
    assert verdict.evidence_basis == "sentinel+trace"


def test_an_unexpected_tool_set_fails() -> None:
    verdict = verify_consumption(
        _evidence(
            trace_lines=_trace(tools=["read_file", "shell"]),
            expected_tools=verifier.FILE_TOOLSET,
            sessions_after=["s1.jsonl"],
        )
    )

    assert not verdict.consumed
    assert any("tools offered" in item for item in verdict.failures)


def test_a_write_call_fails_even_though_the_mount_would_have_stopped_it() -> None:
    verdict = verify_consumption(
        _evidence(
            trace_lines=_trace(call="write_file"),
            expected_tools=verifier.FILE_TOOLSET,
            sessions_after=["s1.jsonl"],
        )
    )

    assert not verdict.consumed
    assert any("write tools" in item for item in verdict.failures)


def test_a_trace_with_no_read_of_the_index_fails() -> None:
    verdict = verify_consumption(
        _evidence(
            trace_lines=_trace(call="search_files"),
            expected_tools=verifier.FILE_TOOLSET,
            sessions_after=["s1.jsonl"],
        )
    )

    assert not verdict.consumed
    assert any("no read_file" in item for item in verdict.failures)


def test_a_tool_result_without_the_sentinel_fails() -> None:
    """ "The model said it read the file" versus "the bytes came back"."""
    verdict = verify_consumption(
        _evidence(
            trace_lines=_trace(result="(file was empty)"),
            expected_tools=verifier.FILE_TOOLSET,
            sessions_after=["s1.jsonl"],
        )
    )

    assert not verdict.consumed
    assert any("tool result" in item for item in verdict.failures)


def test_more_than_one_new_session_file_fails() -> None:
    """Which of them is this run's? The check refuses to guess, because reading
    a previous run's trace is exactly how it would pass without a run."""
    verdict = verify_consumption(
        _evidence(
            trace_lines=_trace(),
            expected_tools=verifier.FILE_TOOLSET,
            sessions_before=["old.jsonl"],
            sessions_after=["old.jsonl", "a.jsonl", "b.jsonl"],
        )
    )

    assert not verdict.consumed
    assert any("exactly one new session" in item for item in verdict.failures)


# -- the argv contract ---------------------------------------------------


def test_the_command_line_never_disables_the_thing_being_verified() -> None:
    """`--safe-mode` implies ignoring the user config and disables instruction
    file and memory injection outright."""
    argv = verifier.sandbox_argv(
        runtime_root=Path("/opt/rt"),
        root=Path("/run/sbx"),
        model="m",
        provider="p",
        hermes="hermes",
    )

    assert "--safe-mode" not in argv
    # An empty toolset argument does the opposite of what it looks like: it
    # falls back to the configured sets, meaning all of them.
    assert "-t" not in argv
    assert argv[argv.index("-m") + 1] == "m"
    assert argv[argv.index("--provider") + 1] == "p"


def test_the_agent_root_is_mounted_read_only() -> None:
    """The toolset this check needs includes write tools, so the limit is
    structural rather than an after-the-fact assertion."""
    argv = verifier.sandbox_argv(
        runtime_root=Path("/opt/rt"),
        root=Path("/run/sbx"),
        model="m",
        provider="p",
        hermes="hermes",
    )

    assert "--read-only-agentroot" in argv


def test_the_prompt_names_neither_the_token_nor_the_path() -> None:
    """Naming the path would let a run pass by having been told where to look
    instead of by following the instruction file."""
    assert "INDEX.md" not in verifier.PROMPT
    assert "memory/team" not in verifier.PROMPT
    assert SENTINEL_PATTERN.search(verifier.PROMPT) is None


def test_every_verification_mints_a_fresh_sentinel() -> None:
    """A fixed token could be answered from a cached response or from a
    previous run's state."""
    minted = {verifier.new_sentinel() for _ in range(50)}

    assert len(minted) == 50
    assert all(SENTINEL_PATTERN.fullmatch(item) for item in minted)


def test_the_report_quotes_no_agent_output(capsys: pytest.CaptureFixture[str]) -> None:
    verdict = verify_consumption(_evidence())
    verifier._report(verdict, path=None)

    printed = capsys.readouterr().out
    assert SENTINEL not in printed
    assert json.loads(printed)["consumed"] is True
