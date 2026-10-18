import asyncio

import pytest
from sqlalchemy import select, text

from src.models.agent_definition import AgentDefinition
from src.models.workflow_definition import WorkflowDefinition
from src.models.workflow_template import WorkflowTemplate
from src.repositories.workflow_definition_repository import WorkflowDefinitionRepository
from src.schemas.workflow import WorkflowDefinitionReplace
from src.schemas.workflow_template import (
    TemplateCaptureRequest,
    TemplateInstantiateRequest,
    TemplateReplaceRequest,
    TemplateVersionRequest,
)
from src.services.model_catalog import GatewayCatalogSnapshot, GatewayCatalogStatus
from src.services.workflow_template import WorkflowTemplateService
from src.services.workflow_template_errors import WorkflowTemplateError
from src.services.workflow_template_snapshot import workflow_draft_from_snapshot


async def catalog():
    return GatewayCatalogSnapshot(GatewayCatalogStatus.AVAILABLE, ({"id": "provider/model-v1"},))


async def wait_for_lock(sessions, waiter_pid, holder_pid):
    """Prove a real lock wait before releasing the holder; timeout is a failure."""
    async with asyncio.timeout(5), sessions() as observer:
        while not await observer.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = :waiter AND NOT granted) "
                "AND :holder = ANY(pg_blocking_pids(:waiter))"
            ),
            {"waiter": waiter_pid, "holder": holder_pid},
        ):
            await asyncio.sleep(0.01)  # polling backoff, never an ordering assertion


async def run_contender(session, started, operation):
    started.set()
    try:
        result = await operation()
        await session.commit()
        return result
    except Exception:
        await session.rollback()
        raise


