from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.authorization_types import (
    AccessPermission,
    PrincipalType,
    ResourceRef,
    ResourceType,
)
from src.models.access_grant import AccessGrant
from src.models.organization_settings import OrganizationSettings
from src.services.model_catalog import (
    GatewayCatalogSnapshot,
    GatewayCatalogStatus,
    is_chat_model,
)


class GatewayUnavailable(Exception):
    pass


class ModelNotFound(Exception):
    pass


class ModelNotOrganizationReadable(Exception):
    pass


@dataclass(frozen=True, slots=True)
class OrganizationModelEntry:
    id: str
    organization_readable: bool
    is_default: bool


@dataclass(frozen=True, slots=True)
class OrganizationModelView:
    gateway_status: GatewayCatalogStatus
    default_model: str | None
    default_available: bool
    default_temperature: float | None
    models: tuple[OrganizationModelEntry, ...]


class OrganizationModelSettingsService:
    @staticmethod
    async def _default_model(
        session: AsyncSession,
        org_id: UUID,
    ) -> str | None:
        return await session.scalar(
            select(OrganizationSettings.default_model).where(
                OrganizationSettings.org_id == org_id
            )
        )

    @staticmethod
    async def _organization_readable_ids(
        session: AsyncSession,
        org_id: UUID,
    ) -> set[str]:
        rows = await session.scalars(
            select(AccessGrant.resource_id).where(
                AccessGrant.org_id == org_id,
                AccessGrant.resource_type == ResourceType.MODEL.value,
                AccessGrant.principal_type == PrincipalType.ORGANIZATION.value,
                AccessGrant.principal_id == org_id,
                AccessGrant.permission.in_(
                    (AccessPermission.READ.value, AccessPermission.WRITE.value)
                ),
            )
        )
        return set(rows)

    @staticmethod
    def _chat_model_ids(snapshot: GatewayCatalogSnapshot) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for model in snapshot.models:
            model_id = model.get("id")
            if not isinstance(model_id, str) or not is_chat_model(model_id):
                continue
            try:
                ResourceRef(ResourceType.MODEL, model_id)
            except ValueError:
                continue
            seen.setdefault(model_id, None)
        return tuple(seen)

    async def inspect(
        self,
        session: AsyncSession,
        org_id: UUID,
        snapshot: GatewayCatalogSnapshot,
    ) -> OrganizationModelView:
        default_model = await self._default_model(session, org_id)
        default_temperature = await self.default_temperature(session, org_id)
        readable_ids = await self._organization_readable_ids(session, org_id)
        model_ids = self._chat_model_ids(snapshot)
        entries = tuple(
            OrganizationModelEntry(
                id=model_id,
                organization_readable=model_id in readable_ids,
                is_default=model_id == default_model,
            )
            for model_id in model_ids
        )
        return OrganizationModelView(
            gateway_status=snapshot.status,
            default_model=default_model,
            default_available=(
                default_model is not None
                and default_model in model_ids
                and default_model in readable_ids
            ),
            default_temperature=default_temperature,
            models=entries,
        )

    @staticmethod
    async def default_temperature(
        session: AsyncSession,
        org_id: UUID,
    ) -> float | None:
        return await session.scalar(
            select(OrganizationSettings.default_temperature).where(
                OrganizationSettings.org_id == org_id
            )
        )

    async def set_default_temperature(
        self,
        session: AsyncSession,
        org_id: UUID,
        temperature: float | None,
    ) -> None:
        statement = (
            insert(OrganizationSettings)
            .values(org_id=org_id, default_temperature=temperature)
            .on_conflict_do_update(
                index_elements=[OrganizationSettings.org_id],
                set_={"default_temperature": temperature},
            )
        )
        await session.execute(statement)

    async def set_default(
        self,
        session: AsyncSession,
        org_id: UUID,
        model_id: str | None,
        snapshot: GatewayCatalogSnapshot | None,
    ) -> None:
        if model_id is not None:
            ResourceRef(ResourceType.MODEL, model_id)
            if snapshot is None or snapshot.status is GatewayCatalogStatus.UNAVAILABLE:
                raise GatewayUnavailable
            if model_id not in self._chat_model_ids(snapshot):
                raise ModelNotFound
            if model_id not in await self._organization_readable_ids(session, org_id):
                raise ModelNotOrganizationReadable

        statement = (
            insert(OrganizationSettings)
            .values(org_id=org_id, default_model=model_id)
            .on_conflict_do_update(
                index_elements=[OrganizationSettings.org_id],
                set_={"default_model": model_id},
            )
        )
        await session.execute(statement)

    @staticmethod
    def catalog_model_ids(snapshot: GatewayCatalogSnapshot) -> tuple[str, ...]:
        """Every model id the gateway publishes; image models are not chat models."""
        seen: dict[str, None] = {}
        for model in snapshot.models:
            model_id = model.get("id")
            if isinstance(model_id, str):
                try:
                    ResourceRef(ResourceType.MODEL, model_id)
                except ValueError:
                    continue
                seen.setdefault(model_id, None)
        return tuple(seen)

    @staticmethod
    async def image_models(
        session: AsyncSession, org_id: UUID
    ) -> tuple[str | None, str | None]:
        row = (
            await session.execute(
                select(
                    OrganizationSettings.image_generation_model,
                    OrganizationSettings.image_edit_model,
                ).where(OrganizationSettings.org_id == org_id)
            )
        ).one_or_none()
        return (row[0], row[1]) if row is not None else (None, None)

    async def set_image_models(
        self,
        session: AsyncSession,
        org_id: UUID,
        generation_model: str | None,
        edit_model: str | None,
        snapshot: GatewayCatalogSnapshot | None,
    ) -> None:
        chosen = [model for model in (generation_model, edit_model) if model is not None]
        if chosen:
            if snapshot is None or snapshot.status is GatewayCatalogStatus.UNAVAILABLE:
                raise GatewayUnavailable
            published = set(self.catalog_model_ids(snapshot))
            if any(model not in published for model in chosen):
                raise ModelNotFound
        values = {"image_generation_model": generation_model, "image_edit_model": edit_model}
        await session.execute(
            insert(OrganizationSettings)
            .values(org_id=org_id, **values)
            .on_conflict_do_update(index_elements=[OrganizationSettings.org_id], set_=values)
        )

    @staticmethod
    async def vision_models(session: AsyncSession, org_id: UUID) -> tuple[str, ...]:
        models = await session.scalar(
            select(OrganizationSettings.vision_models).where(
                OrganizationSettings.org_id == org_id
            )
        )
        return tuple(models or ())

    async def set_vision_models(
        self,
        session: AsyncSession,
        org_id: UUID,
        models: list[str],
        snapshot: GatewayCatalogSnapshot | None,
    ) -> None:
        chosen = list(dict.fromkeys(models))
        if chosen:
            if snapshot is None or snapshot.status is GatewayCatalogStatus.UNAVAILABLE:
                raise GatewayUnavailable
            published = set(self._chat_model_ids(snapshot))
            if any(model not in published for model in chosen):
                raise ModelNotFound
        await session.execute(
            insert(OrganizationSettings)
            .values(org_id=org_id, vision_models=chosen)
            .on_conflict_do_update(
                index_elements=[OrganizationSettings.org_id],
                set_={"vision_models": chosen},
            )
        )

    @classmethod
    def chat_model_ids(cls, snapshot: GatewayCatalogSnapshot) -> tuple[str, ...]:
        return cls._chat_model_ids(snapshot)


organization_model_settings_service = OrganizationModelSettingsService()
