from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch

MCP_SKILL_ID = "11111111-1111-4111-8111-111111111111"
CODE_SKILL_ID = "22222222-2222-4222-8222-222222222222"
FETCH_SKILL_ID = "33333333-3333-4333-8333-333333333333"
GREET_SKILL_ID = "44444444-4444-4444-8444-444444444444"
FETCH_FIXTURE_TITLE = "Kubernetes MCP Fetch Fixture"
FETCH_FINAL_MARKER = f"FETCH FIXTURE VERIFIED: {FETCH_FIXTURE_TITLE}"


def _load_gateway() -> ModuleType:
    path = Path(__file__).parents[1] / "k8s-test-gateway.py"
    spec = importlib.util.spec_from_file_location("k8s_test_gateway", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_owned_web_fault_marker_calls_exact_upstream_case():
    gateway = _load_gateway()
    case = "11111111-1111-4111-8111-111111111111"
    result = gateway._web_response([gateway.Message(role="user", content=f"WEB_E2E_FAULT:{case}")])
    assert result is not None
    payload = json.loads(result.removeprefix("<tool_call>").removesuffix("</tool_call>"))
    assert payload == {"tool": "web_read", "args": {"url": gateway.WEB_FIXTURE_ORIGIN + f"/fault/{case}"}}
    assert gateway._web_response([gateway.Message(role="user", content="WEB_E2E_FAULT:../control")]) is None


def test_question_options_are_consumed_into_final():
    gateway = _load_gateway()
    messages = [gateway.Message(role="user", content="ASK_OPTION")]
    result = gateway._question_marker_response(messages)
    assert result is not None
    payload = json.loads(result.removeprefix("<tool_call>").removesuffix("</tool_call>"))
    assert payload["args"]["options"] == [{"id": "kazan", "label": "Kazan"}, {"id": "tomsk", "label": "Tomsk"}]
    messages.append(gateway.Message(role="user", content=gateway._ASK_RESULT_PREFIX + json.dumps({"tool": "ask_user", "output": {"optionId": "tomsk"}})))
    assert gateway._question_marker_response(messages) == "OPTION CONFIRMED: tomsk"


def test_question_answer_accepts_agent_raw_and_preserves_ordinary_wrapper() -> None:
    gateway = _load_gateway()
    assert gateway._question_answer(
        gateway._ASK_RESULT_PREFIX + json.dumps({"text": "IANA", "optionId": None})
    ) == "IANA"
    assert gateway._question_answer(
        gateway._ASK_RESULT_PREFIX + json.dumps({
            "tool": "ask_user", "output": {"optionId": "tomsk"},
        })
    ) == "tomsk"
    assert gateway._question_answer(
        gateway._ASK_RESULT_PREFIX + json.dumps({"text": "IANA", "unexpected": True})
    ) is None


def test_mixed_web_turn_waits_for_answer_then_searches():
    gateway = _load_gateway()
    messages = [gateway.Message(role="user", content="WEB_E2E_MIXED")]
    first = gateway._web_response(messages)
    assert first is not None and '"ask_user"' in first
    messages.append(gateway.Message(role="user", content=gateway._ASK_RESULT_PREFIX + json.dumps({"tool": "ask_user", "output": {"text": "IANA"}})))
    result = gateway._web_response(messages)
    assert result is not None
    assert json.loads(result.removeprefix("<tool_call>").removesuffix("</tool_call>")) == {
        "tool": "web_search", "args": {"query": "IANA reserved domains"}}


def _tool_result(tool: str, *, source: str | None = None, error: str | None = None) -> str:
    payload: dict[str, Any] = {
        "kind": "untrusted_web_data",
        "tool": tool,
        "source_ids": [source] if source else [],
        "text": json.dumps(
            {"error": error}
            if error
            else (
                {"results": [{"url": "http://93.184.216.2/page"}]}
                if tool == "web_search"
                else {"sourceId": source, "text": "fixture"}
            )
        ),
    }
    return f"Tool result for {tool}: {json.dumps(payload)}"


def test_multi_agent_question_node_waits_then_uses_web_while_sibling_finishes() -> None:
    gateway = _load_gateway()
    initial = gateway.Message(role="user", content=(
        "Goal: User request: research\n\nNode instruction:\n"
        "MULTI_AGENT_NODE:alpha\nMULTI_AGENT_WEB:question"
    ))
    alpha = [initial]
    first = gateway._multi_agent_response(alpha)
    assert first is not None and '"ask_user"' in first

    wrong_answer = [
        initial,
        gateway.Message(role="user", content=gateway._ASK_RESULT_PREFIX + json.dumps({
            "text": "different topic", "optionId": None,
        })),
    ]
    rejected = gateway._multi_agent_response(wrong_answer)
    assert rejected == "MULTI_AGENT_WEB_CHILD:alpha ANSWER:REJECTED"
    assert "<tool_call>" not in rejected

    alpha.append(gateway.Message(role="user", content=gateway._ASK_RESULT_PREFIX + json.dumps({
        "text": "IANA", "optionId": None,
    })))
    search = gateway._multi_agent_response(alpha)
    assert search is not None
    assert json.loads(search.removeprefix("<tool_call>").removesuffix("</tool_call>")) == {
        "tool": "web_search", "args": {"query": "IANA reserved domains"},
    }

    search_source = "11111111-1111-4111-8111-111111111111"
    read_source = "22222222-2222-4222-8222-222222222222"
    alpha.append(gateway.Message(role="user", content=_tool_result("web_search", source=search_source)))
    read = gateway._multi_agent_response(alpha)
    assert read is not None
    assert json.loads(read.removeprefix("<tool_call>").removesuffix("</tool_call>"))["tool"] == "web_read"
    alpha.append(gateway.Message(role="user", content=_tool_result("web_read", source=read_source)))
    assert gateway._multi_agent_response(alpha) == (
        "MULTI_AGENT_WEB_CHILD:alpha ANSWER:IANA"
        f" [source:{search_source}] [source:{read_source}]"
    )

    beta = [gateway.Message(role="user", content=(
        "Goal: User request: research\n\nNode instruction:\n"
        "MULTI_AGENT_NODE:beta\nMULTI_AGENT_WEB:sibling"
    ))]
    assert '"web_search"' in (gateway._multi_agent_response(beta) or "")


def test_multi_agent_shared_budget_requests_six_plus_five_searches() -> None:
    gateway = _load_gateway()
    for node, count in (("alpha", 6), ("beta", 5)):
        messages = [gateway.Message(role="user", content=(
            f"Goal: Node instruction:\nMULTI_AGENT_NODE:{node}\n"
            f"MULTI_AGENT_WEB:budget-{count}"
        ))]
        response = gateway._multi_agent_response(messages)
        assert response is not None
        assert response.count('"tool":"web_search"') == count

    messages.append(gateway.Message(role="user", content=_tool_result("web_search", error="web_limit")))
    assert gateway._multi_agent_response(messages) == "MULTI_AGENT_BUDGET_CHILD:beta:web_limit"


def test_multi_agent_loop_context_requires_only_previous_iteration() -> None:
    gateway = _load_gateway()
    first_draft = [gateway.Message(
        role="user",
        content="Goal: Node instruction:\nMULTI_AGENT_LOOP_CONTEXT:draft\nContext: {}",
    )]
    assert gateway._multi_agent_response(first_draft) == "draft-v1"

    second_draft = [gateway.Message(
        role="user",
        content=(
            "Goal: Node instruction:\nMULTI_AGENT_LOOP_CONTEXT:draft\nContext: "
            "{'previous_iteration': {'iteration': 1, 'node_results': ["
            "{'node_key': 'draft', 'output': 'draft-v1'}, "
            "{'node_key': 'review', 'output': 'review-v1'}]}}"
        ),
    )]
    assert gateway._multi_agent_response(second_draft) == "draft-v2-from-draft-v1"

    synthesis = [gateway.Message(
        role="user",
        content=(
            "Synthesize the final answer to the original user request "
            "draft-v2-from-draft-v1 review-v2"
        ),
    )]
    assert gateway._multi_agent_response(synthesis) == (
        "MULTI_AGENT_LOOP_CONTEXT_FINAL:draft-v2-from-draft-v1"
    )


def test_multi_agent_shared_question_budget_requests_six_plus_five_and_handles_limit() -> None:
    gateway = _load_gateway()
    alpha = [gateway.Message(role="user", content=(
        "Goal: Node instruction:\nMULTI_AGENT_NODE:alpha\n"
        "MULTI_AGENT_QUESTIONS:6"
    ))]
    for index in range(5):
        response = gateway._multi_agent_response(alpha)
        assert response is not None and '"tool":"ask_user"' in response
        assert f"Question {index + 1} for alpha?" in response
        alpha.append(gateway.Message(
            role="user",
            content=gateway._ASK_RESULT_PREFIX + json.dumps({
                "text": f"alpha-{index + 1}", "optionId": None,
            }),
        ))
    sixth = gateway._multi_agent_response(alpha)
    assert sixth is not None and "Question 6 for alpha?" in sixth
    alpha.append(gateway.Message(
        role="user", content="Tool result for ask_user: user_input_limit"
    ))
    assert gateway._multi_agent_response(alpha) == (
        "MULTI_AGENT_QUESTION_BUDGET_CHILD:alpha:user_input_limit"
    )

    beta = [gateway.Message(role="user", content=(
        "Goal: Node instruction:\nMULTI_AGENT_NODE:beta\n"
        "MULTI_AGENT_QUESTIONS:5"
    ))]
    for index in range(5):
        response = gateway._multi_agent_response(beta)
        assert response is not None and f"Question {index + 1} for beta?" in response
        beta.append(gateway.Message(
            role="user",
            content=gateway._ASK_RESULT_PREFIX + json.dumps({
                "text": f"beta-{index + 1}", "optionId": None,
            }),
        ))
    assert gateway._multi_agent_response(beta) == "MULTI_AGENT_QUESTION_BUDGET_CHILD:beta:ok"


def test_multi_agent_question_budget_synthesis_reports_the_shared_limit() -> None:
    gateway = _load_gateway()
    messages = [gateway.Message(role="user", content=(
        "Goal: Synthesize the final answer to the original user request.\n"
        "Context: {'node_outputs': ["
        "{'output': 'MULTI_AGENT_QUESTION_BUDGET_CHILD:alpha:user_input_limit'}, "
        "{'output': 'MULTI_AGENT_QUESTION_BUDGET_CHILD:beta:ok'}]}"
    ))]
    assert gateway._multi_agent_response(messages) == (
        "MULTI_AGENT_QUESTION_BUDGET_FINAL:user_input_limit"
    )


def test_multi_agent_synthesis_cites_only_child_sources() -> None:
    gateway = _load_gateway()
    first = "11111111-1111-4111-8111-111111111111"
    second = "22222222-2222-4222-8222-222222222222"
    messages = [gateway.Message(role="user", content=(
        "Goal: Synthesize the final answer to the original user request.\n"
        "Context: {'node_outputs': ["
        f"{{'output': 'MULTI_AGENT_WEB_CHILD:alpha ANSWER:IANA [source:{first}]', 'source_ids': ['{first}']}}, "
        f"{{'output': 'MULTI_AGENT_WEB_CHILD:beta [source:{second}]', 'source_ids': ['{second}']}}]}}"
    ))]
    assert gateway._multi_agent_response(messages) == (
        f"MULTI_AGENT_WEB_FINAL:alpha,beta ANSWER:IANA [source:{first}] [source:{second}]"
    )


def test_multi_agent_revoke_marker_targets_exact_held_upstream_case() -> None:
    gateway = _load_gateway()
    case = "33333333-3333-4333-8333-333333333333"
    messages = [gateway.Message(role="user", content=(
        "Goal: Node instruction:\nMULTI_AGENT_NODE:beta\n"
        f"MULTI_AGENT_WEB:revoke:{case}"
    ))]
    response = gateway._multi_agent_response(messages)
    assert response is not None
    payload = json.loads(response.removeprefix("<tool_call>").removesuffix("</tool_call>"))
    assert payload == {"tool": "web_read", "args": {
        "url": gateway.WEB_FIXTURE_ORIGIN + f"/fault/{case}",
    }}
    messages.append(gateway.Message(role="user", content=_tool_result("web_read", error="web_access_denied")))
    assert gateway._multi_agent_response(messages) == "MULTI_AGENT_REVOKE_CHILD:beta:web_access_denied"


def test_branching_node_failure_is_http_failure_not_successful_assistant_text():
    gateway = _load_gateway()
    messages = [{"role": "user", "content": "Node instructions:\nMULTI_AGENT_FAIL:alpha\nMULTI_AGENT_NODE:alpha"}]
    status, _, body = _request(gateway.app, "POST", "/v1/chat/completions", {"messages": messages})
    assert status == 400
    assert json.loads(body) == {"detail": "Deterministic node failure"}
    for content in ("MULTI_AGENT_NODE:beta", "Discuss MULTI_AGENT_FAIL:alpha", "Synthesize the final answer to the original user request\nMULTI_AGENT_FAIL:alpha"):
        status, _, _ = _request(gateway.app, "POST", "/v1/chat/completions", {"messages": [{"role": "user", "content": content}]})
        assert status == 200


def _request(
    app: Any,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    async def invoke() -> tuple[int, dict[str, str], bytes]:
        body = json.dumps(payload).encode() if payload is not None else b""
        request_sent = False
        response_complete = asyncio.Event()
        sent: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            await response_complete.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)
            if message["type"] == "http.response.body" and not message.get(
                "more_body", False
            ):
                response_complete.set()

        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": method,
                "scheme": "http",
                "path": path,
                "raw_path": path.encode(),
                "query_string": b"",
                "headers": [(b"content-type", b"application/json")],
                "client": ("test", 1234),
                "server": ("test", 80),
            },
            receive,
            send,
        )
        start = next(item for item in sent if item["type"] == "http.response.start")
        headers = {
            key.decode().lower(): value.decode()
            for key, value in start.get("headers", [])
        }
        response_body = b"".join(
            item.get("body", b"")
            for item in sent
            if item["type"] == "http.response.body"
        )
        return int(start["status"]), headers, response_body

    return asyncio.run(invoke())


