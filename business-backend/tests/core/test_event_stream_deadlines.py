import asyncio
import json

import pytest

from src.core.events import EventBus


class TimedPubsub:
    def __init__(self, *, silent=False):
        self.silent = silent
        self.closed = False
        self.unsubscribed = False

    async def listen(self):
        if self.silent:
            await asyncio.Event().wait()
        for sequence in range(8):
            await asyncio.sleep(0.04)
            yield {"type": "message", "data": json.dumps({"type": "progress", "data": sequence})}

    async def unsubscribe(self, channel):
        self.unsubscribed = True

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_ordinary_stream_retains_absolute_deadline():
    pubsub = TimedPubsub()
    events = [event async for event in EventBus().listen(pubsub, "test", timeout_seconds=0.2)]
    assert 0 < len(events) < 8
    assert pubsub.closed and pubsub.unsubscribed


@pytest.mark.asyncio
async def test_workflow_stream_still_times_out_when_runner_is_silent():
    pubsub = TimedPubsub(silent=True)

    async def consume():
        return [event async for event in EventBus().listen(
            pubsub, "test", timeout_seconds=0.1, reset_timeout_on_message=True,
        )]

    assert await asyncio.wait_for(consume(), timeout=1) == []
    assert pubsub.closed and pubsub.unsubscribed
