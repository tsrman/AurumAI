"""Activities of a Hermes Agent session (docs/HERMES_AGENT.md, phase 4).

They talk to business-backend's runner routes for the session record, with
the conversation's runner token the workflow fetches from the runner, and to
skill-executor for the sandbox. Nothing the sandbox says is trusted: the run
stream is read for its result line and its tool events only, bounded:
the result becomes a bare outcome, the events safe progress (see
hermes_progress).
"""

import asyncio
import json
from typing import Any

import httpx
from temporalio import activity
from temporalio.exceptions import ApplicationError

from src.config import settings
from src.hermes_progress import TurnProgress, keepalive
from src.hermes_questions import ParentQuestions

_HEARTBEAT_SECONDS = 10.0
_MAX_OUTPUT_CHARS = 200_000
# skill-executor's codes that no retry of the same call can fix.
_FINAL = frozenset(
    {
        "hermes_capacity_exhausted",
        "hermes_workspace_full",
        "hermes_workspace_lost",
        "hermes_sandbox_not_found",
        "hermes_sandbox_stopped",
        "hermes_session_not_found",
        "hermes_session_closed",
        "hermes_session_conflict",
        "hermes_correlation_invalid",
        "hermes_access_denied",
        "hermes_turn_inactive",
    }
)


def _failure(code: str) -> ApplicationError:
    return ApplicationError(code, type=code, non_retryable=code in _FINAL)


def _code(response: httpx.Response, fallback: str) -> str:
    try:
        detail = response.json().get("detail")
    except ValueError:
        return fallback
    code = detail.get("code") if isinstance(detail, dict) else None
    return code if isinstance(code, str) and code.startswith("hermes_") and len(code) <= 64 else fallback


async def _backend(method: str, path: str, token: str, body: dict[str, Any] | None = None) -> httpx.Response:
    async with httpx.AsyncClient(timeout=30.0) as client:
        return await client.request(
            method,
            f"{settings.backend_api_url}/v1/internal/hermes{path}",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
        )


async def _executor(method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 120.0) -> httpx.Response:
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.request(method, f"{settings.skill_executor_url}/hermes{path}", json=body)


@activity.defn(name="hermes_prepare_sandbox")
async def hermes_prepare_sandbox(token: str, org_id: str, conversation_id: str, model: str, turn_id: str = "") -> str:
    """The session whose sandbox runs the next turn, as skill-executor's id.

    A running sandbox keeps the token it read at its start, so its session is
    only reused; otherwise opening rotates the token and the sandbox is
    created, restored or resumed with the new one. Restoring can take
    minutes, so the turn's stream is kept open meanwhile.
    """
    progress = TurnProgress(org_id if turn_id else "", conversation_id, turn_id, activity.info().attempt)
    async with keepalive(progress):
        return await _prepare(token, org_id, conversation_id, model)


async def _prepare(token: str, org_id: str, conversation_id: str, model: str) -> str:
    current = await _backend("GET", "/sessions/current", token)
    if current.status_code == 200:
        session_id = str(current.json()["sessionId"]).replace("-", "")
        sandbox = await _executor("GET", f"/sandboxes/{session_id}")
        if sandbox.status_code == 200 and sandbox.json().get("state") == "running":
            return session_id
    elif current.status_code != 404:
        raise _failure(_code(current, "hermes_backend_unavailable"))
    opened = await _backend("POST", "/sessions", token)
    if opened.status_code != 201:
        raise _failure(_code(opened, "hermes_backend_unavailable"))
    issued = opened.json()
    session_id = str(issued["sessionId"]).replace("-", "")
    created = await _executor(
        "POST",
        "/sandboxes",
        {
            "session_id": session_id,
            "org_id": org_id,
            "conversation_id": conversation_id,
            "token": issued["token"],
            "model": model,
        },
        # Restoring a snapshot and waiting for Hermes to start take a while.
        timeout=1200.0,
    )
    if created.status_code not in (200, 201):
        raise _failure(_code(created, "hermes_sandbox_unavailable"))
    return session_id


@activity.defn(name="hermes_bind_turn")
async def hermes_bind_turn(token: str, session_id: str, turn_id: str, execution_id: str) -> None:
    response = await _backend(
        "PUT", f"/sessions/{_uuid(session_id)}/turn", token, {"turnId": turn_id, "executionId": execution_id}
    )
    if response.status_code != 204:
        raise _failure(_code(response, "hermes_backend_unavailable"))


@activity.defn(name="hermes_unbind_turn")
async def hermes_unbind_turn(token: str, session_id: str) -> None:
    response = await _backend("DELETE", f"/sessions/{_uuid(session_id)}/turn", token)
    if response.status_code not in (204, 404):
        raise _failure(_code(response, "hermes_backend_unavailable"))


