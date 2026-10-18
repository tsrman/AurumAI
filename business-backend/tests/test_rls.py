import os
import uuid
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse, urlunparse

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool

from src.core.config import settings
from src.cli import provision_org
from src.core.database import _AppSession
from src.models.skill import Skill
from src.services.organization_provisioning import OrganizationProvisioningService
from src.services.skill_grant_provisioning import skill_grant_provisioning_service
from src.models.organization import Organization
from src.models.user import User
from src.services.quota import QuotaService


pytestmark = pytest.mark.asyncio


def _runtime_db_url() -> str:
    parsed = urlparse(settings.DATABASE_URL)
    password = os.environ.get("APP_DATABASE_ROLE_PASSWORD", "aurum_ai_app")
    return urlunparse(
        parsed._replace(
            netloc=f"aurum_ai_app:{password}@{parsed.hostname}:{parsed.port or 5432}"
        )
    )


async def test_rls_blocks_cross_org_as_app_role():
    admin_engine = create_async_engine(settings.DATABASE_URL)
    admin_sessionmaker = async_sessionmaker(admin_engine, expire_on_commit=False)

    org_a = uuid.uuid4()
    org_b = uuid.uuid4()
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    conv_a = uuid.uuid4()
    intruder_user = uuid.uuid4()

    async with admin_sessionmaker() as session:
        await session.execute(
            text(
                "INSERT INTO organizations (id, name, slug) VALUES (:id, :name, :slug)"
            ),
            [
                {
                    "id": str(org_a),
                    "name": "RLS Org A",
                    "slug": f"rls-org-a-{org_a}",
                },
                {
                    "id": str(org_b),
                    "name": "RLS Org B",
                    "slug": f"rls-org-b-{org_b}",
                },
            ],
        )
        await session.execute(
            text(
                "INSERT INTO users (id, keycloak_id, email, org_id) "
                "VALUES (:id, :keycloak_id, :email, :org_id)"
            ),
            [
                {
                    "id": str(user_a),
                    "keycloak_id": str(uuid.uuid4()),
                    "email": "rls-a@example.com",
                    "org_id": str(org_a),
                },
                {
                    "id": str(user_b),
                    "keycloak_id": str(uuid.uuid4()),
                    "email": "rls-b@example.com",
                    "org_id": str(org_b),
                },
            ],
        )
        await session.execute(
            text(
                "INSERT INTO conversations (id, user_id, org_id, title, model) "
                "VALUES (:id, :user_id, :org_id, :title, :model)"
            ),
            {
                "id": str(conv_a),
                "user_id": str(user_a),
                "org_id": str(org_a),
                "title": "Org A conversation",
                "model": "gpt-4",
            },
        )
        await session.commit()

    app_engine = create_async_engine(_runtime_db_url())
    app_sessionmaker = async_sessionmaker(app_engine, expire_on_commit=False)

    try:
        # Org A sees only org_a's user and conversation.
        async with app_sessionmaker() as session:
            await session.execute(
                text("SELECT set_config('app.current_org_id', :id, false)"),
                {"id": str(org_a)},
            )
            result = await session.execute(
                text("SELECT id, email FROM users ORDER BY email")
            )
            rows = result.mappings().all()

        assert len(rows) == 1
        assert str(rows[0]["id"]) == str(user_a)
        assert rows[0]["email"] == "rls-a@example.com"

        # Org B sees only org_b's user.
        async with app_sessionmaker() as session:
            await session.execute(
                text("SELECT set_config('app.current_org_id', :id, false)"),
                {"id": str(org_b)},
            )
            result = await session.execute(
                text("SELECT id, email FROM users ORDER BY email")
            )
            rows = result.mappings().all()

        assert len(rows) == 1
        assert str(rows[0]["id"]) == str(user_b)
        assert rows[0]["email"] == "rls-b@example.com"

        # Conversations are isolated across orgs.
        async with app_sessionmaker() as session:
            await session.execute(
                text("SELECT set_config('app.current_org_id', :id, false)"),
                {"id": str(org_b)},
            )
            result = await session.execute(
                text("SELECT id FROM conversations")
            )
            rows = result.mappings().all()

        assert len(rows) == 0

        # Inserting a row for a different org is rejected by the WITH CHECK policy.
        with pytest.raises(ProgrammingError):
            async with app_sessionmaker() as session:
                await session.execute(
                    text("SELECT set_config('app.current_org_id', :id, false)"),
                    {"id": str(org_a)},
                )
                await session.execute(
                    text(
                        "INSERT INTO users (id, keycloak_id, email, org_id) "
                        "VALUES (:id, :keycloak_id, :email, :org_id)"
                    ),
                    {
                        "id": str(intruder_user),
                        "keycloak_id": str(uuid.uuid4()),
                        "email": "rls-intruder@example.com",
                        "org_id": str(org_b),
                    },
                )
                await session.commit()
    finally:
        await app_engine.dispose()

        async with admin_sessionmaker() as session:
            await session.execute(
                text("DELETE FROM conversations WHERE id = :id"),
                {"id": str(conv_a)},
            )
            await session.execute(
                text("DELETE FROM users WHERE id IN (:a, :b, :c)"),
                {"a": str(user_a), "b": str(user_b), "c": str(intruder_user)},
            )
            await session.execute(
                text("DELETE FROM organizations WHERE id IN (:a, :b)"),
                {"a": str(org_a), "b": str(org_b)},
            )
            await session.commit()

    await admin_engine.dispose()


