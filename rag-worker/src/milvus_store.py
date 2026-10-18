"""Milvus store for aurum-ai RAG."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from pymilvus import (
    AnnSearchRequest,
    DataType,
    Function,
    FunctionType,
    MilvusClient,
    RRFRanker,
)

from src.config import settings
from src.mutation_transport import GRPC_OPTIONS, mutate

logger = logging.getLogger(__name__)

_client: Optional[MilvusClient] = None

def get_milvus_client() -> MilvusClient:
    global _client
    if _client is not None:
        return _client
    
    try:
        _client = MilvusClient(uri=settings.milvus_uri, grpc_options=GRPC_OPTIONS)
        return _client
    except Exception as exc:
        logger.error(f"Failed to connect to Milvus at {settings.milvus_uri}: {exc}")
        raise

# Every field the current search/insert path names explicitly. `knowledge_base_id`
# was added with Authorization v2: a collection created before it exists and is
# never rewritten, because init_milvus_collection only builds a schema when the
# collection is absent and Milvus cannot backfill a value into rows already
# written. Chunks indexed under the old schema therefore carry no
# knowledge_base_id and are excluded from every search, which filters on it —
# silently, since an empty result set looks like "nothing matched". Detect that
# and say so; the only real remedy is a re-index.
REQUIRED_COLLECTION_FIELDS = (
    "org_id",
    "knowledge_base_id",
    "document_id",
    "chunk_index",
    "content",
)


def missing_collection_fields() -> tuple[str, ...]:
    """Required fields absent from the live collection's declared schema.

    Empty when the collection is up to date, absent, or cannot be described —
    an unreachable Milvus is the caller's problem, not a schema verdict.
    """
    client = get_milvus_client()
    if not client.has_collection(settings.milvus_collection):
        return ()
    try:
        described = client.describe_collection(settings.milvus_collection)
    except Exception:
        logger.warning("Could not describe Milvus collection to verify its schema", exc_info=True)
        return ()
    fields = described.get("fields", []) if isinstance(described, dict) else []
    declared = {
        field.get("name") for field in fields if isinstance(field, dict)
    }
    return tuple(name for name in REQUIRED_COLLECTION_FIELDS if name not in declared)


def init_milvus_collection() -> bool:
    client = get_milvus_client()
    if client.has_collection(settings.milvus_collection):
        missing = missing_collection_fields()
        if missing:
            logger.error(
                "Milvus collection %r predates fields %s. Chunks indexed under the old "
                "schema carry no value for them and are invisible to every search that "
                "filters on one (knowledge_base_id does). Drop the collection and "
                "re-index every document to restore search over existing data.",
                settings.milvus_collection,
                ", ".join(missing),
            )
            return False
        return True

    schema = MilvusClient.create_schema(
        auto_id=False,
        enable_dynamic_field=True,
    )
    schema.add_field("pk", DataType.VARCHAR, is_primary=True, max_length=128)
    schema.add_field("org_id", DataType.VARCHAR, max_length=64, is_partition_key=True)
    schema.add_field("knowledge_base_id", DataType.VARCHAR, max_length=64)
    schema.add_field("document_id", DataType.VARCHAR, max_length=64)
    schema.add_field("chunk_index", DataType.INT64)
    schema.add_field("content", DataType.VARCHAR, max_length=65535, enable_analyzer=True)
    schema.add_field("dense", DataType.FLOAT_VECTOR, dim=settings.embedding_dim)
    schema.add_field("sparse", DataType.SPARSE_FLOAT_VECTOR)

    bm25_function = Function(
        name="content_bm25",
        function_type=FunctionType.BM25,
        input_field_names=["content"],
        output_field_names=["sparse"],
    )
    schema.add_function(bm25_function)

    index_params = client.prepare_index_params()
    index_params.add_index(
        field_name="dense", index_type="AUTOINDEX", metric_type="COSINE"
    )
    index_params.add_index(
        field_name="sparse", index_type="SPARSE_INVERTED_INDEX", metric_type="BM25"
    )

    client.create_collection(
        collection_name=settings.milvus_collection,
        schema=schema,
        index_params=index_params,
    )
    logger.info("Milvus collection '%s' created successfully.", settings.milvus_collection)
    return True

def _acl_allows(doc_acl: List[str], required: Optional[List[str]]) -> bool:
    if not required:
        return True
    doc_set = set(doc_acl or [])
    return bool(doc_set.intersection(required))

async def bulk_insert_milvus(docs: list[tuple[str, Any]]) -> dict[str, Any]:
    if not docs:
        return {"indexed": 0, "errors": 0, "skipped": False}
    
    def _insert() -> dict[str, Any]:
        client = get_milvus_client()
        init_milvus_collection()
        data: list[dict[str, Any]] = []
        for chunk_id, chunk_doc in docs:
            acl = list(getattr(chunk_doc, "acl", None) or ["public"])
            data.append(
                {
                    "pk": str(chunk_id),
                    "org_id": str(chunk_doc.org_id),
                    "knowledge_base_id": str(chunk_doc.knowledge_base_id),
                    "document_id": str(chunk_doc.document_id),
                    "chunk_index": int(chunk_doc.chunk_index),
                    "content": chunk_doc.content,
                    "parent_content": chunk_doc.parent_content,
                    "acl_json": json.dumps(acl, ensure_ascii=False),
                    "document_title": getattr(chunk_doc, "document_title", "") or "",
                    "dense": chunk_doc.embedding,
                }
            )
        res = mutate(client, "upsert", settings.milvus_collection, data)
        upserted = int(res.get("upsert_count", len(data)) if isinstance(res, dict) else len(data))
        return {"indexed": upserted, "errors": 0, "skipped": False}

    from src.activities import AsyncSessionLocal
    from src.lifecycle import begin_vector, settle_vector
    scope,operation=await begin_vector(AsyncSessionLocal)
    if any((str(d.org_id),str(d.knowledge_base_id),str(d.document_id)) != (scope.org_id,scope.knowledge_base_id,scope.document_id) for _,d in docs):
        raise ValueError('rag_vector_scope_invalid')
    # Intent precedes the RPC. Exceptions/cancellation intentionally leave it
    # unresolved, including a to_thread call that outlives its coroutine.
    result=await asyncio.to_thread(_insert)
    await settle_vector(AsyncSessionLocal,scope,operation)
    return result

async def search_milvus_hybrid(
    query: str,
    embedding: List[float],
    limit: int,
    org_id: Optional[str],
    knowledge_base_id: str,
    acl: Optional[List[str]],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    warnings: list[str] = []
    
    if not embedding:
        return [], ["Milvus hybrid requires a query embedding."]

    def _search() -> list[dict[str, Any]]:
        client = get_milvus_client()
        if not client.has_collection(settings.milvus_collection):
            init_milvus_collection()
            return []

        safe_knowledge_base_id = (
            str(knowledge_base_id).replace('"', "").replace("'", "").replace("\\", "")
        )
        if org_id:
            safe_org_id = str(org_id).replace('"', "").replace("'", "").replace("\\", "")
            expr = (
                f'org_id == "{safe_org_id}" and '
                f'knowledge_base_id == "{safe_knowledge_base_id}"'
            )
        else:
            expr = f'knowledge_base_id == "{safe_knowledge_base_id}"'

        fetch_n = max(limit * 4, limit)
        dense_kwargs: dict[str, Any] = {
            "data": [embedding],
            "anns_field": "dense",
            "param": {"metric_type": "COSINE"},
            "limit": fetch_n,
        }
        sparse_kwargs: dict[str, Any] = {
            "data": [query],
            "anns_field": "sparse",
            "param": {"metric_type": "BM25"},
            "limit": fetch_n,
        }
        if expr:
            dense_kwargs["expr"] = expr
            sparse_kwargs["expr"] = expr
            
        req_dense = AnnSearchRequest(**dense_kwargs)
        req_sparse = AnnSearchRequest(**sparse_kwargs)

        res = client.hybrid_search(
            collection_name=settings.milvus_collection,
            reqs=[req_dense, req_sparse],
            ranker=RRFRanker(60),
            limit=fetch_n,
            output_fields=[
                "content",
                "parent_content",
                "document_id",
                "chunk_index",
                "org_id",
                "knowledge_base_id",
                "acl_json",
                "document_title",
            ],
        )

        results: list[dict[str, Any]] = []
        for hit in res[0]:
            entity = hit.get("entity") or {}
            try:
                doc_acl = json.loads(entity.get("acl_json") or "[]")
                if not isinstance(doc_acl, list):
                    doc_acl = ["public"]
            except json.JSONDecodeError:
                doc_acl = ["public"]

            if not _acl_allows(doc_acl, acl):
                continue

            parent = entity.get("parent_content") or ""
            indexed = entity.get("content") or ""
            llm_content = parent or indexed
            results.append(
                {
                    "id": hit.get("id") or entity.get("pk"),
                    "content": llm_content,
                    "indexed_content": indexed,
                    "score": hit.get("distance", 0.0),
                    "metadata": {
                        "document_id": entity.get("document_id"),
                        "chunk_index": entity.get("chunk_index"),
                        "org_id": entity.get("org_id"),
                        "knowledge_base_id": entity.get("knowledge_base_id"),
                        "acl": doc_acl,
                        "document_title": entity.get("document_title") or "",
                        "engine": "milvus",
                    },
                }
            )
            if len(results) >= limit * 2:
                break
        return results

    try:
        results = await asyncio.to_thread(_search)
        return results, warnings
    except Exception as exc:
        logger.error("Milvus hybrid search failed: %s", exc)
        warnings.append(f"Milvus hybrid failed: {exc}")
        return [], warnings
