import uuid
from typing import Any

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.authorization_types import AccessPermission, GrantSource, PrincipalType, ResourceType
from src.models.access_grant import AccessGrant
from src.models.conversation import Conversation
from src.models.knowledge_base import KnowledgeBase
from src.models.skill import Skill
from src.services.document_upload import store_and_index_document

_HYBRID_SEARCH_ACTIVITY = "execute_hybrid_search"


async def _get_or_create_conversation_knowledge_base(
    session: AsyncSession, conversation: Conversation
) -> KnowledgeBase:
    """Idempotent: a conversation gets at most one hidden knowledge base,
    shared by every file attached to it. `owning_conversation_id` is unique,
    so a lookup-then-create race would 500 on the second writer rather than
    silently duplicate — attachments are single-user, single-conversation
    actions, so that race is not a realistic concern here.
    """
    existing = (
        await session.execute(
            select(KnowledgeBase).where(
                KnowledgeBase.owning_conversation_id == conversation.id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    knowledge_base = KnowledgeBase(
        org_id=conversation.org_id,
        # Never shown in any UI (excluded from every list query) — the name
        # only needs to satisfy the org+name uniqueness constraint.
        name=f"__conversation_attachments__{conversation.id}",
        owning_conversation_id=conversation.id,
    )
    session.add(knowledge_base)
    await session.flush()

    # id generated up front (not left to the ORM's flush-time default) so it
    # can double as `name` — observed live: given both `id=<uuid>` and a
    # human-ish `name` in the tools system message, the model emitted the
    # *name* as the tool_call's skill_id instead of the id right next to it,
    # and dispatch 404ed. Making them identical means either field the model
    # picks resolves to the same, real skill — no dispatch-logic change
    # needed (conversation.py's execute_skill lookup is by id only).
    skill_id = uuid.uuid4()
    skill = Skill(
        id=skill_id,
        org_id=conversation.org_id,
        name=str(skill_id),
        description=(
            "Search the file(s) attached to this conversation by the user. "
            f'Always call with knowledge_base_id="{knowledge_base.id}" exactly.'
        ),
        type="tool",
        activity_name=_HYBRID_SEARCH_ACTIVITY,
        input_schema={
            "type": "object",
            "properties": {
                "knowledge_base_id": {
                    "type": "string",
                    "const": str(knowledge_base.id),
                },
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["knowledge_base_id", "query"],
        },
        owning_conversation_id=conversation.id,
    )
    session.add(skill)
    await session.flush()

    await session.execute(
        insert(AccessGrant)
        .values(
            [
                {
                    "id": uuid.uuid4(),
                    "org_id": conversation.org_id,
                    "resource_type": ResourceType.KNOWLEDGE_BASE.value,
                    "resource_id": str(knowledge_base.id),
                    "principal_type": PrincipalType.USER.value,
                    "principal_id": conversation.user_id,
                    # WRITE, not just READ: a second attachment later reuses
                    # the same _require_knowledge_access(..., WRITE) check
                    # the ordinary knowledge-base upload endpoint already
                    # enforces, unchanged.
                    "permission": AccessPermission.WRITE.value,
                    "created_by": None,
                    "created_via": GrantSource.PROVISIONING.value,
                },
                {
                    "id": uuid.uuid4(),
                    "org_id": conversation.org_id,
                    "resource_type": ResourceType.SKILL.value,
                    "resource_id": str(skill.id),
                    "principal_type": PrincipalType.USER.value,
                    "principal_id": conversation.user_id,
                    "permission": AccessPermission.READ.value,
                    "created_by": None,
                    "created_via": GrantSource.PROVISIONING.value,
                },
            ]
        )
        .on_conflict_do_nothing(
            constraint="uq_access_grants_resource_principal_permission"
        )
    )
    await session.commit()
    return knowledge_base


async def attach_file(
    session: AsyncSession, conversation: Conversation, file: UploadFile
) -> dict[str, Any]:
    """Lazily provision the conversation's hidden knowledge base (+ its
    execute_hybrid_search skill, on first attachment only) and index the
    given file into it via the same path the knowledge-base page uses."""
    knowledge_base = await _get_or_create_conversation_knowledge_base(session, conversation)
    return await store_and_index_document(
        session,
        org_id=str(conversation.org_id),
        knowledge_base_id=knowledge_base.id,
        file=file,
        uploaded_by=str(conversation.user_id),
    )
