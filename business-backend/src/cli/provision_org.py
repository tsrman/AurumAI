"""Seed an existing organization's Authorization v2 defaults.

`sync_keycloak_user` provisions an organization the moment it creates its row,
so a stack brought up on an empty volume needs nothing from here. This exists
for the two cases that hook cannot cover:

* an organization created before provisioning existed — its members get 403 on
  /v1/chat/stream and an empty GET /v1/models until it is seeded;
* a global skill (`skills.org_id IS NULL`) added by an operator after those
  organizations already existed, which no per-organization hook will ever see.

Dry-run by default, like every other operator tool in this repo; `--apply`
persists. Each organization is handled in its own transaction, so a failure
part-way through leaves the ones already done intact rather than rolling the
whole fleet back.

Enumerating organizations (`--all`) needs a role that can see across tenants,
because `organizations` is FORCE RLS and the runtime role sees only the current
one. That single read uses APP_MIGRATION_DATABASE_URL (the same variable
entrypoint.sh hands Alembic), falling back to APP_DATABASE_URL. Every *write*
still goes through the runtime role with the org context set — the elevated URL
is never used to insert a grant.

    python -m src.cli.provision_org --org-id <uuid>            # show the plan
    python -m src.cli.provision_org --org-id <uuid> --apply
    python -m src.cli.provision_org --all --apply
"""

import argparse
import asyncio
import os
import sys
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.core.config import settings
from src.core.database import _AppSession
from src.models.organization import Organization
from src.services.organization_provisioning import organization_provisioning_service


def _elevated_url() -> str:
    return os.environ.get("APP_MIGRATION_DATABASE_URL") or settings.DATABASE_URL


async def _all_organization_ids() -> list[UUID]:
    engine = create_async_engine(_elevated_url())
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            rows = await session.execute(select(Organization.id).order_by(Organization.id))
            return list(rows.scalars())
    finally:
        await engine.dispose()


async def _provision_one(
    session_factory: async_sessionmaker[AsyncSession],
    org_id: UUID,
    *,
    apply: bool,
) -> None:
    async with session_factory() as session:
        # The org context normally comes from the request dependency's
        # session.info; a CLI has no request, so set it here — before the first
        # write, or every INSERT trips the RLS WITH CHECK.
        session.info["org_id"] = str(org_id)
        try:
            if not apply:
                plan = await organization_provisioning_service.plan(org_id)
                print(
                    f"{org_id}: would grant capabilities="
                    f"{[c.value for c in plan.capabilities]} "
                    f"models={len(plan.model_ids)} (dry run)"
                )
                return
            # Force a transaction so the after_begin hook applies the context
            # above even when provisioning turns out to write nothing.
            await session.execute(text("SELECT 1"))
            result = await organization_provisioning_service.provision(session, org_id)
            await session.commit()
            print(
                f"{org_id}: capabilities={[c.value for c in result.capabilities]} "
                f"global_skills={len(result.skill_ids)} models={len(result.model_ids)}"
            )
        finally:
            await session.rollback()


def _runtime_session_factory() -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """Sessions wired exactly like the backend's own: `_AppSession` so the
    after_begin hook turns session.info["org_id"] into the SET LOCAL that every
    RLS policy reads. A seam, so a test can point this at the real
    aurum_ai_app role instead of whatever DATABASE_URL happens to be."""
    engine = create_async_engine(settings.DATABASE_URL)
    return engine, async_sessionmaker(
        engine,
        class_=AsyncSession,
        sync_session_class=_AppSession,
        expire_on_commit=False,
    )


async def _run(org_ids: list[UUID], *, apply: bool) -> int:
    engine, session_factory = _runtime_session_factory()
    failures = 0
    try:
        for org_id in org_ids:
            try:
                await _provision_one(session_factory, org_id, apply=apply)
            except Exception as exc:  # noqa: BLE001 — one bad org must not stop the rest
                failures += 1
                print(f"{org_id}: FAILED — {exc}", file=sys.stderr)
    finally:
        await engine.dispose()
    if not apply:
        print("\nDry run — nothing was written. Re-run with --apply.", file=sys.stderr)
    return 1 if failures else 0


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="provision_org",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--org-id", action="append", type=UUID, help="organization UUID (repeatable)"
    )
    target.add_argument(
        "--all", action="store_true", help="every organization in the database"
    )
    parser.add_argument("--apply", action="store_true", help="persist (default: dry run)")
    return parser.parse_args(argv)


async def run(argv: list[str] | None = None) -> int:
    """Awaitable entry point. `main` is the sync wrapper the module runs under;
    tests drive this one, because asyncio.run cannot nest inside a running loop."""
    args = _parse(argv)
    org_ids: list[UUID] = (
        await _all_organization_ids() if args.all else list(args.org_id or [])
    )
    if not org_ids:
        print("No organizations to provision.", file=sys.stderr)
        return 0
    return await _run(org_ids, apply=args.apply)


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(argv))


if __name__ == "__main__":
    raise SystemExit(main())
