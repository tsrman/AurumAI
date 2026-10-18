from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_script(filename: str, module_name: str) -> ModuleType:
    path = Path(__file__).parents[1] / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_quota_smoke_grants_the_isolated_org_chat_and_model_access() -> None:
    source = (Path(__file__).parents[2] / "smoke-test-quota.sh").read_text()
    grant_function = source.index("grant_model_access()")
    grant_call = source.index('grant_model_access "$org_id" "$MODEL_ID"')
    token_issue = source.index("session_token_service.issue")

    assert grant_function < grant_call < token_issue
    assert "INSERT INTO access_grants" in source[grant_function:grant_call]
    assert "INSERT INTO organization_capabilities" in source[grant_function:grant_call]
    assert "'chat.send'" in source[grant_function:grant_call]
    assert "'model'" in source[grant_function:grant_call]
    assert 'model_id="$2"' in source[grant_function:grant_call]
    assert "'organization'" in source[grant_function:grant_call]
    assert "'read'" in source[grant_function:grant_call]


def _request(
    app: Any,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    async def invoke() -> tuple[int, dict[str, str], bytes]:
        body = json.dumps(payload).encode() if payload is not None else b""
        request_sent = False
        response_complete = asyncio.Event()
        sent: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            await response_complete.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)
            if message["type"] == "http.response.body" and not message.get(
                "more_body", False
            ):
                response_complete.set()

        headers = [(b"content-type", b"application/json")]
        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": method,
                "scheme": "http",
                "path": path,
                "raw_path": path.encode(),
                "query_string": b"",
                "headers": headers,
                "client": ("test", 1234),
                "server": ("test", 80),
            },
            receive,
            send,
        )
        start = next(item for item in sent if item["type"] == "http.response.start")
        response_headers = {
            key.decode().lower(): value.decode()
            for key, value in start.get("headers", [])
        }
        response_body = b"".join(
            item.get("body", b"")
            for item in sent
            if item["type"] == "http.response.body"
        )
        return int(start["status"]), response_headers, response_body

    return asyncio.run(invoke())


def test_gateway_streams_fixed_usage_and_done_frame() -> None:
    app = _load_script("quota-test-gateway.py", "quota_test_gateway").app

    health_status, _, health_body = _request(app, "GET", "/health")
    models_status, _, models_body = _request(app, "GET", "/v1/models")
    status, headers, body = _request(
        app,
        "POST",
        "/v1/chat/completions",
        {
            "model": "quota-e2e-model",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
        },
    )

    assert health_status == 200
    assert json.loads(health_body) == {"status": "ok"}
    assert models_status == 200
    assert [item["id"] for item in json.loads(models_body)["data"]] == [
        "quota-e2e-model"
    ]
    assert status == 200
    assert headers["content-type"].startswith("text/event-stream")
    frames = [frame for frame in body.decode().split("\n\n") if frame]
    assert frames[-1] == "data: [DONE]"
    chunks = [json.loads(frame.removeprefix("data: ")) for frame in frames[:-1]]
    assert any(chunk.get("choices", [{}])[0].get("delta", {}).get("content") for chunk in chunks if chunk.get("choices"))
    assert chunks[-1] == {
        "choices": [],
        "usage": {
            "prompt_tokens": 7,
            "completion_tokens": 5,
            "total_tokens": 12,
        },
    }


def test_gateway_has_controlled_upstream_failure() -> None:
    app = _load_script("quota-test-gateway.py", "quota_test_gateway_error").app

    status, _, body = _request(
        app,
        "POST",
        "/v1/chat/completions",
        {
            "model": "quota-e2e-model",
            "messages": [
                {"role": "user", "content": "quota-e2e-upstream-error"}
            ],
            "stream": True,
        },
    )

    assert status == 503
    assert json.loads(body) == {
        "error": {"message": "deterministic upstream failure"}
    }


def test_gateway_has_controlled_success_without_usage_frame() -> None:
    app = _load_script("quota-test-gateway.py", "quota_test_gateway_no_usage").app

    status, _, body = _request(
        app,
        "POST",
        "/v1/chat/completions",
        {
            "model": "quota-e2e-model",
            "messages": [
                {"role": "user", "content": "quota-e2e-no-usage"}
            ],
            "stream": True,
        },
    )

    assert status == 200
    frames = [frame for frame in body.decode().split("\n\n") if frame]
    assert frames[-1] == "data: [DONE]"
    chunks = [json.loads(frame.removeprefix("data: ")) for frame in frames[:-1]]
    assert all(chunk.get("usage") is None for chunk in chunks)


