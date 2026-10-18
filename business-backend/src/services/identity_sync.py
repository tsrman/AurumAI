"""Database-only admission. No Temporal connection is needed by either API plane."""

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo
from src.core.config import settings
from src.services.identity_claims import normalize_groups
from src.models.user import User
from src.models.identity_sync import IdentitySyncInbox, IdentitySyncState
from src.services.auth_service import (
    AuthService,
    LAST_LOGIN_REFRESH_INTERVAL,
    auth_service,
)


class IdentitySyncPending(HTTPException):
    def __init__(self) -> None:
        super().__init__(503, detail="identity_sync_pending", headers={"Retry-After": "1"})


class IdentityClaimsConflict(HTTPException):
    def __init__(self) -> None:
        super().__init__(401, detail="identity_claims_refresh_required")


def normalize_identity(user: UserInfo) -> tuple[dict[str, object], str, int]:
    """Copy only allowlisted bounded fields from an already validated JWT."""
    try:
        org_id, user_id = str(UUID(user.org_id or "")), str(UUID(user.user_id))
    except ValueError as exc:
        raise HTTPException(400, detail="Invalid identity context") from exc
    iat = user.raw_claims.get("iat")
    if type(iat) is not int or not 0 <= iat <= 2**63 - 1:
        raise IdentityClaimsConflict()
    snapshot: dict[str, object] = {"org_id": org_id, "user_id": user_id}
    for key, value, limit in (
        ("email", user.email, 255),
        ("full_name", user.full_name, 255),
        ("org_name", user.org_name, 255),
        ("role", user.role, 20),
    ):
        if value is not None and len(value) > limit:
            raise HTTPException(400, detail="Invalid identity profile")
        snapshot[key] = value
    groups = normalize_groups(user.raw_claims)
    if groups is not None:
        snapshot["groups"] = sorted(groups)
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode()) > 60000:
        raise HTTPException(400, detail="Identity snapshot too large")
    return snapshot, hashlib.sha256(encoded.encode()).hexdigest(), iat


async def _lock(session: AsyncSession, org_id: UUID, user_id: UUID) -> None:
    session.info["org_id"] = str(org_id)
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:identity, 0))"),
        {"identity": f"identity:{org_id}:{user_id}"},
    )