def _completion(
    app: Any, messages: list[dict[str, str]]
) -> tuple[str, list[dict[str, Any]]]:
    status, headers, body = _request(
        app,
        "POST",
        "/v1/chat/completions",
        {"model": "k8s-test", "messages": messages, "stream": True},
    )
    assert status == 200
    assert headers["content-type"].startswith("text/event-stream")

    frames = [frame for frame in body.decode().split("\n\n") if frame]
    assert frames[-1] == "data: [DONE]"
    chunks = [json.loads(frame.removeprefix("data: ")) for frame in frames[:-1]]
    assert chunks
    assert all(frame.startswith("data: ") for frame in frames)
    assert all(chunk["object"] == "chat.completion.chunk" for chunk in chunks)
    assert all(len(chunk["choices"]) == 1 for chunk in chunks)
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    content = "".join(
        chunk["choices"][0].get("delta", {}).get("content", "") for chunk in chunks
    )
    return content, chunks


def test_routing_gateway_selects_candidate_and_supports_negative_classifier_outputs():
    gateway = _load_gateway()
    system = {
        "role": "system",
        "content": "You route chat turns to background multi-agent workflows. Available "
        f"workflows:\n- id={MCP_SKILL_ID} name=owned-source: routing fixture",
    }
    for prompt, expected in (
        (f"ROUTING_SELECT:{MCP_SKILL_ID}", f'<workflow_call>{{"workflow_id":"{MCP_SKILL_ID}"}}</workflow_call>'),
        ("ROUTING_MALFORMED", '<workflow_call>{"workflow_id":'),
        (f"ROUTING_UNAVAILABLE:{CODE_SKILL_ID}", f'<workflow_call>{{"workflow_id":"{CODE_SKILL_ID}"}}</workflow_call>'),
        (f"ROUTING_SELECT:{CODE_SKILL_ID}", ""),
    ):
        content, _ = _completion(gateway.app, [system, {"role": "user", "content": prompt}])
        assert content == expected


