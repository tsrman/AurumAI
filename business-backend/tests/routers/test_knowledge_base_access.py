import uuid
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo, get_current_user
from src.core.authorization_types import AccessPermission, GrantSource, PrincipalType
from src.core.capability_catalog import Capability
from src.core.dependencies import get_org_db_session
from src.models.access_grant import AccessGrant
from src.models.group import Group
from src.models.group_capability import GroupCapability
from src.models.knowledge_base import KnowledgeBase
from src.models.organization import Organization
from src.models.user import User
from src.models.user_group import UserGroup
from src.routers import knowledge_bases


def _grant(
    *,
    org_id: uuid.UUID,
    base_id: uuid.UUID,
    principal_type: PrincipalType,
    principal_id: uuid.UUID,
    permission: AccessPermission = AccessPermission.READ,
) -> AccessGrant:
    return AccessGrant(
        org_id=org_id,
        resource_type="knowledge_base",
        resource_id=str(base_id),
        principal_type=principal_type.value,
        principal_id=principal_id,
        permission=permission.value,
        created_via=GrantSource.PROVISIONING.value,
    )


def _app(
    db: AsyncSession,
    org: Organization,
    user: User,
    *,
    role: str = "member",
) -> FastAPI:
    async def session_override():
        yield db

    app = FastAPI()
    app.include_router(knowledge_bases.router, prefix="/v1")
    app.dependency_overrides[get_current_user] = lambda: UserInfo(
        user_id=str(user.id),
        org_id=str(org.id),
        role=role,
    )
    app.dependency_overrides[get_org_db_session] = session_override
    return app


async def test_v2_create_requires_live_capability_and_atomically_grants_owner_write(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
) -> None:
    denied_app = _app(db, test_org, test_user)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=denied_app), base_url="http://test"
    ) as client:
        denied = await client.post(
            "/v1/knowledge-bases",
            json={"name": "Engineering", "description": "Runbooks"},
        )

    assert denied.status_code == 403
    assert "workspace.knowledge.create" in denied.text

    group = Group(org_id=test_org.id, name="knowledge-creators")
    db.add(group)
    await db.flush()
    db.add(UserGroup(user_id=test_user.id, group_id=group.id))
    db.add(
        GroupCapability(
            group_id=group.id,
            capability=Capability.WORKSPACE_KNOWLEDGE_CREATE.value,
        )
    )
    await db.commit()

    allowed_app = _app(db, test_org, test_user)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=allowed_app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/v1/knowledge-bases",
            json={"name": "  Engineering  ", "description": "  Runbooks  "},
        )

    assert created.status_code == 201
    assert created.json()["data"]["name"] == "Engineering"
    assert created.json()["data"]["canWrite"] is True
    resource_id = created.json()["data"]["id"]
    base = (
        await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == uuid.UUID(resource_id)))
    ).scalar_one()
    grant = (
        await db.execute(
            select(AccessGrant).where(
                AccessGrant.resource_type == "knowledge_base",
                AccessGrant.resource_id == resource_id,
            )
        )
    ).scalar_one()
    assert base.created_by == test_user.id
    assert grant.org_id == test_org.id
    assert grant.principal_type == "user"
    assert grant.principal_id == test_user.id
    assert grant.permission == "write"


