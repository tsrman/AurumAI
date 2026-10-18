from uuid import uuid4

import pytest
from sqlalchemy import select

from src.core.auth import UserInfo
from src.models.group import Group
from src.models.user_group import UserGroup


def claims(org_id, user_id, groups, iat=100):
    raw = {"iat": iat, "secret": "must-not-persist"}
    if groups is not None:
        raw["groups"] = groups
    return UserInfo(user_id=str(user_id), org_id=str(org_id), email="a@example.com", raw_claims=raw)


@pytest.mark.asyncio
async def test_generation_revokes_groups_and_fences_sessions(db, test_org):
    from src.services.identity_sync import (
        submit_identity,
        apply_identity,
        require_applied_identity,
        IdentitySyncPending,
    )

    user_id = uuid4()
    first = await submit_identity(db, claims(test_org.id, user_id, ["finance"]))
    assert first == 1
    assert await submit_identity(db, claims(test_org.id, user_id, ["finance"])) == first
    await apply_identity(db, test_org.id, user_id, first)
    second = await submit_identity(db, claims(test_org.id, user_id, [], 101))
    with pytest.raises(IdentitySyncPending):
        await require_applied_identity(db, test_org.id, user_id)
    await apply_identity(db, test_org.id, user_id, first)
    with pytest.raises(IdentitySyncPending):
        await require_applied_identity(db, test_org.id, user_id)
    await apply_identity(db, test_org.id, user_id, second)
    rows = await db.scalars(select(Group.name).join(UserGroup).where(UserGroup.user_id == user_id))
    assert list(rows) == []


@pytest.mark.asyncio
async def test_stale_conflict_and_missing_groups(db, test_org):
    from src.services.identity_sync import submit_identity, apply_identity, IdentityClaimsConflict

    user_id = uuid4()
    await submit_identity(db, claims(test_org.id, user_id, ["finance"]))
    await apply_identity(db, test_org.id, user_id, 1)
    for iat in (99, 100):
        with pytest.raises(IdentityClaimsConflict):
            await submit_identity(db, claims(test_org.id, user_id, [], iat))
    generation = await submit_identity(db, claims(test_org.id, user_id, None, 101))
    await apply_identity(db, test_org.id, user_id, generation)
    rows = await db.scalars(select(Group.name).join(UserGroup).where(UserGroup.user_id == user_id))
    assert list(rows) == ["finance"]


@pytest.mark.asyncio
async def test_coalesced_login_refresh_does_not_block_security_admission(db, test_org):
    from datetime import timedelta
    from src.models.identity_sync import IdentitySyncState
    from src.services.identity_sync import submit_identity, apply_identity, require_applied_identity

    user_id = uuid4()
    await submit_identity(db, claims(test_org.id, user_id, []))
    await apply_identity(db, test_org.id, user_id, 1)
    state = await db.get(IdentitySyncState, (test_org.id, user_id))
    state.last_requested_at -= timedelta(minutes=16)
    await db.commit()
    assert await submit_identity(db, claims(test_org.id, user_id, [], 101)) == 2
    await require_applied_identity(db, test_org.id, user_id)


@pytest.mark.asyncio
async def test_wait_timeout_keeps_durable_inbox_and_safe_error(db, test_org, monkeypatch):
    from src.core.config import settings
    from src.models.identity_sync import IdentitySyncInbox
    from src.services.identity_sync import ensure_identity_synced, IdentitySyncPending

    monkeypatch.setattr(settings, "IDENTITY_SYNC_WAIT_SECONDS", 0.5)
    user_id = uuid4()
    org_id = test_org.id
    with pytest.raises(IdentitySyncPending) as error:
        await ensure_identity_synced(db, claims(test_org.id, user_id, []))
    assert error.value.detail == "identity_sync_pending"
    inbox = await db.get(IdentitySyncInbox, (org_id, user_id, 1))
    assert inbox is not None
    assert "secret" not in str(inbox.snapshot)


