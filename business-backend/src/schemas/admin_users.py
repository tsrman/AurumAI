from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class AdminUserResponse(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )

    id: UUID
    email: str
    full_name: str | None
    role: str
    status: str
    last_login_at: datetime | None
    created_at: datetime
