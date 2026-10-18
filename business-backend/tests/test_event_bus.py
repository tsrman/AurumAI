import asyncio

import pytest
import pytest_asyncio
import redis.asyncio as redis
from testcontainers.redis import RedisContainer

from src.core.cache import redis_client
from src.core.events import event_bus


@pytest_asyncio.fixture
async def redis_url() -> str:
    with RedisContainer("redis:7-alpine") as container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(6379)
        url = f"redis://{host}:{port}/0"
        redis_client._client = redis.from_url(
            url,
            encoding="utf-8",
            decode_responses=True,
            max_connections=50,
        )
        yield url
        await redis_client.disconnect()


@pytest.mark.asyncio
async def test_event_bus_publish_subscribe_round_trip(redis_url: str) -> None:
    channel = "test:events"
    event = {"type": "ping", "data": {"seq": 42}}

    received: list[dict] = []

    async def consume() -> None:
        async for message in event_bus.subscribe(channel):
            received.append(message)
            return

    async def produce() -> None:
        await asyncio.sleep(0.3)
        await event_bus.publish(channel, event)

    await asyncio.wait_for(
        asyncio.gather(consume(), produce()),
        timeout=5.0,
    )

    assert received == [event]


@pytest.mark.asyncio
async def test_listen_times_out_on_a_channel_that_never_publishes(redis_url: str) -> None:
    """Regression test: the deadline was only checked
    *after* a message arrived from pubsub.listen() — a channel that never
    publishes anything (e.g. the runner crashed before emitting a single
    event) hung the generator open forever instead of ever hitting
    timeout_seconds."""
    channel = "test:silent-channel"
    pubsub = await event_bus.subscribe_channel(channel)

    async def consume() -> list[dict]:
        received: list[dict] = []
        async for message in event_bus.listen(pubsub, channel, timeout_seconds=1):
            received.append(message)
        return received

    # Safety net, not the assertion itself — if the fix regresses, this
    # fails the test with a clear timeout instead of hanging the whole
    # suite forever.
    received = await asyncio.wait_for(consume(), timeout=5.0)

    assert received == []
