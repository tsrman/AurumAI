"""Live checks that gate every Hermes Agent request (docs/HERMES_AGENT.md)."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.authorization_types import AuthorizationSubject
from src.core.capability_catalog import Capability
from src.models.interaction import InteractionTurn
from src.services.authorization import authorization_service

HERMES_CAPABILITIES = (Capability.CHAT_SEND, Capability.AGENTS_HERMES)


class HermesAccessDenied(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def require_hermes_capabilities(
    session: AsyncSession, subject: AuthorizationSubject
) -> None:
    for capability in HERMES_CAPABILITIES:
        if not await authorization_service.has_capability(session, subject, capability):
            raise HermesAccessDenied("hermes_access_denied")


async def active_hermes_turn(
    session: AsyncSession, subject: AuthorizationSubject, conversation_id: UUID
) -> InteractionTurn:
    """The conversation's one active turn, owned by the token's user."""
    turn = await session.scalar(
        select(InteractionTurn).where(
            InteractionTurn.org_id == subject.org_id,
            InteractionTurn.conversation_id == conversation_id,
            InteractionTurn.status == "active",
        )
    )
    if turn is None or turn.owner_id != subject.user_id:
        raise HermesAccessDenied("hermes_turn_inactive")
    return turn


async def hermes_node_model(session: AsyncSession, subject: AuthorizationSubject) -> str:
    """The model a Hermes Agent workflow node runs on: the organization's
    Hermes model, while the operator switch is on and the user holds both
    Hermes capabilities. Raises HermesAccessDenied otherwise."""
    from src.core.config import settings
    from src.services.hermes_egress import organization_hermes_model

    if not settings.HERMES_ENABLED:
        raise HermesAccessDenied("hermes_disabled")
    await require_hermes_capabilities(session, subject)
    model = await organization_hermes_model(session, subject.org_id)
    if not model:
        raise HermesAccessDenied("hermes_model_unset")
    return model
