"""Deterministic OpenAI-compatible gateway for the local Kubernetes smoke test.

Routing decisions are reconstructed from current messages, as a real gateway
would see them. A test-only metadata barrier synchronizes admitted web turns.
It never performs an outbound request and never stores prompts in barrier state.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

MODEL_ID = os.getenv("TEST_GATEWAY_MODEL_ID", "k8s-test")
WEB_FIXTURE_ORIGIN = "http://" + os.getenv(
    "WEB_ACCEPTANCE_FIXTURE_IPV4", "93.184.216.2"
)
FETCH_FIXTURE_TITLE = os.getenv(
    "TEST_GATEWAY_FETCH_TITLE", "Kubernetes MCP Fetch Fixture"
)
_SKILL_LINE = re.compile(
    r"(?im)^\s*-\s*id\s*=\s*"
    r"(?P<id>[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})"
    r"\s+name\s*=\s*(?P<name>[^:\n]+)"
)
_STDOUT_42 = re.compile(r"(?m)^stdout:\s*42\s*$")
_STDOUT_142 = re.compile(r"(?m)^stdout:\s*142\s*$")
_NUMBER_42 = re.compile(r"(?<!\d)42(?!\d)")
_EXIT_CODE = re.compile(
    r"(?im)^exit_code:\s*(?P<code>-?\d+|none)"
    r"(?P<timed_out>\s+\(timed out\))?\s*$"
)
_IS_ERROR_FALSE = re.compile(r"(?i)[\"']?isError[\"']?\s*:\s*false")
_MULTI_AGENT_NODE = re.compile(r"MULTI_AGENT_NODE:([A-Za-z0-9_.-]{1,64})")
_MULTI_AGENT_NODE_RESULT = re.compile(
    r"MULTI_AGENT_NODE_RESULT:([A-Za-z0-9_.-]{1,64})"
)
_MULTI_AGENT_BARRIER = re.compile(
    r"MULTI_AGENT_BARRIER:([A-Za-z0-9_.-]{1,64})"
)
_MULTI_AGENT_LOOP_CONTEXT = re.compile(
    r"MULTI_AGENT_LOOP_CONTEXT:(draft|review)"
)
_MULTI_AGENT_WEB = re.compile(
    r"MULTI_AGENT_WEB:(question|sibling|budget-(?:5|6)|revoke:"
    r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})"
)
_MULTI_AGENT_WEB_CHILD = re.compile(
    r"MULTI_AGENT_(?:WEB|BUDGET|REVOKE)_CHILD:([A-Za-z0-9_.-]{1,64})"
)
_MULTI_AGENT_QUESTIONS = re.compile(r"MULTI_AGENT_QUESTIONS:(5|6)")
_MULTI_AGENT_QUESTION_BUDGET_CHILD = re.compile(
    r"MULTI_AGENT_QUESTION_BUDGET_CHILD:([A-Za-z0-9_.-]{1,64})"
)
_SOURCE_CITATION = re.compile(
    r"\[source:([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\]"
)
_PREPARATION_SCHEMA_MARKER = "valid JSON object matching this schema:"
_ASK_RESULT_PREFIX = "Tool result for ask_user: "
_QUESTION_MARKERS = ("ASK_CITY_THEN_FINAL", "ASK_TWICE", "ASK_LOOP", "ASK_OPTION")


class Message(BaseModel):
    role: str
    content: str


class CompletionRequest(BaseModel):
    model: str = MODEL_ID
    messages: list[Message]
    stream: bool = True


app = FastAPI(title="AurumAI deterministic Kubernetes test gateway")
_web_barrier = {"armed": False, "reached": False, "released": False}
_web_release = asyncio.Event()


def _web_advertised(messages: list[Message]) -> bool:
    return any(message.role == "system" and re.search(r'"tool"\s*:\s*"web_(search|read)"', message.content)
               for message in messages)


@app.api_route("/web-e2e/barrier/{action}", methods=["GET", "POST"])
async def web_barrier(action: str) -> dict[str, bool]:
    global _web_release
    if not re.fullmatch(r"workflow-templates-ui-e2e-[a-f0-9]{16}-model", MODEL_ID):
        raise HTTPException(404, "unavailable")
    if action == "arm":
        if _web_barrier["armed"] and not _web_barrier["released"]:
            raise HTTPException(409, "barrier already armed")
        _web_release = asyncio.Event()
        _web_barrier.update(armed=True, reached=False, released=False)
    elif action == "release":
        if not _web_barrier["armed"]:
            raise HTTPException(409, "barrier not armed")
        _web_barrier["released"] = True
        _web_release.set()
    elif action != "status":
        raise HTTPException(404, "unavailable")
    return dict(_web_barrier)


def _available_skills(messages: list[Message]) -> list[tuple[str, str]]:
    skills: list[tuple[str, str]] = []
    for message in messages:
        if message.role != "system":
            continue
        for match in _SKILL_LINE.finditer(message.content):
            skills.append((match.group("id"), match.group("name").strip()))
    return skills


def _select_skill(skills: list[tuple[str, str]], intent: str) -> str:
    words = {word for word in re.split(r"[^a-z0-9]+", intent.lower()) if len(word) > 2}
    best_id = ""
    best_score = 0
    for skill_id, name in skills:
        name_words = {
            word for word in re.split(r"[^a-z0-9]+", name.lower()) if len(word) > 2
        }
        score = len(words & name_words)
        if score > best_score:
            best_id = skill_id
            best_score = score
    if best_id:
        return best_id
    raise ValueError(f"No skill in the system prompt matches intent {intent!r}")


def _is_code_round(messages: list[Message]) -> bool:
    return any(
        message.role == "system"
        and "running a code skill inside an isolated sandbox" in message.content.lower()
        for message in messages
    )


def _last_user_message(messages: list[Message]) -> str:
    return next(
        (message.content for message in reversed(messages) if message.role == "user"),
        "",
    )


def _preparation_response(messages: list[Message]) -> str | None:
    last = _last_user_message(messages)
    if "Prepare one concise execution brief for every workflow node" not in last:
        return None
    for message in messages:
        if message.role != "system":
            continue
        marker_index = message.content.rfind(_PREPARATION_SCHEMA_MARKER)
        if marker_index < 0:
            continue
        raw_schema = message.content[
            marker_index + len(_PREPARATION_SCHEMA_MARKER) :
        ].lstrip()
        try:
            schema, _ = json.JSONDecoder().raw_decode(raw_schema)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(schema, dict):
            return None
        required = schema.get("required")
        properties = schema.get("properties")
        if (
            not isinstance(required, list)
            or not isinstance(properties, dict)
            or any(not isinstance(key, str) or key not in properties for key in required)
        ):
            return None
        briefs = {
            key: f"MULTI_AGENT_BRIEF:{key}" for key in sorted(set(required))
        }
        return json.dumps(briefs, sort_keys=True, separators=(",", ":"))
    return None


def _multi_agent_response(messages: list[Message]) -> str | None:
    preparation = _preparation_response(messages)
    if preparation is not None:
        return preparation
    last = _last_user_message(messages)
    if "Synthesize the final answer to the original user request" in last:
        if "draft-v2-from-draft-v1" in last and "review-v2" in last:
            return "MULTI_AGENT_LOOP_CONTEXT_FINAL:draft-v2-from-draft-v1"
        question_nodes = sorted(set(_MULTI_AGENT_QUESTION_BUDGET_CHILD.findall(last)))
        if question_nodes:
            outcome = "user_input_limit" if ":user_input_limit" in last else "ok"
            return f"MULTI_AGENT_QUESTION_BUDGET_FINAL:{outcome}"
        web_nodes = sorted(set(_MULTI_AGENT_WEB_CHILD.findall(last)))
        if web_nodes:
            sources = list(dict.fromkeys(_SOURCE_CITATION.findall(last)))
            citations = "".join(f" [source:{source}]" for source in sources)
            if "MULTI_AGENT_BUDGET_CHILD:" in last:
                outcome = "web_limit" if ":web_limit" in last else "ok"
                return f"MULTI_AGENT_BUDGET_FINAL:{outcome}{citations}"
            if "MULTI_AGENT_REVOKE_CHILD:" in last:
                outcome = "web_access_denied" if ":web_access_denied" in last else "ok"
                return f"MULTI_AGENT_REVOKE_FINAL:{outcome}{citations}"
            answer = " ANSWER:IANA" if "ANSWER:IANA" in last else ""
            return f"MULTI_AGENT_WEB_FINAL:{','.join(web_nodes)}{answer}{citations}"
        node_keys = sorted(set(_MULTI_AGENT_NODE_RESULT.findall(last)))
        return f"MULTI_AGENT_FINAL:{','.join(node_keys)}"
    questions = _multi_agent_question_budget_response(messages)
    if questions is not None:
        return questions
    web = _multi_agent_web_response(messages)
    if web is not None:
        return web
    loop_context = _MULTI_AGENT_LOOP_CONTEXT.search(last)
    if loop_context is not None:
        if loop_context.group(1) == "draft":
            if (
                "'previous_iteration':" in last
                and "'iteration': 1" in last
                and "draft-v1" in last
                and "review-v1" in last
            ):
                return "draft-v2-from-draft-v1"
            return "draft-v1"
        return "review-v2" if "draft-v2-from-draft-v1" in last else "review-v1"
    node = _MULTI_AGENT_NODE.search(last)
    if node is not None:
        return f"MULTI_AGENT_NODE_RESULT:{node.group(1)}"
    return None


def _multi_agent_question_budget_response(messages: list[Message]) -> str | None:
    start = next(
        (
            index
            for index, message in enumerate(messages)
            if message.role == "user"
            and _MULTI_AGENT_NODE.search(message.content)
            and _MULTI_AGENT_QUESTIONS.search(message.content)
        ),
        None,
    )
    if start is None:
        return None
    initial = messages[start].content
    node_match = _MULTI_AGENT_NODE.search(initial)
    total_match = _MULTI_AGENT_QUESTIONS.search(initial)
    if node_match is None or total_match is None:
        return None
    node, total = node_match.group(1), int(total_match.group(1))
    later = messages[start + 1 :]
    if any(
        message.role == "user"
        and message.content == "Tool result for ask_user: user_input_limit"
        for message in later
    ):
        return f"MULTI_AGENT_QUESTION_BUDGET_CHILD:{node}:user_input_limit"
    answers = [
        answer
        for message in later
        for answer in [_question_answer(message.content)]
        if answer is not None
    ]
    if len(answers) < total:
        return _ask_user_call(f"Question {len(answers) + 1} for {node}?")
    return f"MULTI_AGENT_QUESTION_BUDGET_CHILD:{node}:ok"


def _multi_agent_web_response(messages: list[Message]) -> str | None:
    """Drive exact test-only node protocols without treating tool data as commands."""
    start = next(
        (
            index
            for index, message in enumerate(messages)
            if message.role == "user"
            and _MULTI_AGENT_NODE.search(message.content)
            and _MULTI_AGENT_WEB.search(message.content)
        ),
        None,
    )
    if start is None:
        return None
    initial = messages[start].content
    node_match = _MULTI_AGENT_NODE.search(initial)
    mode_match = _MULTI_AGENT_WEB.search(initial)
    if node_match is None or mode_match is None:
        return None
    node, mode = node_match.group(1), mode_match.group(1)

    results: list[tuple[str, dict[str, Any], list[str]]] = []
    for message in messages[start + 1 :]:
        match = re.match(
            r"^Tool result for (web_search|web_read): (.*)$",
            message.content,
            re.S,
        )
        if match is None:
            continue
        try:
            wrapper = json.loads(match.group(2))
            parsed = json.loads(wrapper["text"])
            source_ids = wrapper["source_ids"]
            if not isinstance(parsed, dict) or not isinstance(source_ids, list):
                raise TypeError
            results.append((match.group(1), parsed, source_ids))
        except (KeyError, TypeError, ValueError):
            return f"MULTI_AGENT_WEB_CHILD:{node}:invalid-tool-data"

    def call(tool: str, args: dict[str, str]) -> str:
        payload = json.dumps({"tool": tool, "args": args}, separators=(",", ":"))
        return f"<tool_call>{payload}</tool_call>"

    if mode.startswith("budget-"):
        if not results:
            count = int(mode.removeprefix("budget-"))
            return "\n".join(
                call("web_search", {"query": "IANA reserved domains"})
                for _ in range(count)
            )
        errors = [result.get("error") for _, result, _ in results if result.get("error")]
        sources = list(dict.fromkeys(source for _, _, ids in results for source in ids))
        outcome = "web_limit" if "web_limit" in errors else "ok"
        return f"MULTI_AGENT_BUDGET_CHILD:{node}:{outcome}" + "".join(
            f" [source:{source}]" for source in sources
        )

    if mode.startswith("revoke:"):
        if not results:
            identity = mode.removeprefix("revoke:")
            return call("web_read", {"url": f"{WEB_FIXTURE_ORIGIN}/fault/{identity}"})
        errors = [result.get("error") for _, result, _ in results if result.get("error")]
        outcome = "web_access_denied" if "web_access_denied" in errors else "ok"
        return f"MULTI_AGENT_REVOKE_CHILD:{node}:{outcome}"

    answers = [
        answer
        for message in messages[start + 1 :]
        for answer in [_question_answer(message.content)]
        if answer is not None
    ]
    if mode == "question" and not answers:
        return _ask_user_call("Which multi-agent source topic?")
    if mode == "question" and answers[-1] != "IANA":
        return f"MULTI_AGENT_WEB_CHILD:{node} ANSWER:REJECTED"
    if not results:
        return call("web_search", {"query": "IANA reserved domains"})
    if results[-1][0] == "web_search":
        hits = results[-1][1].get("results", [])
        if not isinstance(hits, list) or not hits or not isinstance(hits[0], dict):
            return f"MULTI_AGENT_WEB_CHILD:{node}:empty-search"
        url = hits[0].get("url")
        if not isinstance(url, str):
            return f"MULTI_AGENT_WEB_CHILD:{node}:invalid-tool-data"
        return call("web_read", {"url": url})
    sources = list(dict.fromkeys(source for _, _, ids in results for source in ids))
    answer = " ANSWER:IANA" if mode == "question" else ""
    return f"MULTI_AGENT_WEB_CHILD:{node}{answer}" + "".join(
        f" [source:{source}]" for source in sources
    )


def _ask_user_call(text: str) -> str:
    payload = {"tool": "ask_user", "args": {"text": text, "options": []}}
    return f"<tool_call>{json.dumps(payload, separators=(',', ':'))}</tool_call>"


def _question_answer(content: str) -> str | None:
    if not content.startswith(_ASK_RESULT_PREFIX):
        return None
    try:
        payload = json.loads(content[len(_ASK_RESULT_PREFIX) :])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("tool") == "ask_user":
        output = payload.get("output")
    elif payload and set(payload) <= {"text", "optionId"}:
        # AgentWorkflow feeds the persisted AnswerRequest directly back to
        # its LLM; ordinary ConversationWorkflow retains the tool/output
        # envelope above. Accept only those two exact protocol shapes.
        output = payload
    else:
        return None
    if not isinstance(output, dict):
        return None
    for key in ("text", "optionId"):
        value = output.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _question_marker_response(messages: list[Message]) -> str | None:
    marker: str | None = None
    marker_index = -1
    last_user = _last_user_message(messages)
    last_user_index = next(
        (index for index in range(len(messages) - 1, -1, -1) if messages[index].role == "user"),
        -1,
    )
    continuing_question = _question_answer(last_user) is not None
    for index, message in enumerate(messages):
        if message.role != "user":
            continue
        if not continuing_question and index != last_user_index:
            continue
        found = next(
            (candidate for candidate in _QUESTION_MARKERS if candidate in message.content),
            None,
        )
        if found is not None:
            marker = found
            marker_index = index
    if marker is None:
        return None

    answers = [
        answer
        for message in messages[marker_index + 1 :]
        if message.role == "user"
        for answer in [_question_answer(message.content)]
        if answer is not None
    ]
    if marker == "ASK_CITY_THEN_FINAL":
        return (
            f"CITY CONFIRMED: {answers[0]}"
            if answers
            else _ask_user_call("Which city?")
        )
    if marker == "ASK_OPTION":
        if answers:
            return f"OPTION CONFIRMED: {answers[0]}"
        payload = {"tool": "ask_user", "args": {"text": "Choose city?", "options": [
            {"id": "kazan", "label": "Kazan"}, {"id": "tomsk", "label": "Tomsk"}]}}
        return "<tool_call>" + json.dumps(payload) + "</tool_call>"
    if marker == "ASK_TWICE":
        if not answers:
            return _ask_user_call("Which city?")
        if len(answers) == 1:
            return _ask_user_call("Which district?")
        return f"LOCATION CONFIRMED: {answers[0]} / {answers[1]}"
    return (
        f"LOOP CONFIRMED: {answers[0]}"
        if answers
        else _ask_user_call("Which loop value?")
    )


def _barrier_key(messages: list[Message]) -> str | None:
    match = _MULTI_AGENT_BARRIER.search(_last_user_message(messages))
    return match.group(1) if match is not None else None


def _code_response(messages: list[Message]) -> str:
    last = _last_user_message(messages)
    result = _command_result(last)
    if result == "timed_out":
        return "CODE SKILL ERROR: command timed out"
    if result == "failed":
        return "CODE SKILL ERROR: command failed"
    system_text = "\n".join(
        message.content.lower() for message in messages if message.role == "system"
    )
    if "greet skill" in system_text or "scripts/greet.py" in system_text:
        if result == "succeeded" and "hello, world!" in last.lower():
            return "Hello, World!"
        if result == "succeeded":
            return "CODE SKILL ERROR: expected output missing"
        return _run_command('python scripts/greet.py "World"')
    if result == "succeeded" and _STDOUT_142.search(last):
        return "FINAL RESULT: 142"
    if result == "succeeded" and _STDOUT_42.search(last):
        return _run_command('python3 -c "print(42 + 100)"')
    if result == "succeeded":
        return "CODE SKILL ERROR: expected output missing"
    return _run_command('python3 -c "print(6 * 7)"')


def _command_result(content: str) -> str | None:
    """Return the status of the latest sandbox command result, if present."""
    match = _EXIT_CODE.search(content)
    if match is None:
        return None
    if match.group("timed_out"):
        return "timed_out"
    return "succeeded" if match.group("code") == "0" else "failed"


def _run_command(command: str) -> str:
    payload = json.dumps({"command": command}, separators=(",", ":"))
    return f"<run_command>{payload}</run_command>"


def _outer_response(messages: list[Message]) -> str:
    last = _last_user_message(messages)
    if last.startswith("ROUTING_"):
        return "ROUTING_CHAT_FALLBACK"
    lower = last.lower()
    if lower.startswith("tool result for "):
        prior_user_text = "\n".join(
            message.content.lower()
            for message in messages[:-1]
            if message.role == "user"
        )
        if "fetch" in lower or "fetch" in prior_user_text:
            if FETCH_FIXTURE_TITLE in last and _IS_ERROR_FALSE.search(last):
                return f"FETCH FIXTURE VERIFIED: {FETCH_FIXTURE_TITLE}"
            return "FETCH FIXTURE VALIDATION FAILED"
        if "hello, world!" in lower:
            return "Hello, World!"
        if "final result: 142" in lower:
            return "FINAL RESULT: 142"
        if _NUMBER_42.search(last):
            return "42"

    skills = _available_skills(messages)
    if skills:
        if "fetch" in lower:
            skill_id = _select_skill(skills, "smoke-test-fetch")
            url_match = re.search(r"https?://[^\s]+", last)
            url = (
                url_match.group(0).rstrip(".,)")
                if url_match
                else "http://backend:8000/openapi.json"
            )
            payload = {"skill_id": skill_id, "args": {"url": url}}
        elif "greet" in lower:
            skill_id = _select_skill(skills, "smoke-test-greet")
            payload = {
                "skill_id": skill_id,
                "args": {"task": "Greet someone named World"},
            }
        elif any(marker in lower for marker in ("multistep", "two-step", "code skill")):
            skill_id = _select_skill(skills, "smoke-test-multistep code")
            payload = {
                "skill_id": skill_id,
                "args": {"task": "compute the two-step result"},
            }
        elif any(marker in lower for marker in ("sum", "plus", "17", "25")):
            skill_id = _select_skill(skills, "smoke-test-sum")
            payload = {"skill_id": skill_id, "args": {"a": 17, "b": 25}}
        else:
            return "Hello from the deterministic Kubernetes gateway."
        return f"<tool_call>{json.dumps(payload, separators=(',', ':'))}</tool_call>"

    return "Hello from the deterministic Kubernetes gateway."


def _routing_response(messages: list[Message]) -> str | None:
    system = next((m.content for m in messages if m.role == "system"), "")
    if not system.startswith("You route chat turns to background multi-agent workflows."):
        return None
    prompt = _last_user_message(messages)
    if prompt == "ROUTING_MALFORMED":
        return '<workflow_call>{"workflow_id":'
    match = re.fullmatch(r"ROUTING_(SELECT|UNAVAILABLE):([0-9a-f-]{36})", prompt)
    if match is None:
        return ""
    mode, workflow_id = match.groups()
    candidates = re.findall(r"(?m)^- id=([0-9a-f-]{36}) name=", system)
    if mode == "SELECT" and workflow_id not in candidates:
        return ""
    return '<workflow_call>' + json.dumps({"workflow_id": workflow_id}, separators=(",", ":")) + '</workflow_call>'


def _web_response(messages: list[Message]) -> str | None:
    # Only an exact operator-authored marker starts this fixture protocol.
    # Remote snippets/page text cannot select tools or supply search queries.
    start = next((i for i in range(len(messages) - 1, -1, -1)
                  if messages[i].role == "user" and not messages[i].content.startswith(("Tool result for ", _ASK_RESULT_PREFIX))), None)
    if start is None or not messages[start].content.startswith("WEB_E2E_"):
        return None
    prompt = messages[start].content
    if prompt == "WEB_E2E_TOOLS_HIDDEN":
        return "WEB_E2E_TOOLS_STILL_ADVERTISED" if _web_advertised(messages) else "WEB_E2E_TOOLS_HIDDEN_OK"
    results: list[tuple[str, dict[str, Any], list[str]]] = []
    for message in messages[start + 1:]:
        match = re.match(r"^Tool result for (web_search|web_read): (.*)$", message.content, re.S)
        if match:
            try:
                wrapper = json.loads(match[2])
                results.append((match[1], json.loads(wrapper["text"]), wrapper["source_ids"]))
            except (KeyError, ValueError, TypeError):
                return "WEB_E2E_INVALID_TOOL_DATA"

    def call(tool: str, args: dict[str, str]) -> str:
        return "<tool_call>" + json.dumps({"tool": tool, "args": args}, separators=(",", ":")) + "</tool_call>"

    wrong = re.fullmatch(r"WEB_E2E_WRONG:([0-9a-f-]{36})", prompt)
    if wrong:
        return f"WEB_E2E_WRONG [source:{wrong[1]}]"
    if results:
        errors = [row.get("error") for _, row, _ in results if row.get("error")]
        if errors:
            # Error codes are a fixed backend vocabulary, no raw exception body.
            allowed = {"web_limit", "web_access_denied", "web_url_blocked", "web_too_large",
                       "web_unavailable", "web_timeout", "unsupported_document", "web_disabled"}
            return "WEB_E2E_ERROR:" + (errors[-1] if errors[-1] in allowed else "unknown")
        if prompt in {"WEB_E2E_SEARCH", "WEB_E2E_MIXED"} and results[-1][0] == "web_search":
            hits = results[-1][1].get("results", [])
            if not hits:
                return "WEB_E2E_EMPTY_SEARCH"
            return call("web_read", {"url": hits[0]["url"]})
        ids = list(dict.fromkeys(source for _, _, sources in results for source in sources))
        return "WEB_E2E_FINAL" + (" ANSWER:IANA" if prompt == "WEB_E2E_MIXED" else "") + "".join(f" [source:{source}]" for source in ids)
    if prompt == "WEB_E2E_MIXED":
        answers = [_question_answer(m.content) for m in messages[start + 1:]]
        if "IANA" in answers:
            return call("web_search", {"query": "IANA reserved domains"})
        return _ask_user_call("Which source topic?")
    if prompt == "WEB_E2E_LIMIT":
        return "\n".join(call("web_search", {"query": "IANA reserved domains"}) for _ in range(11))
    if prompt in {"WEB_E2E_SEARCH", "WEB_E2E_REVOKE"}:
        return call("web_search", {"query": "IANA reserved domains"})
    fault = re.fullmatch(r"WEB_E2E_FAULT:([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})", prompt)
    if fault:
        return call("web_read", {"url": WEB_FIXTURE_ORIGIN + "/fault/" + fault[1]})
    paths = {"WEB_E2E_HOSTILE": "/hostile", "WEB_E2E_PDF": "/broken.pdf",
             "WEB_E2E_RECONNECT": "/slow",
             "WEB_E2E_HTML": "/empty", "WEB_E2E_BINARY": "/binary",
             "WEB_E2E_LARGE": "/large", "WEB_E2E_REDIRECT": "/redirect-internal"}
    if prompt in paths:
        return call("web_read", {"url": WEB_FIXTURE_ORIGIN + paths[prompt]})
    return None


def _content_for(request: CompletionRequest) -> str:
    routing = _routing_response(request.messages)
    if routing is not None:
        return routing
    web = _web_response(request.messages)
    if web is not None:
        return web
    question = _question_marker_response(request.messages)
    if question is not None:
        return question
    multi_agent = _multi_agent_response(request.messages)
    if multi_agent is not None:
        return multi_agent
    if _is_code_round(request.messages):
        return _code_response(request.messages)
    return _outer_response(request.messages)


async def _sse(model: str, content: str) -> AsyncIterator[str]:
    digest = hashlib.sha256(f"{model}\0{content}".encode()).hexdigest()[:32]
    completion_id = f"chatcmpl-k8s-{digest}"
    created = 0
    content_chunk: dict[str, Any] = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
    }
    stop_chunk: dict[str, Any] = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(content_chunk, separators=(',', ':'))}\n\n"
    yield f"data: {json.dumps(stop_chunk, separators=(',', ':'))}\n\n"
    yield "data: [DONE]\n\n"


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/models")
async def models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [{"id": MODEL_ID, "object": "model", "owned_by": "aurum-ai-tests"}],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: CompletionRequest) -> StreamingResponse:
    last = _last_user_message(request.messages)
    if last == "WEB_E2E_REVOKE":
        if not _web_barrier["armed"] or not _web_advertised(request.messages):
            raise HTTPException(409, "admitted web turn barrier prerequisite absent")
        _web_barrier["reached"] = True
        try:
            await asyncio.wait_for(_web_release.wait(), timeout=90)
        except TimeoutError:
            raise HTTPException(504, "owned barrier release deadline") from None
    if re.search(r"(?m)^MULTI_AGENT_FAIL:alpha$", last) and _MULTI_AGENT_NODE.search(last):
        raise HTTPException(status_code=400, detail="Deterministic node failure")
    if _barrier_key(request.messages) is not None:
        # Test-only safe boundary: the runner has already published the node's
        # running status before calling the gateway, giving the harness a
        # deterministic window for revoke/cancel. Never log prompt or key.
        await asyncio.sleep(float(os.getenv("TEST_GATEWAY_BARRIER_SECONDS", "3")))
    return StreamingResponse(
        _sse(request.model, _content_for(request)),
        media_type="text/event-stream",
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
