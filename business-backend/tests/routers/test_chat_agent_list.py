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
from src.models.organization_capability import OrganizationCapability
from src.models.user import User
from src.routers import chat
from src.services.agent_authorization import (
    AgentAuthorizationService,
    AgentDefinitionNotFound,
)
from src.services.resource_registry import ResourceRegistry
from src.core.capability_catalog import Capability


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


async def test_a_hermes_agent_names_only_the_knowledge_bases_the_user_may_read(
    db: AsyncSession, test_org: Organization, test_user: User, service: AgentAuthorizationService
) -> None:
    from src.routers.chat import _hermes_instructions

    readable, hidden = KnowledgeBase(org_id=test_org.id, name="Procedures"), KnowledgeBase(org_id=test_org.id, name="Secret")
    db.add_all([readable, hidden])
    await db.flush()
    db.add(_grant(test_org, test_user, "knowledge_base", str(readable.id)))
    await db.commit()
    hermes = await _agent(
        db,
        test_org,
        test_user,
        "Finance analyst",
        kind="hermes",
        knowledge_base_ids=[readable.id, hidden.id],
        ask_user_enabled=False,
    )
    subject = AuthorizationSubject(test_user.id, test_org.id, "member")

    turn = await service.require_chat_agent(db, subject, hermes.id)
    instructions = _hermes_instructions(turn)

    assert turn.knowledge_bases == [{"id": str(readable.id), "name": "Procedures"}]
    assert str(readable.id) in instructions and "Secret" not in instructions
    assert "cannot ask the user" in instructions


async def test_a_hermes_agent_with_its_own_model_carries_it(
    db: AsyncSession, test_org: Organization, test_user: User, service: AgentAuthorizationService
) -> None:
    own = await _agent(db, test_org, test_user, "Own", kind="hermes", model_id="own/model", use_org_hermes_model=False)
    shared = await _agent(db, test_org, test_user, "Shared", kind="hermes", model_id="stale/model")
    subject = AuthorizationSubject(test_user.id, test_org.id, "member")

    assert (await service.require_chat_agent(db, subject, own.id)).hermes_model == "own/model"
    assert (await service.require_chat_agent(db, subject, shared.id)).hermes_model is None


@pytest.mark.parametrize(
    ("organization_model", "own_allowed", "org_allowed", "enabled", "capability", "expected"),
    [
        ("org/model", True, False, True, True, {"Own": "own/model"}),
        (None, True, False, True, True, {"Own": "own/model"}),
        ("org/model", False, True, True, True, {"Shared": "org/model"}),
        ("org/model", True, True, True, True, {"Own": "own/model", "Shared": "org/model"}),
        ("org/model", True, True, False, True, {}),
        ("org/model", True, True, True, False, {}),
    ],
)
async def test_hermes_catalog_checks_only_each_agents_effective_model(
    db, test_org, test_user, monkeypatch, organization_model, own_allowed, org_allowed,
    enabled, capability, expected,
) -> None:  # noqa: ANN001
    await _agent(db, test_org, test_user, "Own", kind="hermes", model_id="own/model", use_org_hermes_model=False)
    await _agent(db, test_org, test_user, "Shared", kind="hermes")
    db.add(OrganizationCapability(org_id=test_org.id, capability=Capability.CHAT_SEND.value))
    if capability:
        db.add(OrganizationCapability(org_id=test_org.id, capability=Capability.AGENTS_HERMES.value))
    if own_allowed:
        db.add(_grant(test_org, test_user, "model", "own/model"))
    if org_allowed:
        db.add(_grant(test_org, test_user, "model", "org/model"))
    await db.commit()
    user = UserInfo(user_id=str(test_user.id), org_id=str(test_org.id), role="member", raw_claims={})
    monkeypatch.setattr(chat.settings, "HERMES_ENABLED", enabled)
    monkeypatch.setattr(chat, "organization_hermes_model", AsyncMock(return_value=organization_model))

    result = await chat.chat_agent_definitions(user=user, session=db)

    assert {item.name: item.modelId for item in result.data} == expected