async def test_rls_blocks_cross_org_organizations_as_app_role():
    """Regression test: organizations and
    organization_settings were left out of both the org-isolation policy
    migration and the FORCE ROW LEVEL SECURITY grant, so the aurum_ai_app
    runtime role (full DML) could read/write every org's row under a bug
    that skipped setting app.current_org_id."""
    admin_engine = create_async_engine(settings.DATABASE_URL)
    admin_sessionmaker = async_sessionmaker(admin_engine, expire_on_commit=False)

    org_a = uuid.uuid4()
    org_b = uuid.uuid4()
    # No organization_settings row of its own — used only to exercise the
    # INSERT/WITH CHECK violation below without colliding with org_b's PK.
    org_c = uuid.uuid4()

    async with admin_sessionmaker() as session:
        await session.execute(
            text(
                "INSERT INTO organizations (id, name, slug) VALUES (:id, :name, :slug)"
            ),
            [
                {"id": str(org_a), "name": "RLS Org A2", "slug": f"rls-org-a2-{org_a}"},
                {"id": str(org_b), "name": "RLS Org B2", "slug": f"rls-org-b2-{org_b}"},
                {"id": str(org_c), "name": "RLS Org C2", "slug": f"rls-org-c2-{org_c}"},
            ],
        )
        await session.execute(
            text(
                "INSERT INTO organization_settings (org_id, default_model) "
                "VALUES (:org_id, :model)"
            ),
            [
                {"org_id": str(org_a), "model": "gpt-4o"},
                {"org_id": str(org_b), "model": "claude-3-5-sonnet"},
            ],
        )
        await session.commit()

    app_engine = create_async_engine(_runtime_db_url())
    app_sessionmaker = async_sessionmaker(app_engine, expire_on_commit=False)

    try:
        async with app_sessionmaker() as session:
            await session.execute(
                text("SELECT set_config('app.current_org_id', :id, false)"),
                {"id": str(org_a)},
            )
            orgs = (await session.execute(text("SELECT id FROM organizations"))).mappings().all()
            settings_rows = (
                (await session.execute(text("SELECT org_id FROM organization_settings")))
                .mappings()
                .all()
            )

        assert [str(r["id"]) for r in orgs] == [str(org_a)]
        assert [str(r["org_id"]) for r in settings_rows] == [str(org_a)]

        # Cross-org update is invisible under RLS's USING clause — 0 rows
        # affected, not an error (unlike INSERT, which hits WITH CHECK).
        async with app_sessionmaker() as session:
            await session.execute(
                text("SELECT set_config('app.current_org_id', :id, false)"),
                {"id": str(org_a)},
            )
            result = await session.execute(
                text("UPDATE organizations SET name = 'hijacked' WHERE id = :id"),
                {"id": str(org_b)},
            )
            assert result.rowcount == 0
            await session.commit()

        # An INSERT for a different (but real, FK-valid) org is rejected
        # outright by the WITH CHECK policy.
        with pytest.raises(ProgrammingError):
            async with app_sessionmaker() as session:
                await session.execute(
                    text("SELECT set_config('app.current_org_id', :id, false)"),
                    {"id": str(org_a)},
                )
                await session.execute(
                    text(
                        "INSERT INTO organization_settings (org_id, default_model) "
                        "VALUES (:org_id, :model)"
                    ),
                    {"org_id": str(org_c), "model": "gpt-4o"},
                )
                await session.commit()
    finally:
        await app_engine.dispose()

        async with admin_sessionmaker() as session:
            await session.execute(
                text("DELETE FROM organization_settings WHERE org_id IN (:a, :b, :c)"),
                {"a": str(org_a), "b": str(org_b), "c": str(org_c)},
            )
            await session.execute(
                text("DELETE FROM organizations WHERE id IN (:a, :b, :c)"),
                {"a": str(org_a), "b": str(org_b), "c": str(org_c)},
            )
            await session.commit()

    await admin_engine.dispose()


