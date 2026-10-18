"""What a brand-new organization is given so it can be used at all.

Authorization v2 is default-deny: no capability row means no action, no grant
row means no resource. That is the right invariant, but it also means a freshly
created organization starts unusable — its members get 403 on /v1/chat/stream
and an empty GET /v1/models. This module is the one place that decides what the
platform hands an organization on day one, so the answer is reviewable in a
single file instead of being implied by the absence of code.

It is idempotent and additive: every write is ON CONFLICT DO NOTHING, and
nothing here removes a capability or a grant an admin has since changed. Run it
twice and the second run is a no-op.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.authorization_types import (
    AccessPermission,
    GrantSource,
    PrincipalType,
    ResourceType,
)
from src.core.capability_catalog import Capability
from src.core.logging import logger
from src.models.access_grant import AccessGrant
from src.models.organization_capability import OrganizationCapability
from src.services.model_catalog import ModelCatalog, chat_model_ids, provisioning_models
from src.services.skill_grant_provisioning import (
    SkillGrantProvisioningService,
    skill_grant_provisioning_service,
)


# Deliberately the smallest set that makes the product work: sending a message.
# Everything else — creating skills, knowledge bases, agents, managing sharing —
# stays off until an org_admin turns it on, because each is a way for a member to
# add something other members then see.
#
# This mirrors the legacy-to-v2 translation table in the design doc, where the
# only capability an ordinary member held (`chat:write`) maps to `chat.send`.
DEFAULT_ORGANIZATION_CAPABILITIES: tuple[Capability, ...] = (Capability.CHAT_SEND,)


@dataclass(frozen=True, slots=True)
class ProvisioningResult:
    org_id: UUID
    capabilities: tuple[Capability, ...]
    skill_ids: tuple[UUID, ...]
    model_ids: tuple[str, ...]

    @property
    def is_empty(self) -> bool:
        return not (self.capabilities or self.skill_ids or self.model_ids)


class OrganizationProvisioningService:
    def __init__(
        self,
        *,
        skills: SkillGrantProvisioningService = skill_grant_provisioning_service,
        model_catalog: ModelCatalog = provisioning_models,
        default_capabilities: Sequence[Capability] = DEFAULT_ORGANIZATION_CAPABILITIES,
    ) -> None:
        self._skills = skills
        self._model_catalog = model_catalog
        self._default_capabilities = tuple(default_capabilities)

    async def plan(self, org_id: UUID) -> ProvisioningResult:
        """What `provision` would write, without writing it. Drives the CLI's
        dry-run; deliberately does not read existing rows, because every write
        is a no-op on conflict anyway."""
        return ProvisioningResult(
            org_id=org_id,
            capabilities=self._default_capabilities,
            skill_ids=(),
            model_ids=await chat_model_ids(self._model_catalog),
        )

    async def provision(
        self,
        session: AsyncSession,
        org_id: UUID,
    ) -> ProvisioningResult:
        """Seed the organization's defaults inside the caller's transaction.

        Every row written carries this `org_id`, so the whole thing runs under
        the RLS-constrained runtime role with the org context already set — no
        elevated database role anywhere on this path.
        """
        capabilities = await self._seed_capabilities(session, org_id)
        skill_ids = await self._skills.provision_organization(session, org_id)
        model_ids = await self._seed_model_grants(session, org_id)
        result = ProvisioningResult(
            org_id=org_id,
            capabilities=capabilities,
            skill_ids=skill_ids,
            model_ids=model_ids,
        )
        logger.info(
            "organization_provisioned",
            org_id=str(org_id),
            capabilities=[capability.value for capability in capabilities],
            global_skills=len(skill_ids),
            models=len(model_ids),
        )
        return result

    async def _seed_capabilities(
        self,
        session: AsyncSession,
        org_id: UUID,
    ) -> tuple[Capability, ...]:
        if not self._default_capabilities:
            return ()
        await session.execute(
            insert(OrganizationCapability)
            .values(
                [
                    {"org_id": org_id, "capability": capability.value}
                    for capability in self._default_capabilities
                ]
            )
            .on_conflict_do_nothing(index_elements=["org_id", "capability"])
        )
        return self._default_capabilities

    async def _seed_model_grants(
        self,
        session: AsyncSession,
        org_id: UUID,
    ) -> tuple[str, ...]:
        model_ids = await chat_model_ids(self._model_catalog)
        if not model_ids:
            # The catalog is fail-closed (services/model_catalog.py): an
            # unreachable gateway yields nothing rather than a guessed list, so
            # this is the difference between "no models exist" and "we could not
            # ask". Either way, inventing model ids here would grant access to
            # models that may not exist — say so instead and let the operator
            # re-run the CLI once the gateway is back.
            logger.warning(
                "organization_provisioned_without_model_grants",
                org_id=str(org_id),
            )
            return ()
        await session.execute(
            insert(AccessGrant)
            .values(
                [
                    {
                        "id": uuid.uuid4(),
                        "org_id": org_id,
                        "resource_type": ResourceType.MODEL.value,
                        "resource_id": model_id,
                        "principal_type": PrincipalType.ORGANIZATION.value,
                        "principal_id": org_id,
                        "permission": AccessPermission.READ.value,
                        "created_by": None,
                        "created_via": GrantSource.PROVISIONING.value,
                    }
                    for model_id in model_ids
                ]
            )
            .on_conflict_do_nothing(
                constraint="uq_access_grants_resource_principal_permission"
            )
        )
        return model_ids


organization_provisioning_service = OrganizationProvisioningService()
