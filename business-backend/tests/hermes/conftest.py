"""A committed organization with a user, a conversation and an active turn.

Hermes routes open their own database sessions, so the fixture commits real
rows through a dedicated engine and removes the organization afterwards.
"""

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport
from jwt.exceptions import PyJWTError
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.core import dependencies, hermes_auth
from src.core.capability_catalog import Capability
from src.core.config import settings
from src.middleware.error_handler import setup_exception_handlers
from src.models.agent_definition import AgentDefinition
from src.models.conversation import Conversation
from src.models.hermes_session import HermesSession
from src.models.interaction import InteractionTurn
from src.models.message import Message
from src.models.workflow_definition import WorkflowDefinition
from src.models.workflow_run import WorkflowRun
from src.models.organization import Organization
from src.models.organization_capability import OrganizationCapability
from src.models.user import User
from src.routers import hermes, hermes_internal
from src.schemas.interaction import TurnLimits
from src.services.quota import UsageReservation, get_quota_service
from src.services.session_token import session_token_service

MODEL = "provider/hermes-model"


class RecordingQuota:
    def __init__(self) -> None:
        self.reservations: list[dict[str, Any]] = []
        self.accepted: list[uuid.UUID] = []
        self.completed: list[tuple[uuid.UUID, int, int, int]] = []
        self.cancelled: list[uuid.UUID] = []

    async def reserve(self, org_id, user_id, conversation_id, model, request_id, **correlation):  # noqa: ANN001, ANN003, ANN201
        self.reservations.append(
            {
                "org_id": org_id,
                "user_id": user_id,
                "conversation_id": conversation_id,
                "model": model,
                "request_id": request_id,
                **correlation,
            }
        )
        from datetime import date

        return UsageReservation(request_id=request_id, period_start=date(2026, 10, 1))

    async def accept(self, org_id, request_id, **_):  # noqa: ANN001, ANN003, ANN201
        self.accepted.append(request_id)

    async def complete(self, org_id, request_id, prompt, completion, total, **_):  # noqa: ANN001, ANN003, ANN201
        self.completed.append((request_id, prompt, completion, total))

    async def cancel(self, org_id, request_id):  # noqa: ANN001, ANN201
        self.cancelled.append(request_id)