async def test_org_context_survives_a_connection_pool_swap_across_commit():
    """Regression test for the 'Could not refresh instance' bug found live
    while testing the security review's M5/M9 fix (chat.py) and again in
    auth_service.py::sync_keycloak_user: a pooled AsyncSession's *physical*
    connection can change out from under it right after commit() —
    Session.commit() releases the connection back to the pool, and the
    session's next statement (e.g. session.refresh(), a real query under
    expire_on_commit=False) can be handed a different connection, one not
    running under this request's app.current_org_id RLS context. Fixed via
    database.py's _AppSession/after_begin, which re-issues the GUC (as
    SET LOCAL) on every new transaction regardless of which physical
    connection it lands on.

    NullPool here forces a genuinely new physical connection on every
    checkout — the worst case the bug depended on, made deterministic
    instead of a matter of luck/concurrent load.
    """
    engine = create_async_engine(_runtime_db_url(), poolclass=NullPool)
    sessionmaker = async_sessionmaker(
        engine, class_=AsyncSession, sync_session_class=_AppSession, expire_on_commit=False
    )

    org_id = uuid.uuid4()
    user_id = uuid.uuid4()

    try:
        async with sessionmaker() as session:
            session.info["org_id"] = str(org_id)
            session.add(
                Organization(id=org_id, name="Pool Swap Org", slug=f"pool-swap-{org_id}")
            )
            await session.flush()
            user = User(
                id=user_id,
                keycloak_id=uuid.uuid4(),
                email="pool-swap@example.com",
                org_id=org_id,
            )
            session.add(user)
            # NullPool: the physical connection behind `session` is closed
            # and discarded right here, not just returned to a pool.
            await session.commit()

            # Before the fix: sqlalchemy.exc.InvalidRequestError("Could not
            # refresh instance") — this SELECT ran on a brand-new
            # connection with no app.current_org_id set, so RLS's USING
            # clause made the just-committed row invisible.
            await session.refresh(user)
            assert user.email == "pool-swap@example.com"
    finally:
        await engine.dispose()
        admin_engine = create_async_engine(settings.DATABASE_URL)
        async with async_sessionmaker(admin_engine, expire_on_commit=False)() as session:
            await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": str(user_id)})
            await session.execute(
                text("DELETE FROM organizations WHERE id = :id"), {"id": str(org_id)}
            )
            await session.commit()
        await admin_engine.dispose()


