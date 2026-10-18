from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Literal
import json

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

from src.workflows.lifecycle import register_lifecycle_execution

with workflow.unsafe.imports_passed_through():
    from src.workflows.activities import (
        create_user_question,
        execute_web_tool,
        get_conversation_id,
        get_session_token,
        list_available_skills,
        llm_call,
        publish_event,
        register_interaction_execution,
    )
    from src.workflows.builtin_tools import (
        ExecutionContext,
        builtin_instructions,
        enabled_builtin_tools,
        prepare_builtin_invocation,
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
    import jsonschema


# See conversation.py's identical constant — the Temporal default
# (unlimited retries) means a persistently failing activity retries forever
# instead of ever completing the workflow.
_ACTIVITY_RETRY_POLICY = RetryPolicy(maximum_attempts=3)


AGENT_SYSTEM_PROMPT = (
    "You are a specialized agent. Achieve the given goal step by step.\n"
    "To call a tool, emit exactly:\n"
    '<tool_call>{"skill_id": "<id>", "args": {...}}</tool_call>\n'
    "You may emit multiple <tool_call> blocks in one response.\n"
    "When the goal is complete, respond with your final answer as plain text "
    "(no <tool_call> tags)."
)


@dataclass(frozen=True)
class AgentExecutionSnapshot:
    agent_definition_id: str
    name: str
    model: str
    system_prompt: str
    temperature: float
    allowed_skill_ids: tuple[str, ...] = ()
    knowledge_bases: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True)
class AgentInput:
    agent: AgentExecutionSnapshot
    goal: str
    context: dict[str, Any] = field(default_factory=dict)
    parent_message_id: str | None = None
    output_schema: dict[str, Any] | None = None
    event_visibility: Literal["external", "internal"] = "external"
    workflow_run_id: str | None = None
    workflow_phase: Literal[
        "supervisor_prepare", "node", "supervisor_synthesize"
    ] | None = None
    workflow_node_key: str | None = None
    turn_id: str | None = None
    execution_iteration: int = 1
    user_questions_enabled: bool = False
    web_tools_enabled: bool = False
    inherited_source_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentResult:
    answer: str
    source_ids: tuple[str, ...] = ()
    unsupported_source_ids: tuple[str, ...] = ()


def _build_agent_system_prompt(
    agent: AgentExecutionSnapshot,
    skills: list[dict[str, Any]],
    *,
    user_questions_enabled: bool = True,
    web_tools_enabled: bool = False,
) -> str:
    sections = [
        agent.system_prompt.strip(),
        AGENT_SYSTEM_PROMPT,
    ]
    builtin_prompt = builtin_instructions(
        "agent",
        user_questions_enabled=user_questions_enabled,
        web_tools_enabled=web_tools_enabled,
    )
    if builtin_prompt:
        sections.append(builtin_prompt)
    if skills:
        skill_lines = []
        for skill in skills:
            schema = json.dumps(skill.get("inputSchema") or {}, sort_keys=True)
            skill_lines.append(
                f"- id={skill.get('id')} name={skill.get('name')}: "
                f"{skill.get('description') or ''} args_schema={schema}"
            )
        sections.append("Configured tools:\n" + "\n".join(skill_lines))
    else:
        sections.append("Configured tools: none")
    if agent.knowledge_bases:
        sections.append(
            "Configured knowledge bases:\n"
            + "\n".join(
                f"- id={item.get('id')} name={item.get('name')}"
                for item in agent.knowledge_bases
            )
        )
    return "\n\n".join(section for section in sections if section)


def _llm_request(
    input: AgentInput,
    messages: list[dict[str, str]],
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "model": input.agent.model,
        "messages": messages,
        "temperature": input.agent.temperature,
        "stream": True,
    }
    if input.workflow_run_id is not None:
        request.update(
            workflow_run_id=input.workflow_run_id,
            workflow_phase=input.workflow_phase,
            workflow_node_key=input.workflow_node_key,
        )
    return request


