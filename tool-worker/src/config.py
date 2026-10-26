from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TOOL_WORKER_",
        env_file=".env",
        extra="ignore",
    )

    temporal_host: str = "localhost:7233"
    temporal_namespace: str = "default"
    task_queue: str = "tools"
    backend_api_url: str = "http://localhost:8000"
    session_token: str = ""
    redis_url: str = "redis://localhost:6379/0"
    skill_executor_url: str = "http://localhost:8090"
    # Within the backend's EVENT_SUBSCRIBE_TIMEOUT (120 s), which a keepalive resets.
    hermes_keepalive_seconds: float = 30.0
    otel_exporter_otlp_endpoint: str = "http://localhost:4317"

    LANGFUSE_ENABLED: bool = Field(default=False, alias="LANGFUSE_ENABLED")
    LANGFUSE_PUBLIC_KEY: str = Field(default="", alias="LANGFUSE_PUBLIC_KEY")
    LANGFUSE_SECRET_KEY: str = Field(default="", alias="LANGFUSE_SECRET_KEY")
    LANGFUSE_HOST: str = Field(default="http://localhost:3000", alias="LANGFUSE_HOST")
    LANGFUSE_CAPTURE_FULL_CONTENT: bool = Field(default=True, alias="LANGFUSE_CAPTURE_FULL_CONTENT")
    LANGFUSE_FLUSH_INTERVAL: int = Field(default=5, alias="LANGFUSE_FLUSH_INTERVAL")


settings = Settings()