def test_routing_marker_in_ordinary_chat_does_not_emit_a_workflow_call():
    gateway = _load_gateway()
    content, _ = _completion(gateway.app, [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": f"ROUTING_SELECT:{MCP_SKILL_ID}"},
    ])
    assert content == "ROUTING_CHAT_FALLBACK"


def _skills_prompt() -> str:
    return f"""You have access to the following tools.
Available tools:
- id={MCP_SKILL_ID} name=smoke-test-sum: Returns a sum args_schema={{"a": 1, "b": 2}}
- id={CODE_SKILL_ID} name=smoke-test-multistep: Computes in a sandbox args_schema={{"task": "..."}}
- id={FETCH_SKILL_ID} name=smoke-test-fetch: Fetches a URL args_schema={{"url": "..."}}
- id={GREET_SKILL_ID} name=smoke-test-greet: Greets in a sandbox args_schema={{"task": "..."}}
"""


def test_health_and_models_are_available() -> None:
    app = _load_gateway().app

    health_status, _, health_body = _request(app, "GET", "/health")
    models_status, _, models_body = _request(app, "GET", "/v1/models")

    assert health_status == 200
    assert json.loads(health_body) == {"status": "ok"}
    assert models_status == 200
    assert [item["id"] for item in json.loads(models_body)["data"]] == ["k8s-test"]


