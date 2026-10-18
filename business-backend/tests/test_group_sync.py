import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo
from src.models.organization import Organization
from src.models.user import User
from src.repositories.group_repository import group_repository
from src.services.auth_service import auth_service


async def test_sync_keycloak_user_creates_groups_and_membership(db: AsyncSession) -> None:
    org_id = uuid.uuid4()
    user_id = uuid.uuid4()

    org = Organization(
        id=org_id,
        name="Test Org",
        slug=f"test-{org_id.hex[:8]}",
    )
    db.add(org)
    await db.flush()

    user = User(
        id=user_id,
        keycloak_id=user_id,
        email="test@example.com",
        org_id=org_id,
        role="member",
    )
    db.add(user)
    await db.commit()

    user_info = UserInfo(
        user_id=str(user_id),
        email="test@example.com",
        full_name="Test User",
        org_id=str(org_id),
        org_name="Test Org",
        role="member",
        raw_claims={"groups": ["admin", "support"]},
    )

    synced_user = await auth_service.sync_keycloak_user(db, user_info)
    assert synced_user.id == user_id

    admin_group = await group_repository.get_by_name_and_org(db, "admin", org_id)
    assert admin_group is not None
    assert admin_group.org_id == org_id

    support_group = await group_repository.get_by_name_and_org(db, "support", org_id)
    assert support_group is not None