@pytest.mark.asyncio
async def test_admitted_jwt_uses_applied_database_role(db, test_org, monkeypatch):
    from src.core.config import settings
    from src.services.identity_sync import submit_identity, apply_identity, ensure_identity_synced
    from src.models.user import User

    monkeypatch.setattr(settings, "IDENTITY_SYNC_WAIT_SECONDS", 0.5)
    user_id = uuid4()
    user = claims(test_org.id, user_id, [])
    await submit_identity(db, user)
    await apply_identity(db, test_org.id, user_id, 1)
    persisted = await db.get(User, user_id)
    persisted.role = "org_admin"
    await db.commit()
    await ensure_identity_synced(db, user)
    assert user.role == "org_admin"


@pytest.mark.asyncio
async def test_app_role_first_provisioning_retry_and_rls(identity_database, monkeypatch):
    from src.core.config import settings
    from src.models.identity_sync import IdentitySyncInbox, IdentitySyncState
    from src.models.organization import Organization
    from src.models.organization_capability import OrganizationCapability
    from src.models.user import User
    from src.services.identity_sync import submit_identity, apply_identity

    monkeypatch.setattr(settings, "DEBUG", True)
    sessions, org_id, user_id = identity_database
    user = claims(org_id, user_id, ["finance"])
    user.email = f"{user_id}@example.com"
    async with sessions() as session:
        await submit_identity(session, user)
    async with sessions() as other_org:
        other_org.info["org_id"] = str(uuid4())
        assert await other_org.get(IdentitySyncInbox, (org_id, user_id, 1)) is None
        assert await other_org.get(IdentitySyncState, (org_id, user_id)) is None
    async with sessions() as worker:
        await apply_identity(worker, org_id, user_id, 1)
        assert await worker.get(Organization, org_id) is not None
        assert await worker.get(User, user_id) is not None
        capabilities = list(
            await worker.scalars(
                select(OrganizationCapability).where(
                    OrganizationCapability.org_id == org_id,
                )
            )
        )
        assert capabilities
        # Live platform grant removal remains authoritative on repeat login.
        for capability in capabilities:
            await worker.delete(capability)
        await worker.commit()
        await apply_identity(worker, org_id, user_id, 1)
        assert (
            list(
                await worker.scalars(
                    select(OrganizationCapability).where(
                        OrganizationCapability.org_id == org_id,
                    )
                )
            )
            == []
        )


@pytest.mark.asyncio
async def test_concurrent_requests_share_generation_and_newer_wins(identity_database):
    import asyncio
    from src.models.identity_sync import IdentitySyncState
    from src.services.identity_sync import submit_identity, IdentityClaimsConflict

    sessions, org_id, user_id = identity_database

    async def submit(groups, iat):
        async with sessions() as session:
            return await submit_identity(session, claims(org_id, user_id, groups, iat))

    assert await asyncio.gather(submit(["finance"], 100), submit(["finance"], 100)) == [1, 1]
    results = await asyncio.gather(
        submit([], 102), submit(["finance"], 101), return_exceptions=True
    )
    assert all(isinstance(result, (int, IdentityClaimsConflict)) for result in results)
    async with sessions() as session:
        session.info["org_id"] = str(org_id)
        state = await session.get(IdentitySyncState, (org_id, user_id))
        assert state.source_iat == 102


@pytest.mark.asyncio
async def test_pending_session_denied_without_synchronizing_claims(db, test_org, monkeypatch):
    from src.core.config import settings
    from src.core.dependencies import _set_org_and_sync
    from src.services.identity_sync import submit_identity, apply_identity, IdentitySyncPending

    monkeypatch.setattr(settings, "IDENTITY_SYNC_ENABLED", True)
    org_id, user_id = test_org.id, uuid4()
    await submit_identity(db, claims(org_id, user_id, ["finance"]))
    await apply_identity(db, org_id, user_id, 1)
    await submit_identity(db, claims(org_id, user_id, [], 101))
    session_user = UserInfo(
        user_id=str(user_id), org_id=str(org_id), raw_claims={"type": "session"}
    )
    with pytest.raises(IdentitySyncPending):
        await _set_org_and_sync(session_user, db)


