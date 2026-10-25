import logging
from typing import AsyncGenerator
from uuid import UUID

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import get_current_user, get_current_user_or_session, UserInfo
from src.core.database import async_session
from src.core.config import settings
from src.services.identity_sync import ensure_identity_synced, ensure_session_identity_ready
from src.models.user import User
from src.services.auth_service import auth_service

logger = logging.getLogger(__name__)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        yield session


async def load_session_identity(user: UserInfo, session: AsyncSession) -> None:
    """Admit a service-token identity: applied identity sync and a live user row."""
    try:
        user_id = UUID(user.user_id)
        org_id = UUID(user.org_id or "")
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid session identity",
        ) from exc
    # Rollback disables new background work, not existing security fences.
    await ensure_session_identity_ready(session, user)
    result = await session.execute(
        select(User).where(User.id == user_id, User.org_id == org_id)
    )
    current_user = result.scalar_one_or_none()
    if current_user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid session identity",
        )
    user.role = current_user.role or "member"


async def fence_session_conversation(user: UserInfo, session: AsyncSession) -> None:
    """Hold the token's conversation generation for the rest of the request."""
    from src.services.conversation_lifecycle import lock_active_conversation
    generation = user.raw_claims.get("generation", 1)
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise HTTPException(401, "Invalid session generation")
    org, conversation = UUID(user.org_id or ""), UUID(user.raw_claims["conversation_id"])
    await lock_active_conversation(session, org, conversation, generation=generation)
    session.info["conversation_fence"] = (org, conversation, generation)
    # Admission is a short transaction. Later database work reacquires
    # this original generation through _AppSession.after_begin.
    await session.commit()


async def _set_org_and_sync(user: UserInfo, session: AsyncSession) -> None:
    # Setting session.info here (a plain dict, no I/O) rather than calling
    # set_config directly is what makes this survive a mid-request
    # connection-pool swap — see database.py's _AppSession/after_begin for
    # why a manual set_config re-issued only here used to still crash
    # inside sync_keycloak_user's own internal commit+refresh.
    if user.org_id:
        session.info["org_id"] = user.org_id
    if user.raw_claims.get("type") == "session":
        await load_session_identity(user, session)
        return
    if settings.IDENTITY_SYNC_ENABLED:
        await ensure_identity_synced(session, user)
    else:
        await auth_service.sync_keycloak_user(session, user)
        await session.commit()


async def _clear_org(session: AsyncSession) -> None:
    # Roll back any uncommitted business changes so a failed request cannot be partially persisted.
    # No explicit app.current_org_id clearing needed anymore: after_begin
    # sets it via SET LOCAL (transaction-scoped), which Postgres itself
    # resets at the next commit/rollback — it cannot leak into whatever
    # request reuses this pooled connection next, unlike the old
    # session-wide set_config(..., false) this replaced.
    try:
        await session.rollback()
    except Exception:
        logger.warning("session rollback failed during org cleanup", exc_info=True)


def get_current_org_id(
    user: UserInfo = Depends(get_current_user),
) -> UUID:
    try:
        return UUID(user.org_id or "")
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing or invalid organization context",
        ) from exc


async def get_org_db_session(
    user: UserInfo = Depends(get_current_user),
) -> AsyncGenerator[AsyncSession, None]:
    # Validate before even constructing the org-scoped session. Otherwise a
    # malformed Keycloak claim can reach auth sync/RLS SQL and surface as 500.
    get_current_org_id(user)
    async with async_session() as session:
        try:
            await _set_org_and_sync(user, session)
            yield session
        finally:
            await _clear_org(session)


async def get_service_db_session(
    user: UserInfo = Depends(get_current_user_or_session),
) -> AsyncGenerator[AsyncSession, None]:
    """Org-scoped session that also accepts runner session tokens."""
    async with async_session() as session:
        try:
            await _set_org_and_sync(user, session)
            if user.raw_claims.get("type") == "session":
                await fence_session_conversation(user, session)
            yield session
        finally:
            await _clear_org(session)