@pytest.mark.parametrize("first", ["instantiate", "archive"])
async def test_instantiate_and_archive_serialize_in_both_orders(committed_template, first):
    fixture = committed_template
    service = WorkflowTemplateService(catalog=catalog)
    async with fixture.sessions() as holder, fixture.sessions() as waiter:
        holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
        waiter_pid = await waiter.scalar(text("SELECT pg_backend_pid()"))
        assert holder_pid != waiter_pid
        # Keep an old identity-map entry: the blocked operation must refresh it.
        stale_template = await waiter.get(WorkflowTemplate, fixture.template_id)
        assert stale_template.version == 1
        if first == "instantiate":
            created = await service.instantiate(
                holder,
                fixture.actor,
                fixture.template_id,
                TemplateInstantiateRequest(version=1, name="Copy"),
            )

            async def operation():
                return await service.archive(
                    waiter, fixture.actor, fixture.template_id, TemplateVersionRequest(version=1)
                )
        else:
            await service.archive(
                holder, fixture.actor, fixture.template_id, TemplateVersionRequest(version=1)
            )

            async def operation():
                return await service.instantiate(
                    waiter,
                    fixture.actor,
                    fixture.template_id,
                    TemplateInstantiateRequest(version=2, name="Copy"),
                )

        started = asyncio.Event()
        task = asyncio.create_task(run_contender(waiter, started, operation))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            await wait_for_lock(fixture.sessions, waiter_pid, holder_pid)
            assert not task.done()
            await holder.commit()
            if first == "archive":
                with pytest.raises(WorkflowTemplateError) as error:
                    await asyncio.wait_for(task, timeout=5)
                assert (error.value.code, error.value.status_code) == ("template_archived", 409)
            else:
                archived = await asyncio.wait_for(task, timeout=5)
                assert (archived.status, archived.version) == ("archived", 2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await holder.rollback()
    async with fixture.sessions() as observer:
        stored = await observer.get(WorkflowTemplate, fixture.template_id)
        assert (stored.status, stored.version) == ("archived", 2)
        copies = (
            await observer.scalars(
                select(WorkflowDefinition).where(
                    WorkflowDefinition.org_id == fixture.actor.org_id,
                    WorkflowDefinition.id != fixture.source_id,
                )
            )
        ).all()
        assert len(copies) == (1 if first == "instantiate" else 0)
        if copies:
            assert copies[0].id == created.workflow.id
            assert copies[0].source_template_version == 1


async def test_concurrent_replace_has_one_success_and_one_stale_conflict(committed_template):
    fixture = committed_template
    service = WorkflowTemplateService(catalog=catalog)
    data = TemplateReplaceRequest(
        source_workflow_id=fixture.source_id, source_workflow_version=1, version=1, name="Winner"
    )
    async with fixture.sessions() as holder, fixture.sessions() as waiter:
        holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
        waiter_pid = await waiter.scalar(text("SELECT pg_backend_pid()"))
        stale = await waiter.get(WorkflowTemplate, fixture.template_id)
        assert stale.version == 1
        winner = await service.replace(holder, fixture.actor, fixture.template_id, data)
        assert winner.version == 2

        async def operation():
            return await service.replace(
                waiter,
                fixture.actor,
                fixture.template_id,
                data.model_copy(update={"name": "Loser"}),
            )

        started = asyncio.Event()
        task = asyncio.create_task(run_contender(waiter, started, operation))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            await wait_for_lock(fixture.sessions, waiter_pid, holder_pid)
            await holder.commit()
            with pytest.raises(WorkflowTemplateError) as error:
                await asyncio.wait_for(task, timeout=5)
            assert (error.value.code, error.value.status_code) == ("version_conflict", 409)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await holder.rollback()
    async with fixture.sessions() as observer:
        stored = await observer.get(WorkflowTemplate, fixture.template_id)
        assert (stored.name, stored.version) == ("Winner", 2)


@pytest.mark.parametrize("requested_version", [1, 2])
async def test_blocked_capture_refreshes_source_version_graph_and_agent_settings(
    committed_template, template_snapshot, requested_version
):
    fixture = committed_template
    service = WorkflowTemplateService(catalog=catalog)
    repository = WorkflowDefinitionRepository()
    async with fixture.sessions() as holder, fixture.sessions() as waiter:
        holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
        waiter_pid = await waiter.scalar(text("SELECT pg_backend_pid()"))
        stale_source = await waiter.get(WorkflowDefinition, fixture.source_id)
        stale_agent = await waiter.get(AgentDefinition, fixture.agent_id)
        stale_nodes, _ = await repository.load_graph(
            waiter, fixture.actor.org_id, fixture.source_id
        )
        assert stale_source.version == 1
        assert stale_agent.system_prompt == "Original settings"
        draft = workflow_draft_from_snapshot(
            template_snapshot,
            {"a0": fixture.agent_id},
            name="Source",
            description="New description",
        )
        draft.nodes[0].instruction = "New committed instruction"
        await repository.replace(
            holder,
            fixture.actor,
            fixture.source_id,
            WorkflowDefinitionReplace(**draft.model_dump(), version=1),
        )
        agent = await holder.get(AgentDefinition, fixture.agent_id)
        agent.system_prompt = "New committed settings"
        agent.temperature = 0.8
        await holder.flush()

        async def operation():
            return await service.capture(
                waiter,
                fixture.actor,
                TemplateCaptureRequest(
                    source_workflow_id=fixture.source_id,
                    source_workflow_version=requested_version,
                    name="Fresh capture",
                ),
            )

        started = asyncio.Event()
        task = asyncio.create_task(run_contender(waiter, started, operation))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            await wait_for_lock(fixture.sessions, waiter_pid, holder_pid)
            await holder.commit()
            if requested_version == 1:
                with pytest.raises(WorkflowTemplateError) as error:
                    await asyncio.wait_for(task, timeout=5)
                assert (error.value.code, error.value.status_code) == (
                    "source_version_conflict",
                    409,
                )
            else:
                captured = await asyncio.wait_for(task, timeout=5)
                assert captured.snapshot["agents"][0]["systemPrompt"] == "New committed settings"
                assert captured.snapshot["agents"][0]["temperature"] == 0.8
                assert captured.snapshot["nodes"][0]["instruction"] == "New committed instruction"
                assert stale_source.version == 2
                assert stale_agent.system_prompt == "New committed settings"
                assert stale_nodes[0].instruction == "Find evidence"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await holder.rollback()
    async with fixture.sessions() as observer:
        captures = (
            await observer.scalars(
                select(WorkflowTemplate).where(
                    WorkflowTemplate.org_id == fixture.actor.org_id,
                    WorkflowTemplate.id != fixture.template_id,
                )
            )
        ).all()
        assert len(captures) == (1 if requested_version == 2 else 0)