def test_models_endpoint_uses_configured_model_id() -> None:
    with patch.dict(os.environ, {"TEST_GATEWAY_MODEL_ID": "gpt-4o-mini"}):
        app = _load_gateway().app

    status, _, body = _request(app, "GET", "/v1/models")

    assert status == 200
    assert [item["id"] for item in json.loads(body)["data"]] == ["gpt-4o-mini"]


def test_ordinary_prompt_yields_assistant_delta() -> None:
    app = _load_gateway().app

    content, _ = _completion(
        app,
        [{"role": "user", "content": "Hello from ordinary Kubernetes smoke test"}],
    )

    assert "deterministic Kubernetes" in content


def test_multi_agent_preparation_returns_briefs_for_exact_schema_keys() -> None:
    app = _load_gateway().app
    schema = {
        "type": "object",
        "properties": {"beta": {"type": "string"}, "alpha": {"type": "string"}},
        "required": ["beta", "alpha"],
        "additionalProperties": False,
    }

    content, _ = _completion(
        app,
        [
            {
                "role": "system",
                "content": (
                    "When the goal is complete, respond with your final answer as a "
                    "valid JSON object matching this schema:\n" + json.dumps(schema)
                ),
            },
            {
                "role": "user",
                "content": "Goal: Prepare one concise execution brief for every workflow node.",
            },
        ],
    )

    assert json.loads(content) == {
        "alpha": "MULTI_AGENT_BRIEF:alpha",
        "beta": "MULTI_AGENT_BRIEF:beta",
    }


