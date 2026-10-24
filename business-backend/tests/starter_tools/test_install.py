"""Installer behavior against a dedicated migrated database and actual runtime RLS."""

import importlib
import uuid

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.core.config import settings
from src.core.database import _AppSession
from src.models.access_grant import AccessGrant
from src.models.authorization_audit_event import AuthorizationAuditEvent
from src.models.organization import Organization
from src.models.organization_capability import OrganizationCapability
from src.models.skill import Skill


class MemoryBundles:
    def __init__(self):
        self.objects = {}
        self.puts = 0

    async def put_bundle(self, key, data):
        self.puts += 1
        if key in self.objects and self.objects[key] != data:
            raise ValueError("bundle collision")
        self.objects[key] = data


@pytest.fixture
async def owned_database(monkeypatch):
    admin_engine = create_async_engine(settings.DATABASE_URL)
    app_engine = create_async_engine(
        settings.DATABASE_URL,
        connect_args={"server_settings": {"role": "aurum_ai_app"}},
    )
    admin = async_sessionmaker(admin_engine, expire_on_commit=False)
    runtime = async_sessionmaker(app_engine, sync_session_class=_AppSession, expire_on_commit=False)
    org_ids = [uuid.uuid4(), uuid.uuid4()]
    module = importlib.import_module("src.services.starter_tools")
    monkeypatch.setattr(module, "STARTER_NAMESPACE", uuid.uuid4())
    skill_ids = [entry.id for entry in module.starter_catalog()]
    try:
        async with admin() as session:
            session.add_all([
                Organization(id=value, name="Starter test", slug=f"starter-test-{value}")
                for value in org_ids
            ])
            await session.commit()
        yield module, admin, runtime, org_ids, skill_ids
    finally:
        async with admin() as session:
            await session.execute(delete(AccessGrant).where(AccessGrant.org_id.in_(org_ids)))
            await session.execute(delete(AuthorizationAuditEvent).where(
                AuthorizationAuditEvent.org_id.in_(org_ids)))
            await session.execute(delete(Skill).where(Skill.id.in_(skill_ids)))
            await session.execute(delete(Organization).where(Organization.id.in_(org_ids)))
            await session.commit()
        await admin_engine.dispose()
        await app_engine.dispose()


async def install(fixture, storage, org_ids=None, apply=True):
    module, _, runtime, owned_orgs, _ = fixture
    async with runtime() as session:
        result = await module.install_starter_tools(
            session, storage, organization_ids=owned_orgs[:1] if org_ids is None else org_ids,
            apply=apply,
        )
        await session.commit()
        return result


async def test_dry_run_writes_no_rows_or_objects(owned_database):
    storage = MemoryBundles()
    await install(owned_database, storage, apply=False)
    _, admin, _, _, skill_ids = owned_database
    async with admin() as session:
        assert not (await session.scalars(select(Skill).where(Skill.id.in_(skill_ids)))).all()
        assert not (await session.scalars(select(AccessGrant).where(AccessGrant.org_id.in_(owned_database[3])))).all()
    assert storage.puts == 0


async def test_repeat_install_preserves_edits_and_revoked_grants(owned_database):
    storage = MemoryBundles()
    await install(owned_database, storage)
    _, admin, _, org_ids, skill_ids = owned_database
    async with admin() as session:
        skill = await session.get(Skill, skill_ids[0])
        skill.name = "Operator name"
        skill.description = "Operator instructions"
        other = await session.get(Skill, skill_ids[-1])
        other.mcp_endpoint = "https://operator.example/sse"
        other.mcp_auth_token = "test-only-operator-token"
        await session.execute(delete(AccessGrant).where(
            AccessGrant.org_id == org_ids[0], AccessGrant.resource_id == str(skill_ids[0])))
        await session.commit()
    await install(owned_database, storage)
    async with admin() as session:
        assert (await session.get(Skill, skill_ids[0])).name == "Operator name"
        assert (await session.get(Skill, skill_ids[0])).description == "Operator instructions"
        other = await session.get(Skill, skill_ids[-1])
        assert other.mcp_endpoint == "https://operator.example/sse"
        assert other.mcp_auth_token == "test-only-operator-token"
        grants = (await session.scalars(select(AccessGrant).where(AccessGrant.org_id.in_(owned_database[3])))).all()
        assert len(grants) == 3
        assert all(grant.org_id == org_ids[0] and grant.resource_type == "skill" for grant in grants)
        assert not (await session.scalars(select(OrganizationCapability).where(OrganizationCapability.org_id.in_(owned_database[3])))).all()
        assert len((await session.scalars(select(AuthorizationAuditEvent).where(AuthorizationAuditEvent.org_id.in_(owned_database[3])))).all()) == 4
    assert storage.puts == 2


async def test_explicit_new_target_gets_tools_without_models_or_capabilities(owned_database):
    storage = MemoryBundles()
    await install(owned_database, storage, org_ids=[])
    await install(owned_database, storage, org_ids=owned_database[3][1:])
    _, admin, runtime, org_ids, _ = owned_database
    async with admin() as session:
        grants = (await session.scalars(select(AccessGrant).where(AccessGrant.org_id.in_(owned_database[3])))).all()
        assert len(grants) == 4
        assert {grant.org_id for grant in grants} == {org_ids[1]}
        assert {grant.resource_type for grant in grants} == {"skill"}
        assert not (await session.scalars(select(OrganizationCapability).where(OrganizationCapability.org_id.in_(owned_database[3])))).all()
    async with runtime() as session:
        session.info["org_id"] = str(org_ids[0])
        assert not (await session.scalars(select(AccessGrant).where(AccessGrant.org_id.in_(owned_database[3])))).all()