async def test_llm_usage_tables_are_forced_rls_and_org_isolated() -> None:
    admin_engine = create_async_engine(settings.DATABASE_URL)
    admin_sessionmaker = async_sessionmaker(admin_engine, expire_on_commit=False)
    app_engine = create_async_engine(_runtime_db_url())
    app_sessionmaker = async_sessionmaker(app_engine, expire_on_commit=False)

    org_a = uuid.uuid4()
    org_b = uuid.uuid4()
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    event_a = uuid.uuid4()
    event_b = uuid.uuid4()
    period_start = date(2026, 8, 1)

    try:
        async with admin_sessionmaker() as session:
            await session.execute(
                text("INSERT INTO organizations (id, name, slug) VALUES (:id, :name, :slug)"),
                [
                    {"id": org_a, "name": "Usage RLS A", "slug": f"usage-rls-a-{org_a}"},
                    {"id": org_b, "name": "Usage RLS B", "slug": f"usage-rls-b-{org_b}"},
                ],
            )
            await session.execute(
                text(
                    "INSERT INTO users (id, keycloak_id, email, org_id) "
                    "VALUES (:id, :keycloak_id, :email, :org_id)"
                ),
                [
                    {
                        "id": user_a,
                        "keycloak_id": uuid.uuid4(),
                        "email": f"usage-rls-a-{user_a}@example.com",
                        "org_id": org_a,
                    },
                    {
                        "id": user_b,
                        "keycloak_id": uuid.uuid4(),
                        "email": f"usage-rls-b-{user_b}@example.com",
                        "org_id": org_b,
                    },
                ],
            )
            await session.execute(
                text(
                    "INSERT INTO organization_usage_monthly "
                    "(org_id, period_start, requests_used) "
                    "VALUES (:org_id, :period_start, 1)"
                ),
                [
                    {"org_id": org_a, "period_start": period_start},
                    {"org_id": org_b, "period_start": period_start},
                ],
            )
            await session.execute(
                text(
                    "INSERT INTO llm_usage_events "
                    "(id, org_id, user_id, request_id, period_start, model, status) "
                    "VALUES (:id, :org_id, :user_id, :request_id, :period_start, "
                    "'gpt-test', 'accepted')"
                ),
                [
                    {
                        "id": event_a,
                        "org_id": org_a,
                        "user_id": user_a,
                        "request_id": uuid.uuid4(),
                        "period_start": period_start,
                    },
                    {
                        "id": event_b,
                        "org_id": org_b,
                        "user_id": user_b,
                        "request_id": uuid.uuid4(),
                        "period_start": period_start,
                    },
                ],
            )
            await session.commit()

        async with app_sessionmaker() as session:
            await session.execute(
                text("SELECT set_config('app.current_org_id', :id, false)"),
                {"id": str(org_a)},
            )
            aggregate_orgs = (
                await session.execute(text("SELECT org_id FROM organization_usage_monthly"))
            ).scalars().all()
            event_orgs = (
                await session.execute(text("SELECT org_id FROM llm_usage_events"))
            ).scalars().all()

            assert aggregate_orgs == [org_a]
            assert event_orgs == [org_a]

            result = await session.execute(
                text(
                    "UPDATE organization_usage_monthly SET requests_used = 99 "
                    "WHERE org_id = :org_id"
                ),
                {"org_id": org_b},
            )
            assert result.rowcount == 0
            await session.rollback()

        with pytest.raises(ProgrammingError):
            async with app_sessionmaker() as session:
                await session.execute(
                    text("SELECT set_config('app.current_org_id', :id, false)"),
                    {"id": str(org_a)},
                )
                await session.execute(
                    text(
                        "INSERT INTO organization_usage_monthly "
                        "(org_id, period_start) VALUES (:org_id, '2026-09-01')"
                    ),
                    {"org_id": org_b},
                )

        async with admin_sessionmaker() as session:
            security = (
                await session.execute(
                    text(
                        "SELECT relname, relrowsecurity, relforcerowsecurity "
                        "FROM pg_class WHERE relname IN "
                        "('llm_usage_events', 'organization_usage_monthly') "
                        "ORDER BY relname"
                    )
                )
            ).all()
            grants = set(
                (
                    await session.execute(
                        text(
                            "SELECT table_name, privilege_type "
                            "FROM information_schema.role_table_grants "
                            "WHERE grantee = 'aurum_ai_app' AND table_name IN "
                            "('llm_usage_events', 'organization_usage_monthly')"
                        )
                    )
                ).all()
            )

        assert security == [
            ("llm_usage_events", True, True),
            ("organization_usage_monthly", True, True),
        ]
        assert grants == {
            (table_name, privilege)
            for table_name in ("llm_usage_events", "organization_usage_monthly")
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE")
        }
    finally:
        await app_engine.dispose()
        async with admin_sessionmaker() as session:
            await session.execute(
                text("DELETE FROM llm_usage_events WHERE org_id IN (:a, :b)"),
                {"a": org_a, "b": org_b},
            )
            await session.execute(
                text("DELETE FROM organization_usage_monthly WHERE org_id IN (:a, :b)"),
                {"a": org_a, "b": org_b},
            )
            await session.execute(
                text("DELETE FROM users WHERE id IN (:a, :b)"),
                {"a": user_a, "b": user_b},
            )
            await session.execute(
                text("DELETE FROM organizations WHERE id IN (:a, :b)"),
                {"a": org_a, "b": org_b},
            )
            await session.commit()
        await admin_engine.dispose()


