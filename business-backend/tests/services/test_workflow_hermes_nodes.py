"""Hermes Agent as a workflow node (docs/HERMES_AGENT.md, phase 6): agent
definitions of kind hermes run nodes on the organization's Hermes model, for
users who may use Hermes, and never plan or synthesize."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from src.core.authorization_types import AuthorizationSubject
from src.core.capability_catalog import Capability
from src.core.config import settings
from src.models.agent_definition import AgentDefinition
from src.models.organization_capability import OrganizationCapability
from src.schemas.agent_definition import AgentDefinitionCreate
from src.schemas.workflow import WorkflowDefinitionCreate, WorkflowNodeDraft
from src.services.hermes_egress import set_organization_hermes_model
from src.services.workflow_authorization import WorkflowAuthorizationService, WorkflowStepAccessDenied
from src.services.workflow_definition import WorkflowDefinitionService
from src.services.workflow_graph import WorkflowGraphError
from src.services.resource_registry import ResourceRegistry
from tests.services.test_workflow_run import catalog, create_runtime_graph, run_service

HERMES_MODEL = "provider/model-v1"


@pytest.fixture
async def hermes_org(db, test_org, monkeypatch):  # noqa: ANN001, ANN201
    monkeypatch.setattr(settings, "HERMES_ENABLED", True)
    db.add_all(
        OrganizationCapability(org_id=test_org.id, capability=capability.value)
        for capability in (Capability.CHAT_SEND, Capability.AGENTS_HERMES)
    )
    await set_organization_hermes_model(db, test_org.id, HERMES_MODEL)
    await db.commit()
    return test_org


async def _hermes_graph(db, test_org, test_user):  # noqa: ANN001, ANN202
    definition, conversation, message, turn_id = await create_runtime_graph(db, test_org, test_user)
    worker = await db.get(AgentDefinition, (await _node_agent_id(db, definition.id)))
    worker.kind = "hermes"
    worker.model_id = "provider/stale-model"
    await db.commit()
    return definition, conversation, message, turn_id


async def _node_agent_id(db, definition_id):  # noqa: ANN001, ANN202
    from sqlalchemy import select

    from src.models.workflow_definition import WorkflowNode

    return await db.scalar(select(WorkflowNode.agent_definition_id).where(WorkflowNode.workflow_definition_id == definition_id))


def _admin(test_org, test_user) -> AuthorizationSubject:  # noqa: ANN001
    return AuthorizationSubject(test_user.id, test_org.id, "org_admin")


async def test_a_hermes_node_runs_on_the_organizations_hermes_model(db, hermes_org, test_user) -> None:
    definition, conversation, message, turn_id = await _hermes_graph(db, hermes_org, test_user)

    run = await run_service().create_authorized_run(
        db, _admin(hermes_org, test_user), definition.id, conversation.id, turn_id, message.id
    )

    [node] = run.execution_snapshot["nodes"]
    assert (node["agent"]["kind"], node["agent"]["model"]) == ("hermes", HERMES_MODEL)
    assert run.execution_snapshot["supervisor"]["kind"] == "agent"


async def test_a_workflow_with_a_hermes_node_does_not_start_without_hermes(
    db, hermes_org, test_user, monkeypatch
) -> None:
    definition, conversation, message, turn_id = await _hermes_graph(db, hermes_org, test_user)
    monkeypatch.setattr(settings, "HERMES_ENABLED", False)

    with pytest.raises(WorkflowGraphError) as refused:
        await run_service().create_authorized_run(
            db, _admin(hermes_org, test_user), definition.id, conversation.id, turn_id, message.id
        )

    assert refused.value.code == "hermes_unavailable"


async def test_a_hermes_node_rechecks_hermes_access_before_it_runs(db, hermes_org, test_user, monkeypatch) -> None:
    definition, conversation, message, turn_id = await _hermes_graph(db, hermes_org, test_user)
    admin = _admin(hermes_org, test_user)
    run = await run_service().create_authorized_run(db, admin, definition.id, conversation.id, turn_id, message.id)
    await db.commit()
    authorization = WorkflowAuthorizationService(registry=ResourceRegistry(model_catalog=catalog))
    await authorization.require_snapshot_step_executable(db, admin, run, phase="node", node_key="research")

    # The organization moved Hermes to another model since the run started.
    await set_organization_hermes_model(db, hermes_org.id, "provider/other-model")
    await db.commit()
    with pytest.raises(WorkflowStepAccessDenied):
        await authorization.require_snapshot_step_executable(db, admin, run, phase="node", node_key="research")

    await set_organization_hermes_model(db, hermes_org.id, HERMES_MODEL)
    await db.commit()
    monkeypatch.setattr(settings, "HERMES_ENABLED", False)
    with pytest.raises(WorkflowStepAccessDenied):
        await authorization.require_snapshot_step_executable(db, admin, run, phase="node", node_key="research")
    # The supervisor's own steps do not depend on Hermes.
    await authorization.require_snapshot_step_executable(db, admin, run, phase="supervisor_prepare", node_key=None)


async def test_hermes_never_supervises_a_workflow(db, hermes_org, test_user) -> None:
    hermes = AgentDefinition(
        org_id=hermes_org.id, name=f"Hermes-{uuid4()}", model_id=HERMES_MODEL, system_prompt="Work", kind="hermes"
    )
    db.add(hermes)
    await db.flush()
    draft = WorkflowDefinitionCreate(
        name=f"Hermes-led-{uuid4()}",
        supervisor_agent_definition_id=hermes.id,
        max_parallelism=1,
        nodes=[WorkflowNodeDraft(node_key="research", display_name="Research", agent_definition_id=hermes.id, instruction="Find")],
        edges=[],
    )

    with pytest.raises(WorkflowGraphError) as refused:
        await WorkflowDefinitionService().validate_draft(db, _admin(hermes_org, test_user), draft)

    assert refused.value.code == "hermes_supervisor"


def test_a_hermes_agent_definition_takes_no_skills_or_knowledge_bases() -> None:
    with pytest.raises(ValidationError):
        AgentDefinitionCreate(name="H", model_id="m", system_prompt="p", kind="hermes", skill_ids=[uuid4()])
    assert AgentDefinitionCreate(name="H", model_id="m", system_prompt="p", kind="hermes").kind == "hermes"
