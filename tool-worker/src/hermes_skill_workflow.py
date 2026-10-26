"""A platform skill that Hermes Agent runs over the MCP bridge (docs/HERMES_AGENT.md, phase 3b).

business-backend starts one workflow per call, its id derived from the call,
so a repeated request attaches to the same run instead of calling the skill
again. The workflow only runs execute_skill on this worker's queue and keeps
the outcome as its result: a failed skill is a completed run with an error
code, never a failure message, which could carry an endpoint or a secret.
"""

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

HERMES_SKILL_WORKFLOW = "HermesSkillCallWorkflow"


@dataclass(frozen=True)
class HermesSkillCall:
    skill_id: str
    conversation_id: str
    session_token: str
    arguments: dict[str, Any] = field(default_factory=dict)


@workflow.defn(name=HERMES_SKILL_WORKFLOW)
class HermesSkillCallWorkflow:
    @workflow.run
    async def run(self, call: HermesSkillCall) -> dict[str, Any]:
        try:
            result = await workflow.execute_activity(
                "execute_skill",
                args=(call.skill_id, call.arguments, call.conversation_id, call.session_token),
                start_to_close_timeout=timedelta(seconds=60),
                # As the runner's own skill calls: bounded, never forever.
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except ActivityError:
            return {"error": "skill_failed"}
        return {"output": result.get("output") if isinstance(result, dict) else None}
