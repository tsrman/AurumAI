from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import update

from src.core.authorization_types import AuthorizationSubject
from src.models.workflow_template import WorkflowTemplate, WorkflowTemplateStatus
from src.repositories.workflow_template_repository import WorkflowTemplateRepository
from src.schemas.workflow_template import TemplateSnapshot
from src.services.workflow_template_errors import WorkflowTemplateError


def _replacement_snapshot(snapshot: TemplateSnapshot) -> TemplateSnapshot:
    replacement = snapshot.model_copy(deep=True)
    replacement.agents[0].system_prompt = "Changed answer"
    return replacement


async def test_create_persists_version_one_with_fresh_aliased_snapshot(
    db, test_org, test_user, template_snapshot
) -> None:
    actor = AuthorizationSubject(test_user.id, test_org.id, "org_admin")
    original_snapshot = template_snapshot.model_dump(mode="json", by_alias=True)

    template = await WorkflowTemplateRepository().create(
        db,
        actor,
        name="Template",
        description="Saved workflow",
        snapshot=template_snapshot,
    )

    assert template.org_id == test_org.id
    assert template.version == 1
    assert template.status == WorkflowTemplateStatus.ACTIVE.value
    assert template.created_by == test_user.id
    assert template.updated_by == test_user.id
    assert template.snapshot == original_snapshot
    assert template.snapshot is not original_snapshot
    assert "supervisorAgentKey" in template.snapshot
    assert "supervisor_agent_key" not in template.snapshot
    assert template.created_at is not None
    assert template.updated_at is not None


async def test_get_is_org_scoped_and_locked_get_refreshes_identity_map(
    db, test_org, test_user, template_snapshot
) -> None:
    actor = AuthorizationSubject(test_user.id, test_org.id, "org_admin")
    repository = WorkflowTemplateRepository()
    template = await repository.create(
        db,
        actor,
        name="Cached name",
        description=None,
        snapshot=template_snapshot,
    )

    assert await repository.get(db, uuid4(), template.id) is None
    await db.execute(
        update(WorkflowTemplate)
        .where(
            WorkflowTemplate.org_id == test_org.id,
            WorkflowTemplate.id == template.id,
        )
        .values(name="Database name", version=7)
        .execution_options(synchronize_session=False)
    )
    assert template.name == "Cached name"

    locked = await repository.get(db, test_org.id, template.id, for_update=True)

    assert locked is template
    assert locked.name == "Database name"
    assert locked.version == 7


async def test_list_by_org_filters_archived_and_orders_by_name_then_id(
    db, test_org, test_user, template_snapshot
) -> None:
    actor = AuthorizationSubject(test_user.id, test_org.id, "org_admin")
    repository = WorkflowTemplateRepository()
    gamma = await repository.create(
        db, actor, name="Gamma", description=None, snapshot=template_snapshot
    )
    alpha = await repository.create(
        db, actor, name="Alpha", description=None, snapshot=template_snapshot
    )
    beta = await repository.create(
        db, actor, name="Beta", description=None, snapshot=template_snapshot
    )
    await repository.archive_locked(db, actor, alpha, version=1)

    active = await repository.list_by_org(db, test_org.id)
    all_templates = await repository.list_by_org(
        db, test_org.id, include_archived=True
    )

    assert [item.id for item in active] == [beta.id, gamma.id]
    assert [item.id for item in all_templates] == [alpha.id, beta.id, gamma.id]
    assert await repository.list_by_org(db, uuid4(), include_archived=True) == []


async def test_archive_rejects_stale_version_and_is_idempotent_at_current_version(
    db, test_org, test_user, template_snapshot
) -> None:
    actor = AuthorizationSubject(test_user.id, test_org.id, "org_admin")
    repository = WorkflowTemplateRepository()
    template = await repository.create(
        db, actor, name="Template", description=None, snapshot=template_snapshot
    )
    locked = await repository.get(db, test_org.id, template.id, for_update=True)
    assert locked is not None

    archived = await repository.archive_locked(db, actor, locked, version=1)
    archived_at = archived.updated_at

    assert archived.version == 2
    assert archived.status == WorkflowTemplateStatus.ARCHIVED.value
    assert archived.updated_by == test_user.id
    with pytest.raises(WorkflowTemplateError) as caught:
        await repository.archive_locked(db, actor, locked, version=1)
    assert (caught.value.status_code, caught.value.code) == (409, "version_conflict")

    archived_again = await repository.archive_locked(db, actor, locked, version=2)

    assert archived_again is archived
    assert archived_again.version == 2
    assert archived_again.updated_at == archived_at


async def test_replace_rejects_stale_and_archived_templates_before_mutation(
    db, test_org, test_user, template_snapshot
) -> None:
    actor = AuthorizationSubject(test_user.id, test_org.id, "org_admin")
    repository = WorkflowTemplateRepository()
    template = await repository.create(
        db, actor, name="Template", description=None, snapshot=template_snapshot
    )
    replacement = _replacement_snapshot(template_snapshot)
    await repository.archive_locked(db, actor, template, version=1)

    with pytest.raises(WorkflowTemplateError) as stale:
        await repository.replace_locked(
            db,
            actor,
            template,
            version=1,
            name="Stale",
            description=None,
            snapshot=replacement,
        )
    assert (stale.value.status_code, stale.value.code) == (409, "version_conflict")

    with pytest.raises(WorkflowTemplateError) as archived:
        await repository.replace_locked(
            db,
            actor,
            template,
            version=2,
            name="Archived",
            description=None,
            snapshot=replacement,
        )
    assert (archived.value.status_code, archived.value.code) == (
        409,
        "template_archived",
    )
    assert template.name == "Template"
    assert template.snapshot["agents"][0]["systemPrompt"] == "Answer"


async def test_replace_increments_version_without_mutating_old_snapshot_copy(
    db, test_org, test_user, template_snapshot
) -> None:
    actor = AuthorizationSubject(test_user.id, test_org.id, "org_admin")
    repository = WorkflowTemplateRepository()
    template = await repository.create(
        db, actor, name="Template", description=None, snapshot=template_snapshot
    )
    old_snapshot_reference = template.snapshot
    old_snapshot_value = deepcopy(old_snapshot_reference)
    original_updated_at = template.updated_at

    updated = await repository.replace_locked(
        db,
        actor,
        template,
        version=1,
        name="Replacement",
        description="New description",
        snapshot=_replacement_snapshot(template_snapshot),
    )

    assert updated is template
    assert updated.version == 2
    assert updated.name == "Replacement"
    assert updated.description == "New description"
    assert updated.updated_by == test_user.id
    assert updated.updated_at >= original_updated_at
    assert updated.snapshot["agents"][0]["systemPrompt"] == "Changed answer"
    assert old_snapshot_reference == old_snapshot_value
    assert old_snapshot_reference["agents"][0]["systemPrompt"] == "Answer"
    assert updated.snapshot is not old_snapshot_reference
