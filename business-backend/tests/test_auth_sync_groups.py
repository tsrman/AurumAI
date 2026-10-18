import uuid
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo
from src.models.group import Group
from src.models.organization import Organization
from src.models.user import User
from src.models.user_group import UserGroup
from src.repositories.group_repository import group_repository
from src.services.auth_service import auth_service


async def _make_org_and_user(db: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    org_id = uuid.uuid4()
    user_id = uuid.uuid4()
    org = Organization(id=org_id, name="Test Org", slug=f"test-{org_id.hex[:8]}")
    db.add(org)
    await db.flush()
    user = User(id=user_id, keycloak_id=user_id, email="t@example.com", org_id=org_id)
    db.add(user)
    await db.commit()
    return org_id, user_id


async def _member_group_names(db: AsyncSession, user_id: uuid.UUID) -> set[str]:
    result = await db.execute(
        select(Group.name)
        .join(UserGroup, UserGroup.group_id == Group.id)
        .where(UserGroup.user_id == user_id)
    )
    return set(result.scalars().all())


async def test_missing_groups_claim_does_not_touch_membership(db: AsyncSession) -> None:
    org_id, user_id = await _make_org_and_user(db)
    group = Group(org_id=org_id, name="preexisting")
    db.add(group)
    await db.flush()
    db.add(UserGroup(user_id=user_id, group_id=group.id))
    await db.commit()

    user_info = UserInfo(
        user_id=str(user_id), org_id=str(org_id), raw_claims={}  # no "groups" key at all
    )
    await auth_service.sync_keycloak_user(db, user_info)

    assert await _member_group_names(db, user_id) == {"preexisting"}


async def test_empty_groups_list_removes_all_membership(db: AsyncSession) -> None:
    org_id, user_id = await _make_org_and_user(db)
    group = Group(org_id=org_id, name="stale")
    db.add(group)
    await db.flush()
    db.add(UserGroup(user_id=user_id, group_id=group.id))
    await db.commit()

    user_info = UserInfo(user_id=str(user_id), org_id=str(org_id), raw_claims={"groups": []})
    await auth_service.sync_keycloak_user(db, user_info)

    assert await _member_group_names(db, user_id) == set()


async def test_reconcile_adds_and_removes_membership(db: AsyncSession) -> None:
    org_id, user_id = await _make_org_and_user(db)
    stale = Group(org_id=org_id, name="stale")
    db.add(stale)
    await db.flush()
    db.add(UserGroup(user_id=user_id, group_id=stale.id))
    await db.commit()

    user_info = UserInfo(
        user_id=str(user_id), org_id=str(org_id), raw_claims={"groups": ["fresh"]}
    )
    await auth_service.sync_keycloak_user(db, user_info)

    assert await _member_group_names(db, user_id) == {"fresh"}


async def test_org_prefixed_groups_are_excluded_from_sync(db: AsyncSession) -> None:
    org_id, user_id = await _make_org_and_user(db)

    user_info = UserInfo(
        user_id=str(user_id),
        org_id=str(org_id),
        raw_claims={"groups": ["org-technosoft", "analysts"]},
    )
    await auth_service.sync_keycloak_user(db, user_info)

    assert await _member_group_names(db, user_id) == {"analysts"}
    assert await group_repository.get_by_name_and_org(db, "org-technosoft", org_id) is None


async def test_autocreated_group_gets_last_synced_at(db: AsyncSession) -> None:
    org_id, user_id = await _make_org_and_user(db)

    user_info = UserInfo(
        user_id=str(user_id), org_id=str(org_id), raw_claims={"groups": ["engineers"]}
    )
    await auth_service.sync_keycloak_user(db, user_info)

    group = await group_repository.get_by_name_and_org(db, "engineers", org_id)
    assert group is not None
    assert group.last_synced_at is not None


async def test_preprovisioned_group_identity_survives_sync(db: AsyncSession) -> None:
    org_id, user_id = await _make_org_and_user(db)
    group = Group(org_id=org_id, name="analysts")
    db.add(group)
    await db.commit()
    assert group.last_synced_at is None

    user_info = UserInfo(
        user_id=str(user_id), org_id=str(org_id), raw_claims={"groups": ["analysts"]}
    )
    await auth_service.sync_keycloak_user(db, user_info)

    synced_group = await group_repository.get_by_name_and_org(db, "analysts", org_id)
    assert synced_group is not None
    assert synced_group.id == group.id
    assert synced_group.last_synced_at is not None


async def test_leading_slash_in_group_name_is_stripped(db: AsyncSession) -> None:
    org_id, user_id = await _make_org_and_user(db)

    user_info = UserInfo(
        user_id=str(user_id), org_id=str(org_id), raw_claims={"groups": ["/analysts"]}
    )
    await auth_service.sync_keycloak_user(db, user_info)

    assert await _member_group_names(db, user_id) == {"analysts"}


async def test_concurrent_new_group_creation_recovers_from_lost_race(db: AsyncSession) -> None:
    """Regression test: two concurrent syncs racing to
    create the same brand-new group both pass the pre-check SELECT — before
    uq_groups_org_id_name (2026_07_19_uq_groups_org_name) + the
    IntegrityError catch, the loser's flush() would crash the whole sync
    instead of just re-fetching what the winner committed.

    Reproduces the race honestly within a single test connection: the
    "winner" row is genuinely committed *before* sync_keycloak_user runs
    (so it's not undone by any later nested-transaction rollback), and the
    pre-check SELECT that would normally see it is stubbed to miss it once
    — exactly the window a real concurrent second session would have hit.
    The INSERT that follows then hits the real unique constraint, not a
    synthetic one."""
    org_id, user_id = await _make_org_and_user(db)

    winner_group = Group(org_id=org_id, name="new-team")
    db.add(winner_group)
    await db.commit()

    real_execute = AsyncSession.execute
    call_count = 0

    async def stubbed_execute(self: AsyncSession, statement: object, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        nonlocal call_count
        call_count += 1
        # Call 1 is _get_or_create_user's user lookup (must see the real
        # user); call 2 is sync_keycloak_user's "existing groups" pre-check
        # — miss it once to reproduce the race window, real DB state
        # afterwards is what actually catches the conflict.
        if call_count == 2:
            return await real_execute(self, select(Group).where(Group.id == None), *args, **kwargs)  # noqa: E711
        return await real_execute(self, statement, *args, **kwargs)

    user_info = UserInfo(
        user_id=str(user_id), org_id=str(org_id), raw_claims={"groups": ["new-team"]}
    )
    with patch.object(AsyncSession, "execute", stubbed_execute):
        await auth_service.sync_keycloak_user(db, user_info)

    assert await _member_group_names(db, user_id) == {"new-team"}
    result = await db.execute(
        select(Group).where(Group.org_id == org_id, Group.name == "new-team")
    )
    groups = result.scalars().all()
    assert len(groups) == 1
    assert groups[0].id == winner_group.id
