"""Organization settings for Hermes Agent (docs/HERMES_AGENT.md), and the
review of the skills it drafts (phase 5b).

The internet egress mode and the model Hermes runs on. The operator's maximum
is shown read-only; a mode above the maximum is refused rather than silently
lowered. The model must be one the organization grants; every Hermes call
checks the user's own grant again.
"""

import uuid
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo
from src.core.authorization_dependencies import authorization_subject, require_org_admin
from src.services.model_access import ensure_model_allowed
from src.core.config import settings
from src.core.dependencies import get_current_org_id, get_org_db_session
from src.models.hermes_skill_draft import HermesSkillDraft
from src.models.user import User
from src.routers.hermes_skill_drafts import SkillDraftView, draft_error
from src.schemas.common import ApiResponse, Meta
from src.services import hermes_skill_drafts
from src.services.hermes_egress import (
    EgressModeAboveMaximum,
    organization_egress_mode,
    organization_hermes_model,
    set_organization_egress_mode,
    set_organization_hermes_model,
)

router = APIRouter()
Mode = Literal["none", "packages", "public"]


class HermesSettingsResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    enabled: bool
    egress_mode: Mode = Field(serialization_alias="egressMode")
    egress_max_mode: Mode = Field(serialization_alias="egressMaxMode")
    model: str | None


class HermesSettingsUpdate(BaseModel):
    """Fields left out are kept; `model: null` turns Hermes off for the organization."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    egress_mode: Mode | None = Field(default=None, alias="egressMode")
    model: str | None = Field(default=None, min_length=1, max_length=255)


def _meta() -> Meta:
    return Meta(
        requestId=f"req_{uuid.uuid4().hex}",
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


async def _response(session: AsyncSession, org_id: UUID) -> HermesSettingsResponse:
    return HermesSettingsResponse(
        enabled=settings.HERMES_ENABLED,
        egress_mode=await organization_egress_mode(session, org_id),
        egress_max_mode=settings.HERMES_EGRESS_MAX_MODE,
        model=await organization_hermes_model(session, org_id),
    )


@router.get("/settings/hermes", response_model=ApiResponse[HermesSettingsResponse])
async def hermes_settings(
    user: UserInfo = Depends(require_org_admin),
    org_id: UUID = Depends(get_current_org_id),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[HermesSettingsResponse]:
    return ApiResponse(data=await _response(session, org_id), meta=_meta())


@router.put("/settings/hermes", response_model=ApiResponse[HermesSettingsResponse])
async def update_hermes_settings(
    data: HermesSettingsUpdate,
    user: UserInfo = Depends(require_org_admin),
    org_id: UUID = Depends(get_current_org_id),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[HermesSettingsResponse]:
    if data.egress_mode is not None:
        try:
            await set_organization_egress_mode(session, org_id, data.egress_mode)
        except EgressModeAboveMaximum:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Egress mode is above the operator's maximum",
            ) from None
    if "model" in data.model_fields_set:
        if data.model is not None:
            await ensure_model_allowed(
                session, str(org_id), data.model, user_id=user.user_id, role=user.role
            )
        await set_organization_hermes_model(session, org_id, data.model)
    await session.commit()
    return ApiResponse(data=await _response(session, org_id), meta=_meta())


DraftStatus = Literal["pending", "approved", "rejected"]


class ReviewedSkillDraft(SkillDraftView):
    author_email: str | None = Field(serialization_alias="authorEmail")
    # A decision names it: the draft may have changed since it was read.
    bundle_checksum: str = Field(serialization_alias="bundleChecksum")


class SkillDraftFileView(BaseModel):
    path: str
    content: str


class SkillDraftDetail(ReviewedSkillDraft):
    files: list[SkillDraftFileView]


class SkillDraftDecision(BaseModel):
    """The version the reviewer read, as `bundleChecksum` of its detail."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    bundle_checksum: str = Field(alias="bundleChecksum", pattern=r"^[0-9a-f]{64}$")


