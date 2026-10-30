"""A file Hermes Agent attaches to its answer over the MCP bridge.

business-backend reserves the operation and starts one workflow per
operation, so a repeated request attaches to the same run. Its activity
(hermes_activities.hermes_attach_file) reads the file from the sandbox
through skill-executor and hands the bytes straight to business-backend,
which checks what they are and stores them: the bytes never enter workflow
history, only the stored file's metadata or an error code. Like a skill call,
a failure is a completed run with its code.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

HERMES_FILE_WORKFLOW = "HermesFileAttachWorkflow"


@dataclass(frozen=True)
class HermesFileAttach:
    session_id: str
    operation_id: str
    path: str
    session_token: str


@workflow.defn(name=HERMES_FILE_WORKFLOW)
class HermesFileAttachWorkflow:
    @workflow.run
    async def run(self, call: HermesFileAttach) -> dict[str, Any]:
        try:
            result: dict[str, Any] = await workflow.execute_activity(
                "hermes_attach_file",
                call,
                start_to_close_timeout=timedelta(seconds=90),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except ActivityError:
            return {"error": "file_unavailable"}
        return result
