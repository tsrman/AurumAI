import json
import logging
from typing import Any, cast

import httpx
from opentelemetry import trace
from temporalio import activity
from temporalio.exceptions import ApplicationError

from src.config import settings
from src.llm_client import llm_client, raise_non_retryable_on_4xx
from src.event_publisher import event_publisher
from src.state_manager import state_manager
from src.langfuse_client import get_langfuse, sanitize_messages, sanitize_content
from src.workflows.builtin_tools import (
    AnswerRequest,
    ExecutionContext,
    ExecutionPhase,
    QuestionRequest,
    image_tool_context,
    web_tool_context,
)

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

TOOL_CALL_TAG = "<tool_call>"
UNSUPPORTED_TOOL_TAGS = ("<\uff5cDSML\uff5c", "<\uff5c\uff5cDSML\uff5c\uff5c")


class _LLMGatewayError(Exception):
    """A real upstream failure reported inside the SSE body (see llm_call's
    error-chunk handling) — must propagate out of the parsing loop's broad
    except-and-continue, not be swallowed as a malformed chunk."""


class _UnsupportedToolFormat(_LLMGatewayError):
    def __init__(self, response_text: str) -> None:
        super().__init__("Model emitted an unsupported tool-call format; use <tool_call> JSON")
        self.response_text = response_text


def _holdback_length(text: str, tag: str) -> int:
    """Length of the longest suffix of `text` that's a proper prefix of
    `tag` — the part that must stay buffered because more chunks could still
    grow it into the full tag. 0 once `text`'s tail can no longer become
    `tag` (e.g. it ends in a character `tag` doesn't start with)."""
    for k in range(min(len(text), len(tag) - 1), 0, -1):
        if text.endswith(tag[:k]):
            return k
    return 0


@activity.defn
@tracer.start_as_current_span("llm_call")
async def llm_call(
    request: dict[str, Any],
    turn_id: str | None = None,
    publish_events: bool = True,
    hide_tool_call_tag: bool = False,
) -> str:
    """`publish_events=False` fully suppresses this call's output (both
    reasoning and content) — used for internal, non-chat-facing calls that
    must never appear in the conversation's SSE stream (EvaluationWorkflow's
    judge call, CodeSkillSessionWorkflow's sub-loop). `hide_tool_call_tag`
    additionally rejects unsupported tool markup, with one internal format
    repair attempt. ConversationWorkflow's skills planning call wants reasoning and
    (almost all) content to stream live, just with a `<tool_call>` tag (and
    anything after it) held back so the user never sees the raw
    skill-selection protocol."""
    try:
        return await _stream_completion(request, turn_id, publish_events, hide_tool_call_tag)
    except _UnsupportedToolFormat as exc:
        # One format repair, without executing or parsing the rejected
        # protocol. Keep it internal and let the common parser validate the
        # returned text. A second invalid response fails the activity.
        repair_request = {
            **request,
            "messages": [
                *request.get("messages", []),
                {"role": "assistant", "content": exc.response_text},
                {"role": "user", "content": (
                    "Your previous tool call was not executed: DSML is unsupported. "
                    "Re-emit the same invocation JSON as plain text between literal ASCII "
                    "<tool_call> and </tool_call> tags. Follow the system instructions for "
                    "skill_id or built-in tool and args fields; preserve the same offered "
                    "tool and arguments. Do not emit DSML, native invocation markup, "
                    "or Markdown fences."
                )},
            ],
        }
        return await _stream_completion(repair_request, turn_id, False, hide_tool_call_tag)


