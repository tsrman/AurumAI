"""A user's own Hermes Agent skill drafts, in their settings (docs/HERMES_AGENT.md,
phase 5b): see what is waiting for an administrator, and withdraw it."""

import uuid
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo, get_current_user
from src.core.authorization_dependencies import authorization_subject
from src.core.dependencies import get_org_db_session
from src.models.hermes_skill_draft import HermesSkillDraft
from src.schemas.common import ApiResponse, Meta
from src.services import hermes_skill_drafts

router = APIRouter()


class SkillDraftView(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: UUID
    path: str
    name: str
    description: str
    status: str
    file_count: int = Field(serialization_alias="fileCount")
    size_bytes: int = Field(serialization_alias="sizeBytes")
    skill_id: UUID | None = Field(serialization_alias="skillId")
    created_at: datetime = Field(serialization_alias="createdAt")
    updated_at: datetime = Field(serialization_alias="updatedAt")

    @classmethod
    def of(cls, draft: HermesSkillDraft) -> "SkillDraftView":
        return cls(
            id=draft.id,
            path=draft.path,
            name=draft.name,
            description=draft.description,
            status=draft.status,
            file_count=draft.file_count,
            size_bytes=draft.bundle_size_bytes,
            skill_id=draft.skill_id,
            created_at=draft.created_at,
            updated_at=draft.updated_at,
        )


def meta() -> Meta:
    return Meta(requestId=f"req_{uuid.uuid4().hex}", timestamp=datetime.now(timezone.utc).isoformat())


_STATUS = {
    "hermes_skill_draft_not_found": status.HTTP_404_NOT_FOUND,
    "hermes_skill_draft_decided": status.HTTP_409_CONFLICT,
    "hermes_skill_bundle_mismatch": status.HTTP_409_CONFLICT,
    "hermes_skill_draft_changed": status.HTTP_409_CONFLICT,
}


def draft_error(exc: hermes_skill_drafts.DraftError) -> HTTPException:
    return HTTPException(status_code=_STATUS.get(exc.code, 422), detail={"code": exc.code})


@router.get("/hermes/skill-drafts", response_model=ApiResponse[list[SkillDraftView]])
async def my_hermes_skill_drafts(
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[list[SkillDraftView]]:
    subject = authorization_subject(user)
    drafts = await hermes_skill_drafts.list_own(session, subject.org_id, subject.user_id)
    return ApiResponse(data=[SkillDraftView.of(draft) for draft in drafts], meta=meta())


@router.delete("/hermes/skill-drafts/{draft_id}", status_code=status.HTTP_204_NO_CONTENT)
async def dismiss_my_hermes_skill_draft(
    draft_id: UUID,
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> Response:
    subject = authorization_subject(user)
    try:
        await hermes_skill_drafts.dismiss(session, subject.org_id, subject.user_id, draft_id)
    except hermes_skill_drafts.DraftError as exc:
        raise draft_error(exc) from None
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
