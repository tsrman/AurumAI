import asyncio
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, ChildWorkflowError

from src.workflows.lifecycle import register_lifecycle_execution

with workflow.unsafe.imports_passed_through():
    from src.workflows.activities import (
        create_user_question,
        execute_image_tool,
        execute_web_tool,
        fail_interaction_turn,
        get_conversation_temperature,
        llm_call,
        load_conversation_state,
        list_available_skills,
        get_session_token,
        persist_assistant_message,
        publish_event,
        register_interaction_execution,
    )
    from src.workflows.builtin_tools import (
        IMAGE_TOOLS,
        ExecutionContext,
        builtin_instructions,
        enabled_builtin_tools,
        parse_builtin,
        prepare_builtin_invocation,
        image_tool_error_context,
        select_source_citations,
        web_tool_error_context,
    )
    from src.workflows.tools import extract_tool_calls
    from src.workflows.user_questions import (
        QuestionWaitState,
        application_error_type,
        question_deadline,
        wait_for_answer,
    )
    from src.workflows.multi_agent import (
        MultiAgentInput,
        MultiAgentResult,
        MultiAgentWorkflow,
    )
    from src.config import settings


# Applied to every activity call in this workflow — the Temporal default
# (unlimited retries) means a persistently failing activity (e.g. a
# non-transient 4xx the gateway keeps rejecting) retries forever and every
# future turn hangs on the same error, since the container never dies (see
# the security review). Genuine 4xx failures are additionally wrapped as
# non-retryable ApplicationError at the activity level (llm_client.py,
# workflows/activities.py) so they fail on the first attempt instead of
# burning through all 3.
_ACTIVITY_RETRY_POLICY = RetryPolicy(maximum_attempts=3)

SKILLS_SYSTEM_PROMPT_TEMPLATE = (
    "You have access to the following tools. To call one, emit exactly:\n"
    '<tool_call>{{"skill_id": "<id>", "args": {{...}}}}</tool_call>\n'
    "You may emit multiple <tool_call> blocks in one response. Only call a tool "
    "when it helps answer the user; otherwise answer normally. If the user explicitly "
    "requests using a tool, call the matching available tool before answering, even "
    "for a simple calculation. For questions about attached files, first search "
    "those files with the available attachment search tool; never guess their contents. "
    "Copy the exact id from the list into skill_id, not the tool's name. "
    "Emit the tool_call block rather than describing a call. After a successful "
    "result, answer from that result without repeating the same completed call.\n\n"
    "The tool protocol is plain response text with literal ASCII <tool_call> and "
    "</tool_call> tags. Do not use DSML, native function invocation markup, XML "
    "attributes, or Markdown fences for tool calls.\n\n"
    "Available tools:\n{tools}"
)


def _build_skills_system_message(
    skills: list[dict[str, Any]],
    *,
    user_questions_enabled: bool = True,
    web_tools_enabled: bool = False,
    image_generation_enabled: bool = False,
    image_editing_enabled: bool = False,
    recent_images: list[dict[str, str]] | None = None,
) -> str:
    tools = []
    for skill in skills:
        description = skill.get("description") or ""
        if skill.get("type") == "code":
            # Code skills have no fixed args schema (see skill_bundles.py) —
            # calling one opens a multi-round sandbox session
            # (CodeSkillSessionWorkflow) that reads the skill's own SKILL.md
            # and runs shell commands on its own until the task is done, so
            # the only thing the outer loop provides is the task itself.
            schema = {
                "type": "object",
                "properties": {"task": {"type": "string"}},
                "required": ["task"],
            }
        else:
            schema = skill.get("inputSchema") or {}
        # Use the same key as the invocation protocol. A separate `id=` next
        # to `name=` led real models to copy the name into skill_id instead.
        tools.append({
            "skill_id": skill.get("id"),
            "name": skill.get("name"),
            "description": description,
            "args_schema": schema,
        })
    skill_prompt = SKILLS_SYSTEM_PROMPT_TEMPLATE.format(tools=json.dumps(tools))
    sections = [skill_prompt]
    builtin_prompt = builtin_instructions(
        "ordinary",
        user_questions_enabled=user_questions_enabled,
        web_tools_enabled=web_tools_enabled,
        image_generation_enabled=image_generation_enabled,
        image_editing_enabled=image_editing_enabled,
        recent_images=recent_images,
    )
    if builtin_prompt:
        sections.insert(0, builtin_prompt)
    return "\n\n".join(section for section in sections if section)


@dataclass
class ConversationInput:
    conversation_id: str
    lifecycle_generation: int = 1
    lifecycle_operation_id: str | None = None


def _message_image_ids(message: dict[str, Any]) -> list[str]:
    value = message.get("image_ids")
    if message.get("role") != "user" or not isinstance(value, list):
        return []
    return [item for item in value[:4] if isinstance(item, str)]


