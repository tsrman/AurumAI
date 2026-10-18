import uuid
from dataclasses import dataclass

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.core.config import settings
from src.core.authorization_types import AuthorizationSubject
from src.core.database import async_session

from src.models.organization import Organization
from src.models.user import User


@dataclass
class CommittedTemplate:
    sessions: async_sessionmaker[AsyncSession]
    actor: AuthorizationSubject
    template_id: uuid.UUID
    source_id: uuid.UUID
    agent_id: uuid.UUID


@pytest_asyncio.fixture
async def committed_template(template_snapshot):
    """Owned, committed rows for independent HTTP observers and lock contenders."""
    from src.models.agent_definition import AgentDefinition
    from src.models.workflow_definition import WorkflowDefinition
    from src.models.workflow_template import WorkflowTemplate
    from src.repositories.workflow_definition_repository import WorkflowDefinitionRepository
    from src.repositories.workflow_template_repository import WorkflowTemplateRepository
    from src.services.workflow_template_snapshot import workflow_draft_from_snapshot

    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    org_id, user_id, agent_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    actor = AuthorizationSubject(user_id, org_id, "org_admin")
    try:
        async with sessions() as session:
            session.add(Organization(id=org_id, name="C5 owned", slug=f"c5-owned-{org_id}"))
            await session.flush()
            session.add(
                User(
                    id=user_id,
                    org_id=org_id,
                    keycloak_id=uuid.uuid4(),
                    email=f"{user_id}@example.com",
                )
            )
            await session.flush()
            session.add(
                AgentDefinition(
                    id=agent_id,
                    org_id=org_id,
                    created_by=user_id,
                    name="Analyst",
                    model_id="provider/model-v1",
                    system_prompt="Original settings",
                    skill_ids=[],
                    knowledge_base_ids=[],
                )
            )
            await session.flush()
            source = await WorkflowDefinitionRepository().create(
                session,
                actor,
                workflow_draft_from_snapshot(
                    template_snapshot, {"a0": agent_id}, name="Source", description=None
                ),
            )
            template = await WorkflowTemplateRepository().create(
                session, actor, name="Template", description=None, snapshot=template_snapshot
            )
            await session.commit()
            yield_value = CommittedTemplate(sessions, actor, template.id, source.id, agent_id)
        yield yield_value
    finally:
        async with sessions() as cleanup:
            # Graph rows cascade from these exact owned workflow IDs. Remove
            # workflows before templates/agents because their references restrict.
            await cleanup.execute(
                delete(WorkflowDefinition).where(WorkflowDefinition.org_id == org_id)
            )
            await cleanup.execute(delete(WorkflowTemplate).where(WorkflowTemplate.org_id == org_id))
            await cleanup.execute(delete(AgentDefinition).where(AgentDefinition.org_id == org_id))
            await cleanup.execute(delete(Organization).where(Organization.id == org_id))
            await cleanup.commit()
        await engine.dispose()


@pytest.fixture
def template_snapshot():
    from src.schemas.workflow_template import TemplateSnapshot

    return TemplateSnapshot.model_validate(
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
                    "temperature": 0.3,
                    "skillIds": [],
                    "knowledgeBaseIds": [],
                }
            ],
            "nodes": [
                {
                    "nodeKey": "research",
                    "displayName": "Research",
                    "agentKey": "a0",
                    "instruction": "Find evidence",
                    "requiresConfirmation": True,
                }
            ],
            "edges": [],
        }
    )


@pytest_asyncio.fixture
async def template_source(db, test_org, test_user, template_snapshot):
    from src.core.authorization_types import AuthorizationSubject
    from src.models.agent_definition import AgentDefinition
    from src.repositories.workflow_definition_repository import (
        WorkflowDefinitionRepository,
    )
    from src.services.workflow_template_snapshot import workflow_draft_from_snapshot

    agent = AgentDefinition(
        org_id=test_org.id,
        created_by=test_user.id,
        name="Source analyst",
        description=template_snapshot.agents[0].description,
        model_id=template_snapshot.agents[0].model_id,
        system_prompt=template_snapshot.agents[0].system_prompt,
        temperature=template_snapshot.agents[0].temperature,
        skill_ids=template_snapshot.agents[0].skill_ids,
        knowledge_base_ids=template_snapshot.agents[0].knowledge_base_ids,
    )
    db.add(agent)
    await db.flush()
    actor = AuthorizationSubject(test_user.id, test_org.id, "org_admin")
    source = await WorkflowDefinitionRepository().create(
        db,
        actor,
        workflow_draft_from_snapshot(
            template_snapshot,
            {"a0": agent.id},
            name="Source",
            description="Source description",
        ),
    )
    source.auto_routable = True
    await db.flush()
    return source


@pytest_asyncio.fixture
async def db() -> AsyncSession:
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        await connection.begin_nested()
        async with async_session(bind=connection, expire_on_commit=False) as session:
            yield session
        await transaction.rollback()
    await engine.dispose()


@pytest_asyncio.fixture
async def test_org(db: AsyncSession) -> Organization:
    org = Organization(name="Test Org", slug=f"test-org-{uuid.uuid4().hex[:8]}")
    db.add(org)
    await db.commit()
    await db.refresh(org)
    return org


@pytest_asyncio.fixture
async def test_user(db: AsyncSession, test_org: Organization) -> User:
    user = User(
        keycloak_id=uuid.uuid4(),
        email=f"test-{uuid.uuid4().hex[:8]}@example.com",
        org_id=test_org.id,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@pytest.fixture
async def identity_database():
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from sqlalchemy.pool import NullPool
    from src.core.config import settings
    from src.core.database import _AppSession
    from src.models.identity_sync import IdentitySyncInbox, IdentitySyncState
    from src.models.organization import Organization
    admin_engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    app_engine = create_async_engine(
        settings.DATABASE_URL, poolclass=NullPool,
        connect_args={"server_settings": {"role": "aurum_ai_app"}},
    )
    sessions = async_sessionmaker(app_engine, sync_session_class=_AppSession, expire_on_commit=False)
    admin_sessions = async_sessionmaker(admin_engine, expire_on_commit=False)
    org_id, user_id = uuid.uuid4(), uuid.uuid4()
    try:
        yield sessions, org_id, user_id
    finally:
        async with admin_sessions() as cleanup:
            await cleanup.execute(delete(IdentitySyncInbox).where(IdentitySyncInbox.org_id == org_id))
            await cleanup.execute(delete(IdentitySyncState).where(IdentitySyncState.org_id == org_id))
            await cleanup.execute(delete(Organization).where(Organization.id == org_id))
            await cleanup.commit()
        await app_engine.dispose()
        await admin_engine.dispose()


@pytest.fixture
def mock_chat_lifecycle(monkeypatch):
    """Fake durable DB/Temporal boundary for router suites using fake sessions."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from src.services import conversation_lifecycle, execution_closure
    monkeypatch.setattr(conversation_lifecycle, 'begin_operation', AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4(), generation=1)))
    monkeypatch.setattr(conversation_lifecycle, 'settle_operation', AsyncMock())
    monkeypatch.setattr(execution_closure, 'register_execution', AsyncMock())
