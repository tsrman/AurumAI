"""Authentication for routes a Hermes Agent sandbox calls.

Only a bearer Hermes session token is accepted: no Keycloak token, no runner
session token and no cookie. The database session is admitted like a runner's:
applied identity, a live user row and the conversation generation fence.
"""

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials
from jwt.exceptions import ExpiredSignatureError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo, security
from src.core.config import settings
from src.core.database import async_session
from src.core.dependencies import _clear_org, fence_session_conversation, load_session_identity
from src.services.hermes_session_token import (
    InvalidHermesSessionToken,
    hermes_session_token_service,
)


def require_hermes_enabled() -> None:
    if not settings.HERMES_ENABLED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


def hermes_session_user(token: str) -> UserInfo:
    """Identity of a verified Hermes token; raises 401 otherwise."""
    try:
        payload = hermes_session_token_service.decode(token)
    except ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Hermes session token expired"
        ) from None
    except InvalidHermesSessionToken:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Hermes session token"
        ) from None
    return UserInfo(
        user_id=payload["sub"],
        org_id=payload["org_id"],
        role="member",
        raw_claims=payload,
    )


async def get_hermes_session_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> UserInfo:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token")
    return hermes_session_user(credentials.credentials)


@asynccontextmanager
async def hermes_db_session(user: UserInfo) -> AsyncIterator[AsyncSession]:
    async with async_session() as session:
        try:
            session.info["org_id"] = user.org_id
            await load_session_identity(user, session)
            await fence_session_conversation(user, session)
            yield session
        finally:
            await _clear_org(session)


async def get_hermes_db_session(
    user: UserInfo = Depends(get_hermes_session_user),
) -> AsyncGenerator[AsyncSession, None]:
    async with hermes_db_session(user) as session:
        yield session


def hermes_conversation_id(user: UserInfo) -> UUID:
    return UUID(str(user.raw_claims["conversation_id"]))