async def test_v2_list_and_read_cover_all_principals_write_implies_read_and_hide_denials(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
) -> None:
    other_org = Organization(name="Other", slug=f"other-{uuid.uuid4()}")
    db.add(other_org)
    await db.flush()
    group = Group(org_id=test_org.id, name="knowledge-readers")
    db.add(group)
    await db.flush()
    db.add(UserGroup(user_id=test_user.id, group_id=group.id))
    bases = {
        name: KnowledgeBase(org_id=test_org.id, created_by=test_user.id, name=name)
        for name in ("Direct", "Group", "Organization", "Writable", "Private")
    }
    foreign = KnowledgeBase(org_id=other_org.id, name="Foreign")
    db.add_all([*bases.values(), foreign])
    await db.flush()
    db.add_all(
        [
            _grant(
                org_id=test_org.id,
                base_id=bases["Direct"].id,
                principal_type=PrincipalType.USER,
                principal_id=test_user.id,
            ),
            _grant(
                org_id=test_org.id,
                base_id=bases["Group"].id,
                principal_type=PrincipalType.GROUP,
                principal_id=group.id,
            ),
            _grant(
                org_id=test_org.id,
                base_id=bases["Organization"].id,
                principal_type=PrincipalType.ORGANIZATION,
                principal_id=test_org.id,
            ),
            _grant(
                org_id=test_org.id,
                base_id=bases["Writable"].id,
                principal_type=PrincipalType.USER,
                principal_id=test_user.id,
                permission=AccessPermission.WRITE,
            ),
        ]
    )
    await db.commit()

    app = _app(db, test_org, test_user)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        listing = await client.get("/v1/knowledge-bases")
        direct = await client.get(f"/v1/knowledge-bases/{bases['Direct'].id}")
        private = await client.get(f"/v1/knowledge-bases/{bases['Private'].id}")
        cross_org = await client.get(f"/v1/knowledge-bases/{foreign.id}")

    assert listing.status_code == 200
    assert {item["name"] for item in listing.json()["data"]} == {
        "Direct",
        "Group",
        "Organization",
        "Writable",
    }
    write_by_name = {
        item["name"]: item["canWrite"] for item in listing.json()["data"]
    }
    assert write_by_name == {
        "Direct": False,
        "Group": False,
        "Organization": False,
        "Writable": True,
    }
    assert direct.status_code == 200
    assert direct.json()["data"]["canWrite"] is False
    assert private.status_code == 404
    assert cross_org.status_code == 404
    assert private.json()["detail"] == cross_org.json()["detail"] == "Knowledge base not found"


async def test_v2_org_admin_bypasses_same_org_grants_only(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
) -> None:
    other_org = Organization(name="Admin Other", slug=f"admin-other-{uuid.uuid4()}")
    db.add(other_org)
    await db.flush()
    local = KnowledgeBase(org_id=test_org.id, name="Local private")
    foreign = KnowledgeBase(org_id=other_org.id, name="Foreign private")
    db.add_all([local, foreign])
    await db.commit()

    app = _app(db, test_org, test_user, role="org_admin")
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        listing = await client.get("/v1/knowledge-bases")
        local_response = await client.get(f"/v1/knowledge-bases/{local.id}")
        foreign_response = await client.get(f"/v1/knowledge-bases/{foreign.id}")

    assert [item["name"] for item in listing.json()["data"]] == ["Local private"]
    assert local_response.status_code == 200
    assert foreign_response.status_code == 404


async def test_v2_delete_requires_write_and_removes_empty_base_with_grants(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
) -> None:
    knowledge_base = KnowledgeBase(org_id=test_org.id, name="Disposable")
    db.add(knowledge_base)
    await db.flush()
    grant = _grant(
        org_id=test_org.id,
        base_id=knowledge_base.id,
        principal_type=PrincipalType.USER,
        principal_id=test_user.id,
    )
    db.add(grant)
    await db.commit()
    app = _app(db, test_org, test_user)

    with patch.object(
        knowledge_bases.rag_client,
        "list_documents",
        AsyncMock(return_value=[]),
    ) as rag_list:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            read_only = await client.delete(
                f"/v1/knowledge-bases/{knowledge_base.id}"
            )
            grant.permission = AccessPermission.WRITE.value
            await db.commit()
            deleted = await client.delete(
                f"/v1/knowledge-bases/{knowledge_base.id}"
            )

    assert read_only.status_code == 403
    assert deleted.status_code == 204
    rag_list.assert_awaited_once_with(str(test_org.id), str(knowledge_base.id))
    assert await db.get(KnowledgeBase, knowledge_base.id) is None
    remaining_grant = await db.execute(
        select(AccessGrant).where(AccessGrant.id == grant.id)
    )
    assert remaining_grant.scalar_one_or_none() is None


async def test_delete_rejects_nonempty_base_without_mutation(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
) -> None:
    knowledge_base = KnowledgeBase(org_id=test_org.id, name="Nonempty")
    db.add(knowledge_base)
    await db.flush()
    db.add(
        _grant(
            org_id=test_org.id,
            base_id=knowledge_base.id,
            principal_type=PrincipalType.USER,
            principal_id=test_user.id,
            permission=AccessPermission.WRITE,
        )
    )
    await db.commit()
    app = _app(db, test_org, test_user)

    with patch.object(
        knowledge_bases.rag_client,
        "list_documents",
        AsyncMock(return_value=[{"id": str(uuid.uuid4())}]),
    ):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.delete(
                f"/v1/knowledge-bases/{knowledge_base.id}"
            )

    assert response.status_code == 409
    assert response.json()["detail"] == "Knowledge base contains documents"
    assert await db.get(KnowledgeBase, knowledge_base.id) is not None
