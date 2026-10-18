from pydantic import BaseModel


class UsageUserResponse(BaseModel):
    requestsUsed: int
    tokensUsed: int


class UsageOrganizationResponse(BaseModel):
    requestsUsed: int
    tokensUsed: int
    requestLimit: int | None
    tokenLimit: int | None
    requestsRemaining: int | None
    tokensRemaining: int | None


class UsageResponse(BaseModel):
    periodStart: str
    periodEnd: str
    user: UsageUserResponse
    organization: UsageOrganizationResponse
