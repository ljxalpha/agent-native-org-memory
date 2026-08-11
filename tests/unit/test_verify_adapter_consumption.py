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


def test_the_sentinel_basis_does_not_overclaim_what_it_shows() -> None:
    """It shows the file's contents reached the model. Whether the agent went
    and read it, or the framework inlined the file the hint referenced, the
    sentinel cannot tell apart -- and an integrator asking "does it actually
    read the file" needs to know that this basis does not answer them.
    """
    verdict = verify_consumption(_evidence())

    assert verdict.consumed
    assert any("reached the model" in note for note in verdict.notes)
    assert any("does not distinguish" in note for note in verdict.notes)


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


@pytest.mark.parametrize("flag", ["--safe-mode", "--ignore-rules"])
def test_the_command_line_never_disables_the_thing_being_verified(flag: str) -> None:
    """Both flags switch off the injection this check exists to observe.

    `--safe-mode` says so plainly. `--ignore-rules` does not: it reads like
    hygiene, and it maps to `skip_context_files=True`, so AGENTS.md never
    reaches the system prompt. A run carrying it cannot show a hint being
    followed, because no hint is there to follow -- and it still produces a
    perfectly ordinary-looking answer.
    """
    argv = verifier.sandbox_argv(
        runtime_root=Path("/opt/rt"),
        root=Path("/run/sbx"),
        model="m",
        provider="p",
        hermes="hermes",
        max_iterations=2,
    )

    assert flag not in argv
    # An empty toolset argument does the opposite of what it looks like: it
    # falls back to the configured sets, meaning all of them.
    assert "-t" not in argv


def test_the_model_and_provider_are_passed_through() -> None:
    argv = verifier.sandbox_argv(
        runtime_root=Path("/opt/rt"),
        root=Path("/run/sbx"),
        model="m",
        provider="p",
        hermes="hermes",
        max_iterations=2,
    )

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
        max_iterations=2,
    )

    assert "--read-only-agentroot" in argv


def test_the_prompt_describes_the_target_in_no_way_at_all() -> None:
    """Not just the path: any description of the target is a pointer.

    An earlier prompt asked for "the team memory index that your instructions
    reference", and the first real run showed an agent acts on that alone --
    it found and read the index with no hint present. A prompt that tells the
    agent such a file exists cannot test whether the hint is what told it.
    """
    prompt = verifier.PROMPT.lower()

    assert "index" not in prompt
    assert "memory" not in prompt
    assert "instruction" not in prompt
    assert "file" not in prompt
    assert "read" not in prompt
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


def test_ruling_out_inlining_upgrades_the_basis() -> None:
    """With inlining excluded, a tool read is the only remaining path by which
    the index's contents could have reached the model."""
    verdict = verify_consumption(
        _evidence(inlining_ruled_out="the loader performs no reference expansion")
    )

    assert verdict.consumed
    assert verdict.evidence_basis == "sentinel+no-inlining"
    assert any("only remaining path" in note for note in verdict.notes)


def test_the_upgraded_basis_records_why_and_not_merely_that() -> None:
    """A reason rather than a boolean: the stronger conclusion is worth exactly
    as much as the basis stated for it, so the basis travels with the verdict.
    """
    verdict = verify_consumption(
        _evidence(inlining_ruled_out="measured: no @-expansion")
    )

    assert any("measured: no @-expansion" in note for note in verdict.notes)


def test_the_conservative_reading_is_the_default() -> None:
    """Unset means unknown, not ruled out. A caller who has not established the
    fact gets the weaker claim."""
    verdict = verify_consumption(_evidence())

    assert verdict.evidence_basis == "sentinel"
    assert any("does not distinguish" in note for note in verdict.notes)


# -- the control must vary exactly one thing -----------------------------