@pytest.mark.parametrize("foreign_scope", ["global", "tenant"])
async def test_foreign_identity_collision_fails_before_upload(owned_database, foreign_scope):
    _, admin, _, org_ids, skill_ids = owned_database
    async with admin() as session:
        session.add(Skill(
            id=skill_ids[-1], org_id=None if foreign_scope == "global" else org_ids[1],
            name="Unrelated resource", type="mcp",
        ))
        await session.commit()
    storage = MemoryBundles()
    with pytest.raises(ValueError, match="identity collision"):
        await install(owned_database, storage)
    assert storage.puts == 0
    async with admin() as session:
        assert len((await session.scalars(select(Skill).where(Skill.id.in_(owned_database[4])))).all()) == 1


async def test_missing_organization_rolls_back_catalog_and_grants(owned_database):
    with pytest.raises(ValueError, match="organization"):
        await install(owned_database, MemoryBundles(), org_ids=[uuid.uuid4()])
    async with owned_database[1]() as session:
        assert not (await session.scalars(select(Skill).where(Skill.id.in_(owned_database[4])))).all()


async def test_cli_refuses_privileged_database_role(owned_database, monkeypatch, capsys):
    cli = importlib.import_module("src.cli.install_starter_tools")
    engine = create_async_engine(settings.DATABASE_URL)
    monkeypatch.setattr(cli, "_runtime_session_factory", lambda: (
        engine, async_sessionmaker(engine, sync_session_class=_AppSession)))
    assert await cli.run(["--apply"]) == 1
    assert "runtime" in capsys.readouterr().err.lower()


async def test_new_org_uses_existing_global_provisioning(owned_database):
    from src.services.skill_grant_provisioning import skill_grant_provisioning_service
    await install(owned_database, MemoryBundles(), org_ids=[])
    _, _, runtime, org_ids, skill_ids = owned_database
    async with runtime() as session:
        session.info["org_id"] = str(org_ids[1])
        result = await skill_grant_provisioning_service.provision_organization(session, org_ids[1])
        assert set(result) == set(skill_ids)
        assert len((await session.scalars(select(AccessGrant).where(AccessGrant.org_id.in_(owned_database[3])))).all()) == 4
        assert (await session.execute(text("SELECT current_user"))).scalar_one() == "aurum_ai_app"


async def test_concurrent_installs_share_identities_and_one_provisioning_receipt(owned_database):
    import asyncio
    storage = MemoryBundles()
    await asyncio.gather(install(owned_database, storage), install(owned_database, storage))
    _, admin, _, org_ids, skill_ids = owned_database
    async with admin() as session:
        assert len((await session.scalars(select(Skill).where(Skill.id.in_(skill_ids)))).all()) == 4
        assert len((await session.scalars(select(AuthorizationAuditEvent).where(
            AuthorizationAuditEvent.org_id == org_ids[0]))).all()) == 4
    assert storage.puts == 2


async def test_installer_restores_callers_transaction_org_context(owned_database):
    module, _, runtime, org_ids, _ = owned_database
    async with runtime() as session:
        session.info["org_id"] = str(org_ids[1])
        await module.install_starter_tools(
            session, MemoryBundles(), organization_ids=org_ids[:1], apply=True)
        assert (await session.execute(text(
            "SELECT current_setting('app.current_org_id', true)"))).scalar_one() == str(org_ids[1])


async def test_cli_apply_uses_runtime_role_and_real_transaction(owned_database, monkeypatch):
    cli = importlib.import_module("src.cli.install_starter_tools")
    engine = create_async_engine(
        settings.DATABASE_URL, connect_args={"server_settings": {"role": "aurum_ai_app"}})
    monkeypatch.setattr(cli, "_runtime_session_factory", lambda: (
        engine, async_sessionmaker(engine, sync_session_class=_AppSession)))
    storage = MemoryBundles()
    monkeypatch.setattr(cli, "StarterBundleStorage", lambda: storage)
    assert await cli.run(["--apply", "--org-id", str(owned_database[3][0])]) == 0
    assert storage.puts == 2
    async with owned_database[1]() as session:
        grants = (await session.scalars(select(AccessGrant).where(
            AccessGrant.org_id == owned_database[3][0]))).all()
        assert len(grants) == 4


async def test_receipt_records_existing_read_grant_without_claiming_a_new_grant(owned_database):
    from src.services.skill_grant_provisioning import skill_grant_provisioning_service
    await install(owned_database, MemoryBundles(), org_ids=[])
    module, admin, runtime, org_ids, skill_ids = owned_database
    async with runtime() as session:
        session.info["org_id"] = str(org_ids[0])
        await skill_grant_provisioning_service.provision_organization(session, org_ids[0])
        await session.commit()
    await install(owned_database, MemoryBundles())
    async with admin() as session:
        receipts = (await session.scalars(select(AuthorizationAuditEvent).where(
            AuthorizationAuditEvent.org_id == org_ids[0]))).all()
        assert len(receipts) == 4
        expected = [{"principal_type": "organization", "principal_id": str(org_ids[0]), "permission": "read"}]
        assert all(receipt.before_state == expected and receipt.after_state == expected
                   for receipt in receipts)
