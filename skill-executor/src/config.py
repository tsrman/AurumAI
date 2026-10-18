from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SKILL_EXECUTOR_",
        env_file=".env",
        extra="ignore",
    )

    bind_host: str = "0.0.0.0"
    bind_port: int = 8090

    sandbox_image: str = "skill-sandbox:latest"
    runtime: Literal["docker", "kubernetes"] = "docker"
    kubernetes_release_name: str = "aurum-ai"
    kubernetes_release_namespace: str = "default"
    kubernetes_namespace: str = "aurum-ai-sandboxes"
    kubernetes_ready_timeout_seconds: int = 60
    kubernetes_api_retry_attempts: int = 3
    kubernetes_image_pull_policy: Literal["Always", "IfNotPresent", "Never"] = "IfNotPresent"
    kubernetes_image_pull_secrets: tuple[str, ...] = ()
    kubernetes_cpu_request: str = "100m"
    kubernetes_cpu_limit: str = "500m"
    kubernetes_memory_request: str = "128Mi"
    kubernetes_memory_limit: str = "256Mi"
    # "none" (default, no network at all) or a real Docker network name for
    # skills an admin has explicitly marked network_policy="egress" — never
    # controllable by the skill bundle or the LLM itself.
    sandbox_network_default: str = "none"
    sandbox_memory_limit: int = 268_435_456  # 256 MB — short scripts, not a chat session
    sandbox_cpu_quota: int = 50_000  # 0.5 CPU (Docker CpuQuota is per 100_000us period)
    sandbox_pids_limit: int = 128
    # /skill is tmpfs, not the container's overlay writable layer: the
    # bundle is one-shot content for a one-shot container anyway, and this
    # keeps it from ever touching host disk (a stronger guarantee than
    # relying on cleanup always running — see reap_orphans) plus faster I/O.
    # 128 MB comfortably covers bundle_max_bytes (50 MB) plus scratch files a
    # skill's own commands create, while leaving headroom under the 256 MB
    # container memory cap (tmpfs usage counts against it).
    sandbox_skill_tmpfs_mb: int = 128

    # Two independent guarantees a sandbox container always dies even if the
    # caller never calls DELETE /sessions/{id} (crash, network partition,
    # etc.): an absolute age cap, and an idle-since-last-exec cap.
    # Sized to fit CodeSkillSessionWorkflow's worst case, not the other way
    # around: up to CODE_SKILL_MAX_ROUNDS=8 rounds of (LLM call ≤120s +
    # exec ≤60s) ≈ 24 min absolute, and a single slow LLM round (≤120s)
    # between execs must not trip the idle cap.
    session_ttl_seconds: int = 1800
    session_idle_ttl_seconds: int = 300
    reap_interval_seconds: int = 15

    exec_timeout_seconds: int = 60
    # Per-stream cap on collected stdout/stderr: the result must fit in a
    # Temporal activity payload (4MB gRPC hard cap), a Redis event, and the
    # next LLM prompt — an unbounded `yes`-style command must degrade into a
    # truncated result, not a poison payload that fails the activity forever.
    exec_output_max_bytes: int = 64 * 1024
    bundle_fetch_timeout_seconds: int = 30
    bundle_max_bytes: int = 50 * 1024 * 1024
    # Mirrors business-backend/src/core/config.py's SKILL_BUNDLE_MAX_* —
    # defense in depth: a bundle that reached this
    # point some other way (direct SQL/MinIO tampering) still gets the same
    # zip-bomb checks upload-time validation already applies.
    bundle_max_entries: int = 2000
    bundle_max_uncompressed_bytes: int = 200 * 1024 * 1024
    bundle_max_compression_ratio: int = 10

    s3_endpoint: str | None = None
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_bucket: str = "aurum-ai-business"
    s3_region: str = "us-east-1"


settings = Settings()
