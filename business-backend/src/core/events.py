import asyncio
import json
from typing import Any, AsyncGenerator

from redis.asyncio.client import PubSub

from src.core.cache import redis_client


class EventBus:
    async def publish(self, channel: str, event: dict[str, Any]) -> None:
        await redis_client.client.publish(channel, json.dumps(event))

    async def subscribe_channel(self, channel: str) -> PubSub:
        """Subscribes to `channel` and returns the live pubsub connection.

        Call this *before* triggering whatever will publish to the channel
        (e.g. signaling the conversation workflow) — Redis pub/sub does not
        buffer messages for subscribers that join late, so subscribing after
        the publisher has already started can silently drop the first events
        (this bit thinking-delta/message-start events, which fire within
        milliseconds of the workflow starting).
        """
        pubsub = redis_client.client.pubsub()
        await pubsub.subscribe(channel)
        return pubsub

    async def listen(
        self, pubsub: PubSub, channel: str, timeout_seconds: int = 120,
        *, reset_timeout_on_message: bool = False,
    ) -> AsyncGenerator[dict[str, Any], None]:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout_seconds
        message_iter = pubsub.listen()
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    # A channel with no publishes at all (e.g. the runner
                    # crashed before emitting anything) never satisfies
                    # `async for` on pubsub.listen() — the deadline check
                    # only ran *after* a message arrived, so a genuinely
                    # silent channel hung open until the client disconnected
                    # instead of ever hitting timeout_seconds
                    #. wait_for bounds each wait to
                    # whatever's left of the real deadline.
                    message = await asyncio.wait_for(message_iter.__anext__(), timeout=remaining)
                except (asyncio.TimeoutError, StopAsyncIteration):
                    break
                if message["type"] == "message":
                    if reset_timeout_on_message:
                        deadline = loop.time() + timeout_seconds
                    yield json.loads(message["data"])
        finally:
            # unsubscribe() alone only sends the UNSUBSCRIBE command — it never
            # calls connection_pool.release(). Without aclose(), every stream
            # permanently holds a connection out of the shared pool
            # (max_connections=50, core/cache.py) until the pool is exhausted
            # and every /v1/* route (rate-limiter, session_manager, ...) starts
            # failing with "Too many connections".
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()  # type: ignore[no-untyped-call]

    async def subscribe(
        self, channel: str, timeout_seconds: int = 120
    ) -> AsyncGenerator[dict[str, Any], None]:
        pubsub = await self.subscribe_channel(channel)
        async for event in self.listen(pubsub, channel, timeout_seconds):
            yield event


event_bus = EventBus()
