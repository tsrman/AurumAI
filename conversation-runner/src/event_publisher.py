import json
import logging
from typing import Any

import redis.asyncio as redis
# `import redis.exceptions` without `as` would rebind the name `redis` to the
# top-level sync package (Python's dotted-import binds the top-level name),
# silently undoing the `as redis` alias above and turning every `self.client`
# into a *synchronous* redis.client.Redis — self.client.publish() then
# returns a plain int instead of a coroutine, breaking every `await` on it.
import redis.exceptions as redis_exceptions

from src.config import settings

logger = logging.getLogger(__name__)

_STATUS_EVENT_FIELDS = {
    "question-status": ("questionId", "turnId", "status", "version"),
    "turn-status": ("turnId", "status", "version"),
}


class EventPublisher:
    def __init__(self) -> None:
        self._client: redis.Redis | None = None

    @property
    def client(self) -> redis.Redis:
        if self._client is None:
            # redis-py's from_url classmethod itself has no return type
            # annotation (upstream gap, not ours) — mypy flags the call as
            # untyped regardless of the assignment target's declared type.
            self._client = redis.from_url(settings.redis_url)  # type: ignore[no-untyped-call]
        return self._client

    async def publish(self, event_type: str, data: dict[str, Any]) -> None:
        status_fields = _STATUS_EVENT_FIELDS.get(event_type)
        if status_fields is not None:
            # Status streams are a metadata-only trust boundary. Even if a
            # future workflow caller accidentally supplies a prompt, answer or
            # tool output, never place it on the browser-facing Redis channel.
            data = {field: data[field] for field in status_fields if field in data}
        channel = f"{settings.org_id or '-'}:session:{settings.conversation_id}:events"
        payload = {"type": event_type, "data": data}
        # The backend's SSE relay routes on the envelope's top-level turn_id and
        # lets anything without one through on every open stream for the
        # conversation.  The workflow progress events carry the turn as camelCase
        # turnId (their payloads are projected onto the frontend's safe
        # allowlist), so accept both spellings here rather than putting a second
        # snake_case key into those payloads.
        if "turn_id" in data:
            payload["turn_id"] = data["turn_id"]
        elif "turnId" in data:
            payload["turn_id"] = data["turnId"]
        try:
            await self.client.publish(
                channel,
                json.dumps(payload),
            )
        except redis_exceptions.RedisError as exc:
            logger.exception("Failed to publish event to Redis: %s", exc)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


event_publisher = EventPublisher()
