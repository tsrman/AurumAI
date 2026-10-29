"""Skills Hermes Agent writes for itself, held as drafts until an administrator
approves them (docs/HERMES_AGENT.md, phase 5b).

After every turn the session workflow reads the skills the sandbox holds
beyond the image's bundled ones and submits each here. Everything in them is
the agent's, so a draft is only text files within fixed bounds, every file is
shown to the reviewer, and nothing runs until an approval turns the draft into
an ordinary code skill under the usual bundle validation and grants.

Drafts are deduplicated per user by their content: a skill seen again, after
any decision, is not drafted again, so a rejection or a dismissal sticks while
the sandbox keeps the skill. A changed skill replaces its directory's open
draft. Approving a later version of an approved skill updates that skill.
Bundles are stored like uploaded ones, by checksum under `skills/{org}/`;
like a replaced bundle, a declined draft's object stays.
"""

import hashlib
import io
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.authorization_types import AccessPermission, AuthorizationSubject, PrincipalType, ResourceRef, ResourceType
from src.models.hermes_skill_draft import HermesSkillDraft
from src.models.skill import Skill
from src.services.grant_administration import GrantSpec, grant_administration_service
from src.services.object_storage import object_storage
from src.services.skill_bundle import SkillBundleError, validate_and_parse

# hermes-relay's skills-read keeps to the same bounds.
MAX_FILES = 32
MAX_FILE_BYTES = 64 * 1024
MAX_SKILL_BYTES = 96 * 1024
MAX_PENDING = 50
_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MAX_DEPTH = 4
# Fixed, so the same files always make the same bundle.
_ZIP_DATE = (1980, 1, 1, 0, 0, 0)


class DraftError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class DraftFile:
    path: str
    content: str


def _safe_path(path: str, depth: int) -> bool:
    parts = path.split("/")
    return 0 < len(parts) <= depth and len(path) <= 200 and all(_PART.match(part) for part in parts)


def _checked(path: str, files: list[DraftFile]) -> list[tuple[str, bytes]]:
    if not _safe_path(path, _MAX_DEPTH):
        raise DraftError("hermes_skill_invalid")
    if not files or len(files) > MAX_FILES:
        raise DraftError("hermes_skill_invalid")
    encoded = sorted((item.path, item.content.encode()) for item in files)
    names = [name for name, _ in encoded]
    if len(set(names)) != len(names) or "SKILL.md" not in names:
        raise DraftError("hermes_skill_invalid")
    for name, data in encoded:
        if not _safe_path(name, _MAX_DEPTH) or len(data) > MAX_FILE_BYTES or "\x00" in name:
            raise DraftError("hermes_skill_invalid")
    if sum(len(data) for _, data in encoded) > MAX_SKILL_BYTES:
        raise DraftError("hermes_skill_invalid")
    return encoded


