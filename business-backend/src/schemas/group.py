from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class GroupSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GroupResponse(GroupSchema):
    id: str
    orgId: Optional[str] = None
    name: str
    keycloakId: Optional[str] = None
    lastSyncedAt: Optional[str] = None
    memberCount: int = 0


class GroupCreateRequest(GroupSchema):
    name: str = Field(min_length=1, max_length=255)


class GroupUpdateRequest(GroupSchema):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)


class GroupMemberResponse(GroupSchema):
    id: str
    email: str
    fullName: Optional[str] = None
    role: str
    status: str
    # "identity": synchronized from Keycloak; "manual": added in the Admin UI.
    source: str


class GroupMembersAddRequest(GroupSchema):
    userIds: list[UUID] = Field(min_length=1, max_length=100)


class GroupMembersAddResponse(GroupSchema):
    added: int
    alreadyMembers: int
