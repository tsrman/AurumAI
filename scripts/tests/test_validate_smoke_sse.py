from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]
VALIDATOR = ROOT / "scripts" / "validate-smoke-sse.py"
TITLE = "Kubernetes MCP Fetch Fixture"
FETCH_FINAL = f"FETCH FIXTURE VERIFIED: {TITLE}"


def _event(event_type: str, data: str) -> str:
    return f"event: {event_type}\ndata: {data}\n\n"


def _validate(
    tmp_path: Path,
    scenario: str,
    body: str,
    *,
    expected_title: str | None = None,
    expected_marker: str | None = None,
) -> subprocess.CompletedProcess[str]:
    stream = tmp_path / f"{scenario}.sse"
    stream.write_text(body)
    command = [sys.executable, str(VALIDATOR), scenario, str(stream)]
    if expected_title is not None:
        command.extend(["--expected-title", expected_title])
    if expected_marker is not None:
        command.extend(["--expected-marker", expected_marker])
    return subprocess.run(command, text=True, capture_output=True, check=False)


def test_mcp_fetch_requires_successful_tool_result_and_distinct_final_marker(
    tmp_path: Path,
) -> None:
    body = (
        _event("tool-calls", '{"calls":[{"skill_id":"fetch"}]}')
        + _event(
            "tool-result",
            '{"result":{"skill_id":"fetch","output":{"result":'
            '{"content":[{"type":"text","text":"'
            + TITLE
            + '"}],"isError":false}}}}',
        )
        + _event(
            "message-end",
            '{"role":"assistant","content":"' + FETCH_FINAL + '"}',
        )
        + _event("done", '{}')
    )

    result = _validate(tmp_path, "mcp-fetch", body, expected_title=TITLE)

    assert result.returncode == 0, result.stderr


def test_mcp_fetch_title_in_failed_tool_result_cannot_satisfy_success(
    tmp_path: Path,
) -> None:
    body = (
        _event("tool-calls", '{"calls":[{"skill_id":"fetch"}]}')
        + _event(
            "tool-result",
            '{"result":{"skill_id":"fetch","output":{"result":'
            '{"content":[{"type":"text","text":"error after '
            + TITLE
            + '"}],"isError":true}}}}',
        )
        + _event(
            "message-end",
            '{"role":"assistant","content":"' + FETCH_FINAL + '"}',
        )
        + _event("done", '{}')
    )

    result = _validate(tmp_path, "mcp-fetch", body, expected_title=TITLE)

    assert result.returncode != 0
    assert "tool result" in result.stderr.lower()


def test_code_greet_rejects_stdout_marker_from_nonzero_run(tmp_path: Path) -> None:
    body = (
        _event("tool-calls", '{"calls":[{"skill_id":"greet"}]}')
        + _event("run-command", '{"round":1,"command":"greet"}')
        + _event(
            "run-result",
            '{"round":1,"result":{"stdout":"Hello, World!\\n",'
            '"stderr":"failed","exit_code":7,"timed_out":false}}',
        )
        + _event("tool-result", '{"result":{"output":"Hello, World!"}}')
        + _event(
            "message-end",
            '{"role":"assistant","content":"Hello, World!"}',
        )
        + _event("done", '{}')
    )

    result = _validate(tmp_path, "code-greet", body)

    assert result.returncode != 0
    assert "exit_code" in result.stderr


def test_code_multistep_rejects_timed_out_stdout_142(tmp_path: Path) -> None:
    body = (
        _event("tool-calls", '{"calls":[{"skill_id":"multistep"}]}')
        + _event("run-command", '{"round":1,"command":"first"}')
        + _event(
            "run-result",
            '{"round":1,"result":{"stdout":"42\\n","stderr":"",'
            '"exit_code":0,"timed_out":false}}',
        )
        + _event("run-command", '{"round":2,"command":"second"}')
        + _event(
            "run-result",
            '{"round":2,"result":{"stdout":"142\\n","stderr":"",'
            '"exit_code":0,"timed_out":true}}',
        )
        + _event("tool-result", '{"result":{"output":"FINAL RESULT: 142"}}')
        + _event(
            "message-end",
            '{"role":"assistant","content":"FINAL RESULT: 142"}',
        )
        + _event("done", '{}')
    )

    result = _validate(tmp_path, "code-multistep", body)

    assert result.returncode != 0
    assert "timed_out" in result.stderr


def test_code_error_final_cannot_satisfy_success(tmp_path: Path) -> None:
    body = (
        _event("tool-calls", '{"calls":[{"skill_id":"greet"}]}')
        + _event("run-command", '{"round":1,"command":"greet"}')
        + _event(
            "run-result",
            '{"round":1,"result":{"stdout":"Hello, World!\\n","stderr":"",'
            '"exit_code":0,"timed_out":false}}',
        )
        + _event("tool-result", '{"result":{"output":"CODE SKILL ERROR"}}')
        + _event(
            "message-end",
            '{"role":"assistant","content":"CODE SKILL ERROR: command failed"}',
        )
        + _event("done", '{}')
    )

    result = _validate(tmp_path, "code-greet", body)

    assert result.returncode != 0
    assert "final assistant content" in result.stderr.lower()


def test_valid_multistep_sse_requires_two_successful_results_and_exact_final(
    tmp_path: Path,
) -> None:
    body = (
        _event("tool-calls", '{"calls":[{"skill_id":"multistep"}]}')
        + _event("run-command", '{"round":1,"command":"first"}')
        + _event(
            "run-result",
            '{"round":1,"result":{"stdout":"42\\n","stderr":"",'
            '"exit_code":0,"timed_out":false}}',
        )
        + _event("run-command", '{"round":2,"command":"second"}')
        + _event(
            "run-result",
            '{"round":2,"result":{"stdout":"142\\n","stderr":"",'
            '"exit_code":0,"timed_out":false}}',
        )
        + _event("tool-result", '{"result":{"output":"FINAL RESULT: 142"}}')
        + _event(
            "message-end",
            '{"role":"assistant","content":"FINAL RESULT: 142"}',
        )
        + _event("done", '{}')
    )

    result = _validate(tmp_path, "code-multistep", body)

    assert result.returncode == 0, result.stderr


def test_ordinary_requires_successful_final_message_and_done(tmp_path: Path) -> None:
    body = (
        _event("assistant-delta", '{"content":"deterministic Kubernetes"}')
        + _event(
            "message-end",
            '{"role":"assistant","content":"deterministic Kubernetes"}',
        )
        + _event("done", '{}')
    )

    result = _validate(
        tmp_path,
        "ordinary",
        body,
        expected_marker="deterministic Kubernetes",
    )

    assert result.returncode == 0, result.stderr


def test_ordinary_rejects_marker_without_message_end(tmp_path: Path) -> None:
    body = _event("assistant-delta", '{"content":"deterministic Kubernetes"}') + _event(
        "done", '{}'
    )

    result = _validate(
        tmp_path,
        "ordinary",
        body,
        expected_marker="deterministic Kubernetes",
    )

    assert result.returncode != 0
    assert "message-end" in result.stderr
