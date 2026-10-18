import asyncio
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from src.activities import (
    close_skill_session,
    execute_skill,
    exec_in_skill_session,
    open_skill_session,
)
from src.config import settings

logger = logging.getLogger(__name__)


async def main() -> None:
    max_retries = 5
    base_delay = 1.0
    client = None

    for attempt in range(max_retries + 1):
        try:
            client = await Client.connect(
                settings.temporal_host,
                namespace=settings.temporal_namespace,
            )
            break
        except Exception as exc:
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                logger.warning(
                    "Failed to connect to Temporal (attempt %d/%d): %s. Retrying in %.1fs...",
                    attempt + 1,
                    max_retries + 1,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    "Failed to connect to Temporal after %d attempts: %s",
                    max_retries + 1,
                    exc,
                )
                raise

    # The loop above only exits without `client` set by re-raising on the
    # final attempt — reaching here always means a successful connection.
    assert client is not None

    worker = Worker(
        client,
        task_queue=settings.task_queue,
        activities=[
            execute_skill,
            open_skill_session,
            exec_in_skill_session,
            close_skill_session,
        ],
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
