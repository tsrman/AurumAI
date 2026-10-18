import uuid

from fastapi import HTTPException
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.authorization_types import AccessPermission, GrantSource, PrincipalType
from src.models.access_grant import AccessGrant
from src.models.group import Group
from src.models.organization import Organization
from src.models.user import User
from src.models.user_group import UserGroup
from src.services.model_access import allowed_model_ids, ensure_model_allowed


async def _identity(
    db: AsyncSession,
    *,
    role: str = "member",
) -> tuple[uuid.UUID, User]:
    org = Organization(name="Model v2 Org", slug=f"model-v2-{uuid.uuid4()}")
    db.add(org)
    await db.flush()
    user = User(
        keycloak_id=uuid.uuid4(),
        email=f"model-v2-{uuid.uuid4()}@example.com",
        org_id=org.id,
        role=role,
    )
    db.add(user)
    await db.commit()
    return org.id, user


def _model_grant(
    *,
    org_id: uuid.UUID,
    model_id: str,
    principal_type: PrincipalType,
    principal_id: uuid.UUID,
) -> AccessGrant:
    return AccessGrant(
        org_id=org_id,
        resource_type="model",
        resource_id=model_id,
        principal_type=principal_type.value,
        principal_id=principal_id,
        permission=AccessPermission.READ.value,
        created_via=GrantSource.PROVISIONING.value,
    )


async def test_allowed_model_ids_fail_closed_without_complete_identity(
    db: AsyncSession,
) -> None:
    assert await allowed_model_ids(db, None, ["gpt-4o"]) == []
    assert await allowed_model_ids(db, str(uuid.uuid4()), ["gpt-4o"]) == []


async def test_allowed_model_ids_requires_grant_for_member(db: AsyncSession) -> None:
    org_id, user = await _identity(db)

    visible = await allowed_model_ids(
        db,
        str(org_id),
        ["gpt-4o", "gpt-4o-mini"],
        user_id=str(user.id),
        role="member",
    )

    assert visible == []


async def test_allowed_models_cover_org_user_group_and_admin_bypass(
    db: AsyncSession,
) -> None:
    org_id, user = await _identity(db)
    group = Group(org_id=org_id, name="v2-model-group")
    db.add(group)
    await db.flush()
    db.add(UserGroup(user_id=user.id, group_id=group.id))
    db.add_all(
        [
            _model_grant(
                org_id=org_id,
                model_id="org/model:v1",
                principal_type=PrincipalType.ORGANIZATION,
                principal_id=org_id,
            ),
            _model_grant(
                org_id=org_id,
                model_id="user-model",
                principal_type=PrincipalType.USER,
                principal_id=user.id,
            ),
            _model_grant(
                org_id=org_id,
                model_id="group.model",
                principal_type=PrincipalType.GROUP,
                principal_id=group.id,
            ),
        ]
    )
    admin = User(
        keycloak_id=uuid.uuid4(),
        email=f"model-admin-{uuid.uuid4()}@example.com",
        org_id=org_id,
        role="org_admin",
    )
    db.add(admin)
    await db.commit()
    candidates = ["org/model:v1", "user-model", "group.model", "private-model"]

    visible = await allowed_model_ids(
        db,
        str(org_id),
        candidates,
        user_id=str(user.id),
        role="member",
    )
    admin_visible = await allowed_model_ids(
        db,
        str(org_id),
        candidates,
        user_id=str(admin.id),
        role="org_admin",
    )

    assert visible == ["org/model:v1", "user-model", "group.model"]
    assert admin_visible == candidates


async def test_model_revocation_is_live_for_same_subject(db: AsyncSession) -> None:
    org_id, user = await _identity(db)
    grant = _model_grant(
        org_id=org_id,
        model_id="live-revoke-model",
        principal_type=PrincipalType.USER,
        principal_id=user.id,
    )
    db.add(grant)
    await db.commit()

    await ensure_model_allowed(
        db,
        str(org_id),
        "live-revoke-model",
        user_id=str(user.id),
        role="member",
    )
    await db.execute(delete(AccessGrant).where(AccessGrant.id == grant.id))
    await db.commit()

    try:
        await ensure_model_allowed(
            db,
            str(org_id),
            "live-revoke-model",
            user_id=str(user.id),
            role="member",
        )
        assert False, "expected revoked model to be denied"
    except HTTPException as exc:
        assert exc.status_code == 403
        assert "live-revoke-model" not in str(exc.detail)


async def test_model_ids_preserve_punctuation_and_reject_overlong_ids(
    db: AsyncSession,
) -> None:
    org_id, user = await _identity(db)
    exact_id = "custom://workspace/model:v1?region=eu-1"
    too_long = "m" * 256
    db.add(
        _model_grant(
            org_id=org_id,
            model_id=exact_id,
            principal_type=PrincipalType.USER,
            principal_id=user.id,
        )
    )
    await db.commit()

    visible = await allowed_model_ids(
        db,
        str(org_id),
        [exact_id, too_long],
        user_id=str(user.id),
        role="member",
    )

    assert visible == [exact_id]


async def test_ensure_model_allowed_denies_ungranted_model(db: AsyncSession) -> None:
    org_id, user = await _identity(db)

    try:
        await ensure_model_allowed(
            db,
            str(org_id),
            "private-model",
            user_id=str(user.id),
            role="member",
        )
        assert False, "expected HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 403


async def test_ensure_model_allowed_admin_bypasses_grants(db: AsyncSession) -> None:
    org_id, admin = await _identity(db, role="org_admin")

    await ensure_model_allowed(
        db,
        str(org_id),
        "private-model",
        user_id=str(admin.id),
        role="org_admin",
    )
