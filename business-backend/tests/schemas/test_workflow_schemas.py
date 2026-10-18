from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from src.schemas.workflow import (
    WorkflowAgentSnapshot,
    WorkflowDefinitionCreate,
    WorkflowDefinitionReplace,
    WorkflowEdgeDraft,
    WorkflowKnowledgeBaseSnapshot,
    WorkflowNodeDraft,
    WorkflowRunSnapshotResponse,
    WorkflowSnapshotNode,
)


def valid_node(key: str = "research") -> dict[str, object]:
    return {
        "nodeKey": key,
        "displayName": "  Research  ",
        "agentDefinitionId": uuid4(),
        "instruction": "  Find primary evidence.  ",
        "positionX": 10.5,
        "positionY": 20.5,
    }


def valid_draft() -> dict[str, object]:
    return {
        "name": "  Contract review  ",
        "description": "  Review in parallel.  ",
        "supervisorAgentDefinitionId": uuid4(),
        "maxParallelism": 2,
        "nodes": [valid_node()],
        "edges": [],
    }


def test_definition_draft_normalizes_text_and_serializes_camel_case() -> None:
    draft = WorkflowDefinitionCreate.model_validate(valid_draft())

    assert draft.name == "Contract review"
    assert draft.description == "Review in parallel."
    assert draft.nodes[0].display_name == "Research"
    assert draft.nodes[0].instruction == "Find primary evidence."
    assert draft.model_dump(by_alias=True) == {
        "name": "Contract review",
        "description": "Review in parallel.",
        "supervisorAgentDefinitionId": draft.supervisor_agent_definition_id,
        "maxParallelism": 2,
        "autoRoutable": False,
        "nodes": [
            {
                "nodeKey": "research",
                "displayName": "Research",
                "agentDefinitionId": draft.nodes[0].agent_definition_id,
                "instruction": "Find primary evidence.",
                "positionX": 10.5,
                "positionY": 20.5,
                "requiresConfirmation": False,
            }
        ],
        "edges": [],
    }


@pytest.mark.parametrize("field", ["nodes", "edges"])
def test_definition_draft_rejects_null_collections(field: str) -> None:
    payload = valid_draft()
    payload[field] = None

    with pytest.raises(ValidationError):
        WorkflowDefinitionCreate.model_validate(payload)


def test_definition_draft_rejects_duplicate_node_keys_and_edges() -> None:
    duplicate_nodes = valid_draft()
    duplicate_nodes["nodes"] = [valid_node("same"), valid_node("same")]
    with pytest.raises(ValidationError, match="node keys must be unique"):
        WorkflowDefinitionCreate.model_validate(duplicate_nodes)

    duplicate_edges = valid_draft()
    duplicate_edges["nodes"] = [valid_node("a"), valid_node("b")]
    duplicate_edges["edges"] = [
        {"sourceNodeKey": "a", "targetNodeKey": "b"},
        {"sourceNodeKey": "a", "targetNodeKey": "b"},
    ]
    with pytest.raises(ValidationError, match="edges must be unique"):
        WorkflowDefinitionCreate.model_validate(duplicate_edges)


def test_edge_draft_condition_defaults_to_on_success_and_rejects_invalid_values() -> None:
    default = WorkflowEdgeDraft(sourceNodeKey="a", targetNodeKey="b")
    assert default.condition == "on_success"

    explicit = WorkflowEdgeDraft(
        sourceNodeKey="a", targetNodeKey="b", condition="on_failure"
    )
    assert explicit.condition == "on_failure"

    with pytest.raises(ValidationError):
        WorkflowEdgeDraft(sourceNodeKey="a", targetNodeKey="b", condition="always")


