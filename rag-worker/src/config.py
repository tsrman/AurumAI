from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    temporal_host: str = "temporal:7233"
    temporal_namespace: str = "default"
    task_queue: str = "rag-ingest"
    # no default — see rag-api/src/config.py's identical
    # field for the reasoning (docker-compose.yml always sets
    # RAG_WORKER_DATABASE_URL explicitly).
    database_url: str
    milvus_uri: str = "http://milvus:19530"
    milvus_collection: str = "documents"
    embedding_dim: int = 1536
    reranker_url: str = "http://reranker:7997"
    embedding_model: str = "Alibaba-NLP/gte-Qwen2-1.5B-instruct"
    chunk_size: int = 1000
    chunk_overlap: int = 100
    embed_batch_size: int = 32
    # a document that chunks into far more pieces than
    # this (e.g. many short sentences) still means one LLM call per chunk
    # for contextual retrieval — cap the count processed per document.
    max_chunks_per_document: int = 2000
    openai_api_key: str = ""
    contextual_retrieval_enabled: bool = True
    contextual_retrieval_mock: bool = False
    contextual_model: str = "gpt-4o-mini"
    llm_gateway_url: str = "https://api.openai.com/v1/chat/completions"
    # Read uploaded document sources from object storage; business-backend
    # stores the object and passes its key to rag-api.
    s3_endpoint: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_bucket: str = "aurum-ai-business"
    s3_region: str = "us-east-1"

    class Config:
        env_prefix = "RAG_WORKER_"

# database_url has no default (see above) — mypy can't see that
# pydantic-settings resolves required fields from the environment rather
# than needing them as constructor args.
settings = Settings()  # type: ignore[call-arg]