@pytest_asyncio.fixture
async def hermes_env(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SimpleNamespace]:
    org_id, user_id, conversation_id, turn_id = (uuid.uuid4() for _ in range(4))
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(dependencies, "async_session", sessions)
    monkeypatch.setattr(hermes_auth, "async_session", sessions)
    monkeypatch.setattr(settings, "HERMES_ENABLED", True)

    async def reject_as_keycloak_token(_token: str) -> dict[str, object]:
        raise PyJWTError("not a Keycloak token")

    monkeypatch.setattr("src.core.auth.keycloak_client.decode_token", reject_as_keycloak_token)
    granted_models: list[tuple[object, ...]] = []

    async def allow_model(session, org, model, **kwargs):  # noqa: ANN001, ANN003, ANN202
        granted_models.append((org, model, kwargs))

    monkeypatch.setattr(hermes, "ensure_model_allowed", allow_model)
    async with sessions() as setup:
        setup.add(Organization(id=org_id, name="Hermes Org", slug=f"hermes-{org_id.hex[:8]}"))
        await setup.commit()
        setup.add(
            User(
                id=user_id,
                keycloak_id=user_id,
                email=f"hermes-{user_id.hex[:8]}@example.com",
                org_id=org_id,
                role="member",
            )
        )
        await setup.flush()
        setup.add_all(
            OrganizationCapability(org_id=org_id, capability=capability.value)
            for capability in (Capability.CHAT_SEND, Capability.AGENTS_HERMES)
        )
        setup.add(Conversation(id=conversation_id, org_id=org_id, user_id=user_id, model=MODEL))
        await setup.flush()
        setup.add(
            InteractionTurn(
                id=turn_id,
                org_id=org_id,
                conversation_id=conversation_id,
                owner_id=user_id,
                limits=TurnLimits().model_dump(),
            )
        )
        await setup.commit()

    quota = RecordingQuota()
    app = FastAPI()
    setup_exception_handlers(app)
    app.include_router(hermes.router, prefix="/v1")
    app.include_router(hermes_internal.router, prefix="/v1/internal")
    app.dependency_overrides[get_quota_service] = lambda: quota

    async def finish_turn() -> None:
        async with sessions() as change:
            await change.execute(
                update(InteractionTurn)
                .where(InteractionTurn.id == turn_id)
                .values(status="completed")
            )
            await change.commit()

    async def revoke(capability: Capability) -> None:
        async with sessions() as change:
            await change.execute(
                delete(OrganizationCapability).where(
                    OrganizationCapability.org_id == org_id,
                    OrganizationCapability.capability == capability.value,
                )
            )
            await change.commit()

    runner_token = session_token_service.issue(str(conversation_id), str(user_id), str(org_id))

    async def internal(method: str, path: str, **kwargs: Any) -> httpx.Response:
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.request(
                method,
                f"/v1/internal/hermes{path}",
                headers={"Authorization": f"Bearer {runner_token}"},
                **kwargs,
            )

    async def open_session(*, bind: bool = True, **correlation: Any) -> dict[str, Any]:
        opened = await internal("POST", "/sessions")
        assert opened.status_code == 201, opened.text
        body = opened.json()
        if bind:
            bound = await internal(
                "PUT",
                f"/sessions/{body['sessionId']}/turn",
                json={"turnId": str(turn_id), **correlation},
            )
            assert bound.status_code == 204, bound.text
        return body

    async def expire_session(session_id: str) -> None:
        async with sessions() as change:
            await change.execute(
                update(HermesSession)
                .where(HermesSession.id == uuid.UUID(session_id))
                .values(expires_at=HermesSession.created_at)
            )
            await change.commit()

    async def add_workflow_run(*, status: str = "running") -> uuid.UUID:
        agent_id, definition_id, message_id, run_id = (uuid.uuid4() for _ in range(4))
        async with sessions() as change:
            change.add(
                AgentDefinition(
                    id=agent_id, org_id=org_id, name="Supervisor", model_id=MODEL, system_prompt="Plan"
                )
            )
            await change.flush()
            change.add(
                WorkflowDefinition(
                    id=definition_id,
                    org_id=org_id,
                    name="Hermes workflow",
                    supervisor_agent_definition_id=agent_id,
                )
            )
            change.add(
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
            await change.flush()
            change.add(
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
                    status=status,
                    execution_snapshot={"supervisor": {}, "nodes": []},
                )
            )
            await change.flush()
            await change.execute(
                update(InteractionTurn)
                .where(InteractionTurn.id == turn_id)
                .values(workflow_run_id=run_id)
            )
            await change.commit()
        return run_id

    try:
        yield SimpleNamespace(
            org_id=org_id,
            user_id=user_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            app=app,
            quota=quota,
            granted_models=granted_models,
            finish_turn=finish_turn,
            revoke=revoke,
            internal=internal,
            open_session=open_session,
            expire_session=expire_session,
            add_workflow_run=add_workflow_run,
            sessions=sessions,
        )
    finally:
        async with sessions() as cleanup:
            await cleanup.execute(
                update(InteractionTurn)
                .where(InteractionTurn.org_id == org_id)
                .values(workflow_run_id=None)
            )
            await cleanup.execute(delete(HermesSession).where(HermesSession.org_id == org_id))
            await cleanup.execute(delete(Organization).where(Organization.id == org_id))
            await cleanup.commit()
        await engine.dispose()


StreamChat = Callable[..., Any]


def scripted_stream(chunks: list[str], requests: list[dict[str, Any]]) -> StreamChat:
    async def fake_stream_chat(
        request: dict[str, Any],
        *,
        on_accepted: Callable[[], Awaitable[None]] | None = None,
    ):  # noqa: ANN202
        requests.append(request)
        if on_accepted is not None:
            await on_accepted()
        for chunk in chunks:
            yield chunk

    return fake_stream_chat
