"""Pydantic schemas for OpenSearch chunk documents."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

from src.config import settings


class ChunkDocument(BaseModel):
    """Strict mapping for chunks_v*_bgem3 index."""

    content: str = Field(min_length=1, description="Indexed/searchable text (contextual)")
    parent_content: str = Field(min_length=1, description="Expanded window for LLM")
    embedding: list[float] = Field(min_length=1)
    org_id: str
    knowledge_base_id: str
    acl: list[str] = Field(default_factory=lambda: ["public"])
    document_id: str
    document_title: str = ""
    source_type: str = "document"
    doc_version: int = 1
    embedding_model_version: str = "bge-m3-v1"
    chunk_index: int = 0
    contextual_prefix: str = ""
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @field_validator("embedding")
    @classmethod
    def validate_dimension(cls, value: list[float]) -> list[float]:
        # Was hardcoded to the old bge-m3 dimension (1024) — the configured
        # model (gte-Qwen2-1.5B-instruct, settings.embedding_dim=1536) failed
        # this check on every single chunk, so indexing never completed.
        if len(value) != settings.embedding_dim:
            raise ValueError(
                f"embedding must be {settings.embedding_dim}-dimensional, got {len(value)}"
            )
        return value

    def to_opensearch_body(self) -> dict[str, Any]:
        body = self.model_dump(exclude_none=True)
        for key in ("created_at", "updated_at"):
            if key in body and isinstance(body[key], datetime):
                body[key] = body[key].isoformat()
        return body


class PostgresChunkRow(BaseModel):
    """Row read from document_chunks table."""

    id: str
    org_id: str
    document_id: str
    content: str
    metadata_json: Optional[str] = None
    created_at: Optional[datetime] = None
