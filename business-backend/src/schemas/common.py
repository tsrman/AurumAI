from typing import Generic, TypeVar, Optional
from pydantic import BaseModel

T = TypeVar("T")


class Pagination(BaseModel):
    cursor: Optional[str] = None
    hasMore: bool = False
    total: Optional[int] = None


class Meta(BaseModel):
    requestId: str
    timestamp: str


class ApiResponse(BaseModel, Generic[T]):
    data: T
    meta: Meta
    pagination: Optional[Pagination] = None
