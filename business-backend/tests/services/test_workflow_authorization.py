from uuid import uuid4

import pytest

from src.core.authorization_types import (
    AccessPermission,
    AuthorizationSubject,
    GrantSource,
    PrincipalType,
    ResourceType,
)
from src.models.access_grant import AccessGrant
from src.models.agent_definition import AgentDefinition
from src.models.conversation import Conversation
from src.models.group import Group
from src.models.message import Message
from src.models.skill import Skill
from src.models.user_group import UserGroup
from src.models.workflow_definition import WorkflowDefinition
from src.models.workflow_run import WorkflowRun
from src.services.authorization import AuthorizationService
from src.services.resource_registry import ResourceRegistry
from src.services.workflow_authorization import (
    WorkflowDefinitionNotReadable,
    WorkflowAuthorizationService,
    WorkflowStepAccessDenied,
)


async def catalog() -> list[dict[str, object]]:
    return [{"id": "model-old"}, {"id": "model-new"}]


def grant(org_id, user_id, resource_type: ResourceType, resource_id: str) -> AccessGrant:
    return AccessGrant(
        org_id=org_id,
        resource_type=resource_type.value,
        resource_id=resource_id,
        principal_type=PrincipalType.USER.value,
        principal_id=user_id,
        permission=AccessPermission.READ.value,
        created_via=GrantSource.PROVISIONING.value,
    )


async def test_snapshot_check_uses_snapshot_dependencies_not_edited_agent(
    db, test_org, test_user
) -> None:
    agent = AgentDefinition(
        org_id=test_org.id,
        name="Researcher",
        model_id="model-old",
        system_prompt="Answer",
    )
    db.add(agent)
    await db.flush()
    definition = WorkflowDefinition(
        org_id=test_org.id,
        name=f"Flow-{uuid4()}",
        supervisor_agent_definition_id=agent.id,
    )
    db.add(definition)
    await db.flush()
    conversation = Conversation(
        org_id=test_org.id,
        user_id=test_user.id,
        title="Authorization",
        model="model-old",
    )
    db.add(conversation)
    await db.flush()
    turn_id = uuid4()
    message = Message(
        org_id=test_org.id,
        conversation_id=conversation.id,
        turn_id=turn_id,
        user_id=test_user.id,
        role="user",
        content="Research",
    )
    db.add(message)
    await db.flush()
    run = WorkflowRun(
        org_id=test_org.id,
        workflow_definition_id=definition.id,
        workflow_definition_version=1,
        initiating_user_id=test_user.id,
        conversation_id=conversation.id,
        turn_id=turn_id,
        source_message_id=message.id,
        temporal_workflow_id=f"workflow-{uuid4()}",
        execution_snapshot={
            "supervisor": {
                "agentDefinitionId": str(agent.id),
                "model": "model-old",
                "allowedSkillIds": [],
                "knowledgeBases": [],
            },
            "nodes": [
                {
                    "nodeKey": "research",
                    "agent": {
                        "agentDefinitionId": str(agent.id),
                        "model": "model-old",
                        "allowedSkillIds": [],
                        "knowledgeBases": [],
                    },
                }
            ],
        },
    )
    db.add(run)
    model_grant = grant(
        test_org.id, test_user.id, ResourceType.MODEL, "model-old"
    )
    db.add_all(
        [
            grant(
                test_org.id,
                test_user.id,
                ResourceType.WORKFLOW,
                str(definition.id),
            ),
            grant(
                test_org.id,
                test_user.id,
                ResourceType.AGENT_DEFINITION,
                str(agent.id),
            ),
            model_grant,
        ]
    )
    await db.commit()
    agent.model_id = "model-new"
    await db.commit()
    subject = AuthorizationSubject(test_user.id, test_org.id, "member")
    service = WorkflowAuthorizationService(
        authorization=AuthorizationService(),
        registry=ResourceRegistry(model_catalog=catalog),
    )

    decision = await service.require_snapshot_step_executable(
        db, subject, run, phase="node", node_key="research"
    )
    await db.delete(model_grant)
    await db.commit()
    with pytest.raises(WorkflowStepAccessDenied):
        await service.require_snapshot_step_executable(
            db, subject, run, phase="node", node_key="research"
        )

    assert decision.run_id == run.id
    assert decision.node_key == "research"