async def test_stale_reservation_sweep_runs_under_the_rls_constrained_app_role() -> None:
    """The sweep is the one quota mutation the service tests cannot vouch for:
    they connect as the migration superuser, who bypasses RLS entirely, while
    production runs as aurum_ai_app under FORCE ROW LEVEL SECURITY. A DELETE
    that the policy or the grants refused would leave the fix green in tests
    and dead where it matters — and it would fail silently, because the sweep
    only ever reports how many rows it reclaimed."""
    admin_engine = create_async_engine(settings.DATABASE_URL)
    admin_sessionmaker = async_sessionmaker(admin_engine, expire_on_commit=False)
    app_engine = create_async_engine(_runtime_db_url())
    # Same wiring as production's async_session: the after_begin hook turns
    # session.info["org_id"] into the SET LOCAL that the org policy reads.
    app_sessionmaker = async_sessionmaker(
        app_engine, sync_session_class=_AppSession, expire_on_commit=False
    )

    org_id = uuid.uuid4()
    user_id = uuid.uuid4()
    stale_id = uuid.uuid4()
    live_id = uuid.uuid4()
    period_start = date(2026, 8, 1)
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)

    try:
        async with admin_sessionmaker() as session:
            await session.execute(
                text("INSERT INTO organizations (id, name, slug) VALUES (:id, :name, :slug)"),
                {"id": org_id, "name": "Sweep RLS", "slug": f"sweep-rls-{org_id}"},
            )
            await session.execute(
                text(
                    "INSERT INTO users (id, keycloak_id, email, org_id) "
                    "VALUES (:id, :keycloak_id, :email, :org_id)"
                ),
                {
                    "id": user_id,
                    "keycloak_id": uuid.uuid4(),
                    "email": f"sweep-rls-{user_id}@example.com",
                    "org_id": org_id,
                },
            )
            await session.execute(
                text(
                    "INSERT INTO organization_usage_monthly "
                    "(org_id, period_start, requests_used) "
                    "VALUES (:org_id, :period_start, 2)"
                ),
                {"org_id": org_id, "period_start": period_start},
            )
            await session.execute(
                text(
                    "INSERT INTO llm_usage_events "
                    "(id, org_id, user_id, request_id, period_start, model, status, "
                    "created_at) VALUES (:id, :org_id, :user_id, :request_id, "
                    ":period_start, 'gpt-test', 'reserved', :created_at)"
                ),
                [
                    {
                        "id": stale_id,
                        "org_id": org_id,
                        "user_id": user_id,
                        "request_id": uuid.uuid4(),
                        "period_start": period_start,
                        "created_at": now - timedelta(hours=1),
                    },
                    {
                        "id": live_id,
                        "org_id": org_id,
                        "user_id": user_id,
                        "request_id": uuid.uuid4(),
                        "period_start": period_start,
                        "created_at": now,
                    },
                ],
            )
            await session.commit()

        service = QuotaService(app_sessionmaker)
        snapshot = await service.preflight(org_id, now=now)

        async with admin_sessionmaker() as session:
            remaining = (
                await session.execute(
                    text("SELECT id FROM llm_usage_events WHERE org_id = :org_id"),
                    {"org_id": org_id},
                )
            ).scalars().all()

        assert snapshot.org_requests_used == 1
        assert remaining == [live_id]
    finally:
        await app_engine.dispose()
        async with admin_sessionmaker() as session:
            await session.execute(
                text("DELETE FROM llm_usage_events WHERE org_id = :id"), {"id": org_id}
            )
            await session.execute(
                text("DELETE FROM organization_usage_monthly WHERE org_id = :id"),
                {"id": org_id},
            )
            await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
            await session.execute(
                text("DELETE FROM organizations WHERE id = :id"), {"id": org_id}
            )
            await session.commit()
        await admin_engine.dispose()


