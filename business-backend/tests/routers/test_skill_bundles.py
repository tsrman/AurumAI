import io
import zipfile
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import delete, select, text

from src.core.auth import UserInfo, get_current_user, get_current_user_or_session
from src.core.authorization_types import AccessPermission, GrantSource, PrincipalType
from src.core.capability_catalog import Capability
from src.core.dependencies import get_org_db_session, get_service_db_session
from src.models.access_grant import AccessGrant
from src.models.organization import Organization
from src.models.organization_capability import OrganizationCapability
from src.models.user import User
from src.routers import skill_bundles


SKILL_MD = (
    b"---\n"
    b"name: greet\n"
    b"description: Greets the user by name.\n"
    b"---\n"
    b"# Greet\nRun scripts/greet.py.\n"
)


def _make_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


@pytest_asyncio.fixture
async def make_client(db, test_org: Organization, test_user: User):
    async def override_get_org_db_session():
        await db.execute(
            text("SELECT set_config('app.current_org_id', :org_id, false)"),
            {"org_id": str(test_org.id)},
        )
        yield db
        await db.execute(text("SELECT set_config('app.current_org_id', '', false)"))

    def make(*, role: str = "member"):
        def override_get_current_user():
            return UserInfo(user_id=str(test_user.id), org_id=str(test_org.id), role=role)

        test_app = FastAPI()
        test_app.include_router(skill_bundles.router, prefix="/v1/skills")
        test_app.dependency_overrides[get_org_db_session] = override_get_org_db_session
        test_app.dependency_overrides[get_service_db_session] = override_get_org_db_session
        test_app.dependency_overrides[get_current_user] = override_get_current_user
        test_app.dependency_overrides[get_current_user_or_session] = override_get_current_user
        return test_app

    return make


@pytest_asyncio.fixture
async def member_client(make_client):
    async with httpx.AsyncClient(
        transport=ASGITransport(app=make_client()), base_url="http://test"
    ) as c:
        yield c


@pytest_asyncio.fixture
async def admin_client(make_client):
    async with httpx.AsyncClient(
        transport=ASGITransport(app=make_client(role="org_admin")), base_url="http://test"
    ) as c:
        yield c


@pytest.fixture(autouse=True)
def mock_object_storage():
    with patch(
        "src.routers.skill_bundles.object_storage.put_bytes", AsyncMock(return_value=None)
    ) as mocked:
        yield mocked


