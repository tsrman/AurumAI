"""Skills Hermes Agent writes for itself: drafted per user, deduplicated by
content so decisions stick, and turned into code skills only by an
administrator who sees every file."""

import hashlib
import uuid
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import select, text

from src.core.auth import UserInfo, get_current_user
from src.core.authorization_types import AuthorizationSubject
from src.core.dependencies import get_current_org_id, get_org_db_session
from src.models.access_grant import AccessGrant
from src.models.hermes_skill_draft import HermesSkillDraft
from src.models.skill import Skill
from src.models.user import User
from src.services import hermes_skill_drafts
from src.services.hermes_skill_drafts import MAX_FILE_BYTES, DraftError, DraftFile

SKILL_MD = "---\nname: arxiv-digest\ndescription: Summarize new arXiv papers\n---\nRun scripts/fetch.py.\n"


def _files(body: str = "print('papers')\n", skill_md: str = SKILL_MD) -> list[DraftFile]:
    return [DraftFile("SKILL.md", skill_md), DraftFile("scripts/fetch.py", body)]


@pytest.fixture
def bucket(monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    stored: dict[str, bytes] = {}

    async def put_bytes(key: str, data: bytes, **_: Any) -> None:
        stored[key] = data

    async def get_bytes(key: str, **_: Any) -> bytes:
        return stored[key]

    monkeypatch.setattr(hermes_skill_drafts.object_storage, "put_bytes", put_bytes)
    monkeypatch.setattr(hermes_skill_drafts.object_storage, "get_bytes", get_bytes)
    return stored


async def _drafts(db, org_id: uuid.UUID) -> list[HermesSkillDraft]:  # noqa: ANN001
    return list((await db.scalars(select(HermesSkillDraft).where(HermesSkillDraft.org_id == org_id))).all())


async def _other_user(db, org_id: uuid.UUID, role: str = "member") -> User:  # noqa: ANN001
    other = User(keycloak_id=uuid.uuid4(), email=f"u-{uuid.uuid4().hex[:8]}@example.com", org_id=org_id, role=role)
    db.add(other)
    await db.flush()
    return other


async def test_table_forces_row_security_with_an_org_policy(db) -> None:  # noqa: ANN001
    row = (
        await db.execute(
            text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'hermes_skill_drafts'")
        )
    ).one()
    policies = (
        await db.execute(text("SELECT policyname FROM pg_policies WHERE tablename = 'hermes_skill_drafts'"))
    ).scalars().all()

    assert row == (True, True)
    assert policies == ["hermes_skill_drafts_org_isolation"]


async def test_another_organization_sees_no_drafts(db, test_org, test_user, bucket) -> None:  # noqa: ANN001
    await hermes_skill_drafts.submit(db, test_org.id, test_user.id, "research/arxiv", _files())
    await db.execute(text("SET LOCAL ROLE aurum_ai_app"))
    await db.execute(text("SELECT set_config('app.current_org_id', :org, true)"), {"org": str(uuid.uuid4())})

    assert (await db.execute(text("SELECT count(*) FROM hermes_skill_drafts"))).scalar() == 0


async def test_a_skill_is_drafted_once_and_a_change_replaces_its_open_draft(db, test_org, test_user, bucket) -> None:  # noqa: ANN001
    org, user = test_org.id, test_user.id

    first = await hermes_skill_drafts.submit(db, org, user, "research/arxiv", _files())
    again = await hermes_skill_drafts.submit(db, org, user, "research/arxiv", _files())
    changed = await hermes_skill_drafts.submit(db, org, user, "research/arxiv", _files("print('v2')\n"))
    [draft] = await _drafts(db, org)

    assert (first, again, changed) == ("drafted", "unchanged", "updated")
    assert (draft.name, draft.description, draft.file_count) == ("arxiv-digest", "Summarize new arXiv papers", 2)
    assert hashlib.sha256(bucket[draft.bundle_storage_key]).hexdigest() == draft.bundle_checksum
    assert draft.bundle_storage_key == f"skills/{org}/{draft.bundle_checksum}.zip"
    assert {item.path: item.content for item in hermes_skill_drafts.files_of(bucket[draft.bundle_storage_key])} == {
        "SKILL.md": SKILL_MD,
        "scripts/fetch.py": "print('v2')\n",
    }


@pytest.mark.parametrize(
    ("path", "files"),
    [
        ("../escape", _files()),
        (".hidden/skill", _files()),
        ("research/arxiv", [DraftFile("scripts/fetch.py", "x")]),
        ("research/arxiv", _files(skill_md="no frontmatter")),
        ("research/arxiv", [*_files(), DraftFile("../outside", "x")]),
        ("research/arxiv", [*_files(), DraftFile("big.txt", "x" * (MAX_FILE_BYTES + 1))]),
        ("research/arxiv", [*_files(), *(DraftFile(f"r/{index}.md", "x") for index in range(31))]),
    ],
)
async def test_what_is_not_a_bounded_text_skill_is_refused(db, test_org, test_user, bucket, path, files) -> None:  # noqa: ANN001
    with pytest.raises(DraftError) as refused:
        await hermes_skill_drafts.submit(db, test_org.id, test_user.id, path, files)

    assert refused.value.code == "hermes_skill_invalid"
    assert bucket == {} and await _drafts(db, test_org.id) == []


async def test_decisions_stick_while_the_sandbox_keeps_the_skill(db, test_org, test_user, bucket) -> None:  # noqa: ANN001
    org, user = test_org.id, test_user.id
    admin = AuthorizationSubject(user_id=(await _other_user(db, org, "org_admin")).id, org_id=org, role="org_admin")
    await hermes_skill_drafts.submit(db, org, user, "a", _files("a\n"))
    await hermes_skill_drafts.submit(db, org, user, "b", _files("b\n"))
    rejected, dismissed = sorted(await _drafts(db, org), key=lambda draft: draft.path)

    await hermes_skill_drafts.reject(db, admin, rejected.id, rejected.bundle_checksum)
    await hermes_skill_drafts.dismiss(db, org, user, dismissed.id)

    assert await hermes_skill_drafts.submit(db, org, user, "a", _files("a\n")) == "unchanged"
    assert await hermes_skill_drafts.submit(db, org, user, "b", _files("b\n")) == "unchanged"
    assert [draft.path for draft in await hermes_skill_drafts.list_own(db, org, user)] == ["a"]
    with pytest.raises(DraftError) as decided:
        await hermes_skill_drafts.dismiss(db, org, user, rejected.id)
    assert decided.value.code == "hermes_skill_draft_decided"
    with pytest.raises(DraftError) as twice:
        await hermes_skill_drafts.reject(db, admin, rejected.id, rejected.bundle_checksum)
    assert twice.value.code == "hermes_skill_draft_decided"


async def test_only_the_author_withdraws_a_draft(db, test_org, test_user, bucket) -> None:  # noqa: ANN001
    await hermes_skill_drafts.submit(db, test_org.id, test_user.id, "a", _files())
    [draft] = await _drafts(db, test_org.id)
    other = await _other_user(db, test_org.id)

    with pytest.raises(DraftError) as refused:
        await hermes_skill_drafts.dismiss(db, test_org.id, other.id, draft.id)

    assert refused.value.code == "hermes_skill_draft_not_found"


async def test_open_drafts_per_user_are_bounded(db, test_org, test_user, bucket, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(hermes_skill_drafts, "MAX_PENDING", 2)
    for name in ("a", "b"):
        await hermes_skill_drafts.submit(db, test_org.id, test_user.id, name, _files(name))

    with pytest.raises(DraftError) as limited:
        await hermes_skill_drafts.submit(db, test_org.id, test_user.id, "c", _files("c"))

    assert limited.value.code == "hermes_skill_draft_limit"
    assert await hermes_skill_drafts.submit(db, test_org.id, test_user.id, "a", _files("a2")) == "updated"


async def test_approval_makes_a_code_skill_and_a_later_version_updates_it(db, test_org, test_user, bucket) -> None:  # noqa: ANN001
    org, user = test_org.id, test_user.id
    admin = AuthorizationSubject(user_id=(await _other_user(db, org, "org_admin")).id, org_id=org, role="org_admin")
    await hermes_skill_drafts.submit(db, org, user, "research/arxiv", _files())
    [first] = await _drafts(db, org)

    approved = await hermes_skill_drafts.approve(db, admin, first.id, first.bundle_checksum, name="arXiv digest")
    skill = await db.get(Skill, approved.skill_id)
    grants = (
        await db.execute(
            select(AccessGrant.principal_id, AccessGrant.permission).where(AccessGrant.resource_id == str(skill.id))
        )
    ).all()

    assert (approved.status, approved.reviewed_by) == ("approved", admin.user_id)
    assert (skill.type, skill.name, skill.description, skill.org_id) == (
        "code", "arXiv digest", "Summarize new arXiv papers", org
    )
    assert (skill.bundle_storage_key, skill.bundle_checksum, skill.bundle_skill_name) == (
        first.bundle_storage_key, first.bundle_checksum, "arxiv-digest"
    )
    assert set(grants) == {(admin.user_id, "write"), (user, "read")}

    await hermes_skill_drafts.submit(db, org, user, "research/arxiv", _files("print('v2')\n"))
    second = next(draft for draft in await _drafts(db, org) if draft.status == "pending")
    updated = await hermes_skill_drafts.approve(db, admin, second.id, second.bundle_checksum)
    await db.refresh(skill)

    assert updated.skill_id == skill.id and skill.name == "arXiv digest"
    assert skill.bundle_checksum == second.bundle_checksum


async def test_a_bundle_changed_in_storage_is_not_approved(db, test_org, test_user, bucket) -> None:  # noqa: ANN001
    admin = AuthorizationSubject(user_id=test_user.id, org_id=test_org.id, role="org_admin")
    await hermes_skill_drafts.submit(db, test_org.id, test_user.id, "a", _files())
    [draft] = await _drafts(db, test_org.id)
    bucket[draft.bundle_storage_key] = b"something else"

    with pytest.raises(DraftError) as mismatch:
        await hermes_skill_drafts.approve(db, admin, draft.id, draft.bundle_checksum)

    assert mismatch.value.code == "hermes_skill_bundle_mismatch"


async def test_a_draft_changed_after_it_was_read_is_not_decided(db, test_org, test_user, bucket) -> None:  # noqa: ANN001
    org, user = test_org.id, test_user.id
    admin = AuthorizationSubject(user_id=(await _other_user(db, org, "org_admin")).id, org_id=org, role="org_admin")
    await hermes_skill_drafts.submit(db, org, user, "a", _files("v1\n"))
    [draft] = await _drafts(db, org)
    read = draft.bundle_checksum
    assert await hermes_skill_drafts.submit(db, org, user, "a", _files("v2\n")) == "updated"

    for decide in (hermes_skill_drafts.approve, hermes_skill_drafts.reject):
        with pytest.raises(DraftError) as changed:
            await decide(db, admin, draft.id, read)
        assert changed.value.code == "hermes_skill_draft_changed"
    await db.refresh(draft)

    assert draft.status == "pending" and draft.skill_id is None
    assert (await db.scalars(select(Skill).where(Skill.org_id == org))).all() == []
    approved = await hermes_skill_drafts.approve(db, admin, draft.id, draft.bundle_checksum)
    assert approved.status == "approved"


async def test_the_approval_grants_are_audited_as_an_administrators(db, test_org, test_user, bucket) -> None:  # noqa: ANN001
    from src.models.authorization_audit_event import AuthorizationAuditEvent

    admin = AuthorizationSubject(user_id=(await _other_user(db, test_org.id, "org_admin")).id, org_id=test_org.id, role="org_admin")
    await hermes_skill_drafts.submit(db, test_org.id, test_user.id, "a", _files())
    [draft] = await _drafts(db, test_org.id)

    approved = await hermes_skill_drafts.approve(db, admin, draft.id, draft.bundle_checksum)
    audits = (
        await db.scalars(select(AuthorizationAuditEvent).where(AuthorizationAuditEvent.resource_id == str(approved.skill_id)))
    ).all()

    assert len(audits) == 2 and {audit.actor_id for audit in audits} == {admin.user_id}


def _app(db, router, prefix: str, user: UserInfo) -> FastAPI:  # noqa: ANN001
    app = FastAPI()
    app.include_router(router, prefix=prefix)

    async def session():  # noqa: ANN202
        yield db

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_org_db_session] = session
    app.dependency_overrides[get_current_org_id] = lambda: uuid.UUID(user.org_id)
    return app


async def test_users_see_and_withdraw_their_drafts(db, test_org, test_user, bucket) -> None:  # noqa: ANN001
    from src.routers import hermes_skill_drafts as route

    await hermes_skill_drafts.submit(db, test_org.id, test_user.id, "research/arxiv", _files())
    member = UserInfo(user_id=str(test_user.id), org_id=str(test_org.id), role="member")
    app = _app(db, route.router, "/v1", member)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        [shown] = (await client.get("/v1/hermes/skill-drafts")).json()["data"]
        withdrawn = await client.delete(f"/v1/hermes/skill-drafts/{shown['id']}")
        again = await client.delete(f"/v1/hermes/skill-drafts/{shown['id']}")
        after = (await client.get("/v1/hermes/skill-drafts")).json()["data"]

    assert {key: shown[key] for key in ("path", "name", "status", "fileCount", "skillId")} == {
        "path": "research/arxiv", "name": "arxiv-digest", "status": "pending", "fileCount": 2, "skillId": None
    }
    assert withdrawn.status_code == 204 and again.status_code == 409 and after == []


async def test_administrators_review_every_file_before_approving(db, test_org, test_user, bucket) -> None:  # noqa: ANN001
    from src.routers import admin_hermes as route

    await hermes_skill_drafts.submit(db, test_org.id, test_user.id, "research/arxiv", _files())
    admin = UserInfo(user_id=str((await _other_user(db, test_org.id, "org_admin")).id), org_id=str(test_org.id), role="org_admin")
    member = UserInfo(user_id=str(test_user.id), org_id=str(test_org.id), role="member")
    author = test_user.email
    app = _app(db, route.router, "/v1/admin", admin)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        [listed] = (await client.get("/v1/admin/hermes/skill-drafts", params={"status": "pending"})).json()["data"]
        detail = (await client.get(f"/v1/admin/hermes/skill-drafts/{listed['id']}")).json()["data"]
        unread = await client.post(f"/v1/admin/hermes/skill-drafts/{listed['id']}/approve", json={})
        approved = await client.post(
            f"/v1/admin/hermes/skill-drafts/{listed['id']}/approve", json={"bundleChecksum": detail["bundleChecksum"]}
        )
        # Refusals roll the request back, here this test's own rows too: last.
        twice = await client.post(
            f"/v1/admin/hermes/skill-drafts/{listed['id']}/reject", json={"bundleChecksum": detail["bundleChecksum"]}
        )
        app.dependency_overrides[get_current_user] = lambda: member
        as_member = await client.get("/v1/admin/hermes/skill-drafts")

    assert listed["authorEmail"] == author
    assert {item["path"]: item["content"] for item in detail["files"]} == {
        "SKILL.md": SKILL_MD,
        "scripts/fetch.py": "print('papers')\n",
    }
    assert unread.status_code == 422
    assert approved.status_code == 200 and approved.json()["data"]["status"] == "approved"
    assert approved.json()["data"]["skillId"] is not None
    assert twice.status_code == 409 and as_member.status_code == 403