async def test_authorization_v2_tables_are_forced_rls_and_org_isolated() -> None:
    admin_engine = create_async_engine(settings.DATABASE_URL)
    admin_sessionmaker = async_sessionmaker(admin_engine, expire_on_commit=False)
    app_engine = create_async_engine(_runtime_db_url())
    app_sessionmaker = async_sessionmaker(app_engine, expire_on_commit=False)

    org_a = uuid.uuid4()
    org_b = uuid.uuid4()
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    group_a = uuid.uuid4()
    group_b = uuid.uuid4()
    grant_a = uuid.uuid4()
    grant_b = uuid.uuid4()
    audit_a = uuid.uuid4()
    audit_b = uuid.uuid4()

    try:
        async with admin_sessionmaker() as session:
            await session.execute(
                text("INSERT INTO organizations (id, name, slug) VALUES (:id, :name, :slug)"),
                [
                    {"id": org_a, "name": "Authorization RLS A", "slug": f"auth-rls-a-{org_a}"},
                    {"id": org_b, "name": "Authorization RLS B", "slug": f"auth-rls-b-{org_b}"},
                ],
            )
            await session.execute(
                text(
                    "INSERT INTO users (id, keycloak_id, email, org_id, role) "
                    "VALUES (:id, :keycloak_id, :email, :org_id, 'member')"
                ),
                [
                    {
                        "id": user_a,
                        "keycloak_id": uuid.uuid4(),
                        "email": f"auth-rls-a-{user_a}@example.com",
                        "org_id": org_a,
                    },
                    {
                        "id": user_b,
                        "keycloak_id": uuid.uuid4(),
                        "email": f"auth-rls-b-{user_b}@example.com",
                        "org_id": org_b,
                    },
                ],
            )
            await session.execute(
                text("INSERT INTO groups (id, org_id, name) VALUES (:id, :org_id, :name)"),
                [
                    {"id": group_a, "org_id": org_a, "name": f"auth-a-{group_a}"},
                    {"id": group_b, "org_id": org_b, "name": f"auth-b-{group_b}"},
                ],
            )
            await session.execute(
                text(
                    "INSERT INTO organization_capabilities (org_id, capability) "
                    "VALUES (:org_id, 'chat.send')"
                ),
                [{"org_id": org_a}, {"org_id": org_b}],
            )
            await session.execute(
                text(
                    "INSERT INTO group_capabilities (group_id, capability) "
                    "VALUES (:group_id, 'workspace.skills.create')"
                ),
                [{"group_id": group_a}, {"group_id": group_b}],
            )
            await session.execute(
                text(
                    "INSERT INTO access_grants "
                    "(id, org_id, resource_type, resource_id, principal_type, "
                    "principal_id, permission, created_via) "
                    "VALUES (:id, :org_id, 'skill', :resource_id, 'user', "
                    ":principal_id, 'read', 'provisioning')"
                ),
                [
                    {
                        "id": grant_a,
                        "org_id": org_a,
                        "resource_id": str(uuid.uuid4()),
                        "principal_id": user_a,
                    },
                    {
                        "id": grant_b,
                        "org_id": org_b,
                        "resource_id": str(uuid.uuid4()),
                        "principal_id": user_b,
                    },
                ],
            )
            await session.execute(
                text(
                    "INSERT INTO authorization_audit_events "
                    "(id, org_id, actor_id, action, resource_type, resource_id, "
                    "before_state, after_state, correlation_id) VALUES "
                    "(:id, :org_id, :actor_id, 'create', 'skill', :resource_id, "
                    "'[]'::jsonb, '[]'::jsonb, :correlation_id)"
                ),
                [
                    {
                        "id": audit_a,
                        "org_id": org_a,
                        "actor_id": user_a,
                        "resource_id": str(uuid.uuid4()),
                        "correlation_id": f"req-{audit_a}",
                    },
                    {
                        "id": audit_b,
                        "org_id": org_b,
                        "actor_id": user_b,
                        "resource_id": str(uuid.uuid4()),
                        "correlation_id": f"req-{audit_b}",
                    },
                ],
            )
            await session.commit()

        async with app_sessionmaker() as session:
            await session.execute(
                text("SELECT set_config('app.current_org_id', :id, false)"),
                {"id": str(org_a)},
            )
            organization_capabilities = (
                await session.execute(text("SELECT org_id FROM organization_capabilities"))
            ).scalars().all()
            group_capabilities = (
                await session.execute(text("SELECT group_id FROM group_capabilities"))
            ).scalars().all()
            grants = (
                await session.execute(text("SELECT id FROM access_grants"))
            ).scalars().all()
            audit_events = (
                await session.execute(text("SELECT id FROM authorization_audit_events"))
            ).scalars().all()
            assert organization_capabilities == [org_a]
            assert group_capabilities == [group_a]
            assert grants == [grant_a]
            assert audit_events == [audit_a]

            update_result = await session.execute(
                text(
                    "UPDATE access_grants SET permission = 'write' "
                    "WHERE id = :grant_id"
                ),
                {"grant_id": grant_b},
            )
            assert update_result.rowcount == 0
            await session.rollback()

        with pytest.raises(ProgrammingError):
            async with app_sessionmaker() as session:
                await session.execute(
                    text("SELECT set_config('app.current_org_id', :id, false)"),
                    {"id": str(org_a)},
                )
                await session.execute(
                    text(
                        "INSERT INTO organization_capabilities (org_id, capability) "
                        "VALUES (:org_id, 'chat.send')"
                    ),
                    {"org_id": org_b},
                )

        async with admin_sessionmaker() as session:
            security = (
                await session.execute(
                    text(
                        "SELECT relname, relrowsecurity, relforcerowsecurity "
                        "FROM pg_class WHERE relname IN "
                        "('organization_capabilities', 'group_capabilities', 'access_grants', "
                        "'authorization_audit_events') "
                        "ORDER BY relname"
                    )
                )
            ).all()
            grants = set(
                (
                    await session.execute(
                        text(
                            "SELECT table_name, privilege_type "
                            "FROM information_schema.role_table_grants "
                            "WHERE grantee = 'aurum_ai_app' AND table_name IN "
                            "('organization_capabilities', 'group_capabilities', 'access_grants', "
                            "'authorization_audit_events')"
                        )
                    )
                ).all()
            )

        assert security == [
            ("access_grants", True, True),
            ("authorization_audit_events", True, True),
            ("group_capabilities", True, True),
            ("organization_capabilities", True, True),
        ]
        assert grants == {
            (table_name, privilege)
            for table_name in (
                "organization_capabilities",
                "group_capabilities",
                "access_grants",
                "authorization_audit_events",
            )
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE")
        }
    finally:
        await app_engine.dispose()
        async with admin_sessionmaker() as session:
            await session.execute(
                text("DELETE FROM authorization_audit_events WHERE org_id IN (:a, :b)"),
                {"a": org_a, "b": org_b},
            )
            await session.execute(
                text("DELETE FROM access_grants WHERE org_id IN (:a, :b)"),
                {"a": org_a, "b": org_b},
            )
            await session.execute(
                text("DELETE FROM group_capabilities WHERE group_id IN (:a, :b)"),
                {"a": group_a, "b": group_b},
            )
            await session.execute(
                text("DELETE FROM organization_capabilities WHERE org_id IN (:a, :b)"),
                {"a": org_a, "b": org_b},
            )
            await session.execute(
                text("DELETE FROM groups WHERE id IN (:a, :b)"),
                {"a": group_a, "b": group_b},
            )
            await session.execute(
                text("DELETE FROM users WHERE id IN (:a, :b)"),
                {"a": user_a, "b": user_b},
            )
            await session.execute(
                text("DELETE FROM organizations WHERE id IN (:a, :b)"),
                {"a": org_a, "b": org_b},
            )
            await session.commit()
        await admin_engine.dispose()


