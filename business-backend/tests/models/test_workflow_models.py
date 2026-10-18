from src.models.llm_usage import LLMUsageEvent
from src.models.workflow_definition import (
    WorkflowDefinition,
    WorkflowDefinitionStatus,
    WorkflowEdge,
    WorkflowNode,
)
from src.models.workflow_run import (
    WorkflowNodeRun,
    WorkflowRun,
    WorkflowRunStatus,
)


def test_workflow_definition_constraints_are_bounded() -> None:
    table = WorkflowDefinition.__table__

    assert table.c.org_id.nullable is False
    assert table.c.version.server_default.arg.text == "1"
    assert table.c.max_parallelism.server_default.arg.text == "4"
    assert {item.name for item in table.constraints} >= {
        "uq_workflow_definitions_org_name",
        "uq_workflow_definitions_org_id_id",
        "ck_workflow_definitions_name_nonblank",
        "ck_workflow_definitions_status",
        "ck_workflow_definitions_version",
        "ck_workflow_definitions_parallelism",
        "fk_workflow_definitions_org_supervisor",
    }


def test_workflow_graph_rows_have_same_org_reference_constraints() -> None:
    node_constraints = {item.name for item in WorkflowNode.__table__.constraints}
    edge_constraints = {item.name for item in WorkflowEdge.__table__.constraints}

    assert node_constraints >= {
        "uq_workflow_nodes_definition_key",
        "uq_workflow_nodes_org_definition_id",
        "fk_workflow_nodes_org_definition",
        "fk_workflow_nodes_org_agent",
        "ck_workflow_nodes_key",
        "ck_workflow_nodes_instruction_nonblank",
    }
    assert edge_constraints >= {
        "uq_workflow_edges_definition_pair",
        "fk_workflow_edges_org_definition",
        "fk_workflow_edges_source_node",
        "fk_workflow_edges_target_node",
        "ck_workflow_edges_no_self_edge",
        "ck_workflow_edges_condition",
    }


def test_workflow_run_models_have_stable_status_and_diagnostic_contracts() -> None:
    run_table = WorkflowRun.__table__
    node_run_table = WorkflowNodeRun.__table__

    assert {item.value for item in WorkflowDefinitionStatus} == {"active", "archived"}
    assert {item.value for item in WorkflowRunStatus} == {
        "pending",
        "planning",
        "running",
        "synthesizing",
        "completed",
        "failed",
        "cancelled",
    }
    assert run_table.c.execution_snapshot.nullable is False
    assert run_table.c.temporal_workflow_id.unique is True
    assert {item.name for item in run_table.constraints} >= {
        "uq_workflow_runs_org_id_id",
        "uq_workflow_runs_conversation_turn",
        "fk_workflow_runs_org_definition",
        "fk_workflow_runs_org_user",
        "fk_workflow_runs_org_conversation",
        "fk_workflow_runs_org_message",
        "ck_workflow_runs_status",
        "ck_workflow_runs_definition_version",
    }
    assert node_run_table.c.diagnostic_expires_at.nullable is True
    assert {item.name for item in node_run_table.constraints} >= {
        "uq_workflow_node_runs_run_phase",
        "fk_workflow_node_runs_org_run",
        "fk_workflow_node_runs_org_agent",
        "ck_workflow_node_runs_status",
        "ck_workflow_node_runs_attempt_count",
    }


def test_llm_usage_has_nullable_workflow_correlation() -> None:
    table = LLMUsageEvent.__table__

    assert table.c.workflow_run_id.nullable is True
    assert table.c.workflow_phase.nullable is True
    assert table.c.workflow_node_key.nullable is True
    assert {item.name for item in table.constraints} >= {
        "fk_llm_usage_events_workflow_run",
        "ck_llm_usage_events_workflow_correlation",
    }