@pytest.mark.asyncio
async def test_upload_code_skill_requires_create_capability(member_client):
    raw = _make_zip({"SKILL.md": SKILL_MD})
    response = await member_client.post(
        "/v1/skills/code",
        data={"name": "greet"},
        files={"bundle": ("greet.zip", raw, "application/zip")},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_upload_code_skill_succeeds(admin_client, mock_object_storage):
    raw = _make_zip({"SKILL.md": SKILL_MD, "scripts/greet.py": b"print('hi')"})
    response = await admin_client.post(
        "/v1/skills/code",
        data={"name": "greet"},
        files={"bundle": ("greet.zip", raw, "application/zip")},
    )
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["type"] == "code"
    assert data["name"] == "greet"
    assert data["description"] == "Greets the user by name."
    assert data["bundleSkillName"] == "greet"
    assert data["bundleSizeBytes"] == len(raw)
    assert len(data["bundleChecksum"]) == 64
    mock_object_storage.assert_awaited_once()


@pytest.mark.asyncio
async def test_v2_upload_uses_create_capability_and_seeds_owner_write_grant(
    make_client,
    db,
    test_org,
    test_user,
):
    db.add(
        OrganizationCapability(
            org_id=test_org.id,
            capability=Capability.WORKSPACE_SKILLS_CREATE.value,
        )
    )
    await db.commit()
    raw = _make_zip({"SKILL.md": SKILL_MD})

    async with httpx.AsyncClient(
        transport=ASGITransport(app=make_client()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/skills/code",
            data={"name": "v2-code-owner"},
            files={"bundle": ("greet.zip", raw, "application/zip")},
        )

    assert response.status_code == 201
    skill_id = response.json()["data"]["id"]
    grant = (
        await db.execute(
            select(AccessGrant).where(
                AccessGrant.org_id == test_org.id,
                AccessGrant.resource_type == "skill",
                AccessGrant.resource_id == skill_id,
            )
        )
    ).scalar_one()
    assert (
        grant.principal_type,
        grant.principal_id,
        grant.permission,
        grant.created_via,
    ) == (
        PrincipalType.USER.value,
        test_user.id,
        AccessPermission.WRITE.value,
        GrantSource.PROVISIONING.value,
    )


@pytest.mark.asyncio
async def test_upload_code_skill_description_override(admin_client):
    raw = _make_zip({"SKILL.md": SKILL_MD})
    response = await admin_client.post(
        "/v1/skills/code",
        data={"name": "greet", "description": "Custom admin description"},
        files={"bundle": ("greet.zip", raw, "application/zip")},
    )
    assert response.status_code == 201
    assert response.json()["data"]["description"] == "Custom admin description"


@pytest.mark.asyncio
async def test_upload_code_skill_rejects_zip_slip(admin_client):
    raw = _make_zip({"SKILL.md": SKILL_MD, "../../etc/passwd": b"pwned"})
    response = await admin_client.post(
        "/v1/skills/code",
        data={"name": "evil"},
        files={"bundle": ("evil.zip", raw, "application/zip")},
    )
    assert response.status_code == 400
    assert "Unsafe path" in response.json()["detail"]


@pytest.mark.asyncio
async def test_upload_code_skill_rejects_missing_skill_md(admin_client):
    raw = _make_zip({"scripts/greet.py": b"print('hi')"})
    response = await admin_client.post(
        "/v1/skills/code",
        data={"name": "no-skill-md"},
        files={"bundle": ("bad.zip", raw, "application/zip")},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_replace_bundle_requires_code_type(admin_client):
    raw = _make_zip({"SKILL.md": SKILL_MD})
    created = await admin_client.post(
        "/v1/skills/code",
        data={"name": "greet"},
        files={"bundle": ("greet.zip", raw, "application/zip")},
    )
    skill_id = created.json()["data"]["id"]

    new_raw = _make_zip({"SKILL.md": SKILL_MD, "scripts/v2.py": b"print('v2')"})
    response = await admin_client.put(
        f"/v1/skills/code/{skill_id}/bundle",
        files={"bundle": ("greet-v2.zip", new_raw, "application/zip")},
    )
    assert response.status_code == 200
    assert response.json()["data"]["bundleSizeBytes"] == len(new_raw)


@pytest.mark.asyncio
async def test_v2_replace_bundle_requires_write_not_read_grant(
    admin_client,
    make_client,
    db,
    test_org,
    test_user,
):
    raw = _make_zip({"SKILL.md": SKILL_MD})
    created = await admin_client.post(
        "/v1/skills/code",
        data={"name": "v2-code-write"},
        files={"bundle": ("greet.zip", raw, "application/zip")},
    )
    skill_id = created.json()["data"]["id"]
    await db.execute(
        delete(AccessGrant).where(
            AccessGrant.org_id == test_org.id,
            AccessGrant.resource_type == "skill",
            AccessGrant.resource_id == skill_id,
            AccessGrant.principal_type == PrincipalType.USER.value,
            AccessGrant.principal_id == test_user.id,
        )
    )
    read_grant = AccessGrant(
        org_id=test_org.id,
        resource_type="skill",
        resource_id=skill_id,
        principal_type=PrincipalType.USER.value,
        principal_id=test_user.id,
        permission=AccessPermission.READ.value,
        created_via=GrantSource.PROVISIONING.value,
    )
    db.add(read_grant)
    await db.commit()
    new_raw = _make_zip({"SKILL.md": SKILL_MD, "scripts/v2.py": b"print('v2')"})

    async with httpx.AsyncClient(
        transport=ASGITransport(app=make_client()), base_url="http://test"
    ) as client:
        read_only = await client.put(
            f"/v1/skills/code/{skill_id}/bundle",
            files={"bundle": ("greet-v2.zip", new_raw, "application/zip")},
        )
        await db.execute(delete(AccessGrant).where(AccessGrant.id == read_grant.id))
        db.add(
            AccessGrant(
                org_id=test_org.id,
                resource_type="skill",
                resource_id=skill_id,
                principal_type=PrincipalType.USER.value,
                principal_id=test_user.id,
                permission=AccessPermission.WRITE.value,
                created_via=GrantSource.PROVISIONING.value,
            )
        )
        await db.commit()
        writable = await client.put(
            f"/v1/skills/code/{skill_id}/bundle",
            files={"bundle": ("greet-v2.zip", new_raw, "application/zip")},
        )

    assert read_only.status_code == 403
    assert writable.status_code == 200


@pytest.mark.asyncio
async def test_replace_bundle_404_for_nonexistent_skill(admin_client):
    raw = _make_zip({"SKILL.md": SKILL_MD})
    response = await admin_client.put(
        "/v1/skills/code/00000000-0000-0000-0000-000000000000/bundle",
        files={"bundle": ("greet.zip", raw, "application/zip")},
    )
    assert response.status_code == 404
