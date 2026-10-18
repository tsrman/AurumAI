from datetime import datetime, timedelta, timezone
from typing import Any, cast

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.config import settings
from src.core.auth import UserInfo
from src.models.group import Group
from src.models.organization import Organization
from src.models.user import User
from src.models.user_group import UserGroup
from src.services.identity_claims import (
    SYNC_EXCLUDED_GROUP_PREFIXES as SYNC_EXCLUDED_GROUP_PREFIXES,
    normalize_groups,
)
from src.services.organization_provisioning import organization_provisioning_service


# `users.last_login_at` had no writer at all, so it read NULL for everyone.
# Browsers hold Keycloak tokens client-side and never call /auth/callback, so
# there is no login event to hang it on — the closest honest signal is this
# sync, which runs on every JWT-authenticated request (service sessions use
# get_service_db_session and never reach it). Coarsened so an active user
# costs at most one extra UPDATE per interval instead of one per request.
LAST_LOGIN_REFRESH_INTERVAL = timedelta(minutes=15)


class AuthService:
    async def exchange_code(
        self,
        code: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> dict[str, Any]:
        data = {
            "grant_type": "authorization_code",
            "client_id": settings.KEYCLOAK_CLIENT_ID,
            "client_secret": settings.KEYCLOAK_CLIENT_SECRET or "",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(settings.keycloak_token_url, data=data)
            resp.raise_for_status()
            return cast(dict[str, Any], resp.json())

    async def refresh_tokens(self, refresh_token: str) -> dict[str, Any]:
        data = {
            "grant_type": "refresh_token",
            "client_id": settings.KEYCLOAK_CLIENT_ID,
            "client_secret": settings.KEYCLOAK_CLIENT_SECRET or "",
            "refresh_token": refresh_token,
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(settings.keycloak_token_url, data=data)
            resp.raise_for_status()
            return cast(dict[str, Any], resp.json())

    async def _get_or_create_user(
        self,
        session: AsyncSession,
        user_info: UserInfo,
    ) -> User:
        result = await session.execute(select(User).where(User.keycloak_id == user_info.user_id))
        user = result.scalar_one_or_none()

        if user is None:
            organization_created = False
            org_result = await session.execute(
                select(Organization).where(Organization.id == user_info.org_id)
            )
            org = org_result.scalar_one_or_none()
            if org is None and user_info.org_id:
                # Two concurrent first-logins for the same brand-new org
                # both pass the SELECT above — a savepoint scopes the
                # IntegrityError (PK conflict) to just this insert instead
                # of the whole request, and the loser just re-selects what
                # the winner committed.
                try:
                    async with session.begin_nested():
                        org = Organization(
                            id=user_info.org_id,
                            name=user_info.org_name or "Default Organization",
                            slug=user_info.org_id.replace("-", ""),
                        )
                        session.add(org)
                        await session.flush()
                        organization_created = True
                except IntegrityError:
                    org_result = await session.execute(
                        select(Organization).where(Organization.id == user_info.org_id)
                    )
                    org = org_result.scalar_one()

            if organization_created:
                assert org is not None
                # Authorization v2 is default-deny, so an organization with no
                # rows at all is an organization nobody can use. Seed the
                # defaults once, here, where the org context is already this
                # organization — see services/organization_provisioning.py.
                await organization_provisioning_service.provision(session, org.id)

            # Same race for the user row itself (concurrent requests with
            # the same Keycloak sub, e.g. two tabs on first login).
            try:
                async with session.begin_nested():
                    user = User(
                        id=user_info.user_id,
                        keycloak_id=user_info.user_id,
                        email=user_info.email or "",
                        full_name=user_info.full_name,
                        org_id=user_info.org_id,
                        role=user_info.role,
                    )
                    session.add(user)
                    await session.flush()
            except IntegrityError:
                result = await session.execute(
                    select(User).where(User.keycloak_id == user_info.user_id)
                )
                user = result.scalar_one()

        user.full_name = user_info.full_name or user.full_name
        user.role = user_info.role
        return user

    async def sync_keycloak_user(
        self,
        session: AsyncSession,
        user_info: UserInfo,
        *,
        commit: bool = True,
    ) -> User:
        if commit:
            # A rollout rollback must not leave an old worker free to overwrite
            # this synchronous update. Worker-owned transactions pass commit=False.
            from src.services.identity_sync import sync_existing_identity_inline

            synchronized = await sync_existing_identity_inline(session, user_info, self)
            if synchronized is not None:
                return synchronized
        user = await self._get_or_create_user(session, user_info)
        seen_at = datetime.now(timezone.utc)
        if (
            user.last_login_at is None
            or seen_at - user.last_login_at >= LAST_LOGIN_REFRESH_INTERVAL
        ):
            user.last_login_at = seen_at

        # A certified complete mapper can omit the claim for zero groups;
        # uncertified omission must continue to preserve existing membership.
        names = normalize_groups(user_info.raw_claims)
        if names is not None:
            now = datetime.now(timezone.utc)

            existing: dict[str, Group] = {}
            if names:
                result = await session.execute(
                    select(Group).where(Group.org_id == user.org_id, Group.name.in_(names))
                )
                existing = {group.name: group for group in result.scalars().all()}
            for name in names - existing.keys():
                # Same concurrent-sync race as _get_or_create_user above —
                # uq_groups_org_id_name (2026_07_19_uq_groups_org_name)
                # turns a lost race into a catchable IntegrityError instead
                # of a duplicate Group row with its own, empty permission set.
                try:
                    async with session.begin_nested():
                        group = Group(org_id=user.org_id, name=name, last_synced_at=now)
                        session.add(group)
                        await session.flush()
                except IntegrityError:
                    result = await session.execute(
                        select(Group).where(Group.org_id == user.org_id, Group.name == name)
                    )
                    group = result.scalar_one()
                existing[name] = group
            for group in existing.values():
                group.last_synced_at = now
            await session.flush()
            desired_ids = {group.id for group in existing.values()}

            current = await session.execute(
                select(UserGroup).where(UserGroup.user_id == user.id)
            )
            for user_group in current.scalars().all():
                if user_group.group_id not in desired_ids:
                    await session.delete(user_group)
                else:
                    desired_ids.discard(user_group.group_id)
            for group_id in desired_ids:
                session.add(UserGroup(user_id=user.id, group_id=group_id))

        if not commit:
            await session.flush()
            return user
        await session.commit()
        # This commit+refresh is exactly the sequence that used to crash
        # with "Could not refresh instance": a pooled AsyncSession can get
        # handed a *different* physical connection right after commit(),
        # one that isn't running under this request's app.current_org_id
        # RLS context, so the refresh's SELECT sees zero rows. Fixed at the
        # session level, not here — see database.py's _AppSession/
        # after_begin, which re-issues the GUC on every new transaction
        # (including the one refresh() below triggers) regardless of which
        # connection it lands on. The caller must still put the org id on
        # session.info before calling this, not call set_config directly.
        await session.refresh(user)
        return user


auth_service = AuthService()
