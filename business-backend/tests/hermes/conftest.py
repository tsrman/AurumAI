"""A committed organization with a user, a conversation and an active turn.

Hermes routes open their own database sessions, so the fixture commits real
rows through a dedicated engine and removes the organization afterwards.
"""

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from jwt.exceptions import PyJWTError
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.core import dependencies, hermes_auth
from src.core.capability_catalog import Capability
from src.core.config import settings
from src.middleware.error_handler import setup_exception_handlers
from src.models.conversation import Conversation
from src.models.interaction import InteractionTurn
from src.models.organization import Organization
from src.models.organization_capability import OrganizationCapability
from src.models.user import User
from src.routers import hermes, hermes_internal
from src.schemas.interaction import TurnLimits
from src.services.quota import UsageReservation, get_quota_service

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
        )
    finally:
        async with sessions() as cleanup:
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
