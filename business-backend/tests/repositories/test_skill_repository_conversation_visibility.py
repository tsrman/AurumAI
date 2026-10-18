import uuid

from src.core.authorization_types import AccessPermission, AuthorizationSubject, GrantSource, PrincipalType
from src.models.access_grant import AccessGrant
from src.models.conversation import Conversation
from src.models.skill import Skill
from src.repositories.skill_repository import skill_repository


async def _grant_skill_read(db, org_id, skill_id, user_id) -> None:
    db.add(
        AccessGrant(
            org_id=org_id,
            resource_type="skill",
            resource_id=str(skill_id),
            principal_type=PrincipalType.USER.value,
            principal_id=user_id,
            permission=AccessPermission.READ.value,
            created_by=None,
            created_via=GrantSource.PROVISIONING.value,
        )
    )


async def test_hidden_conversation_skill_is_invisible_without_matching_conversation_id(
    db, test_org, test_user
):
    conv = Conversation(
        org_id=test_org.id, user_id=test_user.id, title="Attach test", model="gpt-4o-mini"
    )
    db.add(conv)
    await db.flush()

    hidden_skill = Skill(
        org_id=test_org.id,
        name="hidden",
        type="tool",
        activity_name="execute_hybrid_search",
        owning_conversation_id=conv.id,
    )
    db.add(hidden_skill)
    await db.flush()
    await _grant_skill_read(db, test_org.id, hidden_skill.id, test_user.id)
    await db.commit()

    subject = AuthorizationSubject(user_id=test_user.id, org_id=test_org.id, role="member")

    without_conversation = await skill_repository.list_accessible_by_org(db, subject)
    assert hidden_skill.id not in {s.id for s in without_conversation}

    with_this_conversation = await skill_repository.list_accessible_by_org(
        db, subject, conversation_id=conv.id
    )
    assert hidden_skill.id in {s.id for s in with_this_conversation}

    other_conversation_id = uuid.uuid4()
    with_other_conversation = await skill_repository.list_accessible_by_org(
        db, subject, conversation_id=other_conversation_id
    )
    assert hidden_skill.id not in {s.id for s in with_other_conversation}


async def test_ordinary_skill_stays_visible_regardless_of_conversation_id(
    db, test_org, test_user
):
    ordinary = Skill(org_id=test_org.id, name="ordinary", type="tool")
    db.add(ordinary)
    await db.flush()
    await _grant_skill_read(db, test_org.id, ordinary.id, test_user.id)
    await db.commit()

    subject = AuthorizationSubject(user_id=test_user.id, org_id=test_org.id, role="member")

    without_conversation = await skill_repository.list_accessible_by_org(db, subject)
    with_conversation = await skill_repository.list_accessible_by_org(
        db, subject, conversation_id=uuid.uuid4()
    )

    assert ordinary.id in {s.id for s in without_conversation}
    assert ordinary.id in {s.id for s in with_conversation}
