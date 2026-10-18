import uuid
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.authorization_types import (
    AccessPermission,
    GrantSource,
    PrincipalType,
    ResourceType,
)
from src.models.access_grant import AccessGrant
from src.models.skill import Skill


class SkillGrantProvisioningService:
    async def provision_organization(
        self,
        session: AsyncSession,
        org_id: UUID,
    ) -> tuple[UUID, ...]:
        """Grant one organization read on every global skill.

        Runs inside the caller's transaction and writes only rows carrying the
        caller's own `org_id`, so it is satisfied by the `access_grants` RLS
        policy as-is. Global skills stay visible to the SELECT because the
        `skills` policy is `org_id IS NULL OR org_id = current_setting(...)`.
        """
        global_skill_ids = tuple(
            (
                await session.execute(select(Skill.id).where(Skill.org_id.is_(None)))
            ).scalars()
        )
        await self._insert_organization_grants(session, org_id, global_skill_ids)
        return global_skill_ids

    async def publish_global_skill(
        self,
        session: AsyncSession,
        skill: Skill,
        *,
        organization_ids: Sequence[UUID],
    ) -> tuple[UUID, ...]:
        """Fan one global skill out to an explicitly named set of organizations.

        Operator path — `src/cli/provision_org.py` is the supported caller. The
        organization list is a parameter rather than a query on purpose: under
        the runtime role RLS shows `organizations` only the current tenant, so
        enumerating every organization needs a role that can see across tenants
        and is therefore the operator's job, not this function's.

        `access_grants` is FORCE RLS with `WITH CHECK (org_id = current_setting
        ('app.current_org_id'))`, so writing a row for a different organization
        requires switching that setting first. It is restored afterwards: the
        caller's transaction may well continue doing org-scoped work.
        """
        if skill.org_id is not None:
            raise ValueError("only global skills can be published to an organization")
        previous = (
            await session.execute(
                text("SELECT current_setting('app.current_org_id', true)")
            )
        ).scalar_one_or_none()
        published: list[UUID] = []
        try:
            for org_id in organization_ids:
                await self._set_org_context(session, str(org_id))
                await self._insert_organization_grants(session, org_id, (skill.id,))
                published.append(org_id)
        finally:
            await self._set_org_context(session, previous)
        return tuple(published)

    @staticmethod
    async def _set_org_context(session: AsyncSession, org_id: str | None) -> None:
        # `is_local=true` — the change dies with the transaction, exactly like
        # the SET LOCAL that database.py's after_begin hook issues, so it can
        # never leak into whatever request reuses this pooled connection next.
        await session.execute(
            text("SELECT set_config('app.current_org_id', :org_id, true)"),
            {"org_id": org_id or ""},
        )

    @staticmethod
    async def _insert_organization_grants(
        session: AsyncSession,
        org_id: UUID,
        skill_ids: Sequence[UUID],
    ) -> None:
        if not skill_ids:
            return
        await session.execute(
            insert(AccessGrant)
            .values(
                [
                    {
                        "id": uuid.uuid4(),
                        "org_id": org_id,
                        "resource_type": ResourceType.SKILL.value,
                        "resource_id": str(skill_id),
                        "principal_type": PrincipalType.ORGANIZATION.value,
                        "principal_id": org_id,
                        "permission": AccessPermission.READ.value,
                        "created_by": None,
                        "created_via": GrantSource.PROVISIONING.value,
                    }
                    for skill_id in skill_ids
                ]
            )
            .on_conflict_do_nothing(
                constraint="uq_access_grants_resource_principal_permission"
            )
        )


skill_grant_provisioning_service = SkillGrantProvisioningService()
