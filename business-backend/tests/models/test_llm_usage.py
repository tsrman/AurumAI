from sqlalchemy import CheckConstraint

from src.models.llm_usage import (
    LLMUsageEvent,
    OrganizationUsageMonthly,
    UsageEventStatus,
)


def _check_sql(table: object) -> set[str]:
    constraints = getattr(table, "constraints")
    return {
        str(constraint.sqltext)
        for constraint in constraints
        if isinstance(constraint, CheckConstraint)
    }


def test_llm_usage_event_schema_preserves_billable_attribution() -> None:
    table = LLMUsageEvent.__table__

    assert table.name == "llm_usage_events"
    assert [status.value for status in UsageEventStatus] == [
        "reserved",
        "accepted",
        "completed",
    ]
    assert table.c.request_id.unique is True
    assert table.c.conversation_id.nullable is True
    assert not table.c.conversation_id.foreign_keys
    organization_fk = next(
        foreign_key
        for foreign_key in table.c.org_id.foreign_keys
        if foreign_key.column.table.name == "organizations"
    )
    assert organization_fk.ondelete == "CASCADE"
    assert next(iter(table.c.user_id.foreign_keys)).ondelete == "RESTRICT"
    assert str(table.c.status.server_default.arg) == "'reserved'"
    assert str(table.c.prompt_tokens.server_default.arg) == "0"
    assert str(table.c.completion_tokens.server_default.arg) == "0"
    assert str(table.c.total_tokens.server_default.arg) == "0"
    assert table.c.workflow_run_id.nullable is True
    assert table.c.workflow_phase.nullable is True
    assert table.c.workflow_node_key.nullable is True
    workflow_fk = next(
        constraint
        for constraint in table.foreign_key_constraints
        if constraint.name == "fk_llm_usage_events_workflow_run"
    )
    assert workflow_fk.ondelete == "RESTRICT"
    assert str(table.c.kind.server_default.arg) == "chat"
    assert _check_sql(table) == {
        "status IN ('reserved', 'accepted', 'completed')",
        "kind IN ('chat', 'image_generate', 'image_edit')",
        "prompt_tokens >= 0",
        "completion_tokens >= 0",
        "total_tokens >= 0",
        "(workflow_run_id IS NULL AND workflow_phase IS NULL AND "
        "workflow_node_key IS NULL) OR (workflow_run_id IS NOT NULL AND "
        "((workflow_phase = 'node' AND workflow_node_key IS NOT NULL) OR "
        "(workflow_phase IN ('supervisor_prepare', 'supervisor_synthesize') AND "
        "workflow_node_key IS NULL)))",
    }


def test_monthly_usage_schema_has_composite_key_and_non_negative_counters() -> None:
    table = OrganizationUsageMonthly.__table__

    assert table.name == "organization_usage_monthly"
    assert list(table.primary_key.columns.keys()) == ["org_id", "period_start"]
    assert next(iter(table.c.org_id.foreign_keys)).ondelete == "CASCADE"
    assert str(table.c.requests_used.server_default.arg) == "0"
    assert str(table.c.prompt_tokens.server_default.arg) == "0"
    assert str(table.c.completion_tokens.server_default.arg) == "0"
    assert str(table.c.total_tokens.server_default.arg) == "0"
    assert _check_sql(table) == {
        "requests_used >= 0",
        "prompt_tokens >= 0",
        "completion_tokens >= 0",
        "total_tokens >= 0",
    }