def test_loop_back_edge_requires_max_iterations_in_range() -> None:
    edge = WorkflowEdgeDraft(
        sourceNodeKey="a", targetNodeKey="b", isLoopBack=True, maxIterations=5
    )
    assert edge.is_loop_back is True
    assert edge.max_iterations == 5

    with pytest.raises(ValidationError, match="maxIterations is required"):
        WorkflowEdgeDraft(sourceNodeKey="a", targetNodeKey="b", isLoopBack=True)

    with pytest.raises(ValidationError):
        WorkflowEdgeDraft(
            sourceNodeKey="a", targetNodeKey="b", isLoopBack=True, maxIterations=1
        )
    with pytest.raises(ValidationError):
        WorkflowEdgeDraft(
            sourceNodeKey="a", targetNodeKey="b", isLoopBack=True, maxIterations=21
        )


def test_ordinary_edge_rejects_loop_only_fields() -> None:
    with pytest.raises(ValidationError, match="maxIterations is only valid"):
        WorkflowEdgeDraft(sourceNodeKey="a", targetNodeKey="b", maxIterations=3)


def test_definition_replace_requires_positive_current_version() -> None:
    with pytest.raises(ValidationError):
        WorkflowDefinitionReplace.model_validate(valid_draft())

    payload = {**valid_draft(), "version": 0}
    with pytest.raises(ValidationError):
        WorkflowDefinitionReplace.model_validate(payload)

    valid = WorkflowDefinitionReplace.model_validate({**valid_draft(), "version": 3})
    assert valid.version == 3


@pytest.mark.parametrize(
    ("node_key", "valid"),
    [
        ("research", True),
        ("research_2", True),
        ("Research", False),
        ("2research", False),
        ("a" * 65, False),
    ],
)
def test_node_key_uses_stable_bounded_identifier(node_key: str, valid: bool) -> None:
    if valid:
        assert WorkflowNodeDraft.model_validate(valid_node(node_key)).node_key == node_key
    else:
        with pytest.raises(ValidationError):
            WorkflowNodeDraft.model_validate(valid_node(node_key))


def test_snapshot_schema_is_strict_and_uses_camel_case() -> None:
    run_id = uuid4()
    agent_id = uuid4()
    knowledge_id = uuid4()
    agent = WorkflowAgentSnapshot(
        agentDefinitionId=agent_id,
        name="Researcher",
        model="provider/model",
        systemPrompt="Be precise.",
        temperature=0.2,
        allowedSkillIds=(uuid4(),),
        knowledgeBases=(
            WorkflowKnowledgeBaseSnapshot(id=knowledge_id, name="Policies"),
        ),
    )
    snapshot = WorkflowRunSnapshotResponse(
        runId=run_id,
        orgId=uuid4(),
        workflowDefinitionId=uuid4(),
        workflowDefinitionVersion=2,
        conversationId=uuid4(),
        turnId=uuid4(),
        sourceMessageId=uuid4(),
        temporalWorkflowId=f"workflow-run-{run_id}",
        userRequest="Compare reports",
        maxParallelism=2,
        supervisor=agent,
        nodes=(
            WorkflowSnapshotNode(
                nodeKey="research",
                displayName="Research",
                instruction="Find evidence",
                agent=agent,
            ),
        ),
        edges=(WorkflowEdgeDraft(sourceNodeKey="research", targetNodeKey="review"),),
        createdAt=datetime.now(UTC),
    )

    dumped = snapshot.model_dump(by_alias=True)
    assert dumped["runId"] == run_id
    assert dumped["supervisor"]["systemPrompt"] == "Be precise."
    assert dumped["nodes"][0]["nodeKey"] == "research"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        WorkflowEdgeDraft.model_validate(
            {"sourceNodeKey": "a", "targetNodeKey": "b", "unexpected": True}
        )


@pytest.mark.parametrize("reserved", ["supervisor_prepare", "supervisor_synthesize"])
def test_node_key_may_not_reuse_a_supervisor_phase_key(reserved: str) -> None:
    with pytest.raises(ValidationError):
        WorkflowNodeDraft(
            nodeKey=reserved,
            displayName="Reserved",
            agentDefinitionId=uuid4(),
            instruction="Do the thing",
        )