async def test_organization_provisioning_runs_under_the_rls_constrained_app_role() -> None:
    """Provisioning is what makes a new organization usable at all, and every
    row it writes is subject to `access_grants`/`organization_capabilities`
    FORCE RLS. The service tests connect as the migration superuser, who
    bypasses RLS entirely — exactly how a cross-tenant write that production
    refuses stayed green for a whole release. This runs it through the same
    wiring the backend uses: the aurum_ai_app role, org context supplied by
    the after_begin hook."""
    admin_engine = create_async_engine(settings.DATABASE_URL)
    admin_sessionmaker = async_sessionmaker(admin_engine, expire_on_commit=False)
    app_engine = create_async_engine(_runtime_db_url())
    app_sessionmaker = async_sessionmaker(
        app_engine, sync_session_class=_AppSession, expire_on_commit=False
    )

    org_id = uuid.uuid4()
    other_org_id = uuid.uuid4()
    skill_id = uuid.uuid4()

    async def catalog() -> list[dict[str, object]]:
        return [{"id": "gpt-4o-mini"}, {"id": "openai/text-embedding-3-small"}]

    service = OrganizationProvisioningService(model_catalog=catalog)

    try:
        async with admin_sessionmaker() as session:
            await session.execute(
                text("INSERT INTO organizations (id, name, slug) VALUES (:id, :name, :slug)"),
                [
                    {"id": org_id, "name": "Provision RLS", "slug": f"prov-rls-{org_id}"},
                    {
                        "id": other_org_id,
                        "name": "Provision RLS Other",
                        "slug": f"prov-rls-other-{other_org_id}",
                    },
                ],
            )
            await session.execute(
                text("INSERT INTO skills (id, org_id, name, type) VALUES (:id, NULL, :name, 'tool')"),
                {"id": skill_id, "name": f"global-rls-{skill_id}"},
            )
            await session.commit()

        async with app_sessionmaker() as session:
            session.info["org_id"] = str(org_id)
            result = await service.provision(session, org_id)
            await session.commit()

        assert result.model_ids == ("gpt-4o-mini",)
        assert skill_id in result.skill_ids

        async with admin_sessionmaker() as session:
            capabilities = (
                await session.execute(
                    text(
                        "SELECT capability FROM organization_capabilities WHERE org_id = :org"
                    ),
                    {"org": org_id},
                )
            ).scalars().all()
            grants = (
                await session.execute(
                    text(
                        "SELECT resource_type, resource_id FROM access_grants "
                        "WHERE org_id = :org"
                    ),
                    {"org": org_id},
                )
            ).all()
        assert set(capabilities) == {"chat.send"}
        assert set(grants) == {("model", "gpt-4o-mini"), ("skill", str(skill_id))}

        # The operator fan-out writes rows for an organization other than the
        # one the session started on — the case the RLS WITH CHECK rejects
        # unless app.current_org_id is switched per organization.
        async with app_sessionmaker() as session:
            session.info["org_id"] = str(org_id)
            skill = await session.get(Skill, skill_id)
            assert skill is not None
            published = await skill_grant_provisioning_service.publish_global_skill(
                session, skill, organization_ids=[other_org_id]
            )
            await session.commit()
        assert published == (other_org_id,)

        async with admin_sessionmaker() as session:
            fanned_out = (
                await session.execute(
                    text(
                        "SELECT resource_id FROM access_grants WHERE org_id = :org "
                        "AND resource_type = 'skill'"
                    ),
                    {"org": other_org_id},
                )
            ).scalars().all()
        assert list(fanned_out) == [str(skill_id)]
    finally:
        await app_engine.dispose()
        async with admin_sessionmaker() as session:
            await session.execute(
                text("DELETE FROM access_grants WHERE org_id IN (:a, :b)"),
                {"a": org_id, "b": other_org_id},
            )
            await session.execute(
                text("DELETE FROM organization_capabilities WHERE org_id IN (:a, :b)"),
                {"a": org_id, "b": other_org_id},
            )
            await session.execute(
                text("DELETE FROM skills WHERE id = :id"), {"id": skill_id}
            )
            await session.execute(
                text("DELETE FROM organizations WHERE id IN (:a, :b)"),
                {"a": org_id, "b": other_org_id},
            )
            await session.commit()
        await admin_engine.dispose()


