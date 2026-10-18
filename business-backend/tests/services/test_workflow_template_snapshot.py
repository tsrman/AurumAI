from uuid import UUID, uuid4

from src.models.agent_definition import AgentDefinition
from src.models.workflow_definition import WorkflowDefinition, WorkflowEdge, WorkflowNode
from src.schemas.workflow_template import TemplateSnapshot
from src.services.workflow_template_snapshot import (
    build_template_snapshot,
    template_agent_name,
    workflow_draft_from_snapshot,
)


def agent(agent_id: UUID, name: str) -> AgentDefinition:
    return AgentDefinition(
        id=agent_id,
        org_id=uuid4(),
        name=name,
        description="Agent description",
        model_id="provider/model-v1",
        system_prompt="Answer carefully",
        temperature=0.4,
        skill_ids=[uuid4()],
        knowledge_base_ids=[uuid4()],
    )


def test_materialization_retains_hitl_and_reuses_agent(template_snapshot) -> None:
    agent_id = uuid4()
    draft = workflow_draft_from_snapshot(
        template_snapshot,
        {"a0": agent_id},
        name="Copy",
        description=None,
    )

    assert draft.supervisor_agent_definition_id == agent_id
    assert draft.nodes[0].agent_definition_id == agent_id
    assert draft.nodes[0].requires_confirmation is True
    assert draft.auto_routable is False


def test_materialization_preserves_positions_instructions_and_edge_conditions() -> None:
    snapshot = TemplateSnapshot.model_validate(
        {
            "schemaVersion": 1,
            "supervisorAgentKey": "a0",
            "maxParallelism": 2,
            "agents": [
                {
                    "key": "a0",
                    "name": "Analyst",
                    "modelId": "provider/model-v1",
                    "systemPrompt": "Answer",
                }
            ],
            "nodes": [
                {
                    "nodeKey": "research",
                    "displayName": "Research",
                    "agentKey": "a0",
                    "instruction": "Find evidence",
                    "positionX": 1.5,
                    "positionY": 2.5,
                },
                {
                    "nodeKey": "review",
                    "displayName": "Review",
                    "agentKey": "a0",
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
    agent_id = uuid4()

    draft = workflow_draft_from_snapshot(
        snapshot, {"a0": agent_id}, name="Copy", description="Copied"
    )

    assert [node.agent_definition_id for node in draft.nodes] == [agent_id, agent_id]
    assert draft.nodes[0].instruction == "Find evidence"
    assert (draft.nodes[0].position_x, draft.nodes[0].position_y) == (1.5, 2.5)
    assert draft.edges[0].condition == "on_failure"


def test_capture_assigns_sorted_agent_keys_and_preserves_configuration() -> None:
    low_id = UUID("00000000-0000-0000-0000-000000000001")
    high_id = UUID("00000000-0000-0000-0000-000000000002")
    supervisor = agent(high_id, "Supervisor")
    shared = agent(low_id, "Shared worker")
    definition_id = uuid4()
    definition = WorkflowDefinition(
        id=definition_id,
        org_id=uuid4(),
        name="Source",
        description="Template metadata only",
        supervisor_agent_definition_id=high_id,
        max_parallelism=3,
        auto_routable=True,
    )
    first_id = uuid4()
    second_id = uuid4()
    nodes = [
        WorkflowNode(
            id=first_id,
            org_id=definition.org_id,
            workflow_definition_id=definition_id,
            node_key="research",
            display_name="Research",
            agent_definition_id=low_id,
            instruction="Find evidence",
            position_x=10.0,
            position_y=20.0,
            requires_confirmation=True,
        ),
        WorkflowNode(
            id=second_id,
            org_id=definition.org_id,
            workflow_definition_id=definition_id,
            node_key="review",
            display_name="Review",
            agent_definition_id=low_id,
            instruction="Review evidence",
            requires_confirmation=False,
        ),
    ]
    edges = [
        WorkflowEdge(
            id=uuid4(),
            org_id=definition.org_id,
            workflow_definition_id=definition_id,
            source_node_id=first_id,
            target_node_id=second_id,
            condition="on_failure",
            is_loop_back=False,
        )
    ]

    snapshot = build_template_snapshot(
        definition, nodes, edges, [supervisor, shared]
    )

    assert snapshot.supervisor_agent_key == "a1"
    assert [item.key for item in snapshot.agents] == ["a0", "a1"]
    assert [node.agent_key for node in snapshot.nodes] == ["a0", "a0"]
    assert snapshot.nodes[0].requires_confirmation is True
    assert snapshot.edges[0].condition == "on_failure"
    assert snapshot.max_parallelism == 3
    assert "description" not in snapshot.model_dump(by_alias=True)
    assert "autoRoutable" not in snapshot.model_dump(by_alias=True)
    assert snapshot.agents[0].skill_ids == shared.skill_ids
    assert snapshot.agents[0].knowledge_base_ids == shared.knowledge_base_ids


def test_capture_reuses_one_agent_for_supervisor_and_multiple_nodes() -> None:
    shared_id = uuid4()
    shared = agent(shared_id, "Shared")
    definition_id = uuid4()
    definition = WorkflowDefinition(
        id=definition_id,
        org_id=uuid4(),
        name="Source",
        supervisor_agent_definition_id=shared_id,
        max_parallelism=2,
    )
    nodes = [
        WorkflowNode(
            id=uuid4(),
            org_id=definition.org_id,
            workflow_definition_id=definition_id,
            node_key=key,
            display_name=key.title(),
            agent_definition_id=shared_id,
            instruction=f"Run {key}",
            requires_confirmation=False,
        )
        for key in ("research", "review")
    ]

    snapshot = build_template_snapshot(definition, nodes, [], [shared])

    assert [item.key for item in snapshot.agents] == ["a0"]
    assert snapshot.supervisor_agent_key == "a0"
    assert [node.agent_key for node in snapshot.nodes] == ["a0", "a0"]


def test_template_agent_name_is_unique_and_never_exceeds_255_characters() -> None:
    agent_id = UUID("00000000-0000-0000-0000-000000000001")

    result = template_agent_name("x" * 255, agent_id)

    assert result.endswith(f" [{agent_id}]")
    assert len(result) == 255
