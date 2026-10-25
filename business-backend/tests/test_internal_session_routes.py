import importlib
import pkgutil
from collections.abc import Iterable
from typing import Any

from fastapi import APIRouter
from fastapi.routing import APIRoute

import src.routers
from src.core.auth import get_current_user_or_session
from src.core.dependencies import get_service_db_session
from src.core.hermes_auth import get_hermes_db_session, get_hermes_session_user


EXPECTED_INTERNAL_ROUTES = {
    ("chat", "/conversations/{conversation_id}/executions", "POST"),
    ("skills", "/", "GET"),
    ("skills", "/{skill_id}", "GET"),
    ("chat", "/conversations/{conversation_id}/messages", "POST"),
    ("chat", "/conversations/{conversation_id}/messages/{turn_id}/score", "PATCH"),
    ("chat", "/chat/completions", "POST"),
    ("chat", "/conversations/{conversation_id}", "GET"),
    ("documents", "/knowledge-bases/{knowledge_base_id}/search", "POST"),
    ("agent_definitions", "/agent-definitions/{agent_id}/execution", "GET"),
    ("workflow_runs_internal", "/workflow-runs/{run_id}/snapshot", "GET"),
    ("workflow_runs_internal", "/workflow-runs/{run_id}/authorize-step", "POST"),
    ("workflow_runs_internal", "/workflow-runs/{run_id}/transition", "POST"),
    ("workflow_runs_internal", "/workflow-runs/{run_id}/touch-activity", "POST"),
    ("questions_internal", "/turns/{turn_id}/fail", "POST"),
    ("questions_internal", "/turns/{turn_id}/executions", "POST"),
    ("questions_internal", "/executions/{execution_id}/questions", "POST"),
    ("questions_internal", "/questions/{question_id}", "GET"),
    ("questions_internal", "/questions/{question_id}/consume", "POST"),
    ("questions_internal", "/questions/{question_id}/expire", "POST"),
    (
        "web_tools_internal",
        "/turns/{turn_id}/executions/{execution_id}/web-operations/{invocation_id}",
        "POST",
    ),
    (
        "image_tools_internal",
        "/turns/{turn_id}/executions/{execution_id}/image-operations/{invocation_id}",
        "POST",
    ),
    (
        "workflow_runs_internal",
        "/workflow-runs/{run_id}/nodes/{phase_key}",
        "PUT",
    ),
    ("hermes_internal", "/hermes/sessions", "POST"),
    ("hermes_internal", "/hermes/sessions/{session_id}/turn", "PUT"),
    ("hermes_internal", "/hermes/sessions/{session_id}/turn", "DELETE"),
    ("hermes_internal", "/hermes/sessions/{session_id}/close", "POST"),
}

EXPECTED_HERMES_ROUTES = {("hermes", "/hermes/chat/completions", "POST")}


def _dependency_calls(dependencies: Iterable[Any]) -> set[Any]:
    calls: set[Any] = set()
    pending = list(dependencies)
    while pending:
        dependency = pending.pop()
        calls.add(dependency.call)
        pending.extend(dependency.dependencies)
    return calls


def _all_routers() -> dict[str, APIRouter]:
    """Every router module in src.routers, discovered rather than listed.

    A hand-kept list would make this test pass vacuously for a router someone
    forgets to add — which is the one failure mode it exists to prevent, since
    the whole point is catching a *new* session-reachable surface.
    """
    routers: dict[str, APIRouter] = {}
    for module_info in pkgutil.iter_modules(src.routers.__path__):
        module = importlib.import_module(f"src.routers.{module_info.name}")
        router = getattr(module, "router", None)
        if isinstance(router, APIRouter):
            routers[module_info.name] = router
    return routers


def test_internal_session_route_matrix_uses_session_auth_and_org_scoped_db() -> None:
    actual: set[tuple[str, str, str]] = set()
    routers = _all_routers()
    assert "skills" in routers and "chat" in routers, "router discovery found nothing"

    for router_name, router in routers.items():
        for route in router.routes:
            if not isinstance(route, APIRoute):
                continue
            calls = _dependency_calls(route.dependant.dependencies)
            if get_current_user_or_session not in calls:
                continue
            for method in route.methods:
                actual.add((router_name, route.path, method))
            assert get_service_db_session in calls, (
                f"{router_name} {route.path} accepts a session token without "
                "the org-scoped service DB dependency"
            )

    assert actual == EXPECTED_INTERNAL_ROUTES


def test_hermes_token_routes_are_exactly_the_sandbox_surface() -> None:
    actual: set[tuple[str, str, str]] = set()
    for router_name, router in _all_routers().items():
        for route in router.routes:
            if not isinstance(route, APIRoute):
                continue
            calls = _dependency_calls(route.dependant.dependencies)
            if get_hermes_session_user not in calls:
                continue
            for method in route.methods:
                actual.add((router_name, route.path, method))
            assert get_hermes_db_session in calls
            assert get_current_user_or_session not in calls

    assert actual == EXPECTED_HERMES_ROUTES