async def _stream_completion(
    request: dict[str, Any],
    turn_id: str | None,
    publish_events: bool,
    hide_tool_call_tag: bool,
) -> str:
    trace.get_current_span().set_attribute("conversation_id", settings.conversation_id or "")
    langfuse = get_langfuse()
    generation = None
    if langfuse:
        generation = langfuse.generation(
            trace_id=settings.conversation_id,
            name="llm_call",
            model=request.get("model"),
            input={"messages": sanitize_messages(request.get("messages", []))},
            model_parameters={
                "temperature": request.get("temperature"),
                "max_tokens": request.get("max_tokens"),
            },
            metadata={
                "workflow_run_id": request.get("workflow_run_id"),
                "workflow_phase": request.get("workflow_phase"),
                "workflow_node_key": request.get("workflow_node_key"),
            }
            if request.get("workflow_run_id") is not None
            else None,
        )
    full = ""
    # Content not yet published while hide_tool_call_tag is active —
    # everything streams live except a trailing sliver that might still be
    # growing into "<tool_call>", so we never flash a partial "<tool_c" on
    # screen only to yank it back. Once a full tag is confirmed, `suppressed`
    # latches True and nothing more from this call reaches the user — the
    # outer workflow either replaces this response with a tool-result
    # synthesis, or (if extraction finds nothing after all — e.g. malformed
    # JSON in the tag) the true final text still reaches the UI via the
    # message-end event, just without having streamed live for that rare case.
    pending = ""
    suppressed = False
    async for chunk in llm_client.stream_chat(request):
        chunk = chunk.strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            data = json.loads(chunk)
            # business-backend's /v1/chat/completions yields this shape when
            # the real gateway fails mid-stream — a
            # bare "no choices -> continue" silently treated a genuine
            # upstream failure as an empty completion instead of a real
            # error the activity's retry_policy (H7) could act on.
            error = data.get("error")
            if error:
                raise _LLMGatewayError(f"LLM gateway error: {error.get('message', error)}")
            choices = data.get("choices") or []
            if not choices:
                continue
            message_delta = choices[0].get("delta", {})
            reasoning = message_delta.get("reasoning", "")
            if reasoning and publish_events:
                await event_publisher.publish("thinking-delta", {"delta": reasoning, "turn_id": turn_id})
            delta = message_delta.get("content", "")
            if delta:
                full += delta
                if not publish_events:
                    pass
                elif not hide_tool_call_tag:
                    await event_publisher.publish("content-delta", {"delta": delta, "turn_id": turn_id})
                elif not suppressed:
                    pending += delta
                    indexes = [pending.find(tag) for tag in (TOOL_CALL_TAG, *UNSUPPORTED_TOOL_TAGS)]
                    tag_idx = min((idx for idx in indexes if idx != -1), default=-1)
                    if tag_idx != -1:
                        suppressed = True
                        safe = pending[:tag_idx]
                        pending = ""
                        if safe:
                            await event_publisher.publish(
                                "content-delta", {"delta": safe, "turn_id": turn_id}
                            )
                    else:
                        hold = max(_holdback_length(pending, tag)
                                   for tag in (TOOL_CALL_TAG, *UNSUPPORTED_TOOL_TAGS))
                        safe = pending[: len(pending) - hold] if hold else pending
                        pending = pending[len(pending) - hold :] if hold else ""
                        if safe:
                            await event_publisher.publish(
                                "content-delta", {"delta": safe, "turn_id": turn_id}
                            )
        except _LLMGatewayError:
            raise
        except Exception as exc:
            logger.warning("Failed to process LLM chunk: %s", exc, exc_info=True)
            continue
    if publish_events and hide_tool_call_tag and pending and not suppressed:
        await event_publisher.publish("content-delta", {"delta": pending, "turn_id": turn_id})
    if generation:
        generation.end(output={"completion": sanitize_content(full)})
    if not full.strip():
        # A clean SSE EOF, reasoning-only reply, or filtered completion is
        # not a successful answer. Let the workflow's bounded activity retry
        # and terminal failure path handle it instead of persisting a blank.
        raise _LLMGatewayError("LLM returned no answer content")
    if hide_tool_call_tag and any(tag in full for tag in UNSUPPORTED_TOOL_TAGS):
        # Reject provider-native markup; only the shared tool_call parser
        # may select tools. Never persist or expose this internal payload.
        raise _UnsupportedToolFormat(full)
    return full


@activity.defn
@tracer.start_as_current_span("load_conversation_state")
async def load_conversation_state() -> list[dict[str, Any]]:
    trace.get_current_span().set_attribute("conversation_id", settings.conversation_id or "")
    await state_manager.load()
    return state_manager.messages


@activity.defn
@tracer.start_as_current_span("get_session_token")
async def get_session_token() -> str | None:
    # Workflow code can't read settings.session_token directly and see the
    # value set at runtime by POST /bind (pool mode) — it only sees whatever
    # was there when the workflow sandbox last imported the module. Activities
    # run outside that sandbox and always see the live value.
    return settings.session_token


@activity.defn
async def get_evaluator_config() -> dict[str, Any]:
    """Record the explicit operator policy outside the workflow sandbox."""
    return {"enabled": settings.evaluator_enabled, "model": settings.evaluator_model.strip()}


