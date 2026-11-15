from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class DocumentCreate(BaseModel):
    knowledge_base_id: UUID
    title: str
    content: str


class DocumentSearch(BaseModel):
    query: str = Field(min_length=1)
    limit: int = Field(default=10, ge=1, le=100)


class DocumentUpdate(BaseModel):
    title: Optional[str] = None
    status: Optional[str] = None


class DocumentResponse(BaseModel):
    id: UUID
    knowledge_base_id: UUID
    title: str
    # Present only for documents created from an uploaded file.
    source_filename: Optional[str] = None
    status: str
    chunk_count: int
    # Chunks indexed without their contextual-retrieval context, and chunks dropped at
    # the per-document cap. Zero for documents indexed before these were recorded.
    uncontextualized_chunks: int = 0
    truncated_chunks: int = 0
    error: Optional[str] = None
    created_at: datetime
    updated_at: datetime
