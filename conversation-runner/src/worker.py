import asyncio

from temporalio.client import Client
from temporalio.worker import Worker

from src.config import settings
from src.workflows.conversation import ConversationWorkflow
from src.workflows.agent import AgentWorkflow
from src.workflows.code_skill_session import CodeSkillSessionWorkflow
from src.workflows.evaluator import EvaluationWorkflow
from src.workflows.multi_agent import MultiAgentWorkflow
from src.workflows.activities import (
    authorize_workflow_step,
    consume_user_question,
    create_user_question,
    expire_user_question,
    execute_web_tool,
    fail_interaction_turn,
    llm_call,
    load_workflow_run_snapshot,
    load_conversation_state,
    list_available_skills,
    get_session_token,
    get_conversation_id,
    get_conversation_temperature,
    get_evaluator_config,
    persist_assistant_message,
    publish_event,
    read_user_question,
    persist_workflow_node_result,
    record_langfuse_score,
    record_message_score,
    refresh_runner_activity,
    register_interaction_execution,
    register_conversation_execution,
    transition_workflow_run,
)


async def connect() -> Client:
    return await Client.connect(settings.temporal_host, namespace=settings.temporal_namespace)


async def run(client: Client) -> None:
    task_queue = settings.task_queue or f"session-{settings.conversation_id}"
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[
            ConversationWorkflow,
            AgentWorkflow,
            CodeSkillSessionWorkflow,
            EvaluationWorkflow,
            MultiAgentWorkflow,
        ],
        activities=[
            llm_call,
            load_conversation_state,
            list_available_skills,
            get_session_token,
            get_conversation_id,
            get_conversation_temperature,
            get_evaluator_config,
            persist_assistant_message,
            publish_event,
            record_langfuse_score,
            record_message_score,
            load_workflow_run_snapshot,
            authorize_workflow_step,
            transition_workflow_run,
            persist_workflow_node_result,
            refresh_runner_activity,
            register_interaction_execution,
            register_conversation_execution,
            create_user_question,
            read_user_question,
            consume_user_question,
            expire_user_question,
            fail_interaction_turn,
            execute_web_tool,
        ],
    )
    await worker.run()


async def main() -> None:
    client = await connect()
    await run(client)


if __name__ == "__main__":
    asyncio.run(main())