def test_multi_agent_node_and_synthesis_markers_have_precedence() -> None:
    app = _load_gateway().app

    node_content, _ = _completion(
        app,
        [
            {"role": "system", "content": _skills_prompt()},
            {
                "role": "user",
                "content": (
                    "Goal: User request: use sum\n"
                    "Node instruction: MULTI_AGENT_NODE:beta"
                ),
            },
        ],
    )
    synthesis_content, _ = _completion(
        app,
        [
            {"role": "system", "content": "supervisor"},
            {
                "role": "user",
                "content": (
                    "Goal: Synthesize the final answer to the original user request.\n"
                    "Context: outputs MULTI_AGENT_NODE_RESULT:beta then "
                    "MULTI_AGENT_NODE_RESULT:alpha"
                ),
            },
        ],
    )

    assert node_content == "MULTI_AGENT_NODE_RESULT:beta"
    assert synthesis_content == "MULTI_AGENT_FINAL:alpha,beta"


def test_multi_agent_barrier_marker_is_extracted_without_prompt_echo() -> None:
    gateway = _load_gateway()
    messages = [
        gateway.Message(
            role="user",
            content=(
                "Goal: Node instruction: MULTI_AGENT_NODE:gated "
                "MULTI_AGENT_BARRIER:revoke"
            ),
        )
    ]

    assert gateway._barrier_key(messages) == "revoke"
    assert "Goal:" not in gateway._barrier_key(messages)


