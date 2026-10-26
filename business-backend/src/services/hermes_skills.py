"""Platform skills for Hermes Agent over the MCP bridge (docs/HERMES_AGENT.md, phase 3b).

Skills are granted per user and change over time, while Hermes lists the
bridge's tools once, so the bridge offers two fixed tools: one lists the
skills the user may run right now, the other runs one of them. Only MCP and
tool skills qualify; code skills reach Hermes with phase 5, and a
conversation's own attachment search is knowledge_search.

A run goes where the runner's skill calls go: execute_skill on tool-worker's
queue, the only service that talks to skill endpoints. One workflow per call,
its id derived from the call, makes a repeated request attach to the same run.
tool-worker fetches the skill with a runner session token for the
conversation, minted here, as the runner itself passes one.
"""

import asyncio
import json
from datetime import timedelta
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import Client, WorkflowFailureError
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from src.core.authorization_types import (
    AccessPermission,
    AuthorizationSubject,
    ResourceRef,
    ResourceType,
)
from src.core.config import settings
from src.core.temporal import get_temporal_client
from src.models.skill import Skill
from src.services.authorization import authorization_service
from src.services.session_token import session_token_service
from src.services.skill_registry import skill_registry

# tool-worker's src/hermes_skill_workflow.py.
HERMES_SKILL_WORKFLOW = "HermesSkillCallWorkflow"
SKILL_TYPES = frozenset({"mcp", "tool"})
LIST_LIMIT = 100
_DESCRIPTION_CHARS = 2000
OUTPUT_CHARS = 50_000
# Long enough for execute_skill's bounded retries; a run nobody waits for ends.
_EXECUTION_TIMEOUT = timedelta(minutes=10)


class SkillToolError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ListSkillsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunSkillRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_id: UUID
    arguments: dict[str, Any] = Field(default_factory=dict)


def _runnable(skill: Skill) -> bool:
    return skill.type in SKILL_TYPES and skill.owning_conversation_id is None


async def list_skills(session: AsyncSession, subject: AuthorizationSubject) -> dict[str, Any]:
    """Ids, descriptions and input schemas only: never endpoints or credentials."""
    skills = await skill_registry.list_accessible_skills(session, subject, LIST_LIMIT, 0)
    return {
        "skills": [
            {
                "skillId": str(skill.id),
                "name": skill.name,
                "description": (skill.description or "")[:_DESCRIPTION_CHARS],
                "inputSchema": skill.input_schema
                if isinstance(skill.input_schema, dict)
                else {"type": "object"},
            }
            for skill in skills
            if _runnable(skill)
        ]
    }


async def run_skill(
    session: AsyncSession,
    subject: AuthorizationSubject,
    request: RunSkillRequest,
    *,
    conversation_id: UUID,
    generation: int,
    invocation: UUID,
    client: Client | None = None,
) -> dict[str, Any]:
    """The skill's output, bounded; raises SkillToolError."""
    skill = await skill_registry.get_skill(session, str(request.skill_id))
    if (
        skill is None
        or not _runnable(skill)
        or not await authorization_service.can_access(
            session,
            subject,
            ResourceRef(ResourceType.SKILL, str(skill.id)),
            AccessPermission.READ,
            resource_org_id=skill.org_id,
        )
    ):
        raise SkillToolError("skill_not_found")
    skill_id = str(skill.id)
    # Release the fenced transaction before waiting on the skill.
    await session.commit()
    token = session_token_service.issue(
        str(conversation_id), str(subject.user_id), str(subject.org_id), generation=generation
    )
    temporal = client or get_temporal_client()
    workflow_id = f"hermes-skill-{invocation}"
    try:
        handle = await temporal.start_workflow(
            HERMES_SKILL_WORKFLOW,
            {
                "skill_id": skill_id,
                "conversation_id": str(conversation_id),
                "session_token": token,
                "arguments": request.arguments,
            },
            id=workflow_id,
            task_queue=settings.HERMES_SKILL_TASK_QUEUE,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            execution_timeout=_EXECUTION_TIMEOUT,
        )
    except WorkflowAlreadyStartedError:
        # The same call again: wait for the run it started.
        handle = temporal.get_workflow_handle(workflow_id)
    try:
        async with asyncio.timeout(settings.HERMES_SKILL_TIMEOUT_SECONDS):
            result = await handle.result()
    except TimeoutError:
        raise SkillToolError("skill_timeout") from None
    except WorkflowFailureError:
        raise SkillToolError("skill_failed") from None
    if not isinstance(result, dict) or "output" not in result:
        raise SkillToolError("skill_failed")
    output = result["output"]
    text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False, default=str)
    return {"skillId": skill_id, "output": text[:OUTPUT_CHARS], "truncated": len(text) > OUTPUT_CHARS}