def content_sha256(encoded: list[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    for name, data in encoded:
        digest.update(f"{name}\0{len(data)}\0".encode())
        digest.update(data)
    return digest.hexdigest()


def bundle(encoded: list[tuple[str, bytes]]) -> bytes:
    out = io.BytesIO()
    # Stored, not deflated: drafts are small, and the bundle validation
    # refuses archives that compress too well.
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as archive:
        for name, data in encoded:
            info = zipfile.ZipInfo(name, date_time=_ZIP_DATE)
            info.external_attr = 0o644 << 16
            archive.writestr(info, data)
    return out.getvalue()


def files_of(raw: bytes) -> list[DraftFile]:
    """The bundle's files, for review; drafts only ever hold text."""
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        return [
            DraftFile(info.filename, archive.read(info).decode("utf-8", errors="replace"))
            for info in archive.infolist()
            if not info.is_dir()
        ]


async def submit(
    session: AsyncSession, org_id: UUID, user_id: UUID, path: str, files: list[DraftFile]
) -> str:
    """`drafted`, `updated` or `unchanged`; raises DraftError."""
    encoded = _checked(path, files)
    content = content_sha256(encoded)
    seen = await session.scalar(
        select(HermesSkillDraft.id).where(
            HermesSkillDraft.org_id == org_id,
            HermesSkillDraft.user_id == user_id,
            HermesSkillDraft.content_sha256 == content,
        )
    )
    if seen is not None:
        return "unchanged"
    raw = bundle(encoded)
    try:
        parsed = validate_and_parse(raw)
    except SkillBundleError:
        raise DraftError("hermes_skill_invalid") from None
    open_draft = await session.scalar(
        select(HermesSkillDraft)
        .where(
            HermesSkillDraft.org_id == org_id,
            HermesSkillDraft.user_id == user_id,
            HermesSkillDraft.path == path,
            HermesSkillDraft.status == "pending",
        )
        .with_for_update()
    )
    if open_draft is None:
        pending = await session.scalar(
            select(func.count()).where(
                HermesSkillDraft.org_id == org_id,
                HermesSkillDraft.user_id == user_id,
                HermesSkillDraft.status == "pending",
            )
        )
        if (pending or 0) >= MAX_PENDING:
            raise DraftError("hermes_skill_draft_limit")
    key = f"skills/{org_id}/{parsed.checksum}.zip"
    await object_storage.put_bytes(key, raw)
    values = {
        "name": parsed.skill_name[:255],
        "description": parsed.skill_description,
        "content_sha256": content,
        "file_count": len(encoded),
        "bundle_storage_key": key,
        "bundle_checksum": parsed.checksum,
        "bundle_size_bytes": parsed.size_bytes,
    }
    try:
        async with session.begin_nested():
            if open_draft is None:
                session.add(HermesSkillDraft(org_id=org_id, user_id=user_id, path=path, status="pending", **values))
            else:
                for field, value in values.items():
                    setattr(open_draft, field, value)
            await session.flush()
    except IntegrityError:
        # Another turn of the user's submitted at the same moment; the next
        # turn submits again if it still differs.
        return "unchanged"
    return "drafted" if open_draft is None else "updated"


async def list_own(session: AsyncSession, org_id: UUID, user_id: UUID) -> list[HermesSkillDraft]:
    return list(
        (
            await session.scalars(
                select(HermesSkillDraft)
                .where(
                    HermesSkillDraft.org_id == org_id,
                    HermesSkillDraft.user_id == user_id,
                    HermesSkillDraft.status != "dismissed",
                )
                .order_by(HermesSkillDraft.updated_at.desc())
            )
        ).all()
    )


async def dismiss(session: AsyncSession, org_id: UUID, user_id: UUID, draft_id: UUID) -> None:
    """The author withdraws an open draft. The row stays, so the same skill
    is not drafted again on the next turn."""
    draft = await _get(session, org_id, draft_id, for_update=True)
    if draft is None or draft.user_id != user_id:
        raise DraftError("hermes_skill_draft_not_found")
    if draft.status != "pending":
        raise DraftError("hermes_skill_draft_decided")
    draft.status = "dismissed"
    await session.flush()


async def list_for_review(session: AsyncSession, org_id: UUID, status: str | None) -> list[HermesSkillDraft]:
    query = select(HermesSkillDraft).where(HermesSkillDraft.org_id == org_id, HermesSkillDraft.status != "dismissed")
    if status is not None:
        query = query.where(HermesSkillDraft.status == status)
    return list((await session.scalars(query.order_by(HermesSkillDraft.updated_at.desc()).limit(200))).all())


async def _get(
    session: AsyncSession, org_id: UUID, draft_id: UUID, *, for_update: bool = False
) -> HermesSkillDraft | None:
    query = select(HermesSkillDraft).where(HermesSkillDraft.org_id == org_id, HermesSkillDraft.id == draft_id)
    if for_update:
        query = query.with_for_update()
    draft: HermesSkillDraft | None = await session.scalar(query)
    return draft


async def get_for_review(session: AsyncSession, org_id: UUID, draft_id: UUID) -> HermesSkillDraft:
    draft = await _get(session, org_id, draft_id)
    if draft is None or draft.status == "dismissed":
        raise DraftError("hermes_skill_draft_not_found")
    return draft


async def review_files(draft: HermesSkillDraft) -> list[DraftFile]:
    raw = await object_storage.get_bytes(draft.bundle_storage_key)
    if hashlib.sha256(raw).hexdigest() != draft.bundle_checksum:
        raise DraftError("hermes_skill_bundle_mismatch")
    return files_of(raw)


async def _pending(session: AsyncSession, org_id: UUID, draft_id: UUID, reviewed: str) -> HermesSkillDraft:
    """The open draft, still the version the reviewer read: a later turn
    replaces an open draft's files in place."""
    draft = await _get(session, org_id, draft_id, for_update=True)
    if draft is None or draft.status == "dismissed":
        raise DraftError("hermes_skill_draft_not_found")
    if draft.status != "pending":
        raise DraftError("hermes_skill_draft_decided")
    if draft.bundle_checksum != reviewed:
        raise DraftError("hermes_skill_draft_changed")
    return draft


async def _approved_skill(session: AsyncSession, draft: HermesSkillDraft) -> Skill | None:
    """The skill an earlier version of the same skill directory became."""
    skill_id = await session.scalar(
        select(HermesSkillDraft.skill_id)
        .where(
            HermesSkillDraft.org_id == draft.org_id,
            HermesSkillDraft.user_id == draft.user_id,
            HermesSkillDraft.path == draft.path,
            HermesSkillDraft.status == "approved",
            HermesSkillDraft.skill_id.is_not(None),
        )
        .order_by(HermesSkillDraft.reviewed_at.desc())
        .limit(1)
    )
    if skill_id is None:
        return None
    skill: Skill | None = await session.scalar(select(Skill).where(Skill.id == skill_id, Skill.org_id == draft.org_id))
    return skill


async def approve(
    session: AsyncSession,
    reviewer: AuthorizationSubject,
    draft_id: UUID,
    reviewed: str,
    *,
    name: str | None = None,
    description: str | None = None,
) -> HermesSkillDraft:
    """The draft becomes an ordinary code skill, or the new bundle of the skill
    its directory became before. A new skill is the reviewer's, as an uploaded
    one would be, and its author may use it; other access is granted as usual.
    `reviewed` is the bundle checksum of the version the reviewer read."""
    draft = await _pending(session, reviewer.org_id, draft_id, reviewed)
    await review_files(draft)
    bundle_fields = {
        "bundle_storage_key": draft.bundle_storage_key,
        "bundle_checksum": draft.bundle_checksum,
        "bundle_size_bytes": draft.bundle_size_bytes,
        "bundle_skill_name": draft.name[:255],
    }
    skill = await _approved_skill(session, draft)
    if skill is not None:
        for field, value in bundle_fields.items():
            setattr(skill, field, value)
        if name:
            skill.name = name
        if description:
            skill.description = description
    else:
        skill = Skill(
            org_id=reviewer.org_id,
            name=name or draft.name[:255],
            description=description or draft.description,
            type="code",
            input_schema={},
            **bundle_fields,
        )
        session.add(skill)
        await session.flush()
        grants = [(reviewer.user_id, AccessPermission.WRITE)]
        if draft.user_id != reviewer.user_id:
            grants.append((draft.user_id, AccessPermission.READ))
        for principal, permission in grants:
            # Audited like any grant an administrator makes.
            await grant_administration_service.create(
                session,
                reviewer,
                ResourceRef(ResourceType.SKILL, str(skill.id)),
                GrantSpec(PrincipalType.USER, principal, permission),
                correlation_id=f"hermes-skill-draft-{draft.id}",
            )
    _decide(draft, reviewer, "approved")
    draft.skill_id = skill.id
    await session.flush()
    await session.refresh(draft)
    return draft


async def reject(
    session: AsyncSession, reviewer: AuthorizationSubject, draft_id: UUID, reviewed: str
) -> HermesSkillDraft:
    draft = await _pending(session, reviewer.org_id, draft_id, reviewed)
    _decide(draft, reviewer, "rejected")
    await session.flush()
    await session.refresh(draft)
    return draft


def _decide(draft: HermesSkillDraft, reviewer: AuthorizationSubject, status: str) -> None:
    draft.status = status
    draft.reviewed_by = reviewer.user_id
    draft.reviewed_at = datetime.now(timezone.utc)