@pytest.mark.asyncio
async def test_older_unchanged_token_preserves_generation_and_watermark(db, test_org):
    from src.models.identity_sync import IdentitySyncState
    from src.services.identity_sync import submit_identity

    user_id = uuid4()
    await submit_identity(db, claims(test_org.id, user_id, [], 101))
    assert await submit_identity(db, claims(test_org.id, user_id, [], 100)) == 1
    state = await db.get(IdentitySyncState, (test_org.id, user_id))
    assert state.source_iat == 101


@pytest.mark.asyncio
async def test_request_deadline_includes_generation_lock(identity_database, monkeypatch):
    import asyncio
    from src.core.config import settings
    from src.services.identity_sync import _lock, ensure_identity_synced, IdentitySyncPending

    sessions, org_id, user_id = identity_database
    monkeypatch.setattr(settings, "IDENTITY_SYNC_WAIT_SECONDS", 0.1)
    async with sessions() as lock_holder:
        await _lock(lock_holder, org_id, user_id)
        async with sessions() as request:
            started = asyncio.get_running_loop().time()
            with pytest.raises(IdentitySyncPending):
                await ensure_identity_synced(request, claims(org_id, user_id, []))
            assert asyncio.get_running_loop().time() - started < 1


@pytest.mark.asyncio
async def test_callback_withholds_cookies_until_applied(db, test_org, monkeypatch):
    from unittest.mock import AsyncMock
    from fastapi import Response
    from src.core.config import settings
    from src.core.keycloak import keycloak_client
    from src.routers.auth import auth_callback
    from src.schemas.auth import TokenCallbackRequest
    from src.services.auth_service import auth_service
    from src.services.identity_sync import apply_identity, IdentitySyncPending

    org_id, user_id = test_org.id, uuid4()
    monkeypatch.setattr(settings, "IDENTITY_SYNC_ENABLED", True)
    monkeypatch.setattr(settings, "IDENTITY_SYNC_WAIT_SECONDS", 0.5)
    monkeypatch.setattr(
        auth_service, "exchange_code", AsyncMock(return_value={"access_token": "token"})
    )
    monkeypatch.setattr(
        keycloak_client,
        "decode_token",
        AsyncMock(
            return_value={
                "sub": str(user_id),
                "org_id": str(org_id),
                "iat": 100,
                "groups": [],
                "email": f"{user_id}@example.com",
            }
        ),
    )
    request = TokenCallbackRequest(
        code="code", code_verifier="verifier", redirect_uri="http://test/cb"
    )
    response = Response()
    with pytest.raises(IdentitySyncPending):
        await auth_callback(request, response, db)
    assert "set-cookie" not in response.headers
    await apply_identity(db, org_id, user_id, 1)
    await auth_callback(request, response, db)
    assert "access_token=token" in response.headers["set-cookie"]


@pytest.mark.asyncio
async def test_missing_groups_does_not_erase_pending_revocation(db, test_org):
    from src.services.identity_sync import submit_identity, apply_identity

    user_id = uuid4()
    await submit_identity(db, claims(test_org.id, user_id, ["finance"], 100))
    await apply_identity(db, test_org.id, user_id, 1)
    await submit_identity(db, claims(test_org.id, user_id, [], 101))
    newest = await submit_identity(db, claims(test_org.id, user_id, None, 102))
    await apply_identity(db, test_org.id, user_id, newest)
    assert (
        list(
            await db.scalars(select(Group.name).join(UserGroup).where(UserGroup.user_id == user_id))
        )
        == []
    )


def test_enabled_api_engine_hides_snapshot_parameters():
    import os
    import subprocess
    import sys

    environment = {**os.environ, "APP_IDENTITY_SYNC_ENABLED": "true", "APP_DEBUG": "true"}
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from src.core.database import engine; assert engine.sync_engine.hide_parameters",
        ],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_disabled_legacy_sync_fences_previously_queued_generation(db, test_org, monkeypatch):
    from src.core.config import settings
    from src.core.dependencies import _set_org_and_sync
    from src.models.identity_sync import IdentitySyncInbox, IdentitySyncState
    from src.services.identity_sync import submit_identity, apply_identity, require_applied_identity

    monkeypatch.setattr(settings, "IDENTITY_SYNC_ENABLED", False)
    org_id, user_id = test_org.id, uuid4()
    await submit_identity(db, claims(org_id, user_id, ["finance"], 100))
    await apply_identity(db, org_id, user_id, 1)
    queued = await submit_identity(db, claims(org_id, user_id, [], 101))
    await _set_org_and_sync(claims(org_id, user_id, ["legal"], 102), db)
    await apply_identity(db, org_id, user_id, queued)
    names = list(
        await db.scalars(select(Group.name).join(UserGroup).where(UserGroup.user_id == user_id))
    )
    assert names == ["legal"]
    await require_applied_identity(db, org_id, user_id)
    state = await db.get(IdentitySyncState, (org_id, user_id))
    assert state.applied_generation == state.requested_generation > queued
    assert await db.get(IdentitySyncInbox, (org_id, user_id, queued)) is None