def test_the_control_keeps_the_index_and_drops_only_the_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect this replaced: a control that removed the hint *and* the
    index compares "reference present, file present" against "neither".

    An agent that found the file by walking the tree -- rather than by
    following the reference -- produces exactly the passing pattern under that
    comparison, so it could not distinguish the thing being tested from an
    unrelated capability. The reference is what is under test, so the reference
    is the only thing that changes.
    """
    # `prepare` opts the experimental-adapter flag into the process environment
    # and does not put it back -- fine for a one-shot script, but in a test
    # process it leaks into every module that runs afterwards.
    monkeypatch.setenv("MEMORY_NET_ALLOW_EXPERIMENTAL_ADAPTER", "1")
    adapter = REPO_ROOT / "config" / "adapters" / "hermes.yaml"
    root = tmp_path / "sbx"
    (root / "agentroot").mkdir(parents=True)

    index_text, positive_instructions = verifier.prepare(
        root, adapter_path=adapter, sentinel=SENTINEL, inject_hint=True
    )
    index = root / "agentroot" / "memory" / "team" / "INDEX.md"
    assert SENTINEL in index.read_text(encoding="utf-8")
    assert "@memory/team/INDEX.md" in positive_instructions

    control_index, control_instructions = verifier.prepare(
        root, adapter_path=adapter, sentinel=SENTINEL, inject_hint=False
    )

    # The index survives, byte for byte, carrying the same sentinel.
    assert index.is_file()
    assert control_index == index_text
    assert SENTINEL in index.read_text(encoding="utf-8")
    # And the reference is the one thing that is gone.
    assert "@memory/team/INDEX.md" not in control_instructions


# -- the cost bound has to be verified, not assumed -----------------------


def test_a_missing_sandbox_config_refuses_to_start(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="agent_sandbox up"):
        verifier.assert_turn_cap(tmp_path)


@pytest.mark.parametrize(
    "config",
    [
        'platform_toolsets:\n  cli: ["file"]\n',
        "agent:\n  max_turns: 90\n",
        "agent:\n  max_turns: 3\n",
    ],
)
def test_a_sandbox_without_the_expected_cap_refuses_to_start(
    tmp_path: Path, config: str
) -> None:
    """The cap is written by a previous command, and it is the only thing
    bounding this check's cost. Assuming it is present is how a run costs
    ninety calls: that is the agent's own default, and its config file outranks
    the environment variable that looks like the limit.
    """
    path = tmp_path / "home" / ".hermes"
    path.mkdir(parents=True)
    (path / "config.yaml").write_text(config, encoding="utf-8")

    with pytest.raises(SystemExit, match="agent_sandbox up"):
        verifier.assert_turn_cap(tmp_path)


def test_what_agent_sandbox_actually_writes_is_accepted(tmp_path: Path) -> None:
    """Generated by one command and validated by another, so the two have to
    agree on the file's shape or the check refuses every real sandbox."""
    path = tmp_path / "home" / ".hermes"
    path.mkdir(parents=True)
    (path / "config.yaml").write_text(
        sandbox_module().config_yaml("file", max_turns=verifier.DEFAULT_MAX_ITERATIONS),
        encoding="utf-8",
    )

    verifier.assert_turn_cap(tmp_path)


