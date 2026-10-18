from uuid import UUID

from sqlalchemy import String, cast, delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.authorization_types import (
    AccessPermission,
    AuthorizationSubject,
    GrantSource,
    PrincipalType,
    ResourceType,
)
from src.models.access_grant import AccessGrant
from src.models.knowledge_base import KnowledgeBase
from src.services.authorization import authorization_service


class KnowledgeBaseRepository:
    async def lock_for_write(
        self, session: AsyncSession, resource_id: UUID
    ) -> None:
        """Serialize cross-database document writes and base deletion.

        Ordinary KB writes keep this lock across RAG. Conversation attachment
        writes commit durable intent before external I/O; deletion must also
        check those intents under the owner conversation lock before proceeding.
        """
        lock_key = int.from_bytes(resource_id.bytes[:8], "big", signed=True)
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": lock_key},
        )

    async def create_with_owner_write_grant(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        *,
        name: str,
        description: str | None,
    ) -> KnowledgeBase:
        knowledge_base = KnowledgeBase(
            org_id=subject.org_id,
            created_by=subject.user_id,
            name=name,
            description=description,
        )
        session.add(knowledge_base)
        await session.flush()
        session.add(
            AccessGrant(
                org_id=subject.org_id,
                resource_type=ResourceType.KNOWLEDGE_BASE.value,
                resource_id=str(knowledge_base.id),
                principal_type=PrincipalType.USER.value,
                principal_id=subject.user_id,
                permission=AccessPermission.WRITE.value,
                created_via=GrantSource.PROVISIONING.value,
            )
        )
        await session.commit()
        await session.refresh(knowledge_base)
        return knowledge_base

    async def list_by_org(
        self,
        session: AsyncSession,
        org_id: object,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[KnowledgeBase]:
        result = await session.execute(
            select(KnowledgeBase)
            .where(KnowledgeBase.org_id == org_id)
            .order_by(KnowledgeBase.created_at.desc(), KnowledgeBase.id)
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars())

    async def list_accessible(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[KnowledgeBase]:
        access = authorization_service.accessible_resource_predicate(
            subject,
            ResourceType.KNOWLEDGE_BASE,
            AccessPermission.READ,
            cast(KnowledgeBase.id, String),
        )
        result = await session.execute(
            select(KnowledgeBase)
            .where(
                KnowledgeBase.org_id == subject.org_id,
                # Hidden per-conversation knowledge bases (chat attachments)
                # never appear in this listing — the owning conversation's
                # chat UI is the only way to reach one.
                KnowledgeBase.owning_conversation_id.is_(None),
                access,
            )
            .order_by(KnowledgeBase.created_at.desc(), KnowledgeBase.id)
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars())

    async def get_by_id(
        self,
        session: AsyncSession,
        org_id: object,
        resource_id: object,
    ) -> KnowledgeBase | None:
        result = await session.execute(
            select(KnowledgeBase).where(
                KnowledgeBase.id == resource_id,
                KnowledgeBase.org_id == org_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_accessible(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        resource_id: object,
        permission: AccessPermission = AccessPermission.READ,
    ) -> KnowledgeBase | None:
        access = authorization_service.accessible_resource_predicate(
            subject,
            ResourceType.KNOWLEDGE_BASE,
            permission,
            cast(KnowledgeBase.id, String),
        )
        result = await session.execute(
            select(KnowledgeBase).where(
                KnowledgeBase.id == resource_id,
                KnowledgeBase.org_id == subject.org_id,
                access,
            )
        )
        return result.scalar_one_or_none()

    async def delete_with_grants(
        self,
        session: AsyncSession,
        knowledge_base: KnowledgeBase,
    ) -> None:
        await session.execute(
            delete(AccessGrant).where(
                AccessGrant.org_id == knowledge_base.org_id,
                AccessGrant.resource_type == ResourceType.KNOWLEDGE_BASE.value,
                AccessGrant.resource_id == str(knowledge_base.id),
            )
        )
        await session.delete(knowledge_base)
        await session.commit()


knowledge_base_repository = KnowledgeBaseRepository()
