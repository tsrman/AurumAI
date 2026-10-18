import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo, get_current_user
from src.core.dependencies import get_org_db_session
from src.routers.skills import _require_skill_create, _require_skill_write, _skill_response
from src.schemas.common import ApiResponse, Meta
from src.schemas.skill import SkillResponse
from src.services.object_storage import object_storage
from src.services.skill_bundle import ParsedBundle, SkillBundleError, validate_and_parse
from src.services.skill_registry import skill_registry

router = APIRouter()


def _meta() -> Meta:
    return Meta(
        requestId=f"req_{uuid.uuid4().hex}",
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


def _parse_or_400(raw_bytes: bytes) -> ParsedBundle:
    try:
        return validate_and_parse(raw_bytes)
    except SkillBundleError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


def _storage_key(org_id: Optional[str | uuid.UUID], checksum: str) -> str:
    return f"skills/{org_id or 'shared'}/{checksum}.zip"


@router.post("/code", status_code=status.HTTP_201_CREATED)
async def upload_code_skill(
    name: str = Form(...),
    description: Optional[str] = Form(None),
    bundle: UploadFile = File(...),
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[SkillResponse]:
    subject = await _require_skill_create(user, session)
    raw_bytes = await bundle.read()
    parsed = _parse_or_400(raw_bytes)

    storage_key = _storage_key(user.org_id, parsed.checksum)
    await object_storage.put_bytes(storage_key, parsed.raw_bytes)

    create_data: dict[str, Any] = {
        "org_id": user.org_id,
        "name": name,
        # A code skill has no fixed args schema — the first call passes the
        # LLM's stated intent as free text, matching real Agent Skills
        # semantics where the model reads SKILL.md and decides what to run.
        "description": description or parsed.skill_description,
        "type": "code",
        "input_schema": {},
        "bundle_storage_key": storage_key,
        "bundle_checksum": parsed.checksum,
        "bundle_size_bytes": parsed.size_bytes,
        "bundle_skill_name": parsed.skill_name,
    }
    skill = await skill_registry.create_owned_skill(session, subject, **create_data)
    return ApiResponse(data=_skill_response(skill), meta=_meta())


@router.put("/code/{skill_id}/bundle")
async def replace_code_skill_bundle(
    skill_id: str,
    bundle: UploadFile = File(...),
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[SkillResponse]:
    skill = await skill_registry.get_skill(session, skill_id)
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    await _require_skill_write(user, session, skill)
    if skill.type != "code":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Bundle replacement is only supported for type=code skills",
        )

    raw_bytes = await bundle.read()
    parsed = _parse_or_400(raw_bytes)

    storage_key = _storage_key(skill.org_id, parsed.checksum)
    await object_storage.put_bytes(storage_key, parsed.raw_bytes)

    skill = await skill_registry.update_skill(
        session,
        skill,
        bundle_storage_key=storage_key,
        bundle_checksum=parsed.checksum,
        bundle_size_bytes=parsed.size_bytes,
        bundle_skill_name=parsed.skill_name,
    )
    return ApiResponse(data=_skill_response(skill), meta=_meta())