def sandbox_module() -> ModuleType:
    path = REPO_ROOT / "scripts" / "agent_sandbox"
    spec = importlib.util.spec_from_file_location(
        "agent_sandbox_for_verify",
        path,
        loader=importlib.machinery.SourceFileLoader(
            "agent_sandbox_for_verify", str(path)
        ),
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_charge_exceeds_the_iteration_cap_by_the_grace_call() -> None:
    """N iterations permit N+1 requests: on exhausting its budget the agent
    makes one further toolless request to summarise.

    Charging the iteration count would under-report precisely when the cap
    binds, which is the one situation the ledger exists for.
    """
    assert verifier.calls_per_run(2) == 3
    assert verifier.calls_per_run(90) == 91


def test_the_quiet_flag_is_used_on_the_subcommand_that_defines_it() -> None:
    """`-Q` exists only on `chat`. The top-level one-shot form (`-z`) rejects
    it as an unknown argument and exits 2 -- which looks like an agent failure
    and costs a charged attempt to discover.
    """
    argv = verifier.sandbox_argv(
        runtime_root=Path("/opt/rt"),
        root=Path("/run/sbx"),
        model="m",
        provider="p",
        hermes="hermes",
        max_iterations=1,
    )
    tail = argv[argv.index("--") + 1 :]

    assert tail[1] == "chat"
    assert "-z" not in tail
    assert tail[tail.index("-q") + 1] == verifier.PROMPT


def test_the_run_prints_only_the_final_answer() -> None:
    """The sentinel is looked for in stdout, so anything else printed there is
    a way for the check to find its own input and call it evidence.

    A tool preview echoing the file that was just read contains the sentinel
    without the model having produced it -- and that output is indistinguishable
    from a correct answer to a substring search.
    """
    argv = verifier.sandbox_argv(
        runtime_root=Path("/opt/rt"),
        root=Path("/run/sbx"),
        model="m",
        provider="p",
        hermes="hermes",
        max_iterations=2,
    )

    assert "-Q" in argv


@pytest.mark.parametrize(
    "config",
    [
        'platform_toolsets:\n  cli: ["file"]\nagent:\n  max_turns: 2\nsecret: hunter2\n',
        "agent:\n  max_turns: 2\n",
        'platform_toolsets:\n  cli: ["file", "shell"]\nagent:\n  max_turns: 2\n',
    ],
)
def test_the_toolset_is_pinned_too(tmp_path: Path, config: str) -> None:
    """A wider toolset is a second way to blow the budget, not a nicety: a
    delegation tool starts child agents whose iteration budgets are their own.

    The first case has the right cap and the right toolset but an extra key,
    and must pass -- the check pins what it needs, not the whole file.
    """
    path = tmp_path / "home" / ".hermes"
    path.mkdir(parents=True)
    (path / "config.yaml").write_text(config, encoding="utf-8")

    if "hunter2" in config:
        verifier.assert_turn_cap(tmp_path)
    else:
        with pytest.raises(SystemExit):
            verifier.assert_turn_cap(tmp_path)


@pytest.mark.parametrize(
    "config",
    ["agent:\n  max_turns: 2\n  [unclosed\n", "just a string\n", "agent: 2\n"],
)
def test_an_unparsable_or_odd_config_is_refused_without_quoting_it(
    tmp_path: Path, config: str
) -> None:
    """The message must not echo the file. This runs beside credential material,
    the file is one an operator may have edited, and PyYAML's own diagnostics
    quote the offending source line.
    """
    path = tmp_path / "home" / ".hermes"
    path.mkdir(parents=True)
    (path / "config.yaml").write_text(config, encoding="utf-8")

    with pytest.raises(SystemExit) as raised:
        verifier.assert_turn_cap(tmp_path)

    message = str(raised.value)
    assert "agent_sandbox up" in message
    for fragment in ("unclosed", "just a string", "max_turns: 2"):
        assert fragment not in message


def test_the_control_starts_from_the_same_agent_state(tmp_path: Path) -> None:
    """A run leaves a database, caches and logs behind. Reusing them makes the
    control the second half of a sequence rather than a comparison, and any
    difference could be the order instead of the hint.
    """
    root = tmp_path / "sbx"
    (root / "home" / ".hermes").mkdir(parents=True)
    (root / "home" / ".hermes" / "config.yaml").write_text("cfg\n", encoding="utf-8")

    pristine = verifier.snapshot_home(root, root / "pristine-home")
    # The first run dirties the home.
    (root / "home" / ".hermes" / "state.db").write_text("rows\n", encoding="utf-8")
    (root / "home" / ".hermes" / "config.yaml").write_text(
        "mutated\n", encoding="utf-8"
    )

    verifier.restore_home(root, pristine)

    assert not (root / "home" / ".hermes" / "state.db").exists()
    assert (root / "home" / ".hermes" / "config.yaml").read_text() == "cfg\n"


# -- the instruction file must demonstrably reach the system prompt -------


def test_an_unchanged_prompt_between_the_trees_is_refused() -> None:
    """The interesting failure: equal context bytes mean the instruction file
    is not being loaded at all.

    Every run under that condition tests filesystem discovery while looking
    exactly like a test of the hint -- which is how the first attempt at this
    check went wrong, with every line of reasoning about the loader still true.
    """
    with pytest.raises(SystemExit, match="not reaching the system prompt"):
        verifier.assert_hint_reached_the_prompt(
            with_hint=4096, without_hint=4096, instructions="x" * 100
        )


def test_a_smaller_prompt_with_the_hint_is_refused() -> None:
    with pytest.raises(SystemExit, match="not reaching the system prompt"):
        verifier.assert_hint_reached_the_prompt(
            with_hint=4000, without_hint=4096, instructions="x" * 100
        )


def test_a_difference_larger_than_the_hint_is_refused() -> None:
    """The two trees would then differ by something else as well, and the runs
    are no longer a single-variable comparison."""
    with pytest.raises(SystemExit, match="more than the injected hint"):
        verifier.assert_hint_reached_the_prompt(
            with_hint=9000, without_hint=4096, instructions="x" * 100
        )


def test_a_difference_within_the_injected_hint_is_accepted() -> None:
    verifier.assert_hint_reached_the_prompt(
        with_hint=4196, without_hint=4096, instructions="x" * 200
    )


def test_the_probe_calls_no_model() -> None:
    """`prompt-size` builds the prompt a session would use and reports a
    breakdown. It costs nothing, which is why it can gate the spend."""
    argv = verifier.sandbox_argv(
        runtime_root=Path("/opt/rt"),
        root=Path("/run/sbx"),
        model="m",
        provider="p",
        hermes="hermes",
        max_iterations=1,
    )
    probe = [*argv[: argv.index("--") + 1], "hermes", "prompt-size"]

    assert "-z" not in probe
    assert verifier.PROMPT not in probe


def test_the_turn_cap_is_configurable_and_bounds_the_charge() -> None:
    """Two runs at one iteration each is four charged attempts, which is what
    a four-attempt authorisation buys."""
    assert verifier.calls_per_run(1) == 2
    assert 2 * verifier.calls_per_run(1) == 4
    assert verifier.calls_per_run(2) == 3


def test_an_inlined_index_would_be_caught_by_the_upper_bound() -> None:
    """The bound's second job. If the agent expanded the reference, the prompt
    would grow by the index's size as well as the hint's -- so passing this
    measures, rather than argues, that the index was not inlined.

    Sized from the real assets: the injected instruction file is around 140
    bytes and the index around 169, so inlining roughly triples the delta.
    """
    hint_only, inlined = 82, 82 + 169

    verifier.assert_hint_reached_the_prompt(
        with_hint=168 + hint_only, without_hint=168, instructions="x" * 140
    )
    with pytest.raises(SystemExit, match="more than the injected hint"):
        verifier.assert_hint_reached_the_prompt(
            with_hint=168 + inlined, without_hint=168, instructions="x" * 140
        )