@activity.defn
@tracer.start_as_current_span("get_conversation_id")
async def get_conversation_id() -> str | None:
    # Same pool-mode rationale as get_session_token above — settings.
    # conversation_id is also only set at runtime via POST /bind.
    return settings.conversation_id


@activity.defn
@tracer.start_as_current_span("list_available_skills")
async def list_available_skills() -> list[dict[str, Any]]:
    trace.get_current_span().set_attribute("conversation_id", settings.conversation_id or "")
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            f"{settings.llm_gateway_url}/v1/skills/",
            headers={"Authorization": f"Bearer {settings.session_token}"},
            # Surfaces this conversation's own hidden execute_hybrid_search
            # skill (chat file attachments), if it has one — the backend
            # only honors this when it matches the session token's own
            # conversation_id claim, so it can't leak another conversation's.
            params={"conversation_id": settings.conversation_id or ""},
        )
        raise_non_retryable_on_4xx(response)
        return cast(list[dict[str, Any]], response.json().get("data", []))


@activity.defn
@tracer.start_as_current_span("get_conversation_temperature")
async def get_conversation_temperature() -> float:
    # Re-fetched every turn (not cached at workflow start like state_manager's
    # message history) so a conversation-level override set mid-conversation
    # takes effect on the very next turn without needing a workflow restart.
    # The response's `temperature` is already resolved server-side (override
    # -> organization default -> platform default) — this activity never
    # needs to know about that cascade.
    trace.get_current_span().set_attribute("conversation_id", settings.conversation_id or "")
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            f"{settings.llm_gateway_url}/v1/conversations/{settings.conversation_id}",
            headers={"Authorization": f"Bearer {settings.session_token}"},
        )
        raise_non_retryable_on_4xx(response)
        return cast(float, response.json()["data"]["temperature"])


def _raise_workflow_api_error(response: httpx.Response) -> None:
    if not response.is_error:
        return
    status_code = response.status_code
    if status_code in {403, 404}:
        code = "access_revoked"
    elif status_code == 413:
        code = "output_limit_exceeded"
    elif status_code == 429:
        code = "quota_exhausted"
    elif status_code == 409:
        try:
            payload = response.json()
            detail = payload.get("detail")
            candidate = detail.get("code") if isinstance(detail, dict) else None
            if not isinstance(candidate, str):
                error = payload.get("error")
                message = error.get("message") if isinstance(error, dict) else None
                candidate = message.get("code") if isinstance(message, dict) else None
        except (ValueError, AttributeError):
            candidate = None
        code = (
            str(candidate)
            if isinstance(candidate, str) and candidate in {
                "user_input_disabled",
                "user_input_limit",
                "user_input_cancelled",
            }
            else "workflow_unavailable"
        )
    elif 400 <= status_code < 500:
        code = "workflow_unavailable"
    else:
        response.raise_for_status()
        return
    raise ApplicationError(code, type=code, non_retryable=True)