@activity.defn(name="hermes_close_session")
async def hermes_close_session(token: str, session_id: str) -> None:
    response = await _backend("POST", f"/sessions/{_uuid(session_id)}/close", token)
    if response.status_code not in (204, 404):
        raise _failure(_code(response, "hermes_backend_unavailable"))


@activity.defn(name="hermes_retire_sandbox")
async def hermes_retire_sandbox(session_id: str) -> None:
    """Snapshot and remove an idle sandbox; skill-executor keeps it stopped and
    retries when the snapshot cannot be stored."""
    response = await _executor("DELETE", f"/sandboxes/{session_id}?snapshot=true", timeout=1200.0)
    if response.status_code not in (200, 204, 404, 410, 507):
        raise _failure(_code(response, "hermes_sandbox_unavailable"))


@activity.defn(name="hermes_run_turn")
async def hermes_run_turn(
    session_id: str,
    turn_id: str,
    conversation_id: str,
    text: str,
    history: list[dict[str, Any]],
    org_id: str = "",
    parent_workflow_id: str = "",
) -> dict[str, Any]:
    """Run (or, on a retry, attach to) the turn's Hermes run and return its
    outcome with the tool cards it showed, forwarding its questions to the
    conversation workflow. Heartbeats on a timer, since a run can be quiet
    for minutes; a cancellation asks Hermes to stop the run."""
    # The run's id once Hermes has it, for a cancellation to stop.
    started: list[str] = []
    progress = TurnProgress(org_id, conversation_id, turn_id, activity.info().attempt)
    questions = ParentQuestions(parent_workflow_id, turn_id)

    async def beat() -> None:
        while True:
            activity.heartbeat()
            await asyncio.sleep(_HEARTBEAT_SECONDS)

    beating = asyncio.create_task(beat())
    try:
        async with keepalive(progress):
            return await _follow_run(
                session_id, turn_id, conversation_id, text, history, progress, questions, started
            )
    except asyncio.CancelledError:
        if started:
            try:
                await _executor("POST", f"/sandboxes/{session_id}/runs/{started[0]}/stop", timeout=30.0)
            except httpx.HTTPError:
                pass
        raise
    finally:
        beating.cancel()


async def _follow_run(
    session_id: str,
    turn_id: str,
    conversation_id: str,
    text: str,
    history: list[dict[str, Any]],
    progress: TurnProgress,
    questions: ParentQuestions,
    started: list[str],
) -> dict[str, Any]:
    body = {"key": turn_id, "input": text, "session_id": conversation_id, "conversation_history": history}
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None)) as client:
        async with client.stream(
            "POST", f"{settings.skill_executor_url}/hermes/sandboxes/{session_id}/runs", json=body
        ) as response:
            if response.status_code != 200:
                await response.aread()
                raise _failure(_code(response, "hermes_sandbox_unavailable"))
            async for line in response.aiter_lines():
                message = _parse(line)
                if message.get("type") == "started" and isinstance(message.get("run_id"), str):
                    started.append(message["run_id"])
                elif message.get("type") == "error":
                    raise _failure(str(message.get("code") or "hermes_interrupted")[:64])
                elif message.get("type") == "event" and isinstance(message.get("event"), dict):
                    await progress.event(message["event"])
                    await questions.event(message["event"], started[0] if started else None)
                elif message.get("type") == "result":
                    return _outcome(message, progress)
    raise _failure("hermes_interrupted")


@activity.defn(name="hermes_answer_question")
async def hermes_answer_question(session_id: str, run_id: str, prompt_id: str, generation: int, answer: str) -> str:
    """Answer the run's pending question; "settled" when Hermes no longer waits
    for it (answered, timed out, the run ended)."""
    response = await _executor(
        "POST",
        f"/sandboxes/{session_id}/runs/{run_id}/clarify",
        {"request_id": prompt_id, "execution_generation": generation, "answer": answer},
        timeout=60.0,
    )
    if response.status_code == 200:
        return "answered"
    code = _code(response, "hermes_sandbox_unavailable")
    if code == "hermes_clarify_settled":
        return "settled"
    raise _failure(code)


def _parse(line: str) -> dict[str, Any]:
    try:
        message = json.loads(line)
    except ValueError:
        return {}
    return message if isinstance(message, dict) else {}


def _outcome(message: dict[str, Any], progress: TurnProgress) -> dict[str, Any]:
    status = message.get("status")
    output = message.get("output")
    if status != "completed" or not isinstance(output, str) or not output.strip():
        return {"status": "failed", "error": "hermes_run_failed"}
    outcome: dict[str, Any] = {"status": "completed", "output": output[:_MAX_OUTPUT_CHARS]}
    if progress.calls:
        outcome["tool_calls"] = progress.calls
        outcome["tool_results"] = progress.results
    return outcome


def _uuid(session_id: str) -> str:
    return f"{session_id[:8]}-{session_id[8:12]}-{session_id[12:16]}-{session_id[16:20]}-{session_id[20:]}"
