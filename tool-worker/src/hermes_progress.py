"""Safe progress of a Hermes turn on the conversation's public event stream
(docs/HERMES_AGENT.md, phase 4b).

Only tool names Hermes is configured to offer reach the browser, mapped to
platform ids; anything else is `other`. Arguments, previews, reasoning,
tool output and the answer's deltas never leave the worker: the answer
arrives with the turn's end. Keepalives hold the stream open through quiet
minutes, and the backend's relay drops them.

Hermes hands a run's events to one subscriber only, so a retried activity
sees the events from its reattach on; call ids carry the attempt, so a
retry adds cards instead of overwriting the earlier ones.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import redis.asyncio as redis
import redis.exceptions as redis_exceptions

from src.config import settings

logger = logging.getLogger(__name__)

# Cards a single turn shows; later tools still run, unseen.
MAX_CALLS = 50
_MCP_PREFIX = "mcp__aurum__"
# Hermes's own tools left on by skill-executor's configuration, and the
# platform tools of the MCP bridge; the values are the public ids.
_BUILTIN = {
    "terminal": "terminal",
    "process": "terminal",
    "read_terminal": "terminal",
    "close_terminal": "terminal",
    "execute_code": "code",
    "read_file": "files",
    "write_file": "files",
    "patch": "files",
    "search_files": "files",
    "todo": "plan",
    "memory": "memory",
    "session_search": "memory",
    "skill_view": "skills",
    "skills_list": "skills",
    "skill_manage": "skills",
    "delegate_task": "delegate",
    "vision_analyze": "vision",
}
_PLATFORM = {
    "knowledge_search": "knowledge",
    "web_search": "web_search",
    "web_read": "web_read",
    "generate_image": "image",
    "edit_image": "image",
    "list_skills": "skills",
    "run_skill": "skills",
}
# English labels for clients without the catalog; business-ui translates by id.
LABELS = {
    "terminal": "Running commands",
    "code": "Running code",
    "files": "Working with files",
    "plan": "Planning",
    "memory": "Recalling",
    "skills": "Using a skill",
    "delegate": "Delegating a task",
    "vision": "Looking at an image",
    "knowledge": "Searching knowledge",
    "web_search": "Searching sources",
    "web_read": "Reading page",
    "image": "Working on an image",
    "other": "Using a tool",
}


def public_tool(name: object) -> str:
    if not isinstance(name, str):
        return "other"
    if name.startswith(_MCP_PREFIX):
        return _PLATFORM.get(name[len(_MCP_PREFIX) :], "other")
    return _BUILTIN.get(name, "other")


class Publisher:
    def __init__(self) -> None:
        self._client: redis.Redis | None = None

    async def publish(self, org_id: str, conversation_id: str, payload: dict[str, Any]) -> None:
        if self._client is None:
            self._client = redis.from_url(settings.redis_url)  # type: ignore[no-untyped-call]
        try:
            await self._client.publish(f"{org_id}:session:{conversation_id}:events", json.dumps(payload))
        except redis_exceptions.RedisError:
            # Progress is best effort; the answer is persisted either way.
            logger.warning("Could not publish Hermes progress")


publisher = Publisher()


class TurnProgress:
    """Turns one attempt's Hermes events into tool cards, published and kept
    for the answer, so a reload shows what the live stream showed."""

    def __init__(self, org_id: str, conversation_id: str, turn_id: str, attempt: int) -> None:
        self._org_id = org_id
        self._conversation_id = conversation_id
        self._turn_id = turn_id
        self._attempt = attempt
        self._open: dict[str, list[str]] = {}
        self.calls: list[dict[str, Any]] = []
        self.results: dict[str, dict[str, Any]] = {}

    async def _publish(self, event_type: str, data: dict[str, Any]) -> None:
        if not self._org_id:
            return
        payload = {"type": event_type, "data": {**data, "turn_id": self._turn_id}, "turn_id": self._turn_id}
        await publisher.publish(self._org_id, self._conversation_id, payload)

    async def keepalive(self) -> None:
        await self._publish("keepalive", {})

    async def event(self, event: dict[str, Any]) -> None:
        kind = event.get("event")
        if kind == "tool.started":
            if len(self.calls) >= MAX_CALLS:
                return
            tool = public_tool(event.get("tool"))
            call = {
                "call_id": f"hermes-{self._attempt}-{len(self.calls)}",
                "tool": f"hermes.{tool}",
                "label": LABELS[tool],
            }
            self.calls.append(call)
            self._open.setdefault(str(event.get("tool")), []).append(call["call_id"])
            await self._publish("tool-calls", {"calls": [call]})
        elif kind in {"tool.completed", "tool.failed"}:
            # Hermes's events carry no call id; tools of one name finish in order.
            started = self._open.get(str(event.get("tool"))) or []
            if not started:
                return
            call_id = started.pop(0)
            call = next(call for call in self.calls if call["call_id"] == call_id)
            failed = kind == "tool.failed" or event.get("error") is True
            result = {"tool": call["tool"], "status": "failed" if failed else "completed"}
            self.results[call_id] = result
            await self._publish(
                "tool-result", {"call_id": call_id, "tool": call["tool"], "label": call["label"], "result": result}
            )


@asynccontextmanager
async def keepalive(progress: TurnProgress, every: float | None = None) -> AsyncIterator[None]:
    interval = settings.hermes_keepalive_seconds if every is None else every

    async def beat() -> None:
        while True:
            await asyncio.sleep(interval)
            await progress.keepalive()

    task = asyncio.create_task(beat())
    try:
        yield
    finally:
        task.cancel()
