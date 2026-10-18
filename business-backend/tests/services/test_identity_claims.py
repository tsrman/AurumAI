from uuid import uuid4

import pytest
from sqlalchemy import select

from src.core.auth import UserInfo
from src.models.group import Group
from src.models.user_group import UserGroup
from tests.services.test_identity_sync import claims


@pytest.mark.parametrize("mode", ["async", "legacy_enrolled", "legacy_untracked"])
@pytest.mark.asyncio
async def test_complete_marker_revokes_omitted_last_group(db, test_org, mode):
    from src.models.identity_sync import IdentitySyncInbox
    from src.services.auth_service import auth_service
    from src.services.identity_sync import (
        IdentitySyncPending,
        apply_identity,
        require_applied_identity,
        submit_identity,
    )

    org_id, user_id = test_org.id, uuid4()
    initial = claims(org_id, user_id, ["finance"], 100)
    if mode == "legacy_untracked":
        await auth_service.sync_keycloak_user(db, initial)
    else:
        await submit_identity(db, initial)
        await apply_identity(db, org_id, user_id, 1)
    empty = claims(org_id, user_id, None, 102)
    empty.raw_claims["platform_groups_complete"] = True
    if mode == "async":
        generation = await submit_identity(db, empty)
        inbox = await db.get(IdentitySyncInbox, (org_id, user_id, generation))
        assert inbox.snapshot["groups"] == []
        assert "platform_groups_complete" not in inbox.snapshot
        with pytest.raises(IdentitySyncPending):
            await require_applied_identity(db, org_id, user_id)
        await apply_identity(db, org_id, user_id, generation)
    elif mode == "legacy_enrolled":
        stale = await submit_identity(db, claims(org_id, user_id, ["legal"], 101))
        await auth_service.sync_keycloak_user(db, empty)
        await apply_identity(db, org_id, user_id, stale)
        await require_applied_identity(db, org_id, user_id)
    else:
        empty.raw_claims.pop("iat")
        await auth_service.sync_keycloak_user(db, empty)
    assert (
        list(
            await db.scalars(select(Group.name).join(UserGroup).where(UserGroup.user_id == user_id))
        )
        == []
    )


@pytest.mark.parametrize("marker", [None, False, "true", "false", 1, 0, [], {}])
def test_marker_requires_exact_boolean_true(marker):
    from src.services.identity_claims import normalize_groups

    assert normalize_groups({"platform_groups_complete": marker}) is None


@pytest.mark.parametrize("marker", [None, False, True, "true", 1])
@pytest.mark.parametrize(
    "groups,expected", [([], set()), (["/finance", "org-hidden"], {"finance"}), (None, set())]
)
def test_explicit_group_claim_is_authoritative(marker, groups, expected):
    from src.services.identity_claims import normalize_groups

    assert normalize_groups({"groups": groups, "platform_groups_complete": marker}) == expected


@pytest.mark.parametrize("groups", [[], ["finance"]])
@pytest.mark.asyncio
async def test_marker_only_change_with_explicit_groups_keeps_digest_and_generation(
    db, test_org, groups
):
    from src.services.identity_sync import normalize_identity, submit_identity

    user = claims(test_org.id, uuid4(), groups, 100)
    before = normalize_identity(user)
    assert await submit_identity(db, user) == 1
    user.raw_claims["platform_groups_complete"] = True
    after = normalize_identity(user)
    assert before == after
    assert "platform_groups_complete" not in after[0]
    assert await submit_identity(db, user) == 1


def test_complete_omission_and_explicit_empty_list_have_identical_snapshot_digest():
    from src.services.identity_sync import normalize_identity

    user = UserInfo(
        user_id=str(uuid4()), org_id=str(uuid4()), raw_claims={"iat": 100, "groups": []}
    )
    explicit = normalize_identity(user)
    user.raw_claims = {"iat": 100, "platform_groups_complete": True}
    assert normalize_identity(user) == explicit


@pytest.mark.parametrize("groups", ["finance", [1], ["x" * 256], ["x"] * 257])
def test_common_group_normalization_retains_bounds(groups):
    from fastapi import HTTPException
    from src.services.identity_claims import normalize_groups

    with pytest.raises(HTTPException) as error:
        normalize_groups({"groups": groups, "platform_groups_complete": True})
    assert error.value.status_code == 400