def test_multi_agent_barrier_delays_the_stream_by_configured_duration() -> None:
    gateway = _load_gateway()

    async def scenario() -> tuple[list[float], str]:
        entered = asyncio.Event()
        released = asyncio.Event()
        delays: list[float] = []

        async def controlled_sleep(delay: float) -> None:
            delays.append(delay)
            entered.set()
            await released.wait()

        request = gateway.CompletionRequest(
            model="gpt-4o-mini",
            messages=[
                gateway.Message(
                    role="user",
                    content=(
                        "Goal: Node instruction: MULTI_AGENT_NODE:alpha "
                        "MULTI_AGENT_BARRIER:workflow-ui"
                    ),
                )
            ],
            stream=True,
        )
        with (
            patch.dict(os.environ, {"TEST_GATEWAY_BARRIER_SECONDS": "8"}),
            patch.object(gateway.asyncio, "sleep", side_effect=controlled_sleep),
        ):
            response_task = asyncio.create_task(gateway.chat_completions(request))
            await entered.wait()
            assert not response_task.done()
            released.set()
            response = await response_task
            chunks = [chunk async for chunk in response.body_iterator]
        body = "".join(
            chunk.decode() if isinstance(chunk, bytes) else chunk for chunk in chunks
        )
        return delays, body

    delays, body = asyncio.run(scenario())

    assert delays == [8.0]
    assert "MULTI_AGENT_NODE_RESULT:alpha" in body
    assert "data: [DONE]" in body


def test_same_request_yields_identical_sse_chunks() -> None:
    app = _load_gateway().app
    messages = [{"role": "user", "content": "repeatable request"}]

    _, first = _completion(app, messages)
    _, second = _completion(app, messages)

    assert first == second


def test_outer_mcp_prompt_calls_uuid_from_skills_prompt() -> None:
    app = _load_gateway().app

    content, _ = _completion(
        app,
        [
            {"role": "system", "content": _skills_prompt()},
            {"role": "user", "content": "What is 17 plus 25? Use smoke-test-sum."},
        ],
    )

    assert f'"skill_id":"{MCP_SKILL_ID}"' in content
    assert '"a":17' in content
    assert '"b":25' in content
    assert content.startswith("<tool_call>") and content.endswith("</tool_call>")


def test_mcp_result_containing_42_yields_final_42() -> None:
    app = _load_gateway().app

    content, _ = _completion(
        app,
        [
            {"role": "system", "content": _skills_prompt()},
            {"role": "user", "content": "What is 17 plus 25? Use smoke-test-sum."},
            {
                "role": "assistant",
                "content": (
                    f'<tool_call>{{"skill_id":"{MCP_SKILL_ID}","args":{{"a":17,"b":25}}}}'
                    "</tool_call>"
                ),
            },
            {
                "role": "user",
                "content": f"Tool result for {MCP_SKILL_ID}: {{'output': {{'result': 42}}}}",
            },
        ],
    )

    assert content == "42"


