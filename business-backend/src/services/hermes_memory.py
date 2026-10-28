"""Hermes Agent's per-user memory (docs/HERMES_AGENT.md, phase 5a).

The session workflow writes it into the sandbox before each turn and reads
it back after; what comes back is the sandbox's, so it is cut to Hermes's
limits at an entry boundary and stored only on the version it was read
at. A missing row is version 0. Clearing keeps the rows, empty and on a new
version, so a read-back from before the clear is refused instead of
restoring what was cleared.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.hermes_memory import HermesMemory

# Hermes's memory_char_limit and user_char_limit, set in the sandbox's config.
LIMITS = {"memory": 2200, "user": 1375}
ENTRY_DELIMITER = "\n§\n"


class MemoryConflict(Exception):
    """The memory changed since the sandbox read it."""


@dataclass(frozen=True)
class Memory:
    content: str
    version: int


def bounded(kind: str, content: str) -> str:
    """Within the kind's limit, cut at the last whole entry."""
    text = content.replace("\x00", "").strip()
    limit = LIMITS[kind]
    if len(text) <= limit:
        return text
    entries, kept = text.split(ENTRY_DELIMITER), list[str]()
    for entry in entries:
        if len(ENTRY_DELIMITER.join([*kept, entry])) > limit:
            break
        kept.append(entry)
    return ENTRY_DELIMITER.join(kept) if kept else text[:limit]


async def read(session: AsyncSession, org_id: UUID, user_id: UUID) -> dict[str, Memory]:
    rows = (
        await session.scalars(
            select(HermesMemory).where(HermesMemory.org_id == org_id, HermesMemory.user_id == user_id)
        )
    ).all()
    found = {row.kind: Memory(row.content, row.version) for row in rows}
    return {kind: found.get(kind, Memory("", 0)) for kind in LIMITS}


async def write(
    session: AsyncSession, org_id: UUID, user_id: UUID, kind: str, content: str, base_version: int
) -> int:
    """Store the content if the memory is still on `base_version`; the new version."""
    content = bounded(kind, content)
    if base_version == 0:
        inserted = await session.execute(
            insert(HermesMemory)
            .values(org_id=org_id, user_id=user_id, kind=kind, content=content, version=1)
            .on_conflict_do_nothing()
            .returning(HermesMemory.version)
        )
        version = inserted.scalar_one_or_none()
    else:
        updated = await session.execute(
            update(HermesMemory)
            .where(
                HermesMemory.org_id == org_id,
                HermesMemory.user_id == user_id,
                HermesMemory.kind == kind,
                HermesMemory.version == base_version,
            )
            .values(content=content, version=HermesMemory.version + 1)
            .returning(HermesMemory.version)
        )
        version = updated.scalar_one_or_none()
    if version is None:
        raise MemoryConflict()
    return int(version)


async def clear(session: AsyncSession, org_id: UUID, user_id: UUID) -> None:
    await session.execute(
        update(HermesMemory)
        .where(HermesMemory.org_id == org_id, HermesMemory.user_id == user_id)
        .values(content="", version=HermesMemory.version + 1)
    )
