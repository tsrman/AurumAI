from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    bind_port: int = 8110
    # no default — docker-compose.yml always sets
    # RAG_API_DATABASE_URL explicitly (with RAG_APP_DB_PASSWORD threaded
    # through, see the security review), so this only matters for someone
    # running this service outside compose; a hardcoded rag_app:rag_app
    # default there was a real, guessable credential landing in a
    # deployment that simply forgot to set it.
    database_url: str
    migration_database_url: SecretStr = SecretStr("")
    milvus_uri: str = "http://milvus:19530"
    milvus_collection: str = "documents"
    embedding_dim: int = 1536
    reranker_url: str = "http://reranker:7997"
    embedding_model: str = "Alibaba-NLP/gte-Qwen2-1.5B-instruct"
    # Read timeout for the query embedding in /v1/search. A CPU-only model
    # under memory pressure can need longer for the first query after idling.
    search_embedding_timeout_seconds: float = Field(default=10.0, gt=0)
    temporal_host: str = "temporal:7233"
    temporal_namespace: str = "default"
    ingest_task_queue: str = "rag-ingest"
    search_max_limit: int = 50
    # Optional cross-encoder reranking of the fused hybrid results. Empty URL = off
    # (results stay in reciprocal-rank-fusion order). The service must answer
    # POST {rerank_url}/rerank in the Infinity / Cohere shape.
    rerank_url: str = ""
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    # How many fused hits are scored; clamped to search_max_limit. More candidates
    # cost more reranker time and can only improve which `limit` hits are returned.
    rerank_candidates: int = Field(default=30, ge=1)
    rerank_timeout_seconds: float = Field(default=10.0, gt=0)
    # Each passage is cut to this many characters before scoring.
    rerank_max_chars: int = Field(default=2000, ge=100)
    # an unbounded DocumentCreate.content turns into
    # thousands of per-chunk LLM calls plus one giant embedding request in
    # rag-worker — cap it at the point of entry, before it's ever queued
    # for indexing.
    document_max_content_bytes: int = 2 * 1024 * 1024  # 2 MB

    class Config:
        env_prefix = "RAG_API_"

# database_url has no default (see above) — mypy can't see that
# pydantic-settings resolves required fields from the environment rather
# than needing them as constructor args.
settings = Settings()  # type: ignore[call-arg]