@pytest.mark.asyncio
async def test_disabled_session_still_denies_pending_security_generation(db, test_org, monkeypatch):
    from src.core.config import settings
    from src.core.dependencies import _set_org_and_sync
    from src.services.identity_sync import submit_identity, apply_identity, IdentitySyncPending

    monkeypatch.setattr(settings, "IDENTITY_SYNC_ENABLED", False)
    org_id, user_id = test_org.id, uuid4()
    await submit_identity(db, claims(org_id, user_id, ["finance"], 100))
    await apply_identity(db, org_id, user_id, 1)
    await submit_identity(db, claims(org_id, user_id, [], 101))
    session_user = UserInfo(
        user_id=str(user_id), org_id=str(org_id), raw_claims={"type": "session"}
    )
    with pytest.raises(IdentitySyncPending):
        await _set_org_and_sync(session_user, db)
    await _set_org_and_sync(claims(org_id, user_id, [], 102), db)
    await _set_org_and_sync(session_user, db)


@pytest.mark.asyncio
async def test_unchanged_legacy_identity_keeps_applied_generation(db, test_org):
    from src.models.identity_sync import IdentitySyncState
    from src.services.auth_service import auth_service
    from src.services.identity_sync import submit_identity, apply_identity

    org_id, user_id = test_org.id, uuid4()
    user = claims(org_id, user_id, [], 100)
    await submit_identity(db, user)
    await apply_identity(db, org_id, user_id, 1)
    await auth_service.sync_keycloak_user(db, user)
    state = await db.get(IdentitySyncState, (org_id, user_id))
    assert state.requested_generation == state.applied_generation == 1


@pytest.mark.asyncio
async def test_legacy_missing_groups_preserves_queued_revoke_and_rejects_stale_claims(db, test_org):
    from src.services.auth_service import auth_service
    from src.services.identity_sync import submit_identity, apply_identity, IdentityClaimsConflict

    org_id, user_id = test_org.id, uuid4()
    await submit_identity(db, claims(org_id, user_id, ["finance"], 100))
    await apply_identity(db, org_id, user_id, 1)
    await submit_identity(db, claims(org_id, user_id, [], 101))
    with pytest.raises(IdentityClaimsConflict):
        await auth_service.sync_keycloak_user(db, claims(org_id, user_id, ["finance"], 100))
    await auth_service.sync_keycloak_user(db, claims(org_id, user_id, None, 102))
    assert (
        list(
            await db.scalars(select(Group.name).join(UserGroup).where(UserGroup.user_id == user_id))
        )
        == []
    )
    await apply_identity(db, org_id, user_id, 2)
    assert (
        list(
            await db.scalars(select(Group.name).join(UserGroup).where(UserGroup.user_id == user_id))
        )
        == []
    )


@pytest.mark.asyncio
async def test_legacy_first_provisioning_without_iat_does_not_enroll_background_state(
    identity_database, monkeypatch
):
    from src.core.config import settings
    from src.models.identity_sync import IdentitySyncState
    from src.models.user import User
    from src.services.auth_service import auth_service

    sessions, org_id, user_id = identity_database
    monkeypatch.setattr(settings, "DEBUG", True)
    user = claims(org_id, user_id, [])
    user.raw_claims.pop("iat")
    user.email = f"{user_id}@example.com"
    async with sessions() as session:
        await auth_service.sync_keycloak_user(session, user)
        assert await session.get(User, user_id) is not None
        assert await session.get(IdentitySyncState, (org_id, user_id)) is None
