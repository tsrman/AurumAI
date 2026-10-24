"""Install bounded global starter tools; dry-run unless --apply is supplied.

Uses only APP_DATABASE_URL and the normal object-storage settings. Explicit
organization targets receive tool read grants only; model grants, membership,
organization defaults and capabilities are never provisioned here.
"""

import argparse
import asyncio
import sys
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from src.core.config import settings
from src.core.database import _AppSession
from src.services.starter_tools import (
    DEFAULT_MCP_ENDPOINT,
    StarterBundleStorage,
    install_starter_tools,
    starter_catalog,
)


def _runtime_session_factory() -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(settings.DATABASE_URL, hide_parameters=True)
    return engine, async_sessionmaker(
        engine, sync_session_class=_AppSession, expire_on_commit=False,
    )


async def _require_runtime_role(session: AsyncSession) -> None:
    unsafe = (await session.execute(text(
        "SELECT r.rolsuper OR r.rolbypassrls OR "
        "pg_has_role(current_user, c.relowner, 'USAGE') "
        "FROM pg_roles r CROSS JOIN pg_class c "
        "WHERE r.rolname = current_user AND c.oid = 'public.skills'::regclass"
    ))).scalar_one()
    if unsafe:
        raise ValueError("starter installer requires a runtime role without schema ownership or RLS bypass")


async def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="persist (default: dry run)")
    parser.add_argument("--org-id", action="append", type=UUID, default=[], help="tool-only target (repeatable)")
    parser.add_argument("--mcp-endpoint", default=DEFAULT_MCP_ENDPOINT, help="credential-free SSE URL")
    args = parser.parse_args(argv)
    try:
        catalog = starter_catalog(args.mcp_endpoint)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for tool in catalog:
        print(f"{tool.key}: {tool.id} checksum={tool.checksum or 'not-applicable'}")
    if not args.apply:
        print(f"Dry run: ensure four global tools; tool-only targets={len(set(args.org_id))}. Nothing written.")
        return 0
    engine, sessions = _runtime_session_factory()
    try:
        async with sessions() as session:
            await _require_runtime_role(session)
            await install_starter_tools(
                session, StarterBundleStorage(), organization_ids=args.org_id,
                mcp_endpoint=args.mcp_endpoint, apply=True,
            )
            await session.commit()
        print("Starter tools installed; existing definitions and prior provisioning receipts preserved.")
        return 0
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception:
        # Driver/object-store exceptions can contain credentials or request data.
        print("Starter installation failed; no database changes committed. Check operator configuration and storage availability.", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(argv))


if __name__ == "__main__":
    raise SystemExit(main())
