from sqlalchemy import CheckConstraint, UniqueConstraint

from src.models.access_grant import AccessGrant
from src.models.authorization_audit_event import AuthorizationAuditEvent
from src.models.group_capability import GroupCapability
from src.models.organization_capability import OrganizationCapability


def _check_sql(table: object) -> set[str]:
    constraints = getattr(table, "constraints")
    return {
        str(constraint.sqltext)
        for constraint in constraints
        if isinstance(constraint, CheckConstraint)
    }


def test_capability_tables_use_composite_keys_and_closed_capability_values() -> None:
    organization_table = OrganizationCapability.__table__
    group_table = GroupCapability.__table__

    assert list(organization_table.primary_key.columns.keys()) == ["org_id", "capability"]
    assert list(group_table.primary_key.columns.keys()) == ["group_id", "capability"]
    assert next(iter(organization_table.c.org_id.foreign_keys)).ondelete == "CASCADE"
    assert next(iter(group_table.c.group_id.foreign_keys)).ondelete == "CASCADE"

    expected_check = {
        "capability IN ('admin.users.read', 'admin.users.write', 'chat.send', "
        "'images.generate', 'sharing.manage', 'web.access', 'workspace.agents.create', 'workspace.knowledge.create', "
        "'workspace.skills.create')"
    }
    assert _check_sql(organization_table) == expected_check
    assert _check_sql(group_table) == expected_check


def test_access_grant_schema_enforces_closed_values_and_source_actor_invariant() -> None:
    table = AccessGrant.__table__

    assert table.name == "access_grants"
    assert table.c.principal_id.nullable is False
    assert table.c.created_by.nullable is True
    assert not table.c.created_by.foreign_keys
    assert str(table.c.created_via.server_default.arg) == "'admin'"
    assert _check_sql(table) == {
        "resource_type IN ('model', 'skill', 'knowledge_base', 'agent_definition', 'workflow')",
        "principal_type IN ('organization', 'group', 'user')",
        "permission IN ('read', 'write')",
        "created_via IN ('admin', 'migration', 'provisioning')",
        "((created_via = 'admin' AND created_by IS NOT NULL) OR "
        "(created_via IN ('migration', 'provisioning') AND created_by IS NULL))",
    }

    unique_columns = {
        tuple(constraint.columns.keys())
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert (
        "org_id",
        "resource_type",
        "resource_id",
        "principal_type",
        "principal_id",
        "permission",
    ) in unique_columns


def test_access_grant_indexes_cover_resource_decisions_and_subject_preview() -> None:
    indexes = {
        index.name: tuple(index.columns.keys()) for index in AccessGrant.__table__.indexes
    }

    assert indexes["ix_access_grants_resource_lookup"] == (
        "org_id",
        "resource_type",
        "resource_id",
        "permission",
    )
    assert indexes["ix_access_grants_principal_lookup"] == (
        "org_id",
        "principal_type",
        "principal_id",
        "permission",
    )


def test_authorization_audit_is_structured_and_indexed_by_resource_and_actor() -> None:
    table = AuthorizationAuditEvent.__table__
    assert table.c.before_state.nullable is False
    assert table.c.after_state.nullable is False
    assert table.c.correlation_id.nullable is False
    assert next(iter(table.c.org_id.foreign_keys)).ondelete == "CASCADE"
    indexes = {index.name: tuple(index.columns.keys()) for index in table.indexes}
    assert indexes["ix_authorization_audit_events_resource"] == (
        "org_id",
        "resource_type",
        "resource_id",
        "created_at",
    )
    assert indexes["ix_authorization_audit_events_actor"] == (
        "org_id",
        "actor_id",
        "created_at",
    )
