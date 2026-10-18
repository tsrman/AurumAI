from typing import Literal, cast

from src.models.workflow_definition import (
    WorkflowDefinition,
    WorkflowEdge,
    WorkflowNode,
    WorkflowDefinitionStatus,
)
from src.schemas.workflow import (
    WorkflowDefinitionResponse,
    WorkflowEdgeDraft,
    WorkflowNodeDraft,
)


def workflow_definition_response(
    definition: WorkflowDefinition,
    nodes: list[WorkflowNode],
    edges: list[WorkflowEdge],
) -> WorkflowDefinitionResponse:
    node_keys = {node.id: node.node_key for node in nodes}
    return WorkflowDefinitionResponse(
        id=definition.id,
        org_id=definition.org_id,
        name=definition.name,
        description=definition.description,
        supervisor_agent_definition_id=definition.supervisor_agent_definition_id,
        version=definition.version,
        max_parallelism=definition.max_parallelism,
        auto_routable=definition.auto_routable,
        status=WorkflowDefinitionStatus(definition.status),
        created_by=definition.created_by,
        updated_by=definition.updated_by,
        created_at=definition.created_at,
        updated_at=definition.updated_at,
        source_template_id=definition.source_template_id,
        source_template_version=definition.source_template_version,
        nodes=[
            WorkflowNodeDraft(
                node_key=node.node_key,
                display_name=node.display_name,
                agent_definition_id=node.agent_definition_id,
                instruction=node.instruction,
                position_x=node.position_x,
                position_y=node.position_y,
                requires_confirmation=node.requires_confirmation,
            )
            for node in nodes
        ],
        edges=[
            WorkflowEdgeDraft(
                source_node_key=node_keys[edge.source_node_id],
                target_node_key=node_keys[edge.target_node_id],
                condition=cast(Literal["on_success", "on_failure"], edge.condition),
                is_loop_back=edge.is_loop_back,
                max_iterations=edge.max_iterations,
            )
            for edge in edges
        ],
    )
