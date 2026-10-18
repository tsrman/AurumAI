"""Asserts the two entrypoints (main_business.py, main_admin.py) mount the
right routes — see docs/ARCHITECTURE.md's routing matrix.
Importing either app triggers its module-level create_app(...) call, which
needs APP_SECRET_KEY set (same requirement as running the real app)."""

import os
import subprocess
import sys

import pytest

os.environ.setdefault("APP_SECRET_KEY", "x" * 32)


@pytest.mark.parametrize("entrypoint", ["main_admin", "main_business"])
def test_cold_entrypoint_resolves_all_orm_foreign_keys(entrypoint):
    # A shared pytest process imports business models and hides missing FK
    # targets in the independently deployed admin process. Resolve the real
    # metadata graph without making any database or external service calls.
    result = subprocess.run(
        [sys.executable, "-c", (
            f"import src.{entrypoint}\n"
            "from src.core.database import Base\n"
            "required = {'workflow_templates', 'workflow_definitions', 'web_sources'}\n"
            "assert required <= set(Base.metadata.tables)\n"
            "assert Base.metadata.sorted_tables\n"
        )],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def _routes(app) -> set[tuple[str, frozenset]]:
    """The (path, {method}) pairs an app actually serves.

    Reads the OpenAPI schema rather than walking app.routes: since FastAPI
    0.141 / Starlette 1.6 an included router stays a nested `_IncludedRouter`
    object with no public way in, so a scan of app.routes sees only
    /openapi.json, /docs and /redoc. The schema is public API and lists exactly
    the routing matrix this file exists to pin down — verified against live
    requests through ASGI (401/405/404 on the same paths).
    """
    return {
        (path, frozenset({method.upper()}))
        for path, operations in app.openapi()["paths"].items()
        for method in operations
    }


def test_health_on_both_apps():
    from src.main_business import app as business_app
    from src.main_admin import app as admin_app

    assert ("/health", frozenset({"GET"})) in _routes(business_app)
    assert ("/health", frozenset({"GET"})) in _routes(admin_app)


def test_user_app_has_no_admin_write_skill_routes():
    from src.main_business import app as business_app

    routes = _routes(business_app)
    paths = {path for path, _ in routes}
    assert "/v1/skills/" not in {p for p, m in routes if "POST" in m}
    assert "/v1/skills/code" not in paths
    assert "/v1/skills/discover-mcp-tools" not in paths
    assert "/v1/admin/groups/" not in paths
    assert "/v1/admin/authorization/grants" not in paths
    assert not any(path.startswith("/v1/admin/workflows") for path in paths)


def test_user_app_still_has_read_only_skills_and_chat():
    from src.main_business import app as business_app

    routes = _routes(business_app)
    assert ("/v1/skills/", frozenset({"GET"})) in routes
    assert ("/v1/skills/{skill_id}", frozenset({"GET"})) in routes
    assert ("/v1/chat/stream", frozenset({"POST"})) in routes
    assert ("/v1/documents", frozenset({"POST"})) in routes
    assert ("/v1/usage/me", frozenset({"GET"})) in routes
    assert ("/v1/agent-definitions", frozenset({"GET"})) in routes
    assert ("/v1/agent-definitions/{agent_id}/execution", frozenset({"GET"})) in routes
    assert ("/v1/workflows", frozenset({"GET"})) in routes
    assert ("/v1/workflow-runs/{run_id}", frozenset({"GET"})) in routes
    assert (
        "/v1/internal/workflow-runs/{run_id}/authorize-step",
        frozenset({"POST"}),
    ) in routes
    assert (
        "/v1/internal/turns/{turn_id}/executions/{execution_id}"
        "/web-operations/{invocation_id}",
        frozenset({"POST"}),
    ) in routes
    assert (
        "/v1/conversations/{conversation_id}/turns/{turn_id}/sources",
        frozenset({"GET"}),
    ) in routes


def test_admin_app_has_write_skill_and_group_routes():
    from src.main_admin import app as admin_app

    routes = _routes(admin_app)
    paths_with_post = {path for path, m in routes if "POST" in m}
    assert "/v1/skills/" in paths_with_post
    assert "/v1/skills/code" in paths_with_post
    assert "/v1/skills/discover-mcp-tools" in paths_with_post
    assert ("/v1/admin/groups/", frozenset({"GET"})) in routes
    assert ("/v1/admin/groups/{group_id}", frozenset({"PATCH"})) in routes
    assert ("/v1/admin/authorization/grants", frozenset({"POST"})) in routes
    assert ("/v1/admin/authorization/grants/reconcile", frozenset({"PUT"})) in routes
    assert ("/v1/agent-definitions", frozenset({"POST"})) in routes
    assert ("/v1/knowledge-bases", frozenset({"GET"})) in routes
    assert ("/v1/admin/workflows", frozenset({"POST"})) in routes
    assert ("/v1/admin/workflows/{workflow_id}", frozenset({"PUT"})) in routes
    assert ("/v1/admin/workflows/{workflow_id}", frozenset({"DELETE"})) in routes
    assert ("/v1/admin/workflow-runs/{run_id}", frozenset({"GET"})) in routes


def test_admin_app_has_no_chat_or_document_routes():
    from src.main_admin import app as admin_app

    paths = {path for path, _ in _routes(admin_app)}
    assert not any(p.startswith("/v1/chat") for p in paths)
    assert "/v1/documents" not in paths
    assert "/v1/settings/me" not in paths
    assert "/v1/usage/me" not in paths
    assert not any("web-operations" in path for path in paths)
    assert not any(path.endswith("/sources") for path in paths)


def test_templates_are_mounted_only_on_admin_app():
    from src.main_admin import app as admin_app
    from src.main_business import app as business_app

    expected = {
        ("/v1/admin/workflow-templates", frozenset({"GET"})),
        ("/v1/admin/workflow-templates", frozenset({"POST"})),
        ("/v1/admin/workflow-templates/{template_id}", frozenset({"GET"})),
        ("/v1/admin/workflow-templates/{template_id}", frozenset({"PUT"})),
        ("/v1/admin/workflow-templates/{template_id}/instantiate", frozenset({"POST"})),
        ("/v1/admin/workflow-templates/{template_id}/archive", frozenset({"POST"})),
    }
    assert expected <= _routes(admin_app)
    assert not any("workflow-templates" in path for path, _ in _routes(business_app))
