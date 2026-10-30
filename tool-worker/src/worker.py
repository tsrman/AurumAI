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
from src.hermes_activities import (
    hermes_answer_question,
    hermes_attach_file,
    hermes_bind_turn,
    hermes_close_session,
    hermes_load_memory,
    hermes_prepare_sandbox,
    hermes_purge_conversation,
    hermes_retire_sandbox,
    hermes_run_turn,
    hermes_save_memory,
    hermes_save_skills,
    hermes_hydrate_skills,
    hermes_sync_inputs,
    hermes_unbind_turn,
)
from src.hermes_session_workflow import HEARTBEAT_THROTTLE, HermesSessionWorkflow
from src.hermes_purge_workflow import HermesConversationPurgeWorkflow
from src.hermes_file_workflow import HermesFileAttachWorkflow
from src.hermes_skill_workflow import HermesSkillCallWorkflow

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
        workflows=[
            HermesSkillCallWorkflow,
            HermesFileAttachWorkflow,
            HermesSessionWorkflow,
            HermesConversationPurgeWorkflow,
        ],
        max_heartbeat_throttle_interval=HEARTBEAT_THROTTLE,
        activities=[
            execute_skill,
            open_skill_session,
            exec_in_skill_session,
            close_skill_session,
            hermes_prepare_sandbox,
            hermes_answer_question,
            hermes_purge_conversation,
            hermes_load_memory,
            hermes_save_memory,
            hermes_save_skills,
            hermes_hydrate_skills,
            hermes_sync_inputs,
            hermes_bind_turn,
            hermes_unbind_turn,
            hermes_run_turn,
            hermes_retire_sandbox,
            hermes_close_session,
            hermes_attach_file,
        ],
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
