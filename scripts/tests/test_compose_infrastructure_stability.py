from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# Files that document or run Compose commands against an already running stack.
COMMAND_SOURCES = [
    *sorted(ROOT.glob("smoke-test*.sh")),
    ROOT / "AGENTS.md",
    ROOT / "README.md",
    ROOT / "docs/TESTING.md",
    ROOT / "docs/ARCHITECTURE.md",
]
UP_OPTIONS_WITH_VALUES = {"--wait-timeout", "--timeout", "-t", "--scale", "--pull", "--exit-code-from", "--attach"}


def _commands(text: str) -> list[str]:
    joined = re.sub(r"\\\n\s*", " ", text)
    return [line.strip() for line in joined.splitlines() if "docker compose" in line or "${WEB[@]}" in line]


def _targeted_up_arguments(command: str) -> list[str] | None:
    try:
        tokens = shlex.split(command, comments=False)
    except ValueError:
        return None
    if "up" not in tokens:
        return None
    arguments = tokens[tokens.index("up") + 1:]
    services, skip = [], False
    for token in arguments:
        if skip:
            skip = False
            continue
        if token in UP_OPTIONS_WITH_VALUES:
            skip = True
            continue
        if token.startswith("-") or token in {"&&", "||", ";", "|"}:
            if token in {"&&", "||", ";", "|"}:
                break
            continue
        services.append(token)
    return arguments if services else None


def test_targeted_up_never_rebuilds_running_infrastructure() -> None:
    # `up --build <services>` also rebuilds dependencies that have a build
    # section (the postgres image), and Compose then recreates them under a
    # running Keycloak/Temporal. Build the named images first and start them
    # with --no-build (or --no-deps) instead.
    offenders = []
    for path in COMMAND_SOURCES:
        for command in _commands(path.read_text()):
            arguments = _targeted_up_arguments(command)
            if arguments is None:
                continue
            if "--build" in arguments and "--no-deps" not in arguments:
                offenders.append(f"{path.relative_to(ROOT)}: {command[:120]}")
    assert offenders == []


def test_keycloak_validates_pooled_connections_before_use(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("APP_SECRET_KEY=stability-contract\nKEYCLOAK_ADMIN_PASSWORD=stability-contract\n")
    result = subprocess.run(
        ["docker", "compose", "--env-file", str(env_file), "-f", str(ROOT / "docker-compose.yml"), "config", "--format", "json"],
        env={key: value for key, value in os.environ.items() if key not in {"APP_SECRET_KEY", "KEYCLOAK_ADMIN_PASSWORD"}},
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    keycloak = json.loads(result.stdout)["services"]["keycloak"]["environment"]
    # Without foreground validation a Postgres restart leaves dead pooled
    # connections and token requests fail with HTTP 500 until eviction.
    assert keycloak["QUARKUS_DATASOURCE_JDBC_FOREGROUND_VALIDATION_INTERVAL"] == "1S"


def _e2e_backend_environment(tmp_path: Path, extra: str = "") -> dict[str, dict[str, str]]:
    env_file = tmp_path / ".env"
    env_file.write_text("APP_SECRET_KEY=stability-contract\nKEYCLOAK_ADMIN_PASSWORD=stability-contract\n" + extra)
    result = subprocess.run(
        ["docker", "compose", "--env-file", str(env_file), "-f", str(ROOT / "docker-compose.yml"),
         "-f", str(ROOT / "docker-compose.e2e.yml"), "config", "--format", "json"],
        env={key: value for key, value in os.environ.items() if not key.startswith("APP_RATE_LIMIT")},
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    return {name: services[name]["environment"] for name in ("backend", "backend-admin")}


def test_deterministic_overlay_raises_rate_limits_for_sequential_smoke_tests(tmp_path: Path) -> None:
    # One user drives several smoke scenarios within a minute; the per-user
    # counter is shared with /stream (limit 30), so the defaults reject chats.
    for environment in _e2e_backend_environment(tmp_path).values():
        assert environment["APP_RATE_LIMIT_PER_MINUTE"] == "3000"
        assert environment["APP_RATE_LIMIT_PER_MINUTE_STREAM"] == "500"


def test_deterministic_overlay_keeps_explicit_wrapper_rate_limits(tmp_path: Path) -> None:
    extra = "APP_RATE_LIMIT_PER_MINUTE=1234\nAPP_RATE_LIMIT_PER_MINUTE_STREAM=56\n"
    for environment in _e2e_backend_environment(tmp_path, extra).values():
        assert environment["APP_RATE_LIMIT_PER_MINUTE"] == "1234"
        assert environment["APP_RATE_LIMIT_PER_MINUTE_STREAM"] == "56"


def test_long_running_services_all_come_back_after_a_docker_restart(tmp_path: Path) -> None:
    # A worker that restarts on its own while its database, Temporal or
    # Milvus stay down only loops on connection errors (rag-api after a host
    # reboot): every long-running service shares one policy. One-shot jobs
    # keep their bounded retries, and build-only images never run.
    env_file = tmp_path / ".env"
    env_file.write_text("APP_SECRET_KEY=stability-contract\nKEYCLOAK_ADMIN_PASSWORD=stability-contract\n")
    profiles = ["--profile", "*"]
    result = subprocess.run(
        ["docker", "compose", "--env-file", str(env_file), "-f", str(ROOT / "docker-compose.yml"), *profiles,
         "config", "--format", "json"],
        env={key: value for key, value in os.environ.items() if key not in {"APP_SECRET_KEY", "KEYCLOAK_ADMIN_PASSWORD"}},
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    one_shot = {"temporal-admin-tools", "temporal-create-namespace", "rag-migrate"}
    build_only = {"runner", "skill-sandbox"}
    policies = {name: service.get("restart") for name, service in json.loads(result.stdout)["services"].items()}

    assert {name: policy for name, policy in policies.items() if name not in one_shot | build_only} == {
        name: "unless-stopped" for name in policies if name not in one_shot | build_only
    }
    assert policies["rag-migrate"] in (None, "no")
