import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from jwt.exceptions import PyJWTError
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.core import dependencies
from src.core.config import settings
from src.models.access_grant import AccessGrant
from src.models.organization import Organization
from src.models.agent_definition import AgentDefinition
from src.models.conversation import Conversation
from src.models.message import Message
from src.models.skill import Skill
from src.models.user import User
from src.models.workflow_definition import WorkflowDefinition
from src.models.workflow_run import WorkflowRun
from src.routers import skills, workflow_runs_internal
from src.services.resource_registry import ResourceRegistry
from src.services.session_token import session_token_service
from src.services.workflow_authorization import WorkflowAuthorizationService


@pytest.mark.asyncio
async def test_same_identity_only_token_observes_committed_skill_revocation(
    monkeypatch,
) -> None:
    org_id = uuid.uuid4()
    user_id = uuid.uuid4()
    skill_id = uuid.uuid4()
    grant_id = uuid.uuid4()
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(dependencies, "async_session", sessions)

    async with sessions() as setup:
        setup.add(
            Organization(
                id=org_id,
                name="Live Revocation Org",
                slug=f"live-revoke-{org_id.hex[:8]}",
            )
        )
        await setup.commit()
        setup.add_all(
            [
                User(
                    id=user_id,
                    keycloak_id=user_id,
                    email=f"live-revoke-{user_id.hex[:8]}@example.com",
                    org_id=org_id,
                    role="member",
                ),
                Skill(
                    id=skill_id,
                    org_id=org_id,
                    name="live_revocation_skill",
                    type="tool",
                ),
            ]
        )
        await setup.flush()
        setup.add(
            AccessGrant(
                id=grant_id,
                org_id=org_id,
                resource_type="skill",
                resource_id=str(skill_id),
                principal_type="user",
                principal_id=user_id,
                permission="read",
                created_via="provisioning",
            )
        )
        await setup.commit()

    async def reject_as_keycloak_token(_token: str) -> dict[str, object]:
        raise PyJWTError("not a Keycloak token")

    monkeypatch.setattr(
        "src.core.auth.keycloak_client.decode_token", reject_as_keycloak_token
    )
    from src.models.conversation import Conversation
    conversation_id = uuid.uuid4()
    async with sessions() as setup:
        setup.add(Conversation(id=conversation_id, org_id=org_id, user_id=user_id, model="test"))
        await setup.commit()
    token = session_token_service.issue(str(conversation_id), str(user_id), str(org_id))
    app = FastAPI()
    app.include_router(skills.router, prefix="/v1/skills")

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = {"Authorization": f"Bearer {token}"}
            before = await client.get(f"/v1/skills/{skill_id}", headers=headers)

            async with sessions() as revoke:
                revoke.info["org_id"] = str(org_id)
                await revoke.execute(
                    delete(AccessGrant).where(AccessGrant.id == grant_id)
                )
                await revoke.commit()

            after = await client.get(f"/v1/skills/{skill_id}", headers=headers)

        assert before.status_code == 200
        assert before.json()["data"]["id"] == str(skill_id)
        assert after.status_code == 404
    finally:
        async with sessions() as cleanup:
            cleanup.info["org_id"] = str(org_id)
            await cleanup.execute(delete(Organization).where(Organization.id == org_id))
            await cleanup.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_workflow_step_observes_committed_model_revocation(
    monkeypatch,
) -> None:
    org_id, user_id, conversation_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    agent_id, definition_id, message_id, run_id = (
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
    )
    turn_id = uuid.uuid4()
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(dependencies, "async_session", sessions)

    async def model_catalog() -> list[dict[str, object]]:
        return [{"id": "provider/model-v1"}]

    monkeypatch.setattr(
        workflow_runs_internal,
        "workflow_authorization_service",
        WorkflowAuthorizationService(
            registry=ResourceRegistry(model_catalog=model_catalog)
        ),
    )

    async with sessions() as setup:
        setup.add(
            Organization(
                id=org_id,
                name="Workflow Live Revocation Org",
                slug=f"workflow-live-revoke-{org_id.hex[:8]}",
            )
        )
        await setup.commit()
        setup.add_all(
            [
                User(
                    id=user_id,
                    keycloak_id=user_id,
                    email=f"workflow-live-revoke-{user_id.hex[:8]}@example.com",
                    org_id=org_id,
                    role="member",
                ),
                AgentDefinition(
                    id=agent_id,
                    org_id=org_id,
                    name="Supervisor",
                    model_id="provider/model-v1",
                    system_prompt="Plan",
                ),
            ]
        )
        await setup.flush()
        setup.add(
            WorkflowDefinition(
                id=definition_id,
                org_id=org_id,
                name="Revocation workflow",
                supervisor_agent_definition_id=agent_id,
            )
        )
        setup.add(
            Conversation(
                id=conversation_id,
                org_id=org_id,
                user_id=user_id,
                title="Revocation",
                model="provider/model-v1",
            )
        )
        await setup.flush()
        setup.add(
            Message(
                id=message_id,
                org_id=org_id,
                conversation_id=conversation_id,
                turn_id=turn_id,
                user_id=user_id,
                role="user",
                content="Run",
            )
        )
        await setup.flush()
        setup.add(
            WorkflowRun(
                id=run_id,
                org_id=org_id,
                workflow_definition_id=definition_id,
                workflow_definition_version=1,
                initiating_user_id=user_id,
                conversation_id=conversation_id,
                turn_id=turn_id,
                source_message_id=message_id,
                temporal_workflow_id=f"conversation-{conversation_id}:workflow:{turn_id}",
                execution_snapshot={
                    "supervisor": {
                        "agentDefinitionId": str(agent_id),
                        "model": "provider/model-v1",
                        "allowedSkillIds": [],
                        "knowledgeBases": [],
                    },
                    "nodes": [],
                },
            )
        )
        setup.add_all(
            [
                AccessGrant(
                    org_id=org_id,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    principal_type="user",
                    principal_id=user_id,
                    permission="read",
                    created_via="provisioning",
                )
                for resource_type, resource_id in (
                    ("workflow", str(definition_id)),
                    ("agent_definition", str(agent_id)),
                    ("model", "provider/model-v1"),
                )
            ]
        )
        await setup.commit()

    async def reject_as_keycloak_token(_token: str) -> dict[str, object]:
        raise PyJWTError("not a Keycloak token")

    monkeypatch.setattr(
        "src.core.auth.keycloak_client.decode_token", reject_as_keycloak_token
    )
    token = session_token_service.issue(str(conversation_id), str(user_id), str(org_id))
    app = FastAPI()
    app.include_router(workflow_runs_internal.router, prefix="/v1/internal")

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = {"Authorization": f"Bearer {token}"}
            endpoint = f"/v1/internal/workflow-runs/{run_id}/authorize-step"
            body = {"phase": "supervisor_prepare"}
            before = await client.post(endpoint, headers=headers, json=body)

            async with sessions() as revoke:
                revoke.info["org_id"] = str(org_id)
                await revoke.execute(
                    delete(AccessGrant).where(
                        AccessGrant.org_id == org_id,
                        AccessGrant.resource_type == "model",
                        AccessGrant.resource_id == "provider/model-v1",
                    )
                )
                await revoke.commit()

            after = await client.post(endpoint, headers=headers, json=body)

        assert before.status_code == 200
        assert after.status_code == 404
    finally:
        async with sessions() as cleanup:
            cleanup.info["org_id"] = str(org_id)
            await cleanup.execute(delete(Organization).where(Organization.id == org_id))
            await cleanup.commit()
        await engine.dispose()
