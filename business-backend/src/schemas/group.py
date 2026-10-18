from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class GroupSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GroupResponse(GroupSchema):
    id: str
    orgId: Optional[str] = None
    name: str
    keycloakId: Optional[str] = None
    lastSyncedAt: Optional[str] = None


class GroupCreateRequest(GroupSchema):
    name: str = Field(min_length=1, max_length=255)


class GroupUpdateRequest(GroupSchema):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