def test_fetch_result_uses_configured_fixture_title() -> None:
    configured_title = "AurumAI Business Backend"
    with patch.dict(
        os.environ,
        {"TEST_GATEWAY_FETCH_TITLE": configured_title},
    ):
        app = _load_gateway().app

    content, _ = _completion(
        app,
        [
            {"role": "system", "content": _skills_prompt()},
            {
                "role": "user",
                "content": "Fetch http://backend:8000/openapi.json with smoke-test-fetch.",
            },
            {
                "role": "assistant",
                "content": (
                    f'<tool_call>{{"skill_id":"{FETCH_SKILL_ID}","args":'
                    '{"url":"http://backend:8000/openapi.json"}}</tool_call>'
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Tool result for {FETCH_SKILL_ID}: "
                    f'{{"output":{{"result":{{"isError":false,"content":'
                    f'"{configured_title}"}}}}}}}}'
                ),
            },
        ],
    )

    assert content == f"FETCH FIXTURE VERIFIED: {configured_title}"


def test_outer_code_prompt_calls_code_skill_uuid() -> None:
    app = _load_gateway().app

    content, _ = _completion(
        app,
        [
            {"role": "system", "content": _skills_prompt()},
            {
                "role": "user",
                "content": "Use smoke-test-multistep to compute the two-step result.",
            },
        ],
    )

    assert f'"skill_id":"{CODE_SKILL_ID}"' in content
    assert '"task":"compute the two-step result"' in content
    assert content.startswith("<tool_call>") and content.endswith("</tool_call>")


def test_first_code_round_yields_first_command() -> None:
    app = _load_gateway().app

    content, _ = _completion(
        app,
        [
            {
                "role": "system",
                "content": "You are running a code skill inside an isolated sandbox container.",
            },
            {"role": "user", "content": "compute the two-step result"},
        ],
    )

    assert (
        content
        == '<run_command>{"command":"python3 -c \\"print(6 * 7)\\""}</run_command>'
    )


def test_stdout_42_yields_second_command() -> None:
    app = _load_gateway().app

    content, _ = _completion(
        app,
        [
            {
                "role": "system",
                "content": "You are running a code skill inside an isolated sandbox container.",
            },
            {"role": "user", "content": "compute the two-step result"},
            {
                "role": "assistant",
                "content": '<run_command>{"command":"python3 -c \\"print(6 * 7)\\""}</run_command>',
            },
            {
                "role": "user",
                "content": (
                    'Command: python3 -c "print(6 * 7)"\nstdout: 42\n'
                    "stderr: \nexit_code: 0"
                ),
            },
        ],
    )

    assert (
        content
        == '<run_command>{"command":"python3 -c \\"print(42 + 100)\\""}</run_command>'
    )


def test_stdout_142_yields_final_result() -> None:
    app = _load_gateway().app

    content, _ = _completion(
        app,
        [
            {
                "role": "system",
                "content": "You are running a code skill inside an isolated sandbox container.",
            },
            {"role": "user", "content": "compute the two-step result"},
            {
                "role": "assistant",
                "content": '<run_command>{"command":"python3 -c \\"print(42 + 100)\\""}</run_command>',
            },
            {
                "role": "user",
                "content": (
                    'Command: python3 -c "print(42 + 100)"\nstdout: 142\n'
                    "stderr: \nexit_code: 0"
                ),
            },
        ],
    )

    assert content == "FINAL RESULT: 142"


def test_nonzero_code_result_does_not_advance() -> None:
    app = _load_gateway().app

    content, _ = _completion(
        app,
        [
            {
                "role": "system",
                "content": "You are running a code skill inside an isolated sandbox container.",
            },
            {
                "role": "user",
                "content": "stdout: 42\nstderr: failed\nexit_code: 7",
            },
        ],
    )

    assert content == "CODE SKILL ERROR: command failed"


