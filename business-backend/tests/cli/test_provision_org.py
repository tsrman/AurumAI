"""The operator entry point for seeding an organization that already exists.

These build their own engine instead of taking the `db` fixture: that fixture
keeps everything inside one connection's transaction and rolls it back, so rows
it "commits" are invisible to the separate engine the CLI opens — which is the
whole point of the CLI. Same reason `tests/test_rls.py` does it this way.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.cli import provision_org
from src.core.config import settings
from src.services.organization_provisioning import organization_provisioning_service


@pytest.fixture(autouse=True)
def deterministic_catalog(monkeypatch):
    async def catalog() -> list[dict[str, object]]:
        return [{"id": "gpt-4o-mini"}]

    monkeypatch.setattr(organization_provisioning_service, "_model_catalog", catalog)


@pytest.fixture
async def orgs():
    """Two committed organizations, removed again afterwards."""
    engine = create_async_engine(settings.DATABASE_URL)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    ids = [uuid.uuid4(), uuid.uuid4()]
    async with sessionmaker() as session:
        await session.execute(
            text("INSERT INTO organizations (id, name, slug) VALUES (:id, :name, :slug)"),
            [{"id": org_id, "name": "CLI Org", "slug": f"cli-{org_id}"} for org_id in ids],
        )
        await session.commit()
    try:
        yield ids
    finally:
        async with sessionmaker() as session:
            await session.execute(
                text("DELETE FROM access_grants WHERE org_id = ANY(:ids)"), {"ids": ids}
            )
            await session.execute(
                text("DELETE FROM organization_capabilities WHERE org_id = ANY(:ids)"),
                {"ids": ids},
            )
            await session.execute(
                text("DELETE FROM organizations WHERE id = ANY(:ids)"), {"ids": ids}
            )
            await session.commit()
        await engine.dispose()


async def _seeded(org_id: uuid.UUID) -> tuple[set[str], set[str]]:
    engine = create_async_engine(settings.DATABASE_URL)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            capabilities = (
                await session.execute(
                    text(
                        "SELECT capability FROM organization_capabilities "
                        "WHERE org_id = :org"
                    ),
                    {"org": org_id},
                )
            ).scalars().all()
            grants = (
                await session.execute(
                    text(
                        "SELECT resource_id FROM access_grants "
                        "WHERE org_id = :org AND resource_type = 'model'"
                    ),
                    {"org": org_id},
                )
            ).scalars().all()
        return set(capabilities), set(grants)
    finally:
        await engine.dispose()


async def test_dry_run_reports_the_plan_and_writes_nothing(orgs, capsys) -> None:
    assert await provision_org.run(["--org-id", str(orgs[0])]) == 0

    captured = capsys.readouterr()
    assert "chat.send" in captured.out
    assert "Dry run" in captured.err
    assert await _seeded(orgs[0]) == (set(), set())


async def test_apply_seeds_capabilities_and_model_grants(orgs, capsys) -> None:
    assert await provision_org.run(["--org-id", str(orgs[0]), "--apply"]) == 0

    assert await _seeded(orgs[0]) == ({"chat.send"}, {"gpt-4o-mini"})
    assert str(orgs[0]) in capsys.readouterr().out


async def test_apply_is_repeatable(orgs) -> None:
    """An operator re-running this after adding a global skill must not trip the
    unique constraint on rows the first run already wrote."""
    assert await provision_org.run(["--org-id", str(orgs[0]), "--apply"]) == 0
    assert await provision_org.run(["--org-id", str(orgs[0]), "--apply"]) == 0

    assert await _seeded(orgs[0]) == ({"chat.send"}, {"gpt-4o-mini"})


async def test_several_organizations_in_one_run(orgs) -> None:
    assert (
        await provision_org.run(
            ["--org-id", str(orgs[0]), "--org-id", str(orgs[1]), "--apply"]
        )
        == 0
    )

    assert await _seeded(orgs[0]) == ({"chat.send"}, {"gpt-4o-mini"})
    assert await _seeded(orgs[1]) == ({"chat.send"}, {"gpt-4o-mini"})


async def test_all_finds_every_organization_including_pre_existing_ones(
    orgs, capsys
) -> None:
    """The reason --all exists: organizations created before provisioning did
    are exactly the ones no per-request hook will ever revisit. Asserted in dry
    run — an --apply here would seed every organization the shared test database
    happens to contain."""
    assert await provision_org.run(["--all"]) == 0

    printed = capsys.readouterr().out
    assert str(orgs[0]) in printed
    assert str(orgs[1]) in printed
    assert await _seeded(orgs[0]) == (set(), set())


async def test_an_unknown_organization_fails_that_one_and_not_the_run(
    orgs, capsys
) -> None:
    missing = uuid.uuid4()

    exit_code = await provision_org.run(
        ["--org-id", str(missing), "--org-id", str(orgs[0]), "--apply"]
    )

    assert exit_code == 1
    assert str(missing) in capsys.readouterr().err
    # The organization that does exist still got its defaults.
    assert await _seeded(orgs[0]) == ({"chat.send"}, {"gpt-4o-mini"})


async def test_requires_an_explicit_target() -> None:
    with pytest.raises(SystemExit):
        await provision_org.run([])