async def test_provision_org_cli_writes_under_the_rls_constrained_app_role(
    monkeypatch,
) -> None:
    """The CLI's own claim is that every write goes through aurum_ai_app with
    the org context set — but its tests connect as the migration superuser, who
    bypasses RLS entirely. That is exactly the shape that let a cross-tenant
    write stay green for a release, so point the CLI's session factory at the
    real runtime role and drive it end to end."""
    admin_engine = create_async_engine(settings.DATABASE_URL)
    admin_sessionmaker = async_sessionmaker(admin_engine, expire_on_commit=False)
    app_engine = create_async_engine(_runtime_db_url())

    def runtime_factory():
        return app_engine, async_sessionmaker(
            app_engine, sync_session_class=_AppSession, expire_on_commit=False
        )

    monkeypatch.setattr(provision_org, "_runtime_session_factory", runtime_factory)

    async def catalog() -> list[dict[str, object]]:
        return [{"id": "gpt-4o-mini"}]

    monkeypatch.setattr(
        provision_org.organization_provisioning_service, "_model_catalog", catalog
    )

    org_id = uuid.uuid4()
    try:
        async with admin_sessionmaker() as session:
            await session.execute(
                text("INSERT INTO organizations (id, name, slug) VALUES (:id, :name, :slug)"),
                {"id": org_id, "name": "CLI RLS", "slug": f"cli-rls-{org_id}"},
            )
            await session.commit()

        assert await provision_org.run(["--org-id", str(org_id), "--apply"]) == 0

        async with admin_sessionmaker() as session:
            capabilities = (
                await session.execute(
                    text(
                        "SELECT capability FROM organization_capabilities WHERE org_id = :org"
                    ),
                    {"org": org_id},
                )
            ).scalars().all()
            models = (
                await session.execute(
                    text(
                        "SELECT resource_id FROM access_grants "
                        "WHERE org_id = :org AND resource_type = 'model'"
                    ),
                    {"org": org_id},
                )
            ).scalars().all()
        assert set(capabilities) == {"chat.send"}
        assert set(models) == {"gpt-4o-mini"}
    finally:
        # The engine is disposed by _run; recreate nothing, just clean up.
        async with admin_sessionmaker() as session:
            await session.execute(
                text("DELETE FROM access_grants WHERE org_id = :org"), {"org": org_id}
            )
            await session.execute(
                text("DELETE FROM organization_capabilities WHERE org_id = :org"),
                {"org": org_id},
            )
            await session.execute(
                text("DELETE FROM organizations WHERE id = :org"), {"org": org_id}
            )
            await session.commit()
        await admin_engine.dispose()
