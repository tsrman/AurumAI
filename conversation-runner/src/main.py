import asyncio
import contextlib
import logging
import signal
from collections.abc import Generator
from types import FrameType

import uvicorn

from src.config import settings
from src.event_publisher import event_publisher
from src.health import get_bind_event, health_app
from src.telemetry import init_telemetry
from src.worker import connect as connect_temporal, run as run_worker

tracer = init_telemetry("conversation-runner", endpoint=settings.otel_exporter_otlp_endpoint)

logger = logging.getLogger(__name__)


def _raise_keyboard_interrupt(signum: int, frame: FrameType | None) -> None:
    raise KeyboardInterrupt()


@contextlib.contextmanager
def _noop_capture_signals() -> Generator[None, None, None]:
    yield


async def async_main() -> None:
    logger.info("conversation-runner starting")
    logger.info(
        "conversation_id=%s", settings.conversation_id or "(unbound — pool mode)"
    )

    # Runs the health/bind HTTP server in this same event loop (not a
    # separate thread) — /bind needs to set an asyncio.Event that the code
    # below awaits, and asyncio.Event isn't safe to signal across loops in
    # different threads.
    config = uvicorn.Config(health_app, host="0.0.0.0", port=8080, log_level="warning")
    server = uvicorn.Server(config)
    # We own SIGTERM ourselves (see main()'s signal.signal call below) —
    # without this, uvicorn.Server.serve()'s capture_signals() context
    # manager silently overrides the process's SIGTERM handler with its own
    # for as long as serve() runs (i.e. the whole process lifetime), so a
    # real SIGTERM would stop only this small health/bind HTTP server, not
    # the actual Temporal worker loop, and never reach _raise_keyboard_interrupt
    # or the event_publisher.close() cleanup in main()'s finally block.
    # (The old `server.install_signal_handlers = lambda: None` here was
    # written for a pre-0.34 uvicorn API — that attribute no longer exists,
    # so it silently did nothing.)
    server.capture_signals = _noop_capture_signals  # type: ignore[method-assign]
    server_task = asyncio.create_task(server.serve())

    # Connect to Temporal concurrently with waiting for /bind (pool mode) —
    # this, plus Python/imports already having happened by the time a pool
    # container is claimed, is what eliminates the ~3-4s cold-start tax a
    # freshly-created container pays before it can process its first turn.
    client_task = asyncio.create_task(connect_temporal())

    try:
        if not settings.conversation_id:
            logger.info("no conversation_id at boot — waiting for POST /bind")
            await get_bind_event().wait()
        logger.info("user_id=%s", settings.user_id)
        logger.info("task_queue=%s", settings.task_queue)
        client = await client_task
        await run_worker(client)
    finally:
        server.should_exit = True
        await server_task


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)

    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        logger.info("Shutting down conversation-runner")
    finally:
        try:
            asyncio.run(event_publisher.close())
        except Exception as exc:
            logger.exception("Error closing event publisher: %s", exc)


if __name__ == "__main__":
    main()
