from typing import Literal, Optional
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Placeholder values that must never reach a real deployment — a shared
# constant so the fail-fast check below and the field defaults can't drift.
_INSECURE_SECRET_KEY = "change-me-in-production"
_INSECURE_DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/aurum_ai"
# docker-compose.yml's own dev-only fallback for MINIO_ROOT_PASSWORD — see
# .env.example.
_INSECURE_S3_SECRET_KEY = "miniosecret"


def validated_web_tools_origin(value: str) -> str:
    """Return an exact HTTP(S) origin suitable for carrying the service secret."""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("APP_WEB_TOOLS_URL must be an exact web-tools origin") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("APP_WEB_TOOLS_URL must be an exact HTTP(S) origin with a port")
    return value.rstrip("/")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[2] / ".env",
        env_prefix="APP_", extra="forbid", hide_input_in_errors=True
    )

    DEBUG: bool = False
    APP_NAME: str = "AurumAI Backend"
    FRONTEND_URL: str = "http://localhost:3000"

    DATABASE_URL: str = _INSECURE_DATABASE_URL
    REDIS_URL: str = "redis://localhost:6379/0"

    RAG_API_URL: str = "http://rag-api:8110"

    KEYCLOAK_URL: str = "http://localhost:8080"
    KEYCLOAK_ISSUER: Optional[str] = None
    # Tolerated clock difference between Keycloak and this API when checking
    # token iat/nbf/exp. Without it a freshly issued token is rejected as "not
    # yet valid" whenever the API clock trails Keycloak by even a second.
    JWT_CLOCK_SKEW_SECONDS: int = 30
    KEYCLOAK_REALM: str = "aurum-ai"
    KEYCLOAK_CLIENT_ID: str = "business-ui"
    KEYCLOAK_CLIENT_SECRET: Optional[str] = None
    COOKIE_SECURE: Optional[bool] = None

    LLM_GATEWAY_URL: str = "http://localhost:8000"
    GATEWAY_API_KEY: str = ""
    GATEWAY_VERIFY_SSL: bool = True
    GATEWAY_CA_CERT: Optional[str] = None

    SECRET_KEY: str = _INSECURE_SECRET_KEY

    @model_validator(mode="after")
    def _forbid_insecure_defaults_outside_debug(self) -> "Settings":
        # Docker Compose already requires APP_SECRET_KEY via `:?` — this is
        # the guard for every OTHER way this process can start (bare
        # uvicorn/gunicorn, a test stand, a misconfigured k8s manifest):
        # SECRET_KEY signs identity-only runner session tokens,
        # and the DATABASE_URL default is the postgres superuser, which
        # bypasses FORCE ROW LEVEL SECURITY entirely. Both defaults stay in
        # place for local DEBUG dev/test convenience.
        if not self.DEBUG:
            if self.SECRET_KEY == _INSECURE_SECRET_KEY:
                raise ValueError(
                    "APP_SECRET_KEY must be set to a real secret outside DEBUG "
                    "(generate with: openssl rand -hex 32)"
                )
            if self.DATABASE_URL == _INSECURE_DATABASE_URL:
                raise ValueError(
                    "APP_DATABASE_URL must be set outside DEBUG — the default "
                    "connects as the postgres superuser, bypassing RLS"
                )
            # Only meaningful once object storage is actually configured —
            # S3_SECRET_KEY defaults to "" (unused) when S3_ENDPOINT isn't
            # set at all, which is a legitimate deployment that never
            # touches code-skill bundle upload.
            if self.S3_ENDPOINT and self.S3_SECRET_KEY == _INSECURE_S3_SECRET_KEY:
                raise ValueError(
                    "APP_S3_SECRET_KEY must be set outside DEBUG — the default "
                    "matches docker-compose.yml's dev-only MinIO password"
                )
        if self.RUNNER_RUNTIME == "kubernetes" and self.RUNNER_POOL_SIZE != 0:
            raise ValueError("RUNNER_POOL_SIZE must be 0 when RUNNER_RUNTIME is kubernetes")
        if (
            self.RUNNER_TOKEN_REFRESH_AFTER_SECONDS
            + self.INTERACTION_MAINTENANCE_INTERVAL_SECONDS
            >= 30 * 60
        ):
            raise ValueError(
                "runner refresh must complete before the 30-minute session token expires"
            )
        if self.WEB_ENABLED and len(self.WEB_TOOLS_SERVICE_SECRET.get_secret_value()) < 16:
            raise ValueError(
                "APP_WEB_TOOLS_SERVICE_SECRET must contain at least 16 characters "
                "when APP_WEB_ENABLED is true"
            )
        return self

    @property
    def gateway_verify(self) -> bool | str:
        if not self.GATEWAY_VERIFY_SSL:
            return False
        if self.GATEWAY_CA_CERT:
            return self.GATEWAY_CA_CERT
        return True

    @property
    def cookie_secure(self) -> bool:
        if self.COOKIE_SECURE is not None:
            return self.COOKIE_SECURE
        return not self.DEBUG

    S3_ENDPOINT: Optional[str] = None
    S3_ACCESS_KEY: str = ""
    S3_SECRET_KEY: str = ""
    S3_BUCKET: str = "aurum-ai-business"
    S3_REGION: str = "us-east-1"

    # Purge is opt-in after migrations, archive storage and worker readiness.
    ARCHIVE_ENABLED: bool = False
    ARCHIVE_RETENTION_DAYS: int = Field(default=180, gt=0)
    ARCHIVE_SWEEP_INTERVAL_SECONDS: int = Field(default=86400, gt=0)
    ARCHIVE_BATCH_SIZE: int = Field(default=100, gt=0)
    # Only the operator/data worker receives these; API credentials stay separate.
    ARCHIVE_S3_ENDPOINT: Optional[str] = None
    ARCHIVE_S3_ACCESS_KEY: str = ""
    ARCHIVE_S3_SECRET_KEY: SecretStr = SecretStr("")
    ARCHIVE_S3_BUCKET: str = "aurum-ai-archives"
    ARCHIVE_S3_REGION: str = "us-east-1"
    ARCHIVE_RAG_SERVICE_SECRET: SecretStr = SecretStr("")
    ARCHIVE_RAG_URL: str = "http://rag-archive:8111"
    # Conversation image bytes in the platform bucket. Scope these credentials
    # to the conversation-images/ prefix; without them, conversations that
    # hold images stay unarchived.
    ARCHIVE_IMAGES_S3_ENDPOINT: Optional[str] = None
    ARCHIVE_IMAGES_S3_ACCESS_KEY: str = ""
    ARCHIVE_IMAGES_S3_SECRET_KEY: SecretStr = SecretStr("")
    ARCHIVE_IMAGES_S3_BUCKET: str = "aurum-ai-business"
    ARCHIVE_IMAGES_S3_REGION: str = "us-east-1"

    # Skill bundle upload guardrails (see services/skill_bundle.py) —
    # deliberately small caps since bundles are admin-authored script
    # packages, not general file storage.
    SKILL_BUNDLE_MAX_BYTES: int = 50 * 1024 * 1024  # 50 MB
    SKILL_BUNDLE_MAX_ENTRIES: int = 2000
    SKILL_BUNDLE_MAX_UNCOMPRESSED_BYTES: int = 200 * 1024 * 1024  # 200 MB
    SKILL_BUNDLE_MAX_COMPRESSION_RATIO: int = 10

    # File upload handling: routers/documents.py::upload_document.
    # Upload goes through the API rather than a presigned link: MinIO is
    # deliberately published on loopback only (docker-compose.yml), so a
    # browser on another machine could not use that direct signed URL.
    DOCUMENT_UPLOAD_MAX_BYTES: int = 25 * 1024 * 1024  # 25 MB
    # Docling parses these formats; treating everything else as plain text
    # would produce meaningless document content.
    DOCUMENT_UPLOAD_ALLOWED_SUFFIXES: tuple[str, ...] = (
        ".pdf", ".docx", ".doc", ".pptx", ".html", ".htm", ".md", ".txt",
    )

    RATE_LIMIT_PER_MINUTE: int = 300
    RATE_LIMIT_PER_MINUTE_STREAM: int = 30

    INIT_DB_ON_STARTUP: bool = False

    RUNNER_IMAGE: str = "business-runner:latest"
    # Where spawned runner containers reach this backend (conversation state,
    # /chat/completions proxy) — always the backend itself, never the real
    # LLM_GATEWAY_URL, so runner->gateway calls stay authenticated/anonymized
    # through the backend's own proxy.
    RUNNER_BACKEND_URL: str = "http://backend:8000"
    # Runner containers must be isolated from postgres/keycloak/clickhouse/minio
    # — they only ever need temporal, redis, backend and langfuse-server, all
    # of which are attached to this network (see docker-compose.yml).
    DOCKER_NETWORK: str = "aurum-ai_runner-net"
    RUNNER_MEMORY_LIMIT: int = 536870912  # 512 MB
    RUNNER_CPU_QUOTA: int = 100000
    # Reap runner containers with no chat activity for longer than this (seconds).
    RUNNER_IDLE_REAP_SECONDS: int = 1800  # 30 minutes
    # How often the background reaper scans for idle runner containers (seconds).
    RUNNER_REAP_INTERVAL_SECONDS: int = 300  # 5 minutes
    # Warm pool of unbound runner containers, claimed by get_or_create() for
    # brand-new conversations instead of creating (and waiting ~3-4s for) a
    # fresh one — see SessionManager.claim_from_pool / maintain_pool. 0
    # disables pooling entirely; cold-start-per-conversation still works
    # exactly as before.
    RUNNER_POOL_SIZE: int = 2
    RUNNER_POOL_PORT: int = 8080
    RUNNER_RUNTIME: Literal["docker", "kubernetes"] = "docker"
    KUBERNETES_RELEASE_NAME: str = "aurum-ai"
    KUBERNETES_RELEASE_NAMESPACE: str = "default"
    KUBERNETES_RUNNER_NAMESPACE: str = "aurum-ai-runners"
    KUBERNETES_RUNNER_SECRET_NAME: str = "aurum-ai-runner-secrets"
    KUBERNETES_RUNNER_IMAGE_PULL_POLICY: Literal["Always", "IfNotPresent", "Never"] = (
        "IfNotPresent"
    )
    KUBERNETES_RUNNER_IMAGE_PULL_SECRETS: tuple[str, ...] = ()
    KUBERNETES_RUNNER_READY_TIMEOUT_SECONDS: int = 60
    KUBERNETES_API_RETRY_ATTEMPTS: int = 3
    KUBERNETES_RUNNER_CPU_REQUEST: str = "100m"
    KUBERNETES_RUNNER_CPU_LIMIT: str = "1"
    KUBERNETES_RUNNER_MEMORY_REQUEST: str = "256Mi"
    KUBERNETES_RUNNER_MEMORY_LIMIT: str = "512Mi"
    # A runner session token (services/session_token.py, 30-minute TTL) is
    # only ever (re)issued on container create/pool-claim — a conversation
    # that stays active (updates last_active on every message, so the idle
    # reaper never kills it) can outlive the token by hours. get_or_create()
    # reissues + pushes a fresh token to the running container's own
    # POST /refresh-token once the current one is older than this.
    RUNNER_TOKEN_REFRESH_AFTER_SECONDS: int = Field(default=1200, ge=1)  # 20 minutes
    # Durable question waits receive no browser chat traffic, so token rotation
    # and last_active refresh run independently on this short cadence.
    INTERACTION_MAINTENANCE_INTERVAL_SECONDS: int = Field(default=30, ge=1)
    # Bounded hand-off after an answer/deadline; this never extends the
    # original question deadline itself.
    INTERACTION_SETTLEMENT_GRACE_SECONDS: int = Field(default=300, ge=1, le=86400)
    # Rollout kill switch. Backend places the value in each server-owned
    # turn signal and rejects only brand-new reservations while disabled;
    # idempotent retries and already-created question delivery remain valid.
    USER_QUESTIONS_ENABLED: bool = False
    # Operator kill switch for all web access, including the canonical
    # same-organization org_admin capability bypass. Default-deny until the
    # isolated web workloads and their egress enforcement are deployed.
    WEB_ENABLED: bool = False
    WEB_TOOLS_URL: str = "http://web-tools:8120"
    WEB_TOOLS_SERVICE_SECRET: SecretStr = SecretStr("")

    # Operator kill switch for image generation, editing and uploads. Image
    # tools also need the images.generate capability and an organization image
    # model; uploads need a chat model the organization marked as vision.
    IMAGES_ENABLED: bool = False
    # OpenAI-compatible Images API; empty means the chat gateway and its key.
    IMAGES_API_URL: str = ""
    IMAGES_API_KEY: SecretStr = SecretStr("")
    IMAGES_TIMEOUT_SECONDS: float = Field(default=120, gt=0, le=600)
    IMAGES_MAX_BYTES: int = Field(default=20 * 1024 * 1024, ge=1024, le=100 * 1024 * 1024)
    # Each upload is sent to vision models inline as base64, so it stays below
    # common provider per-image limits.
    IMAGES_UPLOAD_MAX_BYTES: int = Field(default=10 * 1024 * 1024, ge=1024, le=50 * 1024 * 1024)

    # Operator kill switch for the Hermes Agent surface (docs/HERMES_AGENT.md).
    # Users also need the agents.hermes capability. A Hermes session and its
    # tokens end at this absolute lifetime even if nothing closes them.
    HERMES_ENABLED: bool = False
    HERMES_SESSION_MAX_LIFETIME_SECONDS: int = Field(default=86400, ge=60, le=7 * 86400)
    # The most internet a Hermes sandbox may get through hermes-egress; the same
    # variable configures the egress guard. Organizations choose within it.
    HERMES_EGRESS_MAX_MODE: Literal["none", "packages", "public"] = "packages"
    # hermes-egress authenticates its per-connection checks with this secret;
    # only the business API needs it (see hermes_egress.require_egress_secret).
    HERMES_EGRESS_SERVICE_SECRET: SecretStr = SecretStr("")
    # Upper bounds for one native-tools completion request from a sandbox.
    HERMES_MAX_TOOLS: int = Field(default=128, ge=1, le=512)
    HERMES_MAX_MESSAGES: int = Field(default=1000, ge=1, le=10000)

    @field_validator("WEB_TOOLS_URL")
    @classmethod
    def _web_tools_origin_only(cls, value: str) -> str:
        return validated_web_tools_origin(value)
    # How often the pool maintainer tops the pool back up / reconciles it
    # against real container state (seconds).
    RUNNER_POOL_MAINTAIN_INTERVAL_SECONDS: int = 30
    # A runner container that Redis doesn't know about but that is still
    # running is only swept once it is older than this (seconds). Both
    # creation paths write their Redis bookkeeping *after* the container is
    # up and healthy, so a container younger than this may simply be one
    # mid-creation right now — the grace period is what keeps the sweep from
    # deleting it out from under its own creator.
    RUNNER_ORPHAN_GRACE_SECONDS: int = 600  # 10 minutes
    OTEL_EXPORTER_OTLP_ENDPOINT: str = Field(
        default="console", alias="OTEL_EXPORTER_OTLP_ENDPOINT"
    )

    TEMPORAL_HOST: str = "localhost:7233"
    TEMPORAL_NAMESPACE: str = "default"

    IDENTITY_SYNC_ENABLED: bool = False
    IDENTITY_SYNC_WAIT_SECONDS: float = Field(default=10, gt=0, le=60)

    LANGFUSE_ENABLED: bool = Field(default=False, alias="LANGFUSE_ENABLED")
    LANGFUSE_PUBLIC_KEY: str = Field(default="", alias="LANGFUSE_PUBLIC_KEY")
    LANGFUSE_SECRET_KEY: str = Field(default="", alias="LANGFUSE_SECRET_KEY")
    LANGFUSE_HOST: str = Field(default="http://localhost:3000", alias="LANGFUSE_HOST")
    LANGFUSE_CAPTURE_FULL_CONTENT: bool = Field(default=True, alias="LANGFUSE_CAPTURE_FULL_CONTENT")
    LANGFUSE_FLUSH_INTERVAL: int = Field(default=5, alias="LANGFUSE_FLUSH_INTERVAL")

    EVENT_SUBSCRIBE_TIMEOUT: int = 120  # seconds; SSE stream closes after this if no "done"

    # Multi-agent workflow product ceilings. Deployments may lower the graph
    # and concurrency bounds, but configuration must never raise them.
    WORKFLOW_MAX_NODES: int = Field(default=20, ge=1, le=20)
    WORKFLOW_MAX_PARALLELISM: int = Field(default=4, ge=1, le=4)
    WORKFLOW_DIAGNOSTIC_RETENTION_DAYS: int = Field(default=30, ge=1)
    WORKFLOW_RETENTION_SWEEP_INTERVAL_SECONDS: int = Field(default=3600, ge=1)

    # Background faithfulness/hallucination judge (see EvaluationWorkflow in
    # conversation-runner) — forwarded into spawned runner containers' env by
    # session_manager.py, not read by anything in this process directly.
    EVALUATOR_ENABLED: bool = Field(default=True, alias="EVALUATOR_ENABLED")
    EVALUATOR_MODEL: str = Field(
        default="",
        alias="EVALUATOR_MODEL",
    )

    # Code skill multi-round sandbox sessions (see CodeSkillSessionWorkflow in
    # conversation-runner) — same forwarding pattern as EVALUATOR_* above.
    CODE_SKILL_MAX_ROUNDS: int = Field(default=8, alias="CODE_SKILL_MAX_ROUNDS")
    CODE_SKILL_ROUND_TIMEOUT_SECONDS: float = Field(
        default=60.0, alias="CODE_SKILL_ROUND_TIMEOUT_SECONDS"
    )

    @property
    def keycloak_jwks_url(self) -> str:
        return f"{self.KEYCLOAK_URL}/realms/{self.KEYCLOAK_REALM}/protocol/openid-connect/certs"

    @property
    def keycloak_token_url(self) -> str:
        return f"{self.KEYCLOAK_URL}/realms/{self.KEYCLOAK_REALM}/protocol/openid-connect/token"


settings = Settings()
