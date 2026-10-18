"""End-to-end document ingestion: parse → chunk → contextualize → index."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional
import asyncio

import httpx
from temporalio import activity

from src.config import settings
from src.indexing.chunking import SentenceWindowChunk, chunk_text_sentence_window
from src.indexing.contextual import build_contextual_content, generate_chunk_context
from src.indexing.parser import ParsedDocument
from src.indexing.schema import ChunkDocument
from src.milvus_store import bulk_insert_milvus

logger = logging.getLogger(__name__)

async def _embed_texts(texts: list[str]) -> list[list[float]]:
    """Batched by embed_batch_size — a single request
    for every chunk in a large document would grow unboundedly and was the
    one place still routinely hitting the 120s embedding-call timeout."""
    embeddings: list[list[float]] = []
    async with httpx.AsyncClient() as client:
        for start in range(0, len(texts), settings.embed_batch_size):
            batch = texts[start : start + settings.embed_batch_size]
            response = await client.post(
                f"{settings.reranker_url}/embeddings",
                json={"model": settings.embedding_model, "input": batch},
                timeout=120.0,
            )
            response.raise_for_status()
            embeddings.extend(item["embedding"] for item in response.json()["data"])
    return embeddings

async def ingest_parsed_document(
    parsed: ParsedDocument,
    *,
    org_id: str,
    knowledge_base_id: str,
    acl: Optional[list[str]] = None,
    doc_version: int = 1,
    document_id: Optional[str] = None,
    persist_to_postgres: bool = True,
) -> dict[str, Any]:
    acl = acl or [f"org:{org_id}"]
    document_id = document_id or str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    excerpt = parsed.text[:1200]

    window_chunks = chunk_text_sentence_window(
        parsed.text,
        max_tokens=settings.chunk_size,
        overlap_sentences=3,
    )
    if not window_chunks:
        return {"document_id": document_id, "chunks_created": 0, "status": "empty"}

    # even a size-capped document (rag-api's
    # document_max_content_bytes) can chunk into more pieces than is sane to
    # run one contextualization LLM call each for — truncate rather than let
    # a pathological document (e.g. mostly very short "sentences") drive
    # thousands of calls.
    if len(window_chunks) > settings.max_chunks_per_document:
        logger.warning(
            "Document %s produced %s chunks, truncating to max_chunks_per_document=%s",
            document_id,
            len(window_chunks),
            settings.max_chunks_per_document,
        )
        window_chunks = window_chunks[: settings.max_chunks_per_document]

    semaphore = asyncio.Semaphore(10)

    async def process_item(
        item: SentenceWindowChunk,
    ) -> tuple[str, str, str, int]:
        async with semaphore:
            context = await generate_chunk_context(
                title=parsed.title,
                chunk=item.sentence,
                document_excerpt=excerpt,
            )
            indexed_text = build_contextual_content(context, item.sentence)
            # index_document's workflow sets heartbeat_timeout=1m but this
            # was the only long-running activity that never called
            # heartbeat() — a document with enough chunks for contextual
            # retrieval (one LLM call per chunk) to run past a minute would
            # eventually hit start_to_close_timeout with no visibility into
            # how far it got. activity.heartbeat() raises RuntimeError
            # outside a real activity execution context — safe here since
            # ingest_parsed_document's only caller is activities.py's
            # index_document activity.
            activity.heartbeat(f"contextualized chunk {item.chunk_index}/{len(window_chunks)}")
            return (indexed_text, item.parent_window, context, item.chunk_index)

    contextualized = await asyncio.gather(*[process_item(item) for item in window_chunks])
    activity.heartbeat("chunks contextualized, requesting embeddings")
    embeddings = await _embed_texts([text for text, _, _, _ in contextualized])
    activity.heartbeat("embeddings received, upserting to Milvus")

    docs: list[tuple[str, ChunkDocument]] = []
    for (indexed_text, parent_window, context, chunk_index), embedding in zip(
        contextualized, embeddings
    ):
        # Deterministic, not uuid4() — bulk_insert_milvus upserts by pk, so a
        # retried activity (same document_id, same chunking) must reuse the
        # same pk per chunk or it inserts permanent duplicates instead of
        # overwriting the partial attempt's rows.
        chunk_id = f"{document_id}:{chunk_index}"
        doc = ChunkDocument(
            content=indexed_text,
            parent_content=parent_window,
            embedding=embedding,
            org_id=org_id,
            knowledge_base_id=knowledge_base_id,
            acl=acl,
            document_id=document_id,
            document_title=parsed.title,
            source_type=parsed.source_type,
            doc_version=doc_version,
            embedding_model_version="v1",
            chunk_index=chunk_index,
            contextual_prefix=context,
            created_at=now,
            updated_at=now,
        )
        docs.append((chunk_id, doc))

    milvus_stats: dict[str, Any] = {"indexed": 0, "errors": 0, "skipped": False}
    try:
        milvus_stats = await bulk_insert_milvus(docs) or milvus_stats
        logger.info(
            "Indexed %s chunks to Milvus (skipped=%s)",
            milvus_stats.get("indexed", 0),
            milvus_stats.get("skipped", False),
        )
    except Exception as exc:
        logger.error("Milvus indexing failed: %s", exc)
        milvus_stats = {"indexed": 0, "errors": len(docs), "skipped": False}

    return {
        "document_id": document_id,
        "title": parsed.title,
        "chunks_created": len(docs),
        "milvus": milvus_stats,
        "status": "ok" if milvus_stats.get("errors", 0) == 0 else "partial",
    }
