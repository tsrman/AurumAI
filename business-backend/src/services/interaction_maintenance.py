"""Bounded runner lifecycle protection for durable user-question waits."""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.config import settings
from src.core.database import async_session
from src.core.logging import logger


class WaitingRuntime(Protocol):
    async def refresh_owned_waiting_runtime(
        self, conversation_id: str, user_id: str, org_id: str | None
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class ProtectedWait:
    org_id: UUID
    conversation_id: UUID
    owner_id: UUID
    status: str
    expires_at: datetime
    settlement_until: datetime
    reconciliation: bool


def wait_protected(
    status: str,
    expires_at: datetime,
    settlement_until: datetime,
    now: datetime,
    turn_active: bool,
) -> bool:
    """Return whether a question still grants a bounded runtime wait lease."""
    return turn_active and (
        (status == "pending" and now < expires_at)
        or (status == "answered" and now < settlement_until)
    )


async def load_protected_waits(
    session_factory: async_sessionmaker[AsyncSession] = async_session,
    conversation_id: str | None = None,
) -> list[ProtectedWait]:
    """Read cross-tenant waits through the narrow SECURITY DEFINER function."""
    async with session_factory.begin() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT * FROM public.protected_interaction_waits("
                    "CAST(:conversation_id AS uuid), :settlement_grace_seconds)"
                ),
                {
                    "conversation_id": conversation_id,
                    "settlement_grace_seconds": (
                        settings.INTERACTION_SETTLEMENT_GRACE_SECONDS
                    ),
                },
            )
        ).mappings()
        return [
            ProtectedWait(
                org_id=row["org_id"],
                conversation_id=row["conversation_id"],
                owner_id=row["owner_id"],
                status=row["question_status"],
                expires_at=row["expires_at"],
                settlement_until=row["settlement_until"],
                reconciliation=bool(row["reconciliation"]),
            )
            for row in rows
        ]


async def durable_wait_protected(
    org_id: str | None,
    conversation_id: str,
    session_factory: async_sessionmaker[AsyncSession] = async_session,
) -> bool:
    waits = await load_protected_waits(session_factory, conversation_id)
    return any(org_id is None or str(wait.org_id) == org_id for wait in waits)


async def maintain_interaction_waits(
    runtime: WaitingRuntime,
    session_factory: async_sessionmaker[AsyncSession] = async_session,
) -> int:
    """Keep each existing exact-owned waiting runtime alive once per pass."""
    waits = await load_protected_waits(session_factory)
    unique = {
        (str(wait.org_id), str(wait.conversation_id), str(wait.owner_id))
        for wait in waits
    }
    maintained = 0
    for org_id, conversation_id, owner_id in sorted(unique):
        try:
            if await runtime.refresh_owned_waiting_runtime(
                conversation_id, owner_id, org_id
            ):
                maintained += 1
        except Exception:
            logger.warning(
                "waiting runtime refresh failed (conversation_id=%s)",
                conversation_id,
                exc_info=True,
            )
    return maintained


async def scrub_expired_web_results(
    session_factory: async_sessionmaker[AsyncSession] = async_session,
    *,
    batch_size: int = 100,
) -> int:
    """Scrub normalized content cross-tenant through a bounded DB function."""
    if not 1 <= batch_size <= 1000:
        raise ValueError("batch_size out of range")
    async with session_factory.begin() as session:
        value = await session.scalar(
            text("SELECT public.scrub_expired_web_results(:batch_size)"),
            {"batch_size": batch_size},
        )
        return int(value or 0)


async def run_interaction_maintenance(runtime: WaitingRuntime) -> None:
    interval = settings.INTERACTION_MAINTENANCE_INTERVAL_SECONDS
    while True:
        try:
            await maintain_interaction_waits(runtime)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("interaction wait maintenance failed", exc_info=True)
        await asyncio.sleep(interval)


async def run_web_result_scrubber() -> None:
    """Scrub expired web cache independently of runtime wait maintenance."""
    interval = settings.INTERACTION_MAINTENANCE_INTERVAL_SECONDS
    while True:
        try:
            await scrub_expired_web_results()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("web result scrub maintenance failed", exc_info=True)
        await asyncio.sleep(interval)
