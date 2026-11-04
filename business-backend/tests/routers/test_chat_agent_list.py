"""Which of the administrator's agents chat offers, and what a turn takes from one."""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo
from src.core.authorization_types import AuthorizationSubject
from src.models.access_grant import AccessGrant
from src.models.agent_definition import AgentDefinition
from src.models.knowledge_base import KnowledgeBase
from src.models.organization import Organization
from src.models.user import User
from src.routers import chat
from src.services.agent_authorization import (
    AgentAuthorizationService,
    AgentDefinitionNotFound,
)
from src.services.resource_registry import ResourceRegistry


async def _catalog() -> list[dict[str, object]]:
    return [{"id": "provider/model-v1"}]


def _grant(org: Organization, user: User, kind: str, resource_id: str) -> AccessGrant:
    return AccessGrant(
        org_id=org.id,
        resource_type=kind,
        resource_id=resource_id,
        principal_type="user",
        principal_id=user.id,
        permission="read",
        created_via="provisioning",
    )


@pytest.fixture
def service() -> AgentAuthorizationService:
    return AgentAuthorizationService(registry=ResourceRegistry(model_catalog=_catalog))


async def _agent(db: AsyncSession, org: Organization, user: User, name: str, *, grant: bool = True, **fields) -> AgentDefinition:  # noqa: ANN003
    values = {"model_id": "provider/model-v1", "system_prompt": "Be useful", **fields}
    agent = AgentDefinition(org_id=org.id, name=name, **values)
    db.add(agent)
    await db.flush()
    if grant:
        db.add(_grant(org, user, "agent_definition", str(agent.id)))
    await db.commit()
    return agent


async def test_chat_offers_only_active_granted_chat_capable_agents(
    db: AsyncSession, test_org: Organization, test_user: User, service: AgentAuthorizationService, monkeypatch: pytest.MonkeyPatch
) -> None:
    db.add(_grant(test_org, test_user, "model", "provider/model-v1"))
    await db.commit()
    shown = await _agent(db, test_org, test_user, "Shown")
    archived = await _agent(db, test_org, test_user, "Archived")
    await _agent(db, test_org, test_user, "Ungranted", grant=False)
    from datetime import datetime, timezone

    archived.archived_at = datetime.now(timezone.utc)
    await db.commit()
    monkeypatch.setattr(chat, "agent_authorization_service", service)
    user = UserInfo(user_id=str(test_user.id), org_id=str(test_org.id), role="member", raw_claims={})

    with patch.object(chat, "ensure_model_allowed", AsyncMock()):
        offered = await chat.chat_agent_definitions(user=user, session=db)

    assert [item.id for item in offered.data] == [str(shown.id)]
    assert (offered.data[0].modelId, offered.data[0].kind) == ("provider/model-v1", "agent")


async def test_hermes_agents_are_offered_only_where_a_hermes_turn_would_be_accepted(
    db: AsyncSession, test_org: Organization, test_user: User, service: AgentAuthorizationService, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.services.hermes_access import HermesAccessDenied

    hermes = await _agent(db, test_org, test_user, "Finance analyst", kind="hermes")
    monkeypatch.setattr(chat, "agent_authorization_service", service)
    user = UserInfo(user_id=str(test_user.id), org_id=str(test_org.id), role="member", raw_claims={})

    async def offered(enabled: bool, denied: bool = False, model: str | None = "hermes-model") -> list[tuple[str, str]]:
        monkeypatch.setattr(chat.settings, "HERMES_ENABLED", enabled)
        access = AsyncMock(side_effect=HermesAccessDenied("hermes_access_denied")) if denied else AsyncMock()
        with (
            patch.object(chat, "require_hermes_capabilities", access),
            patch.object(chat, "organization_hermes_model", AsyncMock(return_value=model)),
            patch.object(chat, "ensure_model_allowed", AsyncMock()),
        ):
            result = await chat.chat_agent_definitions(user=user, session=db)
        return [(item.id, item.kind) for item in result.data]

    assert await offered(True) == [(str(hermes.id), "hermes")]
    assert await offered(False) == []
    assert await offered(True, denied=True) == []
    assert await offered(True, model=None) == []


async def test_a_turn_takes_the_agents_settings_with_its_knowledge_base_names(
    db: AsyncSession, test_org: Organization, test_user: User, service: AgentAuthorizationService
) -> None:
    db.add(_grant(test_org, test_user, "model", "provider/model-v1"))
    base = KnowledgeBase(org_id=test_org.id, name="Procedures")
    db.add(base)
    await db.flush()
    db.add(_grant(test_org, test_user, "knowledge_base", str(base.id)))
    await db.commit()
    agent = await _agent(db, test_org, test_user, "Procedures assistant", knowledge_base_ids=[base.id], temperature=0.1)
    subject = AuthorizationSubject(test_user.id, test_org.id, "member")

    turn = await service.require_chat_agent(db, subject, agent.id)

    assert (turn.name, turn.model_id, turn.temperature) == ("Procedures assistant", "provider/model-v1", 0.1)
    assert turn.knowledge_bases == [{"id": str(base.id), "name": "Procedures"}]


async def test_a_hermes_agent_is_chosen_without_a_stale_model_getting_in_the_way(
    db: AsyncSession, test_org: Organization, test_user: User, service: AgentAuthorizationService
) -> None:
    # No model grant at all: a Hermes agent runs on the organization's current Hermes model,
    # which the chat route checks, so the model stored on the agent is not.
    hermes = await _agent(db, test_org, test_user, "Finance analyst", kind="hermes", model_id="stale/model")
    subject = AuthorizationSubject(test_user.id, test_org.id, "member")

    turn = await service.require_chat_agent(db, subject, hermes.id)

    assert turn.kind == "hermes" and turn.system_prompt == "Be useful"


async def test_archived_agents_cannot_run_a_chat_turn(
    db: AsyncSession, test_org: Organization, test_user: User, service: AgentAuthorizationService
) -> None:
    db.add(_grant(test_org, test_user, "model", "provider/model-v1"))
    await db.commit()
    gone = await _agent(db, test_org, test_user, "Gone")
    subject = AuthorizationSubject(test_user.id, test_org.id, "member")
    from datetime import datetime, timezone

    gone.archived_at = datetime.now(timezone.utc)
    await db.commit()

    with pytest.raises(AgentDefinitionNotFound):
        await service.require_chat_agent(db, subject, gone.id)
    with pytest.raises(AgentDefinitionNotFound):
        await service.require_chat_agent(db, subject, uuid.uuid4())
