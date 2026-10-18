from typing import Optional
from pydantic import BaseModel

from src.core.capability_catalog import Capability


class TokenCallbackRequest(BaseModel):
    code: str
    code_verifier: str
    redirect_uri: str


class TokenRefreshRequest(BaseModel):
    refresh_token: str


class UserInfoResponse(BaseModel):
    user_id: str
    email: Optional[str]
    full_name: Optional[str]
    org_id: Optional[str]
    org_name: Optional[str]
    role: str
    capabilities: list[Capability]
