from uuid import UUID

from src.models.agent_definition import AgentDefinition
from src.models.workflow_definition import WorkflowDefinition, WorkflowEdge, WorkflowNode
from src.schemas.workflow import (
    WorkflowDefinitionCreate,
    WorkflowEdgeDraft,
    WorkflowNodeDraft,
)
from src.schemas.workflow_template import TemplateAgent, TemplateNode, TemplateSnapshot


def build_template_snapshot(
    definition: WorkflowDefinition,
    nodes: list[WorkflowNode],
    edges: list[WorkflowEdge],
    agents: list[AgentDefinition],
) -> TemplateSnapshot:
    sorted_agents = sorted(agents, key=lambda agent: str(agent.id))
    agent_keys = {
        agent.id: f"a{index}" for index, agent in enumerate(sorted_agents)
    }
    node_keys = {node.id: node.node_key for node in nodes}

    return TemplateSnapshot(
        schema_version=1,
        supervisor_agent_key=agent_keys[
            definition.supervisor_agent_definition_id
        ],
        max_parallelism=definition.max_parallelism,
        agents=[
            TemplateAgent(
                key=agent_keys[agent.id],
                name=agent.name,
                description=agent.description,
                model_id=agent.model_id,
                system_prompt=agent.system_prompt,
                temperature=agent.temperature,
                skill_ids=agent.skill_ids,
                knowledge_base_ids=agent.knowledge_base_ids,
            )
            for agent in sorted_agents
        ],
        nodes=[
            TemplateNode(
                node_key=node.node_key,
                display_name=node.display_name,
                agent_key=agent_keys[node.agent_definition_id],
                instruction=node.instruction,
                position_x=node.position_x,
                position_y=node.position_y,
                requires_confirmation=node.requires_confirmation,
            )
            for node in nodes
        ],
        edges=[
            WorkflowEdgeDraft.model_validate(
                {
                    "source_node_key": node_keys[edge.source_node_id],
                    "target_node_key": node_keys[edge.target_node_id],
                    "condition": edge.condition,
                    "is_loop_back": edge.is_loop_back,
                    "max_iterations": edge.max_iterations,
                }
            )
            for edge in edges
        ],
    )


def workflow_draft_from_snapshot(
    snapshot: TemplateSnapshot,
    agent_ids: dict[str, UUID],
    *,
    name: str,
    description: str | None,
) -> WorkflowDefinitionCreate:
    return WorkflowDefinitionCreate(
        name=name,
        description=description,
        supervisor_agent_definition_id=agent_ids[snapshot.supervisor_agent_key],
        max_parallelism=snapshot.max_parallelism,
        auto_routable=False,
        nodes=[
            WorkflowNodeDraft(
                node_key=node.node_key,
                display_name=node.display_name,
                agent_definition_id=agent_ids[node.agent_key],
                instruction=node.instruction,
                position_x=node.position_x,
                position_y=node.position_y,
                requires_confirmation=node.requires_confirmation,
            )
            for node in snapshot.nodes
        ],
        edges=snapshot.edges,
    )


def template_agent_name(name: str, agent_id: UUID) -> str:
    suffix = f" [{agent_id}]"
    return f"{name[: 255 - len(suffix)]}{suffix}"