def test_timed_out_code_result_does_not_finish() -> None:
    app = _load_gateway().app

    content, _ = _completion(
        app,
        [
            {
                "role": "system",
                "content": "You are running a code skill inside an isolated sandbox container.",
            },
            {
                "role": "user",
                "content": "stdout: 142\nstderr: \nexit_code: 0 (TIMED OUT)",
            },
        ],
    )

    assert content == "CODE SKILL ERROR: command timed out"


def test_fetch_and_greet_smoke_routes_are_request_driven() -> None:
    app = _load_gateway().app

    fetch_call, _ = _completion(
        app,
        [
            {"role": "system", "content": _skills_prompt()},
            {
                "role": "user",
                "content": "Use smoke-test-fetch to fetch http://backend:8000/openapi.json.",
            },
        ],
    )
    greet_call, _ = _completion(
        app,
        [
            {"role": "system", "content": _skills_prompt()},
            {"role": "user", "content": "Call smoke-test-greet and greet World."},
        ],
    )

    assert f'"skill_id":"{FETCH_SKILL_ID}"' in fetch_call
    assert '"url":"http://backend:8000/openapi.json"' in fetch_call
    assert f'"skill_id":"{GREET_SKILL_ID}"' in greet_call


def test_greet_code_round_and_syntheses_are_deterministic() -> None:
    app = _load_gateway().app
    code_system = (
        "You are running a code skill inside an isolated sandbox container.\n"
        "# Greet skill\nRun python scripts/greet.py."
    )

    command, _ = _completion(
        app,
        [
            {"role": "system", "content": code_system},
            {"role": "user", "content": "Greet someone named World"},
        ],
    )
    inner_final, _ = _completion(
        app,
        [
            {"role": "system", "content": code_system},
            {
                "role": "user",
                "content": "stdout: Hello, World!\nstderr: \nexit_code: 0",
            },
        ],
    )
    fetch_final, _ = _completion(
        app,
        [
            {"role": "system", "content": _skills_prompt()},
            {"role": "user", "content": "Fetch the fixture title it reports."},
            {
                "role": "user",
                "content": (
                    "Tool result for fetch: {'output': {'result': {"
                    "'content': [{'type': 'text', 'text': '"
                    + FETCH_FIXTURE_TITLE
                    + "'}], "
                    "'isError': False}}}"
                ),
            },
        ],
    )

    assert (
        command
        == '<run_command>{"command":"python scripts/greet.py \\"World\\""}</run_command>'
    )
    assert inner_final == "Hello, World!"
    assert fetch_final == FETCH_FINAL_MARKER


def test_fetch_synthesis_rejects_failed_latest_result_even_if_prompt_has_title() -> None:
    app = _load_gateway().app

    content, _ = _completion(
        app,
        [
            {"role": "system", "content": _skills_prompt()},
            {
                "role": "user",
                "content": f"Fetch the title {FETCH_FIXTURE_TITLE} from the fixture.",
            },
            {
                "role": "user",
                "content": (
                    "Tool result for fetch: {'output': {'result': {"
                    "'content': [{'type': 'text', 'text': 'connection failed after "
                    + FETCH_FIXTURE_TITLE
                    + "'}], "
                    "'isError': True}}}"
                ),
            },
        ],
    )

    assert content == "FETCH FIXTURE VALIDATION FAILED"
    assert "FETCH FIXTURE VERIFIED:" not in content


def test_fetch_synthesis_uses_only_latest_tool_result() -> None:
    app = _load_gateway().app

    content, _ = _completion(
        app,
        [
            {"role": "system", "content": _skills_prompt()},
            {"role": "user", "content": "Fetch the fixture."},
            {
                "role": "user",
                "content": (
                    "Tool result for fetch: {'output': {'result': {"
                    "'content': [{'text': '"
                    + FETCH_FIXTURE_TITLE
                    + "'}], 'isError': False}}}"
                ),
            },
            {
                "role": "user",
                "content": "Tool result for fetch: {'output': 'request failed'}",
            },
        ],
    )

    assert content == "FETCH FIXTURE VALIDATION FAILED"
    assert "FETCH FIXTURE VERIFIED:" not in content