@workflow.defn(name="AgentWorkflow")
class AgentWorkflow:
    def __init__(self) -> None:
        self.thoughts: list[dict[str, Any]] = []
        self.iteration = 0
        self.final_answer = ""
        self._question_wait = QuestionWaitState()

    @workflow.run
    async def run(self, input: AgentInput) -> str | AgentResult:
        await register_lifecycle_execution()
        max_iterations = 3
        execution_context: ExecutionContext | None = None
        used_web_tool = False
        available_source_ids = list(input.inherited_source_ids)
        web_protocol_enabled = input.web_tools_enabled and workflow.patched("web-tools-v1")
        question_protocol_enabled = (
            input.user_questions_enabled and workflow.patched("user-questions-v1")
        )
        enabled_tools = enabled_builtin_tools(
            question_protocol_enabled,
            web_protocol_enabled,
        )
        available_skills = await workflow.execute_activity(
            list_available_skills,
            start_to_close_timeout=timedelta(seconds=15),
            retry_policy=_ACTIVITY_RETRY_POLICY,
        )
        allowed_skill_ids = set(input.agent.allowed_skill_ids)
        filtered_skills = [
            skill
            for skill in available_skills
            if str(skill.get("id", "")) in allowed_skill_ids
        ]
        executable_skill_ids = {
            str(skill.get("id", "")) for skill in filtered_skills
        }
        messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": _build_agent_system_prompt(
                    input.agent,
                    filtered_skills,
                    user_questions_enabled=question_protocol_enabled,
                    web_tools_enabled=web_protocol_enabled,
                ),
            },
            {"role": "user", "content": f"Goal: {input.goal}\nContext: {input.context}"},
        ]
        if input.output_schema:
            messages[0]["content"] += f"\nWhen the goal is complete, respond with your final answer as a valid JSON object matching this schema:\n{json.dumps(input.output_schema)}"

        # Workflow code can't read settings.conversation_id/session_token
        # directly and see pool-mode's runtime POST /bind values — only
        # whatever was there when the workflow sandbox last imported the
        # module (the security review, same rule as get_session_token's own
        # docstring). Fetched once, up front, like conversation.py does.
        conversation_id = await workflow.execute_activity(
            get_conversation_id,
            start_to_close_timeout=timedelta(seconds=15),
            retry_policy=_ACTIVITY_RETRY_POLICY,
        )
        session_token = await workflow.execute_activity(
            get_session_token,
            start_to_close_timeout=timedelta(seconds=15),
            retry_policy=_ACTIVITY_RETRY_POLICY,
        )

        self.iteration = 0
        while self.iteration < max_iterations:
            self.iteration += 1
            request = _llm_request(input, messages)
            raw = await workflow.execute_activity(
                llm_call,
                # publish_events=False — this is an internal planning/tool
                # loop, not the user-facing chat turn: publishing with no
                # turn_id would leak untagged content-delta/thinking-delta
                # onto the conversation's own SSE channel, where the
                # frontend's "no turn_id = broadcast" fallback appends it to
                # whatever answer happens to be on screen. agent-thought
                # below is this workflow's own, correctly-scoped event.
                args=(request, None, False),
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=_ACTIVITY_RETRY_POLICY,
            )

            tool_calls = extract_tool_calls(raw, id_factory=workflow.uuid4)
            has_web_call = web_protocol_enabled and any(
                call.get("tool") in {"web_search", "web_read"} for call in tool_calls
            )
            if input.event_visibility == "external":
                await workflow.execute_activity(
                    publish_event,
                    args=(
                        "agent-thought",
                        {
                            "iteration": self.iteration,
                            "content": "Working with web sources" if has_web_call else raw,
                        },
                    ),
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=_ACTIVITY_RETRY_POLICY,
                )

            # Preserve the pre-question interpretation of historical tool
            # calls during replay. In particular, an old ask_user-shaped
            # hallucination followed the denied-skill path; treating it as a
            # built-in now would add durable-question activities that do not
            # exist in the recorded AgentWorkflow history.
            if not tool_calls:
                self.final_answer = raw
                if input.output_schema:
                    try:
                        raw_clean = raw.strip()
                        if raw_clean.startswith("```json"):
                            raw_clean = raw_clean[7:]
                        if raw_clean.startswith("```"):
                            raw_clean = raw_clean[3:]
                        if raw_clean.endswith("```"):
                            raw_clean = raw_clean[:-3]
                        raw_clean = raw_clean.strip()
                        
                        data = json.loads(raw_clean)
                        jsonschema.validate(instance=data, schema=input.output_schema)
                        self.final_answer = raw_clean
                    except json.JSONDecodeError as e:
                        messages.append({"role": "assistant", "content": raw})
                        messages.append({"role": "user", "content": f"Your response must be valid JSON matching the requested schema. Invalid JSON error: {e}"})
                        continue
                    except Exception as e:
                        # Catch jsonschema.ValidationError or ImportError if not installed
                        messages.append({"role": "assistant", "content": raw})
                        messages.append({"role": "user", "content": f"Your response must match the requested schema. Validation error: {e}"})
                        continue

                if input.event_visibility == "external":
                    await workflow.execute_activity(
                        publish_event,
                        args=(
                            "agent-complete",
                            {"answer": self.final_answer, "iterations": self.iteration},
                        ),
                        start_to_close_timeout=timedelta(seconds=30),
                        retry_policy=_ACTIVITY_RETRY_POLICY,
                    )
                if web_protocol_enabled:
                    selection = select_source_citations(
                        self.final_answer,
                        available_source_ids,
                    )
                    return AgentResult(
                        answer=self.final_answer,
                        source_ids=selection.source_ids,
                        unsupported_source_ids=selection.unsupported_source_ids,
                    )
                return self.final_answer

            messages.append({"role": "assistant", "content": raw})
            for call in tool_calls:
                if (
                    question_protocol_enabled
                    and not web_protocol_enabled
                    and call.get("tool") in {"web_search", "web_read"}
                ):
                    # Before web-tools-v1, ask_user-enabled histories parsed
                    # this shape as an unknown built-in and failed before
                    # scheduling another activity. Preserve that command path.
                    raise ValueError("unknown built-in tool")
                builtin = prepare_builtin_invocation(
                    call,
                    workflow.uuid4,
                    enabled_tools=enabled_tools,
                )
                if builtin is not None:
                    max_iterations = 50
                    if input.turn_id is None or input.workflow_phase is None:
                        raise ValueError("interaction_execution_context_missing")
                    if execution_context is None:
                        info = workflow.info()
                        execution_context = await workflow.execute_activity(
                            register_interaction_execution,
                            args=(
                                input.turn_id,
                                info.workflow_id,
                                info.run_id,
                                info.task_queue,
                                input.workflow_phase,
                                input.workflow_node_key,
                                input.agent.agent_definition_id,
                                input.execution_iteration,
                            ),
                            start_to_close_timeout=timedelta(seconds=30),
                            retry_policy=_ACTIVITY_RETRY_POLICY,
                        )
                    if builtin.name in {"web_search", "web_read"}:
                        used_web_tool = True
                        label = (
                            "Searching sources"
                            if builtin.name == "web_search"
                            else "Reading page"
                        )
                        if input.event_visibility == "external":
                            await workflow.execute_activity(
                                publish_event,
                                args=(
                                    "agent-tool-call",
                                    {"tool": builtin.name, "label": label},
                                ),
                                start_to_close_timeout=timedelta(seconds=30),
                                retry_policy=_ACTIVITY_RETRY_POLICY,
                            )
                        try:
                            web_result = await workflow.execute_activity(
                                execute_web_tool,
                                args=(
                                    execution_context,
                                    builtin.invocation_id,
                                    builtin.name,
                                    builtin.arguments,
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
                            web_result = web_tool_error_context(builtin.name, error_type)
                        for source_id in web_result.get("source_ids", []):
                            if (
                                isinstance(source_id, str)
                                and source_id not in available_source_ids
                            ):
                                available_source_ids.append(source_id)
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    f"Tool result for {builtin.name}: "
                                    f"{json.dumps(web_result)}"
                                ),
                            }
                        )
                        continue

                    try:
                        question = await workflow.execute_activity(
                            create_user_question,
                            args=(
                                execution_context,
                                builtin.invocation_id,
                                builtin.arguments,
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
                        messages.append(
                            {
                                "role": "user",
                                "content": f"Tool result for ask_user: {error_type}",
                            }
                        )
                        continue
                    question_id = str(question.get("questionId") or "")
                    if not question_id:
                        raise ValueError("invalid_question_response")
                    answer = await wait_for_answer(
                        self._question_wait,
                        question_id,
                        question_deadline(question),
                    )
                    # The backend rotates an identity-only token in the live
                    # runner process during long user waits. Workflow code
                    # must observe that mutable value through an activity
                    # before a later skill call.
                    session_token = await workflow.execute_activity(
                        get_session_token,
                        start_to_close_timeout=timedelta(seconds=15),
                        retry_policy=_ACTIVITY_RETRY_POLICY,
                    )
                    messages.append(
                        {
                            "role": "user",
                            "content": f"Tool result for ask_user: {json.dumps(answer)}",
                        }
                    )
                    continue

                skill_id = str(call.get("skill_id", ""))
                args = call.get("args", {})
                if not isinstance(args, dict):
                    args = {"input": args}

                if skill_id not in executable_skill_ids:
                    denied_result = f"skill_not_allowed:{skill_id}"
                    self.thoughts.append(
                        {
                            "iteration": self.iteration,
                            "skill_id": skill_id,
                            "result": denied_result,
                        }
                    )
                    messages.append(
                        {
                            "role": "user",
                            "content": f"Tool result for {skill_id}: {denied_result}",
                        }
                    )
                    continue

                if input.event_visibility == "external":
                    await workflow.execute_activity(
                        publish_event,
                        args=("agent-tool-call", {"skill_id": skill_id, "args": args}),
                        start_to_close_timeout=timedelta(seconds=30),
                        retry_policy=_ACTIVITY_RETRY_POLICY,
                    )

                # session_token, not settings.session_token directly — same
                # rationale as conversation_id above. Without it, tool-worker
                # got no real auth for GET /v1/skills/{id} and silently fell
                # back to a dummy smoke-test token (skill_loader.py).
                tool_result = await workflow.execute_activity(
                    "execute_skill",
                    args=(skill_id, args, conversation_id, session_token),
                    task_queue="tools",
                    start_to_close_timeout=timedelta(seconds=60),
                    # Bounded — see conversation.py's identical execute_skill
                    # call for why: the default (unlimited) policy means a
                    # persistently failing tool retries forever.
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )

                self.thoughts.append(
                    {"iteration": self.iteration, "skill_id": skill_id, "result": tool_result}
                )
                messages.append(
                    {"role": "user", "content": f"Tool result for {skill_id}: {tool_result}"}
                )

        if used_web_tool:
            # A web-capable model that spends all 50 rounds asking for more
            # tools has not produced an answer. The summary call below runs
            # with publish_events=False, where hide_tool_call_tag is a no-op
            # (see llm_call in activities.py), so a <tool_call> the model
            # emits there lands verbatim in final_answer — and
            # select_source_citations validates markers without stripping
            # tags. Failing is the only way not to relabel one more unparsed
            # tool request as a successful, citable final result.
            raise ApplicationError(
                "builtin_round_limit_exceeded",
                type="builtin_round_limit_exceeded",
                non_retryable=True,
            )

        # The iteration budget is exhausted; ask the LLM to summarize.
        request = _llm_request(
            input,
            messages
            + [
                {
                    "role": "user",
                    "content": "Provide a concise final answer based on the work above.",
                }
            ],
        )
        self.final_answer = await workflow.execute_activity(
            llm_call,
            args=(request, None, False),
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=_ACTIVITY_RETRY_POLICY,
        )
        if input.event_visibility == "external":
            await workflow.execute_activity(
                publish_event,
                args=(
                    "agent-complete",
                    {"answer": self.final_answer, "iterations": self.iteration},
                ),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=_ACTIVITY_RETRY_POLICY,
            )
        if web_protocol_enabled:
            selection = select_source_citations(
                self.final_answer,
                available_source_ids,
            )
            return AgentResult(
                answer=self.final_answer,
                source_ids=selection.source_ids,
                unsupported_source_ids=selection.unsupported_source_ids,
            )
        return self.final_answer

    @workflow.signal(name="question_available")
    async def question_available(self, question_id: str, version: int) -> None:
        self._question_wait.notify(question_id, version)
