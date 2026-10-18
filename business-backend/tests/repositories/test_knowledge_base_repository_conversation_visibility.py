from src.core.authorization_types import AccessPermission, AuthorizationSubject, GrantSource, PrincipalType
from src.models.access_grant import AccessGrant
from src.models.conversation import Conversation
from src.models.knowledge_base import KnowledgeBase
from src.repositories.knowledge_base_repository import knowledge_base_repository


async def test_hidden_conversation_knowledge_base_never_appears_in_the_list(
    db, test_org, test_user
):
    conv = Conversation(
        org_id=test_org.id, user_id=test_user.id, title="Attach test", model="gpt-4o-mini"
    )
    db.add(conv)
    await db.flush()

    hidden_kb = KnowledgeBase(
        org_id=test_org.id,
        name="hidden",
        owning_conversation_id=conv.id,
    )
    ordinary_kb = KnowledgeBase(org_id=test_org.id, name="ordinary")
    db.add_all([hidden_kb, ordinary_kb])
    await db.flush()
    db.add_all(
        [
            AccessGrant(
                org_id=test_org.id,
                resource_type="knowledge_base",
                resource_id=str(kb.id),
                principal_type=PrincipalType.USER.value,
                principal_id=test_user.id,
                permission=AccessPermission.READ.value,
                created_by=None,
                created_via=GrantSource.PROVISIONING.value,
            )
            for kb in (hidden_kb, ordinary_kb)
        ]
    )
    await db.commit()

    subject = AuthorizationSubject(user_id=test_user.id, org_id=test_org.id, role="member")
    visible = await knowledge_base_repository.list_accessible(db, subject)

    visible_ids = {kb.id for kb in visible}
    assert ordinary_kb.id in visible_ids
    assert hidden_kb.id not in visible_ids