class SkillDraftApproval(SkillDraftDecision):
    """Optional overrides of the name and description in SKILL.md."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, min_length=1, max_length=2000)


async def _authors(session: AsyncSession, org_id: UUID, user_ids: set[UUID]) -> dict[UUID, str]:
    if not user_ids:
        return {}
    rows = await session.execute(select(User.id, User.email).where(User.org_id == org_id, User.id.in_(user_ids)))
    return {user_id: email for user_id, email in rows.all()}


async def _reviewed(session: AsyncSession, org_id: UUID, draft: HermesSkillDraft) -> ReviewedSkillDraft:
    authors = await _authors(session, org_id, {draft.user_id})
    return _with_author(draft, authors)


def _with_author(draft: HermesSkillDraft, authors: dict[UUID, str]) -> ReviewedSkillDraft:
    return ReviewedSkillDraft(
        **SkillDraftView.of(draft).model_dump(),
        author_email=authors.get(draft.user_id),
        bundle_checksum=draft.bundle_checksum,
    )


@router.get("/hermes/skill-drafts", response_model=ApiResponse[list[ReviewedSkillDraft]])
async def hermes_skill_drafts_for_review(
    draft_status: DraftStatus | None = Query(default=None, alias="status"),
    user: UserInfo = Depends(require_org_admin),
    org_id: UUID = Depends(get_current_org_id),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[list[ReviewedSkillDraft]]:
    drafts = await hermes_skill_drafts.list_for_review(session, org_id, draft_status)
    authors = await _authors(session, org_id, {draft.user_id for draft in drafts})
    return ApiResponse(
        data=[_with_author(draft, authors) for draft in drafts],
        meta=_meta(),
    )


@router.get("/hermes/skill-drafts/{draft_id}", response_model=ApiResponse[SkillDraftDetail])
async def hermes_skill_draft_for_review(
    draft_id: UUID,
    user: UserInfo = Depends(require_org_admin),
    org_id: UUID = Depends(get_current_org_id),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[SkillDraftDetail]:
    """Every file of the draft: what an approval makes runnable."""
    try:
        draft = await hermes_skill_drafts.get_for_review(session, org_id, draft_id)
        files = await hermes_skill_drafts.review_files(draft)
    except hermes_skill_drafts.DraftError as exc:
        raise draft_error(exc) from None
    reviewed = await _reviewed(session, org_id, draft)
    return ApiResponse(
        data=SkillDraftDetail(
            **reviewed.model_dump(), files=[SkillDraftFileView(path=item.path, content=item.content) for item in files]
        ),
        meta=_meta(),
    )


@router.post("/hermes/skill-drafts/{draft_id}/approve", response_model=ApiResponse[ReviewedSkillDraft])
async def approve_hermes_skill_draft(
    draft_id: UUID,
    data: SkillDraftApproval,
    user: UserInfo = Depends(require_org_admin),
    org_id: UUID = Depends(get_current_org_id),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[ReviewedSkillDraft]:
    try:
        draft = await hermes_skill_drafts.approve(
            session,
            authorization_subject(user),
            draft_id,
            data.bundle_checksum,
            name=data.name,
            description=data.description,
        )
    except hermes_skill_drafts.DraftError as exc:
        await session.rollback()
        raise draft_error(exc) from None
    await session.commit()
    return ApiResponse(data=await _reviewed(session, org_id, draft), meta=_meta())


@router.post("/hermes/skill-drafts/{draft_id}/reject", response_model=ApiResponse[ReviewedSkillDraft])
async def reject_hermes_skill_draft(
    draft_id: UUID,
    data: SkillDraftDecision,
    user: UserInfo = Depends(require_org_admin),
    org_id: UUID = Depends(get_current_org_id),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[ReviewedSkillDraft]:
    try:
        draft = await hermes_skill_drafts.reject(session, authorization_subject(user), draft_id, data.bundle_checksum)
    except hermes_skill_drafts.DraftError as exc:
        await session.rollback()
        raise draft_error(exc) from None
    await session.commit()
    return ApiResponse(data=await _reviewed(session, org_id, draft), meta=_meta())
