from uuid import uuid4

import pytest
from sqlalchemy import select

from src.core.authorization_types import AuthorizationSubject
from src.models.agent_definition import AgentDefinition
from src.models.workflow_definition import WorkflowEdge, WorkflowNode
from src.repositories.workflow_definition_repository import (
    WorkflowDefinitionRepository,
    WorkflowVersionConflict,
)
from src.schemas.workflow import WorkflowDefinitionCreate, WorkflowDefinitionReplace


def draft(
    supervisor: AgentDefinition,
    first: AgentDefinition,
    second: AgentDefinition,
) -> WorkflowDefinitionCreate:
    return WorkflowDefinitionCreate.model_validate(
        {
            "name": "Review flow",
            "description": "Two steps",
            "supervisorAgentDefinitionId": supervisor.id,
            "maxParallelism": 2,
            "nodes": [
                {
                    "nodeKey": "research",
                    "displayName": "Research",
                    "agentDefinitionId": first.id,
                    "instruction": "Find evidence",
                },
                {
                    "nodeKey": "review",
                    "displayName": "Review",
                    "agentDefinitionId": second.id,
                    "instruction": "Review evidence",
                },
            ],
            "edges": [
                {
                    "sourceNodeKey": "research",
                    "targetNodeKey": "review",
                    "condition": "on_failure",
                }
            ],
        }
    )


async def test_repository_persists_and_replaces_a_normalized_graph_atomically(
    db, test_org, test_user
) -> None:
    agents = [
        AgentDefinition(
            org_id=test_org.id,
            name=name,
            model_id="provider/model-v1",
            system_prompt="Answer",
        )
        for name in ("Supervisor", "Researcher", "Reviewer")
    ]
    db.add_all(agents)
    await db.flush()
    repository = WorkflowDefinitionRepository()
    subject = AuthorizationSubject(test_user.id, test_org.id, "org_admin")

    definition = await repository.create(db, subject, draft(*agents))
    await db.commit()
    nodes, edges = await repository.load_graph(db, test_org.id, definition.id)

    assert definition.version == 1
    assert [item.node_key for item in nodes] == ["research", "review"]
    assert len(edges) == 1
    assert edges[0].condition == "on_failure"

    replacement = WorkflowDefinitionReplace.model_validate(
        {
            **draft(*agents).model_dump(),
            "version": 1,
            "nodes": [
                {
                    "nodeKey": "summary",
                    "displayName": "Summary",
                    "agentDefinitionId": agents[2].id,
                    "instruction": "Summarize",
                }
            ],
            "edges": [],
        }
    )
    updated = await repository.replace(db, subject, definition.id, replacement)
    await db.commit()

    assert updated.version == 2
    stored_nodes = (
        await db.execute(
            select(WorkflowNode).where(
                WorkflowNode.workflow_definition_id == definition.id
            )
        )
    ).scalars().all()
    stored_edges = (
        await db.execute(
            select(WorkflowEdge).where(
                WorkflowEdge.workflow_definition_id == definition.id
            )
        )
    ).scalars().all()
    assert [item.node_key for item in stored_nodes] == ["summary"]
    assert stored_edges == []

    stale = replacement.model_copy(update={"nodes": draft(*agents).nodes})
    with pytest.raises(WorkflowVersionConflict) as caught:
        await repository.replace(db, subject, definition.id, stale)

    assert (caught.value.expected, caught.value.actual) == (1, 2)
    unchanged = (
        await db.execute(
            select(WorkflowNode).where(
                WorkflowNode.workflow_definition_id == definition.id
            )
        )
    ).scalars().all()
    assert [item.node_key for item in unchanged] == ["summary"]


async def test_repository_returns_none_for_foreign_definition(
    db, test_org, test_user
) -> None:
    repository = WorkflowDefinitionRepository()

    assert await repository.get_by_id(db, test_org.id, uuid4()) is None


async def test_list_accessible_auto_routable_only_excludes_opted_out_definitions(
    db, test_org, test_user
) -> None:
    agents = [
        AgentDefinition(
            org_id=test_org.id,
            name=name,
            model_id="provider/model-v1",
            system_prompt="Answer",
        )
        for name in ("Supervisor", "Researcher", "Reviewer")
    ]
    db.add_all(agents)
    await db.flush()
    repository = WorkflowDefinitionRepository()
    subject = AuthorizationSubject(test_user.id, test_org.id, "org_admin")

    routable_data = draft(*agents).model_copy(
        update={"name": "Auto routable", "auto_routable": True}
    )
    not_routable_data = draft(*agents).model_copy(
        update={"name": "Manual only", "auto_routable": False}
    )
    routable = await repository.create(db, subject, routable_data)
    not_routable = await repository.create(db, subject, not_routable_data)
    await db.commit()

    only_routable = await repository.list_accessible(db, subject, auto_routable_only=True)
    all_accessible = await repository.list_accessible(db, subject)

    assert [item.id for item in only_routable] == [routable.id]
    assert {item.id for item in all_accessible} == {routable.id, not_routable.id}
