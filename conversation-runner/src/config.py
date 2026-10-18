from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # None at boot in pool mode: the container starts generic/unbound and
    # POST /bind (see src/pool.py) fills these in — and the rest of the
    # codebase — once assigned to a real conversation. Set from env at boot
    # (legacy/direct mode, e.g. the cold-start fallback path) means the
    # worker starts immediately without waiting for a bind call.
    conversation_id: str | None = None
    user_id: str | None = None
    org_id: str | None = None
    temporal_host: str = "localhost:7233"
    temporal_namespace: str = "default"
    redis_url: str = "redis://localhost:6379/0"
    llm_gateway_url: str = "http://localhost:8000"
    session_token: str | None = None
    task_queue: str | None = None
    idle_ttl_seconds: int = 600
    otel_exporter_otlp_endpoint: str = "http://localhost:4317"
    LANGFUSE_ENABLED: bool = False
    LANGFUSE_PUBLIC_KEY: str = ""
    LANGFUSE_SECRET_KEY: str = ""
    LANGFUSE_HOST: str = "http://localhost:3000"
    LANGFUSE_CAPTURE_FULL_CONTENT: bool = True
    LANGFUSE_FLUSH_INTERVAL: int = 5
    evaluator_enabled: bool = True
    evaluator_model: str = ""
    # Guardrails for CodeSkillSessionWorkflow's multi-round sandbox loop —
    # bounds how many commands a code skill can run and how long any single
    # command may take before the round reports back "timed out" instead of
    # hanging the whole session (see skill-executor's own session_ttl_seconds
    # for the independent, service-level teardown backstop).
    code_skill_max_rounds: int = 8
    code_skill_round_timeout_seconds: float = 60.0
    # How long a human-in-the-loop confirmation-gated node waits for an
    # approve/reject signal before the run fails with confirmation_timeout.
    # Deliberately bounded — an unbounded wait would tie up this
    # conversation's runner container indefinitely. Matches
    # RUNNER_IDLE_REAP_SECONDS' default 1800s; multi_agent.py refreshes this
    # conversation's runner activity when a node starts waiting so the two
    # timers don't race each other.
    workflow_confirmation_timeout_seconds: float = 1800.0

    @field_validator("idle_ttl_seconds")
    @classmethod
    def _validate_idle_ttl_seconds(cls, value: int) -> int:
        if value < 0:
            raise ValueError("idle_ttl_seconds must be >= 0 (0 = no timeout)")
        return value


settings = Settings()
