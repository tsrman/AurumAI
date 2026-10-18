import asyncio
import logging
import signal
import threading
from types import FrameType

from src.config import settings
from src.health import run_health_server
from src.langfuse_client import shutdown_langfuse
from src.telemetry import init_telemetry
from src.worker import main as run_worker

tracer = init_telemetry("tool-worker", endpoint=settings.otel_exporter_otlp_endpoint)

logger = logging.getLogger(__name__)


def _raise_keyboard_interrupt(signum: int, frame: FrameType | None) -> None:
    raise KeyboardInterrupt()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    logger.info("tool-worker starting")
    logger.info("task_queue=%s", settings.task_queue)
    logger.info("temporal_host=%s", settings.temporal_host)
    logger.info("backend_api_url=%s", settings.backend_api_url)

    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)

    health_thread = threading.Thread(
        target=run_health_server,
        kwargs={"port": 8080},
        daemon=True,
    )
    health_thread.start()

    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        logger.info("Shutting down tool-worker")
    except Exception as exc:
        logger.fatal("tool-worker failed: %s", exc)
        raise
    finally:
        shutdown_langfuse()


if __name__ == "__main__":
    main()
