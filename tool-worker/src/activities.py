import json
import logging
from typing import Any

import httpx
from opentelemetry import trace
from temporalio import activity
from temporalio.exceptions import ApplicationError

from src.langfuse_client import get_or_create_trace, sanitize_content, sanitize_args
from src.mcp_client import MCPClient
from src import skill_executor_client
from src.skill_executor_client import SkillExecutorError
from src.skill_loader import fetch_skill
from src.rag_client import rag_client

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

# Roughly Temporal's own default activity-result payload ceiling — a
# same-order-of-magnitude cap here means an oversized response fails fast
# inside this activity (ApplicationError, non-retryable) instead of the
# activity succeeding and then Temporal itself rejecting the result.
_MAX_MCP_HTTP_RESPONSE_BYTES = 4 * 1024 * 1024


@activity.defn(name="execute_skill")
@tracer.start_as_current_span("execute_skill")
async def execute_skill(
    skill_id: str,
    args: dict[str, Any],
    conversation_id: str | None = None,
    session_token: str | None = None,
) -> dict[str, Any]:
    trace.get_current_span().set_attribute("skill_id", skill_id)
    lf_trace = get_or_create_trace(
        name="execute_skill",
        id=conversation_id,
        metadata={"skill_id": skill_id},
    )

    skill = None
    if lf_trace:
        try:
            fetch_span = lf_trace.span(name="fetch_skill", input={"skill_id": skill_id})
        except Exception as exc:
            logger.warning("Langfuse fetch_skill span start failed: %s", exc)
            fetch_span = None
    else:
        fetch_span = None

    try:
        skill = await fetch_skill(skill_id, session_token=session_token)
    except Exception as exc:
        if lf_trace:
            try:
                lf_trace.update(output={"error": f"fetch_skill failed: {exc}"})
            except Exception:
                pass
        raise
    finally:
        if fetch_span:
            try:
                fetch_span.end(
                    output={"skill_type": skill.get("type") if skill is not None else None}
                )
            except Exception as exc:
                logger.warning("Langfuse fetch_skill span end failed: %s", exc)

    if lf_trace:
        try:
            execute_span = lf_trace.span(name="execute", input={"args": sanitize_args(args)})
        except Exception as exc:
            logger.warning("Langfuse execute span start failed: %s", exc)
            execute_span = None
    else:
        execute_span = None

    execute_ok = False
    try:
        skill_type = skill.get("type")
        if skill_type == "mcp":
            transport = skill.get("mcp_transport") or "stdio"
            if transport == "stdio" and (
                "org_id" not in skill or skill["org_id"] is not None
            ):
                raise ValueError("MCP stdio execution is restricted to operator-managed global skills")
            result = await MCPClient.call_server(
                # dict.get(k, default) only applies the
                # default when the key is absent — business-backend's API
                # response always includes the key (possibly as null), so
                # this must handle an explicit None too, not just a missing
                # key, or a validly-created type=mcp skill with no
                # transport set 500s at call time.
                transport=transport,
                endpoint=skill["mcp_endpoint"],
                tool_name=skill.get("activity_name") or "default",
                args=args,
                auth_token=skill.get("mcp_auth_token"),
            )
        elif skill_type == "tool":
            if skill.get("activity_name"):
                activity_name = skill["activity_name"]
                if activity_name == "execute_hybrid_search":
                    knowledge_base_id = args.get("knowledge_base_id")
                    if not knowledge_base_id:
                        raise ValueError(
                            "Hybrid search requires a knowledge_base_id argument"
                        )
                    if not session_token:
                        raise ValueError(
                            "Hybrid search requires a conversation session token"
                        )
                    result = await rag_client.search(
                        args.get("query", ""),
                        args.get("limit", 5),
                        knowledge_base_id=str(knowledge_base_id),
                        session_token=session_token,
                    )
                else:
                    result = {
                        "placeholder": True,
                        "activity_name": activity_name,
                        "args": args,
                    }
            elif skill.get("mcp_endpoint"):
                # this is an admin-configured, per-skill
                # endpoint, not an MCP server we run ourselves — sending the
                # worker's own internal service token here leaks it to
                # whatever URL an admin configured. Use the skill's own
                # mcp_auth_token (same field the "mcp" branch above already
                # uses), which is empty/None for skills that don't set one.
                headers = {}
                mcp_auth_token = skill.get("mcp_auth_token")
                if mcp_auth_token:
                    headers["Authorization"] = f"Bearer {mcp_auth_token}"
                async with httpx.AsyncClient(timeout=60) as client:
                    async with client.stream(
                        "POST",
                        skill["mcp_endpoint"],
                        json={
                            "tool": skill.get("activity_name", "default"),
                            "arguments": args,
                        },
                        headers=headers,
                    ) as response:
                        response.raise_for_status()
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > _MAX_MCP_HTTP_RESPONSE_BYTES:
                                raise ValueError(
                                    f"Tool skill {skill_id} response exceeded "
                                    f"{_MAX_MCP_HTTP_RESPONSE_BYTES} bytes"
                                )
                        result = json.loads(body)
            else:
                raise ValueError(
                    f"Tool skill {skill_id} has no activity_name or mcp_endpoint"
                )
        elif skill_type == "code":
            # Stage-2 stopgap: one shell command per call, through the
            # existing single-round tool loop — a fresh sandbox session is
            # opened, the command runs once, and the session is torn down
            # immediately. The real multi-round agentic sub-loop (LLM sees
            # output, decides the next command, repeats within one session)
            # is a separate child-workflow path, not this activity.
            command = args.get("command")
            if not command:
                raise ValueError(
                    f"Code skill {skill_id} call is missing a 'command' arg"
                )
            session = await skill_executor_client.open_session(
                skill, conversation_id=conversation_id
            )
            try:
                result = await skill_executor_client.exec_command(
                    session["session_id"], command
                )
            finally:
                await skill_executor_client.close_session(session["session_id"])
        else:
            raise ValueError(f"Unsupported skill type: {skill_type}")
        execute_ok = True
    except (ValueError, KeyError, SkillExecutorError) as exc:
        if lf_trace:
            try:
                lf_trace.update(output={"error": str(exc)})
            except Exception as lf_exc:
                logger.warning("Langfuse error update failed: %s", lf_exc)
        raise ApplicationError(str(exc), non_retryable=True) from exc
    except Exception as exc:
        if lf_trace:
            try:
                lf_trace.update(output={"error": f"{type(exc).__name__}: {exc}"})
            except Exception as lf_exc:
                logger.warning("Langfuse error trace update failed: %s", lf_exc)
        raise
    finally:
        if execute_span:
            try:
                if execute_ok:
                    output = {"result": sanitize_content(str(result))}
                else:
                    output = {"error": "execution failed before result"}
                execute_span.end(output=output)
            except Exception as exc:
                logger.warning("Langfuse execute span end failed: %s", exc)

    if lf_trace:
        try:
            lf_trace.update(output={"result": sanitize_content(str(result))})
        except Exception as exc:
            logger.warning("Langfuse trace update failed: %s", exc)

    return {"skill_id": skill_id, "output": result}


