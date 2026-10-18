import uuid
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from src.core.authorization_types import AccessPermission, GrantSource, PrincipalType, ResourceType
from src.models.access_grant import AccessGrant
from src.models.conversation import Conversation
from src.models.knowledge_base import KnowledgeBase
from src.models.skill import Skill
from src.services.conversation_attachments import attach_file


async def _grants(db, resource_type: str, resource_id: str) -> list[AccessGrant]:
    rows = await db.execute(
        select(AccessGrant).where(
            AccessGrant.resource_type == resource_type,
            AccessGrant.resource_id == resource_id,
        )
    )
    return list(rows.scalars())


async def test_first_attachment_provisions_hidden_kb_skill_and_grants(db, test_org, test_user):
    conv = Conversation(
        org_id=test_org.id, user_id=test_user.id, title="Attach test", model="gpt-4o-mini"
    )
    db.add(conv)
    await db.commit()

    fake_doc = {"id": str(uuid.uuid4()), "knowledge_base_id": "placeholder"}
    with patch(
        "src.services.conversation_attachments.store_and_index_document",
        AsyncMock(return_value=fake_doc),
    ) as store:
        result = await attach_file(db, conv, file=object())  # type: ignore[arg-type]

    assert result is fake_doc

    kb = (
        await db.execute(
            select(KnowledgeBase).where(KnowledgeBase.owning_conversation_id == conv.id)
        )
    ).scalar_one()
    skill = (
        await db.execute(select(Skill).where(Skill.owning_conversation_id == conv.id))
    ).scalar_one()

    assert skill.type == "tool"
    assert skill.activity_name == "execute_hybrid_search"
    assert skill.input_schema["properties"]["knowledge_base_id"]["const"] == str(kb.id)
    assert str(kb.id) in skill.description
    # Observed live: given both id=<uuid> and a human-ish name in the tools
    # system message, the model emitted the *name* as skill_id instead of
    # the id — dispatch (execute_skill, looked up by id only) 404ed. Name
    # equal to id means either field the model picks resolves correctly.
    assert skill.name == str(skill.id)

    kb_grants = await _grants(db, ResourceType.KNOWLEDGE_BASE.value, str(kb.id))
    assert len(kb_grants) == 1
    assert kb_grants[0].principal_type == PrincipalType.USER.value
    assert kb_grants[0].principal_id == test_user.id
    assert kb_grants[0].permission == AccessPermission.WRITE.value
    assert kb_grants[0].created_via == GrantSource.PROVISIONING.value
    assert kb_grants[0].created_by is None

    skill_grants = await _grants(db, ResourceType.SKILL.value, str(skill.id))
    assert len(skill_grants) == 1
    assert skill_grants[0].permission == AccessPermission.READ.value

    store.assert_awaited_once()
    assert store.await_args.kwargs["knowledge_base_id"] == kb.id


async def test_second_attachment_reuses_the_same_hidden_kb_and_skill(db, test_org, test_user):
    conv = Conversation(
        org_id=test_org.id, user_id=test_user.id, title="Attach test", model="gpt-4o-mini"
    )
    db.add(conv)
    await db.commit()

    with patch(
        "src.services.conversation_attachments.store_and_index_document",
        AsyncMock(return_value={"id": str(uuid.uuid4())}),
    ):
        await attach_file(db, conv, file=object())  # type: ignore[arg-type]
        await attach_file(db, conv, file=object())  # type: ignore[arg-type]

    kbs = (
        await db.execute(
            select(KnowledgeBase).where(KnowledgeBase.owning_conversation_id == conv.id)
        )
    ).scalars().all()
    skills = (
        await db.execute(select(Skill).where(Skill.owning_conversation_id == conv.id))
    ).scalars().all()

    assert len(kbs) == 1
    assert len(skills) == 1