def test_backend_test_app_mounts_real_quota_routes(monkeypatch: Any) -> None:
    monkeypatch.setenv("APP_DEBUG", "true")
    monkeypatch.setenv(
        "APP_DATABASE_URL",
        "postgresql+asyncpg://aurum_ai_app:aurum_ai_app@127.0.0.1:1/aurum_ai",
    )
    monkeypatch.setenv("APP_SECRET_KEY", "quota-e2e-contract-secret")
    monkeypatch.setenv("APP_KEYCLOAK_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("APP_LLM_GATEWAY_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("APP_GATEWAY_API_KEY", "quota-e2e-test")
    app = _load_script("quota-test-app.py", "quota_test_app").app

    health_status, _, _ = _request(app, "GET", "/health")
    usage_status, _, _ = _request(app, "GET", "/v1/usage/me")
    proxy_status, _, _ = _request(
        app,
        "POST",
        "/v1/chat/completions",
        {"model": "quota-e2e-model", "messages": [], "stream": True},
    )

    assert health_status == 200
    assert usage_status == 401
    assert proxy_status == 401


def test_quota_smoke_covers_admin_authorization_and_new_contracts() -> None:
    source = (Path(__file__).parents[2] / "smoke-test-quota.sh").read_text()

    assert 'ADMIN_CLIENT_ID="${ADMIN_CLIENT_ID:-admin-ui}"' in source
    assert 'ADMIN_USER="${ADMIN_USER:-admin}"' in source
    assert 'MEMBER_USER="${TEST_USER:-user}"' in source
    assert "quota-test-admin-app.py" in source
    assert '"$admin_url/v1/admin/analytics/usage' in source
    assert '"$admin_url/v1/admin/models"' in source
    assert '"$backend_url/v1/settings/me"' in source
    assert 'assert_status "$status" 403 "member admin analytics denial"' in source
    assert 'assert_status "$status" 401 "anonymous admin analytics denial"' in source
    assert "unaccountedRequests" in source
    # The monthly quota block must be exercised too — "remaining" derived from
    # the report's own period was the bug it replaced.
    assert '"requestsRemaining": 7' in source
    assert 'd["byUserTotal"] == len(d["byUser"])' in source
    assert "foreign-analytics-marker" in source


def test_admin_test_app_mounts_real_admin_routes_and_denies_anonymous(
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("APP_DEBUG", "true")
    monkeypatch.setenv(
        "APP_DATABASE_URL",
        "postgresql+asyncpg://aurum_ai_app:aurum_ai_app@127.0.0.1:1/aurum_ai",
    )
    monkeypatch.setenv("APP_SECRET_KEY", "quota-e2e-contract-secret")
    monkeypatch.setenv("APP_KEYCLOAK_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("APP_KEYCLOAK_CLIENT_ID", "admin-ui")
    monkeypatch.setenv("APP_LLM_GATEWAY_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("APP_GATEWAY_API_KEY", "quota-e2e-test")
    app = _load_script("quota-test-admin-app.py", "quota_test_admin_app").app

    for path in (
        "/v1/admin/models",
        "/v1/admin/users",
        "/v1/admin/analytics/usage",
    ):
        response_status, _, _ = _request(app, "GET", path)
        assert response_status == 401


def test_quota_smoke_defaults_to_the_compose_postgres_image_with_partman() -> None:
    import re

    import yaml

    root = Path(__file__).parents[2]
    source = (root / "smoke-test-quota.sh").read_text()
    compose = yaml.safe_load((root / "docker-compose.yml").read_text())
    project_image = compose["services"]["postgres"]["image"]
    default = re.search(r'POSTGRES_IMAGE="\$\{QUOTA_E2E_POSTGRES_IMAGE:-([^}]+)\}"', source)
    assert default is not None
    # The partition migration requires pg_partman, which only the project image installs.
    assert default.group(1) == project_image
    assert "partman" in (root / "infra/postgres/Dockerfile").read_text()
    image_check = source.index('docker image inspect "$POSTGRES_IMAGE"')
    assert image_check < source.index('"$POSTGRES_IMAGE" >/dev/null')
    assert "docker compose build postgres" in source[image_check:image_check + 400]
