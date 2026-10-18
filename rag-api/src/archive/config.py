"""Private maintenance process configuration, independent of ordinary API settings."""

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ArchiveSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RAG_API_", hide_input_in_errors=True)
    archive_database_url: SecretStr
    archive_service_secret: SecretStr = SecretStr("")
    archive_s3_endpoint: str
    archive_s3_access_key: str
    archive_s3_secret_key: SecretStr
    archive_s3_bucket: str
    archive_s3_region: str = "us-east-1"
    s3_endpoint: str
    s3_access_key: str
    s3_secret_key: SecretStr
    s3_bucket: str
    s3_region: str = "us-east-1"
    milvus_uri: str = "http://milvus:19530"
    milvus_collection: str = "documents"
    temporal_host: str = "temporal:7233"
    temporal_namespace: str = "default"
