"""Atomic, bounded partition maintenance through a vetted database routine."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class PartitionMaintenanceBusy(RuntimeError):
    """A live writer owns a required relation; retry after its transaction ends."""


async def maintain_partitions(session: AsyncSession) -> int:
    """Create UTC months and drain at most 10,000 default rows per parent.

    The caller owns commit/rollback. The definer routine uses an isolated NOLOGIN
    owner; application connections must not run as a superuser or BYPASSRLS role.
    """
    privileged = await session.scalar(
        text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user")
    )
    if privileged:
        raise ValueError("Partition maintenance requires a restricted application connection")
    result = await session.scalar(text("SELECT public.platform_maintain_partitions()"))
    if result is None:
        raise PartitionMaintenanceBusy("Partition maintenance deferred by a live writer; retry")
    return int(result)


async def partition_status(session: AsyncSession) -> list[dict[str, object]]:
    """Read-only metadata; contains no tenant payload or relation-name input."""
    rows = (
        await session.execute(
            text("""
        SELECT p.relname AS parent, count(*) AS partitions,
               bool_and(c.relrowsecurity AND c.relforcerowsecurity) AS secured
        FROM pg_inherits i JOIN pg_class p ON p.oid=i.inhparent
        JOIN pg_class c ON c.oid=i.inhrelid
        WHERE p.oid IN ('public.conversations'::regclass,'public.messages'::regclass,
                       'public.audit_logs'::regclass)
        GROUP BY p.relname ORDER BY p.relname
    """)
        )
    ).mappings()
    return [dict(row) for row in rows]
