import json
import uuid
from typing import Any, Callable


def extract_tool_calls(
    text: str, id_factory: Callable[[], Any] | None = None
) -> list[dict[str, Any]]:
    """Extract tool_calls JSON embedded in <tool_call>...</tool_call> markers.

    `id_factory` produces the fallback `call_id` for calls the model didn't
    id itself. Workflow code MUST pass `workflow.uuid4` here: this function
    runs inside workflow (not activity) code, and the default `uuid.uuid4`
    draws from `os.urandom`, so on history replay it would mint different
    ids — and `call_id` feeds the child-workflow id for code skills, where a
    replay mismatch is a fatal nondeterminism error."""
    make_id = id_factory or uuid.uuid4
    calls: list[dict[str, Any]] = []
    start_tag = "<tool_call>"
    end_tag = "</tool_call>"
    pos = 0
    while True:
        idx = text.find(start_tag, pos)
        if idx == -1:
            break
        end = text.find(end_tag, idx + len(start_tag))
        if end == -1:
            break
        payload = text[idx + len(start_tag) : end]
        try:
            parsed = json.loads(payload)
            items = parsed if isinstance(parsed, list) else [parsed]
            for item in items:
                if isinstance(item, dict):
                    item.setdefault("call_id", str(make_id()))
                    calls.append(item)
        except json.JSONDecodeError:
            pass
        pos = end + len(end_tag)
    return calls


def extract_run_commands(text: str) -> list[dict[str, Any]]:
    """Extract run_command JSON embedded in <run_command>...</run_command>
    markers — used only inside a code skill's multi-round sandbox session
    (see CodeSkillSessionWorkflow). Deliberately a separate tag from
    <tool_call>: the outer loop picks a skill, this inner loop picks shell
    commands to run within that skill's already-open sandbox, and conflating
    the two tags would let the outer LLM turn accidentally trigger sandbox
    execution (or vice versa)."""
    calls: list[dict[str, Any]] = []
    start_tag = "<run_command>"
    end_tag = "</run_command>"
    pos = 0
    while True:
        idx = text.find(start_tag, pos)
        if idx == -1:
            break
        end = text.find(end_tag, idx + len(start_tag))
        if end == -1:
            break
        payload = text[idx + len(start_tag) : end]
        try:
            parsed = json.loads(payload)
            items = parsed if isinstance(parsed, list) else [parsed]
            for item in items:
                if isinstance(item, dict) and item.get("command"):
                    calls.append(item)
        except json.JSONDecodeError:
            pass
        pos = end + len(end_tag)
    return calls