async def _state(session: AsyncSession, org_id: UUID, user_id: UUID) -> IdentitySyncState | None:
    result = await session.execute(
        select(IdentitySyncState)
        .where(IdentitySyncState.org_id == org_id, IdentitySyncState.user_id == user_id)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def submit_identity(session: AsyncSession, user: UserInfo) -> int:
    snapshot, digest, iat = normalize_identity(user)
    org_id, user_id = UUID(user.org_id or ""), UUID(user.user_id)
    await _lock(session, org_id, user_id)
    state = await _state(session, org_id, user_id)
    now = datetime.now(timezone.utc)
    if state is not None:
        if iat <= state.source_iat and digest != state.claims_digest:
            raise IdentityClaimsConflict()
        if digest == state.claims_digest:
            # Advance the high-water mark even when claims themselves are unchanged.
            state.source_iat = max(iat, state.source_iat)
            if now - state.last_requested_at < LAST_LOGIN_REFRESH_INTERVAL:
                await session.commit()
                return state.requested_generation
        if "groups" not in snapshot:
            pending = await session.get(
                IdentitySyncInbox, (org_id, user_id, state.requested_generation)
            )
            if pending is not None and "groups" in pending.snapshot:
                # Missing mapper data cannot cancel an already admitted revocation.
                # Carry its effective membership forward while retaining the digest
                # of the actual incoming claims for token conflict detection.
                snapshot["groups"] = pending.snapshot["groups"]
        state.requested_generation += 1
        if digest != state.claims_digest:
            state.security_generation = state.requested_generation
        state.claims_digest = digest
        state.source_iat = max(iat, state.source_iat)
        state.status = "pending"
        state.last_requested_at = now
    else:
        state = IdentitySyncState(
            org_id=org_id,
            user_id=user_id,
            requested_generation=1,
            applied_generation=0,
            claims_digest=digest,
            source_iat=iat,
            status="pending",
            last_requested_at=now,
            security_generation=1,
        )
        session.add(state)
    # Only the latest snapshot can be applied. Obsolete workflows safely become no-ops.
    await session.execute(
        delete(IdentitySyncInbox).where(
            IdentitySyncInbox.org_id == org_id,
            IdentitySyncInbox.user_id == user_id,
        )
    )
    session.add(
        IdentitySyncInbox(
            org_id=org_id,
            user_id=user_id,
            generation=state.requested_generation,
            snapshot=snapshot,
        )
    )
    await session.commit()
    return state.requested_generation


async def sync_existing_identity_inline(
    session: AsyncSession, user: UserInfo, service: AuthService
) -> User | None:
    """Fence queued work when synchronous auth takes over an existing identity.

    Identities never enrolled into background sync keep legacy claim validation,
    provisioning and commit behavior. The shared lock also serializes a first
    background admission racing with an in-flight legacy request during rollout.
    """
    try:
        org_id, user_id = UUID(user.org_id or ""), UUID(user.user_id)
    except ValueError:
        return None
    await _lock(session, org_id, user_id)
    state = await _state(session, org_id, user_id)
    if state is None:
        return None
    snapshot, digest, iat = normalize_identity(user)
    if iat <= state.source_iat and digest != state.claims_digest:
        raise IdentityClaimsConflict()
    if "groups" not in snapshot:
        pending = await session.get(
            IdentitySyncInbox, (org_id, user_id, state.requested_generation)
        )
        if pending is not None and "groups" in pending.snapshot:
            snapshot["groups"] = pending.snapshot["groups"]
    synchronized = await service.sync_keycloak_user(
        session, _user_from_snapshot(snapshot), commit=False
    )
    if state.applied_generation < state.requested_generation or digest != state.claims_digest:
        state.requested_generation += 1
        state.applied_generation = state.requested_generation
        state.security_generation = state.requested_generation
        state.claims_digest = digest
        state.status = "applied"
        await session.execute(
            delete(IdentitySyncInbox).where(
                IdentitySyncInbox.org_id == org_id,
                IdentitySyncInbox.user_id == user_id,
            )
        )
    state.source_iat = max(iat, state.source_iat)
    now = datetime.now(timezone.utc)
    if now - state.last_requested_at >= LAST_LOGIN_REFRESH_INTERVAL:
        state.last_requested_at = now
    # User/group changes, fencing and inbox removal share this exact commit.
    await session.commit()
    await session.refresh(synchronized)
    return synchronized


def _user_from_snapshot(snapshot: dict[str, object]) -> UserInfo:
    payload = dict(snapshot)
    groups = payload.pop("groups", None)
    raw_claims = {"groups": groups} if "groups" in snapshot else {}
    return UserInfo.model_validate({**payload, "raw_claims": raw_claims})


async def require_applied_identity(session: AsyncSession, org_id: UUID, user_id: UUID) -> None:
    state = await _state(session, org_id, user_id)
    if state is not None and state.applied_generation < state.security_generation:
        raise IdentitySyncPending()


async def ensure_session_identity_ready(session: AsyncSession, user: UserInfo) -> None:
    await require_applied_identity(session, UUID(user.org_id or ""), UUID(user.user_id))


async def ensure_identity_synced(session: AsyncSession, user: UserInfo) -> None:
    try:
        async with asyncio.timeout(settings.IDENTITY_SYNC_WAIT_SECONDS):
            await _wait_for_identity(session, user)
    except TimeoutError:
        await session.rollback()
        raise IdentitySyncPending() from None


async def _wait_for_identity(session: AsyncSession, user: UserInfo) -> None:
    await submit_identity(session, user)
    while True:
        try:
            await require_applied_identity(session, UUID(user.org_id or ""), UUID(user.user_id))
            current_user = await session.get(User, UUID(user.user_id), populate_existing=True)
            if current_user is None:
                raise IdentitySyncPending()
            user.role = current_user.role or "member"
            return
        except IdentitySyncPending:
            await session.rollback()
            await asyncio.sleep(0.1)


async def apply_identity(
    session: AsyncSession, org_id: UUID, user_id: UUID, generation: int
) -> None:
    await _lock(session, org_id, user_id)
    state = await _state(session, org_id, user_id)
    if (
        state is None
        or state.requested_generation != generation
        or state.applied_generation >= generation
    ):
        await session.commit()
        return
    inbox = await session.get(IdentitySyncInbox, (org_id, user_id, generation))
    if inbox is None:
        raise RuntimeError("identity_snapshot_unavailable")
    user = _user_from_snapshot(inbox.snapshot)
    await auth_service.sync_keycloak_user(session, user, commit=False)
    state.applied_generation = generation
    state.status = "applied"
    await session.delete(inbox)
    await session.commit()
