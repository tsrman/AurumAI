import json
import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import suppress
from typing import Any
from fastapi.responses import StreamingResponse


def sse_event(event_type: str, data: dict[str, Any]) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


def create_streaming_response(generator: AsyncGenerator[str, None]) -> StreamingResponse:
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def reconciliation_ticks(
    notifications: AsyncIterator[dict[str, Any]],
    *,
    interval_seconds: float,
    timeout_seconds: float,
) -> AsyncGenerator[dict[str, Any] | None, None]:
    """Yield immediately, on a notification, and periodically until timeout.

    The pending ``__anext__`` task is not cancelled at every timer boundary:
    cancelling an async-generator read can close Redis PubSub's iterator and
    create a lost-notification window between resubscriptions.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    pending: asyncio.Task[dict[str, Any]] | None = None
    exhausted = False

    async def next_notification() -> dict[str, Any]:
        return await anext(notifications)

    try:
        pending = asyncio.create_task(next_notification())
        # Enter the source generator before exposing the initial snapshot so
        # its cleanup/finally is armed even when that snapshot is terminal.
        await asyncio.sleep(0)
        yield None
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            wait_for = min(interval_seconds, remaining)
            if exhausted:
                await asyncio.sleep(wait_for)
                yield None
                continue
            if pending is None:
                pending = asyncio.create_task(next_notification())
            done, _ = await asyncio.wait({pending}, timeout=wait_for)
            if pending not in done:
                yield None
                continue
            try:
                notification = pending.result()
            except StopAsyncIteration:
                exhausted = True
                pending = None
                continue
            pending = None
            yield notification
    finally:
        if pending is not None:
            if not pending.done():
                pending.cancel()
            with suppress(asyncio.CancelledError, StopAsyncIteration):
                await pending
        close = getattr(notifications, "aclose", None)
        if close is not None:
            with suppress(Exception):
                await close()