async def _workflow_api_request(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> Any:
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.request(
            method,
            f"{settings.llm_gateway_url}{path}",
            headers={"Authorization": f"Bearer {settings.session_token}"},
            json=payload,
        )
        _raise_workflow_api_error(response)
        if not response.content:
            return None
        return response.json().get("data")


def _interaction_data(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ApplicationError(
            "workflow_unavailable", type="workflow_unavailable", non_retryable=True
        )
    return cast(dict[str, Any], data)


@activity.defn
@tracer.start_as_current_span("register_interaction_execution")
async def register_interaction_execution(
    turn_id: str,
    workflow_id: str,
    run_id: str,
    task_queue: str,
    phase: ExecutionPhase,
    node_key: str | None,
    agent_definition_id: str | None,
    iteration: int,
) -> ExecutionContext:
    data = _interaction_data(
        await _workflow_api_request(
            "POST",
            f"/v1/internal/turns/{turn_id}/executions",
            {
                "workflowId": workflow_id,
                "runId": run_id,
                "taskQueue": task_queue,
                "phase": phase,
                "nodeKey": node_key,
                "agentDefinitionId": agent_definition_id,
                "iteration": iteration,
            },
        )
    )
    if (
        str(data.get("turnId", "")) != turn_id
        or data.get("phase") != phase
        or data.get("nodeKey") != node_key
        or not data.get("executionId")
    ):
        raise ApplicationError(
            "workflow_unavailable", type="workflow_unavailable", non_retryable=True
        )
    return ExecutionContext(
        turn_id=turn_id,
        execution_id=str(data["executionId"]),
        phase=phase,
        node_key=node_key,
    )


@activity.defn
@tracer.start_as_current_span("create_user_question")
async def create_user_question(
    context: ExecutionContext,
    invocation_id: str,
    question: dict[str, Any],
) -> dict[str, Any]:
    request = QuestionRequest.model_validate(question)
    return _interaction_data(
        await _workflow_api_request(
            "POST",
            f"/v1/internal/executions/{context.execution_id}/questions",
            {
                "invocationId": invocation_id,
                "question": request.model_dump(by_alias=True),
            },
        )
    )


@activity.defn
@tracer.start_as_current_span("read_user_question")
async def read_user_question(question_id: str) -> dict[str, Any]:
    return _interaction_data(
        await _workflow_api_request("GET", f"/v1/internal/questions/{question_id}")
    )


@activity.defn
@tracer.start_as_current_span("consume_user_question")
async def consume_user_question(question_id: str) -> dict[str, Any]:
    data = _interaction_data(
        await _workflow_api_request("POST", f"/v1/internal/questions/{question_id}/consume")
    )
    return AnswerRequest.model_validate(data).model_dump(by_alias=True)


@activity.defn
@tracer.start_as_current_span("expire_user_question")
async def expire_user_question(question_id: str) -> dict[str, Any]:
    return _interaction_data(
        await _workflow_api_request("POST", f"/v1/internal/questions/{question_id}/expire")
    )


@activity.defn
@tracer.start_as_current_span("fail_interaction_turn")
async def fail_interaction_turn(turn_id: str) -> None:
    await _workflow_api_request("POST", f"/v1/internal/turns/{turn_id}/fail")


_WEB_ERROR_CODES = {
    "web_disabled",
    "web_access_denied",
    "web_limit",
    "web_timeout",
    "web_unavailable",
    "web_url_blocked",
    "web_too_large",
    "unsupported_document",
    "web_cancelled",
}


def _raise_web_api_error(response: httpx.Response) -> None:
    if not response.is_error:
        return
    code = "web_unavailable"
    try:
        payload = response.json()
        detail = payload.get("detail")
        candidate = detail.get("code") if isinstance(detail, dict) else None
        if not isinstance(candidate, str):
            error = payload.get("error")
            message = error.get("message") if isinstance(error, dict) else None
            candidate = message.get("code") if isinstance(message, dict) else None
        if isinstance(candidate, str) and candidate in _WEB_ERROR_CODES:
            code = candidate
    except (ValueError, AttributeError):
        pass
    raise ApplicationError(code, type=code, non_retryable=True)


@activity.defn
@tracer.start_as_current_span("execute_web_tool")
async def execute_web_tool(
    context: ExecutionContext,
    invocation_id: str,
    tool: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Execute one durable web operation and return only validated tool data."""
    async with httpx.AsyncClient(timeout=75.0, trust_env=False) as client:
        response = await client.request(
            "POST",
            f"{settings.llm_gateway_url}/v1/internal/turns/{context.turn_id}/"
            f"executions/{context.execution_id}/web-operations/{invocation_id}",
            headers={"Authorization": f"Bearer {settings.session_token}"},
            json={"tool": tool, "args": arguments},
        )
    _raise_web_api_error(response)
    try:
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("invalid web response")
        return web_tool_context(tool, payload)
    except (ValueError, AttributeError) as exc:
        raise ApplicationError(
            "web_unavailable", type="web_unavailable", non_retryable=True
        ) from exc


_IMAGE_ERROR_CODES = {
    "image_disabled",
    "image_access_denied",
    "image_limit",
    "image_timeout",
    "image_unavailable",
    "image_rejected",
    "image_not_found",
    "image_cancelled",
    "image_quota_exceeded",
}


def _raise_image_api_error(response: httpx.Response) -> None:
    if not response.is_error:
        return
    code = "image_unavailable"
    try:
        payload = response.json()
        detail = payload.get("detail")
        candidate = detail.get("code") if isinstance(detail, dict) else None
        if isinstance(candidate, str) and candidate in _IMAGE_ERROR_CODES:
            code = candidate
    except (ValueError, AttributeError):
        pass
    raise ApplicationError(code, type=code, non_retryable=True)


@activity.defn
@tracer.start_as_current_span("execute_image_tool")
async def execute_image_tool(
    context: ExecutionContext,
    invocation_id: str,
    tool: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Execute one durable image operation and return only its id and size."""
    async with httpx.AsyncClient(timeout=330.0, trust_env=False) as client:
        response = await client.request(
            "POST",
            f"{settings.llm_gateway_url}/v1/internal/turns/{context.turn_id}/"
            f"executions/{context.execution_id}/image-operations/{invocation_id}",
            headers={"Authorization": f"Bearer {settings.session_token}"},
            json={"tool": tool, "args": arguments},
        )
    _raise_image_api_error(response)
    try:
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("invalid image response")
        return image_tool_context(tool, payload)
    except (ValueError, AttributeError) as exc:
        raise ApplicationError(
            "image_unavailable", type="image_unavailable", non_retryable=True
        ) from exc


@activity.defn
@tracer.start_as_current_span("load_workflow_run_snapshot")
async def load_workflow_run_snapshot(run_id: str) -> dict[str, Any]:
    data = await _workflow_api_request(
        "GET", f"/v1/internal/workflow-runs/{run_id}/snapshot"
    )
    if not isinstance(data, dict):
        raise ApplicationError(
            "workflow_unavailable", type="workflow_unavailable", non_retryable=True
        )
    return cast(dict[str, Any], data)


@activity.defn
@tracer.start_as_current_span("authorize_workflow_step")
async def authorize_workflow_step(
    run_id: str,
    phase: str,
    node_key: str | None,
) -> None:
    payload: dict[str, Any] = {"phase": phase}
    if node_key is not None:
        payload["nodeKey"] = node_key
    await _workflow_api_request(
        "POST", f"/v1/internal/workflow-runs/{run_id}/authorize-step", payload
    )


@activity.defn
@tracer.start_as_current_span("refresh_runner_activity")
async def refresh_runner_activity(run_id: str) -> None:
    """Refreshes this conversation's runner idle timestamp — called once
    when a node enters waiting_human so a long confirmation wait doesn't
    race RUNNER_IDLE_REAP_SECONDS (see business-backend's touch-activity
    endpoint docstring for the full race description)."""
    await _workflow_api_request(
        "POST", f"/v1/internal/workflow-runs/{run_id}/touch-activity"
    )


@activity.defn
@tracer.start_as_current_span("transition_workflow_run")
async def transition_workflow_run(
    run_id: str,
    status: str,
    error_code: str | None,
) -> None:
    await _workflow_api_request(
        "POST",
        f"/v1/internal/workflow-runs/{run_id}/transition",
        {"status": status, "errorCode": error_code},
    )


@activity.defn
@tracer.start_as_current_span("persist_workflow_node_result")
async def persist_workflow_node_result(
    run_id: str,
    phase_key: str,
    status: str,
    output: str | None,
    error_code: str | None,
) -> None:
    await _workflow_api_request(
        "PUT",
        f"/v1/internal/workflow-runs/{run_id}/nodes/{phase_key}",
        {"status": status, "output": output, "errorCode": error_code},
    )


@activity.defn
@tracer.start_as_current_span("publish_event")
async def publish_event(event_type: str, data: dict[str, Any]) -> None:
    trace.get_current_span().set_attribute("conversation_id", settings.conversation_id or "")
    trace.get_current_span().set_attribute("event_type", event_type)
    langfuse = get_langfuse()
    span = None
    if langfuse:
        try:
            span = langfuse.span(
                trace_id=settings.conversation_id,
                name=f"publish_event:{event_type}",
                input={"event_type": event_type},
            )
        except Exception as exc:
            logger.warning("Langfuse publish_event span failed: %s", exc)
    await event_publisher.publish(event_type, data)
    if span:
        try:
            span.end()
        except Exception as exc:
            logger.warning("Langfuse publish_event span end failed: %s", exc)


@activity.defn
@tracer.start_as_current_span("persist_assistant_message")
async def persist_assistant_message(
    content: str,
    model: str | None = None,
    turn_id: str | None = None,
    tool_calls: list[Any] | None = None,
    tool_results: dict[str, Any] | None = None,
) -> None:
    trace.get_current_span().set_attribute("conversation_id", settings.conversation_id or "")
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{settings.llm_gateway_url}/v1/conversations/{settings.conversation_id}/messages",
            headers={"Authorization": f"Bearer {settings.session_token}"},
            json={
                "role": "assistant",
                "content": content,
                "model": model,
                "turn_id": turn_id,
                "tool_calls": tool_calls,
                "tool_results": tool_results,
            },
        )
        raise_non_retryable_on_4xx(response)


@activity.defn
@tracer.start_as_current_span("record_message_score")
async def record_message_score(
    turn_id: str, score: float | None, reason: str | None
) -> None:
    trace.get_current_span().set_attribute("conversation_id", settings.conversation_id or "")
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.patch(
            f"{settings.llm_gateway_url}/v1/conversations/{settings.conversation_id}/messages/{turn_id}/score",
            headers={"Authorization": f"Bearer {settings.session_token}"},
            json={"score": score, "reason": reason},
        )
        raise_non_retryable_on_4xx(response)


@activity.defn
@tracer.start_as_current_span("record_langfuse_score")
async def record_langfuse_score(trace_id: str, name: str, value: float, comment: str = "") -> None:
    langfuse = get_langfuse()
    if langfuse:
        try:
            langfuse.score(
                trace_id=trace_id,
                name=name,
                value=value,
                comment=comment
            )
            langfuse.flush()
        except Exception as exc:
            logger.warning("Could not store the evaluation score in Langfuse: %s", exc)

@activity.defn
@tracer.start_as_current_span("classify_query")
async def classify_query(query: str) -> bool:
    """Classifies if the query requires knowledge base retrieval."""
    prompt = f"""Decide whether this user request requires looking up factual information in the corporate knowledge base.
Answer only 'YES' or 'NO'.
Greetings, basic questions that can be answered without specific knowledge, and general reasoning do not require a lookup (NO).
Questions about equipment, processes, errors, documents, instructions and regulations do require it (YES).
The request may be written in any language.

Request: {query}"""
    
    request = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "max_tokens": 5
    }
    
    full = ""
    try:
        async for chunk in llm_client.stream_chat(request):
            chunk = chunk.strip()
            if not chunk or chunk == "[DONE]":
                continue
            try:
                data = json.loads(chunk)
                delta = data["choices"][0].get("delta", {}).get("content", "")
                if delta:
                    full += delta
            except Exception:
                continue
    except Exception as e:
        logger.warning(f"Query classification failed: {e}")
        return False
            
    return bool(full.strip().upper().startswith("YES"))


@activity.defn
async def register_conversation_execution(workflow_id: str, run_id: str) -> None:
    """Read bound process credentials outside the deterministic workflow sandbox."""
    await _workflow_api_request(
        'POST', f'/v1/conversations/{settings.conversation_id}/executions',
        {'workflow_id': workflow_id, 'run_id': run_id},
    )


# tool-worker's src/hermes_session_workflow.py, by name and shape.
HERMES_SESSION_WORKFLOW = "HermesSessionWorkflow"


def hermes_session_id(conversation_id: str) -> str:
    return f"hermes-session-{conversation_id}"


@activity.defn
async def hand_off_hermes_turn(turn: dict[str, Any]) -> None:
    """Start the conversation's Hermes session workflow, or signal the running
    one, with this turn; it answers with the hermes_turn_result signal."""
    await activity.client().start_workflow(
        HERMES_SESSION_WORKFLOW,
        {
            "org_id": settings.org_id or "",
            "conversation_id": settings.conversation_id or "",
            "idle_seconds": settings.hermes_idle_seconds,
        },
        id=hermes_session_id(settings.conversation_id or ""),
        task_queue=settings.hermes_task_queue,
        start_signal="hermes_turn",
        start_signal_args=[turn],
    )


@activity.defn
async def cancel_hermes_turn(turn_id: str) -> None:
    handle = activity.client().get_workflow_handle(hermes_session_id(settings.conversation_id or ""))
    await handle.signal("hermes_cancel_turn", turn_id)


@activity.defn
async def touch_hermes_turn(turn_id: str) -> None:
    """Keep this runner (and its token) alive while a long Hermes turn runs:
    chat requests do that for other turns, and a Hermes turn makes none."""
    await _workflow_api_request("POST", f"/v1/internal/hermes/turns/{turn_id}/touch")