@activity.defn(name="open_skill_session")
@tracer.start_as_current_span("open_skill_session")
async def open_skill_session(
    skill_id: str,
    conversation_id: str | None = None,
    session_token: str | None = None,
) -> dict[str, Any]:
    """Open a multi-round sandbox session for a code skill — called once at
    the start of CodeSkillSessionWorkflow, unlike execute_skill's code branch
    which opens/execs/closes a session for a single one-shot command."""
    trace.get_current_span().set_attribute("skill_id", skill_id)
    skill = await fetch_skill(skill_id, session_token=session_token)
    try:
        return await skill_executor_client.open_session(
            skill, conversation_id=conversation_id
        )
    except SkillExecutorError as exc:
        # Non-retryable by contract (see SkillExecutorError docstring) —
        # without this, Temporal's default policy retries the failed call
        # forever and the calling workflow hangs.
        raise ApplicationError(str(exc), non_retryable=True) from exc


@activity.defn(name="exec_in_skill_session")
@tracer.start_as_current_span("exec_in_skill_session")
async def exec_in_skill_session(
    session_id: str, command: str, timeout: float | None = None
) -> dict[str, Any]:
    trace.get_current_span().set_attribute("session_id", session_id)
    try:
        return await skill_executor_client.exec_command(
            session_id, command, timeout=timeout
        )
    except SkillExecutorError as exc:
        # Same contract as open_skill_session — a 404 for a reaped session
        # will never succeed on retry, so fail fast instead of hanging.
        raise ApplicationError(str(exc), non_retryable=True) from exc


@activity.defn(name="close_skill_session")
@tracer.start_as_current_span("close_skill_session")
async def close_skill_session(session_id: str) -> None:
    """Idempotent (see skill_executor_client.close_session) — the calling
    workflow's try/finally calls this even after a failed round, and
    skill-executor's own TTL reaper is the backstop if this activity itself
    never runs (worker crash, workflow timeout)."""
    trace.get_current_span().set_attribute("session_id", session_id)
    await skill_executor_client.close_session(session_id)