async def test_same_org_admin_bypasses_local_grants_even_after_archive(
    db, test_org, test_user
) -> None:
    agent = AgentDefinition(
        org_id=test_org.id,
        name="Admin agent",
        model_id="model-old",
        system_prompt="Answer",
    )
    db.add(agent)
    await db.flush()
    definition = WorkflowDefinition(
        org_id=test_org.id,
        name=f"Archived-{uuid4()}",
        supervisor_agent_definition_id=agent.id,
        status="archived",
    )
    db.add(definition)
    await db.flush()
    conversation = Conversation(
        org_id=test_org.id,
        user_id=test_user.id,
        title="Archived authorization",
        model="model-old",
    )
    db.add(conversation)
    await db.flush()
    turn_id = uuid4()
    message = Message(
        org_id=test_org.id,
        conversation_id=conversation.id,
        turn_id=turn_id,
        user_id=test_user.id,
        role="user",
        content="Research",
    )
    db.add(message)
    await db.flush()
    run = WorkflowRun(
        org_id=test_org.id,
        workflow_definition_id=definition.id,
        workflow_definition_version=1,
        initiating_user_id=test_user.id,
        conversation_id=conversation.id,
        turn_id=turn_id,
        source_message_id=message.id,
        temporal_workflow_id=f"workflow-{uuid4()}",
        execution_snapshot={
            "supervisor": {
                "agentDefinitionId": str(agent.id),
                "model": "model-old",
                "allowedSkillIds": [],
                "knowledgeBases": [],
            },
            "nodes": [],
        },
    )
    db.add(run)
    await db.commit()
    service = WorkflowAuthorizationService(
        registry=ResourceRegistry(model_catalog=catalog)
    )

    decision = await service.require_snapshot_step_executable(
        db,
        AuthorizationSubject(test_user.id, test_org.id, "org_admin"),
        run,
        phase="supervisor_synthesize",
        node_key=None,
    )

    assert decision.phase == "supervisor_synthesize"


@pytest.mark.parametrize("principal", ["organization", "group", "user"])
async def test_definition_read_accepts_additive_org_group_and_user_grants(
    db, test_org, test_user, principal
) -> None:
    agent = AgentDefinition(
        org_id=test_org.id,
        name=f"Grant supervisor-{uuid4()}",
        model_id="model-old",
        system_prompt="Answer",
    )
    db.add(agent)
    await db.flush()
    definition = WorkflowDefinition(
        org_id=test_org.id,
        name=f"Grant workflow-{uuid4()}",
        supervisor_agent_definition_id=agent.id,
    )
    db.add(definition)
    await db.flush()
    principal_id = test_user.id
    if principal == "organization":
        principal_id = test_org.id
    elif principal == "group":
        group = Group(org_id=test_org.id, name=f"Readers-{uuid4()}")
        db.add(group)
        await db.flush()
        db.add(UserGroup(user_id=test_user.id, group_id=group.id))
        principal_id = group.id
    service = WorkflowAuthorizationService(
        registry=ResourceRegistry(model_catalog=catalog)
    )
    with pytest.raises(WorkflowDefinitionNotReadable):
        await service.require_definition_readable(
            db,
            AuthorizationSubject(test_user.id, test_org.id, "member"),
            definition.id,
        )
    db.add(
        AccessGrant(
            org_id=test_org.id,
            resource_type=ResourceType.WORKFLOW.value,
            resource_id=str(definition.id),
            principal_type=principal,
            principal_id=principal_id,
            permission=AccessPermission.READ.value,
            created_via=GrantSource.PROVISIONING.value,
        )
    )
    await db.commit()

    readable = await service.require_definition_readable(
        db,
        AuthorizationSubject(test_user.id, test_org.id, "member"),
        definition.id,
    )

    assert readable.id == definition.id


async def test_global_skill_requires_explicit_grant_and_missing_skill_fails_closed(
    db, test_org, test_user
) -> None:
    skill = Skill(org_id=None, name=f"Global-{uuid4()}", type="tool")
    agent = AgentDefinition(
        org_id=test_org.id,
        name=f"Skill supervisor-{uuid4()}",
        model_id="model-old",
        system_prompt="Answer",
        skill_ids=[],
    )
    db.add_all([skill, agent])
    await db.flush()
    agent.skill_ids = [skill.id]
    definition = WorkflowDefinition(
        org_id=test_org.id,
        name=f"Global skill workflow-{uuid4()}",
        supervisor_agent_definition_id=agent.id,
    )
    db.add(definition)
    await db.flush()
    conversation = Conversation(
        org_id=test_org.id,
        user_id=test_user.id,
        title="Global skill",
        model="model-old",
    )
    db.add(conversation)
    await db.flush()
    turn_id = uuid4()
    message = Message(
        org_id=test_org.id,
        conversation_id=conversation.id,
        turn_id=turn_id,
        user_id=test_user.id,
        role="user",
        content="Use skill",
    )
    db.add(message)
    await db.flush()
    run = WorkflowRun(
        org_id=test_org.id,
        workflow_definition_id=definition.id,
        workflow_definition_version=1,
        initiating_user_id=test_user.id,
        conversation_id=conversation.id,
        turn_id=turn_id,
        source_message_id=message.id,
        temporal_workflow_id=f"workflow-{uuid4()}",
        execution_snapshot={
            "supervisor": {
                "agentDefinitionId": str(agent.id),
                "model": "model-old",
                "allowedSkillIds": [str(skill.id)],
                "knowledgeBases": [],
            },
            "nodes": [],
        },
    )
    db.add(run)
    db.add_all(
        [
            grant(test_org.id, test_user.id, ResourceType.WORKFLOW, str(definition.id)),
            grant(test_org.id, test_user.id, ResourceType.AGENT_DEFINITION, str(agent.id)),
            grant(test_org.id, test_user.id, ResourceType.MODEL, "model-old"),
        ]
    )
    await db.commit()
    subject = AuthorizationSubject(test_user.id, test_org.id, "member")
    service = WorkflowAuthorizationService(
        registry=ResourceRegistry(model_catalog=catalog)
    )

    with pytest.raises(WorkflowStepAccessDenied):
        await service.require_snapshot_step_executable(
            db, subject, run, phase="supervisor_prepare", node_key=None
        )
    db.add(grant(test_org.id, test_user.id, ResourceType.SKILL, str(skill.id)))
    await db.commit()
    await service.require_snapshot_step_executable(
        db, subject, run, phase="supervisor_prepare", node_key=None
    )
    await db.delete(skill)
    await db.commit()

    with pytest.raises(WorkflowStepAccessDenied):
        await service.require_snapshot_step_executable(
            db, subject, run, phase="supervisor_prepare", node_key=None
        )