@workflow.defn
class ConversationWorkflow:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self._pending_messages: list[dict[str, Any]] = []
        self._cancelled = False
        self._cancelled_workflow_run_ids: set[str] = set()
        self._active_workflow_run_id: str | None = None
        self._active_workflow_handle: Any | None = None
        self._active_turn_id: str | None = None
        self._cancelled_turn_ids: set[str] = set()
        self._question_wait = QuestionWaitState()
        self.conversation_id = ""
        self._last_result = ""

    @workflow.run
    async def run(self, input: ConversationInput) -> str:
        await register_lifecycle_execution()
        loaded_messages = await workflow.execute_activity(
            load_conversation_state,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_ACTIVITY_RETRY_POLICY,
        )
        self.messages = loaded_messages or []
        self.conversation_id = input.conversation_id

        idle_timeout = (
            timedelta(seconds=settings.idle_ttl_seconds) if settings.idle_ttl_seconds > 0 else None
        )

        while True:
            try:
                await workflow.wait_condition(
                    lambda: bool(self._pending_messages), timeout=idle_timeout
                )
            except asyncio.TimeoutError:
                break

            # Drain everything queued so far before going back to waiting —
            # cancel_generation only aborts the turn in flight when it
            # fires (_cancelled is reset below, before each new turn
            # starts), so a message that arrived while the previous turn
            # was being cancelled is never silently dropped: it still gets
            # its own full turn right here instead of the whole workflow
            # exiting underneath it.
            while self._pending_messages:
                message = self._pending_messages.pop(0)
                turn_id = str(message.get("turn_id") or "")
                self._active_turn_id = turn_id
                self._cancelled = turn_id in self._cancelled_turn_ids
                try:
                    await self._run_turn(message)
                except Exception as exc:
                    # A failed turn must still end the SSE stream. The
                    # backend's /v1/chat/stream generator only stops on a
                    # "done" event (routers/chat.py) — when the turn raised,
                    # nothing published one, so the browser sat on an open
                    # connection with a spinner until EVENT_SUBSCRIBE_TIMEOUT
                    # (120s) cut it off, and the user was given no reason.
                    #
                    # Swallowing here rather than letting it propagate is
                    # deliberate: an escaping exception kills the whole
                    # workflow, and this workflow is long-lived — one bad
                    # turn would take every later message in the conversation
                    # down with it. asyncio.CancelledError derives from
                    # BaseException, so `except Exception` still lets Temporal
                    # cancellation through untouched.
                    await self._fail_turn(message, exc)
                finally:
                    self._cancelled_turn_ids.discard(turn_id)
                    self._active_turn_id = None

        return self._last_result

    async def _fail_turn(self, message: dict[str, Any], exc: Exception) -> None:
        """Report a failed turn to the user and close the stream.

        Reported as message-end + done, not a new "error" event type, because
        business-ui's SSE reducer (hooks/useChat.ts) has no error branch —
        an unknown event would be ignored and leave the spinner running,
        which is the very failure this is fixing.
        """
        turn_id = str(message.get("turn_id") or "")
        workflow.logger.error(f"Turn {turn_id} failed: {exc}")
        # Persist terminal state before closing SSE. The patch marker keeps
        # histories recorded before this activity was introduced replayable:
        # old failed turns have no marker and therefore retain their original
        # command sequence, while every new failed turn is closed durably.
        if turn_id and workflow.patched("failed-turn-terminal-v1"):
            try:
                await workflow.execute_activity(
                    fail_interaction_turn,
                    args=(turn_id,),
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=_ACTIVITY_RETRY_POLICY,
                )
            except Exception as terminal_exc:  # noqa: BLE001 - preserve SSE cleanup
                workflow.logger.error(
                    f"Could not close failed turn {turn_id}: {terminal_exc}"
                )
        # Unwrap to the root cause: Temporal wraps an activity failure in
        # ActivityError -> ApplicationError, whose str() is
        # "Activity task failed (...)" plus scheduling metadata and says
        # nothing about what actually went wrong. The innermost cause is the
        # gateway's own message ("model_not_found: ..."), which is the only
        # part worth putting in front of the user.
        root = exc
        while root.__cause__ is not None:
            root = root.__cause__  # type: ignore[assignment]
        if isinstance(root, ApplicationError) and root.type in {
            "user_input_timeout",
            "user_input_cancelled",
            "user_input_limit",
            "authorization_revoked",
            "access_revoked",
        }:
            try:
                await workflow.execute_activity(
                    publish_event,
                    args=(
                        "done",
                        {"turn_id": turn_id, "evaluation_pending": False},
                    ),
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=_ACTIVITY_RETRY_POLICY,
                )
            except Exception as publish_exc:  # noqa: BLE001 - last-resort path
                workflow.logger.error(
                    f"Could not publish done for turn {turn_id}: {publish_exc}"
                )
            return
        text = f"Could not get a model response. Reason: {root}"
        for event, data in (
            ("message-end", {"role": "assistant", "content": text, "turn_id": turn_id}),
            ("done", {"turn_id": turn_id, "evaluation_pending": False}),
        ):
            try:
                await workflow.execute_activity(
                    publish_event,
                    args=(event, data),
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=_ACTIVITY_RETRY_POLICY,
                )
            except Exception as publish_exc:  # noqa: BLE001 - last-resort path
                # Nothing further to fall back on: if even publishing fails,
                # the stream is left to time out as before, but the workflow
                # still survives for the next message.
                workflow.logger.error(
                    f"Could not publish {event} for turn {turn_id}: {publish_exc}"
                )

    async def _run_turn(self, message: dict[str, Any]) -> None:
        self._append_message_once(message)
        if message.get("workflow_run_id") is not None:
            await self._run_multi_agent_turn(message)
            return
        model = str(message.get("model") or "gpt-4o-mini")
        turn_id = str(message.get("turn_id") or "")

        skills = await workflow.execute_activity(
            list_available_skills,
            start_to_close_timeout=timedelta(seconds=15),
            retry_policy=_ACTIVITY_RETRY_POLICY,
        )
        session_token = await workflow.execute_activity(
            get_session_token,
            start_to_close_timeout=timedelta(seconds=15),
            retry_policy=_ACTIVITY_RETRY_POLICY,
        )
        # Existing histories predate this activity: replaying them here must
        # not schedule a command they never recorded, or Replayer sees
        # nondeterminism. New executions always take the patched branch.
        temperature: float | None = None
        if workflow.patched("conversation-temperature-v1"):
            temperature = await workflow.execute_activity(
                get_conversation_temperature,
                start_to_close_timeout=timedelta(seconds=15),
                retry_policy=_ACTIVITY_RETRY_POLICY,
            )
        has_skills = bool(skills)
        user_questions_enabled = message.get("user_questions_enabled") is True
        web_tools_requested = message.get("web_tools_enabled") is True
        image_generation_requested = message.get("image_generation_enabled") is True
        image_editing_requested = message.get("image_editing_enabled") is True
        llm_messages = [
            {
                "role": "system",
                "content": _build_skills_system_message(
                    skills,
                    user_questions_enabled=user_questions_enabled,
                    web_tools_enabled=web_tools_requested,
                    image_generation_enabled=image_generation_requested,
                    image_editing_enabled=image_editing_requested,
                    recent_images=message.get("recent_images"),
                ),
            }
        ] + self.messages
        request: dict[str, Any] = {"model": model, "messages": llm_messages, "stream": True}
        if temperature is not None:
            request["temperature"] = temperature

        await workflow.execute_activity(
            publish_event,
            args=("message-start", {"role": "assistant", "turn_id": turn_id}),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_ACTIVITY_RETRY_POLICY,
        )
        if self._cancelled:
            return

        # With skills available, planning still streams reasoning and content,
        # using hide_tool_call_tag=True. llm_call withholds only the selection
        # tag <tool_call>{...}</tool_call> and the content following it,
        # preventing raw tool protocol from reaching the user while preserving
        # the rest of the live answer stream.
        raw_result = await workflow.execute_activity(
            llm_call,
            args=(request, turn_id, True, True),
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=_ACTIVITY_RETRY_POLICY,
        )
        if self._cancelled:
            return

        context_used_raw: list[dict[str, Any]] = []
        persisted_tool_results: list[dict[str, Any]] = []
        source_ids: list[str] = []
        tool_calls = extract_tool_calls(raw_result, id_factory=workflow.uuid4)
        # Histories created before user questions existed treated an ask_user
        # tag as ordinary model text when no skills were available. Replaying
        # such a history through the new branch would schedule
        # register_interaction_execution where the recorded history scheduled
        # persist_assistant_message, killing every long-lived conversation on
        # rollout. The patch marker keeps the old command path while replaying
        # those events and enables built-ins once execution reaches new work.
        # Keep patch markers after the planning LLM activity. The original
        # user-questions rollout recorded its marker at this exact point, so
        # evaluating either protocol marker while building the prompt would
        # make pre-web histories nondeterministic.
        web_tools_enabled = web_tools_requested and workflow.patched("web-tools-v1")
        question_protocol_enabled = user_questions_enabled and workflow.patched(
            "user-questions-v1"
        )
        # Evaluated only when a turn asks for images, so histories without
        # image turns record no marker and replay unchanged.
        image_tools_enabled = (
            image_generation_requested or image_editing_requested
        ) and workflow.patched("image-tools-v1")
        enabled_tools = enabled_builtin_tools(
            question_protocol_enabled,
            web_tools_enabled,
            image_generation_enabled=image_tools_enabled and image_generation_requested,
            image_editing_enabled=image_tools_enabled and image_editing_requested,
        )
        image_tools = enabled_tools & IMAGE_TOOLS
        # Scoped deliberately: this guard covers exactly the branches that go
        # on to parse and dispatch tool calls. With neither questions nor
        # skills the else-branch below treats a <tool_call> tag as ordinary
        # hallucinated text — that is itself a fix (executing such a tag used
        # to really run the named tool), so widening the condition here would
        # turn those responses into hard workflow failures instead.
        if not web_tools_enabled and (question_protocol_enabled or has_skills):
            if any(
                call.get("tool") in {"web_search", "web_read"}
                for call in tool_calls
            ):
                raise ValueError("unknown built-in tool")
        has_builtin = any(
            (parsed := parse_builtin(call, image_tools=image_tools)) is not None
            and parsed[0] in enabled_tools
            for call in tool_calls
        )
        image_ids: list[str] = []
        if has_builtin or (has_skills and tool_calls):
            (
                result,
                tool_calls,
                persisted_tool_results,
                context_used_raw,
                source_ids,
                image_ids,
            ) = (
                await self._run_tool_rounds(
                    raw_result,
                    tool_calls,
                    llm_messages,
                    turn_id,
                    session_token,
                    skills,
                    model,
                    temperature,
                    has_builtin,
                    enabled_tools,
                )
            )
            if self._cancelled:
                return
        elif has_skills:
            # workflow.uuid4 is replay-deterministic; the default
            # uuid.uuid4 fallback must never run inside workflow code.
            # No call was emitted. llm_call already streamed the response;
            # hide_tool_call_tag only held a possible trailing tag prefix.
            result = raw_result
        else:
            # No skills were offered to the model at all (system prompt
            # never mentioned <tool_call>), so a <tool_call> tag in the raw
            # text is a hallucination, not a real selection — parsing and
            # executing it anyway used to actually run it (admin:read
            # holders bypass all resource-level skill gates) while the raw
            # tag stayed embedded in the "final" answer shown to the user,
            # since this branch never did the synthesis pass the has_skills
            # branch does. tool_calls stays empty so
            # tool_results_by_call below is trivially empty too.
            result = raw_result
            tool_calls = []

        self.messages.append({"role": "assistant", "content": result})
        # Persisted so history reloads can render the same tool-call/
        # tool-result rows the live SSE stream showed — see
        # business-ui's backendMessageToUIMessage.
        tool_results_by_call = {
            str(call.get("call_id")): call_result
            for call, call_result in zip(tool_calls, persisted_tool_results)
        }
        citation_selection = select_source_citations(result, source_ids)
        if citation_selection.source_ids or citation_selection.unsupported_source_ids:
            tool_results_by_call["__web_sources__"] = {
                "sourceIds": list(citation_selection.source_ids),
                "unsupportedSourceIds": list(citation_selection.unsupported_source_ids),
            }
        if image_ids:
            # Every image the turn produced is shown with the answer; the
            # backend keeps only ids this turn really generated.
            tool_results_by_call["__images__"] = {"imageIds": list(image_ids)}
        await workflow.execute_activity(
            persist_assistant_message,
            args=(
                result,
                model,
                turn_id,
                tool_calls or None,
                tool_results_by_call or None,
            ),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_ACTIVITY_RETRY_POLICY,
        )
        if self._cancelled:
            return

        await workflow.execute_activity(
            publish_event,
            args=("message-end", {"role": "assistant", "content": result, "turn_id": turn_id}),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_ACTIVITY_RETRY_POLICY,
        )
        if self._cancelled:
            return

        # Optional background evaluation controlled by settings.evaluator_enabled.
        evaluation_pending = await self._start_evaluation(
            query=str(message.get("content") or ""),
            context_used=context_used_raw,
            answer=result,
            turn_id=turn_id,
        )

        await workflow.execute_activity(
            publish_event,
            args=("done", {"turn_id": turn_id, "evaluation_pending": evaluation_pending}),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_ACTIVITY_RETRY_POLICY,
        )
        if self._cancelled:
            return

        self._last_result = result

    async def _run_tool_rounds(
        self,
        raw: str,
        calls: list[dict[str, Any]],
        base_messages: list[dict[str, Any]],
        turn_id: str,
        session_token: str | None,
        skills: list[dict[str, Any]],
        model: str,
        temperature: float | None,
        builtin_mode: bool,
        enabled_tools: frozenset[str],
    ) -> tuple[
        str,
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[str],
        list[str],
    ]:
        messages = list(base_messages)
        image_ids: list[str] = []
        persisted_calls: list[dict[str, Any]] = []
        persisted_results: list[dict[str, Any]] = []
        context_used: list[dict[str, Any]] = []
        execution_context: ExecutionContext | None = None
        source_ids: list[str] = []

        for _round in range(50):
            messages.append({"role": "assistant", "content": raw})
            round_results: list[tuple[str, dict[str, Any]]] = []
            for call in calls:
                if (
                    "web_search" not in enabled_tools
                    and call.get("tool") in {"web_search", "web_read"}
                ):
                    raise ValueError("unknown built-in tool")
                invocation = prepare_builtin_invocation(
                    call,
                    workflow.uuid4,
                    enabled_tools=enabled_tools,
                )
                if invocation is not None:
                    builtin_mode = True
                    if execution_context is None:
                        info = workflow.info()
                        execution_context = await workflow.execute_activity(
                            register_interaction_execution,
                            args=(
                                turn_id,
                                info.workflow_id,
                                info.run_id,
                                info.task_queue,
                                "ordinary",
                                None,
                                None,
                                1,
                            ),
                            start_to_close_timeout=timedelta(seconds=30),
                            retry_policy=_ACTIVITY_RETRY_POLICY,
                        )
                    if invocation.name in {"web_search", "web_read"}:
                        label = (
                            "Searching sources"
                            if invocation.name == "web_search"
                            else "Reading page"
                        )
                        safe_call = {
                            "call_id": invocation.invocation_id,
                            "tool": invocation.name,
                            "label": label,
                        }
                        await workflow.execute_activity(
                            publish_event,
                            args=("tool-calls", {"calls": [safe_call], "turn_id": turn_id}),
                            start_to_close_timeout=timedelta(seconds=30),
                            retry_policy=_ACTIVITY_RETRY_POLICY,
                        )
                        try:
                            result = await workflow.execute_activity(
                                execute_web_tool,
                                args=(
                                    execution_context,
                                    invocation.invocation_id,
                                    invocation.name,
                                    invocation.arguments,
                                ),
                                start_to_close_timeout=timedelta(seconds=90),
                                retry_policy=_ACTIVITY_RETRY_POLICY,
                            )
                        except ActivityError as exc:
                            error_type = application_error_type(exc)
                            if error_type is None or (
                                not error_type.startswith("web_")
                                and error_type != "unsupported_document"
                            ):
                                raise
                            result = web_tool_error_context(invocation.name, error_type)
                        if self._cancelled:
                            return (
                                raw,
                                persisted_calls,
                                persisted_results,
                                context_used,
                                source_ids,
                                image_ids,
                            )
                        safe_result = {
                            "tool": invocation.name,
                            "status": "completed"
                            if not json.loads(str(result["text"])).get("error")
                            else "failed",
                            "sourceIds": list(result.get("source_ids", [])),
                        }
                        persisted_calls.append(safe_call)
                        persisted_results.append(safe_result)
                        for source_id in result.get("source_ids", []):
                            if isinstance(source_id, str) and source_id not in source_ids:
                                source_ids.append(source_id)
                        context_used.append(result)
                        round_results.append((invocation.name, result))
                        await workflow.execute_activity(
                            publish_event,
                            args=(
                                "tool-result",
                                {
                                    "call_id": invocation.invocation_id,
                                    "tool": invocation.name,
                                    "label": label,
                                    "result": safe_result,
                                    "turn_id": turn_id,
                                },
                            ),
                            start_to_close_timeout=timedelta(seconds=30),
                            retry_policy=_ACTIVITY_RETRY_POLICY,
                        )
                        continue

                    if invocation.name in IMAGE_TOOLS:
                        label = (
                            "Generating image"
                            if invocation.name == "generate_image"
                            else "Editing image"
                        )
                        safe_call = {
                            "call_id": invocation.invocation_id,
                            "tool": invocation.name,
                            "label": label,
                        }
                        await workflow.execute_activity(
                            publish_event,
                            args=("tool-calls", {"calls": [safe_call], "turn_id": turn_id}),
                            start_to_close_timeout=timedelta(seconds=30),
                            retry_policy=_ACTIVITY_RETRY_POLICY,
                        )
                        try:
                            result = await workflow.execute_activity(
                                execute_image_tool,
                                args=(
                                    execution_context,
                                    invocation.invocation_id,
                                    invocation.name,
                                    invocation.arguments,
                                ),
                                start_to_close_timeout=timedelta(minutes=6),
                                retry_policy=_ACTIVITY_RETRY_POLICY,
                            )
                        except ActivityError as exc:
                            error_type = application_error_type(exc)
                            if error_type is None or not error_type.startswith("image_"):
                                raise
                            result = image_tool_error_context(invocation.name, error_type)
                        if self._cancelled:
                            return (
                                raw,
                                persisted_calls,
                                persisted_results,
                                context_used,
                                source_ids,
                                image_ids,
                            )
                        produced = [
                            image_id
                            for image_id in result.get("image_ids", [])
                            if isinstance(image_id, str)
                        ]
                        safe_result = {
                            "tool": invocation.name,
                            "status": "completed" if produced else "failed",
                            "imageIds": produced,
                        }
                        persisted_calls.append(safe_call)
                        persisted_results.append(safe_result)
                        for image_id in produced:
                            if image_id not in image_ids:
                                image_ids.append(image_id)
                        context_used.append(result)
                        round_results.append((invocation.name, result))
                        await workflow.execute_activity(
                            publish_event,
                            args=(
                                "tool-result",
                                {
                                    "call_id": invocation.invocation_id,
                                    "tool": invocation.name,
                                    "label": label,
                                    "result": safe_result,
                                    "turn_id": turn_id,
                                },
                            ),
                            start_to_close_timeout=timedelta(seconds=30),
                            retry_policy=_ACTIVITY_RETRY_POLICY,
                        )
                        continue

                    try:
                        question = await workflow.execute_activity(
                            create_user_question,
                            args=(
                                execution_context,
                                invocation.invocation_id,
                                invocation.arguments,
                            ),
                            start_to_close_timeout=timedelta(seconds=30),
                            retry_policy=_ACTIVITY_RETRY_POLICY,
                        )
                    except ActivityError as exc:
                        error_type = application_error_type(exc)
                        if error_type not in {
                            "user_input_disabled",
                            "user_input_limit",
                        }:
                            raise
                        result = {
                            "tool": "ask_user",
                            "output": {"error": error_type},
                        }
                        context_used.append(result)
                        round_results.append(("ask_user", result))
                        continue
                    question_id = str(question.get("questionId") or "")
                    if not question_id:
                        raise ValueError("invalid_question_response")
                    answer = await wait_for_answer(
                        self._question_wait,
                        question_id,
                        question_deadline(question),
                        lambda: self._cancelled,
                    )
                    session_token = await workflow.execute_activity(
                        get_session_token,
                        start_to_close_timeout=timedelta(seconds=15),
                        retry_policy=_ACTIVITY_RETRY_POLICY,
                    )
                    result = {"tool": "ask_user", "output": answer}
                    context_used.append(result)
                    round_results.append(("ask_user", result))
                    continue

                skill_id = str(call.get("skill_id", ""))
                if not any(str(skill.get("id", "")) == skill_id for skill in skills):
                    result = {
                        "skill_id": skill_id,
                        "output": f"skill_not_allowed:{skill_id}",
                    }
                else:
                    result = (
                        await self._maybe_call_tools(
                            [call], turn_id, session_token, skills, model
                        )
                    )[0]
                    persisted_calls.append(call)
                    persisted_results.append(result)
                context_used.append(result)
                round_results.append((skill_id, result))

            if self._cancelled:
                return raw, persisted_calls, persisted_results, context_used, source_ids, image_ids
            messages.extend(
                {
                    "role": "user",
                    "content": f"Tool result for {name}: {json.dumps(result)}",
                }
                for name, result in round_results
            )
            next_request: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "stream": True,
            }
            if temperature is not None:
                next_request["temperature"] = temperature
            raw = await workflow.execute_activity(
                llm_call,
                args=(next_request, turn_id, True, True),
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=_ACTIVITY_RETRY_POLICY,
            )
            calls = extract_tool_calls(raw, id_factory=workflow.uuid4)
            if not calls:
                return raw, persisted_calls, persisted_results, context_used, source_ids, image_ids
            next_has_builtin = any(
                (parsed := parse_builtin(call, image_tools=enabled_tools & IMAGE_TOOLS))
                is not None
                and parsed[0] in enabled_tools
                for call in calls
            )
            if not builtin_mode and not next_has_builtin:
                # Preserve the old single skill batch: a second skill-only
                # selection is not silently granted a larger loop budget.
                return raw, persisted_calls, persisted_results, context_used, source_ids, image_ids
            builtin_mode = builtin_mode or next_has_builtin

        raise ValueError("builtin_round_limit_exceeded")

    async def _start_evaluation(
        self,
        *,
        query: str,
        context_used: Any,
        answer: str,
        turn_id: str,
        reveal_reason: bool = True,
    ) -> bool:
        if not settings.evaluator_enabled:
            return False
        try:
            from src.workflows.evaluator import EvaluationInput

            evaluator_model = settings.evaluator_model
            if workflow.patched("evaluator-explicit-model-v1"):
                policy = await workflow.execute_activity(
                    "get_evaluator_config",
                    start_to_close_timeout=timedelta(seconds=10),
                    retry_policy=_ACTIVITY_RETRY_POLICY,
                )
                if not policy["enabled"] or not policy["model"]:
                    return False
                evaluator_model = policy["model"]

            serialized_context = str(context_used) if context_used else "No context was used"
            await workflow.start_child_workflow(
                "EvaluationWorkflow",
                EvaluationInput(
                    conversation_id=self.conversation_id,
                    turn_id=turn_id,
                    query=query,
                    context_used=serialized_context,
                    agent_answer=answer,
                    model=evaluator_model,
                    reveal_reason=reveal_reason,
                ),
                id=f"{self.conversation_id}-eval-{turn_id}",
            )
            return True
        except Exception as exc:
            workflow.logger.warning(f"Could not start EvaluationWorkflow: {exc}")
            return False

    async def _run_multi_agent_turn(self, message: dict[str, Any]) -> None:
        run_id = str(message["workflow_run_id"])
        try:
            await self._multi_agent_turn(message, run_id)
        finally:
            # The run id has to leave the cancellation set on *every* exit,
            # including an activity that exhausts its retries after the child
            # finished. This workflow is long-lived, so per-return cleanup let
            # the set grow for the life of the conversation.
            self._cancelled_workflow_run_ids.discard(run_id)

    async def _multi_agent_turn(self, message: dict[str, Any], run_id: str) -> None:
        turn_id = str(message.get("turn_id") or "")
        # self._cancelled is checked for parity with the ordinary turn (which
        # returns early on it): a cancel delivered after this turn was dequeued
        # but before _active_workflow_run_id was set has nowhere else to land.
        if self._cancelled or run_id in self._cancelled_workflow_run_ids:
            result = MultiAgentResult(status="cancelled")
        else:
            self._active_workflow_run_id = run_id
            try:
                handle = await workflow.start_child_workflow(
                    MultiAgentWorkflow.run,
                    MultiAgentInput(
                        run_id=run_id,
                        user_questions_enabled=message.get("user_questions_enabled") is True,
                        web_tools_enabled=message.get("web_tools_enabled") is True,
                    ),
                    id=f"{run_id}:orchestrator",
                )
                self._active_workflow_handle = handle
                # A cancel signal delivered while start_child_workflow was in
                # flight recorded the run id but had no handle to cancel yet.
                if run_id in self._cancelled_workflow_run_ids:
                    handle.cancel()
                result = await handle
            except ChildWorkflowError:
                result = MultiAgentResult(
                    status="cancelled" if run_id in self._cancelled_workflow_run_ids else "failed",
                    error_code=None
                    if run_id in self._cancelled_workflow_run_ids
                    else "execution_failed",
                )
            finally:
                self._active_workflow_handle = None
                self._active_workflow_run_id = None

        if (
            result.status == "completed"
            and (
                self._cancelled
                or run_id in self._cancelled_workflow_run_ids
            )
        ):
            result = MultiAgentResult(status="cancelled")

        status_payload: dict[str, Any] = {
            "turnId": turn_id,
            "workflowRunId": run_id,
            "status": result.status,
        }
        if result.error_code is not None:
            status_payload["errorCode"] = result.error_code
        await workflow.execute_activity(
            publish_event,
            args=("workflow-run-status", status_payload),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_ACTIVITY_RETRY_POLICY,
        )

        if (
            result.status == "completed"
            and (
                self._cancelled
                or run_id in self._cancelled_workflow_run_ids
            )
        ):
            await workflow.execute_activity(
                publish_event,
                args=(
                    "workflow-run-status",
                    {
                        "turnId": turn_id,
                        "workflowRunId": run_id,
                        "status": "cancelled",
                    },
                ),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=_ACTIVITY_RETRY_POLICY,
            )
            result = MultiAgentResult(status="cancelled")

        if result.status != "completed" or result.answer is None:
            await workflow.execute_activity(
                publish_event,
                args=("done", {"turn_id": turn_id, "evaluation_pending": False}),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=_ACTIVITY_RETRY_POLICY,
            )
            return

        source_metadata = None
        if result.source_ids or result.unsupported_source_ids:
            source_metadata = {
                "__web_sources__": {
                    "sourceIds": list(result.source_ids),
                    "unsupportedSourceIds": list(result.unsupported_source_ids),
                }
            }
        await workflow.execute_activity(
            persist_assistant_message,
            args=(result.answer, result.model, turn_id, None, source_metadata),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_ACTIVITY_RETRY_POLICY,
        )
        if self._cancelled or run_id in self._cancelled_workflow_run_ids:
            # A cancel landing during the persist activity still has to close
            # the stream: /v1/chat/stream ends only on `done`, and no cancel
            # signal handler publishes one, so returning here left the browser
            # spinning until EVENT_SUBSCRIBE_TIMEOUT.
            await workflow.execute_activity(
                publish_event,
                args=("done", {"turn_id": turn_id, "evaluation_pending": False}),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=_ACTIVITY_RETRY_POLICY,
            )
            return
        self.messages.append({"role": "assistant", "content": result.answer})
        await workflow.execute_activity(
            publish_event,
            args=(
                "message-end",
                {"role": "assistant", "content": result.answer, "turn_id": turn_id},
            ),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_ACTIVITY_RETRY_POLICY,
        )
        evaluation_pending = await self._start_evaluation(
            query=str(message.get("content") or ""),
            context_used=result.context_used,
            answer=result.answer,
            turn_id=turn_id,
            # context_used here is the raw node outputs — admin-only.  Keep the
            # score, drop the judge's user-visible justification.
            reveal_reason=False,
        )
        await workflow.execute_activity(
            publish_event,
            args=("done", {"turn_id": turn_id, "evaluation_pending": evaluation_pending}),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_ACTIVITY_RETRY_POLICY,
        )
        self._last_result = result.answer

    def _append_message_once(self, message: dict[str, Any]) -> None:
        comparable = {
            "role": message.get("role"),
            "content": message.get("content"),
            "turn_id": message.get("turn_id"),
        }
        image_ids = _message_image_ids(message)
        if self.messages:
            last = self.messages[-1]
            if comparable["turn_id"] is not None:
                if (
                    last.get("role") == comparable["role"]
                    and last.get("content") == comparable["content"]
                    and last.get("turn_id") == comparable["turn_id"]
                ):
                    return
            else:
                if (
                    last.get("role") == comparable["role"]
                    and last.get("content") == comparable["content"]
                ):
                    return
        # Only ids travel here; the chat completions proxy inlines the bytes.
        self.messages.append({**comparable, "image_ids": image_ids} if image_ids else comparable)

    async def _maybe_call_tools(
        self,
        tool_calls: list[dict[str, Any]],
        turn_id: str | None,
        session_token: str | None,
        skills: list[dict[str, Any]] | None = None,
        model: str = "gpt-4o-mini",
    ) -> list[dict[str, Any]]:
        """Invoke each already-parsed tool_call on the tools queue."""
        if self._cancelled or not tool_calls:
            return []

        await workflow.execute_activity(
            publish_event,
            args=("tool-calls", {"calls": tool_calls, "turn_id": turn_id}),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_ACTIVITY_RETRY_POLICY,
        )

        skill_types = {str(s.get("id")): s.get("type") for s in (skills or [])}

        results = []
        for call in tool_calls:
            if self._cancelled:
                return results

            call_id = call.get("call_id")
            skill_id = str(call.get("skill_id", ""))
            args = call.get("args", {})
            if not isinstance(args, dict):
                args = {"input": args}

            if skill_types.get(skill_id) == "code":
                # Code skills run a multi-round sandbox session, not a
                # single activity call — see CodeSkillSessionWorkflow. The
                # child workflow's single string result is wrapped the same
                # way execute_skill wraps its own result, so from here on
                # (synthesis prompt, tool-result event) a code skill still
                # looks like "one call -> one result" to the rest of this loop.
                from src.workflows.code_skill_session import CodeSkillSessionInput

                task = str(args.get("task") or args.get("input") or "")
                try:
                    answer = await workflow.execute_child_workflow(
                        "CodeSkillSessionWorkflow",
                        CodeSkillSessionInput(
                            skill_id=skill_id,
                            task=task,
                            conversation_id=self.conversation_id,
                            turn_id=turn_id,
                            session_token=session_token,
                            model=model,
                            max_rounds=settings.code_skill_max_rounds,
                            round_timeout_seconds=settings.code_skill_round_timeout_seconds,
                        ),
                        id=f"{self.conversation_id}-codeskill-{call_id}",
                    )
                except ChildWorkflowError as exc:
                    # A failed skill degrades into an error result the
                    # synthesis LLM can relay — never a failed conversation
                    # workflow (which would leave the SSE stream hanging
                    # with no message-end/done).
                    answer = f"Skill execution failed: {exc.cause or exc}"
                tool_result = {"skill_id": skill_id, "output": answer}
            else:
                try:
                    tool_result = await workflow.execute_activity(
                        "execute_skill",
                        args=(skill_id, args, self.conversation_id, session_token),
                        task_queue="tools",
                        start_to_close_timeout=timedelta(seconds=60),
                        # Bounded: with the default (unlimited) policy a
                        # persistently failing tool would retry forever and
                        # hang the turn.
                        retry_policy=RetryPolicy(maximum_attempts=3),
                    )
                except ActivityError as exc:
                    tool_result = {
                        "skill_id": skill_id,
                        "output": f"Skill execution failed: {exc.cause or exc}",
                    }
            results.append(tool_result)

            if self._cancelled:
                return results

            await workflow.execute_activity(
                publish_event,
                args=(
                    "tool-result",
                    {
                        "call_id": call_id,
                        "skill_id": skill_id,
                        "result": tool_result,
                        "turn_id": turn_id,
                    },
                ),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=_ACTIVITY_RETRY_POLICY,
            )
        return results

    @workflow.signal(name="user_message")
    async def user_message(self, message: dict[str, Any]) -> None:
        self._pending_messages.append(message)

    @workflow.signal(name="cancel_generation")
    async def cancel_generation(self) -> None:
        self._cancelled = True
        if self._active_workflow_run_id is None:
            return
        # Record the intent *before* testing for a handle, exactly as
        # cancel_workflow_run does.  _run_multi_agent_turn sets
        # _active_workflow_run_id before awaiting start_child_workflow and
        # assigns the handle only after it returns — a signal delivered on that
        # await sees no handle, and gating the add on one dropped the
        # cancellation entirely: the run finished, an answer was persisted for a
        # turn the user had cancelled, and the LLM spend was charged in full.
        self._cancelled_workflow_run_ids.add(self._active_workflow_run_id)
        if self._active_workflow_handle is not None and not self._active_workflow_handle.done():
            self._active_workflow_handle.cancel()

    @workflow.signal(name="cancel_workflow_run")
    async def cancel_workflow_run(self, run_id: str) -> None:
        self._cancelled_workflow_run_ids.add(run_id)
        if (
            self._active_workflow_run_id == run_id
            and self._active_workflow_handle is not None
            and not self._active_workflow_handle.done()
        ):
            self._active_workflow_handle.cancel()

    @workflow.signal(name="cancel_turn")
    async def cancel_turn(self, turn_id: str) -> None:
        is_queued = any(
            str(message.get("turn_id") or "") == turn_id for message in self._pending_messages
        )
        if self._active_turn_id != turn_id and not is_queued:
            return
        self._cancelled_turn_ids.add(turn_id)
        if self._active_turn_id != turn_id:
            return
        self._cancelled = True
        if self._active_workflow_run_id is not None:
            self._cancelled_workflow_run_ids.add(self._active_workflow_run_id)
        if self._active_workflow_handle is not None and not self._active_workflow_handle.done():
            self._active_workflow_handle.cancel()

    @workflow.signal(name="question_available")
    async def question_available(self, question_id: str, version: int) -> None:
        self._question_wait.notify(question_id, version)
