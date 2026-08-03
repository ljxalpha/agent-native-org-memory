import errno
import json
import os
import shlex
import sys
import time
from pathlib import Path

import pytest

from memory_net.distiller.llm.base import LLMRequest
from memory_net.distiller.llm.hermes_tool import HermesToolProvider
from memory_net.distiller.llm.mock import MockLLMProvider
from memory_net.distiller.pipeline import DistillationPipeline


def test_hermes_tool_provider_requires_command() -> None:
    with pytest.raises(RuntimeError, match="HERMES_TOOL_COMMAND"):
        HermesToolProvider(command=None).generate(LLMRequest("s", "u", "m"))


def test_hermes_tool_provider_accepts_quoted_command_args() -> None:
    script = (
        "import json, sys; "
        "request=json.load(sys.stdin); "
        "print(json.dumps({'text': request['user_prompt'], 'model': request['model']}))"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"

    response = HermesToolProvider(command=command).generate(
        LLMRequest("system", "user", "model")
    )

    assert response.text == "user"
    assert response.model == "model"


def _echo_command(payload: str) -> str:
    script = f"import sys; sys.stdout.write({payload!r})"
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"


def test_provider_relays_the_backend_declared_provenance() -> None:
    """A real backend behind the bridge must stay visible in the aggregate."""
    payload = json.dumps(
        {
            "text": "t",
            "model": "m",
            "usage": {"input_tokens": 3},
            "usage_source": "reported",
            "provider_backend": "hermes/claude-x",
            "raw_meta": {"finish_reason": "stop"},
        }
    )

    response = HermesToolProvider(command=_echo_command(payload)).generate(
        LLMRequest("s", "u", "m")
    )

    assert response.usage_source == "reported"
    assert response.provider_backend == "hermes/claude-x"
    assert response.raw_meta == {"finish_reason": "stop"}


def test_a_bridge_without_provenance_degrades_instead_of_failing() -> None:
    """Older bridges predate the three keys; they must not turn the run red."""
    payload = json.dumps({"text": "t", "model": "m", "usage": {"input_tokens": 3}})

    response = HermesToolProvider(command=_echo_command(payload)).generate(
        LLMRequest("s", "u", "m")
    )

    # Never optimistically "reported": the bridge cannot vouch for counts it
    # only relayed. The backend label still identifies who ran the call.
    assert response.usage_source == "unknown"
    assert response.provider_backend == "hermes_tool"
    assert response.raw_meta == {}


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        ("replay", "unknown"),
        ("", "unknown"),
        (7, "unknown"),
        ("estimated", "estimated"),
    ],
)
def test_only_in_domain_usage_sources_are_honoured(
    declared: object, expected: str
) -> None:
    payload = json.dumps({"text": "t", "model": "m", "usage_source": declared})

    response = HermesToolProvider(command=_echo_command(payload)).generate(
        LLMRequest("s", "u", "m")
    )

    assert response.usage_source == expected


def test_non_string_provenance_from_the_bridge_is_dropped_not_coerced() -> None:
    """A malformed bridge must not be able to poison the response contract."""
    payload = json.dumps(
        {
            "text": "t",
            "model": "m",
            "provider_backend": 1,
            "raw_meta": {"ok": "yes", "bad": 2, "3": "no-int-keys-in-json"},
        }
    )

    response = HermesToolProvider(command=_echo_command(payload)).generate(
        LLMRequest("s", "u", "m")
    )

    assert response.provider_backend == "hermes_tool"
    assert response.raw_meta == {"ok": "yes", "3": "no-int-keys-in-json"}


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError as exc:
        # EPERM means the pid exists but belongs to someone else; only ESRCH
        # proves it is gone.
        return exc.errno != errno.ESRCH
    return True


def test_a_timeout_kills_the_bridge_process_group_not_just_the_bridge(
    tmp_path: Path,
) -> None:
    """A killed bridge must not leave a live grandchild burning quota.

    The stub spawns a long-lived child and then hangs. Signalling only the
    bridge would leave that grandchild running (it is reparented, not reaped),
    still holding whatever agent session the bridge started.
    """
    pid_file = tmp_path / "grandchild.pid"
    script = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time;"
        " time.sleep(120)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(120)\n"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"

    with pytest.raises(RuntimeError, match="timed out"):
        HermesToolProvider(command=command, timeout_s=2.0).generate(
            LLMRequest("s", "u", "m")
        )

    grandchild = int(pid_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 10.0
    while _is_alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _is_alive(grandchild)


def test_non_json_stdout_raises_with_both_streams_in_the_message() -> None:
    """A chatty agent is the expected failure; the message must be diagnosable.

    A bare ``JSONDecodeError`` is neither the provider's declared error type nor
    does it carry the stdout/stderr evidence needed to tell what the bridge did.
    """
    script = (
        "import sys; "
        "sys.stdout.write('thinking out loud...'); "
        "sys.stderr.write('bridge warning: no toolset')"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"

    with pytest.raises(RuntimeError) as excinfo:
        HermesToolProvider(command=command).generate(LLMRequest("s", "u", "m"))

    message = str(excinfo.value)
    assert "thinking out loud" in message
    assert "bridge warning: no toolset" in message


def test_a_non_object_json_response_is_rejected_rather_than_guessed() -> None:
    """No silent "treat the raw stdout as text" fallback: it would turn a broken
    bridge into a successful run carrying garbage into the distillation."""
    script = "import sys; sys.stdout.write('[1, 2, 3]')"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"

    with pytest.raises(RuntimeError, match="JSON object"):
        HermesToolProvider(command=command).generate(LLMRequest("s", "u", "m"))


def test_mock_and_hermes_fixture_pipeline_are_equivalent(tmp_path: Path) -> None:
    system = tmp_path / "system.md"
    user = tmp_path / "user.md"
    system.write_text("system", encoding="utf-8")
    user.write_text("records {{ records_json }}", encoding="utf-8")
    records = [_record()]
    mock_output = DistillationPipeline(
        provider=MockLLMProvider(),
        system_prompt_path=system,
        user_prompt_path=user,
    ).run(records, run_id="run")
    hermes_output = DistillationPipeline(
        provider=FixedProvider(mock_output.response.text),
        system_prompt_path=system,
        user_prompt_path=user,
    ).run(records, run_id="run")

    assert [p.frontmatter["topic_id"] for p in mock_output.products] == [
        p.frontmatter["topic_id"] for p in hermes_output.products
    ]


class FixedProvider(MockLLMProvider):
    def __init__(self, text: str) -> None:
        self.text = text

    def generate(self, request: LLMRequest):  # type: ignore[no-untyped-def]
        from memory_net.distiller.llm.base import LLMResponse

        return LLMResponse(text=self.text, model=request.model)


def _record() -> dict[str, str]:
    return {
        "memory_id": "leaf-a-mem-1",
        "revision_hash": "abc12345",
        "author_node": "alice",
        "source_branch_sha": "deadbeef",
        "relative_path": "memories/auth/retry-abc12345.md",
        "category_hint": "auth",
        "content": "---\ntitle: Retry\n---\nUse backoff.",
    }