async def test_synthesis_rechecks_node_agents_not_only_the_supervisor(
    db, test_org, test_user
) -> None:
    """Synthesis folds every node's output into the answer, so a grant revoked
    after that node completed must stop the synthesis too — checking only the
    supervisor would let a revoked agent's work reach the user."""
    supervisor = AgentDefinition(
        org_id=test_org.id,
        name=f"Supervisor-{uuid4()}",
        model_id="model-old",
        system_prompt="Synthesize",
        skill_ids=[],
    )
    node_agent = AgentDefinition(
        org_id=test_org.id,
        name=f"Node-{uuid4()}",
        model_id="model-old",
        system_prompt="Research",
        skill_ids=[],
    )
    db.add_all([supervisor, node_agent])
    await db.flush()
    definition = WorkflowDefinition(
        org_id=test_org.id,
        name=f"Synthesis workflow-{uuid4()}",
        supervisor_agent_definition_id=supervisor.id,
    )
    db.add(definition)
    await db.flush()
    conversation = Conversation(
        org_id=test_org.id,
        user_id=test_user.id,
        title="Synthesis",
        model="model-old",
    )
    db.add(conversation)
    await db.flush()
    turn_id = uuid4()
    message = Message(
        org_id=test_org.id,
        conversation_id=conversation.id,
        turn_id=turn_id,
        user_id=test_user.id,
        role="user",
        content="Research it",
    )
    db.add(message)
    await db.flush()
    agent_snapshot = {
        "agentDefinitionId": str(node_agent.id),
        "model": "model-old",
        "allowedSkillIds": [],
        "knowledgeBases": [],
    }
    run = WorkflowRun(
        org_id=test_org.id,
        workflow_definition_id=definition.id,
        workflow_definition_version=1,
        initiating_user_id=test_user.id,
        conversation_id=conversation.id,
        turn_id=turn_id,
        source_message_id=message.id,
        temporal_workflow_id=f"workflow-{uuid4()}",
        execution_snapshot={
            "supervisor": {
                "agentDefinitionId": str(supervisor.id),
                "model": "model-old",
                "allowedSkillIds": [],
                "knowledgeBases": [],
            },
            "nodes": [{"nodeKey": "research", "agent": agent_snapshot}],
        },
    )
    db.add(run)
    node_grant = grant(
        test_org.id, test_user.id, ResourceType.AGENT_DEFINITION, str(node_agent.id)
    )
    db.add_all(
        [
            grant(test_org.id, test_user.id, ResourceType.WORKFLOW, str(definition.id)),
            grant(
                test_org.id,
                test_user.id,
                ResourceType.AGENT_DEFINITION,
                str(supervisor.id),
            ),
            node_grant,
            grant(test_org.id, test_user.id, ResourceType.MODEL, "model-old"),
        ]
    )
    await db.commit()
    subject = AuthorizationSubject(test_user.id, test_org.id, "member")
    service = WorkflowAuthorizationService(
        registry=ResourceRegistry(model_catalog=catalog)
    )

    await service.require_snapshot_step_executable(
        db, subject, run, phase="supervisor_synthesize", node_key=None
    )

    # The node has already completed; revoking its grant now must still block
    # the synthesis that would consume its output.
    await db.delete(node_grant)
    await db.commit()

    with pytest.raises(WorkflowStepAccessDenied):
        await service.require_snapshot_step_executable(
            db, subject, run, phase="supervisor_synthesize", node_key=None
        )
    # The supervisor itself is still fully granted, so preparation is unaffected.
    await service.require_snapshot_step_executable(
        db, subject, run, phase="supervisor_prepare", node_key=None
    )
